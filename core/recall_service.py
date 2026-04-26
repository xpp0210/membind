"""
MemBind 统一检索服务

封装 recall → binding评分 → 记录绑定历史 的完整流程，
供 MCP Server 和 HTTP API 共享调用。
"""

import logging

from core.retriever import HybridRetriever, BindingScorer
from core.conflict import conflict_detector
from services.binding_service import record_binding

logger = logging.getLogger(__name__)


class RecallService:
    """统一检索+绑定服务"""

    def __init__(self):
        self.retriever = HybridRetriever()
        self.scorer = BindingScorer()

    async def recall_and_bind(
        self,
        query: str,
        context: dict | None = None,
        top_k: int = 5,
        recall_n: int = 20,
        namespace: str | None = None,
        check_conflicts: bool = False,
    ) -> dict:
        """
        统一 recall+binding+record 流程。

        Args:
            query: 查询文本
            context: 上下文 dict（如 {"scene": "coding"}）
            top_k: 最终返回数量
            recall_n: 向量召回数量
            namespace: 命名空间
            check_conflicts: 是否附加冲突检测

        Returns:
            {
                "query": str,
                "results": list[dict],        # 带 binding 信息的记忆列表
                "total_recalled": int,         # 向量召回总数
                "top_k": int,                  # 实际返回数量
                "conflict_warnings": list,     # 冲突警告（仅 check_conflicts=True）
            }
        """
        # 向量召回
        recalled = await self.retriever.recall(
            query, context, recall_n,
            namespace=namespace,
        )

        if not recalled:
            return {
                "query": query,
                "results": [],
                "total_recalled": 0,
                "top_k": 0,
                "conflict_warnings": [],
            }

        # Binding 评分
        scored = []
        for mem in recalled:
            binding = self.scorer.score(query, mem, context)
            scored.append({**mem, "binding": binding})

        # 按 binding_score 排序取 top_k
        scored.sort(key=lambda x: x["binding"]["binding_score"], reverse=True)
        top_results = scored[:top_k]

        # 记录 binding 历史
        for r in top_results:
            record_binding(r["id"], query, r["binding"]["binding_score"], context)

        # 可选冲突检测
        conflict_warnings = []
        if check_conflicts:
            try:
                conflict_warnings = await conflict_detector.detect_recall_conflicts(top_results)
            except Exception:
                logger.warning("recall conflict check failed", exc_info=True)

        return {
            "query": query,
            "results": top_results,
            "total_recalled": len(recalled),
            "top_k": len(top_results),
            "conflict_warnings": conflict_warnings,
        }


# 模块级单例
recall_service = RecallService()
