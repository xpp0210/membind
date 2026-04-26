# MemBind 优化改进方案

> 版本: 0.2.0 → 0.3.0 | 生成时间: 2026-04-26 | 作者: 安宝

## 一、项目现状概览

| 指标 | 数值 |
|------|------|
| 业务代码 | 3,351 行 Python（不含 tests） |
| 测试代码 | 2,594 行（13 个测试文件） |
| HTTP API | 18 个端点 |
| MCP 工具 | 11 个 |
| 数据表 | 8 张（含 2 张虚拟表） |
| 向量维度 | 1024（BAAI/bge-m3） |
| 存储 | SQLite WAL + sqlite-vec + FTS5 |
| LLM | 智谱 glm-4-flash（标签提取 + 矛盾判断） |

### 项目结构

```
membind/
├── api/            # HTTP API 层（7 个路由文件）
├── core/           # 核心业务逻辑（8 个模块）
├── db/             # 数据库层（connection + init.sql + migrate）
├── models/         # Pydantic 模型（3 个文件）
├── services/       # 服务层（binding_service）
├── mcp_server.py   # MCP 协议入口（11 个工具）
├── server.py       # FastAPI 应用入口
└── config.py       # 配置管理
```

---

## 二、问题诊断

### 2.1 严重问题（P0 — 必须修复）

#### P0-1: 写入逻辑三处重复

**问题**: 同一个"写入记忆"逻辑在三个地方各自实现了一遍，总计约 150 行重复代码：

| 位置 | 文件 | 行数 | 差异 |
|------|------|------|------|
| HTTP write API | `api/write.py:64-94` | 30 行 | 完整版：标签 + 冲突预检 + 存储 |
| MCP _write | `mcp_server.py:173-218` | 45 行 | 完整版：标签 + 快速冲突 + 簇分配 + 存储 |
| Conversation _store | `api/conversation.py:67-106` | 40 行 | 简化版：无冲突检测 |

三处都执行相同的步骤：生成 embedding → INSERT memories → INSERT context_tags → INSERT memories_vec。但差异导致行为不一致：

- HTTP 版有完整冲突预检（调 LLM），MCP 版只做快速向量比对
- MCP 版额外做了簇分配（`cluster_manager.assign_cluster`），HTTP 版和 Conversation 版没有
- Conversation 版缺少 namespace 传递到簇分配

**影响**: 修一个 bug 要改三处，且三处行为已经不一致。

#### P0-2: 余弦相似度四份拷贝

**问题**: `_cosine_similarity` 在以下位置各实现了一次：

| 文件 | 行号 | 签名 |
|------|------|------|
| `core/conflict.py` | 187-195 | `(a: list[float], b: list[float]) -> float` |
| `core/conversation.py` | 177-186 | 同上 |
| `core/cluster.py` | 213-223 | 同上 |
| `core/cluster.py` | 207-211 | `_cosine_similarity_blob(blob, vec_b)` — BLOB 版 |

其中 `conflict.py` 和 `conversation.py` 的实现完全相同（纯 Python 版），`cluster.py` 有一个 BLOB 适配层 + 一个 float 版。

**影响**: ~60 行重复代码。如果将来要换 numpy 加速或修正精度 bug，要改四处。

#### P0-3: init.sql chunks 表重复定义

**问题**: `db/init.sql` 中 chunks 表被定义了两次：

```sql
-- 第 81-94 行（正确版本，编号 5）
CREATE TABLE IF NOT EXISTS chunks (...);

-- 第 106-123 行（重复版本，编号又写了 6）
CREATE TABLE IF NOT EXISTS chunks(...);
```

第二次定义虽然因为 `IF NOT EXISTS` 不会报错，但索引也是重复的（`idx_chunks_session`、`idx_chunks_turn`、`idx_chunks_memory` 各创建两次），且编号 6 被错用导致 `memories_vec` 实际编号是 7。

**影响**: 混淆、误导维护者。索引重复创建不报错但不干净。

---

### 2.2 中等问题（P1 — 应当修复）

#### P1-1: EmbeddingGenerator 每次调用新建 httpx.AsyncClient

**位置**: `core/writer.py:202`、`core/writer.py:224`

```python
async with httpx.AsyncClient(timeout=30.0) as client:  # 每次都新建
    resp = await client.post(...)
```

**问题**: 每次生成 embedding 都创建新的 TCP 连接，没有连接复用。批量写入场景（conversation API 一次提取 3-5 条记忆）会连续创建 3-5 个短连接。

**方案**: 改为模块级或实例级复用 `httpx.AsyncClient`，配合 `lifespan` 事件管理生命周期。

