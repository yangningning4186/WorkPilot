"""阅读层三指标：`read_before_claim` / `quote_verifiability` / `locator_accuracy`。

口径见 [docs/04 §5](../../docs/04-知识与阅读设计.md)。这一层回答的问题和检索层、
生成层都不一样：**模型说"这句话在第 12 页"的时候，它是真的读过第 12 页吗，那句话真的
在那里吗，页码真的是 12 吗。**

三条指标的分母刻意互不重叠，这样一次跑批能把失败归到具体哪一环：

* ``read_before_claim`` —— 分母是回答里的每一个 ``[p.N]``。这一条纯规则判：
  N 在之前被成功的 ``read_material`` 覆盖过就算数。playbook 的第一条纪律是"没读过
  就是不知道"，搜索片段是开窗截出来的、可能从句子中间断开，据此补全含义就是在编。
* ``quote_verifiability`` —— 分母是模型交给 ``reader_goto`` / ``reader_annotate``
  的每一条 quote。**按语言分桶报告**：用中文问英文论文时模型给的"引文"往往是它自己
  的译文，这一档必然低，混在一起报会把跨语言的正常损耗说成缺陷。
* ``locator_accuracy`` —— 分母是**已验证存在**的那些 quote。引文根本不在文中时，
  "它在第几页"没有答案，把它算进来只会让两条指标互相污染。

**校验复用产品自己的 `verify_quote`**，不另写一套匹配。评测在这里衡量的是模型，不是
校验器；换一套宽松些的匹配规则，只会让评测和线上对"什么叫引对了"给出两个答案。

材料从 fixture 正文重建，而不是去读跑批时的临时工作区：报告要能离线重算
（``cowork_runner rescore``），而那个目录那时早就没了。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.cowork.reading import (
    Material,
    ReadingError,
    parse_locators,
    units_from_sections,
    verify_quote,
)
from app.ingest.markdown import parse_markdown

# 阅读那一族工具。其余工具即使碰了同一个文件也不算阅读行为。
READING_TOOLS = frozenset(
    {
        "material_outline",
        "search_material",
        "read_material",
        "reader_goto",
        "reader_annotate",
    }
)
_QUOTE_TOOLS = frozenset({"reader_goto", "reader_annotate"})

# playbook 规定的引用写法：[p.12]、[p.12,17]、[p.12-14]。三种都要认——只认第一种，
# 会把模型完全按要求写出来的连续引用判成"没有标出处"。
_CITATION = re.compile(r"\[p\.\s*([0-9][0-9,，\s\-–—]*)\]")
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ]")


@dataclass(frozen=True)
class _Citation:
    """回答正文里的一处出处标注。一个 `[p.12,17]` 展开成两处——它声称了两件事。"""

    locator: int
    marker: str


@dataclass
class _Tally:
    """一条比率指标的分子分母。跨样本用**微平均**合并：每条样本的引用条数差得很远，
    先各自算比率再取平均，等于让只标了一处的样本和标了十处的样本一样重。
    """

    total: int = 0
    passed: int = 0

    def add(self, ok: bool) -> None:
        self.total += 1
        self.passed += int(ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "rate": round(self.passed / self.total, 6) if self.total else None,
        }


def parse_citations(response: str) -> list[_Citation]:
    """把回答里的 `[p.N]` 全部展开成逐条声明，保留出现顺序。"""

    citations: list[_Citation] = []
    for match in _CITATION.finditer(response or ""):
        marker = match.group(0)
        body = match.group(1).replace("，", ",")
        for chunk in body.split(","):
            piece = chunk.strip()
            if not piece:
                continue
            bounds = re.split(r"[\-–—]", piece)
            try:
                start = int(bounds[0])
                end = int(bounds[-1]) if len(bounds) > 1 and bounds[-1].strip() else start
            except ValueError:
                continue
            if end < start:
                start, end = end, start
            # 区间上限与 read_material 的展开上限同源：`[p.1-99999]` 不该在这里造出十万条。
            for locator in range(start, min(end, start + 32) + 1):
                citations.append(_Citation(locator=locator, marker=marker))
    return citations


def script_of(text: str) -> str:
    """一段文字属于哪一种书写体系。只分中英两档——分语言报告要的是"问答语言和原文
    语言对不对得上"，不是语种识别。"""

    return "zh" if _CJK.search(text or "") else "en"


