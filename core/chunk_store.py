"""
MemBind Chunk 存储层

提供chunk的CRUD + timeline查询能力，兼容OpenClaw的memory_timeline/memory_get接口。
"""

import uuid
import sqlite3
from typing import Optional
from db.connection import get_connection


def save_chunk(
    session_key: str, turn_id: str, seq: int, role: str,
    content: str, summary: Optional[str] = None,
    memory_id: Optional[str] = None, owner: str = "agent:main"
) -> str:
    """写入一条chunk，返回chunk_id"""
    chunk_id = str(uuid.uuid4())
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO chunks (id, session_key, turn_id, seq, role, content, summary, memory_id, owner)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chunk_id, session_key, turn_id, seq, role, content, summary, memory_id, owner)
        )
    return chunk_id


def save_chunks_batch(messages: list[dict], session_key: str,
                      turn_id: str, owner: str = "agent:main") -> list[str]:
    """批量写入chunks（模拟onConversationTurn），返回chunk_id列表"""
    ids = []
    with get_connection() as conn:
        for seq, msg in enumerate(messages):
            chunk_id = str(uuid.uuid4())
            role = msg.get("role", "user")
            content = msg.get("content", "")
            summary = msg.get("summary")
            if not content or not content.strip():
                continue
            conn.execute(
                """INSERT INTO chunks (id, session_key, turn_id, seq, role, content, summary, owner)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (chunk_id, session_key, turn_id, seq, role, content, summary, owner)
            )
            ids.append(chunk_id)
    return ids


def get_chunk(chunk_id: str) -> Optional[dict]:
    """获取单条chunk"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM chunks WHERE id = ? AND is_deleted = 0", (chunk_id,)
        ).fetchone()
        return dict(row) if row else None


def get_by_ref(session_key: str, turn_id: str, seq: int) -> Optional[dict]:
    """按ChunkRef获取chunk"""
    with get_connection() as conn:
        row = conn.execute(
            """SELECT * FROM chunks WHERE session_key = ? AND turn_id = ? AND seq = ? AND is_deleted = 0""",
            (session_key, turn_id, seq)
        ).fetchone()
        return dict(row) if row else None


def get_neighbors(session_key: str, turn_id: str, seq: int,
                  window: int = 2) -> list[dict]:
    """获取上下文chunk（timeline用），返回前后window个chunk + 当前chunk"""
    with get_connection() as conn:
        # 获取当前 chunk 的 created_at 作为锚点
        anchor = conn.execute(
            "SELECT created_at, seq FROM chunks WHERE session_key = ? AND turn_id = ? AND seq = ? AND is_deleted = 0",
            (session_key, turn_id, seq)
        ).fetchone()
        if not anchor:
            return []

        anchor_time, anchor_seq = anchor[0], anchor[1]

        # 前 window 条（按时间倒序取再反转）
        before = conn.execute("""
            SELECT * FROM chunks
            WHERE session_key = ? AND is_deleted = 0
              AND (created_at, seq) < (?, ?)
            ORDER BY created_at DESC, seq DESC
            LIMIT ?
        """, (session_key, anchor_time, anchor_seq, window)).fetchall()

        # 当前
        current = conn.execute("""
            SELECT * FROM chunks WHERE session_key = ? AND turn_id = ? AND seq = ? AND is_deleted = 0
        """, (session_key, turn_id, seq)).fetchone()

        # 后 window 条
        after = conn.execute("""
            SELECT * FROM chunks
            WHERE session_key = ? AND is_deleted = 0
              AND (created_at, seq) > (?, ?)
            ORDER BY created_at ASC, seq ASC
            LIMIT ?
        """, (session_key, anchor_time, anchor_seq, window)).fetchall()

    result = []
    for row in reversed(before):
        result.append({**dict(row), "relation": "before"})
    if current:
        result.append({**dict(current), "relation": "current"})
    for row in after:
        result.append({**dict(row), "relation": "after"})
    return result


def get_by_memory_id(memory_id: str) -> list[dict]:
    """获取关联到某个memory的所有chunks"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM chunks WHERE memory_id = ? AND is_deleted = 0 ORDER BY created_at",
            (memory_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def link_chunk_to_memory(chunk_id: str, memory_id: str):
    """将chunk关联到memory（提取为独立记忆后调用）"""
    with get_connection() as conn:
        conn.execute(
            "UPDATE chunks SET memory_id = ?, updated_at = datetime('now') WHERE id = ?",
            (memory_id, chunk_id)
        )
