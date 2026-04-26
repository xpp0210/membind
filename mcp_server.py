"""
MemBind MCP Server（stdio模式）

通过MCP协议暴露5个工具：memory_write / memory_recall / memory_get / memory_timeline / memory_feedback
直接调用core和db模块，不走HTTP API。
"""

import os
import sys
import json
import uuid
import struct
from datetime import datetime

# 确保项目根目录在path中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from config import settings
from db.connection import init_db, get_connection
from core.writer import ContextTagger, EmbeddingGenerator
from core.retriever import HybridRetriever, BindingScorer
from core.conflict import conflict_detector
from core.lifecycle import lifecycle_manager
from core.cluster import cluster_manager
from core.merger import merger
from core.memory_writer import memory_writer
from services.binding_service import record_binding, update_feedback, get_stats

# 初始化数据库
init_db()

# 模块级实例
tagger = ContextTagger()
embedder = EmbeddingGenerator()
retriever = HybridRetriever()
scorer = BindingScorer()

server = Server("membind")


@server.list_tools()
async def list_tools():
    return [
        types.Tool(
            name="memory_write",
            description="写入一条记忆到MemBind",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "记忆文本"},
                    "scene": {"type": "string", "description": "场景：coding/research/writing/ops/chat/learning/general"},
                    "entities": {"type": "array", "items": {"type": "string"}, "description": "实体列表"},
                },
                "required": ["content"],
            },
        ),
        types.Tool(
            name="memory_recall",
            description="从MemBind检索记忆",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "查询文本"},
                    "top_k": {"type": "integer", "description": "返回数量，默认5"},
                    "scene": {"type": "string", "description": "场景过滤"},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="memory_get",
            description="查询单条记忆完整信息（content + tags + stats）",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "记忆ID"},
                },
                "required": ["memory_id"],
            },
        ),
        types.Tool(
            name="memory_timeline",
            description="返回该记忆前后的相关记忆（按created_at排序）",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "记忆ID"},
                    "window": {"type": "integer", "description": "前后各取多少条，默认2"},
                },
                "required": ["memory_id"],
            },
        ),
        types.Tool(
            name="memory_stats",
            description="记忆库概览统计",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="memory_decay",
            description="批量衰减N天未命中的记忆（默认30天）",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "衰减天数阈值，默认30"},
                },
            },
        ),
        types.Tool(
            name="memory_conflict_check",
            description="主动冲突检查，检测给定内容与已有记忆的冲突",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "待检查的内容"},
                },
                "required": ["content"],
            },
        ),
        types.Tool(
            name="memory_merge",
            description="合并两条记忆为一条，原记忆软删除",
            inputSchema={
                "type": "object",
                "properties": {
                    "id_a": {"type": "string", "description": "记忆A的ID"},
                    "id_b": {"type": "string", "description": "记忆B的ID"},
                },
                "required": ["id_a", "id_b"],
            },
        ),
        types.Tool(
            name="memory_export",
            description="导出记忆列表",
            inputSchema={
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "description": "按场景过滤"},
                    "limit": {"type": "integer", "description": "最大数量，默认100"},
                },
            },
        ),
        types.Tool(
            name="memory_feedback",
            description="反馈记忆相关性",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "记忆ID"},
                    "query": {"type": "string", "description": "原始查询"},
                    "relevant": {"type": "boolean", "description": "是否相关"},
                },
                "required": ["memory_id", "query", "relevant"],
            },
        ),
        types.Tool(
            name="memory_cluster_stats",
            description="知识簇统计信息",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


async def _write(content: str, scene: str | None = None, entities: list[str] | None = None) -> dict:
    """写入记忆（核心逻辑）"""
    hint = {}
    if scene:
        hint["scene"] = scene
    if entities:
        hint["entities"] = entities

    result = await memory_writer.write(
        content=content,
        hint_context=hint if hint else None,
        conflict_mode="fast",
    )

    return {
        "id": result.memory_id,
        "scene": result.scene,
        "importance": result.importance,
        "conflict_warnings": result.conflict_warnings,
    }


async def _recall(query: str, top_k: int = 5, scene: str | None = None) -> dict:
    """检索记忆（核心逻辑）"""
    context = {"scene": scene} if scene else None
    recalled = await retriever.recall(query, context, top_k * 4)

    if not recalled:
        return {"results": [], "total": 0}

    scored = []
    for mem in recalled:
        binding = scorer.score(query, mem, context)
        scored.append({**mem, "binding": binding})

    scored.sort(key=lambda x: x["binding"]["binding_score"], reverse=True)
    top_results = scored[:top_k]

    for r in top_results:
        record_binding(r["id"], query, r["binding"]["binding_score"], context)

    return {
        "results": [
            {
                "id": r["id"],
                "content": r["content"],
                "score": r["score"],
                "binding_score": r["binding"]["binding_score"],
                "tags": r.get("tags", {}),
            }
            for r in top_results
        ],
        "total": len(top_results),
    }


def _get(memory_id: str) -> dict:
    """查询单条记忆完整信息"""
    with get_connection() as conn:
        row = conn.execute(
            """SELECT m.content, m.importance, m.created_at, m.updated_at,
                      t.scene, t.task_type, t.entities,
                      (SELECT COUNT(*) FROM binding_history br WHERE br.memory_id = m.id) as binding_count,
                      (SELECT COUNT(*) FROM binding_history br WHERE br.memory_id = m.id AND br.was_relevant = 1) as hit_count
               FROM memories m
               LEFT JOIN context_tags t ON t.memory_id = m.id
               WHERE m.id = ?""",
            (memory_id,),
        ).fetchone()
        if not row:
            return {"error": "not found", "memory_id": memory_id}
        return {
            "id": memory_id,
            "content": row["content"],
            "scene": row["scene"],
            "task_type": row["task_type"],
            "entities": json.loads(row["entities"]) if row["entities"] else [],
            "importance": row["importance"],
            "hit_count": row["hit_count"],
            "binding_count": row["binding_count"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


def _timeline(memory_id: str, window: int = 2) -> dict:
    """返回该记忆前后的相关记忆"""
    with get_connection() as conn:
        row = conn.execute("SELECT created_at FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            return {"error": "not found", "memory_id": memory_id}
        pivot = row["created_at"]

        before = conn.execute(
            "SELECT id, content, created_at FROM memories WHERE created_at < ? ORDER BY created_at DESC LIMIT ?",
            (pivot, window),
        ).fetchall()
        after = conn.execute(
            "SELECT id, content, created_at FROM memories WHERE created_at > ? ORDER BY created_at ASC LIMIT ?",
            (pivot, window),
        ).fetchall()

        results = [{"id": r["id"], "content": r["content"], "created_at": r["created_at"]} for r in reversed(before)]
        results.append({"id": memory_id, "content": conn.execute("SELECT content FROM memories WHERE id = ?", (memory_id,)).fetchone()["content"], "created_at": pivot, "current": True})
        results.extend({"id": r["id"], "content": r["content"], "created_at": r["created_at"]} for r in after)

    return {"results": results, "total": len(results)}


def _stats() -> dict:
    """记忆库概览统计"""
    stats = get_stats()
    with get_connection() as conn:
        recent = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE is_deleted = 0 AND created_at > datetime('now', '-7 days')"
        ).fetchone()[0]
    stats["recent_writes"] = recent
    return stats


def _decay(days: int = 30) -> dict:
    """批量衰减：先执行衰减，再清理低于阈值的"""
    decay_result = lifecycle_manager.decay_all()
    cleanup_result = lifecycle_manager.cleanup()
    return {
        "decayed_count": decay_result["affected_count"],
        "cleaned_count": cleanup_result["cleaned_count"],
    }


async def _conflict_check(content: str) -> dict:
    """主动冲突检查：用向量相似度快速比对Top-10"""
    from core.writer import EmbeddingGenerator

    gen = EmbeddingGenerator()
    embedding = await gen.generate(content)
    if not embedding or all(v == 0.0 for v in embedding):
        return {"has_conflict": False, "message": "embedding生成失败"}

    with get_connection() as conn:
        rows = conn.execute(
            """SELECT m.id, m.content, v.embedding
               FROM memories m JOIN memories_vec v ON v.id = m.id
               WHERE m.is_deleted = 0"""
        ).fetchall()

    if not rows:
        return {"has_conflict": False}

    scored = []
    for row in rows:
        mem_id, mem_content, emb_bytes = row[0], row[1], row[2]
        if not emb_bytes:
            continue
        import struct
        existing_emb = list(struct.unpack(f"{len(embedding)}f", emb_bytes[: len(embedding) * 4]))
        sim = conflict_detector._cosine_similarity(embedding, existing_emb)
        scored.append({"id": mem_id, "content": mem_content, "similarity": round(sim, 4)})

    scored.sort(key=lambda x: x["similarity"], reverse=True)
    top = scored[:10]
    conflicting = [m for m in top if m["similarity"] > 0.85]

    return {
        "has_conflict": len(conflicting) > 0,
        "conflicting": conflicting if conflicting else None,
    }


def _export(scene: str | None = None, limit: int = 100) -> dict:
    """导出记忆列表"""
    with get_connection() as conn:
        if scene:
            rows = conn.execute(
                """SELECT m.id, m.content, COALESCE(t.scene, 'general') as scene,
                          m.importance, m.created_at
                   FROM memories m LEFT JOIN context_tags t ON t.memory_id = m.id
                   WHERE m.is_deleted = 0 AND t.scene = ?
                   ORDER BY m.created_at DESC LIMIT ?""",
                (scene, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT m.id, m.content, COALESCE(t.scene, 'general') as scene,
                          m.importance, m.created_at
                   FROM memories m LEFT JOIN context_tags t ON t.memory_id = m.id
                   WHERE m.is_deleted = 0
                   ORDER BY m.created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()

        memories = [
            {"id": r["id"], "content": r["content"], "scene": r["scene"],
             "importance": round(r["importance"], 2), "created_at": r["created_at"]}
            for r in rows
        ]
    return {"total": len(memories), "memories": memories}


async def _feedback(memory_id: str, query: str, relevant: bool) -> dict:
    """反馈记忆相关性"""
    update_feedback(memory_id, query, relevant)
    return {"status": "ok", "memory_id": memory_id, "relevant": relevant}


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "memory_write":
        result = await _write(
            content=arguments["content"],
            scene=arguments.get("scene"),
            entities=arguments.get("entities"),
        )
    elif name == "memory_recall":
        result = await _recall(
            query=arguments["query"],
            top_k=arguments.get("top_k", 5),
            scene=arguments.get("scene"),
        )
    elif name == "memory_get":
        result = _get(memory_id=arguments["memory_id"])
    elif name == "memory_timeline":
        result = _timeline(memory_id=arguments["memory_id"], window=arguments.get("window", 2))
    elif name == "memory_feedback":
        result = await _feedback(
            memory_id=arguments["memory_id"],
            query=arguments["query"],
            relevant=arguments["relevant"],
        )
    elif name == "memory_stats":
        result = _stats()
    elif name == "memory_decay":
        result = _decay(days=arguments.get("days", 30))
    elif name == "memory_conflict_check":
        result = await _conflict_check(content=arguments["content"])
    elif name == "memory_merge":
        result = await merger.merge(id_a=arguments["id_a"], id_b=arguments["id_b"])
    elif name == "memory_export":
        result = _export(scene=arguments.get("scene"), limit=arguments.get("limit", 100))
    elif name == "memory_cluster_stats":
        result = cluster_manager.get_stats()
    else:
        raise ValueError(f"Unknown tool: {name}")

    return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
