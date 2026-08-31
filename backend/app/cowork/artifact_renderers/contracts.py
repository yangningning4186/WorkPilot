"""格式专属 ArtifactSpec；高级排版只暴露受约束图示与安全画布。"""

from __future__ import annotations

from itertools import combinations, pairwise
from typing import Annotated, Literal, Self
from unicodedata import east_asian_width

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Scalar = str | int | float | bool | None
SlideBullet = Annotated[str, Field(min_length=1, max_length=180)]
EvidencePolicy = Literal["none", "optional", "required"]
ArtifactType = Literal["docx", "xlsx", "pptx", "pdf", "html"]


def _display_units(value: str) -> int:
    return sum(2 if east_asian_width(character) in {"W", "F"} else 1 for character in value)


def _segment_intersects_rectangle(
    start: tuple[float, float],
    end: tuple[float, float],
    rectangle: tuple[float, float, float, float],
) -> bool:
    """Liang–Barsky segment/rectangle intersection in canvas percentage space."""

    left, top, right, bottom = rectangle
    delta_x = end[0] - start[0]
    delta_y = end[1] - start[1]
    lower, upper = 0.0, 1.0
    for direction, distance in (
        (-delta_x, start[0] - left),
        (delta_x, right - start[0]),
        (-delta_y, start[1] - top),
        (delta_y, bottom - start[1]),
    ):
        if direction == 0:
            if distance < 0:
                return False
            continue
        ratio = distance / direction
        if direction < 0:
            lower = max(lower, ratio)
        else:
            upper = min(upper, ratio)
        if lower > upper:
            return False
    return True


class _StrictSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimRecord(_StrictSpec):
    claim_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    text: str = Field(min_length=1, max_length=4_000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)
    target_type: Literal["pptx_slide", "docx_paragraph", "html_section", "pdf_section"]
    target_id: str = Field(min_length=1, max_length=120)


