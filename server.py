"""
MemBind 服务入口

FastAPI应用实例 + CORS + API Key认证 + 健康检查 + 启动初始化
"""

import time
from collections import defaultdict

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from contextlib import asynccontextmanager

from db.connection import init_db
from api.deps import verify_api_key
from config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化数据库"""
    init_db()
    yield


app = FastAPI(
    title="MemBind",
    description="Binding-First Agent记忆系统",
    version="0.2.0",
    lifespan=lifespan,
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


# ── 速率限制中间件 ──
_rate_limit_store: dict[str, list[float]] = defaultdict(list)

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """简单内存速率限制（每分钟N次）"""
    if settings.RATE_LIMIT_PER_MINUTE <= 0:
        return await call_next(request)
    
    client_key = request.client.host if request.client else "unknown"
    now = time.time()
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
    """跳过/health，其他路由检查API Key"""
    if request.url.path == "/health":
        return await call_next(request)
    try:
        verify_api_key(request)
    except Exception as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)


@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "ok", "service": "membind", "version": "0.2.0"}


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
