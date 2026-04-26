"""
测试写入自动Hook - 冲突检测集成到_write()

8个测试用例验证：
1. 写入无冲突时返回空conflict_warnings
2. 写入高相似内容时检测到冲突（similarity > 0.85）
3. 写入低相似内容时不标记冲突
4. conflict_warnings包含正确的id和similarity
5. 无embedding时写入不崩溃（conflict_warnings为空）
6. 连续写入多条记忆都能正确检测
7. 冲突不阻止写入（记忆仍然被创建）
8. 空数据库写入无冲突
"""

import os
import sys
import struct
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 清除API密钥
os.environ.pop("EMBEDDING_API_KEY", None)
os.environ.pop("LLM_API_KEY", None)

from db.connection import get_connection
from mcp_server import handle_write


def _clean_all_tables():
    """清空所有表数据"""
    with get_connection() as conn:
        for table in ["memories_fts", "binding_records", "context_tags", "memories_vec", "memories", "knowledge_clusters", "conflict_log"]:
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        conn.commit()


def _insert_memory_with_embedding(content: str, embedding: list[float]) -> str:
    """直接插入一条带embedding的记忆（绕过_write，用于准备测试数据）"""
    import uuid
    from datetime import datetime, timezone
    memory_id = uuid.uuid4().hex[:16]
    tag_id = uuid.uuid4().hex[:16]
    now = datetime.now(timezone.utc).isoformat()

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO memories (id, content, importance, metadata, created_at, updated_at) VALUES (?, ?, 0.5, '{}', ?, ?)",
            (memory_id, content, now, now),
        )
        conn.execute(
            "INSERT INTO context_tags (id, memory_id, scene, task_type, entities, created_at) VALUES (?, ?, 'general', 'general', '[]', ?)",
            (tag_id, memory_id, now),
        )
        if embedding and any(v != 0.0 for v in embedding):
            vec_bytes = struct.pack(f"{len(embedding)}f", *embedding)
            conn.execute("INSERT INTO memories_vec (id, embedding) VALUES (?, ?)", (memory_id, vec_bytes))
        conn.commit()
    return memory_id


def _make_similar_embedding(base: list[float], noise: float = 0.01) -> list[float]:
    """在base embedding上加小噪声，生成高相似向量"""
    import random
    return [v + random.uniform(-noise, noise) for v in base]


def _make_different_embedding(dim: int = 1024) -> list[float]:
    """生成与base方向不同的向量"""
    import random
    return [random.uniform(-1, 1) for _ in range(dim)]


# 固定测试embedding（1024维）
BASE_EMBEDDING = [0.1] * 1024


@pytest.fixture(autouse=True)
def fresh_db():
    """每个测试清空所有表"""
    _clean_all_tables()
    yield


@pytest.mark.asyncio
async def test_write_no_conflict_empty_warnings():
    """1. 写入无冲突时返回空conflict_warnings"""
    result = await handle_write({"content": "这是一条全新的记忆内容"})
    assert "conflict_warnings" in result
    assert result["conflict_warnings"] == []


@pytest.mark.asyncio
async def test_write_high_similarity_detects_conflict():
    """2. 写入高相似内容时检测到冲突"""
    # 先插入一条记忆
    _insert_memory_with_embedding("Python是解释型编程语言", BASE_EMBEDDING)

    # Mock memory_writer's embedder返回相似向量
    from unittest.mock import patch, AsyncMock
    from core.memory_writer import memory_writer
    similar_emb = _make_similar_embedding(BASE_EMBEDDING, noise=0.001)

    with patch.object(memory_writer.embedder, "generate", new_callable=AsyncMock, return_value=similar_emb):
        result = await handle_write({"content": "Python是一种解释型的编程语言"})

    assert result["conflict_warnings"], "应该检测到冲突"
    assert any(w["similarity"] > 0.85 for w in result["conflict_warnings"])


@pytest.mark.asyncio
async def test_write_low_similarity_no_conflict():
    """3. 写入低相似内容时不标记冲突"""
    _insert_memory_with_embedding("完全不同的主题", BASE_EMBEDDING)

    from unittest.mock import patch, AsyncMock
    from core.memory_writer import memory_writer
    diff_emb = _make_different_embedding()

    with patch.object(memory_writer.embedder, "generate", new_callable=AsyncMock, return_value=diff_emb):
        result = await handle_write({"content": "一个全新的话题"})

    assert result["conflict_warnings"] == []