class ArtifactEnvelope(_StrictSpec):
    schema_version: Literal[1] = 1
    artifact_type: ArtifactType
    title: str = Field(min_length=1, max_length=300)
    purpose: str | None = Field(default=None, max_length=1_000)
    audience: str | None = Field(default=None, max_length=500)
    evidence_policy: EvidencePolicy = "optional"
    claims: list[ClaimRecord] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def _unique_claim_ids(self) -> Self:
        claim_ids = [claim.claim_id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim_id 必须唯一")
        return self


class StorySlide(_StrictSpec):
    id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    role: Literal["hero", "supporting", "transition"]
    rhythm: Literal["peak", "valley"]
    message: str = Field(min_length=1, max_length=1_000)
    support: str | None = Field(default=None, max_length=4_000)
    layout_intent: str = Field(min_length=1, max_length=120)
    visual_role: Literal["anchor", "evidence", "atmosphere", "none"] = "none"
    visual: str | None = Field(default=None, max_length=1_000)
    density: Literal["focus", "standard", "dense"] = "standard"
    content_units: int = Field(default=0, ge=0, le=100)
    whitespace_intent: str | None = Field(default=None, max_length=500)
    anti_pattern: str | None = Field(default=None, max_length=500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)


class PresentationStory(_StrictSpec):
    audience: str = Field(min_length=1, max_length=500)
    goal: str = Field(min_length=1, max_length=1_000)
    slides: list[StorySlide] = Field(min_length=1, max_length=100)


class PresentationDesign(_StrictSpec):
    theme: str = Field(default="research-clean", min_length=1, max_length=80)
    typography: dict[Literal["title", "body", "caption"], str] = Field(default_factory=dict)
    color_roles: dict[
        Literal[
            "background",
            "surface",
            "text_primary",
            "text_secondary",
            "accent",
            "positive",
            "warning",
        ],
        str,
    ] = Field(default_factory=dict)
    layout_rules: list[str] = Field(default_factory=list, max_length=50)
    image_strategy: str | None = Field(default=None, max_length=1_000)
    chart_strategy: str | None = Field(default=None, max_length=1_000)


class SlideDesignMapping(_StrictSpec):
    id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    layout: str = Field(min_length=1, max_length=120)
    primary_visual: str = Field(min_length=1, max_length=500)
    visual_role: Literal["anchor", "evidence", "atmosphere", "none"] = "none"
    density: Literal["focus", "standard", "dense"] = "standard"
    content_units: int = Field(default=0, ge=0, le=100)
    whitespace_intent: str | None = Field(default=None, max_length=500)
    anti_pattern: str | None = Field(default=None, max_length=500)


class ArtifactPlan(_StrictSpec):
    schema_version: Literal[1] = 1
    artifact_type: Literal["pptx"] = "pptx"
    story: PresentationStory
    design: PresentationDesign
    slide_mapping: list[SlideDesignMapping] = Field(default_factory=list, max_length=100)
    open_questions: list[str] = Field(default_factory=list, max_length=50)


class PresentationTheme(_StrictSpec):
    name: str = Field(default="research-clean", min_length=1, max_length=80)
    background: str = Field(default="F7F8F6", pattern=r"^[0-9A-Fa-f]{6}$")
    surface: str = Field(default="FFFFFF", pattern=r"^[0-9A-Fa-f]{6}$")
    text_primary: str = Field(default="17211D", pattern=r"^[0-9A-Fa-f]{6}$")
    text_secondary: str = Field(default="5F6D66", pattern=r"^[0-9A-Fa-f]{6}$")
    accent: str = Field(default="167A5B", pattern=r"^[0-9A-Fa-f]{6}$")
    positive: str = Field(default="26845F", pattern=r"^[0-9A-Fa-f]{6}$")
    warning: str = Field(default="C37632", pattern=r"^[0-9A-Fa-f]{6}$")
    title_font: str = Field(default="Arial", min_length=1, max_length=100)
    body_font: str = Field(default="Arial", min_length=1, max_length=100)
    east_asia_font: str = Field(default="Microsoft YaHei", min_length=1, max_length=100)


class ChartSeries(_StrictSpec):
    name: str = Field(min_length=1, max_length=120)
    values: list[float] = Field(min_length=1, max_length=30)


class ChartSpec(_StrictSpec):
    chart_type: Literal["bar", "column", "line"] = "column"
    categories: list[str] = Field(min_length=1, max_length=12)
    series: list[ChartSeries] = Field(min_length=1, max_length=4)
    unit: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def _same_series_length(self) -> ChartSpec:
        expected = len(self.categories)
        if any(len(series.values) != expected for series in self.series):
            raise ValueError("chart 每个 series 的 values 数量必须与 categories 一致")
        return self


class MetricSpec(_StrictSpec):
    value: str = Field(min_length=1, max_length=30)
    label: str = Field(min_length=1, max_length=80)
    detail: str | None = Field(default=None, max_length=120)


class TimelineItem(_StrictSpec):
    label: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=80)
    detail: str | None = Field(default=None, max_length=160)


class MatrixItem(_StrictSpec):
    x: str = Field(min_length=1, max_length=80)
    y: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=100)


class CardSpec(_StrictSpec):
    """A compact, self-contained information unit for editable native card layouts."""

    title: str = Field(min_length=1, max_length=100)
    detail: str = Field(min_length=1, max_length=300)
    kicker: str | None = Field(default=None, max_length=40)


class DiagramNodeSpec(_StrictSpec):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")
    title: str = Field(min_length=1, max_length=60)
    detail: str | None = Field(default=None, max_length=120)
    emphasis: Literal["primary", "normal", "muted"] = "normal"


class DiagramEdgeSpec(_StrictSpec):
    source: str = Field(min_length=1, max_length=80)
    target: str = Field(min_length=1, max_length=80)
    label: str | None = Field(default=None, max_length=40)


