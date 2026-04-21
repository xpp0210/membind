"""
MemBind Chunk API Tests
"""

import pytest


@pytest.mark.asyncio
async def test_capture_chunks(client):
    """测试批量写入chunks"""
    resp = await client.post("/api/v1/chunks/capture", json={
        "session_key": "test-session",
        "turn_id": "turn-001",
        "owner": "agent:main",
        "messages": [
            {"role": "user", "content": "你好，帮我查一下Redis的配置"},
            {"role": "assistant", "content": "Redis配置如下：maxmemory=256mb，eviction=allkeys-lru"},
        ],
    })
    data = resp.json()
    assert resp.status_code == 200
    assert data["count"] == 2
    assert len(data["chunk_ids"]) == 2


@pytest.mark.asyncio
async def test_capture_empty_messages(client):
    """测试空消息返回400"""
    resp = await client.post("/api/v1/chunks/capture", json={
        "session_key": "test-session",
        "turn_id": "turn-002",
        "messages": [],
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_capture_filters_empty_content(client):
    """测试过滤空内容的chunk不会被写入"""
    resp = await client.post("/api/v1/chunks/capture", json={
        "session_key": "test-session",
        "turn_id": "turn-003",
        "messages": [
            {"role": "user", "content": "   "},
            {"role": "user", "content": "有效内容"},
            {"role": "assistant", "content": ""},
        ],
    })
    data = resp.json()
    assert resp.status_code == 200
    # Only non-empty content chunks should be saved
    assert data["count"] == 1


@pytest.mark.asyncio
async def test_timeline(client):
    """测试timeline展开"""
    # 写入两轮对话
    await client.post("/api/v1/chunks/capture", json={
        "session_key": "timeline-session",
        "turn_id": "turn-t1",
        "messages": [
            {"role": "user", "content": "第一条消息"},
            {"role": "assistant", "content": "第二条回复"},
        ],
    })
    await client.post("/api/v1/chunks/capture", json={
        "session_key": "timeline-session",
        "turn_id": "turn-t2",
        "messages": [
            {"role": "user", "content": "第三条消息"},
            {"role": "assistant", "content": "第四条回复"},
        ],
    })

    resp = await client.get("/api/v1/chunks/timeline", params={
        "session_key": "timeline-session",
        "turn_id": "turn-t1",
        "seq": 0,
        "window": 1,
    })
    data = resp.json()
    assert resp.status_code == 200
    assert "entries" in data
    assert "anchor_ref" in data
    assert data["anchor_ref"]["sessionKey"] == "timeline-session"
    assert data["anchor_ref"]["turnId"] == "turn-t1"


@pytest.mark.asyncio
async def test_get_chunk(client):
    """测试获取单条chunk"""
    resp = await client.post("/api/v1/chunks/capture", json={
        "session_key": "get-session",
        "turn_id": "turn-g1",
        "messages": [
            {"role": "user", "content": "测试获取完整内容"},
        ],
    })
    chunk_id = resp.json()["chunk_ids"][0]

    resp = await client.get(f"/api/v1/chunks/{chunk_id}")
    data = resp.json()
    assert resp.status_code == 200
    assert data["content"] == "测试获取完整内容"
    assert data["role"] == "user"
    assert data["session_key"] == "get-session"


@pytest.mark.asyncio
async def test_get_chunk_not_found(client):
    """测试获取不存在的chunk"""
    resp = await client.get("/api/v1/chunks/nonexistent-id")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_chunks_by_memory(client):
    """测试按memory_id查关联chunk"""
    # 写入chunks
    resp = await client.post("/api/v1/chunks/capture", json={
        "session_key": "mem-session",
        "turn_id": "turn-m1",
        "messages": [
            {"role": "user", "content": "关联测试内容1"},
            {"role": "assistant", "content": "关联测试内容2"},
        ],
    })
    chunk_ids = resp.json()["chunk_ids"]

    # 关联到memory
    from db.connection import get_connection
    with get_connection() as conn:
        conn.execute("UPDATE chunks SET memory_id = ? WHERE id = ?", ("mem-001", chunk_ids[0]))

    resp = await client.get("/api/v1/chunks/by-memory/mem-001")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == chunk_ids[0]


@pytest.mark.asyncio
async def test_chunks_dont_break_existing_memory(client):
    """确认chunks不影响现有memories功能"""
    resp = await client.post("/api/v1/memory/write", json={
        "content": "测试记忆不受chunks影响",
    })
    assert resp.status_code == 200
    memory_id = resp.json()["id"]

    resp = await client.post("/api/v1/memory/recall", json={
        "query": "测试记忆不受chunks影响",
    })
    assert resp.status_code == 200
    assert len(resp.json()["results"]) > 0
