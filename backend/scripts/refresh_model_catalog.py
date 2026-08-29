"""Validate and atomically refresh the deployment model catalog.

The source can be a checked-in/generated JSON file or an HTTPS metadata endpoint that returns
WorkPilot's versioned catalog schema.  Provider-specific discovery belongs in a small exporter
that emits this stable schema; routing never consumes a provider's unstable wire response.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from workpilot_ai.model_catalog import load_model_catalog


def _read_source(source: str) -> bytes:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        request = Request(source, headers={"Accept": "application/json"})
        with urlopen(request, timeout=30) as response:
            return response.read()
    return Path(source).read_bytes()


def refresh(source: str, destination: Path) -> None:
    raw = _read_source(source)
    document = json.loads(raw)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        load_model_catalog(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="catalog JSON path or HTTPS URL")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("../config/model-catalog.json"),
        help="destination catalog path (default assumes execution from backend/)",
    )
    arguments = parser.parse_args()
    refresh(arguments.source, arguments.output)


if __name__ == "__main__":
    main()
