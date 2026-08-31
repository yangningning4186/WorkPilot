import os
from pathlib import Path

from app.cowork.artifact_renderers import render_candidate
from app.cowork.artifact_renderers.contracts import PresentationSpec, SlideSpec
from app.cowork.office_preview import _sanitize_quicklook_html, render_office_preview
from app.cowork.tools import _prune_model_preview_cache


def test_quicklook_preview_removes_active_content_and_embeds_local_images(
    tmp_path: Path,
) -> None:
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    raw = """
    <html><body onload="steal()">
      <meta http-equiv="refresh" content="0;url=https://evil.example">
      <script>window.top.document.body.textContent='owned'</script>
      <iframe src="https://evil.example"></iframe>
      <img src="image.png"><img src="../../secret.txt">
      <a href="javascript:steal()">open</a>
    </body></html>
    """

    rendered = _sanitize_quicklook_html(raw, tmp_path.resolve())

    assert "<script" not in rendered.casefold()
    assert "onload" not in rendered.casefold()
    assert "<iframe" not in rendered.casefold()
    assert "http-equiv" not in rendered.casefold()
    assert "javascript:" not in rendered.casefold()
    assert "data:image/png;base64," in rendered
    assert "../../secret.txt" not in rendered


def test_pptx_preview_uses_bundled_per_slide_renderer(tmp_path: Path) -> None:
    source = tmp_path / "deck.pptx"
    render_candidate(
        PresentationSpec(
            title="预览",
            slides=[
                SlideSpec(id="s1", layout="title", title="第一页"),
                SlideSpec(id="s2", layout="statement", title="第二页", body="稳定渲染"),
            ],
        ),
        source,
    )

    preview = render_office_preview(
        source,
        cache_root=tmp_path / "cache",
        timeout_s=5,
        max_source_bytes=10 * 1024 * 1024,
        max_cache_entries=2,
    )

    assert preview is not None
    assert preview.mode == "workpilot-pptx"
    html = preview.path.read_text(encoding="utf-8")
    assert html.count("data:image/png;base64,") == 2
    assert "第 1 页" in html and "第 2 页" in html


def test_model_presentation_cache_is_bounded_per_run_and_by_bytes(tmp_path: Path) -> None:
    root = tmp_path / "model-presentation"
    run_root = root / "run-1"
    run_root.mkdir(parents=True)
    leaves: list[Path] = []
    for index in range(3):
        leaf = run_root / f"spec-{index}"
        leaf.mkdir()
        (leaf / "candidate.pptx").write_bytes(bytes([index]) * 16)
        os.utime(leaf, ns=(index + 1, index + 1))
        leaves.append(leaf)

    _prune_model_preview_cache(
        root,
        keep=leaves[-1],
        entries_per_run=2,
        max_bytes=64,
    )

    assert leaves[-1].is_dir()
    assert not leaves[0].exists()
    assert len([item for item in run_root.iterdir() if item.is_dir()]) == 2

    _prune_model_preview_cache(
        root,
        keep=leaves[-1],
        entries_per_run=2,
        max_bytes=16,
    )

    assert leaves[-1].is_dir()
    assert len([item for item in run_root.iterdir() if item.is_dir()]) == 1
