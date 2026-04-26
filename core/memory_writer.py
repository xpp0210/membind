"""
MemBind 统一记忆写入器

所有写入路径（HTTP API / MCP / Conversation）统一走这个模块。
标签 → embedding → 冲突预检 → 存储 → 簇分配
"""

import json
import struct
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field

from config import settings
from core.writer import ContextTagger, EmbeddingGenerator
from core.conflict import conflict_detector
from core.cluster import cluster_manager
from db.connection import get_connection


@dataclass
class WriteResult:
    """写入结果"""
    memory_id: str
    content: str
    scene: str
    importance: float
    conflict_warnings: list[dict] = field(default_factory=list)
    conflict_check: dict | None = None  # HTTP API 用的完整冲突检查结果
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
        """
        写入一条记忆。

        Args:
            content: 记忆文本
            namespace: 命名空间
            importance: 手动指定重要性（None 则自动计算）
            hint_context: 上下文提示 {scene, task_type, entities}
            conflict_mode: 冲突检测模式
                - "full": 向量比对 + LLM矛盾判断（HTTP write API 用）
                - "fast": 仅向量 Top-10 比对（MCP 用）
                - "none": 不做冲突检测（Conversation 用）
        """
        # 1. 标签提取
        tag = await self.tagger.tag(content, hint_context=hint_context)
        if importance is not None:
            tag.importance = importance

        # 2. Embedding 生成
        embedding = await self.embedder.generate(content)

        # 3. 冲突预检
        conflict_warnings: list[dict] = []
        conflict_check: dict | None = {"status": "clear"}

        if conflict_mode != "none" and embedding and any(v != 0.0 for v in embedding):
            if conflict_mode == "full":
                try:
                    conflict_result = await conflict_detector.detect_write_conflict(content, embedding)
                    if conflict_result.check == "conflict":
                        conflict_warnings = [
                            {"memory_id": c.memory_id_b, "similarity": c.similarity,
                             "reason": c.reason, "resolution": c.resolution}
                            for c in conflict_result.conflicts
                        ]
                        conflict_check = {
                            "status": "conflict",
                            "conflicts": conflict_warnings,
                        }
                    elif conflict_result.check == "similar":
                        conflict_warnings = [
                            {"memory_id": c.memory_id_b, "similarity": c.similarity}
                            for c in conflict_result.conflicts
                        ]
                        conflict_check = {
                            "status": "similar",
                            "similar": conflict_warnings,
                        }
                    else:
                        conflict_check = {"status": "clear"}
                except Exception:
                    conflict_check = {"status": "error"}
            else:  # fast
                try:
                    fast_result = await conflict_detector.detect_write_conflict_fast(content, embedding)
                    if fast_result.get("has_conflict"):
                        conflict_warnings = fast_result.get("conflicts", [])
                except Exception:
                    pass

        # 4. 存储 + 簇分配
        memory_id, cluster_id = await self._store(content, tag, embedding, namespace)

        return WriteResult(
            memory_id=memory_id,
            content=content,
            scene=tag.scene,
            importance=tag.importance,
            conflict_warnings=conflict_warnings,
            conflict_check=conflict_check,
            cluster_id=cluster_id,
        )

    async def _store(
        self,
        content: str,
        tag,
        embedding: list[float] | None,
        namespace: str,
    ) -> tuple[str, str | None]:
        """存储记忆到数据库 + 分配知识簇"""
        memory_id = uuid.uuid4().hex[:16]
        tag_id = uuid.uuid4().hex[:16]
        now = datetime.now(timezone.utc).isoformat()
        entities_json = json.dumps(tag.entities, ensure_ascii=False)

        cluster_id: str | None = None

        with get_connection() as conn:
            # 写 memories 主表
            conn.execute(
                """INSERT INTO memories (id, content, importance, metadata, created_at, updated_at, namespace)
                   VALUES (?, ?, ?, '{}', ?, ?, ?)""",
                (memory_id, content, tag.importance, now, now, namespace),
            )
            # 写 context_tags（1:1）
            conn.execute(
                """INSERT INTO context_tags (id, memory_id, scene, task_type, entities, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (tag_id, memory_id, tag.scene, tag.task_type, entities_json, now),
            )
            # 写向量虚拟表
            if embedding and any(v != 0.0 for v in embedding):
                vec_bytes = struct.pack(f"{len(embedding)}f", *embedding)
                conn.execute(
                    "INSERT INTO memories_vec (id, embedding) VALUES (?, ?)",
                    (memory_id, vec_bytes),
                )
            # 同步 FTS5（在事务内，确保 tags 已存在）
            conn.execute(
                """INSERT OR REPLACE INTO memories_fts(memory_id, content, scene, entities)
                   VALUES (?, ?, ?, ?)""",
                (memory_id, content, tag.scene, entities_json),
            )

        # 簇分配（在存储连接外，因为 assign_cluster 有自己的连接）
        if embedding and any(v != 0.0 for v in embedding):
            cluster_id = await cluster_manager.assign_cluster(memory_id, embedding)

        return memory_id, cluster_id


# 模块级单例
memory_writer = MemoryWriter()
