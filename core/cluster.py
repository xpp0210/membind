"""
MemBind 知识簇管理器

写入时自动聚类相似记忆，检索时簇级扩展召回。
基于向量余弦相似度，无LLM调用开销。
"""

import math
import struct
import uuid

from db.connection import get_connection


class ClusterManager:
    """知识簇管理器：写入时自动聚类，检索时簇扩展"""

    def __init__(self):
        self.similarity_threshold = 0.75  # 加入已有簇的阈值
        self.merge_threshold = 0.85       # 合并簇的阈值

    async def assign_cluster(self, memory_id: str, embedding: list[float]) -> str | None:
        """
        为新记忆分配簇：
        1. 计算与所有簇中心的余弦相似度
        2. 最高相似度 > 0.75 → 加入该簇，更新簇中心（均值）
        3. 都不满足 → 创建新簇
        4. 返回cluster_id
        """
        if not embedding or len(embedding) == 0:
            return None

        with get_connection() as conn:
            # 获取所有现有簇
            rows = conn.execute(
                "SELECT id, centroid_embedding, member_count FROM knowledge_clusters"
            ).fetchall()

            best_cluster_id = None
            best_sim = -1.0

            for row in rows:
                cluster_id, centroid_blob, member_count = row
                if not centroid_blob:
                    continue
                sim = self._cosine_similarity_blob(centroid_blob, embedding)
                if sim > best_sim:
                    best_sim = sim
                    best_cluster_id = cluster_id

            if best_cluster_id and best_sim > self.similarity_threshold:
                # 加入已有簇，更新簇中心
                self._update_centroid(conn, best_cluster_id, embedding)
                conn.execute(
                    "UPDATE memories SET cluster_id = ? WHERE id = ?",
                    (best_cluster_id, memory_id),
                )
                return best_cluster_id
            else:
                # 创建新簇
                cluster_id = uuid.uuid4().hex[:16]
                centroid_bytes = struct.pack(f"{len(embedding)}f", *embedding)
                conn.execute(
                    "INSERT INTO knowledge_clusters (id, name, centroid_embedding, member_count) VALUES (?, ?, ?, 1)",
                    (cluster_id, f"cluster_{cluster_id[:6]}", centroid_bytes),
                )
                conn.execute(
                    "UPDATE memories SET cluster_id = ? WHERE id = ?",
                    (cluster_id, memory_id),
                )
                return cluster_id

    def expand_cluster(self, memory_ids: list[str], exclude_ids: set[str]) -> list[dict]:
        """
        簇扩展：给定一组记忆ID，找出同簇的其他记忆
        1. 查这些记忆的cluster_id
        2. 同簇但不在exclude_ids中的记忆也召回
        返回扩展后的记忆列表
        """
        if not memory_ids:
            return []

        with get_connection() as conn:
            # 找到这些记忆的cluster_id
            placeholders = ",".join("?" * len(memory_ids))
            rows = conn.execute(
                f"SELECT DISTINCT cluster_id FROM memories WHERE id IN ({placeholders}) AND cluster_id IS NOT NULL",
                memory_ids,
            ).fetchall()

            cluster_ids = [r[0] for r in rows]
            if not cluster_ids:
                return []

            # 查同簇的其他记忆
            cluster_placeholders = ",".join("?" * len(cluster_ids))
            exclude_placeholders = ",".join("?" * len(exclude_ids)) if exclude_ids else ""

            if exclude_ids:
                query = f"""
                    SELECT m.id, m.content, m.importance, m.created_at, m.cluster_id,
                           t.scene, t.task_type, t.entities
                    FROM memories m
                    LEFT JOIN context_tags t ON t.memory_id = m.id
                    WHERE m.cluster_id IN ({cluster_placeholders})
                    AND m.id NOT IN ({exclude_placeholders})
                    AND m.is_deleted = 0
                """
                params = cluster_ids + list(exclude_ids)
            else:
                query = f"""
                    SELECT m.id, m.content, m.importance, m.created_at, m.cluster_id,
                           t.scene, t.task_type, t.entities
                    FROM memories m
                    LEFT JOIN context_tags t ON t.memory_id = m.id
                    WHERE m.cluster_id IN ({cluster_placeholders})
                    AND m.is_deleted = 0
                """
                params = cluster_ids

            rows = conn.execute(query, params).fetchall()
            import json
            results = []
            for row in rows:
                results.append({
                    "id": row[0],
                    "content": row[1],
                    "importance": row[2],
                    "created_at": row[3],
                    "cluster_id": row[4],
                    "tags": {
                        "scene": row[5],
                        "task_type": row[6],
                        "entities": json.loads(row[7]) if row[7] else [],
                    },
                    "score": 0.0,
                })
            return results

    def merge_clusters(self, threshold: float = 0.85) -> int:
        """定期合并相似簇，返回合并数量"""
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT id, centroid_embedding, member_count FROM knowledge_clusters"
            ).fetchall()

            clusters = []
            for row in rows:
                cid, blob, count = row
                if blob:
                    emb = list(struct.unpack(f"{len(blob) // 4}f", blob))
                    clusters.append({"id": cid, "embedding": emb, "count": count})

            merged_count = 0
            merged_ids = set()

            for i in range(len(clusters)):
                if clusters[i]["id"] in merged_ids:
                    continue
                for j in range(i + 1, len(clusters)):
                    if clusters[j]["id"] in merged_ids:
                        continue
                    sim = self._cosine_similarity_vec(clusters[i]["embedding"], clusters[j]["embedding"])
                    if sim > threshold:
                        # 把j的成员归入i
                        target_id = clusters[i]["id"]
                        source_id = clusters[j]["id"]
                        # 更新memories的cluster_id
                        conn.execute(
                            "UPDATE memories SET cluster_id = ? WHERE cluster_id = ?",
                            (target_id, source_id),
                        )
                        # 更新目标簇的member_count
                        new_count = clusters[i]["count"] + clusters[j]["count"]
                        conn.execute(
                            "UPDATE knowledge_clusters SET member_count = ? WHERE id = ?",
                            (new_count, target_id),
                        )
                        # 删除被合并的簇
                        conn.execute("DELETE FROM knowledge_clusters WHERE id = ?", (source_id,))
                        merged_ids.add(source_id)
                        merged_count += 1

            return merged_count

    def get_stats(self) -> dict:
        """获取簇统计信息"""
        with get_connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM knowledge_clusters").fetchone()[0]
            avg_members = conn.execute(
                "SELECT AVG(member_count) FROM knowledge_clusters"
            ).fetchone()[0] or 0

            largest = conn.execute(
                "SELECT id, name, member_count FROM knowledge_clusters ORDER BY member_count DESC LIMIT 5"
            ).fetchall()

            return {
                "total_clusters": total,
                "avg_members": round(avg_members, 2),
                "largest_clusters": [
                    {"id": r[0], "name": r[1], "member_count": r[2]}
                    for r in largest
                ],
            }

    def _cosine_similarity_blob(self, blob: bytes, vec_b: list[float]) -> float:
        """BLOB向量与float列表的余弦相似度"""
        dim = len(blob) // 4
        vec_a = list(struct.unpack(f"{dim}f", blob))
        return self._cosine_similarity_vec(vec_a, vec_b)

    def _cosine_similarity_vec(self, vec_a: list[float], vec_b: list[float]) -> float:
        """两个float列表的余弦相似度"""
        dim = min(len(vec_a), len(vec_b))
        if dim == 0:
            return 0.0
        dot = sum(vec_a[i] * vec_b[i] for i in range(dim))
        norm_a = math.sqrt(sum(v * v for v in vec_a[:dim]))
        norm_b = math.sqrt(sum(v * v for v in vec_b[:dim]))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def _update_centroid(self, conn, cluster_id: str, new_embedding: list[float]) -> None:
        """增量更新簇中心（均值）"""
        row = conn.execute(
            "SELECT centroid_embedding, member_count FROM knowledge_clusters WHERE id = ?",
            (cluster_id,),
        ).fetchone()
        if not row or not row[0]:
            return

        old_blob = row[0]
        old_count = row[1]
        dim = len(old_blob) // 4
        old_vec = list(struct.unpack(f"{dim}f", old_blob))

        # 增量均值: new_centroid = old_centroid + (new_vec - old_centroid) / (old_count + 1)
        new_dim = min(dim, len(new_embedding))
        updated = []
        for i in range(new_dim):
            val = old_vec[i] + (new_embedding[i] - old_vec[i]) / (old_count + 1)
            updated.append(val)
        # 保持维度一致
        updated.extend(old_vec[new_dim:])

        new_blob = struct.pack(f"{len(updated)}f", *updated)
        conn.execute(
            "UPDATE knowledge_clusters SET centroid_embedding = ?, member_count = ? WHERE id = ?",
            (new_blob, old_count + 1, cluster_id),
        )


# 模块级单例
cluster_manager = ClusterManager()