def material_from_text(relative_path: str, content: str) -> Material | None:
    """用产品自己的切分器把 fixture 正文重建成材料。

    切分必须走 `units_from_sections`：locator 是这一层的全部意义所在，评测另切一份
    就等于在拿另一套页码去判模型的页码。解析不出内容返回 None，由调用方记成不可判，
    而不是当成"模型引错了"。
    """

    try:
        document = parse_markdown(content)
        units = units_from_sections(document)
    except (ValueError, ReadingError):
        return None
    if not units:
        return None
    path = Path(relative_path)
    return Material(
        path=path,
        material_id=hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
        filename=path.name,
        title=path.stem,
        unit="section",
        units=units,
        # 大纲只用于给模型看结构，三条指标一条也用不到它，所以不在这里凑一份。
        outline=(),
        parser="markdown",
        byte_size=len(content.encode("utf-8")),
    )


class _Materials:
    """把工具调用里的绝对路径映射回 fixture 里的那份正文。

    路径按后缀匹配：跑批把 fixture 写进一个临时工作区，trace 里留下的是那次运行的
    绝对路径，而 fixture 的键是工作区内的相对路径。
    """

    def __init__(self, files: Mapping[str, str], *, changed: Iterable[str] = ()) -> None:
        self._files = dict(files)
        self._changed = set(changed)
        self._cache: dict[str, Material | None] = {}
        self.unscorable: dict[str, str] = {}

    def resolve(self, raw_path: object) -> Material | None:
        path = str(raw_path or "")
        if not path:
            return None
        relative = self._match(path)
        if relative is None:
            self.unscorable.setdefault(path, "material_not_in_fixture")
            return None
        if relative in self._changed:
            # 运行期间文件被改过：现在这份正文已经不是模型当时读到的那一份，
            # 拿它判引文只会得出一个看起来精确、其实无意义的数字。
            self.unscorable.setdefault(relative, "material_changed_during_run")
            return None
        if relative not in self._cache:
            self._cache[relative] = material_from_text(relative, self._files[relative])
            if self._cache[relative] is None:
                self.unscorable.setdefault(relative, "material_not_parseable")
        return self._cache[relative]

    def _match(self, path: str) -> str | None:
        if path in self._files:
            return path
        normalized = path.replace("\\", "/")
        candidates = [key for key in self._files if normalized.endswith("/" + key)]
        # 最长匹配：`a/notes.md` 与 `notes.md` 同时存在时，短的那个会误命中。
        return max(candidates, key=len) if candidates else None


@dataclass
class _ReadingScore:
    read_before_claim: _Tally = field(default_factory=_Tally)
    quote_verifiability: _Tally = field(default_factory=_Tally)
    locator_accuracy: _Tally = field(default_factory=_Tally)
    quotes_by_script: dict[str, _Tally] = field(default_factory=dict)
    cross_language_quotes: _Tally = field(default_factory=_Tally)
    ungrounded_citations: list[int] = field(default_factory=list)
    unverified_quotes: list[dict[str, Any]] = field(default_factory=list)
    misplaced_quotes: list[dict[str, Any]] = field(default_factory=list)


