"""兼容旧导入；实现已移动到 ``app.rag.retrieval.query_decomposition``。"""

from app.rag.retrieval.query_decomposition import (
    QueryDecompositionError,
    QueryPlan,
    fallback_query_plan,
    parse_query_plan,
    plan_retrieval_queries,
)

__all__ = [
    "QueryDecompositionError",
    "QueryPlan",
    "fallback_query_plan",
    "parse_query_plan",
    "plan_retrieval_queries",
]
