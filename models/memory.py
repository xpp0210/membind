"""
MemBind 记忆数据模型

定义 Memory 的请求/响应/DB映射 Schema。
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class MemoryCreate(BaseModel):
    """写入请求"""
    content: str = Field(..., min_length=1, max_length=10000, description="记忆内容")
    context: Optional[dict] = Field(None, description="可选上下文（scene/task_type/entities）")
    metadata: Optional[dict] = Field(None, description="可选元数据")
    importance: Optional[float] = Field(None, ge=0.0, le=10.0, description="重要性评分，不传则自动计算")


class ContextTagResponse(BaseModel):
    """上下文标签"""
    scene: str = "general"
    task_type: str = "default"
    entities: list[str] = []
    importance: float = 5.0


class MemoryResponse(BaseModel):
    """写入响应"""
    id: str
    content: str
    tags: ContextTagResponse
    conflict_check: Optional[dict] = None  # {"has_conflict": bool, "similar_id": str|None}
    created_at: datetime
