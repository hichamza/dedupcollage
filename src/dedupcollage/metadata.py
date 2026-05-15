"""EXIF and container metadata extraction; effective_date with fallback chain.

We shell out to ``exiftool -json`` once per file (it speaks every camera
dialect) and parse a small fixed set of fields. When ExifTool is unavailable
we fall back to Pillow's EXIF for JPEG/TIFF.

The fallback chain for the date we organize by:
    1. ``DateTimeOriginal`` from EXIF                        → 'exif_taken'
    2. ``CreateDate`` from EXIF / ``creation_time`` (video)  → 'exif_created'
    3. ``file mtime``                                        → 'mtime'

The cluster takes the *best* source across its members, so a single sibling
with EXIF rescues the whole cluster from the ``-s`` save-date fallback.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dedupcollage._paths import exiftool_path

log = logging.getLogger(__name__)

DATE_SOURCE_TAKEN = "exif_taken"
DATE_SOURCE_CREATED = "exif_created"
DATE_SOURCE_MTIME = "mtime"

DATE_SOURCE_RANK = {
    DATE_SOURCE_TAKEN: 3,
    DATE_SOURCE_CREATED: 2,
    DATE_SOURCE_MTIME: 1,
    None: 0,
}


@dataclass
class FileMetadata:
    capture_time: str | None       # ISO 8601 if available (no timezone)
    camera_make: str | None
    camera_model: str | None
    camera_serial: str | None
    lens_model: str | None
    has_full_exif: bool
    effective_date: str            # always set
    date_source: str               # always set


_EXIF_DATE_RE = re.compile(r"^(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def _parse_exif_datetime(s: str | None) -> str | None:
    if not s:
        return None
    m = _EXIF_DATE_RE.match(s.strip())
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}:{m.group(6)}"


def _mtime_iso(path: Path) -> str:
    ts = path.stat().st_mtime
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _exiftool_dict(path: Path) -> dict | None:
    et = exiftool_path()
    if et is None:
        return None
    try:
        proc = subprocess.run(
            [str(et), "-json", "-n", "-fast", "-DateTimeOriginal", "-CreateDate", "-MediaCreateDate",
             "-Make", "-Model", "-SerialNumber", "-InternalSerialNumber", "-LensModel",
             "-GPSLatitude", "-GPSLongitude", str(path)],
            check=True, capture_output=True, text=True, timeout=15,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        log.debug("exiftool failed for %s: %s", path, e)
        return None
    try:
        data = json.loads(proc.stdout)
        return data[0] if isinstance(data, list) and data else None
    except (json.JSONDecodeError, IndexError):
        return None


def _pillow_exif(path: Path) -> dict | None:
    """Tiny fallback when ExifTool isn't installed. JPEG/TIFF only."""
    try:
        from PIL import ExifTags, Image

        img = Image.open(path)
        exif = img.getexif()
        if not exif:
            return None
        inverse = {v: k for k, v in ExifTags.TAGS.items()}
        out: dict = {}
        if inverse.get("DateTimeOriginal") in exif:
            out["DateTimeOriginal"] = exif[inverse["DateTimeOriginal"]]
        if inverse.get("DateTime") in exif:
            out["CreateDate"] = exif[inverse["DateTime"]]
        if inverse.get("Make") in exif:
            out["Make"] = exif[inverse["Make"]]
        if inverse.get("Model") in exif:
            out["Model"] = exif[inverse["Model"]]
        if inverse.get("LensModel") in exif:
            out["LensModel"] = exif[inverse["LensModel"]]
        return out
    except Exception:  # noqa: BLE001
        return None


def extract_metadata(path: Path) -> FileMetadata:
    """Pull metadata and compute effective_date for ``path``."""
    raw = _exiftool_dict(path) or _pillow_exif(path) or {}

    taken = _parse_exif_datetime(raw.get("DateTimeOriginal"))
    created = _parse_exif_datetime(
        raw.get("CreateDate") or raw.get("MediaCreateDate")
    )

    if taken:
        effective, source = taken, DATE_SOURCE_TAKEN
    elif created:
        effective, source = created, DATE_SOURCE_CREATED
    else:
        effective, source = _mtime_iso(path), DATE_SOURCE_MTIME

    capture = taken or created
    make = (raw.get("Make") or "").strip() or None
    model = (raw.get("Model") or "").strip() or None
    serial = (raw.get("SerialNumber") or raw.get("InternalSerialNumber") or "").strip() or None
    lens = (raw.get("LensModel") or "").strip() or None
    has_full = bool(capture and make and model)

    return FileMetadata(
        capture_time=capture,
        camera_make=make,
        camera_model=model,
        camera_serial=str(serial) if serial else None,
        lens_model=lens,
        has_full_exif=has_full,
        effective_date=effective,
        date_source=source,
    )


def better_source(a: str | None, b: str | None) -> str | None:
    """Return the more authoritative of two date_source labels."""
    return a if DATE_SOURCE_RANK[a] >= DATE_SOURCE_RANK[b] else b


def cluster_effective_date(rows: list) -> tuple[str | None, str | None]:
    """Across cluster members, pick the (effective_date, date_source) with the best rank.

    ``rows`` is an iterable of sqlite3.Row objects with ``effective_date`` and
    ``date_source`` columns.
    """
    best_rank = 0
    best_date: str | None = None
    best_source: str | None = None
    for r in rows:
        rank = DATE_SOURCE_RANK.get(r["date_source"], 0)
        if rank > best_rank and r["effective_date"]:
            best_rank = rank
            best_date = r["effective_date"]
            best_source = r["date_source"]
    return best_date, best_source
