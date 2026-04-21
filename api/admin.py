"""
MemBind 反馈 + 管理API

POST /api/v1/memory/feedback — 反馈（强化/衰减）
GET /api/v1/stats — 系统统计
GET /api/v1/memory/{memory_id} — 记忆详情
"""

from fastapi import APIRouter, Request

from services.binding_service import update_feedback, get_stats
from db.connection import get_connection
from api.deps import get_namespace

router = APIRouter(prefix="/api/v1", tags=["memory"])


@router.post("/memory/feedback")
async def feedback(body: dict, request: Request):
    """反馈记忆的相关性：relevant=true强化，false衰减"""
    memory_id = body.get("memory_id", "")
    query = body.get("query", "")
    relevant = body.get("relevant", True)
    context = body.get("context")
    namespace = get_namespace(request)

    if not memory_id:
        return {"error": "memory_id is required"}

    update_feedback(memory_id, query, relevant, context)
    delta = "+0.5" if relevant else "-0.5"

    return {
        "status": "ok",
        "memory_id": memory_id,
        "action": "boosted" if relevant else "decayed",
        "importance_delta": delta,
    }


@router.get("/stats")
async def stats(request: Request):
    """系统统计（按namespace过滤）"""
    namespace = get_namespace(request)
    return get_stats(namespace=namespace)


@router.get("/memory/{memory_id}")
async def get_memory(memory_id: str, request: Request):
    namespace = get_namespace(request)
    """获取记忆详情"""
    import json

    with get_connection() as conn:
        row = conn.execute(
            """SELECT m.id, m.content, m.importance, m.hit_count, m.binding_count,
                      m.source, m.created_at, m.updated_at,
                      t.scene, t.task_type, t.entities
               FROM memories m
               LEFT JOIN context_tags t ON t.memory_id = m.id
               WHERE m.id = ? AND m.is_deleted = 0 AND m.namespace = ?""",
            (memory_id, namespace),
        ).fetchone()

        if not row:
            return {"error": "memory not found"}

        # 最近binding记录
        bindings = conn.execute(
            """SELECT query, binding_score, was_relevant, activated_at
               FROM binding_history WHERE memory_id = ?
               ORDER BY activated_at DESC LIMIT 5""",
            (memory_id,),
        ).fetchall()

        return {
            "id": row[0],
            "content": row[1],
            "importance": row[2],
            "hit_count": row[3],
            "binding_count": row[4],
            "source": row[5],
            "created_at": row[6],
            "updated_at": row[7],
            "tags": {
                "scene": row[8] or "general",
                "task_type": row[9] or "default",
                "entities": json.loads(row[10]) if row[10] else [],
            },
            "recent_bindings": [
                {"query": b[0], "score": b[1], "relevant": b[2], "at": b[3]}
                for b in bindings
            ],
        }
