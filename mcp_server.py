"""
MemBind MCP Server（stdio模式）

通过MCP协议暴露11个工具：
  memory_write / memory_recall / memory_get / memory_timeline / memory_stats /
  memory_decay / memory_conflict_check / memory_merge / memory_export /
  memory_feedback / memory_cluster_stats
直接调用core和db模块，不走HTTP API。
"""

import os
import sys
import json

# 确保项目根目录在path中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from db.connection import init_db, get_connection
from core.conflict import conflict_detector
from core.lifecycle import lifecycle_manager
from core.cluster import cluster_manager
from core.merger import merger
from core.memory_writer import memory_writer
from core.recall_service import recall_service
from services.binding_service import update_feedback, get_stats

# 初始化数据库
init_db()

# 模块级实例

server = Server("membind")


# ── 工具 schema 定义 ──

_namespace_prop = {"type": "string", "description": "命名空间，默认default"}

TOOL_SCHEMAS = {
    "memory_write": {
        "description": "写入一条记忆到MemBind",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "记忆文本"},
                "scene": {"type": "string", "description": "场景：coding/research/writing/ops/chat/learning/general"},
                "entities": {"type": "array", "items": {"type": "string"}, "description": "实体列表"},
                "namespace": _namespace_prop,
            },
            "required": ["content"],
        },
    },
    "memory_recall": {
        "description": "从MemBind检索记忆",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "查询文本"},
                "top_k": {"type": "integer", "description": "返回数量，默认5"},
                "scene": {"type": "string", "description": "场景过滤"},
                "namespace": _namespace_prop,
            },
            "required": ["query"],
        },
    },
    "memory_get": {
        "description": "查询单条记忆完整信息（content + tags + stats）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "记忆ID"},
                "namespace": _namespace_prop,
            },
            "required": ["memory_id"],
        },
    },
    "memory_timeline": {
        "description": "返回该记忆前后的相关记忆（按created_at排序）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "记忆ID"},
                "window": {"type": "integer", "description": "前后各取多少条，默认2"},
            },
            "required": ["memory_id"],
        },
    },
    "memory_stats": {
        "description": "记忆库概览统计",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "memory_decay": {
        "description": "批量衰减N天未命中的记忆（默认30天）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "衰减天数阈值，默认30"},
                "namespace": _namespace_prop,
            },
        },
    },
    "memory_conflict_check": {
        "description": "主动冲突检查，检测给定内容与已有记忆的冲突",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "待检查的内容"},
            },
            "required": ["content"],
        },
    },
    "memory_merge": {
        "description": "合并两条记忆为一条，原记忆软删除",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id_a": {"type": "string", "description": "记忆A的ID"},
                "id_b": {"type": "string", "description": "记忆B的ID"},
            },
            "required": ["id_a", "id_b"],
        },
    },
    "memory_export": {
        "description": "导出记忆列表",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scene": {"type": "string", "description": "按场景过滤"},
                "limit": {"type": "integer", "description": "最大数量，默认100"},
                "namespace": _namespace_prop,
            },
        },
    },
    "memory_feedback": {
        "description": "反馈记忆相关性",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "记忆ID"},
                "query": {"type": "string", "description": "原始查询"},
                "relevant": {"type": "boolean", "description": "是否相关"},
                "namespace": _namespace_prop,
            },
            "required": ["memory_id", "query", "relevant"],
        },
    },
    "memory_cluster_stats": {
        "description": "知识簇统计信息",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
}


# ── Handler 实现 ──

async def handle_write(args: dict) -> dict:
    """写入记忆"""
    content = args["content"]
    scene = args.get("scene")
    entities = args.get("entities")
    namespace = args.get("namespace", "default")

    hint = {}
    if scene:
        hint["scene"] = scene
    if entities:
        hint["entities"] = entities

    result = await memory_writer.write(
        content=content,
        namespace=namespace,
        hint_context=hint if hint else None,
        conflict_mode="fast",
    )

    return {
        "id": result.memory_id,
        "scene": result.scene,
        "importance": result.importance,
        "conflict_warnings": result.conflict_warnings,
    }


async def handle_recall(args: dict) -> dict:
    """检索记忆（使用 recall_service）"""
    query = args["query"]
    top_k = args.get("top_k", 5)
    scene = args.get("scene")
    namespace = args.get("namespace", "default")

    context = {"scene": scene} if scene else None
    result = await recall_service.recall_and_bind(
        query, context, top_k=top_k, check_conflicts=False, namespace=namespace,
    )

    return {
        "results": [
            {
                "id": r["id"],
                "content": r["content"],
                "score": r["score"],
                "binding_score": r["binding"]["binding_score"],
                "tags": r.get("tags", {}),
            }
            for r in result["results"]
        ],
        "total": result["top_k"],
    }


