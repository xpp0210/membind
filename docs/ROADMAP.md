# MemBind 改进路线图

> 生成日期: 2026-04-26 | 基于两轮优化后(v0.2.0)的剩余改进项

---

## P4 — 代码细节

预计耗时: ~30min | 风险: 低

### P4-1: `api/conversation.py:67` print→logging
- [ ] `print(f"[MemBind] 存储对话记忆失败: {e}")` → `logger.error("存储对话记忆失败: %s", e)`
- [ ] 文件顶部加 `import logging` + `logger = logging.getLogger(__name__)`

### P4-2: `core/writer.py` ContextTagger/EmbeddingGenerator 改用共享客户端
- [ ] 删除 `ContextTagger` 类内 `_llm_client` 字段 + `_get_llm_client()` + `_close_llm_client()` 方法
- [ ] 删除 `EmbeddingGenerator` 类内 `_client` 字段 + `_get_client()` + `_close_client()` 方法
- [ ] `from core.http_client import get_client`
- [ ] 调用处 `self._get_llm_client()` → `get_client(timeout=30.0)`
- [ ] 调用处 `self._get_client()` → `get_client(timeout=15.0)`

### P4-3: `core/conflict.py` 模块级客户端改用共享
- [ ] 删除模块级 `_llm_client` 变量 + `_get_llm_client()` 函数
- [ ] `from core.http_client import get_client`
- [ ] 调用处 `client = await _get_llm_client()` → `client = get_client(timeout=30.0)`

### P4-4: `api/lifecycle.py` Pydantic model 替换 `body:dict`
- [ ] 新增 `DecayRequest(dry_run: bool = False)`
- [ ] 新增 `CleanupRequest(dry_run: bool = False)`
- [ ] 新增 `BoostRequest(amount: float | None = None)`
- [ ] `decay()` 参数 `body: dict | None` → `body: DecayRequest | None = DecayRequest()`
- [ ] `cleanup()` 参数 `body: dict | None` → `body: CleanupRequest | None = CleanupRequest()`
- [ ] `boost()` 参数 `body: dict | None` → `body: BoostRequest | None = BoostRequest()`

### P4-5: `docker/docker-compose.yml` 规范化
- [ ] 删除 `version: "3.8"` 行
- [ ] 加 `healthcheck` 配置（test/interval/timeout/retries/start_period）

### P4-6: `mcp_server.py` namespace 传递
- [ ] `handle_write` 加 `namespace = args.get("namespace", "default")`，传给 `memory_writer.write()`
- [ ] `handle_recall` 加 namespace，传给 `recall_service.recall_and_bind()`
- [ ] `handle_feedback` 加 namespace
- [ ] `handle_boost` 加 namespace
- [ ] `handle_restore` 加 namespace
- [ ] `handle_decay` 加 namespace，传给 `lifecycle_manager.decay_all()`
- [ ] `handle_cleanup` 加 namespace，传给 `lifecycle_manager.cleanup()`

---

## P3 — 运维增强

预计耗时: ~2h | 风险: 低

### P3-1: Prometheus 指标
- [ ] 新建 `core/metrics.py` — 定义 Counter/Histogram/Gauge:
  - `membind_requests_total` (labels: method, endpoint, status)
  - `membind_request_duration_seconds` (labels: method, endpoint)
  - `membind_memories_total` (labels: namespace)
  - `membind_conflicts_total` (labels: resolution)
- [ ] `server.py` 加 middleware 自动采集请求指标
- [ ] `server.py` 加 `/metrics` 端点
- [ ] `requirements.txt` 加 `prometheus-client`

### P3-2: OpenAPI 文档分组
- [ ] `server.py` `FastAPI()` 加 `openapi_tags` 参数:
  - memory: 记忆写入与检索
  - conflict: 冲突检测与管理
  - lifecycle: 生命周期管理
  - chunks: MemOS兼容Chunk存储
  - conversation: 对话解析与记忆提取

### P3-3: 数据库自动备份
- [ ] 新建 `scripts/backup.sh`:
  - `sqlite3 "$DB_PATH" ".backup '$BACKUP_DIR/membind_$(date +%Y%m%d_%H%M).db'"`
  - 保留最近7份: `ls -t ... | tail -n +8 | xargs -r rm --`
- [ ] 新建 `deploy/backup.service` (ExecStart=scripts/backup.sh)
- [ ] 新建 `deploy/backup.timer` (OnCalendar=hourly)
- [ ] `backup.sh` 加 `chmod +x`

### P3-4: 配置热更新
- [ ] `config.py` 加 `CONFIG_WATCH: bool = False`
- [ ] `server.py` lifespan 中若开启，用 `watchdog` 监听 `.env` 变更
- [ ] 变更时重建 `settings = Settings()`
- [ ] `requirements.txt` 加 `watchdog`

---

## P5 — 架构演进

预计耗时: ~4h | 风险: 中高

### P5-1: PostgreSQL 后端
- [ ] `requirements.txt` 加 `asyncpg`、`pgvector`
- [ ] `config.py` 加 `DATABASE_URL: str = "sqlite:///data/membind.db"`，解析协议路由后端
- [ ] 新建 `db/pg_connection.py` — asyncpg 连接池，实现与 `get_connection()` 相同接口
- [ ] `db/connection.py` — 根据 DATABASE_URL 协议路由到 sqlite/pg
- [ ] 新建 `db/init_pg.sql` — PG 语法（SERIAL PRIMARY KEY、bytea、tsvector）
- [ ] `core/retriever.py` — sqlite-vec KNN → pgvector `embedding <=> $1 ORDER BY`
- [ ] `core/writer.py` — FTS5 `_update_fts_index()` → pg `tsvector` 更新
- [ ] `core/chunk_store.py` — SQL 语法适配（`datetime('now')` → `NOW()` 等）

### P5-2: OAuth2 认证
- [ ] `requirements.txt` 加 `python-jose[cryptography]`、`passlib[bcrypt]`
- [ ] `db/init.sql` 新增 `users` 表（id, username, hashed_password, namespaces TEXT[]）
- [ ] `config.py` 加 `OAUTH2_ENABLED: bool = False`、`SECRET_KEY: str`
- [ ] 新建 `api/auth.py` — `POST /auth/token`（获取JWT）、`POST /auth/register`（注册）
- [ ] `api/deps.py` 加 `OAuth2PasswordBearer`，验证 token → 查用户 → 校验 namespace 权限
- [ ] `mcp_server.py` MCP 工具调用时先验证 token

### P5-3: Web UI 管理界面
- [ ] 新建 `web/index.html` — 单页应用（~800行 HTML+CSS+JS）
  - 记忆列表（表格，搜索/importance排序/namespace过滤）
  - 冲突管理（列表 + 一键解决）
  - 生命周期仪表盘（衰减/清理/强化操作 + candidates 预览）
  - 统计图表（总记忆数/冲突数/namespace分布）
- [ ] `server.py` 加 `app.mount("/ui", StaticFiles(directory="web"), name="ui")`

---

## 依赖关系

```
P4-2 + P4-3 (httpx统一) → P4-6 (MCP namespace) 可并行
P3-1 (metrics) → P3-2 (openapi_tags) 可并行
P5-1 (PG) 独立，改动面最大
P5-2 (OAuth2) 独立
P5-3 (Web UI) 独立
```

## 建议执行顺序

1. **P4 全部** — 低风险，30min 搞定
2. **P3-1 + P3-2** — 运维基础，1h
3. **P3-3 + P3-4** — 运维完善，1h
4. **P5 按需** — 除非有明确场景，否则暂不执行
