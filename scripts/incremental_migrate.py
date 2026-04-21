#!/usr/bin/env python3
"""增量迁移 MemOS → MemBind（去重+过滤噪音）"""
import sqlite3, hashlib, json, os, sys, requests
from datetime import datetime

MEMOS_DB = os.path.expanduser("~/.openclaw/memos-local/memos.db")
MEMBIND_DB = os.path.expanduser("~/projects/membind/data/membind.db")
EMBEDDING_API_URL = os.getenv("EMBEDDING_API_URL", "https://api.siliconflow.cn/v1/embeddings")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")

# 从.env加载
env_file = os.path.expanduser("~/projects/membind/.env")
if os.path.exists(env_file):
    for line in open(env_file):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if k == "EMBEDDING_API_KEY" and v:
                EMBEDDING_API_KEY = v

def get_embedding(text: str) -> list[float]:
    resp = requests.post(EMBEDDING_API_URL, headers={
        "Authorization": f"Bearer {EMBEDDING_API_KEY}",
        "Content-Type": "application/json"
    }, json={"model": "BAAI/bge-m3", "input": text, "encoding_format": "float"}, timeout=30)
    return resp.json()["data"][0]["embedding"]

def main():
    memos = sqlite3.connect(MEMOS_DB)
    bind = sqlite3.connect(MEMBIND_DB)
    
    # 已有记忆（用content hash去重）
    existing = set()
    for row in bind.execute("SELECT content FROM memories"):
        existing.add(hashlib.md5(row[0].encode()).hexdigest())
    print(f"MemBind已有: {len(existing)} 条")

    # MemOS有效chunks
    rows = memos.execute("""
        SELECT content, kind, created_at 
        FROM chunks 
        WHERE role='assistant' AND length(content) > 50
        ORDER BY created_at
    """).fetchall()

    # 过滤噪音+去重
    noise_patterns = ["NO_REPLY", "HEARTBEAT_OK", "扫描完成。发现~15个噪音chunk"]
    new_chunks = []
    seen_hashes = set()
    for content, kind, created_at in rows:
        # 噪音过滤
        if any(p in content for p in noise_patterns):
            continue
        if content.startswith("NO_REPLY") or content.startswith("HEARTBEAT"):
            continue
        # 去重
        h = hashlib.md5(content.encode()).hexdigest()
        if h in existing or h in seen_hashes:
            continue
        seen_hashes.add(h)
        new_chunks.append((content, kind, created_at))

    print(f"新增需迁移: {len(new_chunks)} 条")

    # 迁移
    success = 0
    for i, (content, kind, created_at) in enumerate(new_chunks):
        try:
            emb = get_embedding(content)
            import uuid
            mid = str(uuid.uuid4())[:16]
            
            # 场景推断
            scene = "general"
            if any(k in content for k in ["代码","bug","部署","重构","接口","函数"]):
                scene = "coding"
            elif any(k in content for k in ["论文","研究","基准","对比"]):
                scene = "research"
            elif any(k in content for k in ["文章","写","标题","发布","草稿"]):
                scene = "writing"
            elif any(k in content for k in ["服务器","运维","监控","cron","gateway"]):
                scene = "ops"

            bind.execute("INSERT INTO memories (id, content, importance, source, created_at) VALUES (?,?,?,?,?)",
                        (mid, content, 5.0, "memos-migration", created_at))
            bind.execute("INSERT INTO context_tags (id, memory_id, scene, created_at) VALUES (?,?,?,?)",
                        (str(uuid.uuid4())[:16], mid, scene, created_at))
            # 向量用sqlite-vec需要扩展，这里只存memories表
            success += 1
            if (i+1) % 10 == 0:
                print(f"  进度: {i+1}/{len(new_chunks)}")
                bind.commit()
        except Exception as e:
            print(f"  失败 [{i}]: {e}")

    bind.commit()
    total = bind.execute("SELECT count(*) FROM memories WHERE is_deleted=0").fetchone()[0]
    print(f"\n✅ 迁移完成: +{success} 条, MemBind总计 {total} 条")

if __name__ == "__main__":
    main()
