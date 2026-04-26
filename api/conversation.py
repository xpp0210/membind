"""
MemBind 对话记忆提取API

POST /api/v1/memory/conversation
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from core.conversation import ConversationParser
from core.memory_writer import memory_writer
from api.deps import get_namespace

router = APIRouter(prefix="/api/v1/memory", tags=["conversation"])

parser = ConversationParser()


class ConversationMessage(BaseModel):
    role: str = Field(..., description="user 或 assistant")
    content: str = Field(..., min_length=1)


class ConversationRequest(BaseModel):
    messages: list[ConversationMessage]
    auto_store: bool = True
    skip_dedup: bool = False


@router.post("/conversation")
async def conversation_parse(req: ConversationRequest, request: Request):
    """对话记忆提取：过滤 → LLM压缩 → 去重 → 存储"""
    namespace = get_namespace(request)

    messages = [m.model_dump() for m in req.messages]
    result = await parser.parse(messages, skip_dedup=req.skip_dedup)

    stored_memories = []
    stored_count = 0

    if req.auto_store and result.extracted:
        for mem in result.extracted:
            try:
                hint = {}
                if mem.get("scene"):
                    hint["scene"] = mem["scene"]
                if mem.get("entities"):
                    hint["entities"] = mem["entities"]

                write_result = await memory_writer.write(
                    content=mem["content"],
                    namespace=namespace,
                    importance=mem.get("importance"),
                    hint_context=hint if hint else None,
                    conflict_mode="none",
                )
                if write_result:
                    stored_memories.append({
                        "memory_id": write_result.memory_id,
                        "content": write_result.content,
                        "importance": write_result.importance,
                        "scene": write_result.scene,
                        "entities": mem.get("entities", []),
                    })
                    stored_count += 1
            except Exception as e:
                print(f"[MemBind] 存储对话记忆失败: {e}")

    return {
        "filtered_count": len(messages) - len(result.skipped),
        "extracted_count": len(result.extracted),
        "stored_count": stored_count,
        "memories": stored_memories,
    }
