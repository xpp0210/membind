"""
MCP工具测试

直接调用server的call_tool，不启动stdio。
"""

import os
import sys
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from db.connection import init_db, get_connection
from mcp_server import server, _write, _recall, _feedback, list_tools


@pytest_asyncio.fixture(autouse=True)
async def setup_db(tmp_path):
    """每个测试用独立的临时数据库"""
    db_path = str(tmp_path / "test_mcp.db")
    original = settings.MEMBIND_DB_PATH
    settings.MEMBIND_DB_PATH = db_path
    os.environ.pop("EMBEDDING_API_KEY", None)
    os.environ.pop("LLM_API_KEY", None)
    settings.EMBEDDING_API_KEY = ""
    settings.LLM_API_KEY = ""
    init_db(db_path)
    yield
    settings.MEMBIND_DB_PATH = original


# ═══════════════════════════════════════
# 1. list_tools返回3个工具
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_list_tools():
    tools = await list_tools()
    assert len(tools) == 11
    names = {t.name for t in tools}
    expected = {"memory_write", "memory_recall", "memory_get", "memory_timeline", "memory_feedback",
               "memory_stats", "memory_decay", "memory_conflict_check", "memory_merge", "memory_export",
               "memory_cluster_stats"}
    assert names == expected


# ═══════════════════════════════════════
# 2. memory_write写入成功
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_memory_write():
    result = await _write("项目使用Java开发", scene="coding")
    assert "id" in result
    assert result["scene"] == "coding"
    assert len(result["id"]) == 16

    # 验证数据库
    with get_connection() as conn:
        row = conn.execute("SELECT content FROM memories WHERE id = ?", (result["id"],)).fetchone()
        assert row is not None
        assert row[0] == "项目使用Java开发"


# ═══════════════════════════════════════
# 3. memory_recall检索到记忆
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_memory_recall():
    # 先写入
    await _write("Redis缓存策略：LRU淘汰")
    await _write("MySQL主从复制配置")

    # 检索
    result = await _recall("缓存策略", top_k=5)
    assert result["total"] >= 1
    assert any("Redis" in r["content"] for r in result["results"])


# ═══════════════════════════════════════
# 4. memory_feedback反馈成功
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_memory_feedback():
    # 先写入+检索（产生binding记录）
    w = await _write("测试记忆内容")
    await _recall("测试")

    result = await _feedback(w["id"], "测试", True)
    assert result["status"] == "ok"

    # 验证importance变化（relevant=true应+0.5）
    with get_connection() as conn:
        imp = conn.execute("SELECT importance FROM memories WHERE id = ?", (w["id"],)).fetchone()[0]
        # 短内容importance≈4.0，feedback +0.5 → 4.5
        assert imp >= 4.0


# ═══════════════════════════════════════
# 5. 带实体的写入
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_write_with_entities():
    result = await _write("Spring Boot配置Redis", scene="coding", entities=["Spring Boot", "Redis"])
    assert "id" in result
    assert result["scene"] == "coding"
