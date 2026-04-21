"""
MemBind 绑定服务

记录binding_history + 更新命中计数 + 反馈处理 + 统计
"""

import json
from datetime import datetime, timezone

from db.connection import get_connection


def record_binding(memory_id: str, query: str, binding_score: float,
                   context: dict | None = None):
    """记录一次binding激活"""
    ctx_scene = (context or {}).get("scene", "")
    ctx_task = (context or {}).get("task_type", "")
    now = datetime.now(timezone.utc).isoformat()

    with get_connection() as conn:
        conn.execute(
            """INSERT INTO binding_history (memory_id, query, context_scene, context_task,
               binding_score, activated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (memory_id, query, ctx_scene, ctx_task, binding_score, now),
        )
        conn.execute(
            """UPDATE memories SET hit_count = hit_count + 1,
               binding_count = binding_count + 1,
               updated_at = ? WHERE id = ?""",
            (now, memory_id),
        )
        conn.commit()


def update_feedback(memory_id: str, query: str, relevant: bool,
                    context: dict | None = None):
    """更新binding反馈"""
    now = datetime.now(timezone.utc).isoformat()

    with get_connection() as conn:
        row = conn.execute(
            """SELECT id FROM binding_history
               WHERE memory_id = ? AND query = ?
               ORDER BY activated_at DESC LIMIT 1""",
            (memory_id, query),
        ).fetchone()

        if row:
            conn.execute(
                "UPDATE binding_history SET was_relevant = ? WHERE id = ?",
                (1 if relevant else 0, row[0]),
            )

        delta = 0.5 if relevant else -0.5
        conn.execute(
            """UPDATE memories SET importance = MAX(0, MIN(10, importance + ?)),
               updated_at = ? WHERE id = ?""",
            (delta, now, memory_id),
        )
        conn.commit()


def get_stats(namespace: str = "default") -> dict:
    """获取系统统计（按namespace过滤）"""
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM memories WHERE is_deleted = 0 AND namespace = ?", (namespace,)).fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM memories WHERE is_deleted = 0 AND importance > 1.0 AND namespace = ?", (namespace,)).fetchone()[0]

        feedback_rows = conn.execute(
            "SELECT was_relevant FROM binding_history WHERE was_relevant IS NOT NULL"
        ).fetchall()
        total_feedback = len(feedback_rows)
        relevant_count = sum(1 for r in feedback_rows if r[0] == 1)
        accuracy = relevant_count / total_feedback if total_feedback > 0 else None

        conflicts = conn.execute("SELECT COUNT(*) FROM conflict_log").fetchone()[0]

        scene_rows = conn.execute(
            """SELECT COALESCE(t.scene, 'general') as scene, COUNT(*) as cnt
               FROM memories m LEFT JOIN context_tags t ON t.memory_id = m.id
               WHERE m.is_deleted = 0 AND m.namespace = ? GROUP BY scene ORDER BY cnt DESC LIMIT 5""",
            (namespace,),
        ).fetchall()
        scene_dist = {row[0]: row[1] for row in scene_rows}

        bindings = conn.execute("SELECT COUNT(*) FROM binding_history").fetchone()[0]

        return {
            "total_memories": total,
            "active_memories": active,
            "total_bindings": bindings,
            "binding_accuracy": round(accuracy, 4) if accuracy else None,
            "pending_conflicts": conflicts,
            "scene_distribution": scene_dist,
        }
