"""单份材料内的 locator 寻址全文匹配。

这是阅读模式里**替掉检索**的那一块，而不是检索的降级实现。一份材料至多几百个
unit，线性扫一遍是毫秒级；更重要的是——**每条命中天然带着自己的 locator**。模型
之所以能引"第 12 页"，是因为它就是在第 12 页上搜到的，而不是事后被提示词要求补一
个页码。向量检索给不了这个性质：它返回的是 chunk，chunk 到页码要再映射一次。

匹配刻意分三层，从便宜到宽松，并且**把命中在哪一层告诉调用方**，模型据此区分逐字
命中和模糊命中：

1. ``exact``——原样，忽略大小写。
2. ``normalised``——折叠空白、软化标点。从 PDF 里复制出来的句子在原文里往往被硬
   换行截断过，这一层让它仍然能匹配上。
3. ``terms``——按命中词数排序。自然语言问句在前两层必然全落空，没有这一层就等于
   告诉模型"文档里没有"，而实际只是问法和原文用词不同。

全是 ``(locator, text)`` 上的纯函数：不碰 store、不做 I/O、不读配置，所以三层升级
的行为可以直接写单测，而不用先造一个 PDF。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.cowork.reading.models import SearchHit

MatchMode = Literal["exact", "normalised", "terms"]

DEFAULT_LIMIT = 8
MAX_LIMIT = 20
SNIPPET_RADIUS = 140

# "同一段文字"在 PDF 里和在聊天消息里会不一样的那些字符：任意空白串，以及会被转写
# 的标点族（弯直引号、连接号与破折号、中英文逗号句号）。
_WS_RUN = re.compile(r"\s+")
_SOFT_PUNCT = re.compile(r"[‘’“”–—\-_'\"`,，、;；:：.。!！?？()（）\[\]【】]")
# str 模式下 `\w` 是 Unicode-aware 的，CJK 会被当成词字符留在一起。
_TERM_SPLIT = re.compile(r"\W+", re.UNICODE)
# 短到没有区分度的词（英文虚词、单个汉字助词）直接丢掉，否则会稀释第三层的排序。
_MIN_TERM_LEN = 2
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ]")


@dataclass(frozen=True)
class SearchResult:
    """一次查询的命中，外加它是哪一层匹配出来的。"""

    hits: tuple[SearchHit, ...]
    mode: MatchMode | None
    truncated: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.hits

    @property
    def is_loose(self) -> bool:
        """第三层命中。调用方必须据此提示模型"读过再引"。"""
        return self.mode == "terms"

    def to_dict(self) -> dict[str, object]:
        return {
            "hits": [hit.to_dict() for hit in self.hits],
            "mode": self.mode,
            "truncated": self.truncated,
        }


def normalise(text: str) -> str:
    """折叠空白并软化标点，用于容忍性比较。"""
    return _WS_RUN.sub(" ", _SOFT_PUNCT.sub("", text or "")).strip().lower()


def terms_of(query: str) -> tuple[str, ...]:
    """把查询切成排序用的词，丢掉没有信号的碎片。

    CJK 没有词边界，只靠 `\\W+` 会切出一个巨大的整串，第三层就退化成精确匹配了。
    所以长度 ≥3 的 CJK 串展开成重叠 bigram——这正是让中文问句能部分命中的原因。
    """
    raw = [term for term in _TERM_SPLIT.split((query or "").lower()) if term]
    expanded: list[str] = []
    for term in raw:
        if len(term) >= 3 and _CJK.search(term):
            expanded.extend(term[index : index + 2] for index in range(len(term) - 1))
        elif len(term) >= _MIN_TERM_LEN:
            expanded.append(term)
    # 去重但保留首次出现顺序，重叠 bigram 不会互相顶掉。
    return tuple(dict.fromkeys(expanded))


def search_units(
    units: Sequence[tuple[int, str]],
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
) -> SearchResult:
    """在 units 里找 query，逐层升级。"""
    needle = (query or "").strip()
    if not needle:
        return SearchResult(hits=(), mode=None)

    bounded = max(1, min(int(limit), MAX_LIMIT))

    for mode in ("exact", "normalised"):
        # 多取一条只为判断是否被截断，不进入返回结果。
        hits = _literal_hits(units, needle, mode=mode, limit=bounded + 1)
        if hits:
            return SearchResult(
                hits=tuple(hits[:bounded]),
                mode=mode,
                truncated=len(hits) > bounded,
            )

    ranked = _term_hits(units, needle, limit=bounded + 1)
    if not ranked:
        return SearchResult(hits=(), mode=None)
    return SearchResult(
        hits=tuple(ranked[:bounded]),
        mode="terms",
        truncated=len(ranked) > bounded,
    )


def locate_quote(text: str, quote: str) -> int:
    """quote 在 text 里的字符偏移，找不到返回 -1。

    用于在把阅读器拽到某一页之前，确认模型给的引文真的在那一页上。找不到时退回
    归一化比较——丢了一个换行的引文仍然算数；此时返回的偏移指向归一化后的位置，
    足够回答"这句话是真的吗"，但**不能**拿去算几何。
    """
    if not text or not quote:
        return -1
    direct = text.lower().find(quote.lower())
    if direct >= 0:
        return direct
    return normalise(text).find(normalise(quote))


def _literal_hits(
    units: Sequence[tuple[int, str]],
    needle: str,
    *,
    mode: str,
    limit: int,
) -> list[SearchHit]:
    hits: list[SearchHit] = []
    target = needle.lower() if mode == "exact" else normalise(needle)
    if not target:
        return hits
    for locator, text in units:
        haystack = text.lower() if mode == "exact" else normalise(text)
        position = haystack.find(target)
        if position < 0:
            continue
        # 窗口永远从**原文**里切：归一化后的偏移没法索引原文，所以那一层改用
        # 首个命中词在原文中的位置定位，而不是拿归一化偏移硬套。
        anchor = position if mode == "exact" else _first_term_offset(text, needle)
        hits.append(
            SearchHit(
                locator=locator,
                snippet=_snippet(text, anchor, len(needle)),
                offset=max(0, anchor),
                match=needle,
            )
        )
        if len(hits) >= limit:
            break
    return hits


def _term_hits(
    units: Sequence[tuple[int, str]],
    needle: str,
    *,
    limit: int,
) -> list[SearchHit]:
    query_terms = terms_of(needle)
    if not query_terms:
        return []
    scored: list[tuple[int, int, SearchHit]] = []
    for locator, text in units:
        lowered = text.lower()
        present = [term for term in query_terms if term in lowered]
        if not present:
            continue
        anchor = min(lowered.find(term) for term in present)
        scored.append(
            (
                len(present),
                locator,
                SearchHit(
                    locator=locator,
                    snippet=_snippet(text, anchor, len(present[0])),
                    offset=max(0, anchor),
                    match=" ".join(present),
                ),
            )
        )
    # 命中词多的排前面；同分时按 locator 升序，结果读起来是文档顺序而不是随机的。
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [hit for _, _, hit in scored[:limit]]


def _first_term_offset(text: str, needle: str) -> int:
    lowered = text.lower()
    for term in terms_of(needle):
        found = lowered.find(term)
        if found >= 0:
            return found
    return 0


def _snippet(text: str, offset: int, match_len: int) -> str:
    """offset 附近的一行窗口，被截断的一侧补省略号。"""
    if not text:
        return ""
    start = max(0, offset - SNIPPET_RADIUS)
    end = min(len(text), offset + max(1, match_len) + SNIPPET_RADIUS)
    window = _WS_RUN.sub(" ", text[start:end]).strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{window}{suffix}"


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MatchMode",
    "SearchResult",
    "locate_quote",
    "normalise",
    "search_units",
    "terms_of",
]
