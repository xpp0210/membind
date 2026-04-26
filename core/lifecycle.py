"""
MemBind 生命周期管理（V2）

基于认知科学的记忆衰减模型：
- 指数衰减（Ebbinghaus遗忘曲线），衰减速率受巩固等级调节
- 记忆巩固机制：频繁激活的记忆逐步巩固，衰减变慢
- 支持手动提升、恢复、候选预览
"""

import math
from datetime import datetime, timezone

from config import settings
from db.connection import get_connection


class LifecycleManager:
    """记忆生命周期管理器（认知科学衰减模型）"""

    def decay_all(self, dry_run: bool = False, namespace: str = "default") -> dict:
        """
        对所有活跃记忆执行importance衰减。

        V2衰减公式（Ebbinghaus指数衰减）：
          decay = DECAY_STEP * time_factor * activity_factor * consolidation_factor * importance_factor

        其中：
          time_factor = (HALF_LIFE / (HALF_LIFE + hours))^2
          activity_factor = 1.0 - min(binding_ratio, 1.0) * 0.5
          consolidation_factor = 1.0 / (1.0 + consolidation_level)
          importance_factor = importance / 10.0
        """
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        affected = []

        with get_connection() as conn:
            rows = conn.execute(
                """SELECT id, importance, hit_count, binding_count,
                          COALESCE(consolidation_level, 0) as consolidation_level,
                          updated_at
                   FROM memories WHERE is_deleted = 0 AND importance > 0 AND namespace = ?""",
                (namespace,),
            ).fetchall()

            for row in rows:
                mem_id, importance, hit_count, binding_count, consolidation_level, updated_at = row
                actual_decay = self._calculate_decay(
                    importance, hit_count, binding_count, consolidation_level, updated_at, now
                )
                new_importance = max(0.0, importance - actual_decay)

                if not dry_run:
                    conn.execute(
                        "UPDATE memories SET importance = ?, updated_at = ? WHERE id = ?",
                        (new_importance, now_iso, mem_id),
                    )
                affected.append({
                    "id": mem_id,
                    "old_importance": round(importance, 4),
                    "new_importance": round(new_importance, 4),
                    "decay": round(actual_decay, 4),
                    "consolidation_level": consolidation_level,
                })

            if not dry_run:
                conn.commit()

        return {
            "action": "decay",
            "model": "ebbinghaus_v2",
            "dry_run": dry_run,
            "affected_count": len(affected),
            "details": affected,
        }

    def _calculate_decay(
        self,
        importance: float,
        hit_count: int,
        binding_count: int,
        consolidation_level: int,
        updated_at: str,
        now: datetime,
    ) -> float:
        """
        计算单条记忆的衰减量（Ebbinghaus指数衰减模型）。

        公式拆解：
          1. time_factor: 时间因子，初始衰减快后期慢
             = (HALF_LIFE / (HALF_LIFE + hours))^2
          2. activity_factor: 活跃度因子，binding_ratio高的记忆衰减慢
             = 1.0 - min(binding_ratio, 1.0) * 0.5
          3. consolidation_factor: 巩固因子，等级越高衰减越慢
             = 1.0 / (1.0 + consolidation_level)
             0->1.0x, 1->0.5x, 2->0.33x, 3->0.25x
          4. importance_factor: 低importance记忆衰减更慢
             = importance / 10.0
        """
        # 时间因子
        try:
            updated = datetime.fromisoformat(updated_at)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            hours = max((now - updated).total_seconds() / 3600, 0.1)
        except Exception:
            hours = 1.0

        half_life = settings.DECAY_HALF_LIFE_HOURS
        time_factor = (half_life / (half_life + hours)) ** 2

        # 活跃度因子
        binding_ratio = binding_count / max(hit_count, 1) if hit_count > 0 else 0.0
        activity_factor = 1.0 - min(binding_ratio, 1.0) * 0.5

        # 巩固因子
        consolidation_factor = 1.0 / (1.0 + consolidation_level)

        # importance因子（低importance衰减更慢，避免快速清理）
        importance_factor = importance / 10.0

        decay = settings.DECAY_STEP * time_factor * activity_factor * consolidation_factor * importance_factor

        return decay

    def consolidate(self, namespace: str = "default", dry_run: bool = False) -> dict:
        """
        记忆巩固：提升符合条件的记忆的巩固等级。

        巩固条件（全部满足）：
          - binding_count >= 阈值（level 0->1: 5次, 1->2: 15次, 2->3: 30次）
          - binding_accuracy >= CONSOLIDATION_MIN_ACCURACY
          - 距上次巩固 >= CONSOLIDATION_MIN_HOURS
        """
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        thresholds = settings.consolidation_thresholds_list
        consolidated = []

        with get_connection() as conn:
            rows = conn.execute(
                """SELECT m.id, m.content, m.binding_count,
                          COALESCE(m.consolidation_level, 0) as consolidation_level,
                          m.updated_at,
                          COALESCE(SUM(CASE WHEN bh.was_relevant = 1 THEN 1 ELSE 0 END), 0) as relevant_count,
                          COUNT(bh.id) as total_bindings
                   FROM memories m
                   LEFT JOIN binding_history bh ON bh.memory_id = m.id
                   WHERE m.is_deleted = 0 AND m.namespace = ?
                   GROUP BY m.id
                   HAVING m.binding_count > 0""",
                (namespace,),
            ).fetchall()

            for row in rows:
                mem_id, content, binding_count, level, updated_at, relevant_count, total_bindings = row
                current_level = level

                if current_level >= 3:
                    continue

                accuracy = relevant_count / max(total_bindings, 1)
                next_threshold = thresholds[min(current_level, len(thresholds) - 1)]

                if binding_count < next_threshold:
                    continue
                if accuracy < settings.CONSOLIDATION_MIN_ACCURACY:
                    continue

                # 时间间隔检查
                try:
                    updated = datetime.fromisoformat(updated_at)
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    hours_since = (now - updated).total_seconds() / 3600
                except Exception:
                    hours_since = 25.0

                if hours_since < settings.CONSOLIDATION_MIN_HOURS:
                    continue

                new_level = current_level + 1

                if not dry_run:
                    conn.execute(
                        "UPDATE memories SET consolidation_level = ?, updated_at = ? WHERE id = ?",
                        (new_level, now_iso, mem_id),
                    )

                consolidated.append({
                    "id": mem_id,
                    "content": content[:80],
                    "old_level": current_level,
                    "new_level": new_level,
                    "binding_count": binding_count,
                    "accuracy": round(accuracy, 4),
                })

            if not dry_run:
                conn.commit()

        return {
            "action": "consolidate",
            "dry_run": dry_run,
            "consolidated_count": len(consolidated),
            "details": consolidated,
        }

    def cleanup(self, dry_run: bool = False, namespace: str = "default") -> dict:
        """清理importance低于阈值的记忆（软删除）"""
        threshold = settings.CLEANUP_THRESHOLD
        now = datetime.now(timezone.utc).isoformat()
        cleaned = []

        with get_connection() as conn:
            rows = conn.execute(
                """SELECT id, content, importance, created_at
                   FROM memories WHERE is_deleted = 0 AND importance < ? AND namespace = ?""",
                (threshold, namespace),
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
        """恢复已软删除的记忆，重置巩固等级"""
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
                "UPDATE memories SET is_deleted = 0, importance = 5.0, consolidation_level = 0, updated_at = ? WHERE id = ?",
                (now, memory_id),
            )
            conn.commit()

        return {"status": "ok", "id": memory_id, "action": "restored", "importance": 5.0, "consolidation_level": 0}

    def get_decay_candidates(self, limit: int = 20, namespace: str = "default") -> list[dict]:
        """获取importance最低的记忆（即将被清理）"""
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT id, content, importance, hit_count, binding_count, updated_at,
                          COALESCE(consolidation_level, 0) as consolidation_level
                   FROM memories WHERE is_deleted = 0
                   AND namespace = ?
                   ORDER BY importance ASC LIMIT ?""",
                (namespace, limit),
            ).fetchall()

            return [
                {
                    "id": r[0],
                    "content": r[1][:80],
                    "importance": round(r[2], 4),
                    "hit_count": r[3],
                    "binding_count": r[4],
                    "updated_at": r[5],
                    "consolidation_level": r[6],
                }
                for r in rows
            ]


lifecycle_manager = LifecycleManager()
