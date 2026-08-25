"""把退役 PostgreSQL 中导出的 70 条 generation suite 迁到文件型 KB 锚点。

这是一次性、确定性的迁移器，不参与 nightly。输入是两份保留的历史报告和当前三个
文件型 KB；输出包含全部题目与稳定内容锚点，之后 runner 只读输出 suite。
"""

from __future__ import annotations

import argparse
import collections
import difflib
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.rag.kb.service import LocalKbService
from eval.kb_retrieval_runner import IndexCatalog, load_catalog

DEFAULT_REPAIR = Path(
    "eval/outputs/dev-benchmark-repair/20260817T174136Z-f83fefb64408.json"
)
DEFAULT_RETRIEVAL = Path(
    "eval/outputs/dev-suite-retrieval/"
    "20260817T025642.034728Z-nightly-baseline-20260817/heading/report.json"
)
DEFAULT_OUTPUT = Path("eval/suites/m1-dev-70-v2.json")
SOURCE_KBS = ("agent-engineering", "agent-research", "interview-prep")
MIN_FUZZY_MATCH = 0.60


@dataclass(frozen=True)
class _IndexedText:
    slug: str
    filename: str
    content_hash: str
    page_no: int | None
    text: str


def migrate(
    *,
    repair_path: Path,
    retrieval_path: Path,
    output_path: Path,
    kb_root: Path,
) -> dict[str, Any]:
    repair = _load_json(repair_path)
    retrieval = _load_json(retrieval_path)
    items = repair.get("items")
    if not isinstance(items, list) or len(items) != 70:
        raise ValueError("repair export 必须恰好包含 70 条 items")
    version_sources = _version_sources(retrieval)
    settings = Settings(knowledge_base_path=kb_root)
    service = LocalKbService(kb_root, settings=settings)
    catalogs = {
        slug: load_catalog(
            service,
            settings=settings,
            kb_slug=slug,
            kb_version_id=None,
        )
        for slug in SOURCE_KBS
    }
    indexed, source_meta = _indexed_texts(catalogs)
    corpus_names = sorted(
        {
            Path(str(hit["source_uri"])).name
            for item in retrieval.get("items", [])
            for hit in item.get("retrieved", [])
            if hit.get("source_uri")
        }
    )
    missing = set(corpus_names) - set(source_meta)
    if missing:
        raise ValueError(f"历史 corpus 在当前文件型 KB 中缺失: {sorted(missing)}")

    cache: dict[tuple[str, int, int, str], dict[str, object]] = {}
    migrated_items: list[dict[str, object]] = []
    scores: list[float] = []
    for raw in items:
        groups = []
        for group in raw.get("gold_evidence_groups", []):
            alternatives = []
            for span in group.get("alternatives", []):
                key = (
                    str(span["version_id"]),
                    int(span["char_start"]),
                    int(span["char_end"]),
                    str(span["quote"]),
                )
                migrated = cache.get(key)
                if migrated is None:
                    filename = version_sources.get(key[0])
                    if filename is None:
                        raise ValueError(f"旧 version_id 无法映射到文件: {key[0]}")
                    migrated = _migrate_span(
                        span,
                        filename=filename,
                        pages=indexed[filename],
                        content_hash=source_meta[filename]["content_hash"],
                    )
                    cache[key] = migrated
                alternatives.append(migrated)
                scores.append(float(migrated["migration_match_score"]))
            groups.append(
                {
                    "fact_id": str(group["fact_id"]),
                    "alternatives": alternatives,
                }
            )
        migrated_items.append(
            {
                "item_id": str(raw["id"]),
                "dataset_name": str(raw["dataset_name"]),
                "split": str(raw["dataset_split"]),
                "category": str(raw["category"]),
                "difficulty": int(raw["difficulty"]),
                "question": str(raw["question"]),
                "gold_answer": str(raw["gold_answer"]),
                "constraints": raw.get("constraints") or {
                    "must_include": [],
                    "must_not_include": [],
                },
                "temporal_ctx": raw.get("temporal_ctx"),
                "gold_evidence_groups": groups,
            }
        )

    payload: dict[str, Any] = {
        "schema_version": 2,
        "name": "m1-dev-70-v2",
        "description": (
            "M1 六类人工 dev grounded-generation 套件；由已复核的 PostgreSQL 导出"
            "机械迁移到当前文件型 KB 的 content hash、页码与字符区间，70 条全部内嵌。"
        ),
        "origin": "human",
        "item_count": len(migrated_items),
        "review": {
            "status": "approved",
            "reviewer": "行之",
            "reviewed_at": "2026-08-25T00:00:00+08:00",
            "note": "保留原人工题目、答案、约束与事实组；迁移后的内容锚点已逐条 fail-closed 校验。",
        },
        "migration": {
            "method": "normalized exact match, then bounded fuzzy re-anchor",
            "minimum_match_score": MIN_FUZZY_MATCH,
            "minimum_observed_match_score": min(scores),
            "repair_export_sha256": _sha256(repair_path),
            "retrieval_report_sha256": _sha256(retrieval_path),
            "source_kb_versions": {
                slug: catalogs[slug].version.version_id for slug in SOURCE_KBS
            },
        },
        "corpus": {
            "kb_slug": "m1-dev-70-v2",
            "derivation": "历史 70 条 retrieval 报告中所有 top-10 source_uri 的并集",
            "documents": [
                {
                    "filename": filename,
                    "content_hash": source_meta[filename]["content_hash"],
                    "source_kb": source_meta[filename]["source_kb"],
                }
                for filename in corpus_names
            ],
        },
        "items": migrated_items,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def _version_sources(report: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in report.get("items", []):
        for hit in item.get("retrieved", []):
            version_id = str(hit.get("version_id") or "")
            source = Path(str(hit.get("source_uri") or "")).name
            if not version_id or not source:
                continue
            previous = result.setdefault(version_id, source)
            if previous != source:
                raise ValueError(f"旧 version_id 映射到多个文件: {version_id}")
    return result


def _indexed_texts(
    catalogs: dict[str, IndexCatalog],
) -> tuple[dict[str, tuple[_IndexedText, ...]], dict[str, dict[str, str]]]:
    by_filename: dict[str, list[_IndexedText]] = collections.defaultdict(list)
    source_meta: dict[str, dict[str, str]] = {}
    for slug, catalog in catalogs.items():
        documents = {item.filename: item for item in catalog.manifest.documents}
        grouped: dict[tuple[str, int | None], list[Any]] = collections.defaultdict(list)
        for node in catalog.nodes:
            grouped[(node.filename, node.page_no)].append(node)
        for (filename, page_no), nodes in grouped.items():
            document = documents[filename]
            size = max(node.char_end for node in nodes)
            chars: list[str | None] = [None] * size
            for node in nodes:
                for offset, char in enumerate(node.text, start=node.char_start):
                    previous = chars[offset]
                    if previous is not None and previous != char:
                        raise ValueError(f"{filename} 索引重叠区正文不一致")
                    chars[offset] = char
            # SentenceSplitter 会把文档首尾空白 strip 掉，极少数空位只可能是空白。
            text = "".join(char if char is not None else " " for char in chars)
            by_filename[filename].append(
                _IndexedText(
                    slug=slug,
                    filename=filename,
                    content_hash=document.content_hash,
                    page_no=page_no,
                    text=text,
                )
            )
            meta = {"content_hash": document.content_hash, "source_kb": slug}
            previous_meta = source_meta.setdefault(filename, meta)
            if previous_meta != meta:
                raise ValueError(f"同名文件在多个 KB 中内容不一致: {filename}")
    return (
        {name: tuple(sorted(pages, key=lambda page: page.page_no or 0)) for name, pages in by_filename.items()},
        source_meta,
    )


def _migrate_span(
    raw: dict[str, Any],
    *,
    filename: str,
    pages: tuple[_IndexedText, ...],
    content_hash: str,
) -> dict[str, object]:
    historical = _literal_quote(str(raw["quote"]))
    normalized, _ = _normalize_with_map(historical)
    exact: list[tuple[_IndexedText, int, int]] = []
    for page in pages:
        current, offsets = _normalize_with_map(page.text)
        start = 0
        while normalized and (found := current.find(normalized, start)) >= 0:
            exact.append(
                (page, offsets[found], offsets[found + len(normalized) - 1] + 1)
            )
            start = found + 1
    if exact:
        old_start = int(raw["char_start"])
        page, start, end = min(exact, key=lambda hit: abs(hit[1] - old_start))
        score = 1.0
    else:
        candidates = [(_best_partial(historical, page.text), page) for page in pages]
        (score, start, end), page = max(candidates, key=lambda item: item[0][0])
        if score < MIN_FUZZY_MATCH:
            raise ValueError(
                f"{filename} gold 无法可靠迁移: score={score:.3f}, "
                f"quote={historical[:100]!r}"
            )
    start, end = _trim_range(page.text, start, end)
    quote = page.text[start:end]
    if not quote:
        raise ValueError(f"{filename} 迁移得到空 quote")
    return {
        "content_hash": content_hash,
        "filename": filename,
        "page_no": page.page_no,
        "char_start": start,
        "char_end": end,
        "quote": quote,
        "migration_match_score": round(score, 6),
    }


def _literal_quote(value: str) -> str:
    # 旧的早期题目对正则元字符做过 re.escape；后期人工题是原文字面量。
    return re.sub(r"\\([.\-_*+?{}\[\]()^$|\\])", r"\1", value).replace("\\ ", " ")


def _normalize_with_map(value: str) -> tuple[str, list[int]]:
    normalized: list[str] = []
    offsets: list[int] = []
    # 逐原字符规范化，offset 才仍指向原文。整串 NFKC 会把 ``ﬁ`` 展成两个字符，
    # 后面的索引全部右移，迁移出的 char_end 可能越出原文。
    for index, original in enumerate(value):
        for char in unicodedata.normalize("NFKC", original).casefold():
            if char.isalnum():
                normalized.append(char)
                offsets.append(index)
    return "".join(normalized), offsets


def _best_partial(needle: str, haystack: str) -> tuple[float, int, int]:
    query, _ = _normalize_with_map(needle)
    text, offsets = _normalize_with_map(haystack)
    if not query or not text:
        return 0.0, 0, 0
    starts: collections.Counter[int] = collections.Counter()
    width = min(24, max(8, len(query) // 10))
    step = max(width, len(query) // 20)
    for query_index in range(0, max(1, len(query) - width + 1), step):
        shingle = query[query_index : query_index + width]
        found = text.find(shingle)
        while found >= 0:
            starts[max(0, found - query_index)] += 1
            found = text.find(shingle, found + 1)
    candidate_starts = [start for start, _count in starts.most_common(12)]
    if not candidate_starts:
        for query_index in range(0, max(1, len(query) - 7), max(8, len(query) // 20)):
            found = text.find(query[query_index : query_index + 8])
            if found >= 0:
                candidate_starts.append(max(0, found - query_index))
    best = (0.0, 0, 0)
    tolerance = max(10, len(query) // 10)
    for candidate in set(candidate_starts):
        for shift in (-tolerance, 0, tolerance):
            start = max(0, candidate + shift)
            for factor in (0.8, 1.0, 1.2):
                end = min(len(text), start + max(1, int(len(query) * factor)))
                score = difflib.SequenceMatcher(
                    None, query, text[start:end], autojunk=False
                ).ratio()
                if score > best[0]:
                    best = (score, offsets[start], offsets[end - 1] + 1)
    return best


def _trim_range(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} 根节点必须是对象")
    return value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="迁移 generation suite 到文件型 KB v2")
    parser.add_argument("--repair", type=Path, default=DEFAULT_REPAIR)
    parser.add_argument("--retrieval", type=Path, default=DEFAULT_RETRIEVAL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--kb-root", type=Path, default=Path("~/.workpilot/kb"))
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    payload = migrate(
        repair_path=args.repair,
        retrieval_path=args.retrieval,
        output_path=args.output,
        kb_root=args.kb_root.expanduser(),
    )
    print(
        f"已写 {args.output}: {payload['item_count']} 条, "
        f"corpus={len(payload['corpus']['documents'])} 篇"
    )


if __name__ == "__main__":
    main()
