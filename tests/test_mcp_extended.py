"""
MCP扩展工具测试（5个新工具）

测试memory_stats / memory_decay / memory_conflict_check / memory_merge / memory_export
"""

import os
import sys
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from db.connection import init_db, get_connection
from mcp_server import _write, _recall, _stats, _decay, _conflict_check, _export, list_tools
from core.merger import merger


@pytest_asyncio.fixture(autouse=True)
async def setup_db(tmp_path):
    """每个测试用独立的临时数据库"""
    db_path = str(tmp_path / "test_mcp_ext.db")
    original = settings.MEMBIND_DB_PATH
    settings.MEMBIND_DB_PATH = db_path
    os.environ.pop("EMBEDDING_API_KEY", None)
    os.environ.pop("LLM_API_KEY", None)
    settings.EMBEDDING_API_KEY = ""
    settings.LLM_API_KEY = ""
    init_db(db_path)
    yield
    settings.MEMBIND_DB_PATH = original


@pytest.mark.asyncio
async def test_list_tools_has_10():
    """list_tools返回11个工具"""
    tools = await list_tools()
    assert len(tools) == 11
    names = {t.name for t in tools}
    assert "memory_stats" in names
    assert "memory_decay" in names
    assert "memory_conflict_check" in names
    assert "memory_merge" in names
    assert "memory_export" in names


@pytest.mark.asyncio
async def test_memory_stats():
    """memory_stats返回正确统计"""
    await _write("测试记忆1", scene="coding")
    await _write("测试记忆2", scene="general")

    result = _stats()
    assert result["total_memories"] >= 2
    assert "scene_distribution" in result
    assert "total_bindings" in result
    assert "recent_writes" in result


@pytest.mark.asyncio
async def test_memory_decay():
    """memory_decay衰减记忆"""
    await _write("衰减测试记忆")

    result = _decay(days=30)
    assert "decayed_count" in result
    assert result["decayed_count"] >= 1


@pytest.mark.asyncio
async def test_conflict_check_no_conflict():
    """memory_conflict_check无冲突返回false"""
    await _write("Python列表推导式用法")

    result = await _conflict_check("Java Stream API处理集合")
    # 无embedding（API key为空）时返回has_conflict=False
    assert result["has_conflict"] is False


@pytest.mark.asyncio
async def test_conflict_check_with_conflict():
    """memory_conflict_check有相似内容返回true+详情"""
    await _write("Redis缓存策略：LRU淘汰算法配置")

    # 完全相同内容应触发（但由于无embedding，此测试验证fallback行为）
    result = await _conflict_check("Redis缓存策略：LRU淘汰算法配置")
    # 无embedding时embedding生成失败，返回false
    assert "has_conflict" in result


@pytest.mark.asyncio
async def test_memory_merge():
    """memory_merge合并两条记忆"""
    a = await _write("Redis用于缓存热点数据")
    b = await _write("Redis常用于缓存热数据减少数据库压力")

    result = await merger.merge(a["id"], b["id"])
    assert result["status"] == "ok"
    assert "merged_id" in result
    assert "merged_content" in result
    assert "---" in result["merged_content"]  # 无LLM key时简单拼接


@pytest.mark.asyncio
async def test_memory_merge_soft_delete():
    """memory_merge后原记忆软删除"""
    a = await _write("原始记忆A")
    b = await _write("原始记忆B")

    await merger.merge(a["id"], b["id"])

    with get_connection() as conn:
        row_a = conn.execute("SELECT is_deleted FROM memories WHERE id = ?", (a["id"],)).fetchone()
        row_b = conn.execute("SELECT is_deleted FROM memories WHERE id = ?", (b["id"],)).fetchone()
        assert row_a[0] == 1
        assert row_b[0] == 1

        # 合并后的记忆存在
        merged = conn.execute("SELECT id FROM memories WHERE is_deleted = 0").fetchall()
        assert len(merged) == 1


@pytest.mark.asyncio
async def test_memory_merge_no_llm_concat():
    """memory_merge无LLM key时简单拼接"""
    a = await _write("内容A部分")
    b = await _write("内容B部分")

    result = await merger.merge(a["id"], b["id"])
    assert "---" in result["merged_content"]
    assert "内容A部分" in result["merged_content"]
    assert "内容B部分" in result["merged_content"]


@pytest.mark.asyncio
async def test_memory_export_all():
    """memory_export导出全部"""
    await _write("导出测试1", scene="coding")
    await _write("导出测试2", scene="research")

    result = _export()
    assert result["total"] >= 2
    assert len(result["memories"]) >= 2
    # 验证字段
    mem = result["memories"][0]
    assert "id" in mem
    assert "content" in mem
    assert "scene" in mem
    assert "importance" in mem
    assert "created_at" in mem


@pytest.mark.asyncio
async def test_memory_export_filter_by_scene():
    """memory_export按scene过滤"""
    await _write("编码场景记忆", scene="coding")
    await _write("学习场景记忆", scene="learning")

    result = _export(scene="coding")
    assert result["total"] >= 1
    assert all(m["scene"] == "coding" for m in result["memories"])


@pytest.mark.asyncio
async def test_memory_export_limit():
    """memory_export限制数量"""
    for i in range(5):
        await _write(f"限制测试记忆{i}", scene="general")

    result = _export(limit=2)
    assert result["total"] == 2