#### P1-2: 冲突检测 O(N²) 全量加载

**位置**: `core/conflict.py:47-52`（`detect_write_conflict`）和 `core/conflict.py:108-114`（`detect_write_conflict_fast`）

```python
rows = conn.execute("""
    SELECT m.id, m.content, v.embedding
    FROM memories m JOIN memories_vec v ON v.id = m.id
    WHERE m.is_deleted = 0
""").fetchall()  # 加载全部 embedding！
```

**问题**: 每次写入都把所有活跃记忆的 embedding 加载到内存做余弦相似度比对。1024 维 float32 = 4KB/条，1 万条记忆 = 40MB 内存 + N 次余弦计算。

**方案**: 先用 sqlite-vec 的 `memories_vec` 做 KNN 查询取 Top-10，只对 Top-10 做精确余弦比对（而非全量）。`detect_write_conflict_fast` 已经是"快速版"但实现上还是全量加载。

#### P1-3: 冲突检测的 LLM 调用串行

**位置**: `core/conflict.py:60-91`

```python
for row in rows:
    ...
    llm_result = await self._check_contradiction_llm(new_content, mem_content)
```

**问题**: 如果有 5 条高相似记忆需要 LLM 判断，串行调用 5 次 glm-4-flash（每次 ~2s），总计 ~10s。

**方案**: 用 `asyncio.gather` 并行调用。

#### P1-4: FTS5 触发器时序问题

**位置**: `db/init.sql:158-163`

```sql
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(memory_id, content, scene, entities)
    SELECT NEW.id, NEW.content,
           COALESCE((SELECT scene FROM context_tags WHERE memory_id = NEW.id), ''),
           ...
END;
```

**问题**: `AFTER INSERT ON memories` 触发时，`context_tags` 中可能还没有该记忆的标签（因为 tags 是在另一个 INSERT 中写入的，事务提交顺序不确定）。结果是 FTS5 索引中 scene/entities 永远为空。

**证据**: `db/connection.py:117-126` 的 `_init_fts` 有一个补录逻辑，专门从 `context_tags` 补数据到 `memories_fts`——这正是因为触发器时序不对。

#### P1-5: lifecycle API 传了 namespace 但 core 层没接收

**位置**: `api/lifecycle.py:24` → `core/lifecycle.py:16`

```python
# API 层
return lifecycle_manager.decay_all(dry_run=dry_run, namespace=namespace)

# core 层
def decay_all(self, dry_run: bool = False) -> dict:  # ← 没有 namespace 参数！
```

**问题**: `decay_all()` 和 `cleanup()` 和 `get_decay_candidates()` 都没有 `namespace` 参数，所有 namespace 的记忆会被一起衰减/清理。

#### P1-6: `_get` 查询了不存在的表

**位置**: `mcp_server.py:261-262`

```python
(SELECT COUNT(*) FROM binding_records br WHERE br.memory_id = m.id) as binding_count,
(SELECT COUNT(*) FROM binding_records br WHERE br.memory_id = m.id AND br.feedback = 1) as hit_count
```

**问题**: 查询了 `binding_records` 表，但实际表名是 `binding_history`。这会导致 `_get` 工具运行时报错。

#### P1-7: admin feedback 接口用 dict 接收请求体

**位置**: `api/admin.py:19`

```python
async def feedback(body: dict, request: Request):
    memory_id = body.get("memory_id", "")
```

**问题**: 没有 Pydantic 模型校验，`memory_id` 缺失时返回 `{"error": ...}` 而非 422。同理 `api/recall.py:22-29` 的 `RecallRequest` 是手写 `__init__` 而非 Pydantic 模型。

---

### 2.3 轻微问题（P2 — 建议改进）

#### P2-1: httpx.AsyncClient 未统一管理

涉及文件：`core/writer.py`（2 处）、`core/conflict.py`（2 处）、`core/conversation.py`（1 处）、`core/merger.py`（1 处）

总计 6 处各自 `async with httpx.AsyncClient(...) as client`，无连接池复用。

#### P2-2: cluster_manager 在 recall 中每次 new

**位置**: `core/retriever.py:102-103`

```python
from core.cluster import ClusterManager
cluster_mgr = ClusterManager()  # 每次 recall 都新建实例
```

应使用模块级单例 `cluster_manager`（已在 `core/cluster.py:256` 导出）。

#### P2-3: mcp_server.py 重复 import

**位置**: `mcp_server.py:28-29`

```python
from core.cluster import cluster_manager
from core.cluster import cluster_manager  # 重复！
```

#### P2-4: `_store_memory` 中用 `__import__("json")`