class DiagramSpec(_StrictSpec):
    """Editable diagrams with deterministic topology and bounded node counts."""

    kind: Literal["process", "cycle", "hierarchy", "funnel", "pyramid"]
    nodes: list[DiagramNodeSpec] = Field(min_length=2, max_length=8)
    edges: list[DiagramEdgeSpec] = Field(default_factory=list, max_length=7)
    orientation: Literal["horizontal", "vertical"] = "horizontal"
    center_label: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _valid_diagram(self) -> DiagramSpec:
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("diagram node id 必须唯一")
        valid_ids = set(node_ids)
        if any(
            edge.source not in valid_ids
            or edge.target not in valid_ids
            or edge.source == edge.target
            for edge in self.edges
        ):
            raise ValueError("diagram edge 必须连接两个不同的现有 node id")

        minimum, maximum = {
            "process": (2, 6),
            "cycle": (3, 6),
            "hierarchy": (3, 8),
            "funnel": (3, 5),
            "pyramid": (3, 5),
        }[self.kind]
        if not minimum <= len(self.nodes) <= maximum:
            raise ValueError(f"{self.kind} diagram 只支持 {minimum}–{maximum} 个节点")
        if self.kind == "process" and self.orientation == "vertical" and len(self.nodes) > 5:
            raise ValueError("vertical process 最多支持 5 个节点")
        if self.kind != "process" and self.orientation != "horizontal":
            raise ValueError("orientation 只能用于 process diagram")
        if self.center_label is not None and self.kind != "cycle":
            raise ValueError("center_label 只能用于 cycle diagram")
        if self.kind != "hierarchy" and self.edges:
            raise ValueError(f"{self.kind} diagram 按 nodes 顺序自动连线，不能提供 edges")
        node_capacity = {
            "process": 110,
            "cycle": 60,
            "hierarchy": 75,
            "funnel": 90,
            "pyramid": 90,
        }[self.kind]
        if any(
            _display_units(node.title) + _display_units(node.detail or "") > node_capacity
            for node in self.nodes
        ):
            raise ValueError(f"{self.kind} diagram 节点文字过长；缩短标签或改用普通内容页")
        if self.kind != "hierarchy":
            return self

        if len(self.edges) != len(self.nodes) - 1:
            raise ValueError("hierarchy diagram 必须用 n-1 条 edges 构成一棵树")
        indegree = {node_id: 0 for node_id in node_ids}
        children: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
        for edge in self.edges:
            indegree[edge.target] += 1
            children[edge.source].append(edge.target)
        roots = [node_id for node_id, degree in indegree.items() if degree == 0]
        if len(roots) != 1 or any(degree > 1 for degree in indegree.values()):
            raise ValueError("hierarchy diagram 必须只有一个根节点，且每个子节点只有一个父节点")
        if any(len(values) > 4 for values in children.values()):
            raise ValueError("hierarchy diagram 单个节点最多连接 4 个直接子节点")
        depths = {roots[0]: 0}
        stack = [roots[0]]
        while stack:
            parent = stack.pop()
            for child in children[parent]:
                if child in depths:
                    raise ValueError("hierarchy diagram 不能包含环")
                depths[child] = depths[parent] + 1
                stack.append(child)
        if len(depths) != len(self.nodes):
            raise ValueError("hierarchy diagram 的所有节点必须从根节点可达")
        if max(depths.values()) > 3:
            raise ValueError("hierarchy diagram 最多支持 4 层")
        return self


class _CanvasPositionedElement(_StrictSpec):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")
    x: float = Field(ge=0, le=100, description="安全内容区内的横向百分比")
    y: float = Field(ge=0, le=100, description="安全内容区内的纵向百分比")
    width: float = Field(ge=5, le=100, description="安全内容区宽度百分比")
    height: float = Field(ge=4, le=100, description="安全内容区高度百分比")

    @model_validator(mode="after")
    def _inside_safe_canvas(self) -> _CanvasPositionedElement:
        if self.x + self.width > 100 or self.y + self.height > 100:
            raise ValueError("canvas element 必须完整位于 0–100 安全内容区内")
        return self


class CanvasTextElement(_CanvasPositionedElement):
    type: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=500)
    font_size: int = Field(default=20, ge=16, le=36)
    bold: bool = False
    color_role: Literal["text_primary", "text_secondary", "accent", "positive", "warning"] = (
        "text_primary"
    )
    align: Literal["left", "center", "right"] = "left"
    valign: Literal["top", "middle", "bottom"] = "top"

    @model_validator(mode="after")
    def _usable_text_height(self) -> CanvasTextElement:
        if self.height < max(8.0, self.font_size * 0.32):
            raise ValueError("canvas text 的 height 不足以容纳所选字号")
        return self


