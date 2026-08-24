from pathlib import Path

from app.cowork.office_preview import _sanitize_quicklook_html


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
