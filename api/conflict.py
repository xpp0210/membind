"""
MemBind 冲突管理API

GET /api/v1/conflicts — 列出未解决冲突
POST /api/v1/conflicts/resolve — 手动解决
POST /api/v1/conflicts/auto-resolve — LLM自动解决
"""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request

from core.conflict import conflict_detector
from db.connection import get_connection
from api.deps import get_namespace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["conflict"])


@router.get("/conflicts")
async def list_conflicts(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """列出所有未解决的冲突"""
    namespace = get_namespace(request)
    with get_connection() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM conflict_log cl "
            "JOIN memories m_a ON m_a.id = cl.memory_id_a "
            "WHERE cl.resolution = 'pending' AND m_a.namespace = ?",
            (namespace,),
        ).fetchone()[0]

        rows = conn.execute(
            """SELECT cl.id, cl.memory_id_a, cl.memory_id_b, cl.similarity, cl.reason, cl.created_at
               FROM conflict_log cl
               JOIN memories m_a ON m_a.id = cl.memory_id_a
               WHERE cl.resolution = 'pending' AND m_a.namespace = ?
               ORDER BY cl.created_at DESC
               LIMIT ? OFFSET ?""",
            (namespace, limit, offset),
        ).fetchall()

        # 补充content信息
        conflicts = []
        for row in rows:
            cid, id_a, id_b, sim, reason, created = row
            a_content = conn.execute(
                "SELECT content FROM memories WHERE id = ?", (id_a,)
            ).fetchone()
            b_content = conn.execute(
                "SELECT content FROM memories WHERE id = ?", (id_b,)
            ).fetchone()

            conflicts.append({
                "id": cid,
                "memory_id_a": id_a,
                "memory_id_b": id_b,
                "content_a": a_content[0] if a_content else "",
                "content_b": b_content[0] if b_content else "",
                "similarity": sim,
                "reason": reason or "",
                "created_at": created,
            })

    return {"total": total, "limit": limit, "offset": offset, "conflicts": conflicts}


@router.post("/conflicts/resolve")
async def resolve_conflict(body: dict):
    """手动解决冲突"""
    conflict_id = body.get("conflict_id", "")
    resolution = body.get("resolution", "")

    if not conflict_id or not resolution:
        return {"error": "conflict_id and resolution are required"}

    valid = ("keep_both", "keep_new", "keep_old", "merge")
    if resolution not in valid:
        return {"error": f"resolution must be one of {valid}"}

    with get_connection() as conn:
        row = conn.execute(
            "SELECT memory_id_a, memory_id_b FROM conflict_log WHERE id = ? AND resolution = 'pending'",
            (conflict_id,),
        ).fetchone()

        if not row:
            return {"error": "conflict not found or already resolved"}

        id_a, id_b = row[0], row[1]
        now = datetime.now(timezone.utc).isoformat()

        # 更新conflict_log
        conn.execute(
            "UPDATE conflict_log SET resolution = ?, resolved_at = ? WHERE id = ?",
            (resolution, now, conflict_id),
        )

        # 根据resolution执行软删除
        if resolution == "keep_old":
            conn.execute("UPDATE memories SET is_deleted = 1, updated_at = ? WHERE id = ?", (now, id_b))
        elif resolution == "keep_new":
            conn.execute("UPDATE memories SET is_deleted = 1, updated_at = ? WHERE id = ?", (now, id_a))
        # keep_both / merge: 不额外操作

        conn.commit()

    return {
        "status": "ok",
        "conflict_id": conflict_id,
        "resolution": resolution,
        "soft_deleted": {
            "keep_old": [id_b],
            "keep_new": [id_a],
        }.get(resolution, []),
    }


@router.post("/conflicts/auto-resolve")
async def auto_resolve_conflicts():
    """基于LLM自动解决所有pending冲突"""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT cl.id, cl.memory_id_a, cl.memory_id_b,
                      m_a.content AS content_a, m_b.content AS content_b,
                      m_a.importance AS importance_a, m_b.importance AS importance_b,
                      m_a.created_at AS created_a, m_b.created_at AS created_b
               FROM conflict_log cl
               JOIN memories m_a ON m_a.id = cl.memory_id_a
               JOIN memories m_b ON m_b.id = cl.memory_id_b
               WHERE cl.resolution = 'pending'"""
        ).fetchall()

        if not rows:
            return {"status": "ok", "resolved": 0, "details": []}

        details = []
        now = datetime.now(timezone.utc).isoformat()

        for row in rows:
            cid, id_a, id_b = row[0], row[1], row[2]
            content_a, content_b = row[3], row[4]
            imp_a, imp_b = row[5] or 5.0, row[6] or 5.0
            created_a, created_b = row[7], row[8]

            # 调LLM判断
            try:
                resolution = await conflict_detector._check_contradiction_llm(content_a, content_b)
                chosen = resolution.get("resolution", "keep_both")
                reason = resolution.get("reason", "auto-resolved")
            except Exception as e:
                logger.warning(f"Auto-resolve failed for {cid}: {e}")
                # 降级：保留importance更高的
                chosen = "keep_new" if imp_b >= imp_a else "keep_old"
                reason = f"fallback: importance_a={imp_a}, importance_b={imp_b}"

            # 更新
            conn.execute(
                "UPDATE conflict_log SET resolution = ?, resolved_at = ? WHERE id = ?",
                (chosen, now, cid),
            )
            if chosen == "keep_old":
                conn.execute("UPDATE memories SET is_deleted = 1, updated_at = ? WHERE id = ?", (now, id_b))
            elif chosen == "keep_new":
                conn.execute("UPDATE memories SET is_deleted = 1, updated_at = ? WHERE id = ?", (now, id_a))

            details.append({
                "conflict_id": cid,
                "resolution": chosen,
                "reason": reason,
            })

        conn.commit()

    return {"status": "ok", "resolved": len(details), "details": details}
