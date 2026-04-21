"""
知识簇测试

覆盖：创建簇、加入簇、簇扩展、合并、MCP工具、迁移
"""

import os
import sys
import struct
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from db.connection import init_db, get_connection
from core.cluster import ClusterManager


@pytest_asyncio.fixture(autouse=True)
async def setup_db(tmp_path):
    """每个测试用独立的临时数据库"""
    db_path = str(tmp_path / "test_cluster.db")
    original = settings.MEMBIND_DB_PATH
    settings.MEMBIND_DB_PATH = db_path
    os.environ.pop("EMBEDDING_API_KEY", None)
    os.environ.pop("LLM_API_KEY", None)
    settings.EMBEDDING_API_KEY = ""
    settings.LLM_API_KEY = ""
    init_db(db_path)
    yield
    settings.MEMBIND_DB_PATH = original


def _make_embedding(seed: float, dim: int = 1024) -> list[float]:
    """生成确定性测试向量，不同seed产生不同方向的向量"""
    import math
    vec = []
    for i in range(dim):
        # 用seed决定相位，使不同seed的向量方向不同
        vec.append(math.sin(seed + i * 0.1))
    return vec


def _insert_memory(conn, memory_id: str, content: str, cluster_id: str | None = None,
                   embedding: list[float] | None = None):
    """插入测试记忆"""
    import json
    conn.execute(
        "INSERT INTO memories (id, content, importance, metadata, created_at, updated_at, cluster_id) VALUES (?, ?, 5.0, '{}', '2026-01-01T00:00:00', '2026-01-01T00:00:00', ?)",
        (memory_id, content, cluster_id),
    )
    conn.execute(
        "INSERT INTO context_tags (id, memory_id, scene, task_type, entities, created_at) VALUES (?, ?, 'coding', 'default', '[]', '2026-01-01T00:00:00')",
        (memory_id + "_tag", memory_id),
    )
    if embedding:
        vec_bytes = struct.pack(f"{len(embedding)}f", *embedding)
        conn.execute("INSERT INTO memories_vec (id, embedding) VALUES (?, ?)", (memory_id, vec_bytes))


# ═══════════════════════════════════════
# 1. 创建新簇（无匹配簇时）
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_create_new_cluster():
    mgr = ClusterManager()
    emb = _make_embedding(1.0)

    with get_connection() as conn:
        _insert_memory(conn, "mem_001", "测试记忆", embedding=emb)

    cid = await mgr.assign_cluster("mem_001", emb)
    assert cid is not None

    with get_connection() as conn:
        row = conn.execute("SELECT member_count FROM knowledge_clusters WHERE id = ?", (cid,)).fetchone()
        assert row is not None
        assert row[0] == 1

        mem = conn.execute("SELECT cluster_id FROM memories WHERE id = ?", ("mem_001",)).fetchone()
        assert mem is not None
        assert mem[0] == cid


# ═══════════════════════════════════════
# 2. 加入已有簇（相似度>0.75）
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_join_existing_cluster():
    mgr = ClusterManager()
    emb1 = _make_embedding(1.0)

    with get_connection() as conn:
        _insert_memory(conn, "mem_001", "Java开发经验", embedding=emb1)

    cid1 = await mgr.assign_cluster("mem_001", emb1)

    # 相似的embedding（只有微小差异）
    emb2 = _make_embedding(1.0001)
    with get_connection() as conn:
        _insert_memory(conn, "mem_002", "Java项目经验", embedding=emb2)

    cid2 = await mgr.assign_cluster("mem_002", emb2)

    # 应该加入同一个簇
    assert cid1 == cid2

    with get_connection() as conn:
        row = conn.execute("SELECT member_count FROM knowledge_clusters WHERE id = ?", (cid1,)).fetchone()
        assert row[0] == 2


# ═══════════════════════════════════════
# 3. 不加入低相似度簇
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_no_join_low_similarity():
    mgr = ClusterManager()
    emb1 = _make_embedding(1.0)

    with get_connection() as conn:
        _insert_memory(conn, "mem_001", "Java开发", embedding=emb1)

    cid1 = await mgr.assign_cluster("mem_001", emb1)

    # 完全不同的embedding
    emb2 = _make_embedding(100.0)
    with get_connection() as conn:
        _insert_memory(conn, "mem_002", "完全不同的内容", embedding=emb2)

    cid2 = await mgr.assign_cluster("mem_002", emb2)

    # 不应该加入同一个簇
    assert cid1 != cid2


# ═══════════════════════════════════════
# 4. 簇中心更新正确（均值）
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_centroid_update():
    mgr = ClusterManager()
    emb1 = _make_embedding(1.0)

    with get_connection() as conn:
        _insert_memory(conn, "mem_001", "测试1", embedding=emb1)

    cid = await mgr.assign_cluster("mem_001", emb1)

    emb2 = _make_embedding(1.1)  # 足够相似加入同一簇
    with get_connection() as conn:
        _insert_memory(conn, "mem_002", "测试2", embedding=emb2)

    cid2 = await mgr.assign_cluster("mem_002", emb2)
    assert cid == cid2  # 确认加入同一簇

    with get_connection() as conn:
        row = conn.execute("SELECT centroid_embedding FROM knowledge_clusters WHERE id = ?", (cid,)).fetchone()
        centroid = list(struct.unpack(f"{len(row[0]) // 4}f", row[0]))
        # 第一个元素: (sin(1.0) + sin(1.1)) / 2
        import math
        expected = (math.sin(1.0) + math.sin(1.1)) / 2
        assert abs(centroid[0] - expected) < 0.01


