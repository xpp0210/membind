"""
MemBind 写入API

POST /api/v1/memory/write
"""

import struct
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from config import settings
from core.writer import ContextTagger, EmbeddingGenerator
from core.conflict import conflict_detector
from db.connection import get_connection
from models.memory import MemoryCreate, MemoryResponse, ContextTagResponse
from api.deps import get_namespace

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])

tagger = ContextTagger()
embedder = EmbeddingGenerator()


@router.post("/write", response_model=MemoryResponse)
async def write_memory(req: MemoryCreate, request: Request):
    namespace = get_namespace(request)
    """写入一条记忆：提取标签 → 生成embedding → 冲突预检 → 存储"""
    # 1. 提取上下文标签
    tag = await tagger.tag(req.content, hint_context=req.context)
    if req.importance is not None:
        tag.importance = req.importance

    # 2. 生成embedding
    embedding = await embedder.generate(req.content)

    # 3. 冲突预检（可选增强，失败不影响写入）
    conflict_check = {"status": "clear"}
    conflict_result = None
    try:
        if embedding and any(v != 0.0 for v in embedding):
            conflict_result = await conflict_detector.detect_write_conflict(req.content, embedding)
            if conflict_result.check == "conflict":
                conflict_check = {
                    "status": "conflict",
                    "conflicts": [
                        {"memory_id": c.memory_id_b, "similarity": c.similarity,
                         "reason": c.reason, "resolution": c.resolution}
                        for c in conflict_result.conflicts
                    ],
                }
            elif conflict_result.check == "similar":
                conflict_check = {
                    "status": "similar",
                    "similar": [
                        {"memory_id": c.memory_id_b, "similarity": c.similarity}
                        for c in conflict_result.conflicts
                    ],
                }
    except Exception:
        conflict_check = {"status": "error"}

    # 4. 写入数据库
    memory_id = uuid.uuid4().hex[:16]
    tag_id = uuid.uuid4().hex[:16]
    now = datetime.utcnow().isoformat()
    metadata_json = req.metadata and __import__("json").dumps(req.metadata, ensure_ascii=False) or "{}"
    entities_json = __import__("json").dumps(tag.entities, ensure_ascii=False)

    with get_connection() as conn:
        # 写memories主表（不含scene/实体——这些在context_tags里）
        conn.execute(
            """INSERT INTO memories (id, content, importance, metadata, created_at, updated_at, namespace)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (memory_id, req.content, tag.importance, metadata_json, now, now, namespace),
        )

        # 写context_tags（1:1）
        conn.execute(
            """INSERT INTO context_tags (id, memory_id, scene, task_type, entities, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (tag_id, memory_id, tag.scene, tag.task_type, entities_json, now),
        )

        # 写向量虚拟表
        if embedding and any(v != 0.0 for v in embedding):
            vec_bytes = struct.pack(f"{len(embedding)}f", *embedding)
            conn.execute(
                "INSERT INTO memories_vec (id, embedding) VALUES (?, ?)",
                (memory_id, vec_bytes),
            )

        conn.commit()

    # 5. 记录冲突到conflict_log
    if conflict_check.get("status") == "conflict" and conflict_result:
        for c in conflict_result.conflicts:
            c.memory_id_a = memory_id
            conflict_detector.log_conflict(c, memory_id)

    return MemoryResponse(
        id=memory_id,
        content=req.content,
        tags=ContextTagResponse(
            scene=tag.scene,
            task_type=tag.task_type,
            entities=tag.entities,
            importance=tag.importance,
        ),
        conflict_check=conflict_check,
        created_at=datetime.fromisoformat(now),
    )
