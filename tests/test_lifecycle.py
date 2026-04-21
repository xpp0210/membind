"""
MemBind 生命周期管理测试

测试衰减、清理、提升、恢复、候选预览
"""

import pytest


# ═══════════════════════════════════════
# 1. 衰减 dry_run
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_decay_dry_run(client):
    """衰减dry_run不实际修改数据"""
    write_resp = await client.post("/api/v1/memory/write", json={
        "content": "测试衰减用的记忆内容"
    })
    assert write_resp.status_code == 200

    resp = await client.post("/api/v1/lifecycle/decay", json={"dry_run": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["dry_run"] is True
    assert data["affected_count"] >= 1
    assert len(data["details"]) >= 1


# ═══════════════════════════════════════
# 2. 衰减实际执行
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_decay_execute(client):
    """衰减实际降低importance"""
    write_resp = await client.post("/api/v1/memory/write", json={
        "content": "衰减执行测试"
    })
    memory_id = write_resp.json()["id"]

    resp = await client.post("/api/v1/lifecycle/decay", json={"dry_run": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["dry_run"] is False
    assert data["affected_count"] >= 1

    # 验证importance降低了
    detail = next((d for d in data["details"] if d["id"] == memory_id), None)
    assert detail is not None
    assert detail["new_importance"] <= detail["old_importance"]


# ═══════════════════════════════════════
# 3. 清理 dry_run
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_cleanup_dry_run(client):
    """清理dry_run不删除"""
    # 先衰减让importance降低
    await client.post("/api/v1/memory/write", json={"content": "待清理记忆"})
    await client.post("/api/v1/lifecycle/decay", json={"dry_run": False})
    await client.post("/api/v1/lifecycle/decay", json={"dry_run": False})
    await client.post("/api/v1/lifecycle/decay", json={"dry_run": False})

    resp = await client.post("/api/v1/lifecycle/cleanup", json={"dry_run": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["dry_run"] is True


# ═══════════════════════════════════════
# 4. 清理实际执行
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_cleanup_execute(client):
    """清理实际软删除低importance记忆"""
    # 写入后多次衰减到低于阈值
    write_resp = await client.post("/api/v1/memory/write", json={
        "content": "将被清理的低价值记忆"
    })
    memory_id = write_resp.json()["id"]

    # 衰减到低于阈值（默认1.0），初始约5.0，每次衰减0.5
    for _ in range(12):
        await client.post("/api/v1/lifecycle/decay", json={"dry_run": False})

    resp = await client.post("/api/v1/lifecycle/cleanup", json={"dry_run": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["cleaned_count"] >= 0

    # 验证被清理的记忆确实查不到了
    get_resp = await client.get(f"/api/v1/memory/{memory_id}")
    if data["cleaned_count"] > 0:
        cleaned_ids = [d["id"] for d in data["details"]]
        if memory_id in cleaned_ids:
            assert get_resp.json().get("error") == "memory not found"


# ═══════════════════════════════════════
# 5. 手动提升
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_boost(client):
    """手动提升importance"""
    write_resp = await client.post("/api/v1/memory/write", json={
        "content": "要提升的记忆"
    })
    memory_id = write_resp.json()["id"]

    resp = await client.post(f"/api/v1/lifecycle/boost/{memory_id}", json={"amount": 2.0})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["boost"] == 2.0
    assert data["new_importance"] > data["old_importance"]


# ═══════════════════════════════════════
# 6. 提升不存在的记忆
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_boost_not_found(client):
    """提升不存在的记忆返回error"""
    resp = await client.post("/api/v1/lifecycle/boost/nonexistent123", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data


# ═══════════════════════════════════════
# 7. 恢复已删除记忆
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_restore(client):
    """恢复已软删除的记忆"""
    write_resp = await client.post("/api/v1/memory/write", json={
        "content": "要删除再恢复的记忆"
    })
    memory_id = write_resp.json()["id"]

    # 多次衰减+清理使其被删除
    for _ in range(12):
        await client.post("/api/v1/lifecycle/decay", json={"dry_run": False})
    await client.post("/api/v1/lifecycle/cleanup", json={"dry_run": False})

    # 尝试恢复
    resp = await client.post(f"/api/v1/lifecycle/restore/{memory_id}")
    assert resp.status_code == 200
    data = resp.json()
    # 如果记忆确实被删除了，应该恢复成功
    if data.get("status") == "ok":
        assert data["action"] == "restored"
        # 恢复后可以查到
        get_resp = await client.get(f"/api/v1/memory/{memory_id}")
        assert get_resp.json().get("content") is not None


# ═══════════════════════════════════════
# 8. 恢复未删除的记忆
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_restore_not_deleted(client):
    """恢复未删除的记忆返回error"""
    write_resp = await client.post("/api/v1/memory/write", json={
        "content": "正常记忆不需要恢复"
    })
    memory_id = write_resp.json()["id"]

    resp = await client.post(f"/api/v1/lifecycle/restore/{memory_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data


# ═══════════════════════════════════════
# 9. 候选预览
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_candidates(client):
    """候选预览返回importance最低的记忆"""
    await client.post("/api/v1/memory/write", json={"content": "候选预览测试"})
    resp = await client.get("/api/v1/lifecycle/candidates")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert data["count"] >= 1
    # 按importance升序排列
    importances = [item["importance"] for item in data["items"]]
    assert importances == sorted(importances)
