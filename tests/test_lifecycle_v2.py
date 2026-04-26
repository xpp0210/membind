"""
MemBind V2 生命周期管理测试

测试Ebbinghaus指数衰减模型 + 记忆巩固机制
"""

import pytest
from datetime import datetime, timezone, timedelta


# ═══════════════════════════════════════
# 1. V2衰减 dry_run
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_v2_decay_dry_run(client):
    """V2衰减dry_run不实际修改数据"""
    write_resp = await client.post("/api/v1/memory/write", json={
        "content": "V2衰减测试记忆"
    })
    assert write_resp.status_code == 200

    resp = await client.post("/api/v1/lifecycle/decay", json={"dry_run": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["dry_run"] is True
    assert data["model"] == "ebbinghaus_v2"
    assert data["affected_count"] >= 1


# ═══════════════════════════════════════
# 2. V2衰减实际执行
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_v2_decay_execute(client):
    """V2衰减实际降低importance"""
    write_resp = await client.post("/api/v1/memory/write", json={
        "content": "V2衰减执行测试"
    })
    memory_id = write_resp.json()["id"]

    resp = await client.post("/api/v1/lifecycle/decay", json={"dry_run": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "ebbinghaus_v2"

    detail = next((d for d in data["details"] if d["id"] == memory_id), None)
    assert detail is not None
    assert detail["new_importance"] <= detail["old_importance"]
    assert "consolidation_level" in detail


# ═══════════════════════════════════════
# 3. V2衰减 - 巩固等级影响衰减量
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_v2_consolidation_reduces_decay(client):
    """巩固等级高的记忆衰减量应该更小"""
    # 写入两条记忆
    resp1 = await client.post("/api/v1/memory/write", json={
        "content": "未巩固记忆"
    })
    resp2 = await client.post("/api/v1/memory/write", json={
        "content": "已巩固记忆"
    })
    id1 = resp1.json()["id"]
    id2 = resp2.json()["id"]

    # 直接操作测试数据库
    import sqlite3
    from config import settings
    conn = sqlite3.connect(settings.MEMBIND_DB_PATH)
    conn.execute("UPDATE memories SET consolidation_level = 0 WHERE id = ?", (id1,))
    conn.execute("UPDATE memories SET consolidation_level = 3 WHERE id = ?", (id2,))
    conn.commit()
    conn.close()

    # 执行衰减
    resp = await client.post("/api/v1/lifecycle/decay", json={"dry_run": False})
    data = resp.json()

    detail1 = next((d for d in data["details"] if d["id"] == id1), None)
    detail2 = next((d for d in data["details"] if d["id"] == id2), None)

    assert detail1 is not None
    assert detail2 is not None
    # 巩固等级3的记忆衰减量应该显著小于等级0的
    assert detail2["decay"] < detail1["decay"]


# ═══════════════════════════════════════
# 4. 记忆巩固 dry_run
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_consolidate_dry_run(client):
    """巩固dry_run不实际修改"""
    resp = await client.post("/api/v1/lifecycle/consolidate", json={"dry_run": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["dry_run"] is True
    assert "consolidated_count" in data


# ═══════════════════════════════════════
# 5. 记忆巩固 - 无binding不应巩固
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_consolidate_no_bindings(client):
    """没有binding历史的记忆不应被巩固"""
    await client.post("/api/v1/memory/write", json={
        "content": "新写入无binding记忆"
    })

    resp = await client.post("/api/v1/lifecycle/consolidate", json={"dry_run": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["consolidated_count"] == 0


# ═══════════════════════════════════════
# 6. 记忆巩固 - 有足够binding应升级
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_consolidate_with_bindings(client):
    """有足够binding且accuracy高的记忆应被巩固"""
    write_resp = await client.post("/api/v1/memory/write", json={
        "content": "频繁使用的记忆"
    })
    memory_id = write_resp.json()["id"]

    # 直接操作测试数据库插入binding_history
    import sqlite3
    from config import settings
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(settings.MEMBIND_DB_PATH)
    for i in range(6):
        relevant = 1 if i < 4 else 0  # 4/6 = 0.67 > 0.6
        conn.execute(
            """INSERT INTO binding_history (id, memory_id, query, binding_score, was_relevant, activated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (f"bind-test-{i}", memory_id, f"测试查询{i}", 0.8, relevant, now),
        )
    # Set updated_at to 25 hours ago (CONSOLIDATION_MIN_HOURS = 24)
    from datetime import timedelta
    past = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    conn.execute(
        "UPDATE memories SET binding_count = 6, hit_count = 6, updated_at = ? WHERE id = ?",
        (past, memory_id),
    )
    conn.commit()
    conn.close()

    resp = await client.post("/api/v1/lifecycle/consolidate", json={"dry_run": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["consolidated_count"] >= 1

    if data["consolidated_count"] > 0:
        detail = data["details"][0]
        assert detail["old_level"] == 0
        assert detail["new_level"] == 1


# ═══════════════════════════════════════
# 7. 记忆巩固 - accuracy太低不应升级
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_consolidate_low_accuracy(client):
    """binding accuracy低于阈值不应被巩固"""
    write_resp = await client.post("/api/v1/memory/write", json={
        "content": "低准确率记忆"
    })
    memory_id = write_resp.json()["id"]

    import sqlite3
    from config import settings
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(settings.MEMBIND_DB_PATH)
    for i in range(10):
        relevant = 1 if i < 3 else 0  # 3/10 = 0.3 < 0.6
        conn.execute(
            """INSERT INTO binding_history (id, memory_id, query, binding_score, was_relevant, activated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (f"bind-low-{i}", memory_id, f"查询{i}", 0.8, relevant, now),
        )
    from datetime import timedelta
    past = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    conn.execute(
        "UPDATE memories SET binding_count = 10, hit_count = 10, updated_at = ? WHERE id = ?",
        (past, memory_id),
    )
    conn.commit()
    conn.close()

    resp = await client.post("/api/v1/lifecycle/consolidate", json={"dry_run": False})
    data = resp.json()
    assert data["consolidated_count"] == 0


# ═══════════════════════════════════════
# 8. 恢复已删除记忆 - consolidation_level重置
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_restore_resets_consolidation(client, db_conn):
    """恢复记忆应重置consolidation_level为0"""
    write_resp = await client.post("/api/v1/memory/write", json={
        "content": "将被恢复的记忆"
    })
    memory_id = write_resp.json()["id"]

    # 设置高巩固等级
    db_conn.execute(
        "UPDATE memories SET importance = 0.5, consolidation_level = 3 WHERE id = ?",
        (memory_id,),
    )
    db_conn.commit()

    # 标记删除
    now = datetime.now(timezone.utc).isoformat()
    db_conn.execute(
        "UPDATE memories SET is_deleted = 1 WHERE id = ?", (memory_id,)
    )
    db_conn.commit()

    # 恢复
    resp = await client.post(f"/api/v1/lifecycle/restore/{memory_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["consolidation_level"] == 0


# ═══════════════════════════════════════
# 9. 候选预览包含consolidation_level
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_candidates_includes_consolidation(client):
    """候选预览应包含consolidation_level字段"""
    await client.post("/api/v1/memory/write", json={"content": "候选预览V2测试"})
    resp = await client.get("/api/v1/lifecycle/candidates")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    assert "consolidation_level" in data["items"][0]
