"""
MemBind 服务入口

FastAPI应用实例 + CORS + API Key认证 + 健康检查 + 启动初始化
"""

import logging
import time as _time
from collections import defaultdict

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response as StarletteResponse
from contextlib import asynccontextmanager

from db.connection import init_db
from api.deps import verify_api_key
from config import settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化数据库"""
    init_db()

    # 配置热更新监听
    _watcher = None
    if settings.CONFIG_WATCH:
        try:
            import asyncio
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
            import config as config_module

            class EnvHandler(FileSystemEventHandler):
                def on_modified(self, event):
                    if event.src_path.endswith(".env"):
                        logger.info("检测到 .env 变更，重载配置")
                        config_module.settings = config_module.Settings()

            _watcher = Observer()
            _watcher.schedule(EnvHandler(), ".", recursive=False)
            _watcher.start()
            logger.info("配置热更新已启用，监听 .env 变更")
        except ImportError:
            logger.warning("watchdog 未安装，配置热更新不可用 (pip install watchdog)")

    yield

    if _watcher:
        _watcher.stop()
        _watcher.join()


app = FastAPI(
    title="MemBind",
    description="Binding-First Agent记忆系统",
    version="0.3.0",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "memory", "description": "记忆写入与检索"},
        {"name": "conflict", "description": "冲突检测与管理"},
        {"name": "lifecycle", "description": "生命周期管理（衰减/清理/强化）"},
        {"name": "chunks", "description": "MemOS兼容Chunk存储"},
        {"name": "conversation", "description": "对话解析与记忆提取"},
    ],
)

# ── CORS中间件 ──
_allowed_origins = settings.CORS_ALLOWED_ORIGINS
if _allowed_origins:
    _origins = [o.strip() for o in _allowed_origins.split(",") if o.strip()]
else:
    _origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Prometheus 指标中间件 ──
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    from core.metrics import REQUEST_TOTAL, REQUEST_DURATION
    method = request.method
    path = request.url.path
    start = _time.perf_counter()
    response = await call_next(request)
    duration = _time.perf_counter() - start
    REQUEST_TOTAL.labels(method=method, endpoint=path, status=response.status_code).inc()
    REQUEST_DURATION.labels(method=method, endpoint=path).observe(duration)
    return response


# ── 速率限制中间件 ──
_rate_limit_store: dict[str, list[float]] = defaultdict(list)

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """简单内存速率限制（每分钟N次）"""
    if settings.RATE_LIMIT_PER_MINUTE <= 0:
        return await call_next(request)

    client_key = request.client.host if request.client else "unknown"
    now = _time.time()
    minute_ago = now - 60

    # 清理过期记录
    _rate_limit_store[client_key] = [
        t for t in _rate_limit_store[client_key] if t > minute_ago
    ]

    if len(_rate_limit_store[client_key]) >= settings.RATE_LIMIT_PER_MINUTE:
        return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})

    _rate_limit_store[client_key].append(now)
    return await call_next(request)


# ── API Key认证中间件 ──
@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    """跳过/health和/metrics，其他路由检查API Key"""
    if request.url.path in ("/health", "/metrics"):
        return await call_next(request)
    try:
        verify_api_key(request)
    except Exception as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)


@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "ok", "service": "membind", "version": "0.3.0"}


@app.get("/metrics")
async def metrics():
    from prometheus_client import generate_latest
    return StarletteResponse(
        content=generate_latest(),
        media_type="text/plain",
    )


# ── 路由挂载 ──
from api.write import router as write_router
from api.recall import router as recall_router
from api.admin import router as admin_router
from api.conflict import router as conflict_router
from api.conversation import router as conversation_router
from api.lifecycle import router as lifecycle_router
from api.chunk import router as chunk_router

app.include_router(write_router)
app.include_router(recall_router)
app.include_router(admin_router)
app.include_router(conflict_router)
app.include_router(conversation_router)
app.include_router(lifecycle_router)
app.include_router(chunk_router)
