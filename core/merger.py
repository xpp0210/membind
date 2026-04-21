"""
MemBind 记忆合并器

将两条记忆合并为一条，原记忆软删除。
有LLM key时用LLM合并，否则简单拼接。
"""

import json
import logging
import uuid
from datetime import datetime

from config import settings
from db.connection import get_connection

logger = logging.getLogger(__name__)


class Merger:
    """记忆合并器"""

    async def merge(self, id_a: str, id_b: str) -> dict:
        """合并两条记忆"""
        # 读取两条记忆
        with get_connection() as conn:
            row_a = conn.execute(
                "SELECT id, content, importance FROM memories WHERE id = ? AND is_deleted = 0",
                (id_a,),
            ).fetchone()
            row_b = conn.execute(
                "SELECT id, content, importance FROM memories WHERE id = ? AND is_deleted = 0",
                (id_b,),
            ).fetchone()

        if not row_a:
            return {"error": f"memory {id_a} not found or deleted"}
        if not row_b:
            return {"error": f"memory {id_b} not found or deleted"}

        content_a, content_b = row_a["content"], row_b["content"]
        importance = max(row_a["importance"], row_b["importance"])

        # 尝试LLM合并，失败时简单拼接
        merged_content = await self._llm_merge(content_a, content_b)

        # 写入新记忆
        merged_id = uuid.uuid4().hex[:16]
        now = datetime.utcnow().isoformat()

        with get_connection() as conn:
            # 简单查询获取scene和entities
            scene_a = conn.execute(
                "SELECT COALESCE(t.scene, 'general') FROM context_tags t WHERE t.memory_id = ?", (id_a,)
            ).fetchone()
            scene_b = conn.execute(
                "SELECT COALESCE(t.scene, 'general') FROM context_tags t WHERE t.memory_id = ?", (id_b,)
            ).fetchone()
            scene = scene_a[0] if scene_a else "general"

            # 合并entities
            entities_a = conn.execute(
                "SELECT entities FROM context_tags WHERE memory_id = ?", (id_a,)
            ).fetchone()
            entities_b = conn.execute(
                "SELECT entities FROM context_tags WHERE memory_id = ?", (id_b,)
            ).fetchone()
            merged_entities = set()
            for e in [entities_a, entities_b]:
                if e and e[0]:
                    try:
                        merged_entities.update(json.loads(e[0]))
                    except (json.JSONDecodeError, TypeError):
                        pass

            conn.execute(
                """INSERT INTO memories (id, content, importance, metadata, created_at, updated_at)
                   VALUES (?, ?, ?, '{}', ?, ?)""",
                (merged_id, merged_content, importance, now, now),
            )
            tag_id = uuid.uuid4().hex[:16]
            conn.execute(
                """INSERT INTO context_tags (id, memory_id, scene, task_type, entities, created_at)
                   VALUES (?, ?, ?, '', ?, ?)""",
                (tag_id, merged_id, scene,
                 json.dumps(list(merged_entities), ensure_ascii=False), now),
            )
            # 软删除原记忆
            conn.execute(
                "UPDATE memories SET is_deleted = 1, updated_at = ? WHERE id IN (?, ?)",
                (now, id_a, id_b),
            )
            conn.commit()

        return {
            "merged_id": merged_id,
            "merged_content": merged_content,
            "status": "ok",
        }

    async def _llm_merge(self, content_a: str, content_b: str) -> str:
        """用LLM合并两条记忆内容，失败时简单拼接"""
        if not settings.LLM_API_KEY:
            return f"{content_a}\n---\n{content_b}"

        try:
            import httpx

            prompt = (
                f"合并以下两条记忆，保留所有关键信息，去除重复内容，输出合并后的纯文本：\n"
                f"记忆A: {content_a}\n"
                f"记忆B: {content_b}\n"
                f"合并结果："
            )

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{settings.LLM_API_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
                    json={
                        "model": settings.LLM_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                    },
                )
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"LLM merge failed, falling back to concat: {e}")

        return f"{content_a}\n---\n{content_b}"


merger = Merger()