def score_reading(
    *,
    response: str,
    trace: Sequence[Mapping[str, Any]],
    fixture_files: Mapping[str, str],
    changed_files: Iterable[str] = (),
) -> dict[str, Any] | None:
    """算一条样本的阅读三指标；这条样本没碰阅读工具就返回 None。

    返回 None 而不是一份全零的报告：没用阅读工具的办公任务不该把 `read_before_claim`
    的分母往下拉——那会让"这批任务里根本没有阅读题"和"阅读题全做砸了"长得一样。
    """

    used_reading_tool = any(str(call.get("name")) in READING_TOOLS for call in trace)
    if not used_reading_tool:
        return None

    materials = _Materials(fixture_files, changed=changed_files)
    score = _ReadingScore()
    read_locators: set[int] = set()
    material_scripts: list[str] = []

    for call in trace:
        name = str(call.get("name"))
        if name not in READING_TOOLS or call.get("status") != "ok":
            continue
        arguments = call.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        material = materials.resolve(arguments.get("path"))
        if material is None:
            continue
        material_scripts.append(script_of("".join(unit.text for unit in material.units[:3])))

        if name == "read_material":
            read_locators.update(_read_locators(call, arguments, material))
            continue
        if name not in _QUOTE_TOOLS:
            continue

        quote = str(arguments.get("quote") or "").strip()
        claimed = arguments.get("locator")
        if not quote or not isinstance(claimed, int):
            # `reader_goto` 允许不带 quote 做纯导航：没有断言，也就没有什么可校验。
            continue
        check = verify_quote(material, claimed, quote)
        script = script_of(quote)
        score.quote_verifiability.add(check.verified)
        score.quotes_by_script.setdefault(script, _Tally()).add(check.verified)
        if script != material_scripts[-1]:
            score.cross_language_quotes.add(check.verified)
        if not check.verified:
            score.unverified_quotes.append({"locator": claimed, "quote": quote[:120]})
            continue
        # 引文确实存在，才轮得到问"页码对不对"。
        score.locator_accuracy.add(not check.moved)
        if check.moved:
            score.misplaced_quotes.append(
                {"claimed": claimed, "found": check.found_locator, "quote": quote[:120]}
            )

    for citation in parse_citations(response):
        grounded = citation.locator in read_locators
        score.read_before_claim.add(grounded)
        if not grounded:
            score.ungrounded_citations.append(citation.locator)

    return {
        "read_before_claim": {
            **score.read_before_claim.to_dict(),
            "read_locators": sorted(read_locators),
            "ungrounded_citations": sorted(set(score.ungrounded_citations)),
        },
        "quote_verifiability": {
            **score.quote_verifiability.to_dict(),
            "by_script": {
                script: tally.to_dict() for script, tally in sorted(score.quotes_by_script.items())
            },
            "cross_language": score.cross_language_quotes.to_dict(),
            "unverified": score.unverified_quotes,
        },
        "locator_accuracy": {
            **score.locator_accuracy.to_dict(),
            "misplaced": score.misplaced_quotes,
        },
        "unscorable": [
            {"path": path, "reason": reason}
            for path, reason in sorted(materials.unscorable.items())
        ],
    }


def _read_locators(
    call: Mapping[str, Any],
    arguments: Mapping[str, Any],
    material: Material,
) -> list[int]:
    """这次 `read_material` 到底读到了哪些 locator。

    优先用工具返回的那一份：越界值在产品侧是被丢掉而不是夹逼的，照着入参算会把一个
    根本没读到的 locator 当成读过了。返回缺失（结果被截断）时才退回解析入参。
    """

    result = call.get("result")
    if isinstance(result, dict):
        locators = result.get("locators")
        if isinstance(locators, list):
            return [value for value in locators if isinstance(value, int)]
    try:
        return parse_locators(str(arguments.get("locators") or ""), material.unit_count)
    except ReadingError:
        return []


def merge_reading_scores(scores: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """把逐条样本的阅读分合并成一份跑批级报告（微平均）。"""

    if not scores:
        return None
    merged = {
        "items": len(scores),
        "read_before_claim": _merge_tally(scores, "read_before_claim"),
        "quote_verifiability": _merge_tally(scores, "quote_verifiability"),
        "locator_accuracy": _merge_tally(scores, "locator_accuracy"),
        "quote_verifiability_by_script": _merge_scripts(scores),
        "quote_verifiability_cross_language": _merge_nested(
            scores, "quote_verifiability", "cross_language"
        ),
        "unscorable_materials": sum(len(score.get("unscorable") or []) for score in scores),
    }
    return merged


def _rate(total: int, passed: int) -> dict[str, Any]:
    return {
        "total": total,
        "passed": passed,
        "rate": round(passed / total, 6) if total else None,
    }


def _merge_tally(scores: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    total = sum(int(score[key]["total"]) for score in scores)
    passed = sum(int(score[key]["passed"]) for score in scores)
    return _rate(total, passed)


def _merge_nested(scores: Sequence[Mapping[str, Any]], key: str, nested: str) -> dict[str, Any]:
    total = sum(int(score[key][nested]["total"]) for score in scores)
    passed = sum(int(score[key][nested]["passed"]) for score in scores)
    return _rate(total, passed)


def _merge_scripts(scores: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, list[int]] = {}
    for score in scores:
        for script, tally in (score["quote_verifiability"].get("by_script") or {}).items():
            row = buckets.setdefault(script, [0, 0])
            row[0] += int(tally["total"])
            row[1] += int(tally["passed"])
    return {script: _rate(total, passed) for script, (total, passed) in sorted(buckets.items())}


__all__ = [
    "READING_TOOLS",
    "material_from_text",
    "merge_reading_scores",
    "parse_citations",
    "score_reading",
    "script_of",
]
