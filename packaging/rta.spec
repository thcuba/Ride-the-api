"""
PyInstaller spec for ride-the-api as a native server (cross-platform).

Strategy onedir (more reliable than onefile for data files and for the NSSM
service): produces dist/ride-the-api/ containing the `ride-the-api` binary
(`.exe` on Windows), the Python runtime and data files. The entry point is
gui_main.py: on Windows it opens a small tkinter control panel (headless with
`--service` for NSSM), on Linux/macOS the same binary falls back to headless.
Inno Setup then wraps the onedir into a Windows installer.

Note: onnxruntime / numpy are dead scaffolding (never imported) and are
excluded to keep the bundle small and the build fast.
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Repo root = parent of the directory containing this spec (packaging/).
REPO_ROOT = Path(SPECPATH).parent
os.chdir(REPO_ROOT)

# Modules used by name / dynamically that PyInstaller cannot detect alone.
hidden = [
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "pydantic",
    "pydantic_settings",
    "sqlalchemy.dialects.sqlite",
    # Async SQLite driver imported by name inside SQLAlchemy (dialect
    # aiosqlite); PyInstaller's module graph misses it otherwise.
    "aiosqlite",
    "watchfiles",
    "yaml",
]
hidden += collect_submodules("pydantic")
hidden += collect_submodules("sqlalchemy")

# Data files bundled next to the binary:
#   - config/config.yaml            -> default config; the gui_main launcher
#     seeds it into a writable data dir on first run (the installer no longer
#     needs to copy it manually).
#   - webui/                        -> dashboard.html + patterns.html.
#   - core/pattern_db/schemas/*.json -> JSON Schemas used by the Pattern DB
#     validator (loaded via Path(__file__) relative to the module dir).
_datas = [
    (str(REPO_ROOT / "config/config.yaml"), "config"),
    (str(REPO_ROOT / "webui"), "webui"),
    (str(REPO_ROOT / "core/pattern_db/schemas"), "core/pattern_db/schemas"),
]

# `certs/` is generated at runtime by CertManager; bundle the (possibly empty)
# dir only if it exists at build time to avoid a PyInstaller data-source error.
if (REPO_ROOT / "certs").is_dir():
    _datas.append((str(REPO_ROOT / "certs"), "certs"))

datas = _datas


a = Analysis(
    [str(REPO_ROOT / "gui_main.py")],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "onnxruntime",
        "onnxruntime_gpu",
        "numpy",
        "asyncpg",
        "psycopg2",
        "matplotlib",
        "PIL",
        "tests",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ride-the-api",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,    # no console window: gui_main.py owns the UI (tkinter or headless)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ride-the-api",
)
