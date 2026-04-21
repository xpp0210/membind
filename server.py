"""
MemBind 服务入口

FastAPI应用实例 + CORS + API Key认证 + 健康检查 + 启动初始化
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from contextlib import asynccontextmanager

from db.connection import init_db
from api.deps import verify_api_key


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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
