"""
MemBind 数据库连接管理

- SQLite WAL模式，支持并发读写
- init_db(): 读取init.sql建表
- get_connection(): 上下文管理器，自动commit/rollback
"""

import sqlite3
from pathlib import Path
from contextlib import contextmanager

import sqlite_vec

from config import settings


def _apply_pragma(conn: sqlite3.Connection) -> None:
    """应用SQLite优化配置"""
    conn.execute("PRAGMA journal_mode=WAL")       # WAL模式，并发友好
    conn.execute("PRAGMA synchronous=NORMAL")      # 性能平衡
    conn.execute("PRAGMA foreign_keys=ON")         # 启用外键约束
    conn.execute("PRAGMA cache_size=-64000")        # 64MB缓存
    # 注册sqlite-vec扩展
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


@contextmanager
def get_connection(db_path: str | None = None):
    """
    获取数据库连接的上下文管理器

    用法:
        with get_connection() as conn:
            conn.execute("SELECT ...")
    """
    path = db_path or settings.MEMBIND_DB_PATH
    # 确保目录存在
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row  # 返回字典式行
    _apply_pragma(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str | None = None) -> None:
    """
    初始化数据库：读取init.sql执行建表

    幂等操作，重复调用不会报错（使用了IF NOT EXISTS）。
    """
    init_sql_path = Path(__file__).parent / "init.sql"
    sql = init_sql_path.read_text(encoding="utf-8")

    with get_connection(db_path) as conn:
        conn.executescript(sql)

        # 迁移：原子化相关字段
        _migrate_columns(conn)

    print(f"[MemBind] 数据库初始化完成: {db_path or settings.MEMBIND_DB_PATH}")


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """检查并添加新列（幂等迁移）"""
    # 获取memories表现有列
    cursor = conn.execute("PRAGMA table_info(memories)")
    existing = {row[1] for row in cursor.fetchall()}

    migrations = [
        ("source_memory_id", "TEXT"),
        ("is_atomic", "INTEGER DEFAULT 1"),
        ("cluster_id", "TEXT REFERENCES knowledge_clusters(id)"),
        # Phase 2.2双层时间戳
        ("event_date", "TEXT"),
        # 多Agent命名空间隔离
        ("namespace", "TEXT DEFAULT 'default'"),
    ]

    for col_name, col_type in migrations:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {col_name} {col_type}")
            print(f"[MemBind] 迁移完成: 新增列 memories.{col_name}")

    # 为namespace创建索引（必须在列添加之后）
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories(namespace)")
    except Exception:
        pass

    # FTS5索引初始化（幂等）
    _init_fts(conn)


def _init_fts(conn: sqlite3.Connection) -> None:
    """初始化FTS5表（幂等），为已有数据建索引"""
    try:
        # 确保表存在（init.sql中已CREATE IF NOT EXISTS）
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if 'memories_fts' not in tables:
            conn.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    memory_id, content, scene, entities, tokenize='unicode61'
                );
            """)
        # 为已有数据建索引
        existing = conn.execute("SELECT count(*) FROM memories_fts").fetchone()[0]
        total = conn.execute("SELECT count(*) FROM memories WHERE is_deleted = 0").fetchone()[0]
        if existing < total:
            conn.execute("""
                INSERT OR IGNORE INTO memories_fts(memory_id, content, scene, entities)
                SELECT m.id, m.content, COALESCE(t.scene, ''), COALESCE(t.entities, '')
                FROM memories m LEFT JOIN context_tags t ON t.memory_id = m.id
                WHERE m.is_deleted = 0
            """)
            print(f"[MemBind] FTS5索引构建完成: {existing} -> {total}")
    except Exception as e:
        print(f"[MemBind] FTS5初始化跳过: {e}")
