"""
MemBind 上下文标签模型
"""

from pydantic import BaseModel, Field


class ContextTag(BaseModel):
    """上下文标签（由 ContextTagger 自动提取）"""
    memory_id: str = ""
    scene: str = Field("general", description="场景：coding/research/writing/ops/general")
    task_type: str = Field("default", description="任务类型：debug/design/review/learn/default")
    entities: list[str] = Field(default_factory=list, description="提取的实体（技术名词/工具名）")
    importance: float = Field(5.0, ge=0.0, le=10.0, description="重要性评分")
