"""
MemBind 冲突检测器

写入时冲突预检 + 检索时冲突警告
"""

import json
import struct
import uuid
import logging
import httpx
from datetime import datetime, timezone
from dataclasses import dataclass, field

from config import settings
from db.connection import get_connection
from core.utils import cosine_similarity, blob_to_floats

logger = logging.getLogger(__name__)

# ── 模块级 httpx 连接池（复用连接） ──
_llm_client: httpx.AsyncClient | None = None


async def _get_llm_client() -> httpx.AsyncClient:
    global _llm_client
    if _llm_client is None or _llm_client.is_closed:
        _llm_client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
        )
    return _llm_client


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
        """检查新记忆是否与已有记忆冲突（KNN预筛选 + LLM判断）"""
        if not new_embedding or all(v == 0.0 for v in new_embedding):
            return ConflictCheckResult(check="clear")

        vec_bytes = struct.pack(f"{len(new_embedding)}f", *new_embedding)

        # KNN 预筛选 Top-30
        try:
            with get_connection() as conn:
                knn_rows = conn.execute(
                    "SELECT id, distance FROM memories_vec WHERE embedding MATCH ? AND k = 30",
                    (vec_bytes,),
                ).fetchall()
        except Exception:
            return ConflictCheckResult(check="clear")

        if not knn_rows:
            return ConflictCheckResult(check="clear")

        # 过滤超过阈值的
        high_sim_rows = [(vid, 1.0 - dist) for vid, dist in knn_rows if (1.0 - dist) > settings.CONFLICT_THRESHOLD]
        if not high_sim_rows:
            return ConflictCheckResult(check="clear")

        # 获取 content
        vec_ids = [str(vid) for vid, _ in high_sim_rows]
        with get_connection() as conn:
            placeholders = ",".join("?" * len(vec_ids))
            rows = conn.execute(
                f"""SELECT m.id, m.content
                FROM memories m
                WHERE m.id IN ({placeholders}) AND m.is_deleted = 0""",
                vec_ids,
            ).fetchall()

        mem_map = {str(row[0]): row[1] for row in rows}

        # LLM 并行判断（P1-3）
        import asyncio
        candidates = []
        for vid, sim in high_sim_rows:
            mem_content = mem_map.get(str(vid), "")
            if mem_content:
                candidates.append((str(vid), mem_content, sim))

        llm_results = await asyncio.gather(
            *[self._check_contradiction_llm(new_content, content)
              for _, content, _ in candidates],
            return_exceptions=True,
        )

        conflicts: list[ConflictInfo] = []
        similar: list[ConflictInfo] = []

        for (mem_id, mem_content, sim), llm_result in zip(candidates, llm_results):
            if isinstance(llm_result, Exception):
                continue
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

    # ── 写入时快速冲突检测（KNN Top-20 向量比对，不调LLM） ──
    async def detect_write_conflict_fast(
        self, content: str, embedding: list[float]
    ) -> dict:
        """
        写入时快速冲突检测（KNN Top-20 向量比对，不调LLM）
        先用 sqlite-vec KNN 取 Top-20，再做精确比对
        """
        if not embedding or all(v == 0.0 for v in embedding):
            return {"has_conflict": False, "conflicts": []}

        vec_bytes = struct.pack(f"{len(embedding)}f", *embedding)

        # Step 1: KNN 预筛选（sqlite-vec 向量搜索）
        try:
            with get_connection() as conn:
                knn_rows = conn.execute(
                    "SELECT id, distance FROM memories_vec WHERE embedding MATCH ? AND k = 20",
                    (vec_bytes,),
                ).fetchall()
        except Exception:
            return {"has_conflict": False, "conflicts": []}

        if not knn_rows:
            return {"has_conflict": False, "conflicts": []}

        # Step 2: 对 Top-20 获取 content，计算精确相似度
        vec_ids = [str(row[0]) for row in knn_rows]
        scored = []
        with get_connection() as conn:
            placeholders = ",".join("?" * len(vec_ids))
            rows = conn.execute(
                f"""SELECT m.id, m.content
                FROM memories m
                WHERE m.id IN ({placeholders}) AND m.is_deleted = 0""",
                vec_ids,
            ).fetchall()

        for row in rows:
            mem_id, mem_content = row[0], row[1]
            # 从 KNN 结果中找距离
            distance = None
            for vid, dist in knn_rows:
                if str(vid) == str(mem_id):
                    distance = dist
                    break
            if distance is not None:
                sim = 1.0 - distance  # cosine distance → similarity
                scored.append({
                    "id": mem_id,
                    "similarity": round(sim, 4),
                    "content_preview": (mem_content or "")[:100],
                })

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
                    embeddings[mid] = blob_to_floats(row[0], settings.EMBEDDING_DIM)

        # 两两比较
        for i in range(len(results)):
            for j in range(i + 1, len(results)):
                id_a = results[i].get("id")
                id_b = results[j].get("id")
                emb_a = embeddings.get(id_a)
                emb_b = embeddings.get(id_b)
                if not emb_a or not emb_b:
                    continue

                sim = cosine_similarity(emb_a, emb_b)
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

    # ── LLM矛盾判断 ──
    async def _check_contradiction_llm(
        self, content_a: str, content_b: str
    ) -> dict:
        """用LLM判断两条记忆是否矛盾，失败时默认不矛盾"""
        try:
            prompt = (
                f"判断以下两条记忆是否矛盾。\n"
                f"记忆A: {content_a}\n"
                f"记忆B: {content_b}\n"
                f"输出JSON: {json.dumps({'contradiction': False, 'reason': '', 'resolution': 'keep_both'})}\n"
                f"resolution可选: keep_both, keep_new, keep_old"
            )

            client = await _get_llm_client()
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
        now = datetime.now(timezone.utc).isoformat()

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

    @staticmethod
    def _default_no_conflict() -> dict:
        return {"contradiction": False, "reason": "", "resolution": "keep_both"}


# 单例
conflict_detector = ConflictDetector()
