-- MemBind 数据库初始化脚本
-- SQLite + sqlite-vec 向量检索

-- ============================================================
-- 1. memories 表：记忆主表
-- ============================================================
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,                   -- UUID v4
    content TEXT NOT NULL,                 -- 记忆文本
    importance REAL DEFAULT 5.0,           -- 重要性评分 0-10
    hit_count INTEGER DEFAULT 0,           -- 被检索命中次数
    binding_count INTEGER DEFAULT 0,       -- 被binding激活次数
    source TEXT DEFAULT 'unknown',         -- 来源：openclaw/manual/api
    source_session TEXT,                   -- 来源会话ID
    metadata TEXT,                         -- JSON: 任意附加元数据
    is_deleted INTEGER DEFAULT 0,          -- 软删除标记
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance);
CREATE INDEX IF NOT EXISTS idx_memories_deleted ON memories(is_deleted);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);

-- ============================================================
-- 2. context_tags 表：记忆上下文标签（1:1）
-- ============================================================
CREATE TABLE IF NOT EXISTS context_tags (
    id TEXT PRIMARY KEY,                   -- UUID v4
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    scene TEXT,                            -- 场景：coding/research/writing/ops/chat/learning/config
    task_type TEXT,                        -- 任务类型：debug/deploy/refactor/learn/analysis/writing/config/chat
    entities TEXT,                         -- JSON array: ["Java", "Spring Boot"]
    source_session TEXT,                   -- 来源会话
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(memory_id)                      -- 每条记忆只有一组标签
);

CREATE INDEX IF NOT EXISTS idx_tags_scene ON context_tags(scene);
CREATE INDEX IF NOT EXISTS idx_tags_task ON context_tags(task_type);

-- ============================================================
-- 3. binding_history 表：binding激活记录
-- ============================================================
CREATE TABLE IF NOT EXISTS binding_history (
    id TEXT PRIMARY KEY,                   -- UUID v4
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    query TEXT NOT NULL,                   -- 触发查询文本
    context_scene TEXT,                    -- 查询时的场景
    context_task TEXT,                     -- 查询时的任务类型
    binding_score REAL NOT NULL,           -- binding评分 0-1
    was_relevant INTEGER DEFAULT 1,        -- feedback: 1=相关, 0=不相关, NULL=未反馈
    activated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_bind_memory ON binding_history(memory_id);
CREATE INDEX IF NOT EXISTS idx_bind_time ON binding_history(activated_at);
CREATE INDEX IF NOT EXISTS idx_bind_relevant ON binding_history(was_relevant);

-- ============================================================
-- 4. conflict_log 表：冲突检测日志
-- ============================================================
CREATE TABLE IF NOT EXISTS conflict_log (
    id TEXT PRIMARY KEY,                   -- UUID v4
    memory_id_a TEXT NOT NULL REFERENCES memories(id),
    memory_id_b TEXT NOT NULL REFERENCES memories(id),
    similarity REAL NOT NULL,              -- 语义相似度 0-1
    reason TEXT DEFAULT '',                -- 冲突原因（LLM生成）
    resolution TEXT DEFAULT 'pending',     -- keep_both/merge/replace_a/replace_b/pending
    resolved_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    CHECK(memory_id_a < memory_id_b)       -- 避免重复记录 (a,b) 和 (b,a)
);

CREATE INDEX IF NOT EXISTS idx_conflict_time ON conflict_log(created_at);
CREATE INDEX IF NOT EXISTS idx_conflict_resolution ON conflict_log(resolution);

-- ============================================================
-- 5. chunks 表：对话片段（OpenClaw兼容层）
-- ============================================================
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,           -- UUID v4
    session_key TEXT NOT NULL,     -- 会话标识
    turn_id TEXT NOT NULL,         -- 对话轮次
    seq INTEGER NOT NULL,          -- 序号
    role TEXT NOT NULL,            -- user/assistant/system/tool
    content TEXT NOT NULL,         -- 原始内容
    summary TEXT,                  -- 摘要
    memory_id TEXT,                -- 关联的记忆ID（如果被提取为独立记忆）
    owner TEXT DEFAULT 'agent:main',
    is_deleted INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks(session_key);
CREATE INDEX IF NOT EXISTS idx_chunks_turn ON chunks(session_key, turn_id, seq);
CREATE INDEX IF NOT EXISTS idx_chunks_memory ON chunks(memory_id);

-- ============================================================
-- 6. memories_vec 虚拟表：向量索引（sqlite-vec）
-- ============================================================
-- ============================================================
-- 6. chunks 表：对话片段（OpenClaw plugin兼容层）
-- ============================================================
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,                   -- UUID v4
    session_key TEXT NOT NULL,             -- 会话标识
    turn_id TEXT NOT NULL,                 -- 对话轮次
    seq INTEGER NOT NULL,                  -- 序号
    role TEXT NOT NULL,                    -- user/assistant/system/tool
    content TEXT NOT NULL,                 -- 原始内容
    summary TEXT,                          -- 摘要
    memory_id TEXT,                        -- 关联的记忆ID（被提取为独立记忆时）
    owner TEXT DEFAULT 'agent:main',       -- 归属
    is_deleted INTEGER DEFAULT 0,          -- 软删除
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks(session_key);
CREATE INDEX IF NOT EXISTS idx_chunks_turn ON chunks(session_key, turn_id, seq);
CREATE INDEX IF NOT EXISTS idx_chunks_memory ON chunks(memory_id);

-- ============================================================
-- 7. memories_vec 虚拟表：向量索引（sqlite-vec）
-- ============================================================
CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
    id TEXT PRIMARY KEY,
    embedding float[1024]                  -- BAAI/bge-m3 输出维度
);

-- ============================================================
-- 8. knowledge_clusters 表：知识簇
-- ============================================================
CREATE TABLE IF NOT EXISTS knowledge_clusters (
    id TEXT PRIMARY KEY,
    name TEXT,                          -- 自动生成或手动命名
    centroid_embedding BLOB,            -- 簇中心向量（1024维，float32）
    member_count INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_clusters_created ON knowledge_clusters(created_at);

-- ============================================================
-- 9. memories_fts 虚拟表：FTS5全文检索
-- ============================================================
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    memory_id,
    content,
    scene,
    entities,
    tokenize='unicode61'
);

-- FTS同步触发器
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(memory_id, content, scene, entities)
    SELECT NEW.id, NEW.content,
           COALESCE((SELECT scene FROM context_tags WHERE memory_id = NEW.id), ''),
           COALESCE((SELECT entities FROM context_tags WHERE memory_id = NEW.id), '');
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    DELETE FROM memories_fts WHERE memory_id = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content, is_deleted ON memories BEGIN
    DELETE FROM memories_fts WHERE memory_id = OLD.id;
    INSERT INTO memories_fts(memory_id, content, scene, entities)
    SELECT NEW.id, NEW.content,
           COALESCE((SELECT scene FROM context_tags WHERE memory_id = NEW.id), ''),
           COALESCE((SELECT entities FROM context_tags WHERE memory_id = NEW.id), '')
    WHERE NEW.is_deleted = 0;
END;
