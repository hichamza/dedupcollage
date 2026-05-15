"""Generic helpers — logging setup, byte/time formatting, filename hygiene."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from dedupcollage._paths import log_dir

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

T = TypeVar("T")


def setup_logging(level: str = "INFO", to_file: bool = True) -> None:
    """Configure the root logger. Idempotent.

    Logs always go to stderr; if ``to_file`` is True they also go to
    ``{app_data_dir}/logs/dedupcollage.log`` with daily rotation by date in name.
    """
    root = logging.getLogger()
    if getattr(root, "_dc_configured", False):
        return
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if to_file:
        log_path = log_dir() / f"dedupcollage-{datetime.now().strftime('%Y-%m-%d')}.log"
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)

    root._dc_configured = True  # type: ignore[attr-defined]


def iso_now() -> str:
    """UTC timestamp in ISO 8601, suitable for SQLite TEXT columns."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_bytes(n: int | float) -> str:
    """Human-friendly byte size: 1234567 -> '1.18 MB'."""
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.2f} {unit}"
    return f"{n:.2f} PB"


_INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TRAILING_DOTS_SPACES = re.compile(r"[. ]+$")


def safe_filename(s: str, replacement: str = "-") -> str:
    """Sanitize a string for use as a Windows path component.

    Replaces reserved characters, collapses whitespace, and strips trailing
    dots/spaces (which Windows treats specially). Returns 'unknown' if the
    input is empty or becomes empty after cleaning.
    """
    cleaned = _INVALID_PATH_CHARS.sub(replacement, s or "").strip()
    cleaned = _TRAILING_DOTS_SPACES.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(rf"{re.escape(replacement)}+", replacement, cleaned).strip(replacement)
    return cleaned or "unknown"


def chunked(iterable: Iterable[T], size: int) -> Iterator[list[T]]:
    """Yield successive lists of length ``size`` from ``iterable``."""
    batch: list[T] = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def classify_kind(path: Path) -> str:
    """Classify a file by extension: 'image', 'raw', 'video', or 'other'."""
    ext = path.suffix.lower().lstrip(".")
    if ext in ("jpg", "jpeg", "png", "heic", "heif", "webp", "bmp", "gif", "tiff", "tif"):
        return "image"
    if ext in ("cr2", "cr3", "nef", "arw", "dng", "raf", "rw2", "orf", "pef", "srw"):
        return "raw"
    if ext in ("mp4", "mov", "m4v", "avi", "mkv", "mts", "3gp"):
        return "video"
    return "other"


DEFAULT_EXTENSIONS: tuple[str, ...] = (
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".bmp", ".gif", ".tiff", ".tif",
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".rw2", ".orf", ".pef", ".srw",
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".3gp",
)
