"""
MemBind 对话记忆提取API

POST /api/v1/memory/conversation
"""

import struct
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.conversation import ConversationParser
from core.writer import EmbeddingGenerator
from db.connection import get_connection
from config import settings
from api.deps import get_namespace

router = APIRouter(prefix="/api/v1/memory", tags=["conversation"])

parser = ConversationParser()
embedder = EmbeddingGenerator()


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
                stored = await _store_memory(mem, namespace=namespace)
                if stored:
                    stored_memories.append(stored)
                    stored_count += 1
            except Exception as e:
                print(f"[MemBind] 存储对话记忆失败: {e}")

    return {
        "filtered_count": len(messages) - len(result.skipped),
        "extracted_count": len(result.extracted),
        "stored_count": stored_count,
        "memories": stored_memories,
    }


async def _store_memory(mem: dict, namespace: str = "default") -> dict | None:
    """将提取的记忆存入数据库（复用write逻辑）"""
    content = mem["content"]
    importance = mem.get("importance", 5.0)
    scene = mem.get("scene", "general")
    entities = mem.get("entities", [])

    # 生成embedding
    embedding = await embedder.generate(content)

    memory_id = uuid.uuid4().hex[:16]
    tag_id = uuid.uuid4().hex[:16]
    now = datetime.utcnow().isoformat()
    entities_json = __import__("json").dumps(entities, ensure_ascii=False)

    with get_connection() as conn:
        conn.execute(
            """INSERT INTO memories (id, content, importance, metadata, created_at, updated_at, namespace)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (memory_id, content, importance, "{}", now, now, namespace),
        )
        conn.execute(
            """INSERT INTO context_tags (id, memory_id, scene, task_type, entities, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (tag_id, memory_id, scene, "default", entities_json, now),
        )
        if embedding and any(v != 0.0 for v in embedding):
            vec_bytes = struct.pack(f"{len(embedding)}f", *embedding)
            conn.execute(
                "INSERT INTO memories_vec (id, embedding) VALUES (?, ?)",
                (memory_id, vec_bytes),
            )

    return {
        "memory_id": memory_id,
        "content": content,
        "importance": importance,
        "scene": scene,
        "entities": entities,
    }
