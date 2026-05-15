# PyInstaller spec for DedupCollage.
# Run with: pyinstaller --clean packaging/dedupcollage.spec

import os
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH).parent if "SPECPATH" in dir() else Path(".").resolve()
THIRD_PARTY = ROOT / "packaging" / "third_party" / "bin"

datas = []
if THIRD_PARTY.exists():
    for binary in THIRD_PARTY.glob("*"):
        datas.append((str(binary), "bin"))

a = Analysis(
    [str(ROOT / "src" / "dedupcollage" / "gui" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "dedupcollage", "dedupcollage.cli", "dedupcollage.gui",
        "dedupcollage.scan", "dedupcollage.fingerprint",
        "dedupcollage.perceptual", "dedupcollage.integrity",
        "dedupcollage.metadata", "dedupcollage.analyze",
        "dedupcollage.cluster", "dedupcollage.select",
        "dedupcollage.organize", "dedupcollage.governor",
        "PIL.Image", "PIL.ExifTags", "pillow_heif", "rawpy",
        "xxhash", "imagehash",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test"],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="dedupcollage",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / "packaging" / "icon.ico") if (ROOT / "packaging" / "icon.ico").exists() else None,
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, name="dedupcollage",
)