class CanvasShapeElement(_CanvasPositionedElement):
    type: Literal["shape"] = "shape"
    shape: Literal["rectangle", "rounded_rectangle", "oval", "chevron", "hexagon"] = (
        "rounded_rectangle"
    )
    title: str = Field(min_length=1, max_length=100)
    detail: str | None = Field(default=None, max_length=240)
    fill_role: Literal["background", "surface", "accent", "positive", "warning"] = "surface"
    fill_style: Literal["soft", "solid"] = "soft"
    font_size: int = Field(default=18, ge=16, le=28)

    @model_validator(mode="after")
    def _usable_shape_height(self) -> CanvasShapeElement:
        minimum = max(10.0, self.font_size * 0.38)
        if self.detail:
            minimum = max(minimum, 16.0)
        if self.height < minimum:
            raise ValueError("canvas shape 的 height 不足以容纳 title/detail")
        return self


class CanvasImageElement(_CanvasPositionedElement):
    type: Literal["image"] = "image"
    image_path: str = Field(min_length=1, max_length=4096)
    image_alt: str = Field(min_length=1, max_length=300)
    image_fit: Literal["contain", "cover"] = "contain"


class CanvasConnectorElement(_StrictSpec):
    type: Literal["connector"] = "connector"
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")
    source_id: str = Field(min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=80)
    label: str | None = Field(default=None, max_length=40)
    style: Literal["straight", "elbow"] = "straight"
    color_role: Literal["text_secondary", "accent", "positive", "warning"] = "accent"


CanvasElementSpec = Annotated[
    CanvasTextElement | CanvasShapeElement | CanvasImageElement | CanvasConnectorElement,
    Field(discriminator="type"),
]


class CanvasSpec(_StrictSpec):
    """A bounded coordinate DSL inside the renderer-owned title and safe area."""

    elements: list[CanvasElementSpec] = Field(min_length=2, max_length=24)

    @model_validator(mode="after")
    def _valid_canvas(self) -> CanvasSpec:
        ids = [element.id for element in self.elements]
        if len(ids) != len(set(ids)):
            raise ValueError("canvas element id 必须唯一")
        positioned = [
            element
            for element in self.elements
            if isinstance(
                element,
                (CanvasTextElement, CanvasShapeElement, CanvasImageElement),
            )
        ]
        if len(positioned) < 2:
            raise ValueError("canvas 至少需要 2 个可见元素")
        drawable_ids = {element.id for element in positioned}
        connectors = [
            element for element in self.elements if isinstance(element, CanvasConnectorElement)
        ]
        if len(connectors) > 10:
            raise ValueError("canvas 最多支持 10 条连接线")
        if any(
            connector.source_id not in drawable_ids
            or connector.target_id not in drawable_ids
            or connector.source_id == connector.target_id
            for connector in connectors
        ):
            raise ValueError("canvas connector 必须连接两个不同的现有可见元素 id")

        by_id = {element.id: element for element in positioned}
        for connector in connectors:
            source = by_id[connector.source_id]
            target = by_id[connector.target_id]
            start = (source.x + source.width / 2, source.y + source.height / 2)
            end = (target.x + target.width / 2, target.y + target.height / 2)
            points = (
                [start, ((start[0] + end[0]) / 2, start[1]), ((start[0] + end[0]) / 2, end[1]), end]
                if connector.style == "elbow"
                else [start, end]
            )
            for other in positioned:
                if other.id in {connector.source_id, connector.target_id}:
                    continue
                rectangle = (
                    other.x,
                    other.y,
                    other.x + other.width,
                    other.y + other.height,
                )
                if any(
                    _segment_intersects_rectangle(first, second, rectangle)
                    for first, second in pairwise(points)
                ):
                    raise ValueError(
                        f"canvas connector {connector.id} 穿过 element {other.id}；"
                        "调整元素位置或改用 diagram"
                    )

        for element in positioned:
            if isinstance(element, CanvasTextElement):
                content = element.text
                font_size = element.font_size
            elif isinstance(element, CanvasShapeElement):
                content = element.title + (element.detail or "")
                font_size = element.font_size
            else:
                continue
            capacity = max(
                16,
                round(element.width * element.height * 0.11 * 20 / font_size),
            )
            if _display_units(content) > capacity:
                raise ValueError(
                    f"canvas element {element.id} 的文字超过当前边界容量；"
                    "缩短文字或增大 width/height"
                )

        for first, second in combinations(positioned, 2):
            overlap_width = max(
                0.0,
                min(first.x + first.width, second.x + second.width) - max(first.x, second.x),
            )
            overlap_height = max(
                0.0,
                min(first.y + first.height, second.y + second.height) - max(first.y, second.y),
            )
            intersection = overlap_width * overlap_height
            smaller = min(first.width * first.height, second.width * second.height)
            if smaller and intersection / smaller > 0.15:
                raise ValueError(
                    f"canvas elements {first.id} 与 {second.id} 大面积重叠；"
                    "把文字放进 shape，或重新划分边界"
                )
        return self


