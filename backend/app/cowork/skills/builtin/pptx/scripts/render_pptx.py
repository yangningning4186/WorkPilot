"""Thin WorkPilot adapter for the Skill-owned PptxGenJS renderer.

Python owns the validated PresentationSpec contract, local-image sanitation, and process
boundary. Slide construction and OOXML generation are exclusively implemented by the
JavaScript renderer in ``scripts/pptxgenjs``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from app.cowork.artifact_renderers.contracts import PresentationSpec
from app.cowork.artifact_renderers.image_assets import compatible_raster_path
from app.cowork.skills.builtin.pptx.scripts.visual_kits import (
    verify_visual_kit_asset,
    visual_kit_family,
    visual_kit_theme,
)

RENDERER_ROOT = Path(__file__).resolve().with_name("pptxgenjs")
RENDERER_SCRIPT = RENDERER_ROOT / "render_pptx.cjs"
RENDERER_PACKAGE = RENDERER_ROOT / "package.json"
RENDER_TIMEOUT_SECONDS = 120


class PptxGenJSRenderError(RuntimeError):
    """The fixed PptxGenJS renderer is unavailable or failed."""


def _resolved_spec(spec: PresentationSpec) -> PresentationSpec:
    """Apply a selected visual kit without overwriting explicit theme fields."""

    verify_visual_kit_asset(spec.visual_kit)
    kit_theme = visual_kit_theme(spec.visual_kit)
    explicit = {name: getattr(spec.theme, name) for name in spec.theme.model_fields_set}
    theme = spec.theme.model_copy(update={**kit_theme, **explicit})
    return spec.model_copy(update={"theme": theme})


def _renderer_command() -> tuple[str, ...]:
    fixed = os.environ.get("WORKPILOT_PPTX_RENDERER", "").strip()
    if fixed:
        executable = Path(fixed).expanduser()
        if not executable.is_absolute() or executable.is_symlink() or not executable.is_file():
            raise PptxGenJSRenderError("WORKPILOT_PPTX_RENDERER 必须指向内置的绝对普通文件")
        return (str(executable),)

    configured_node = os.environ.get("WORKPILOT_NODE", "").strip()
    if configured_node:
        node = Path(configured_node).expanduser()
        if not node.is_absolute() or node.is_symlink() or not node.is_file():
            raise PptxGenJSRenderError("WORKPILOT_NODE 必须指向绝对普通文件")
        node_path = str(node)
    else:
        discovered = shutil.which("node")
        if discovered is None:
            raise PptxGenJSRenderError(
                "PptxGenJS 运行时不可用：开发态需要 Node.js，发布态需要内置 Renderer"
            )
        node_path = discovered

    if not RENDERER_SCRIPT.is_file() or not RENDERER_PACKAGE.is_file():
        raise PptxGenJSRenderError("pptx Skill 缺少 PptxGenJS renderer 源文件")
    dependency = RENDERER_ROOT / "node_modules" / "pptxgenjs" / "package.json"
    if not dependency.is_file():
        raise PptxGenJSRenderError(
            "pptx Skill 的 PptxGenJS 依赖未安装；在 scripts/pptxgenjs 运行 npm ci"
        )
    return node_path, str(RENDERER_SCRIPT)


def _rasterized_payload(spec: PresentationSpec, stack: ExitStack) -> dict[str, Any]:
    payload = spec.model_dump(mode="json")
    for slide in payload["slides"]:
        image_path = slide.get("image_path")
        if image_path:
            compatible = stack.enter_context(
                compatible_raster_path(Path(image_path), max_dimension=1800)
            )
            slide["image_path"] = str(compatible)
        canvas = slide.get("canvas")
        if not isinstance(canvas, dict):
            continue
        for element in canvas.get("elements", []):
            if not isinstance(element, dict) or element.get("type") != "image":
                continue
            compatible = stack.enter_context(
                compatible_raster_path(Path(str(element["image_path"])), max_dimension=1800)
            )
            element["image_path"] = str(compatible)
    return {
        "schema_version": 1,
        "renderer": "pptxgenjs",
        "visual_family": visual_kit_family(spec.visual_kit),
        "spec": payload,
    }


def render_presentation(spec: PresentationSpec, target: Path) -> None:
    """Compile a validated PresentationSpec using PptxGenJS."""

    if target.suffix.casefold() != ".pptx":
        raise ValueError("PptxGenJS Renderer 只能写入 .pptx")
    if target.exists() and target.is_symlink():
        raise PptxGenJSRenderError("PptxGenJS Renderer 拒绝覆盖符号链接")
    target.parent.mkdir(parents=True, exist_ok=True)
    resolved = _resolved_spec(spec)
    command = _renderer_command()
    with ExitStack() as stack:
        payload = _rasterized_payload(resolved, stack)
        with tempfile.TemporaryDirectory(
            prefix="workpilot-pptxgenjs-",
            dir=target.parent,
        ) as raw_directory:
            input_path = Path(raw_directory) / "presentation-spec.json"
            input_path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            try:
                completed = subprocess.run(
                    [*command, str(input_path), str(target)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=RENDER_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise PptxGenJSRenderError(f"PptxGenJS Renderer 无法完成：{error}") from error
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "unknown error").strip()[-4_000:]
        raise PptxGenJSRenderError(f"PptxGenJS Renderer 失败：{details}")
    if not target.is_file() or target.stat().st_size < 1_000:
        raise PptxGenJSRenderError("PptxGenJS Renderer 未生成有效 PPTX 文件")


__all__ = ["PptxGenJSRenderError", "render_presentation"]
