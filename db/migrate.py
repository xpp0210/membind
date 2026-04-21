"""
MemOS → MemBind 数据迁移脚本

从 memos.db 读取有效 chunks，过滤噪音，生成 embedding，写入 MemBind。
"""

import sys
import os
import uuid
import struct
import asyncio
import time

# 添加项目根目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from dotenv import load_dotenv

# 加载 .env（从项目根目录）
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from config import settings
from db.connection import get_connection
from core.writer import ContextTagger


# ── 配置 ──
MEMOS_DB = "/home/xiepengpeng/.openclaw/memos-local/memos.db"
BATCH_SIZE = 15  # embedding 每批条数

# 噪音过滤
NOISE_PATTERNS = ["NO_REPLY", "HEARTBEAT_OK", "HEARTBEAT_FAILED"]


def read_valid_chunks():
    """从 MemOS 读取有效 chunks"""
    import sqlite3
    conn = sqlite3.connect(MEMOS_DB)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT id, content, summary, session_key, created_at, task_id, owner
        FROM chunks
        WHERE role = 'assistant'
          AND length(content) > 50
          AND content NOT LIKE 'NO_REPLY%'
          AND content NOT LIKE 'HEARTBEAT%'
        ORDER BY created_at
    """).fetchall()

    conn.close()

    valid = []
    skipped = 0
    for row in rows:
        # 额外过滤：纯工具输出噪音
        content = row["content"]
        if any(p in content for p in NOISE_PATTERNS):
            skipped += 1
            continue
        if len(content.strip()) < 50:
            skipped += 1
            continue
        valid.append(row)

    return valid, skipped


async def generate_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """批量生成 embedding（硅基流动 BGE-M3）"""
    api_key = settings.EMBEDDING_API_KEY
    if not api_key:
        print("[ERROR] EMBEDDING_API_KEY 未设置")
        return [[0.0] * settings.EMBEDDING_DIM for _ in texts]

    # 截断过长文本
    truncated = [t[:2000] for t in texts]

    async with httpx.AsyncClient(timeout=60.0) as client:
        for attempt in range(3):
            try:
                resp = await client.post(
                    settings.EMBEDDING_API_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": settings.EMBEDDING_API_MODEL, "input": truncated},
                )
                resp.raise_for_status()
                data = resp.json()
                items = sorted(data["data"], key=lambda x: x["index"])
                return [item["embedding"] for item in items]
            except Exception as e:
                print(f"[WARN] embedding 批次失败 (attempt {attempt+1}): {e}")
                if attempt < 2:
                    await asyncio.sleep(2)
                else:
                    print("[ERROR] embedding 批次最终失败，使用零向量")
                    return [[0.0] * settings.EMBEDDING_DIM for _ in texts]


def float_list_to_bytes(vec: list[float]) -> bytes:
    """float list → bytes (for sqlite-vec)"""
    return struct.pack(f"{len(vec)}f", *vec)


def write_memories(chunks_with_tags_and_embs):
    """写入 MemBind 数据库"""
    tagger = ContextTagger()

    with get_connection() as conn:
        for chunk, tag, emb in chunks_with_tags_and_embs:
            mid = str(uuid.uuid4())

            # 1. 写 memories
            conn.execute("""
                INSERT INTO memories (id, content, importance, source, source_session, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                mid,
                chunk["content"],
                tag.importance,
                "memos_migration",
                chunk["session_key"],
                f'{{"task_id": "{chunk["task_id"] or ""}", "original_id": "{chunk["id"]}"}}',
                chunk["created_at"],
            ))

            # 2. 写 context_tags
            tag_id = str(uuid.uuid4())
            entities_json = ",".join(tag.entities) if tag.entities else ""
            conn.execute("""
                INSERT INTO context_tags (id, memory_id, scene, task_type, entities, source_session)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (tag_id, mid, tag.scene, tag.task_type, entities_json, chunk["session_key"]))

            # 3. 写 memories_vec
            emb_bytes = float_list_to_bytes(emb)
            conn.execute("""
                INSERT INTO memories_vec (id, embedding) VALUES (?, ?)
            """, (mid, emb_bytes))


async def migrate():
    print("=" * 60)
    print("MemOS → MemBind 数据迁移")
    print("=" * 60)

    # Step 1: 读取有效 chunks
    print("\n[1/4] 读取 MemOS chunks...")
    chunks, skipped = read_valid_chunks()
    print(f"  有效 chunks: {len(chunks)}, 跳过: {skipped}")

    if not chunks:
        print("  没有数据需要迁移")
        return

    # Step 2: 提取标签（纯规则，不调LLM）
    print(f"\n[2/4] 提取上下文标签（规则引擎）...")
    tagger = ContextTagger()
    tagged = []
    for chunk in chunks:
        tag = tagger.tag_sync(chunk["content"])
        tagged.append((chunk, tag))

    # 统计场景分布
    scene_counts = {}
    for _, tag in tagged:
        scene_counts[tag.scene] = scene_counts.get(tag.scene, 0) + 1
    print(f"  场景分布: {scene_counts}")

    # Step 3: 批量生成 embedding
    print(f"\n[3/4] 生成 embedding（{len(chunks)} 条，每批 {BATCH_SIZE}）...")
    all_embeddings = []
    total_batches = (len(chunks) + BATCH_SIZE - 1) // BATCH_SIZE
    t0 = time.time()

    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        texts = [c["content"][:2000] for c in batch]
        embs = await generate_embeddings_batch(texts)
        all_embeddings.extend(embs)

        if batch_num % 10 == 0 or batch_num == total_batches:
            elapsed = time.time() - t0
            print(f"  批次 {batch_num}/{total_batches} ({len(all_embeddings)}/{len(chunks)}) - {elapsed:.1f}s")

    # Step 4: 写入 MemBind
    print(f"\n[4/4] 写入 MemBind...")
    chunks_with_data = [(tagged[j][0], tagged[j][1], all_embeddings[j]) for j in range(len(chunks))]
    write_memories(chunks_with_data)

    # 验证
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM memories WHERE source='memos_migration'").fetchone()[0]
        vec_total = conn.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0]

    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"迁移完成！")
    print(f"  总耗时: {elapsed:.1f}s")
    print(f"  迁移数: {total}")
    print(f"  跳过数: {skipped}")
    print(f"  向量数: {vec_total}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(migrate())
