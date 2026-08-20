"""交付物扩展名与可信 MIME 的单一白名单。"""

from typing import Final

TEXT_ARTIFACT_MIME_BY_SUFFIX: Final[dict[str, str]] = {
    ".csv": "text/csv",
    ".htm": "text/html",
    ".html": "text/html",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".markdown": "text/markdown",
    ".md": "text/markdown",
    ".rst": "text/plain",
    ".toml": "application/toml",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}
TEXT_ARTIFACT_SUFFIXES: Final[frozenset[str]] = frozenset(TEXT_ARTIFACT_MIME_BY_SUFFIX)
