"""
MemBind 检索层

HybridRetriever: 两阶段检索（向量召回 + 评分排序）
BindingScorer: 场景绑定评分（规则引擎 + LLM兜底）
"""

import json
import math
import struct
from datetime import datetime, timezone

from config import settings
from db.connection import get_connection
from core.writer import EmbeddingGenerator

embedder = EmbeddingGenerator()


class HybridRetriever:
    """混合检索器：向量语义召回 → 多维度评分排序"""

    async def recall(self, query: str, context: dict | None = None,
                     top_n: int = 20, namespace: str = "default") -> list[dict]:
        """
        第一阶段：向量召回 top_n 条记忆
        返回 [{id, content, tags, score, score_breakdown}]
        """
        # 生成查询embedding
        query_vec = await embedder.generate(query)

        if not query_vec or all(v == 0.0 for v in query_vec):
            # 无embedding时退化为全文搜索
            return self._text_fallback(query, context, top_n, namespace=namespace)

        vec_bytes = struct.pack(f"{len(query_vec)}f", *query_vec)

        with get_connection() as conn:
            try:
                rows = conn.execute(
                    "SELECT id, distance FROM memories_vec WHERE embedding MATCH ? AND k = ?",
                    (vec_bytes, min(top_n * 2, 50)),
                ).fetchall()
            except Exception:
                return self._text_fallback(query, context, top_n)

            if not rows:
                return []

            # 查询对应的记忆详情
            # WHERE m.id = ? AND m.is_deleted = 0
            results = []
            for vec_id, distance in rows:
                # vec_id可能是rowid(int)或id(str)，需要适配
                row = conn.execute(
                    """SELECT m.id, m.content, m.importance, m.hit_count, m.created_at,
                              t.scene, t.task_type, t.entities
                       FROM memories m
                       LEFT JOIN context_tags t ON t.memory_id = m.id
                       WHERE m.id = ? AND m.is_deleted = 0 AND m.namespace = ?""",
                    (str(vec_id), namespace),
                ).fetchone()

                if not row:
                    continue

                mem_id, content, importance, hit_count, created_at, scene, task_type, entities = row
                entities = json.loads(entities) if entities else []

                # 多维度评分
                semantic_sim = 1.0 - distance  # cosine distance → similarity
                time_decay = self._time_decay(created_at)
                importance_norm = importance / 10.0  # 归一化到0-1

                score = (
                    settings.SEMANTIC_WEIGHT * semantic_sim
                    + settings.TIME_DECAY_WEIGHT * time_decay
                    + settings.IMPORTANCE_WEIGHT * importance_norm
                )

                # 上下文场景加权
                if context and context.get("scene") and scene:
                    if context["scene"] == scene:
                        score *= 1.3  # 场景匹配加权30%
                    else:
                        score *= 0.6  # 场景不匹配降权40%

                results.append({
                    "id": mem_id,
                    "content": content,
                    "tags": {"scene": scene, "task_type": task_type, "entities": entities},
                    "importance": importance,
                    "score": round(score, 4),
                    "score_breakdown": {
                        "semantic": round(semantic_sim, 4),
                        "time_decay": round(time_decay, 4),
                        "importance_norm": round(importance_norm, 4),
                    },
                })

            # 簇扩展
            from core.cluster import ClusterManager
            cluster_mgr = ClusterManager()
            result_ids = {r["id"] for r in results}
            expanded = cluster_mgr.expand_cluster(list(result_ids), result_ids)

            # 合并去重
            existing_ids = {r["id"] for r in results}
            for mem in expanded:
                if mem["id"] not in existing_ids:
                    mem["score"] *= 0.7
                    mem["cluster_expanded"] = True
                    results.append(mem)

            # FTS5全文检索 + RRF融合
            fts_results = self._fts_search(query, top_n)
            if fts_results:
                def rrf_score(rank, k=60):
                    return 1.0 / (k + rank + 1)

                merged = {}
                for rank, mem in enumerate(results):
                    mid = mem["id"]
                    if mid not in merged:
                        merged[mid] = mem
                    merged[mid]["rrf_vec"] = rrf_score(rank)

                for rank, mem in enumerate(fts_results):
                    mid = mem["id"]
                    if mid not in merged:
                        merged[mid] = mem
                    merged[mid]["rrf_fts"] = rrf_score(rank)

                for mem in merged.values():
                    vec_s = mem.get("rrf_vec", 0)
                    fts_s = mem.get("rrf_fts", 0)
                    if vec_s > 0 and fts_s > 0:
                        mem["score"] = mem.get("score", 0) * 0.6 + vec_s * 0.2 + fts_s * 0.2
                    elif fts_s > 0:
                        mem["score"] = fts_s

                results = sorted(merged.values(), key=lambda x: x["score"], reverse=True)

            # 按综合分排序取top_n
            results.sort(key=lambda x: x["score"], reverse=True)
            return results[:top_n]

    def _fts_search(self, query: str, top_n: int = 20) -> list[dict]:
        """FTS5全文检索"""
        # FTS5 MATCH语法：特殊字符用引号包裹
        safe_query = query.replace("'", "''")
        # 分词后用AND连接
        tokens = safe_query.split()
        match_expr = " AND ".join(f'"{t}"' for t in tokens) if tokens else '""'

        with get_connection() as conn:
            try:
                rows = conn.execute("""
                    SELECT m.id, m.content, m.importance, m.hit_count, m.created_at,
                           t.scene, t.task_type, t.entities,
                           rank as fts_rank
                    FROM memories_fts fts
                    JOIN memories m ON m.id = fts.memory_id
                    LEFT JOIN context_tags t ON t.memory_id = m.id
                    WHERE memories_fts MATCH ? AND m.is_deleted = 0
                    ORDER BY rank
                    LIMIT ?
                """, (match_expr, top_n)).fetchall()
            except Exception:
                return []

            results = []
            for row in rows:
                mem_id, content, importance, hit_count, created_at, scene, task_type, entities, fts_rank = row
                entities = json.loads(entities) if entities else []
                results.append({
                    "id": mem_id,
                    "content": content,
                    "tags": {"scene": scene, "task_type": task_type, "entities": entities},
                    "importance": importance,
                    "fts_rank": fts_rank,
                    "source": "fts",
                })
            return results

    def _text_fallback(self, query: str, context: dict | None, top_n: int, namespace: str = "default") -> list[dict]:
        """无embedding时的全文搜索降级"""
        keywords = query.lower().split()
        results = []

        with get_connection() as conn:
            rows = conn.execute(
                """SELECT m.id, m.content, m.importance, m.hit_count, m.created_at,
                          t.scene, t.task_type, t.entities
                   FROM memories m
                   LEFT JOIN context_tags t ON t.memory_id = m.id
                   WHERE m.is_deleted = 0 AND m.namespace = ?
                   ORDER BY m.importance DESC, m.created_at DESC
                   LIMIT ?""",
                (namespace, top_n),
            ).fetchall()

            for row in rows:
                mem_id, content, importance, hit_count, created_at, scene, task_type, entities = row
                entities = json.loads(entities) if entities else []
                # 简单关键词匹配打分
                content_lower = content.lower()
                match_count = sum(1 for kw in keywords if kw in content_lower)
                score = match_count / max(len(keywords), 1) * 0.5 + importance / 10.0 * 0.5
                results.append({
                    "id": mem_id,
                    "content": content,
                    "tags": {"scene": scene, "task_type": task_type, "entities": entities},
                    "importance": importance,
                    "score": round(score, 4),
                    "score_breakdown": {"semantic": 0, "time_decay": 0, "importance_norm": importance / 10.0},
                })

            return results

    def _time_decay(self, created_at: str) -> float:
        """时间衰减：0.995^hours，72h后降到约0.70"""
        try:
            created = datetime.fromisoformat(created_at)
            now = datetime.now(timezone.utc)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            hours = max((now - created).total_seconds() / 3600, 0)
            return 0.995 ** hours
        except Exception:
            return 1.0


