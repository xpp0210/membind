"""
MemBind 冲突管理测试套件

覆盖：写入冲突检测、冲突列表、手动解决、自动解决
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ═══════════════════════════════════════
# 1. 写入无冲突
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_write_no_conflict(client):
    """写入不相关记忆，conflict_check=clear"""
    resp = await client.post("/api/v1/memory/write", json={
        "content": "今天天气很好，去公园散步了"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["conflict_check"]["status"] == "clear"


# ═══════════════════════════════════════
# 2. 写入检测到冲突（mock LLM）
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_write_conflict_detected(client):
    """写入矛盾记忆，conflict_check=conflict"""
    from core.conflict import ConflictDetector, ConflictCheckResult, ConflictInfo

    # 写入第一条
    await client.post("/api/v1/memory/write", json={
        "content": "项目使用MySQL数据库"
    })

    # Mock detect_write_conflict 返回冲突
    conflict_result = ConflictCheckResult(
        check="conflict",
        conflicts=[ConflictInfo(
            memory_id_a="new",
            memory_id_b="existing",
            content_a="项目使用PostgreSQL数据库",
            content_b="项目使用MySQL数据库",
            similarity=0.92,
            contradiction=True,
            reason="数据库类型矛盾",
            resolution="keep_new",
        )],
    )

    # Patch on the singleton used by write.py, plus make embedding non-zero to trigger conflict check
    from core.conflict import conflict_detector
    from core.writer import EmbeddingGenerator
    import struct
    fake_emb = struct.pack("1024f", *[0.1] * 1024)
    with patch.object(conflict_detector, "detect_write_conflict", new_callable=AsyncMock, return_value=conflict_result), \
         patch.object(EmbeddingGenerator, "generate", new_callable=AsyncMock, return_value=[0.1] * 1024):
        resp = await client.post("/api/v1/memory/write", json={
            "content": "项目使用PostgreSQL数据库"
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["conflict_check"]["status"] == "conflict"
    assert len(data["conflict_check"]["conflicts"]) == 1


# ═══════════════════════════════════════
# 3. 写入相似但不矛盾
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_write_similar_not_contradiction(client):
    """写入相似但补充性信息，conflict_check=similar"""
    from core.conflict import ConflictDetector, ConflictCheckResult, ConflictInfo

    await client.post("/api/v1/memory/write", json={
        "content": "项目使用MySQL数据库"
    })

    conflict_result = ConflictCheckResult(
        check="similar",
        conflicts=[ConflictInfo(
            memory_id_a="new",
            memory_id_b="existing",
            content_a="MySQL数据库使用InnoDB引擎",
            content_b="项目使用MySQL数据库",
            similarity=0.88,
            contradiction=False,
            reason="补充信息",
            resolution="keep_both",
        )],
    )

    from core.conflict import conflict_detector
    from core.writer import EmbeddingGenerator
    with patch.object(conflict_detector, "detect_write_conflict", new_callable=AsyncMock, return_value=conflict_result), \
         patch.object(EmbeddingGenerator, "generate", new_callable=AsyncMock, return_value=[0.1] * 1024):
        resp = await client.post("/api/v1/memory/write", json={
            "content": "MySQL数据库使用InnoDB引擎"
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["conflict_check"]["status"] == "similar"


# ═══════════════════════════════════════
# 4. 检索冲突警告
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_recall_conflict_warnings(client):
    """检索包含矛盾结果时返回warnings"""
    from core.conflict import conflict_detector

    warnings = [
        {
            "memory_ids": ["mem_a", "mem_b"],
            "similarity": 0.95,
            "contents": ["MySQL", "PostgreSQL"],
        }
    ]

    await client.post("/api/v1/memory/write", json={"content": "test recall warning"})

    # Mock retriever to return results so detect_recall_conflicts gets called
    fake_recalled = [
        {"id": "mem_a", "content": "MySQL", "score": 0.9, "tags": {}},
        {"id": "mem_b", "content": "PostgreSQL", "score": 0.8, "tags": {}},
    ]
    with patch("api.recall.retriever.recall", new_callable=AsyncMock, return_value=fake_recalled), \
         patch("api.recall.scorer.score", return_value={"binding_score": 0.5, "reason": "test"}), \
         patch("api.recall.record_binding"), \
         patch.object(conflict_detector, "detect_recall_conflicts", new_callable=AsyncMock, return_value=warnings):
        resp = await client.post("/api/v1/memory/recall", json={"query": "数据库"})

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["conflict_warnings"]) == 1
    assert data["conflict_warnings"][0]["similarity"] == 0.95


# ═══════════════════════════════════════
# 5. 列出pending冲突
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_list_conflicts(client):
    """列出pending冲突"""
    # 直接插入conflict_log
    from db.connection import get_connection
    from core.conflict import ConflictDetector, ConflictCheckResult, ConflictInfo
    import uuid

    # 写入两条记忆
    r1 = await client.post("/api/v1/memory/write", json={"content": "使用Java开发"})
    r2 = await client.post("/api/v1/memory/write", json={"content": "使用Python开发"})
    id_a = r1.json()["id"]
    id_b = r2.json()["id"]

    # 插入一条pending冲突
    # conflict_log要求memory_id_a < memory_id_b
    lo, hi = min(id_a, id_b), max(id_a, id_b)
    conflict_id = uuid.uuid4().hex[:16]

    with get_connection() as conn:
        conn.execute(
            """INSERT INTO conflict_log (id, memory_id_a, memory_id_b, similarity, resolution, created_at)
               VALUES (?, ?, ?, ?, 'pending', datetime('now'))""",
            (conflict_id, lo, hi, 0.92),
        )
        conn.commit()

    resp = await client.get("/api/v1/conflicts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert any(c["id"] == conflict_id for c in data["conflicts"])


# ═══════════════════════════════════════
# 6. 解决冲突 — keep_new（软删除旧记忆）
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_resolve_conflict_keep_new(client):
    """解决冲突时保留新记忆，软删除旧记忆"""
    from db.connection import get_connection
    import uuid

    r1 = await client.post("/api/v1/memory/write", json={"content": "旧记忆内容"})
    r2 = await client.post("/api/v1/memory/write", json={"content": "新记忆内容"})
    id_a, id_b = min(r1.json()["id"], r2.json()["id"]), max(r1.json()["id"], r2.json()["id"])
    conflict_id = uuid.uuid4().hex[:16]

    with get_connection() as conn:
        conn.execute(
            """INSERT INTO conflict_log (id, memory_id_a, memory_id_b, similarity, resolution, created_at)
               VALUES (?, ?, ?, ?, 'pending', datetime('now'))""",
            (conflict_id, id_a, id_b, 0.90),
        )
        conn.commit()

    # keep_new: 软删除memory_id_a（较小的id）
    resp = await client.post("/api/v1/conflicts/resolve", json={
        "conflict_id": conflict_id,
        "resolution": "keep_new",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert id_a in data["soft_deleted"]

    # 验证软删除
    with get_connection() as conn:
        deleted = conn.execute(
            "SELECT is_deleted FROM memories WHERE id = ?", (id_a,)
        ).fetchone()[0]
        assert deleted == 1


# ═══════════════════════════════════════
# 7. 解决冲突 — keep_both（保留两条）
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_resolve_conflict_keep_both(client):
    """解决冲突保留两条"""
    from db.connection import get_connection
    import uuid

    r1 = await client.post("/api/v1/memory/write", json={"content": "记忆A"})
    r2 = await client.post("/api/v1/memory/write", json={"content": "记忆B"})
    id_a, id_b = min(r1.json()["id"], r2.json()["id"]), max(r1.json()["id"], r2.json()["id"])
    conflict_id = uuid.uuid4().hex[:16]

    with get_connection() as conn:
        conn.execute(
            """INSERT INTO conflict_log (id, memory_id_a, memory_id_b, similarity, resolution, created_at)
               VALUES (?, ?, ?, ?, 'pending', datetime('now'))""",
            (conflict_id, id_a, id_b, 0.85),
        )
        conn.commit()

    resp = await client.post("/api/v1/conflicts/resolve", json={
        "conflict_id": conflict_id,
        "resolution": "keep_both",
    })
    assert resp.status_code == 200
    assert resp.json()["soft_deleted"] == []

    # 两条都未被删除
    with get_connection() as conn:
        for mid in [id_a, id_b]:
            d = conn.execute("SELECT is_deleted FROM memories WHERE id = ?", (mid,)).fetchone()[0]
            assert d == 0


# ═══════════════════════════════════════
# 8. 自动解决（mock LLM）
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_auto_resolve(client):
    """自动解决pending冲突"""
    from db.connection import get_connection
    from core.conflict import ConflictDetector
    import uuid

    r1 = await client.post("/api/v1/memory/write", json={"content": "Java开发"})
    r2 = await client.post("/api/v1/memory/write", json={"content": "Python开发"})
    id_a, id_b = min(r1.json()["id"], r2.json()["id"]), max(r1.json()["id"], r2.json()["id"])
    conflict_id = uuid.uuid4().hex[:16]

    with get_connection() as conn:
        conn.execute(
            """INSERT INTO conflict_log (id, memory_id_a, memory_id_b, similarity, resolution, created_at)
               VALUES (?, ?, ?, ?, 'pending', datetime('now'))""",
            (conflict_id, id_a, id_b, 0.88),
        )
        conn.commit()

    # Mock LLM返回keep_new
    with patch.object(ConflictDetector, "_check_contradiction_llm", new_callable=AsyncMock,
                      return_value={"contradiction": True, "reason": "LLM决定保留新记忆", "resolution": "keep_new"}):
        resp = await client.post("/api/v1/conflicts/auto-resolve")

    assert resp.status_code == 200
    data = resp.json()
    assert data["resolved"] >= 1
    assert data["details"][0]["resolution"] == "keep_new"

    # 冲突列表应该为空
    resp2 = await client.get("/api/v1/conflicts")
    assert resp2.json()["total"] == 0