PresentationLayout = Literal[
    "title",
    "statement",
    "section",
    "two_column",
    "comparison",
    "big_number",
    "chart",
    "image_text",
    "quote",
    "timeline",
    "matrix",
    "cards",
    "activity",
    "diagram",
    "canvas",
]


class SlideSpec(_StrictSpec):
    id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    role: Literal["hero", "supporting", "transition"] = "supporting"
    rhythm: Literal["peak", "valley"] = "valley"
    title: str = Field(min_length=1, max_length=300)
    layout: PresentationLayout
    subtitle: str | None = Field(default=None, max_length=500)
    body: str | None = Field(default=None, max_length=4_000)
    bullets: list[SlideBullet] = Field(default_factory=list, max_length=6)
    left_title: str | None = Field(default=None, max_length=160)
    left_items: list[SlideBullet] = Field(default_factory=list, max_length=5)
    right_title: str | None = Field(default=None, max_length=160)
    right_items: list[SlideBullet] = Field(default_factory=list, max_length=5)
    metrics: list[MetricSpec] = Field(default_factory=list, max_length=4)
    quote_attribution: str | None = Field(default=None, max_length=200)
    timeline: list[TimelineItem] = Field(default_factory=list, max_length=4)
    matrix: list[MatrixItem] = Field(default_factory=list, max_length=4)
    cards: list[CardSpec] = Field(default_factory=list, max_length=4)
    activity_prompt: str | None = Field(default=None, max_length=300)
    activity_steps: list[SlideBullet] = Field(default_factory=list, max_length=4)
    activity_timebox: str | None = Field(default=None, max_length=40)
    activity_debrief: str | None = Field(default=None, max_length=300)
    diagram: DiagramSpec | None = None
    canvas: CanvasSpec | None = None
    chart: ChartSpec | None = None
    image_path: str | None = Field(default=None, min_length=1, max_length=4096)
    image_caption: str | None = Field(default=None, max_length=300)
    image_alt: str | None = Field(default=None, max_length=300)
    image_fit: Literal["contain", "cover"] = "contain"
    notes: str | None = Field(default=None, max_length=4_000)

    @model_validator(mode="after")
    def _layout_content_contract(self) -> SlideSpec:
        image_layouts = {"title", "section", "image_text"}
        if self.image_path is not None and self.layout not in image_layouts:
            raise ValueError("image_path 只能用于 title、section 或 image_text layout")
        if self.image_path is None and (
            self.image_caption is not None
            or self.image_alt is not None
            or self.image_fit != "contain"
        ):
            raise ValueError("image_caption、image_alt 与 image_fit 必须与 image_path 一起使用")
        if self.image_path is not None and self.image_alt is None:
            self.image_alt = self.image_caption or self.title

        if self.layout == "title" and self.subtitle is not None and self.body is not None:
            raise ValueError("title 不能同时提供 subtitle 与 body，body 不会显示")
        if self.layout == "image_text" and self.bullets and self.body:
            raise ValueError("image_text 不能同时提供 bullets 与 body，body 不会显示")

        if self.layout in {"two_column", "comparison"}:
            if not self.left_title or not self.right_title:
                raise ValueError(f"{self.layout} 必须提供左右标题")
            if not self.left_items or not self.right_items:
                raise ValueError(f"{self.layout} 必须提供左右非空条目")
        elif self.layout == "big_number" and not self.metrics:
            raise ValueError("big_number 必须提供 metrics")
        elif self.layout == "chart":
            if self.chart is None:
                raise ValueError("chart layout 必须提供 chart")
            if len(self.chart.categories) * len(self.chart.series) < 2:
                raise ValueError("chart 至少需要两个可比较数据点")
        elif self.layout == "image_text":
            if self.image_path is None:
                raise ValueError("image_text 必须提供 image_path")
            if not self.bullets and not self.body and not self.image_caption:
                raise ValueError("image_text 必须提供 bullets、body 或 image_caption 作为解读")
        elif self.layout == "quote" and not self.body:
            raise ValueError("quote 必须提供 body")
        elif self.layout == "timeline" and len(self.timeline) < 2:
            raise ValueError("timeline 至少包含 2 个节点")
        elif self.layout == "matrix" and len(self.matrix) < 2:
            raise ValueError("matrix 至少包含 2 个对象")
        elif self.layout == "cards" and len(self.cards) < 2:
            raise ValueError("cards 至少包含 2 张有标题和说明的卡片")
        elif self.layout == "activity":
            if not self.activity_prompt:
                raise ValueError("activity 必须提供 activity_prompt")
            if len(self.activity_steps) < 2:
                raise ValueError("activity 至少提供 2 个可执行步骤")
            if not self.activity_timebox:
                raise ValueError("activity 必须提供 activity_timebox")
            if not self.activity_debrief:
                raise ValueError("activity 必须提供 activity_debrief")
        elif self.layout == "diagram" and self.diagram is None:
            raise ValueError("diagram layout 必须提供 diagram")
        elif self.layout == "canvas" and self.canvas is None:
            raise ValueError("canvas layout 必须提供 canvas")

        populated: set[str] = set()
        for field_name in (
            "subtitle",
            "body",
            "left_title",
            "right_title",
            "quote_attribution",
            "chart",
            "image_path",
            "image_caption",
            "image_alt",
            "activity_prompt",
            "activity_timebox",
            "activity_debrief",
            "diagram",
            "canvas",
        ):
            if getattr(self, field_name) is not None:
                populated.add(field_name)
        for field_name in (
            "bullets",
            "left_items",
            "right_items",
            "metrics",
            "timeline",
            "matrix",
            "cards",
            "activity_steps",
        ):
            if getattr(self, field_name):
                populated.add(field_name)
        if self.image_fit != "contain":
            populated.add("image_fit")
        allowed_by_layout = {
            "title": {
                "subtitle",
                "body",
                "image_path",
                "image_caption",
                "image_alt",
                "image_fit",
            },
            "statement": {"subtitle", "body"},
            "section": {
                "subtitle",
                "body",
                "image_path",
                "image_caption",
                "image_alt",
                "image_fit",
            },
            "two_column": {"left_title", "left_items", "right_title", "right_items"},
            "comparison": {"left_title", "left_items", "right_title", "right_items"},
            "big_number": {"metrics", "body"},
            "chart": {"chart", "body"},
            "image_text": {
                "body",
                "bullets",
                "image_path",
                "image_caption",
                "image_alt",
                "image_fit",
            },
            "quote": {"body", "quote_attribution"},
            "timeline": {"timeline"},
            "matrix": {"matrix"},
            "cards": {"cards"},
            "activity": {
                "activity_prompt",
                "activity_steps",
                "activity_timebox",
                "activity_debrief",
            },
            "diagram": {"diagram"},
            "canvas": {"canvas"},
        }
        ignored = sorted(populated - allowed_by_layout[self.layout])
        if ignored:
            raise ValueError(f"{self.layout} layout 不消费字段：{', '.join(ignored)}")
        return self