class BindingScorer:
    """场景绑定评分器：评估记忆与当前查询的绑定强度"""

    def score(self, query: str, memory: dict, context: dict | None = None) -> dict:
        """
        第二阶段：对召回的记忆进行binding评分
        返回 {binding_score, dimensions: {intent, scene, entity, time}}
        """
        # 维度1：意图匹配（关键词重合度）
        query_words = set(query.lower().split())
        content_words = set(memory["content"].lower().split())
        intent_score = len(query_words & content_words) / max(len(query_words), 1)
        intent_score = min(intent_score * 1.5, 1.0)  # 放大：少量重合就算相关

        # 维度2：场景一致性
        mem_scene = memory.get("tags", {}).get("scene", "general")
        ctx_scene = (context or {}).get("scene", "")
        if ctx_scene and mem_scene:
            scene_score = 1.0 if ctx_scene == mem_scene else 0.3
        else:
            scene_score = 0.5  # 无场景信息时给中性分

        # 维度3：实体重叠（Jaccard系数）
        mem_entities = set(e.lower() for e in memory.get("tags", {}).get("entities", []))
        ctx_entities = set(e.lower() for e in (context or {}).get("entities", []))
        # 也从query中提取实体
        query_entities = set(query.lower().split()) & mem_entities
        all_entities = mem_entities | ctx_entities | query_entities
        entity_score = len(mem_entities & (ctx_entities | query_entities)) / max(len(all_entities), 1) if all_entities else 0.5

        # 维度4：时间新鲜度（线性衰减，7天为周期）
        created_at = memory.get("created_at", "")
        try:
            created = datetime.fromisoformat(created_at)
            now = datetime.now(timezone.utc)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            days = (now - created).total_seconds() / 86400
            time_score = max(1.0 - days / 30.0, 0.1)  # 30天线性衰减到0.1
        except Exception:
            time_score = 0.5

        # 加权合并
        binding_score = (
            settings.BINDING_INTENT_WEIGHT * intent_score
            + settings.BINDING_SCENE_WEIGHT * scene_score
            + settings.BINDING_ENTITY_WEIGHT * entity_score
            + settings.BINDING_TIME_WEIGHT * time_score
        )

        return {
            "binding_score": round(binding_score, 4),
            "dimensions": {
                "intent": round(intent_score, 4),
                "scene": round(scene_score, 4),
                "entity": round(entity_score, 4),
                "time": round(time_score, 4),
            },
        }
