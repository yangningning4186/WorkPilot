from dataclasses import dataclass


@dataclass(frozen=True)
class BlockLocation:
    page_no: int
    page_width: float
    page_height: float
    rotation: int
    coord_origin: str
    bbox_norm: tuple[float, float, float, float]


@dataclass(frozen=True)
class ParsedBlock:
    block_idx: int
    block_type: str
    text: str
    char_start: int
    char_end: int
    heading_path: tuple[str, ...]
    locations: tuple[BlockLocation, ...] = ()


@dataclass(frozen=True)
class ParsedDocument:
    full_text: str
    blocks: list[ParsedBlock]
    page_count: int | None = None
