"""Per-file integrity scoring.

Each file passes through three checks during the analyze stage:
  * decode test — does Pillow/HEIC/RAW open and fully load the pixels?
  * JPEG EOI marker — for JPEGs only, are the last two bytes ``FF D9``?
  * valid pixel rows — how far down the image can we scan before variance
    drops to near zero (signature of gray-bar corruption)?

These feed a continuous ``quality_score``. Higher is better.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile

log = logging.getLogger(__name__)

# Decode partially-corrupt JPEGs without raising — we still want a score.
ImageFile.LOAD_TRUNCATED_IMAGES = True

# Variance below this is considered "dead row" (uniform color, ie. corruption).
_DEAD_ROW_VARIANCE = 8.0


@dataclass
class IntegrityResult:
    decode_ok: bool
    decode_error: str | None
    width: int | None
    height: int | None
    valid_pixel_rows: int | None
    jpeg_eoi_ok: bool | None
    pil_image: Image.Image | None  # the decoded image, kept for perceptual hashing


def _open_image(path: Path) -> Image.Image:
    """Open ``path`` with Pillow, transparently handling HEIC and RAW."""
    suffix = path.suffix.lower()
    if suffix in (".heic", ".heif"):
        try:
            import pillow_heif  # noqa: F401
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
    if suffix in (".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".rw2", ".orf", ".pef", ".srw"):
        try:
            import rawpy

            with rawpy.imread(str(path)) as raw:
                rgb = raw.postprocess(use_camera_wb=True, output_bps=8)
            return Image.fromarray(rgb)
        except Exception as e:  # noqa: BLE001
            raise OSError(f"raw decode failed: {e}") from e
    img = Image.open(path)
    img.load()  # force decode now so truncation errors surface here
    return img


def _jpeg_eoi_ok(path: Path) -> bool | None:
    """For JPEGs, verify the last two bytes are FF D9. None for non-JPEG."""
    suffix = path.suffix.lower()
    if suffix not in (".jpg", ".jpeg"):
        return None
    try:
        with open(path, "rb") as f:
            try:
                f.seek(-2, 2)
            except OSError:
                return False
            tail = f.read(2)
            return tail == b"\xff\xd9"
    except OSError:
        return False


def _valid_pixel_rows(img: Image.Image) -> int | None:
    """Count rows from the top that have non-trivial variance.

    For a clean image this equals ``height``. For a file with gray-bar
    corruption near the bottom this is the row where the gray bar starts.
    """
    try:
        gray = img.convert("L")
        arr = np.asarray(gray, dtype=np.uint8)
    except Exception:  # noqa: BLE001
        return None
    if arr.ndim != 2 or arr.size == 0:
        return None
    variances = arr.var(axis=1)
    # Walk from the bottom up; the first row with non-degenerate variance
    # is the last "valid" row.
    h = arr.shape[0]
    for i in range(h - 1, -1, -1):
        if variances[i] > _DEAD_ROW_VARIANCE:
            return int(i + 1)
    return 0


def analyze_integrity(path: Path) -> IntegrityResult:
    """Run the decode + corruption checks for one file."""
    try:
        img = _open_image(path)
    except Exception as e:  # noqa: BLE001
        return IntegrityResult(
            decode_ok=False,
            decode_error=str(e)[:200],
            width=None, height=None, valid_pixel_rows=None,
            jpeg_eoi_ok=_jpeg_eoi_ok(path),
            pil_image=None,
        )
    w, h = img.size
    rows = _valid_pixel_rows(img)
    return IntegrityResult(
        decode_ok=True,
        decode_error=None,
        width=int(w),
        height=int(h),
        valid_pixel_rows=rows,
        jpeg_eoi_ok=_jpeg_eoi_ok(path),
        pil_image=img,
    )


def quality_score(
    *,
    size: int,
    decode_ok: bool,
    valid_pixel_rows: int | None,
    height: int | None,
    has_full_exif: bool,
    has_capture_time: bool,
    jpeg_eoi_ok: bool | None,
) -> float:
    """Continuous score; higher is better. See ARCHITECTURE for rationale."""
    if not decode_ok:
        # An undecodable file is still worth *something* if its bytes
        # might donate metadata to a sibling, but it can never win.
        return float(size) / 1_000_000.0

    rows_fraction = 1.0
    if valid_pixel_rows is not None and height:
        rows_fraction = max(0.0, min(1.0, valid_pixel_rows / max(height, 1)))

    score = 10_000.0
    score += rows_fraction * 5_000.0
    score += min(size / 1024.0, 50_000.0) / 50.0  # bigger = better, capped at ~1000 pts
    if has_full_exif:
        score += 500.0
    if has_capture_time:
        score += 200.0
    if jpeg_eoi_ok is False:
        score -= 1_000.0
    return score
