"""
MemBind 生命周期管理API
"""

import logging
from fastapi import APIRouter, Request
from pydantic import BaseModel

from core.lifecycle import lifecycle_manager
from api.deps import get_namespace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/lifecycle", tags=["lifecycle"])


class DecayRequest(BaseModel):
    dry_run: bool = False


class CleanupRequest(BaseModel):
    dry_run: bool = False


class BoostRequest(BaseModel):
    amount: float | None = None


@router.post("/decay")
async def decay(body: DecayRequest | None = DecayRequest(), request: Request = None):
    """执行importance衰减（所有活跃记忆）"""
    namespace = get_namespace(request)
    return lifecycle_manager.decay_all(dry_run=body.dry_run, namespace=namespace)


@router.post("/cleanup")
async def cleanup(body: CleanupRequest | None = CleanupRequest(), request: Request = None):
    """清理importance低于阈值的记忆（软删除）"""
    namespace = get_namespace(request)
    return lifecycle_manager.cleanup(dry_run=body.dry_run, namespace=namespace)


@router.post("/boost/{memory_id}")
async def boost(memory_id: str, body: BoostRequest | None = BoostRequest()):
    """手动提升记忆importance"""
    return lifecycle_manager.boost(memory_id, amount=body.amount)


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
