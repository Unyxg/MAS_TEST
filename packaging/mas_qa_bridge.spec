# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec - build on Windows:  pyinstaller --noconfirm packaging/mas_qa_bridge.spec
# Output: dist/MAS-QA-Bridge/MAS-QA-Bridge.exe (+ _internal/). config.yaml goes next to the .exe.
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

ROOT = Path(SPECPATH).parent

# Some libraries read their own package metadata at import time
# (imageio: importlib.metadata.version("imageio")) - bundle it.
datas = []
for dist in ("imageio", "mss", "pillow", "numpy", "requests", "PyYAML", "psutil", "opencv-python"):
    try:
        datas += copy_metadata(dist)
    except Exception:
        pass

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        # imageio picks its writer plugin by name at runtime
        "imageio.plugins.pillow",
        "imageio.config.plugins",
        "imageio.config.extensions",
        # imported lazily inside functions
        "main_window",
        "demo_data",
        "self_test",
        "win32cred",
        "win32timezone",
        "win32com.shell.shell",
        "win32com.shell.shellcon",
    ],
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MAS-QA-Bridge",
    console=False,  # GUI app; --self-test writes self_test.log next to the exe
    upx=False,  # UPX-packed binaries trigger more antivirus false positives
)
coll = COLLECT(exe, a.binaries, a.datas, name="MAS-QA-Bridge", upx=False)
