# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec per strutturare ride-the-api come server nativo Windows.

Strategia onedir (piu affidabile di onefile per data files e per il servizio
NSSM): prodotta una cartella dist/ride-the-api/ contenente ride-the-api.exe,
il runtime Python e i file di dati. Inno Setup impacchettera poi questa
cartella nell'installer .exe.

Nota: onnxruntime / numpy sono scaffolding morto (mai importati nel codice)
ed vengono esclusi per ridurre dimensione e tempi di build.
"""

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# Moduli usati "per nome" o dinamicamente che PyInstaller non rileva da solo.
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
]
hidden += collect_submodules("pydantic")
hidden += collect_submodules("sqlalchemy")

# Data files da incapsulare accanto all'exe: config di default. L'installer
# copiera il vero config in %ProgramData%\\ride-the-api (working dir del servizio).
datas = [
    ("config/config.yaml", "config"),
    ("certs", "certs"),
]


a = Analysis(
    ["core/server.py"],
    pathex=["."],
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
    console=True,        # server a console: visibile i log in primo piano
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