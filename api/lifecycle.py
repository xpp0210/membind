"""
MemBind 生命周期管理API（V2）

新增记忆巩固端点。
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


class ConsolidateRequest(BaseModel):
    dry_run: bool = False


@router.post("/decay")
async def decay(body: DecayRequest | None = DecayRequest(), request: Request = None):
    """执行importance衰减（Ebbinghaus指数衰减模型）"""
    namespace = get_namespace(request)
    return lifecycle_manager.decay_all(dry_run=body.dry_run, namespace=namespace)


@router.post("/consolidate")
async def consolidate(body: ConsolidateRequest | None = ConsolidateRequest(), request: Request = None):
    """
    执行记忆巩固：提升符合条件的记忆的巩固等级。

    巩固条件：binding_count >= 阈值 && accuracy >= 0.6 && 距上次巩固 >= 24h
    巩固效果：衰减速率降低（level 0→1x, 1→0.5x, 2→0.33x, 3→0.25x）
    """
    namespace = get_namespace(request)
    return lifecycle_manager.consolidate(namespace=namespace, dry_run=body.dry_run)


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