**位置**: `api/conversation.py:80`、`api/write.py:69`

```python
entities_json = __import__("json").dumps(entities, ensure_ascii=False)
```

应在文件顶部 `import json`。

#### P2-5: get_neighbors 全量加载 session chunks

**位置**: `core/chunk_store.py:73-78`

```python
rows = conn.execute(
    """SELECT * FROM chunks
       WHERE session_key = ? AND is_deleted = 0
       ORDER BY created_at, seq""",
    (session_key,)
).fetchall()  # 加载该 session 所有 chunks
```

当 session 很大时（如持续对话几百轮），这是不必要的全量加载。应改为 SQL 定位 + LIMIT/OFFSET。

#### P2-6: `datetime.utcnow()` 已废弃

涉及文件：`api/write.py:67`、`api/conversation.py:79`、`core/conflict.py:247`、`core/merger.py:48`、`api/conflict.py:97,144`

Python 3.12+ 中 `datetime.utcnow()` 已标记为 deprecated，应使用 `datetime.now(timezone.utc)`。

---

## 三、优化方案

### P0: 消除重复（预计 2h，减少 ~300 行代码）

#### P0-1: 统一 MemoryWriter

**目标**: 创建 `core/memory_writer.py`，统一所有写入路径。

```python
# core/memory_writer.py（新建）
"""统一的记忆写入器"""

import json
import struct
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass

from config import settings
from core.writer import ContextTagger, EmbeddingGenerator
from core.conflict import conflict_detector
from core.cluster import cluster_manager
from db.connection import get_connection


@dataclass
class WriteResult:
    memory_id: str
    content: str
    scene: str
    importance: float
    conflict_warnings: list[dict]
    cluster_id: str | None = None


class MemoryWriter:
    """统一写入器：标签 → embedding → 冲突预检 → 存储 → 簇分配"""

    def __init__(self):
        self.tagger = ContextTagger()
        self.embedder = EmbeddingGenerator()

    async def write(
        self,
        content: str,
        namespace: str = "default",
        importance: float | None = None,
        hint_context: dict | None = None,
        conflict_mode: str = "fast",  # "full" | "fast" | "none"
    ) -> WriteResult:
        # 1. 标签
        tag = await self.tagger.tag(content, hint_context=hint_context)
        if importance is not None:
            tag.importance = importance

        # 2. Embedding
        embedding = await self.embedder.generate(content)

        # 3. 冲突预检
        conflict_warnings = []
        if conflict_mode != "none" and embedding and any(v != 0.0 for v in embedding):
            if conflict_mode == "full":
                result = await conflict_detector.detect_write_conflict(content, embedding)
                if result.check in ("conflict", "similar"):
                    conflict_warnings = [
                        {"memory_id": c.memory_id_b, "similarity": c.similarity,
                         "reason": c.reason, "resolution": c.resolution}
                        for c in result.conflicts
                    ]
            else:  # fast
                result = await conflict_detector.detect_write_conflict_fast(content, embedding)
                conflict_warnings = result.get("conflicts", [])

        # 4. 存储
        memory_id, cluster_id = self._store(
            content, tag, embedding, namespace
        )

        return WriteResult(
            memory_id=memory_id,
            content=content,
            scene=tag.scene,
            importance=tag.importance,
            conflict_warnings=conflict_warnings,
            cluster_id=cluster_id,
        )

    def _store(self, content, tag, embedding, namespace) -> tuple[str, str | None]:
        memory_id = uuid.uuid4().hex[:16]
        tag_id = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc).isoformat()
        entities_json = json.dumps(tag.entities, ensure_ascii=False)

        cluster_id = None
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO memories (id, content, importance, metadata, created_at, updated_at, namespace)
                   VALUES (?, ?, ?, '{}', ?, ?, ?)""",
                (memory_id, content, tag.importance, now, now, namespace),
            )
            conn.execute(
                """INSERT INTO context_tags (id, memory_id, scene, task_type, entities, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (tag_id, memory_id, tag.scene, tag.task_type, entities_json, now),
            )
            if embedding and any(v != 0.0 for v in embedding):
                vec_bytes = struct.pack(f"{len(embedding)}f", *embedding)
                conn.execute(
                    "INSERT INTO memories_vec (id, embedding) VALUES (?, ?)",
                    (memory_id, vec_bytes),
                )

        # 簇分配（在存储连接外，因为 assign_cluster 有自己的连接）
        if embedding and any(v != 0.0 for v in embedding):
            cluster_id = await cluster_manager.assign_cluster(memory_id, embedding)

        return memory_id, cluster_id


# 模块级单例
memory_writer = MemoryWriter()
```

