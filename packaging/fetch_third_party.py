"""Download ExifTool and FFmpeg into ``packaging/third_party/bin/``.

Run before building the installer so the binaries get bundled. The build
machine needs internet; the resulting installer ships with these binaries
included so end-user installs are fully offline.

Versions are pinned via the URLs below — bump them deliberately.
"""

from __future__ import annotations

import io
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

EXIFTOOL_URL = "https://exiftool.org/exiftool-13.00_64.zip"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

THIS_DIR = Path(__file__).resolve().parent
BIN_DIR = THIS_DIR / "third_party" / "bin"


def _download(url: str) -> bytes:
    print(f"  GET {url}")
    with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 - build-time, trusted URLs
        return r.read()


def _fetch_exiftool() -> None:
    data = _download(EXIFTOOL_URL)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            base = Path(name).name.lower()
            if base.startswith("exiftool") and base.endswith(".exe"):
                target = BIN_DIR / "exiftool.exe"
                with z.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                print(f"  installed {target}")
                return
    raise RuntimeError("exiftool.exe not found in archive")


def _fetch_ffmpeg() -> None:
    data = _download(FFMPEG_URL)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            base = Path(name).name.lower()
            if base in ("ffmpeg.exe", "ffprobe.exe"):
                target = BIN_DIR / base
                with z.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                print(f"  installed {target}")


def main() -> int:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Fetching third-party binaries into {BIN_DIR}")
    try:
        _fetch_exiftool()
        _fetch_ffmpeg()
    except Exception as e:  # noqa: BLE001
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
