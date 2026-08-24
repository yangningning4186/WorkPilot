# ruff: noqa: F821
"""PyInstaller recipe for the single WorkPilot backend sidecar executable."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

backend_root = Path.cwd()
project_root = backend_root.parent

datas = [(str(project_root / "config" / "routing.yaml"), "config")]
datas += collect_data_files(
    "app.cowork.skills",
    includes=["builtin/**/*"],
)
binaries = []
hiddenimports = [
    "app.main",
    "aiosqlite",
    "sqlalchemy.dialects.sqlite.aiosqlite",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

# 这些包通过 entry point、插件目录或字符串模块名加载，静态 import 图看不全。只收产品
# 实际依赖的 LlamaIndex 子发行版，不把整个 llama-index 生态一起塞进安装包。
for package in (
    "llama_index.core",
    "llama_index.embeddings.openai",
    "llama_index.retrievers.bm25",
    "llama_index.vector_stores.faiss",
    "playwright",
):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

hiddenimports += collect_submodules("workpilot_ai")
hiddenimports += collect_submodules("workpilot_telemetry")

analysis = Analysis(
    [str(backend_root / "app" / "desktop_sidecar.py")],
    pathex=[
        str(backend_root),
        str(backend_root / "packages" / "workpilot-ai" / "src"),
        str(backend_root / "packages" / "workpilot-telemetry" / "src"),
    ],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["mypy", "pytest", "ruff", "IPython", "jupyter"],
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
    name="workpilot-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
