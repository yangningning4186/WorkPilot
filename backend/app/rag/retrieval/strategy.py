
from app.knowledge_contracts import ChunkStrategy as ChunkStrategy

CHUNK_STRATEGIES: tuple[ChunkStrategy, ...] = (
    "fixed",
    "heading",
    "recursive",
    "semantic",
)


def validate_chunk_strategy(value: str) -> ChunkStrategy:
    if value not in CHUNK_STRATEGIES:
        raise ValueError(f"未知的 chunk strategy: {value}, 可选 {CHUNK_STRATEGIES}")
    return value
