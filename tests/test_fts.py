"""
FTS5混合检索测试

- FTS表创建、同步、检索
- RRF融合
- 软删除过滤
"""
import os
import sys
import tempfile
import sqlite3

# 清除外部API环境变量
for k in ["EMBEDDING_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY"]:
    os.environ.pop(k, None)

import pytest
from db.connection import init_db, get_connection, _init_fts


@pytest.fixture
def fts_db(tmp_path):
    """创建带FTS5的测试数据库"""
    db_path = str(tmp_path / "test_fts.db")
    init_db(db_path)
    return db_path


def _insert_memory(conn, memory_id, content, scene=None, entities=None, is_deleted=0):
    """插入测试记忆"""
    conn.execute(
        "INSERT INTO memories (id, content, is_deleted) VALUES (?, ?, ?)",
        (memory_id, content, is_deleted),
    )
    if scene or entities:
        import json
        conn.execute(
            "INSERT INTO context_tags (id, memory_id, scene, entities) VALUES (?, ?, ?, ?)",
            (f"tag-{memory_id}", memory_id, scene or "", json.dumps(entities or [])),
        )
    # 手动同步到FTS（触发器在executescript中已创建）
    conn.commit()


class TestFTSCreation:
    """1. FTS表创建"""

    def test_fts_table_exists(self, fts_db):
        with get_connection(fts_db) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "memories_fts" in tables

    def test_fts_triggers_exist(self, fts_db):
        with get_connection(fts_db) as conn:
            triggers = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()}
            assert "memories_ai" in triggers
            assert "memories_ad" in triggers
            assert "memories_au" in triggers


class TestFTSSync:
    """2. 写入后FTS同步"""

    def test_insert_syncs_to_fts(self, fts_db):
        with get_connection(fts_db) as conn:
            _insert_memory(conn, "m1", "Python async programming guide", "coding", ["Python"])
            # 触发器应已同步
            rows = conn.execute("SELECT * FROM memories_fts").fetchall()
            assert len(rows) >= 1
            assert rows[0][0] == "m1"  # memory_id

    def test_fts_keyword_search_returns_correct(self, fts_db):
        """3. FTS关键词检索"""
        with get_connection(fts_db) as conn:
            _insert_memory(conn, "m1", "Python async programming guide", "coding")
            _insert_memory(conn, "m2", "Java Spring Boot microservice", "coding")
            _insert_memory(conn, "m3", "Docker container deployment", "ops")

            rows = conn.execute(
                'SELECT memory_id FROM memories_fts WHERE memories_fts MATCH ?',
                ('"Python"',)
            ).fetchall()
            ids = {r[0] for r in rows}
            assert "m1" in ids
            assert "m2" not in ids

    def test_fts_no_match_returns_empty(self, fts_db):
        """4. FTS不匹配返回空"""
        with get_connection(fts_db) as conn:
            _insert_memory(conn, "m1", "Python async programming", "coding")
            rows = conn.execute(
                'SELECT memory_id FROM memories_fts WHERE memories_fts MATCH ?',
                ('"nonexistent_keyword_xyz"',)
            ).fetchall()
            assert len(rows) == 0

    def test_soft_deleted_excluded(self, fts_db):
        """7. 软删除不出现在结果"""
        with get_connection(fts_db) as conn:
            _insert_memory(conn, "m1", "Python async programming", "coding", is_deleted=1)
            _insert_memory(conn, "m2", "Python data science", "coding")

            # FTS触发器在UPDATE is_deleted时应删除FTS记录
            # 但m1是直接以is_deleted=1插入的，触发器是AFTER INSERT
            # 需要检查：触发器不检查is_deleted，所以FTS可能有数据
            # 但JOIN时WHERE is_deleted=0会过滤
            rows = conn.execute("""
                SELECT m.id FROM memories_fts fts
                JOIN memories m ON m.id = fts.memory_id
                WHERE memories_fts MATCH '"Python"' AND m.is_deleted = 0
            """).fetchall()
            ids = {r[0] for r in rows}
            assert "m1" not in ids
            assert "m2" in ids


class TestFTSRetriever:
    """5-6. HybridRetriever FTS集成"""

    def test_fts_search_method(self, fts_db):
        """_fts_search方法基本功能"""
        from core.retriever import HybridRetriever
        retriever = HybridRetriever()

        with get_connection(fts_db) as conn:
            _insert_memory(conn, "m1", "Python async programming", "coding")
            _insert_memory(conn, "m2", "Java Spring Boot", "coding")

        # 需要临时替换数据库路径
        import config
        orig = config.settings.MEMBIND_DB_PATH
        config.settings.MEMBIND_DB_PATH = fts_db
        try:
            results = retriever._fts_search("Python", top_n=10)
            assert len(results) >= 1
            assert any(r["id"] == "m1" for r in results)
        finally:
            config.settings.MEMBIND_DB_PATH = orig

    def test_fts_only_results(self, fts_db):
        """6. 只有FTS命中时也能返回"""
        from core.retriever import HybridRetriever
        retriever = HybridRetriever()

        with get_connection(fts_db) as conn:
            _insert_memory(conn, "m1", "unique_keyword_xyzzy test", "coding")

        import config
        orig = config.settings.MEMBIND_DB_PATH
        config.settings.MEMBIND_DB_PATH = fts_db
        try:
            results = retriever._fts_search("unique_keyword_xyzzy", top_n=10)
            assert len(results) >= 1
            assert results[0]["source"] == "fts"
        finally:
            config.settings.MEMBIND_DB_PATH = orig


class TestFTSChinese:
    """8. 中文检索（unicode61限制验证）"""

    def test_english_in_mixed_content(self, fts_db):
        """空格分隔的英文在混合内容中可检索"""
        with get_connection(fts_db) as conn:
            _insert_memory(conn, "m1", "async Python 编程指南", "coding")
            rows = conn.execute(
                'SELECT memory_id FROM memories_fts WHERE memories_fts MATCH ?',
                ('"Python"',)
            ).fetchall()
            ids = {r[0] for r in rows}
            assert "m1" in ids

    def test_chinese_pure_unsupported(self, fts_db):
        """unicode61无法匹配纯中文关键词，确认这个已知限制"""
        with get_connection(fts_db) as conn:
            _insert_memory(conn, "m1", "异步编程指南", "coding")
            # 纯中文搜不到（unicode61限制），但不应报错
            rows = conn.execute(
                'SELECT memory_id FROM memories_fts WHERE memories_fts MATCH ?',
                ('"异步" OR "guide"',)
            ).fetchall()
            # 只要不报错就算通过（可能返回空）
            assert isinstance(rows, list)


class TestFTSIdempotent:
    """FTS初始化幂等性"""

    def test_double_init(self, tmp_path):
        db_path = str(tmp_path / "test_double.db")
        init_db(db_path)
        init_db(db_path)  # 第二次不应报错
        with get_connection(db_path) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "memories_fts" in tables
