import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

_BOUNDARY_RE = re.compile(
    r"(?:[。！？!?]|\.(?=(?:\[S[1-9]\d*\])*(?:\s|$)))"
    r"(?:\[S[1-9]\d*\])*"
)


def cited_claims(answer: str, citation_id: str) -> str:
    label = f"[{citation_id}]"
    claims: list[str] = []
    for line in answer.splitlines():
        start = 0
        for match in _BOUNDARY_RE.finditer(line):
            claims.append(line[start : match.end()].strip())
            start = match.end()
        if tail := line[start:].strip():
            claims.append(tail)
    return "\n".join(claim for claim in claims if label in claim)


def export_review(reports: list[Path], output: Path) -> int:
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for path in reports:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        dataset = str(payload["dataset"])
        for item in payload["items"]:
            for citation in item["citations"]:
                key = (str(item["item_id"]), str(citation["citation_id"]))
                if key in seen:
                    raise ValueError(f"重复引用复核键: {key}")
                seen.add(key)
                rows.append(
                    {
                        "item_id": item["item_id"],
                        "dataset": dataset,
                        "category": item["category"],
                        "question": item["question"],
                        "gold_answer": item["gold_answer"],
                        "answer": item["answer"],
                        "citation_id": citation["citation_id"],
                        "cited_claims": cited_claims(
                            str(item["answer"]), str(citation["citation_id"])
                        ),
                        "citation_quote": citation["quote"],
                        "supported": "",
                        "reason": "",
                        "reviewer": "",
                        "reviewed_at": "",
                    }
                )
    fieldnames = [
        "item_id",
        "dataset",
        "category",
        "question",
        "gold_answer",
        "answer",
        "citation_id",
        "cited_claims",
        "citation_quote",
        "supported",
        "reason",
        "reviewer",
        "reviewed_at",
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出 M0 逐引用人工语义支撑复核表")
    parser.add_argument(
        "--generation-report", type=Path, action="append", required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    count = export_review(args.generation_report, args.output)
    print(f"review_rows={count} output={args.output}")


if __name__ == "__main__":
    main()
