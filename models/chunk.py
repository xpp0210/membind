"""
MemBind Chunk 数据模型

Chunk 是对话片段的存储单元，兼容 OpenClaw MemOS 的 ChunkRef 模型。
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class ChunkRef(BaseModel):
    """Chunk引用（兼容MemOS的ChunkRef）"""
    sessionKey: str
    chunkId: str
    turnId: str
    seq: int


class ChunkCreate(BaseModel):
    """单条chunk写入"""
    session_key: str
    turn_id: str
    seq: int
    role: str = Field(..., pattern="^(user|assistant|system|tool)$")
    content: str = Field(..., min_length=1)
    summary: Optional[str] = None
    memory_id: Optional[str] = None
    owner: str = "agent:main"


class ChunkResponse(BaseModel):
    """Chunk响应"""
    id: str
    session_key: str
    turn_id: str
    seq: int
    role: str
    content: str
    summary: Optional[str] = None
    memory_id: Optional[str] = None
    owner: str
    created_at: datetime
    relation: str = "current"  # before/current/after（timeline用）


class TimelineEntry(BaseModel):
    """Timeline条目"""
    id: str
    session_key: str
    turn_id: str
    seq: int
    role: str
    content: str
    summary: Optional[str] = None
    memory_id: Optional[str] = None
    created_at: datetime
    relation: str  # before/current/after


class TimelineResult(BaseModel):
    """Timeline响应"""
    entries: list[TimelineEntry] = []
    anchor_ref: Optional[ChunkRef] = None


class CaptureRequest(BaseModel):
    """批量捕获请求（模拟onConversationTurn）"""
    session_key: str
    turn_id: str
    messages: list[dict]  # [{"role": "user|assistant|system|tool", "content": "..."}]
    owner: str = "agent:main"
