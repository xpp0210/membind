"""
MemBind 检索API

POST /api/v1/memory/recall — 两阶段检索（recall → bind）
"""

from fastapi import APIRouter, Request

from core.recall_service import recall_service
from api.deps import get_namespace
from models.memory import RecallRequest

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])


@router.post("/recall")
async def recall_memory(req: RecallRequest, request: Request):
    namespace = get_namespace(request)
    """两阶段检索：向量召回 → binding评分取top_k"""

    if not req.query:
        return {"error": "query is required", "results": []}

    result = await recall_service.recall_and_bind(
        query=req.query,
        context=req.context,
        top_k=req.top_k,
        recall_n=req.recall_n,
        namespace=namespace,
        check_conflicts=True,
    )

    return {
        "query": result["query"],
        "results": result["results"],
        "total_recalled": result["total_recalled"],
        "top_k": result["top_k"],
        "conflict_warnings": result["conflict_warnings"],
    }
