"""Path resolution for bundled binaries, the SQLite database, and log files.

This module hides three sources of paths:
  1. PyInstaller bundle dir (``sys._MEIPASS``) when running from the installed .exe.
  2. Vendored ``packaging/third_party/bin`` when running from source.
  3. The system ``PATH`` as a last resort.

It also exposes ``app_data_dir()`` for runtime artifacts (the SQLite DB, logs,
the manifest CSV). On Windows that resolves to ``%LOCALAPPDATA%\\DedupCollage``.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_DIR_NAME = "DedupCollage"


def _frozen_internal_dir() -> Path | None:
    """If running from a PyInstaller bundle, return its data dir; else None."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def _source_third_party_dir() -> Path:
    """Vendored binaries when running from source — ``packaging/third_party/bin``."""
    return Path(__file__).resolve().parents[2] / "packaging" / "third_party" / "bin"


def find_bundled_binary(name: str) -> Path | None:
    """Locate an executable by name; first bundled, then vendored, then PATH.

    On Windows we look for ``{name}.exe`` if the bare name isn't found.
    Returns None if the binary cannot be located.
    """
    candidates: list[Path] = []

    frozen = _frozen_internal_dir()
    if frozen is not None:
        candidates.append(frozen / "bin" / name)

    candidates.append(_source_third_party_dir() / name)

    if sys.platform == "win32":
        candidates += [c.with_suffix(".exe") for c in list(candidates)]

    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c

    on_path = shutil.which(name)
    if on_path:
        return Path(on_path)
    return None


def exiftool_path() -> Path | None:
    """Path to the ExifTool executable, or None if not found."""
    return find_bundled_binary("exiftool")


def ffmpeg_path() -> Path | None:
    """Path to the FFmpeg executable, or None if not found."""
    return find_bundled_binary("ffmpeg")


def ffprobe_path() -> Path | None:
    """Path to the FFprobe executable, or None if not found."""
    return find_bundled_binary("ffprobe")


def app_data_dir() -> Path:
    """Per-user state directory. Created on first call."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = Path(base) / APP_DIR_NAME
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    else:
        d = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_db_path() -> Path:
    """Default location of the SQLite index."""
    return app_data_dir() / "dedupcollage.db"


def log_dir() -> Path:
    """Directory for log files. Created on first call."""
    d = app_data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_default_root() -> Path:
    """Fallback output root if the user doesn't specify one."""
    return app_data_dir() / "output"
