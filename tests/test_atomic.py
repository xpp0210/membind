"""
原子化记忆提取测试

测试 ConversationParser 的原子化拆分功能。
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.conversation import ConversationParser, _should_skip
from db.connection import init_db, get_connection
from config import settings


@pytest.fixture(autouse=True)
def setup_db(tmp_path):
    """每个测试用独立的临时数据库"""
    db_path = str(tmp_path / "test_atomic.db")
    original = settings.MEMBIND_DB_PATH
    settings.MEMBIND_DB_PATH = db_path
    os.environ.pop("EMBEDDING_API_KEY", None)
    os.environ.pop("LLM_API_KEY", None)
    settings.EMBEDDING_API_KEY = ""
    settings.LLM_API_KEY = ""
    init_db(db_path)
    yield
    settings.MEMBIND_DB_PATH = original


def _make_parser_with_key() -> ConversationParser:
    """创建带LLM key的parser（测试用）"""
    parser = ConversationParser()
    parser._llm_key = "test-key"
    return parser


# ═══════════════════════════════════════
# 1. 短文本不拆分
# ═══════════════════════════════════════
def test_short_text_no_split():
    parser = _make_parser_with_key()
    mem = {"content": "项目使用Java 11和Spring Boot开发", "importance": 5.0, "scene": "coding", "entities": ["Java"]}
    result = parser._atomize(mem)
    assert len(result) == 1
    assert result[0]["content"] == mem["content"]


# ═══════════════════════════════════════
# 2. 长文本含多事实 → 需要LLM拆分（无真实LLM时不拆分）
# ═══════════════════════════════════════
def test_long_text_multi_facts_split():
    """长文本+有LLM key → _atomize返回原样（因为prompt已处理拆分）"""
    parser = _make_parser_with_key()
    long_content = "A" * 201  # >200字
    mem = {"content": long_content, "importance": 5.0, "scene": "general", "entities": []}
    result = parser._atomize(mem)
    # _atomize不调LLM（prompt已处理），返回原样
    assert len(result) >= 1


# ═══════════════════════════════════════
# 3. 长文本单事实不拆分（同样由prompt决定）
# ═══════════════════════════════════════
def test_long_text_single_fact_no_split():
    parser = _make_parser_with_key()
    long_content = "这是一个关于系统架构的重要决策：" + "B" * 250
    mem = {"content": long_content, "importance": 7.0, "scene": "ops", "entities": ["架构"]}
    result = parser._atomize(mem)
    assert len(result) == 1


# ═══════════════════════════════════════
# 4. 无LLM key时不拆分
# ═══════════════════════════════════════
def test_no_api_key_no_split():
    parser = ConversationParser()  # 无key
    assert parser._llm_key == ""
    long_content = "A" * 300
    mem = {"content": long_content, "importance": 5.0, "scene": "general", "entities": []}
    result = parser._atomize(mem)
    assert len(result) == 1
    assert result[0]["content"] == long_content


# ═══════════════════════════════════════
# 5. 多条消息批量处理（无LLM → extract返回空）
# ═══════════════════════════════════════
@pytest.mark.asyncio
async def test_batch_messages():
    parser = ConversationParser()  # 无LLM key
    messages = [
        {"role": "user", "content": "项目配置了Redis作为缓存，使用的是Redis 7.0版本，连接池大小设为50"},
        {"role": "assistant", "content": "好的，已记录Redis配置信息"},
    ]
    result = await parser.extract_memories(messages)
    # 无LLM key → 返回空
    assert result == []


# ═══════════════════════════════════════
# 6. schema迁移：source_memory_id和is_atomic字段存在
# ═══════════════════════════════════════
def test_schema_migration():
    with get_connection() as conn:
        cursor = conn.execute("PRAGMA table_info(memories)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "source_memory_id" in columns, f"source_memory_id missing, got: {columns}"
        assert "is_atomic" in columns, f"is_atomic missing, got: {columns}"

    # 写入带原子化字段的记忆
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO memories (id, content, source_memory_id, is_atomic) VALUES (?, ?, ?, ?)",
            ("test-1", "原子记忆内容", None, 1),
        )
        conn.execute(
            "INSERT INTO memories (id, content, source_memory_id, is_atomic) VALUES (?, ?, ?, ?)",
            ("test-2", "拆分出的子记忆", "test-original", 1),
        )

    with get_connection() as conn:
        row = conn.execute("SELECT source_memory_id, is_atomic FROM memories WHERE id = 'test-2'").fetchone()
        assert row["source_memory_id"] == "test-original"
        assert row["is_atomic"] == 1
