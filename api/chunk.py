"""
MemBind Chunk API

提供chunk的写入、timeline、获取接口，兼容OpenClaw的memory_timeline/memory_get。
"""

from fastapi import APIRouter, HTTPException, Request
from models.chunk import (
    ChunkRef, ChunkResponse, TimelineResult, TimelineEntry, CaptureRequest
)
from core import chunk_store
from api.deps import get_namespace

router = APIRouter(prefix="/api/v1/chunks", tags=["chunks"])


@router.post("/capture", response_model=dict)
async def capture_chunks(req: CaptureRequest):
    """批量写入chunks（模拟OpenClaw的onConversationTurn）"""
    if not req.messages:
        raise HTTPException(400, "messages不能为空")
    ids = chunk_store.save_chunks_batch(
        req.messages, req.session_key, req.turn_id, req.owner
    )
    return {"status": "ok", "count": len(ids), "chunk_ids": ids}


@router.get("/timeline", response_model=TimelineResult)
async def get_timeline(
    session_key: str, turn_id: str, seq: int, window: int = 2
):
    """根据ChunkRef获取上下文（对应OpenClaw的memory_timeline）"""
    # 验证anchor存在
    anchor = chunk_store.get_by_ref(session_key, turn_id, seq)
    if not anchor:
        return TimelineResult(
            entries=[],
            anchor_ref=ChunkRef(
                sessionKey=session_key, chunkId="", turnId=turn_id, seq=seq
            )
        )

    neighbors = chunk_store.get_neighbors(session_key, turn_id, seq, window)
    entries = [
        TimelineEntry(
            id=c["id"],
            session_key=c["session_key"],
            turn_id=c["turn_id"],
            seq=c["seq"],
            role=c["role"],
            content=c["content"],
            summary=c.get("summary"),
            memory_id=c.get("memory_id"),
            created_at=c["created_at"],
            relation=c["relation"],
        )
        for c in neighbors
    ]
    return TimelineResult(
        entries=entries,
        anchor_ref=ChunkRef(
            sessionKey=anchor["session_key"],
            chunkId=anchor["id"],
            turnId=anchor["turn_id"],
            seq=anchor["seq"],
        )
    )


@router.get("/{chunk_id}", response_model=ChunkResponse)
async def get_chunk(chunk_id: str):
    """获取完整chunk（对应OpenClaw的memory_get）"""
    chunk = chunk_store.get_chunk(chunk_id)
    if not chunk:
        raise HTTPException(404, f"Chunk not found: {chunk_id}")
    return ChunkResponse(
        id=chunk["id"],
        session_key=chunk["session_key"],
        turn_id=chunk["turn_id"],
        seq=chunk["seq"],
        role=chunk["role"],
        content=chunk["content"],
        summary=chunk.get("summary"),
        memory_id=chunk.get("memory_id"),
        owner=chunk["owner"],
        created_at=chunk["created_at"],
    )


@router.get("/by-memory/{memory_id}", response_model=list[ChunkResponse])
async def get_chunks_by_memory(memory_id: str):
    """获取关联到某个memory的所有chunks"""
    chunks = chunk_store.get_by_memory_id(memory_id)
    return [
        ChunkResponse(
            id=c["id"],
            session_key=c["session_key"],
            turn_id=c["turn_id"],
            seq=c["seq"],
            role=c["role"],
            content=c["content"],
            summary=c.get("summary"),
            memory_id=c.get("memory_id"),
            owner=c["owner"],
            created_at=c["created_at"],
        )
        for c in chunks
    ]
