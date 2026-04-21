"""
MemBind 冲突检测器

写入时冲突预检 + 检索时冲突警告
"""

import json
import math
import uuid
import logging
from datetime import datetime
from dataclasses import dataclass, field

from config import settings
from db.connection import get_connection

logger = logging.getLogger(__name__)


@dataclass
class ConflictInfo:
    memory_id_a: str
    memory_id_b: str
    content_a: str
    content_b: str
    similarity: float
    contradiction: bool
    reason: str = ""
    resolution: str = "keep_both"


@dataclass
class ConflictCheckResult:
    check: str  # "clear" | "conflict" | "similar"
    conflicts: list[ConflictInfo] = field(default_factory=list)


class ConflictDetector:

    # ── 写入时冲突预检 ──
    async def detect_write_conflict(
        self, new_content: str, new_embedding: list[float]
    ) -> ConflictCheckResult:
        """检查新记忆是否与已有记忆冲突"""
        # 获取所有非删除记忆的embedding和content
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT m.id, m.content, v.embedding
                   FROM memories m
                   JOIN memories_vec v ON v.id = m.id
                   WHERE m.is_deleted = 0"""
            ).fetchall()

        if not rows:
            return ConflictCheckResult(check="clear")

        conflicts: list[ConflictInfo] = []
        similar: list[ConflictInfo] = []

        for row in rows:
            mem_id, mem_content, emb_bytes = row[0], row[1], row[2]
            if not emb_bytes:
                continue

            # 解码embedding bytes → float list
            existing_emb = self._bytes_to_floats(emb_bytes, settings.EMBEDDING_DIM)
            if not existing_emb:
                continue

            sim = self._cosine_similarity(new_embedding, existing_emb)
            if sim <= settings.CONFLICT_THRESHOLD:
                continue

            # 高相似度 → LLM判断是否矛盾
            llm_result = await self._check_contradiction_llm(new_content, mem_content)

            info = ConflictInfo(
                memory_id_a="new",
                memory_id_b=mem_id,
                content_a=new_content,
                content_b=mem_content,
                similarity=round(sim, 4),
                contradiction=llm_result["contradiction"],
                reason=llm_result.get("reason", ""),
                resolution=llm_result.get("resolution", "keep_both"),
            )

            if llm_result["contradiction"]:
                conflicts.append(info)
            else:
                similar.append(info)

        if conflicts:
            return ConflictCheckResult(check="conflict", conflicts=conflicts)
        if similar:
            return ConflictCheckResult(check="similar", conflicts=similar)
        return ConflictCheckResult(check="clear")

    # ── 写入时快速冲突检测（Top-10向量比对，不调LLM） ──
    async def detect_write_conflict_fast(
        self, content: str, embedding: list[float]
    ) -> dict:
        """
        写入时快速冲突检测（Top-10向量比对）
        与detect_write_conflict的区别：不做LLM判定，只做向量相似度比对
        返回: {"has_conflict": bool, "conflicts": [{"id", "similarity", "content_preview"}]}
        """
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT m.id, m.content, v.embedding
                   FROM memories m
                   JOIN memories_vec v ON v.id = m.id
                   WHERE m.is_deleted = 0"""
            ).fetchall()

        if not rows:
            return {"has_conflict": False, "conflicts": []}

        scored = []
        for row in rows:
            mem_id, mem_content, emb_bytes = row[0], row[1], row[2]
            if not emb_bytes:
                continue
            existing_emb = self._bytes_to_floats(emb_bytes, settings.EMBEDDING_DIM)
            if not existing_emb:
                continue
            sim = self._cosine_similarity(embedding, existing_emb)
            scored.append({
                "id": mem_id,
                "similarity": round(sim, 4),
                "content_preview": (mem_content or "")[:100],
            })

        # Top-10
        scored.sort(key=lambda x: x["similarity"], reverse=True)
        top = scored[:10]
        conflicts = [m for m in top if m["similarity"] > settings.CONFLICT_THRESHOLD]

        return {"has_conflict": len(conflicts) > 0, "conflicts": conflicts}

    # ── 检索时冲突检测 ──
    async def detect_recall_conflicts(
        self, results: list[dict]
    ) -> list[dict]:
        """对检索结果两两检查冲突"""
        if len(results) < 2:
            return []

        warnings = []
        # 收集embedding
        mem_ids = [r.get("id") for r in results if r.get("id")]
        embeddings = {}

        with get_connection() as conn:
            for mid in mem_ids:
                row = conn.execute(
                    "SELECT embedding FROM memories_vec WHERE id = ?", (mid,)
                ).fetchone()
                if row and row[0]:
                    embeddings[mid] = self._bytes_to_floats(row[0], settings.EMBEDDING_DIM)

        # 两两比较
        for i in range(len(results)):
            for j in range(i + 1, len(results)):
                id_a = results[i].get("id")
                id_b = results[j].get("id")
                emb_a = embeddings.get(id_a)
                emb_b = embeddings.get(id_b)
                if not emb_a or not emb_b:
                    continue

                sim = self._cosine_similarity(emb_a, emb_b)
                if sim > settings.CONFLICT_THRESHOLD:
                    warnings.append({
                        "memory_ids": [id_a, id_b],
                        "similarity": round(sim, 4),
                        "contents": [
                            results[i].get("content", "")[:80],
                            results[j].get("content", "")[:80],
                        ],
                    })

        return warnings

    # ── 余弦相似度（纯Python） ──
    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)

    # ── LLM矛盾判断 ──
    async def _check_contradiction_llm(
        self, content_a: str, content_b: str
    ) -> dict:
        """用LLM判断两条记忆是否矛盾，失败时默认不矛盾"""
        try:
            import httpx

            prompt = (
                f"判断以下两条记忆是否矛盾。\n"
                f"记忆A: {content_a}\n"
                f"记忆B: {content_b}\n"
                f"输出JSON: {json.dumps({'contradiction': False, 'reason': '', 'resolution': 'keep_both'})}\n"
                f"resolution可选: keep_both, keep_new, keep_old"
            )

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{settings.LLM_API_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
                    json={
                        "model": settings.LLM_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                    },
                )
                if resp.status_code != 200:
                    return self._default_no_conflict()

                text = resp.json()["choices"][0]["message"]["content"]
                # 提取JSON
                start = text.find("{")
                end = text.rfind("}") + 1
                if start >= 0 and end > start:
                    result = json.loads(text[start:end])
                    return {
                        "contradiction": bool(result.get("contradiction", False)),
                        "reason": str(result.get("reason", "")),
                        "resolution": str(result.get("resolution", "keep_both")),
                    }
        except Exception as e:
            logger.warning(f"LLM contradiction check failed: {e}")

        return self._default_no_conflict()

    # ── 写入conflict_log ──
    def log_conflict(self, conflict: ConflictInfo, new_memory_id: str):
        """将冲突记录到conflict_log表"""
        id_a = min(new_memory_id, conflict.memory_id_b)
        id_b = max(new_memory_id, conflict.memory_id_b)
        now = datetime.utcnow().isoformat()

        with get_connection() as conn:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO conflict_log
                       (id, memory_id_a, memory_id_b, similarity, resolution, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (uuid.uuid4().hex[:16], id_a, id_b,
                     conflict.similarity, conflict.resolution, now),
                )
                conn.commit()
            except Exception as e:
                logger.warning(f"Failed to log conflict: {e}")

    # ── 工具方法 ──
    @staticmethod
    def _bytes_to_floats(data: bytes, dim: int) -> list[float]:
        import struct
        if not data:
            return []
        try:
            return list(struct.unpack(f"{dim}f", data[: dim * 4]))
        except Exception:
            return []

    @staticmethod
    def _default_no_conflict() -> dict:
        return {"contradiction": False, "reason": "", "resolution": "keep_both"}


# 单例
conflict_detector = ConflictDetector()
