"""Load the curated visual kits bundled with the pptx Skill.

The source PPTX files are retained as assets and integrity anchors.  WorkPilot's fixed
renderer consumes safe, editable design tokens extracted from those templates instead
of importing arbitrary masters or macros into a generated deck.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

CATALOG_PATH = Path(__file__).resolve().parents[1] / "assets" / "templates" / "catalog.json"
ASSET_ROOT = CATALOG_PATH.parent


class VisualKitError(ValueError):
    """The visual-kit catalog or a selected template asset is invalid."""


@lru_cache(maxsize=1)
def load_visual_kit_catalog() -> dict[str, Any]:
    try:
        payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise VisualKitError(f"PPT 视觉套件目录不可读：{error}") from error
    if payload.get("schema_version") != 1 or not isinstance(payload.get("kits"), dict):
        raise VisualKitError("PPT 视觉套件目录格式无效")
    return cast(dict[str, Any], payload)


def visual_kit_ids() -> tuple[str, ...]:
    return tuple(sorted(load_visual_kit_catalog()["kits"]))


def get_visual_kit(name: str) -> dict[str, Any]:
    kit = load_visual_kit_catalog()["kits"].get(name)
    if not isinstance(kit, dict):
        choices = ", ".join(visual_kit_ids())
        raise VisualKitError(f"未知 PPT 视觉套件 {name!r}；可选：{choices}")
    return kit


@lru_cache(maxsize=32)
def verify_visual_kit_asset(name: str) -> Path | None:
    kit = get_visual_kit(name)
    raw_file = kit.get("template_file")
    expected = kit.get("source_sha256")
    if raw_file is None:
        return None
    if not isinstance(raw_file, str) or not isinstance(expected, str):
        raise VisualKitError(f"PPT 视觉套件 {name!r} 缺少模板完整性信息")
    path = ASSET_ROOT / raw_file
    if not path.is_file() or path.is_symlink():
        raise VisualKitError(f"PPT 视觉套件 {name!r} 的模板资源缺失")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise VisualKitError(f"PPT 视觉套件 {name!r} 的模板资源已变化")
    return path


def visual_kit_family(name: str) -> str:
    family = get_visual_kit(name).get("family")
    if not isinstance(family, str) or not family:
        raise VisualKitError(f"PPT 视觉套件 {name!r} 缺少 family")
    return family


def visual_kit_theme(name: str) -> dict[str, str]:
    verify_visual_kit_asset(name)
    theme = get_visual_kit(name).get("theme")
    if not isinstance(theme, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in theme.items()
    ):
        raise VisualKitError(f"PPT 视觉套件 {name!r} 缺少合法 theme")
    return dict(theme)


__all__ = [
    "ASSET_ROOT",
    "CATALOG_PATH",
    "VisualKitError",
    "get_visual_kit",
    "load_visual_kit_catalog",
    "verify_visual_kit_asset",
    "visual_kit_family",
    "visual_kit_ids",
    "visual_kit_theme",
]
