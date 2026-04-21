"""
MemBind 生命周期管理

LifecycleManager: 记忆衰减、清理、恢复
"""

from datetime import datetime, timezone

from config import settings
from db.connection import get_connection


class LifecycleManager:
    """记忆生命周期管理器"""

    def decay_all(self, dry_run: bool = False) -> dict:
        """对所有活跃记忆执行importance衰减。活跃度高的衰减慢。"""
        decay_amount = settings.IMPORTANCE_DECAY
        now = datetime.now(timezone.utc).isoformat()
        affected = []

        with get_connection() as conn:
            rows = conn.execute(
                """SELECT id, importance, hit_count, binding_count
                   FROM memories WHERE is_deleted = 0 AND importance > 0"""
            ).fetchall()

            for row in rows:
                mem_id, importance, hit_count, binding_count = row
                activity_ratio = binding_count / max(hit_count, 1) if hit_count > 0 else 0.0
                actual_decay = decay_amount * (1.0 - min(activity_ratio, 1.0))
                new_importance = max(0.0, importance - actual_decay)

                if not dry_run:
                    conn.execute(
                        "UPDATE memories SET importance = ?, updated_at = ? WHERE id = ?",
                        (new_importance, now, mem_id),
                    )
                affected.append({
                    "id": mem_id,
                    "old_importance": round(importance, 4),
                    "new_importance": round(new_importance, 4),
                    "decay": round(actual_decay, 4),
                })

            if not dry_run:
                conn.commit()

        return {
            "action": "decay",
            "dry_run": dry_run,
            "affected_count": len(affected),
            "details": affected,
        }

    def cleanup(self, dry_run: bool = False) -> dict:
        """清理importance低于阈值的记忆（软删除）"""
        threshold = settings.CLEANUP_THRESHOLD
        now = datetime.now(timezone.utc).isoformat()
        cleaned = []

        with get_connection() as conn:
            rows = conn.execute(
                """SELECT id, content, importance, created_at
                   FROM memories WHERE is_deleted = 0 AND importance < ?""",
                (threshold,),
            ).fetchall()

            for row in rows:
                mem_id, content, importance, created_at = row
                if not dry_run:
                    conn.execute(
                        "UPDATE memories SET is_deleted = 1, updated_at = ? WHERE id = ?",
                        (now, mem_id),
                    )
                cleaned.append({
                    "id": mem_id,
                    "content": content[:80],
                    "importance": round(importance, 4),
                    "created_at": created_at,
                })

            if not dry_run:
                conn.commit()

        return {
            "action": "cleanup",
            "dry_run": dry_run,
            "threshold": threshold,
            "cleaned_count": len(cleaned),
            "details": cleaned,
        }

    def boost(self, memory_id: str, amount: float | None = None) -> dict:
        """手动提升记忆importance"""
        boost_amount = amount if amount is not None else settings.IMPORTANCE_BOOST
        now = datetime.now(timezone.utc).isoformat()

        with get_connection() as conn:
            row = conn.execute(
                "SELECT importance FROM memories WHERE id = ? AND is_deleted = 0",
                (memory_id,),
            ).fetchone()

            if not row:
                return {"error": "memory not found or already deleted"}

            old_importance = row[0]
            new_importance = min(10.0, old_importance + boost_amount)

            conn.execute(
                "UPDATE memories SET importance = ?, updated_at = ? WHERE id = ?",
                (new_importance, now, memory_id),
            )
            conn.commit()

        return {
            "status": "ok",
            "id": memory_id,
            "old_importance": round(old_importance, 4),
            "new_importance": round(new_importance, 4),
            "boost": round(boost_amount, 4),
        }

    def restore(self, memory_id: str) -> dict:
        """恢复已软删除的记忆"""
        now = datetime.now(timezone.utc).isoformat()

        with get_connection() as conn:
            row = conn.execute(
                "SELECT is_deleted FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()

            if not row:
                return {"error": "memory not found"}

            if not row[0]:
                return {"error": "memory is not deleted"}

            conn.execute(
                "UPDATE memories SET is_deleted = 0, importance = 5.0, updated_at = ? WHERE id = ?",
                (now, memory_id),
            )
            conn.commit()

        return {"status": "ok", "id": memory_id, "action": "restored", "importance": 5.0}

    def get_decay_candidates(self, limit: int = 20) -> list[dict]:
        """获取importance最低的记忆（即将被清理）"""
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT id, content, importance, hit_count, binding_count, updated_at
                   FROM memories WHERE is_deleted = 0
                   ORDER BY importance ASC LIMIT ?""",
                (limit,),
            ).fetchall()

            return [
                {
                    "id": r[0],
                    "content": r[1][:80],
                    "importance": round(r[2], 4),
                    "hit_count": r[3],
                    "binding_count": r[4],
                    "updated_at": r[5],
                }
                for r in rows
            ]


lifecycle_manager = LifecycleManager()