**改造影响**:

| 原位置 | 改造方式 |
|--------|----------|
| `api/write.py` write_memory | 调用 `memory_writer.write(conflict_mode="full")` |
| `mcp_server.py` _write | 调用 `memory_writer.write(conflict_mode="fast")` |
| `api/conversation.py` _store_memory | 调用 `memory_writer.write(conflict_mode="none")` |

预计删除 ~120 行重复代码。

#### P0-2: 提取 cosine_similarity 到 core/utils.py

```python
# core/utils.py（新建）
"""共享数学工具"""

import math
import struct


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """两个 float 列表的余弦相似度"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def blob_to_floats(data: bytes, dim: int) -> list[float]:
    """BLOB 字节转 float 列表"""
    if not data:
        return []
    try:
        return list(struct.unpack(f"{dim}f", data[:dim * 4]))
    except Exception:
        return []


def cosine_similarity_blob(blob: bytes, vec: list[float]) -> float:
    """BLOB 向量与 float 列表的余弦相似度"""
    dim = len(blob) // 4
    float_vec = list(struct.unpack(f"{dim}f", blob))
    return cosine_similarity(float_vec, vec)
```

**改造影响**: `conflict.py`、`conversation.py`、`cluster.py` 的 `_cosine_similarity` 和 `_bytes_to_floats` 全部替换为 `from core.utils import ...`。

预计删除 ~60 行重复代码。

#### P0-3: 修复 init.sql

删除 `db/init.sql:103-123` 的重复 chunks 表定义和重复索引，将 `memories_vec` 编号改回 6。

---

### P1: 性能与健壮性（预计 3h）

#### P1-1: httpx.AsyncClient 连接池复用

```python
# core/writer.py 修改
class EmbeddingGenerator:
    def __init__(self):
        self._api_url = settings.EMBEDDING_API_URL
        self._api_key = settings.EMBEDDING_API_KEY
        self._model = settings.EMBEDDING_API_MODEL
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._client

    async def generate(self, text: str) -> list[float]:
        client = await self._get_client()
        # ... 使用 client.post() 而非 async with httpx.AsyncClient() ...

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
```

在 `server.py` 的 `lifespan` 中管理关闭。

#### P1-2: 冲突检测改用 KNN 预筛选

```python
# core/conflict.py 修改 detect_write_conflict_fast
async def detect_write_conflict_fast(self, content, embedding):
    vec_bytes = struct.pack(f"{len(embedding)}f", *embedding)

    with get_connection() as conn:
        # 先用 sqlite-vec KNN 取 Top-20
        rows = conn.execute(
            "SELECT id, distance FROM memories_vec WHERE embedding MATCH ? AND k = 20",
            (vec_bytes,),
        ).fetchall()

    # 只对 Top-20 做精确余弦比对
    scored = []
    for vec_id, distance in rows:
        sim = 1.0 - distance  # sqlite-vec 返回 cosine distance
        mem = conn.execute(
            "SELECT content FROM memories WHERE id = ? AND is_deleted = 0", (str(vec_id),)
        ).fetchone()
        if mem:
            scored.append({"id": str(vec_id), "similarity": round(sim, 4),
                          "content_preview": mem["content"][:100]})

    scored.sort(key=lambda x: x["similarity"], reverse=True)
    conflicts = [m for m in scored[:10] if m["similarity"] > settings.CONFLICT_THRESHOLD]
    return {"has_conflict": len(conflicts) > 0, "conflicts": conflicts}
```

**效果**: 从 O(N) 全量加载降为 O(K)（K=20），1 万条记忆时从 40MB 降到 80KB。

#### P1-3: 冲突 LLM 并行调用

```python
# core/conflict.py 修改 detect_write_conflict
import asyncio

# 收集所有需要 LLM 判断的候选
candidates = []
for row in high_sim_rows:  # Top-20 中超过阈值的部分
    candidates.append((mem_id, mem_content))

# 并行调用 LLM
llm_results = await asyncio.gather(
    *[self._check_contradiction_llm(new_content, content)
      for _, content in candidates],
    return_exceptions=True,
)
```

#### P1-4: 修复 FTS5 触发器时序

**方案**: 将 FTS 同步从触发器改为应用层写入后手动同步（在 MemoryWriter._store 的 `conn.commit()` 之后）。

```python
# 在 _store 末尾、commit 之后
conn.execute("""
    INSERT OR REPLACE INTO memories_fts(memory_id, content, scene, entities)
    SELECT m.id, m.content, COALESCE(t.scene, ''), COALESCE(t.entities, '')
    FROM memories m LEFT JOIN context_tags t ON t.memory_id = m.id
    WHERE m.id = ?
""", (memory_id,))
```

