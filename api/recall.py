"""
MemBind 检索API

POST /api/v1/memory/recall — 两阶段检索（recall → bind）
"""

from fastapi import APIRouter, Query, Request

from core.retriever import HybridRetriever, BindingScorer
from core.conflict import conflict_detector
from services.binding_service import record_binding
from db.connection import get_connection
from api.deps import get_namespace
from models.memory import RecallRequest

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])

retriever = HybridRetriever()
scorer = BindingScorer()


@router.post("/recall")
async def recall_memory(req: RecallRequest, request: Request):
    namespace = get_namespace(request)
    """两阶段检索：向量召回top-20 → binding评分取top_k"""

    if not req.query:
        return {"error": "query is required", "results": []}

    # 第一阶段：向量召回
    recalled = await retriever.recall(req.query, req.context, req.recall_n, namespace=namespace)

    if not recalled:
        return {"query": req.query, "results": [], "total_recalled": 0}

    # 第二阶段：binding评分
    scored = []
    for mem in recalled:
        binding = scorer.score(req.query, mem, req.context)
        scored.append({**mem, "binding": binding})

    # 按binding_score排序取top_k
    scored.sort(key=lambda x: x["binding"]["binding_score"], reverse=True)
    top_results = scored[:req.top_k]

    # 记录binding历史
    for r in top_results:
        record_binding(r["id"], req.query, r["binding"]["binding_score"], req.context)

    # 冲突检测（附加信息，不阻塞）
    conflict_warnings = []
    try:
        conflict_warnings = await conflict_detector.detect_recall_conflicts(top_results)
    except Exception:
        pass

    return {
        "query": req.query,
        "results": top_results,
        "total_recalled": len(recalled),
        "top_k": len(top_results),
        "conflict_warnings": conflict_warnings,
    }
