"""WorkPilot 自有的确定性 Artifact Renderer 公共契约。"""

from pathlib import Path

from app.cowork.artifact_renderers.contracts import (
    ArtifactEnvelope,
    ArtifactPlan,
    ArtifactSpec,
    DocumentSpec,
    HtmlReportSpec,
    PdfSpec,
    PresentationSpec,
    WorkbookSpec,
)


def render_candidate(spec: ArtifactSpec, target: Path) -> None:
    """惰性进入格式分发，避免导入某个格式脚本时反向初始化整条 pipeline。"""

    from app.cowork.artifact_renderers.pipeline import render_candidate as dispatch

    dispatch(spec, target)

__all__ = [
    "ArtifactEnvelope",
    "ArtifactPlan",
    "ArtifactSpec",
    "DocumentSpec",
    "HtmlReportSpec",
    "PdfSpec",
    "PresentationSpec",
    "WorkbookSpec",
    "render_candidate",
]