同时删除 `init.sql` 中的 `memories_ai` 和 `memories_au` 触发器（保留 `memories_ad` 删除触发器）。

#### P1-5: LifecycleManager 支持 namespace

```python
# core/lifecycle.py 修改
def decay_all(self, dry_run=False, namespace="default") -> dict:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, importance, hit_count, binding_count
               FROM memories WHERE is_deleted = 0 AND importance > 0 AND namespace = ?""",
            (namespace,),
        ).fetchall()
```

同理修改 `cleanup()` 和 `get_decay_candidates()`。

#### P1-6: 修复 mcp_server._get 表名

```python
# mcp_server.py:261-262 修改
(SELECT COUNT(*) FROM binding_history br WHERE br.memory_id = m.id) as binding_count,
(SELECT COUNT(*) FROM binding_history br WHERE br.memory_id = m.id AND br.was_relevant = 1) as hit_count
```

#### P1-7: admin/recall 接口加 Pydantic 模型

```python
# models/memory.py 新增
class FeedbackRequest(BaseModel):
    memory_id: str
    query: str = ""
    relevant: bool = True
    context: dict | None = None

class RecallRequest(BaseModel):
    query: str
    context: dict | None = None
    top_k: int = Field(5, ge=1, le=50)
    recall_n: int = Field(20, ge=1, le=100)
```

---

### P2: 代码质量（预计 1h）

| 编号 | 问题 | 修复 |
|------|------|------|
| P2-1 | 6 处独立 httpx.AsyncClient | 统一为 `core/http_client.py` 模块级单例 |
| P2-2 | retriever 中每次 new ClusterManager | 改为 `from core.cluster import cluster_manager` |
| P2-3 | mcp_server 重复 import | 删除重复行 |
| P2-4 | `__import__("json")` | 文件顶部 `import json` |
| P2-5 | get_neighbors 全量加载 | 改用 SQL `OFFSET/LIMIT` 定位 |
| P2-6 | `datetime.utcnow()` | 全部替换为 `datetime.now(timezone.utc)` |
| P2-7 | MCP _write 不传 namespace | 增加 namespace 参数支持 |

---

## 四、执行计划

```
Phase 1 (P0, ~2h)  — 消除重复，零功能变更
├── P0-2: 创建 core/utils.py (15min)
├── P0-3: 修复 init.sql (5min)
├── P0-1: 创建 core/memory_writer.py (60min)
│   ├── 实现核心类
│   ├── 改造 api/write.py
│   ├── 改造 mcp_server.py _write
│   └── 改造 api/conversation.py _store_memory
└── 运行测试验证 (40min)

Phase 2 (P1, ~3h) — 性能与健壮性
├── P1-6: 修复 mcp_server._get 表名 (5min)
├── P1-5: LifecycleManager 支持 namespace (20min)
├── P1-7: Pydantic 模型补全 (15min)
├── P1-1: httpx 连接池复用 (30min)
├── P1-2: 冲突检测 KNN 预筛选 (30min)
├── P1-4: FTS5 触发器修复 (20min)
├── P1-3: 冲突 LLM 并行调用 (15min)
└── 运行测试验证 (45min)

Phase 3 (P2, ~1h) — 代码质量
├── P2-1~P2-7 批量修复 (40min)
└── 运行测试验证 (20min)
```

**总计**: ~6h

---

## 五、风险评估

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| P0-1 统一写入改变行为 | 低 | 高 | 逐个 API 改造并跑测试 |
| P1-2 KNN 预筛选漏检 | 低 | 中 | K=20 足够覆盖 0.85 阈值 |
| P1-4 删除触发器导致 FTS 缺数据 | 低 | 中 | 保留 `_init_fts` 补录逻辑 |
| 测试覆盖不全 | 中 | 中 | Phase 1 完成后先跑全量测试 |

---

## 六、预期收益

| 指标 | 当前 | 优化后 |
|------|------|--------|
| 重复代码 | ~300 行（3 处写入 + 4 处相似度） | ~0 行 |
| 写入一次 embedding 延迟 | ~200ms（新建连接） | ~50ms（连接复用） |
| 冲突检测内存占用 | O(N) × 4KB | O(20) × 4KB |
| 冲突检测 LLM 耗时 | O(N) × 2s（串行） | O(N) × 2s（并行） |
| init.sql 干净度 | chunks 重复定义 | 单一来源 |
| Bug 修复面 | 改一处漏三处 | 改一处生效全局 |