# ═══════════════════════════════════════
# 5. 簇扩展召回同簇记忆
# ═══════════════════════════════════════
def test_expand_cluster_recalls():
    mgr = ClusterManager()

    with get_connection() as conn:
        # 创建簇
        centroid = struct.pack("1024f", *_make_embedding(1.0))
        conn.execute(
            "INSERT INTO knowledge_clusters (id, name, centroid_embedding, member_count) VALUES (?, ?, ?, 2)",
            ("cluster_001", "测试簇", centroid),
        )
        _insert_memory(conn, "mem_001", "记忆A", cluster_id="cluster_001")
        _insert_memory(conn, "mem_002", "记忆B", cluster_id="cluster_001")
        _insert_memory(conn, "mem_003", "无关记忆", cluster_id=None)

    # 给定mem_001，应该扩展出mem_002
    expanded = mgr.expand_cluster(["mem_001"], {"mem_001"})
    ids = {m["id"] for m in expanded}
    assert "mem_002" in ids
    assert "mem_001" not in ids
    assert "mem_003" not in ids


# ═══════════════════════════════════════
# 6. 簇扩展排除已有记忆
# ═══════════════════════════════════════
def test_expand_cluster_excludes():
    mgr = ClusterManager()

    with get_connection() as conn:
        centroid = struct.pack("1024f", *_make_embedding(1.0))
        conn.execute(
            "INSERT INTO knowledge_clusters (id, name, centroid_embedding, member_count) VALUES (?, ?, ?, 3)",
            ("cluster_001", "测试簇", centroid),
        )
        _insert_memory(conn, "mem_001", "记忆A", cluster_id="cluster_001")
        _insert_memory(conn, "mem_002", "记忆B", cluster_id="cluster_001")
        _insert_memory(conn, "mem_003", "记忆C", cluster_id="cluster_001")

    # 给定mem_001和mem_002，排除它们，应该只返回mem_003
    expanded = mgr.expand_cluster(["mem_001", "mem_002"], {"mem_001", "mem_002"})
    ids = {m["id"] for m in expanded}
    assert ids == {"mem_003"}


# ═══════════════════════════════════════
# 7. 合并相似簇
# ═══════════════════════════════════════
def test_merge_clusters():
    mgr = ClusterManager()
    emb = _make_embedding(1.0)

    with get_connection() as conn:
        centroid = struct.pack("1024f", *emb)
        conn.execute(
            "INSERT INTO knowledge_clusters (id, name, centroid_embedding, member_count) VALUES (?, ?, ?, 1)",
            ("cluster_001", "簇A", centroid),
        )
        conn.execute(
            "INSERT INTO knowledge_clusters (id, name, centroid_embedding, member_count) VALUES (?, ?, ?, 1)",
            ("cluster_002", "簇B", centroid),  # 相同质心，相似度=1.0
        )
        _insert_memory(conn, "mem_001", "A", cluster_id="cluster_001")
        _insert_memory(conn, "mem_002", "B", cluster_id="cluster_002")

    merged = mgr.merge_clusters(threshold=0.85)
    assert merged == 1

    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM knowledge_clusters").fetchone()[0]
        assert total == 1

        mem1 = conn.execute("SELECT cluster_id FROM memories WHERE id = ?", ("mem_001",)).fetchone()
        mem2 = conn.execute("SELECT cluster_id FROM memories WHERE id = ?", ("mem_002",)).fetchone()
        assert mem1[0] == mem2[0]


# ═══════════════════════════════════════
# 8. memory_cluster_stats工具返回正确
# ═══════════════════════════════════════
def test_cluster_stats():
    mgr = ClusterManager()

    with get_connection() as conn:
        centroid = struct.pack("1024f", *_make_embedding(1.0))
        conn.execute(
            "INSERT INTO knowledge_clusters (id, name, centroid_embedding, member_count) VALUES (?, ?, ?, 5)",
            ("c1", "大簇", centroid),
        )
        conn.execute(
            "INSERT INTO knowledge_clusters (id, name, centroid_embedding, member_count) VALUES (?, ?, ?, 2)",
            ("c2", "小簇", centroid),
        )

    stats = mgr.get_stats()
    assert stats["total_clusters"] == 2
    assert stats["avg_members"] == 3.5
    assert len(stats["largest_clusters"]) == 2
    assert stats["largest_clusters"][0]["member_count"] == 5


# ═══════════════════════════════════════
# 9. 无embedding时创建簇不崩溃
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_no_embedding_no_crash():
    mgr = ClusterManager()

    with get_connection() as conn:
        _insert_memory(conn, "mem_001", "无embedding记忆")

    result = await mgr.assign_cluster("mem_001", [])
    assert result is None

    result = await mgr.assign_cluster("mem_001", None)
    assert result is None


# ═══════════════════════════════════════
# 10. event_date字段迁移验证
# ═══════════════════════════════════════
def test_event_date_migration():
    with get_connection() as conn:
        # 验证cluster_id和event_date列存在
        cursor = conn.execute("PRAGMA table_info(memories)")
        cols = {row[1] for row in cursor.fetchall()}
        assert "cluster_id" in cols
        assert "event_date" in cols

        # 验证knowledge_clusters表存在
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_clusters'")
        assert cursor.fetchone() is not None

    # 写入带event_date的记忆验证
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO memories (id, content, importance, metadata, created_at, updated_at, event_date) VALUES (?, ?, 5.0, '{}', '2026-01-01', '2026-01-01', '2026-04-20')",
            ("mem_ev001", "有日期的记忆"),
        )
        row = conn.execute("SELECT event_date FROM memories WHERE id = ?", ("mem_ev001",)).fetchone()
        assert row[0] == "2026-04-20"
