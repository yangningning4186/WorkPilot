# ruff: noqa: F821
"""PyInstaller recipe for WorkPilot's self-contained artifact Python runtime."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

backend_root = Path.cwd()
datas = []
binaries = []
hiddenimports = []

# 模型脚本会动态 import 这些包，静态 import 图不足以收齐字体、模板、二进制扩展与子模块。
for package in (
    "docx",
    "lxml",
    "openpyxl",
    "PIL",
    "pymupdf",
    "pptx",
    "reportlab",
    "xlsxwriter",
):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

analysis = Analysis(
    [str(backend_root / "app" / "artifact_python_runtime.py")],
    pathex=[str(backend_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "fastapi",
        "httpx",
        "llama_index",
        "mcp",
        "mypy",
        "playwright",
        "pytest",
        "ruff",
        "sqlalchemy",
        "uvicorn",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="workpilot-artifact-python",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