@pytest.mark.asyncio
async def test_conflict_warnings_has_id_and_similarity():
    """4. conflict_warnings包含正确的id和similarity"""
    existing_id = _insert_memory_with_embedding("测试内容ABC", BASE_EMBEDDING)

    from unittest.mock import patch, AsyncMock
    from core.memory_writer import memory_writer
    similar_emb = _make_similar_embedding(BASE_EMBEDDING, noise=0.001)

    with patch.object(memory_writer.embedder, "generate", new_callable=AsyncMock, return_value=similar_emb):
        result = await handle_write({"content": "测试内容ABC"})

    warnings = result["conflict_warnings"]
    assert len(warnings) > 0
    w = warnings[0]
    assert "id" in w
    assert "similarity" in w
    assert w["id"] == existing_id
    assert w["similarity"] > 0.85


@pytest.mark.asyncio
async def test_write_no_embedding_no_crash():
    """5. 无embedding时写入不崩溃"""
    from unittest.mock import patch, AsyncMock
    from core.memory_writer import memory_writer

    with patch.object(memory_writer.embedder, "generate", new_callable=AsyncMock, return_value=None):
        result = await handle_write({"content": "没有embedding的记忆"})

    assert "conflict_warnings" in result
    assert result["conflict_warnings"] == []
    assert "id" in result  # 记忆仍然被创建


@pytest.mark.asyncio
async def test_consecutive_writes_detect_conflicts():
    """6. 连续写入多条记忆都能正确检测"""
    from unittest.mock import patch, AsyncMock
    from core.memory_writer import memory_writer

    # 第一条
    with patch.object(memory_writer.embedder, "generate", new_callable=AsyncMock, return_value=BASE_EMBEDDING):
        r1 = await handle_write({"content": "第一条记忆"})
    assert r1["conflict_warnings"] == []

    # 第二条（相似）→ 应该和第一条冲突
    similar_emb = _make_similar_embedding(BASE_EMBEDDING, noise=0.001)
    with patch.object(memory_writer.embedder, "generate", new_callable=AsyncMock, return_value=similar_emb):
        r2 = await handle_write({"content": "和第一条很像的记忆"})
    assert r2["conflict_warnings"], "第二条应该和第一条冲突"

    # 第三条（不同方向）→ 不冲突
    diff_emb = _make_different_embedding()
    with patch.object(memory_writer.embedder, "generate", new_callable=AsyncMock, return_value=diff_emb):
        r3 = await handle_write({"content": "完全不同的第三条"})
    assert r3["conflict_warnings"] == []


@pytest.mark.asyncio
async def test_conflict_does_not_block_write():
    """7. 冲突不阻止写入（记忆仍然被创建）"""
    _insert_memory_with_embedding("原始记忆", BASE_EMBEDDING)

    from unittest.mock import patch, AsyncMock
    from core.memory_writer import memory_writer
    similar_emb = _make_similar_embedding(BASE_EMBEDDING, noise=0.001)

    with patch.object(memory_writer.embedder, "generate", new_callable=AsyncMock, return_value=similar_emb):
        result = await handle_write({"content": "与原始记忆冲突的内容"})

    # 有冲突警告但记忆仍被创建
    assert result["conflict_warnings"]
    assert "id" in result
    assert result["id"]  # ID非空

    # 验证数据库中确实存在
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM memories WHERE id = ?", (result["id"],)).fetchone()
        assert row is not None, "记忆应该被写入数据库"


@pytest.mark.asyncio
async def test_empty_database_write_no_conflict():
    """8. 空数据库写入无冲突"""
    from unittest.mock import patch, AsyncMock
    from core.memory_writer import memory_writer

    with patch.object(memory_writer.embedder, "generate", new_callable=AsyncMock, return_value=BASE_EMBEDDING):
        result = await handle_write({"content": "空数据库的第一条记忆"})

    assert result["conflict_warnings"] == []
    assert "id" in result