def handle_get(args: dict) -> dict:
    """查询单条记忆完整信息"""
    memory_id = args["memory_id"]
    namespace = args.get("namespace", "default")

    with get_connection() as conn:
        row = conn.execute(
            """SELECT m.content, m.importance, m.created_at, m.updated_at,
                      t.scene, t.task_type, t.entities,
                      (SELECT COUNT(*) FROM binding_history br WHERE br.memory_id = m.id) as binding_count,
                      (SELECT COUNT(*) FROM binding_history br WHERE br.memory_id = m.id AND br.was_relevant = 1) as hit_count
               FROM memories m
               LEFT JOIN context_tags t ON t.memory_id = m.id
               WHERE m.id = ? AND m.namespace = ?""",
            (memory_id, namespace),
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


def handle_timeline(args: dict) -> dict:
    """返回该记忆前后的相关记忆"""
    memory_id = args["memory_id"]
    window = args.get("window", 2)

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


def handle_stats(_args: dict) -> dict:
    """记忆库概览统计"""
    stats = get_stats()
    with get_connection() as conn:
        recent = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE is_deleted = 0 AND created_at > datetime('now', '-7 days')"
        ).fetchone()[0]
    stats["recent_writes"] = recent
    return stats


def handle_decay(args: dict) -> dict:
    """批量衰减：先执行衰减，再清理低于阈值的"""
    days = args.get("days", 30)
    namespace = args.get("namespace", "default")
    decay_result = lifecycle_manager.decay_all(namespace=namespace)
    cleanup_result = lifecycle_manager.cleanup(namespace=namespace)
    return {
        "decayed_count": decay_result["affected_count"],
        "cleaned_count": cleanup_result["cleaned_count"],
    }


async def handle_conflict_check(args: dict) -> dict:
    """主动冲突检查：用 conflict_detector.detect_write_conflict_fast()"""
    content = args["content"]
    from core.writer import EmbeddingGenerator
    gen = EmbeddingGenerator()
    embedding = await gen.generate(content)
    if not embedding or all(v == 0.0 for v in embedding):
        return {"has_conflict": False, "message": "embedding生成失败"}
    return await conflict_detector.detect_write_conflict_fast(content, embedding)


async def handle_merge(args: dict) -> dict:
    """合并两条记忆"""
    return await merger.merge(id_a=args["id_a"], id_b=args["id_b"])


def handle_export(args: dict) -> dict:
    """导出记忆列表"""
    scene = args.get("scene")
    limit = args.get("limit", 100)
    namespace = args.get("namespace", "default")

    with get_connection() as conn:
        if scene:
            rows = conn.execute(
                """SELECT m.id, m.content, COALESCE(t.scene, 'general') as scene,
                          m.importance, m.created_at
                   FROM memories m LEFT JOIN context_tags t ON t.memory_id = m.id
                   WHERE m.is_deleted = 0 AND t.scene = ? AND m.namespace = ?
                   ORDER BY m.created_at DESC LIMIT ?""",
                (scene, namespace, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT m.id, m.content, COALESCE(t.scene, 'general') as scene,
                          m.importance, m.created_at
                   FROM memories m LEFT JOIN context_tags t ON t.memory_id = m.id
                   WHERE m.is_deleted = 0 AND m.namespace = ?
                   ORDER BY m.created_at DESC LIMIT ?""",
                (namespace, limit),
            ).fetchall()

        memories = [
            {"id": r["id"], "content": r["content"], "scene": r["scene"],
             "importance": round(r["importance"], 2), "created_at": r["created_at"]}
            for r in rows
        ]
    return {"total": len(memories), "memories": memories}


async def handle_feedback(args: dict) -> dict:
    """反馈记忆相关性"""
    memory_id = args["memory_id"]
    query = args["query"]
    relevant = args["relevant"]
    namespace = args.get("namespace", "default")
    update_feedback(memory_id, query, relevant)
    return {"status": "ok", "memory_id": memory_id, "relevant": relevant}


def handle_cluster_stats(_args: dict) -> dict:
    """知识簇统计"""
    return cluster_manager.get_stats()


# ── Handler 注册表 ──

HANDLERS = {
    "memory_write": handle_write,
    "memory_recall": handle_recall,
    "memory_get": handle_get,
    "memory_timeline": handle_timeline,
    "memory_stats": handle_stats,
    "memory_decay": handle_decay,
    "memory_conflict_check": handle_conflict_check,
    "memory_merge": handle_merge,
    "memory_export": handle_export,
    "memory_feedback": handle_feedback,
    "memory_cluster_stats": handle_cluster_stats,
}


# ── MCP Server 生命周期 ──

@server.list_tools()
async def list_tools():
    return [
        types.Tool(name=name, **schema)
        for name, schema in TOOL_SCHEMAS.items()
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    handler = HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"Unknown tool: {name}")

    result = await handler(arguments) if _is_coroutine(handler) else handler(arguments)
    return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


def _is_coroutine(func) -> bool:
    import asyncio
    return asyncio.iscoroutinefunction(func)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
