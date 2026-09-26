# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build definition for LinkedIn Enricher.

Build it on Windows with:   build.bat
(You cannot cross-build a Windows .exe from macOS or Linux.)

Two deliberate choices:

* A one-FOLDER build, not one-file. A one-file exe unpacks to a temp directory on
  every launch, which is slower, upsets antivirus more often, and has bitten
  people whose antivirus quarantines the unpack. The folder build starts faster
  and is far easier to diagnose.

* Chromium is NOT bundled. It is ~150 MB and pinning it inside the exe has always
  been fragile. The app downloads it on first launch into the user's own
  %LOCALAPPDATA%\\ms-playwright, where it survives upgrades of this app.
"""

import sys
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hidden = [
    # keyring finds its backends through entry points, which PyInstaller cannot
    # see. Without these the password store silently disappears.
    "keyring.backends.Windows",
    "keyring.backends.macOS",
    "keyring.backends.SecretService",
    "keyring.backends.chainer",
    "keyring.backends.fail",
    "win32ctypes.core",
    "win32ctypes.core.cffi",
    "win32ctypes.core.ctypes",
] + collect_submodules("openpyxl")

# Qt modules we never use; leaving them out roughly halves the download.
excluded_qt = [
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtNfc", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning", "PySide6.QtQml", "PySide6.QtQuick",
    "PySide6.QtQuick3D", "PySide6.QtQuickControls2", "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSpatialAudio", "PySide6.QtSql",
    "PySide6.QtStateMachine", "PySide6.QtSvgWidgets", "PySide6.QtTest",
    "PySide6.QtTextToSpeech", "PySide6.QtWebChannel", "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets",
]

a = Analysis(
    ["li_app.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=excluded_qt + ["tkinter", "matplotlib", "numpy", "pandas", "pytest",
                            "IPython", "PIL"],
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
    name="LinkedInEnricher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # UPX compression trips a lot of antivirus engines
    console=False,          # a windowed app: no console box behind it
    disable_windowed_traceback=False,
    icon="app.ico" if sys.platform == "win32" else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LinkedInEnricher",
)
