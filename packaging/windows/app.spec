# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Pioneer DJ Link (Windows).

One-folder build (faster startup, smaller installer delta on update) producing
    dist/PioneerDJLink/PioneerDJLink.exe
which Inno Setup then wraps into a single Setup.exe.
"""

from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# Spec files are executed with __file__ unset; resolve repo root from CWD.
ROOT = Path.cwd()

hidden = []
hidden += collect_submodules("pyqtgraph")
hidden += collect_submodules("PyQt6")

datas = []
datas += collect_data_files("pyqtgraph")

block_cipher = None


a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Heavyweight optional deps that aren't actually imported.
        "aubio",
        "sounddevice",
        "rtmidi",
        # Trim the most common large unused stdlib + Qt modules.
        "tkinter",
        "PyQt6.QtWebEngine",
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.Qt3DCore",
        "PyQt6.Qt3DRender",
        "PyQt6.QtBluetooth",
        "PyQt6.QtPositioning",
        "PyQt6.QtQml",
        "PyQt6.QtQuick",
        "PyQt6.QtMultimedia",
        "PyQt6.QtPdf",
    ],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    exclude_binaries=False,
    name="PioneerDJLink",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "packaging" / "windows" / "app.ico")
        if (ROOT / "packaging" / "windows" / "app.ico").exists() else None,
    runtime_tmpdir=None,
    onefile=True,
)
