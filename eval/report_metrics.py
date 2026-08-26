"""跑批报告的读取与逐样本指标抽取。

`compare.py`（两次跑批配对）与 `strategy_matrix.py`（多策略配对矩阵）共用这里的
指标定义——同一个指标只能有一处实现，否则两个工具迟早给出对不上的数字。

所有指标都表达成 `RatioPoint(numerator, denominator)`：聚合值 = Σ分子/Σ分母。
`denominator=0` 表示该样本不适用于这个指标，重采样时既不进分子也不进分母。
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval.stats import INELIGIBLE, RatioPoint

KIND_RETRIEVAL = "retrieval"
KIND_GENERATION = "generation"

Extractor = Callable[[dict[str, Any], dict[str, Any]], RatioPoint]

VERDICT_LABELS = {
    "improved": "显著提升",
    "regressed": "显著下降",
    "inconclusive": "无显著差异",
    "not_applicable": "不适用",
}


@dataclass(frozen=True)
class MetricSpec:
    name: str
    title: str
    unit: str
    higher_is_better: bool
    extract: Extractor
    note: str = ""


# --------------------------------------------------------------------- 检索轨


def _retrieval_metric(field: str) -> Extractor:
    def extract(item: dict[str, Any], config: dict[str, Any]) -> RatioPoint:
        retrieval = item.get("retrieval")
        # 不可答题没有 gold span，检索质量指标整体不适用
        if not isinstance(retrieval, dict) or retrieval.get(field) is None:
            return INELIGIBLE
        return RatioPoint(float(retrieval[field]), 1.0)

    return extract


def top_k_chunks(item: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    retrieved = item.get("retrieved")
    if not isinstance(retrieved, list):
        return []
    top_k = item.get("effective_top_k") or config.get("top_k")
    chunks = [chunk for chunk in retrieved if isinstance(chunk, dict)]
    return chunks[: int(top_k)] if top_k else chunks


def _context_redundancy(item: dict[str, Any], config: dict[str, Any]) -> RatioPoint:
    """Top-K 上下文里被重复覆盖的字符占比。

    同一 version 内多个 chunk 的字符区间重叠，就是塞给模型的重复文本。
    E1 跨分块策略比较必须报这个：大 chunk 容易靠"包住"拿高 recall，
    冗余率会把这部分代价显性化（docs/06 §2.1）。
    """
    chunks = top_k_chunks(item, config)
    if not chunks:
        return INELIGIBLE
    spans: dict[str, list[tuple[int, int]]] = {}
    total = 0
    for chunk in chunks:
        start, end = chunk.get("char_start"), chunk.get("char_end")
        if start is None or end is None or end <= start:
            continue
        total += int(end) - int(start)
        spans.setdefault(str(chunk.get("version_id")), []).append((int(start), int(end)))
    if total <= 0:
        return INELIGIBLE
    union = sum(_union_length(ranges) for ranges in spans.values())
    return RatioPoint(float(total - union), float(total))


def _union_length(ranges: list[tuple[int, int]]) -> int:
    merged_end = None
    total = 0
    for start, end in sorted(ranges):
        if merged_end is None or start > merged_end:
            total += end - start
            merged_end = end
        elif end > merged_end:
            total += end - merged_end
            merged_end = end
    return total


def _retrieved_tokens(item: dict[str, Any], config: dict[str, Any]) -> RatioPoint:
    """检索侧成本代理：塞进上下文的 token 数（不可答题同样计入）。"""
    chunks = top_k_chunks(item, config)
    if not chunks:
        return INELIGIBLE
    tokens = sum(int(chunk.get("content_tokens") or 0) for chunk in chunks)
    return RatioPoint(float(tokens), 1.0)


_REFUSAL_THRESHOLD_SOURCES = frozenset({"dev_calibrated", "independent_calibration", "manual"})


def _refusal_correct_by_score(item: dict[str, Any], config: dict[str, Any]) -> RatioPoint:
    threshold = config.get("refusal_threshold")
    score = item.get("top_score")
    if threshold is None or score is None:
        return INELIGIBLE
    # 阈值必须声明标定来源才算数。没有来源的历史报告一律不适用——0.35 那批就是
    # 把归一化打分器的阈值套在 RRF 分数上，指标照常输出但恒为「全拒答」。
    # 让它 INELIGIBLE，比让它带着一个 answerable_f1=0 的参照点进门禁要好。
    if config.get("refusal_threshold_source") not in _REFUSAL_THRESHOLD_SOURCES:
        return INELIGIBLE
    # 各自用自己 config 里的阈值：换阈值本身就是被对照的变量
    answered = float(score) >= float(threshold)
    return RatioPoint(float(answered is bool(item["answerable"])), 1.0)


def _latency(item: dict[str, Any], config: dict[str, Any]) -> RatioPoint:
    latency = item.get("latency_ms")
    if latency is None or item.get("error") is not None:
        return INELIGIBLE
    return RatioPoint(float(latency), 1.0)


# --------------------------------------------------------------------- 生成轨


def _completed(item: dict[str, Any]) -> bool:
    return item.get("error") is None


def _generation_flag(
    field: str,
    subfield: str | None = None,
    *,
    non_refusal_only: bool = False,
    answerable_only: bool = False,
) -> Extractor:
    def extract(item: dict[str, Any], config: dict[str, Any]) -> RatioPoint:
        if not _completed(item):
            return INELIGIBLE
        # 一侧拒答一侧作答时，配对逻辑会把两侧一起剔除，不会拿拒答样本充数
        if non_refusal_only and item.get("refused") is not False:
            return INELIGIBLE
        if answerable_only and not item.get("answerable"):
            return INELIGIBLE
        value = item[field]
        flag = value[subfield] if subfield else value
        return RatioPoint(float(bool(flag)), 1.0)

    return extract


def _citation_gold_alignment(item: dict[str, Any], config: dict[str, Any]) -> RatioPoint:
    if not _completed(item) or item.get("refused") is not False:
        return INELIGIBLE
    alignment = item["citation_gold_alignment"]
    total = float(alignment["total"])
    if not total:
        return INELIGIBLE
    # micro 口径：重采样时按引用条数加权，与单跑报告的聚合方式一致
    return RatioPoint(float(alignment["aligned"]), total)


def _citation_support_answerable(item: dict[str, Any], config: dict[str, Any]) -> RatioPoint:
    """可答题上「引文真的落在 gold 证据里」的比例，拒答记 0 分而不是退出分母。

    `citation_wellformed` 只查标签格式、引用记录与 quote 逐字一致，按构造几乎恒为
    1.0，不能当质量信号。真正要守的是引文能不能支撑结论，而那只有对着 gold span
    才能判。分母固定为可答题：否则模型多拒答一条，分母就少一条，指标反而变好看。
    """

    if not _completed(item) or not item.get("answerable"):
        return INELIGIBLE
    # 可答却拒答：没有产出任何有支撑的引文，计 0，不允许静默退出分母
    if item.get("refused") is not False:
        return RatioPoint(0.0, 1.0)
    alignment = item["citation_gold_alignment"]
    total = float(alignment["total"])
    if not total:
        # 作答了却一条引文都没给，同样是零支撑
        return RatioPoint(0.0, 1.0)
    return RatioPoint(float(alignment["aligned"]) / total, 1.0)


def _constraint_pass_answerable(item: dict[str, Any], config: dict[str, Any]) -> RatioPoint:
    """可答题的约束通过率；拒答计为失败。

    旧口径把拒答样本算作「通过」，于是多拒答就能把分数刷上去——baseline 里
    constraint_pass(0.571) 高于 constraint_pass_answerable(0.474) 就是这个效应。
    """

    if not _completed(item) or not item.get("answerable"):
        return INELIGIBLE
    if item.get("refused") is not False:
        return RatioPoint(0.0, 1.0)
    return RatioPoint(float(bool(item["constraint_pass"]["passed"])), 1.0)


def _optional_number(*path: str) -> Extractor:
    """读取可选的成本字段；字段缺失时返回不适用，绝不当成 0。"""

    def extract(item: dict[str, Any], config: dict[str, Any]) -> RatioPoint:
        if not _completed(item):
            return INELIGIBLE
        value: Any = item
        for key in path:
            if not isinstance(value, dict) or key not in value:
                return INELIGIBLE
            value = value[key]
        if value is None or isinstance(value, bool):
            return INELIGIBLE
        return RatioPoint(float(value), 1.0)

    return extract


RETRIEVAL_METRICS: tuple[MetricSpec, ...] = (
    MetricSpec(
        "span_recall_at_k",
        "span Recall@K",
        "ratio",
        True,
        _retrieval_metric("span_recall_at_k"),
    ),
    MetricSpec(
        "budget_span_recall",
        "budget span Recall",
        "ratio",
        True,
        _retrieval_metric("budget_span_recall"),
    ),
    MetricSpec("ndcg_at_k", "nDCG@K", "ratio", True, _retrieval_metric("ndcg_at_k")),
    MetricSpec(
        "alpha_ndcg_at_k",
        "α-nDCG@K",
        "ratio",
        True,
        _retrieval_metric("alpha_ndcg_at_k"),
    ),
    MetricSpec("mrr", "MRR", "ratio", True, _retrieval_metric("mrr")),
    MetricSpec(
        "context_precision",
        "context precision",
        "ratio",
        True,
        _retrieval_metric("context_precision"),
    ),
    MetricSpec(
        "gold_doc_recall_at_k",
        "gold 文档 Recall@K",
        "ratio",
        True,
        _retrieval_metric("gold_doc_recall_at_k"),
        note="命中的 gold 文档数占比。跨文档题被单一文档霸榜时直接掉档。",
    ),
    MetricSpec(
        "max_doc_share_at_k",
        "单文档最大占比@K",
        "ratio",
        False,
        _retrieval_metric("max_doc_share_at_k"),
        note="Top-K 中单篇文档占比，越低越好。1.0 表示被一篇文档霸榜。",
    ),
    MetricSpec(
        "context_redundancy",
        "上下文冗余率",
        "ratio",
        False,
        _context_redundancy,
        note="Top-K chunk 字符区间的重复覆盖占比，越低越好。",
    ),
    MetricSpec(
        "retrieved_tokens",
        "检索上下文 token",
        "count",
        False,
        _retrieved_tokens,
        note="塞进上下文的 token 数，是检索侧成本代理；不含生成侧 token。",
    ),
    MetricSpec(
        "refusal_correct_at_threshold",
        "配置阈值下拒答正确率",
        "ratio",
        True,
        _refusal_correct_by_score,
    ),
    MetricSpec("latency_ms", "检索延迟均值(ms)", "ms", False, _latency),
)

GENERATION_METRICS: tuple[MetricSpec, ...] = (
    MetricSpec(
        "refusal_correct",
        "拒答正确率",
        "ratio",
        True,
        _generation_flag("refusal_correct"),
    ),
    MetricSpec(
        "citation_wellformed_non_refusal",
        "引文格式合规(非拒答)",
        "ratio",
        True,
        _generation_flag("citation_validity", "valid", non_refusal_only=True),
        note=(
            "只校验标签格式、引用记录、block/version/document 与 quote 逐字一致。"
            "这是前置条件不是质量信号，按构造几乎恒为 1.0；质量看 citation_support_answerable。"
        ),
    ),
    MetricSpec(
        "citation_support_answerable",
        "引文 gold 支撑率(可答题)",
        "ratio",
        True,
        _citation_support_answerable,
        note="分母固定为可答题，拒答与零引文都计 0，堵住「多拒答把分母做小」的路。",
    ),
    MetricSpec(
        "constraint_pass",
        "constraint_pass(全部，诊断用)",
        "ratio",
        True,
        _generation_flag("constraint_pass", "passed"),
        note="含拒答样本，拒答天然通过；只作诊断，不进门禁。",
    ),
    MetricSpec(
        "constraint_pass_answerable",
        "constraint_pass(可答题)",
        "ratio",
        True,
        _constraint_pass_answerable,
        note="拒答计为失败，避免靠提高拒答率刷分。",
    ),
    MetricSpec(
        "citation_gold_alignment",
        "引用 gold span 对齐率",
        "ratio",
        True,
        _citation_gold_alignment,
    ),
    MetricSpec("latency_ms", "端到端延迟均值(ms)", "ms", False, _latency),
    MetricSpec(
        "total_tokens",
        "生成 token 总量",
        "count",
        False,
        _optional_number("total_tokens"),
        note="需要跑批导出逐条 token 用量；当前跑批未记录时标记为不可用。",
    ),
    MetricSpec(
        "cost_usd",
        "单题成本(USD)",
        "cost",
        False,
        _optional_number("cost_usd"),
        note="需要跑批导出逐条成本；当前跑批未记录时标记为不可用。",
    ),
)

METRICS: dict[str, tuple[MetricSpec, ...]] = {
    KIND_RETRIEVAL: RETRIEVAL_METRICS,
    KIND_GENERATION: GENERATION_METRICS,
}

PRIMARY_METRIC: dict[str, str] = {
    # budget span recall 是跨策略比较的主口径（docs/06 §2.1）
    KIND_RETRIEVAL: "budget_span_recall",
    KIND_GENERATION: "constraint_pass",
}

# 这些配置项一旦不同，两份报告算的就不是同一个指标，默认拒绝比较。
CONTROLLED_KEYS: dict[str, tuple[str, ...]] = {
    KIND_RETRIEVAL: ("origin", "top_k", "token_budget", "theta", "alpha"),
    KIND_GENERATION: ("origin", "top_k", "theta"),
}


# ------------------------------------------------------------------- 报告读取


@dataclass(frozen=True)
class LoadedReport:
    path: Path
    payload: dict[str, Any]
    kind: str

    @property
    def config(self) -> dict[str, Any]:
        config = self.payload.get("config")
        return config if isinstance(config, dict) else {}

    @property
    def items(self) -> list[dict[str, Any]]:
        items = self.payload["items"]
        if not isinstance(items, list):
            raise TypeError(f"报告 items 必须是数组: {self.path}")
        return items

    @property
    def specs(self) -> tuple[MetricSpec, ...]:
        return METRICS[self.kind]

    def describe(self) -> dict[str, object]:
        return {
            "label": self.payload.get("label"),
            "run_id": self.payload.get("run_id"),
            "git_sha": self.payload.get("git_sha"),
            "config_hash": self.payload.get("config_hash"),
            "item_count": len(self.items),
            "source_report": str(self.path),
        }


def load_report(path: Path) -> LoadedReport:
    resolved = path / "report.json" if path.is_dir() else path
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"报告根节点必须是对象: {resolved}")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError(f"报告缺少逐样本 items，无法配对: {resolved}")
    if [index for index, item in enumerate(items) if not item.get("item_id")]:
        raise ValueError(
            f"报告的 items 缺少 item_id，无法按样本配对（refusal_baseline 报告不支持）: {resolved}"
        )
    return LoadedReport(path=resolved, payload=payload, kind=detect_kind(items[0], resolved))


def detect_kind(item: dict[str, Any], path: Path) -> str:
    if "citations" in item:
        return KIND_GENERATION
    if "retrieval" in item:
        return KIND_RETRIEVAL
    raise ValueError(f"无法识别报告类型，仅支持 retrieval 与 generation 报告: {path}")


def config_diff(
    left: dict[str, Any], right: dict[str, Any], *, ignore: Sequence[str] = ()
) -> dict[str, dict[str, Any]]:
    skipped = set(ignore)
    return {
        key: {"baseline": left.get(key), "candidate": right.get(key)}
        for key in sorted(set(left) | set(right))
        if key not in skipped and left.get(key) != right.get(key)
    }


def gold_span_fingerprint(
    item: dict[str, Any],
) -> tuple[tuple[str, int, int, str], ...]:
    """gold span 的身份指纹：稳定来源 + 字符区间 + quote。

    新文件系统报告优先使用 content hash + page，旧报告回退到 document version UUID。
    锚的是来源字符区间而不是 chunk（ADR-0006），所以这个指纹
    在不同分块策略之间必须完全一致；不一致说明混了解析版本或重标过，
    此时跨策略比较无效。
    """
    diagnostics = item.get("span_diagnostics")
    if not isinstance(diagnostics, list):
        return ()
    return tuple(
        sorted(
            (
                (
                    f"{span.get('content_hash')}:page:{span.get('page_no')}"
                    if span.get("content_hash")
                    else str(span.get("version_id"))
                ),
                int(span.get("char_start", -1)),
                int(span.get("char_end", -1)),
                str(span.get("quote", "")),
            )
            for span in diagnostics
            if isinstance(span, dict)
        )
    )


# ------------------------------------------------------------------- 展示格式


def as_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("报告节点必须是对象")
    return value


def as_list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError("报告节点必须是数组")
    return value


def short_hash(value: Any) -> str:
    return "N/A" if value is None else str(value)[:12]


def config_cell(value: Any) -> str:
    return "—" if value is None else f"`{value}`"


def truncate_question(value: Any, limit: int = 32) -> str:
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_value(value: Any, unit: str) -> str:
    if value is None:
        return "N/A"
    number = float(value)
    if unit == "ms":
        return f"{number:.1f}"
    if unit == "count":
        return f"{number:.0f}"
    if unit == "cost":
        return f"{number:.6f}"
    return f"{number:.2%}"


def format_delta(value: Any, unit: str) -> str:
    if value is None:
        return "N/A"
    # 浮点求和残差会渲染成 "-0.0"，先按展示精度归零，避免读成"下降"
    digits = {"ms": 1, "count": 0, "cost": 6}.get(unit, 4)
    number = round(float(value), digits) or 0.0
    if unit == "ms":
        return f"{number:+.1f}"
    if unit == "count":
        return f"{number:+.0f}"
    if unit == "cost":
        return f"{number:+.6f}"
    return f"{number:+.2%}"
