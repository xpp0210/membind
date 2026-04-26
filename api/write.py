"""
MemBind 写入API

POST /api/v1/memory/write
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Request

from core.memory_writer import memory_writer
from models.memory import MemoryCreate, MemoryResponse, ContextTagResponse
from api.deps import get_namespace

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])


@router.post("/write", response_model=MemoryResponse)
async def write_memory(req: MemoryCreate, request: Request):
    """写入一条记忆：提取标签 → 生成embedding → 冲突预检 → 存储"""
    namespace = get_namespace(request)

    result = await memory_writer.write(
        content=req.content,
        namespace=namespace,
        importance=req.importance,
        hint_context=req.context,
        conflict_mode="full",
    )

    # 记录冲突到 conflict_log（仅真实冲突）
    if result.conflict_check and result.conflict_check.get("status") == "conflict":
        from core.conflict import ConflictInfo, conflict_detector
        for w in result.conflict_warnings:
            conflict_info = ConflictInfo(
                memory_id_a=result.memory_id,
                memory_id_b=w["memory_id"],
                content_a=result.content,
                content_b="",
                similarity=w["similarity"],
                contradiction=True,
                reason=w.get("reason", ""),
                resolution=w.get("resolution", "keep_both"),
            )
            conflict_detector.log_conflict(conflict_info, result.memory_id)

    return MemoryResponse(
        id=result.memory_id,
        content=result.content,
        tags=ContextTagResponse(
            scene=result.scene,
            task_type="default",
            entities=[],
            importance=result.importance,
        ),
        conflict_check=result.conflict_check,
        created_at=datetime.now(timezone.utc),
    )
