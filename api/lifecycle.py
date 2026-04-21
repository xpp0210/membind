"""
MemBind 生命周期管理API

POST /api/v1/lifecycle/decay — 执行importance衰减
POST /api/v1/lifecycle/cleanup — 清理低importance记忆
POST /api/v1/lifecycle/boost/{memory_id} — 手动提升importance
POST /api/v1/lifecycle/restore/{memory_id} — 恢复已删除记忆
GET /api/v1/lifecycle/candidates — 查看即将被清理的记忆
"""

from fastapi import APIRouter, Request

from core.lifecycle import lifecycle_manager
from api.deps import get_namespace

router = APIRouter(prefix="/api/v1/lifecycle", tags=["lifecycle"])


@router.post("/decay")
async def decay(body: dict | None = None, request: Request = None):
    """执行importance衰减（所有活跃记忆）"""
    namespace = get_namespace(request)
    dry_run = (body or {}).get("dry_run", False)
    return lifecycle_manager.decay_all(dry_run=dry_run, namespace=namespace)


@router.post("/cleanup")
async def cleanup(body: dict | None = None, request: Request = None):
    """清理importance低于阈值的记忆（软删除）"""
    namespace = get_namespace(request)
    dry_run = (body or {}).get("dry_run", False)
    return lifecycle_manager.cleanup(dry_run=dry_run, namespace=namespace)


@router.post("/boost/{memory_id}")
async def boost(memory_id: str, body: dict | None = None):
    """手动提升记忆importance"""
    amount = (body or {}).get("amount")
    return lifecycle_manager.boost(memory_id, amount=amount)


@router.post("/restore/{memory_id}")
async def restore(memory_id: str):
    """恢复已软删除的记忆"""
    return lifecycle_manager.restore(memory_id)


@router.get("/candidates")
async def candidates(limit: int = 20, request: Request = None):
    """查看importance最低的记忆（即将被清理）"""
    namespace = get_namespace(request)
    items = lifecycle_manager.get_decay_candidates(limit=limit, namespace=namespace)
    return {"count": len(items), "items": items}