class PresentationSpec(ArtifactEnvelope):
    artifact_type: Literal["pptx"] = "pptx"
    visual_kit: str = Field(
        default="workpilot-clean",
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    theme: PresentationTheme = Field(default_factory=PresentationTheme)
    slides: list[SlideSpec] = Field(min_length=1, max_length=100)

    @field_validator("visual_kit")
    @classmethod
    def _known_visual_kit(cls, value: str) -> str:
        # Lazy import keeps the generic contract module free of renderer import cycles while
        # still making an unknown kit a schema error rather than a late rendering surprise.
        from app.cowork.skills.builtin.pptx.scripts.visual_kits import visual_kit_ids

        if value not in visual_kit_ids():
            raise ValueError(f"未知 PPT 视觉套件：{value}")
        return value

    @model_validator(mode="after")
    def _unique_slide_ids(self) -> PresentationSpec:
        ids = [slide.id for slide in self.slides]
        if len(ids) != len(set(ids)):
            raise ValueError("slide id 必须唯一")
        valid = set(ids)
        if any(
            claim.target_type != "pptx_slide" or claim.target_id not in valid
            for claim in self.claims
        ):
            raise ValueError("PPT claim 必须绑定存在的 pptx_slide id")
        return self


class DocumentBlock(_StrictSpec):
    id: str | None = Field(default=None, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    type: Literal["paragraph", "bullets", "table", "quote", "callout", "image"]
    text: str | None = Field(default=None, max_length=20_000)
    items: list[Annotated[str, Field(min_length=1, max_length=1_000)]] = Field(
        default_factory=list, max_length=100
    )
    headers: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list, max_length=10
    )
    rows: list[list[Scalar]] = Field(default_factory=list, max_length=2_000)
    style: Literal["normal", "lead", "caption", "positive", "warning"] = "normal"
    image_path: str | None = Field(default=None, min_length=1, max_length=4096)
    image_caption: str | None = Field(default=None, max_length=500)
    image_alt: str | None = Field(default=None, max_length=500)
    image_width_inches: float | None = Field(default=None, ge=1.0, le=6.5)

    @model_validator(mode="after")
    def _valid_table_shape(self) -> DocumentBlock:
        text_blocks = {"paragraph", "quote", "callout"}
        if self.type in text_blocks:
            if self.text is None or not self.text.strip():
                raise ValueError(f"{self.type} block 必须提供非空 text")
        elif self.text is not None:
            raise ValueError("text 只能用于 paragraph、quote 或 callout block")
        if self.type == "bullets":
            if not self.items:
                raise ValueError("bullets block 必须提供至少一个 item")
        elif self.items:
            raise ValueError("items 只能用于 bullets block")
        if self.type == "table":
            column_count = len(self.headers) or max((len(row) for row in self.rows), default=0)
            if column_count < 1 or column_count > 10:
                raise ValueError("DOCX table 必须包含 1–10 列")
            if any(len(row) != column_count for row in self.rows):
                raise ValueError("DOCX table 每行列数必须一致")
        elif self.headers or self.rows:
            raise ValueError("headers 与 rows 只能用于 table block")
        if self.type != "paragraph" and self.style != "normal":
            raise ValueError("style 只能用于 paragraph block")
        if self.type == "image":
            if self.image_path is None:
                raise ValueError("image block 必须提供 image_path")
            if self.image_alt is None:
                raise ValueError("image block 必须提供 image_alt；装饰图可使用空字符串")
        elif any(
            value is not None
            for value in (
                self.image_path,
                self.image_caption,
                self.image_alt,
                self.image_width_inches,
            )
        ):
            raise ValueError("image_path、caption、alt 与 width 只能用于 image block")
        return self


class DocumentSection(_StrictSpec):
    id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    heading: str = Field(min_length=1, max_length=300)
    level: int = Field(default=1, ge=1, le=3)
    blocks: list[DocumentBlock] = Field(default_factory=list, max_length=500)


class DocumentSpec(ArtifactEnvelope):
    artifact_type: Literal["docx"] = "docx"
    subtitle: str | None = Field(default=None, max_length=500)
    author: str | None = Field(default=None, max_length=200)
    sections: list[DocumentSection] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _claim_targets_exist(self) -> DocumentSpec:
        levels = [section.level for section in self.sections]
        if levels and levels[0] != 1:
            raise ValueError("DOCX 第一节必须从 Heading 1 开始")
        if any(current > previous + 1 for previous, current in pairwise(levels)):
            raise ValueError("DOCX 标题层级不能跳级")
        block_ids = {
            block.id
            for section in self.sections
            for block in section.blocks
            if block.id and block.type == "paragraph"
        }
        if any(
            claim.target_type != "docx_paragraph" or claim.target_id not in block_ids
            for claim in self.claims
        ):
            raise ValueError("DOCX claim 必须绑定带 id 的 paragraph block")
        return self


class WorkbookCell(_StrictSpec):
    address: str = Field(pattern=r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")
    value: Scalar = None
    formula: str | None = Field(default=None, min_length=2, max_length=2_000)
    style: Literal["normal", "title", "header", "metric", "currency", "percent", "date"] = "normal"

    @model_validator(mode="after")
    def _value_or_formula(self) -> WorkbookCell:
        if self.formula is not None and not self.formula.startswith("="):
            raise ValueError("formula 必须以 = 开头")
        if self.formula is not None and self.value is not None:
            raise ValueError("cell 不能同时设置 value 和 formula")
        return self


class WorkbookTable(_StrictSpec):
    name: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$")
    anchor: str = Field(default="A1", pattern=r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")
    headers: list[str] = Field(min_length=1, max_length=100)
    rows: list[list[Scalar]] = Field(default_factory=list, max_length=100_000)

    @model_validator(mode="after")
    def _rectangular(self) -> WorkbookTable:
        if any(len(row) != len(self.headers) for row in self.rows):
            raise ValueError("table 每行列数必须与 headers 一致")
        return self


class WorkbookChart(_StrictSpec):
    chart_type: Literal["bar", "column", "line"] = "column"
    title: str = Field(min_length=1, max_length=200)
    data_range: str = Field(min_length=3, max_length=100)
    categories_range: str = Field(min_length=3, max_length=100)
    anchor: str = Field(default="H2", pattern=r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")


class WorksheetSpec(_StrictSpec):
    name: str = Field(min_length=1, max_length=31)
    freeze_panes: str | None = Field(default=None, pattern=r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")
    cells: list[WorkbookCell] = Field(default_factory=list, max_length=20_000)
    tables: list[WorkbookTable] = Field(default_factory=list, max_length=100)
    charts: list[WorkbookChart] = Field(default_factory=list, max_length=50)


class WorkbookSpec(ArtifactEnvelope):
    artifact_type: Literal["xlsx"] = "xlsx"
    sheets: list[WorksheetSpec] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _unique_sheet_names(self) -> WorkbookSpec:
        names = [sheet.name.casefold() for sheet in self.sheets]
        if len(names) != len(set(names)):
            raise ValueError("sheet name 必须唯一（不区分大小写）")
        if self.claims:
            raise ValueError("ArtifactManifest v1 尚不支持 XLSX claim target")
        return self


class HtmlSection(_StrictSpec):
    id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    heading: str = Field(min_length=1, max_length=300)
    blocks: list[DocumentBlock] = Field(default_factory=list, max_length=500)


class HtmlReportSpec(ArtifactEnvelope):
    artifact_type: Literal["html"] = "html"
    summary: str | None = Field(default=None, max_length=4_000)
    sections: list[HtmlSection] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _html_claim_targets(self) -> HtmlReportSpec:
        ids = {section.id for section in self.sections}
        if any(
            claim.target_type != "html_section" or claim.target_id not in ids
            for claim in self.claims
        ):
            raise ValueError("HTML claim 必须绑定存在的 html_section id")
        return self


class PdfSpec(ArtifactEnvelope):
    artifact_type: Literal["pdf"] = "pdf"
    summary: str | None = Field(default=None, max_length=4_000)
    sections: list[HtmlSection] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _pdf_claim_targets(self) -> PdfSpec:
        ids = {section.id for section in self.sections}
        if any(
            claim.target_type != "pdf_section" or claim.target_id not in ids
            for claim in self.claims
        ):
            raise ValueError("PDF claim 必须绑定存在的 pdf_section id")
        return self


ArtifactSpec = Annotated[
    PresentationSpec | DocumentSpec | WorkbookSpec | HtmlReportSpec | PdfSpec,
    Field(discriminator="artifact_type"),
]


__all__ = [
    "ArtifactEnvelope",
    "ArtifactPlan",
    "ArtifactSpec",
    "ClaimRecord",
    "DocumentSpec",
    "HtmlReportSpec",
    "PdfSpec",
    "PresentationSpec",
    "WorkbookSpec",
]
