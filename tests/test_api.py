"""
MemBind API 测试套件

使用 httpx.AsyncClient + 临时数据库，零外部依赖。
Embedding/LLM API key为空时自动降级为零向量/规则标签。
"""

import pytest


# ═══════════════════════════════════════
# 1. 健康检查
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_health(client):
    """GET /health 返回 status ok"""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


# ═══════════════════════════════════════
# 2. 写入记忆
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_write_memory(client):
    """POST /api/v1/memory/write 写入一条，验证返回id和tags"""
    resp = await client.post("/api/v1/memory/write", json={
        "content": "今天调试了Redis连接超时问题，原因是连接池配置太小"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert len(data["id"]) == 16
    assert data["tags"]["scene"] == "ops"  # 包含Redis/config关键词
    assert isinstance(data["tags"]["entities"], list)


# ═══════════════════════════════════════
# 3. 检索
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_recall(client):
    """写入2条后recall，验证返回结果"""
    await client.post("/api/v1/memory/write", json={
        "content": "Docker容器内存限制设置为2GB，使用docker-compose配置"
    })
    await client.post("/api/v1/memory/write", json={
        "content": "Spring Boot项目打包用mvn clean package -DskipTests"
    })

    resp = await client.post("/api/v1/memory/recall", json={
        "query": "Docker内存配置"
    })
    assert resp.status_code == 200
    data = resp.json()
    # 零向量模式下所有记忆都会被召回（文本fallback）
    assert data["total_recalled"] > 0
    if data["results"]:
        assert data["results"][0]["binding"]["binding_score"] > 0


# ═══════════════════════════════════════
# 4. 反馈 boost
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_feedback_boost(client):
    """recall后feedback relevant=true，验证importance_delta为正"""
    write_resp = await client.post("/api/v1/memory/write", json={
        "content": "K8s部署Pod重启策略改为Always"
    })
    memory_id = write_resp.json()["id"]

    # 先recall产生binding记录
    await client.post("/api/v1/memory/recall", json={"query": "K8s部署"})

    # 反馈
    resp = await client.post("/api/v1/memory/feedback", json={
        "memory_id": memory_id,
        "query": "K8s部署",
        "relevant": True,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "boosted"
    # importance_delta应该是"+0.5"
    assert "+" in data["importance_delta"]


# ═══════════════════════════════════════
# 5. 反馈 punish
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_feedback_punish(client):
    """feedback relevant=false，验证importance_delta为负"""
    write_resp = await client.post("/api/v1/memory/write", json={
        "content": "测试无关内容的数据"
    })
    memory_id = write_resp.json()["id"]

    await client.post("/api/v1/memory/recall", json={"query": "测试"})

    resp = await client.post("/api/v1/memory/feedback", json={
        "memory_id": memory_id,
        "query": "测试",
        "relevant": False,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "decayed"
    assert "-" in data["importance_delta"]


# ═══════════════════════════════════════
# 6. 统计
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_stats(client):
    """写入后stats验证total_memories>0"""
    await client.post("/api/v1/memory/write", json={
        "content": "MySQL慢查询优化：给user表加索引"
    })
    resp = await client.get("/api/v1/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_memories"] > 0


# ═══════════════════════════════════════
# 7. 获取记忆详情
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_get_memory(client):
    """用id获取详情，验证content匹配"""
    content = "Git合并冲突解决：使用git mergetool配合vimdiff"
    write_resp = await client.post("/api/v1/memory/write", json={"content": content})
    memory_id = write_resp.json()["id"]

    resp = await client.get(f"/api/v1/memory/{memory_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["content"] == content
    assert data["id"] == memory_id


# ═══════════════════════════════════════
# 8. 同场景计数
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_write_duplicate_scene(client):
    """写入同场景两条，验证scene_distribution计数正确"""
    await client.post("/api/v1/memory/write", json={
        "content": "Nginx配置反向代理到8080端口"
    })
    await client.post("/api/v1/memory/write", json={
        "content": "Linux服务器磁盘空间清理，删除旧日志文件"
    })

    resp = await client.get("/api/v1/stats")
    data = resp.json()
    # 两条都含ops关键词，scene_distribution中ops应>=2
    scene_dist = data["scene_distribution"]
    assert scene_dist.get("ops", 0) >= 2
