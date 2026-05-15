"""Stage 3 — analyze. Decode, hash perceptually, extract metadata, score.

For each pending file we run three independent analyses:
  * integrity (decode test, valid pixel rows, JPEG EOI marker)
  * perceptual hash (pHash + dHash from the decoded thumbnail)
  * metadata (EXIF / container; derives effective_date with fallback chain)

The decoded image from the integrity step is reused for perceptual hashing
so we never decode twice. Videos take a separate path through
``perceptual.hash_video`` (frame sampling via ffmpeg).
"""

from __future__ import annotations

import logging
from pathlib import Path

from dedupcollage.db import STAGE_ANALYZED, transaction, update_file
from dedupcollage.integrity import analyze_integrity, quality_score
from dedupcollage.metadata import extract_metadata
from dedupcollage.perceptual import hash_image, hash_video
from dedupcollage.utils import classify_kind

log = logging.getLogger(__name__)


def _analyze_image(path: Path) -> dict:
    integ = analyze_integrity(path)
    phash_b = dhash_b = None
    if integ.pil_image is not None:
        try:
            phash_b, dhash_b = hash_image(integ.pil_image)
        except Exception as e:  # noqa: BLE001
            log.warning("perceptual hash failed for %s: %s", path, e)

    meta = extract_metadata(path)

    size = int(path.stat().st_size) if path.exists() else 0
    score = quality_score(
        size=size,
        decode_ok=integ.decode_ok,
        valid_pixel_rows=integ.valid_pixel_rows,
        height=integ.height,
        has_full_exif=meta.has_full_exif,
        has_capture_time=meta.capture_time is not None,
        jpeg_eoi_ok=integ.jpeg_eoi_ok,
    )

    return {
        "decode_ok": 1 if integ.decode_ok else 0,
        "decode_error": integ.decode_error,
        "width": integ.width,
        "height": integ.height,
        "valid_pixel_rows": integ.valid_pixel_rows,
        "jpeg_eoi_ok": (None if integ.jpeg_eoi_ok is None else (1 if integ.jpeg_eoi_ok else 0)),
        "phash": phash_b,
        "dhash": dhash_b,
        "capture_time": meta.capture_time,
        "camera_make": meta.camera_make,
        "camera_model": meta.camera_model,
        "camera_serial": meta.camera_serial,
        "lens_model": meta.lens_model,
        "has_full_exif": 1 if meta.has_full_exif else 0,
        "effective_date": meta.effective_date,
        "date_source": meta.date_source,
        "quality_score": score,
        "last_stage_done": STAGE_ANALYZED,
    }


def _analyze_video(path: Path) -> dict:
    hashes = hash_video(path)
    meta = extract_metadata(path)
    size = int(path.stat().st_size) if path.exists() else 0
    # A video that we can decode at least one frame from gets a score similar
    # to an image with full EXIF. If hashing failed, score is lower.
    decode_ok = hashes is not None
    score = quality_score(
        size=size,
        decode_ok=decode_ok,
        valid_pixel_rows=None,
        height=None,
        has_full_exif=meta.has_full_exif,
        has_capture_time=meta.capture_time is not None,
        jpeg_eoi_ok=None,
    )
    return {
        "decode_ok": 1 if decode_ok else 0,
        "decode_error": None if decode_ok else "video frame extraction failed",
        "phash": hashes[0] if hashes else None,
        "dhash": hashes[1] if hashes else None,
        "capture_time": meta.capture_time,
        "camera_make": meta.camera_make,
        "camera_model": meta.camera_model,
        "camera_serial": meta.camera_serial,
        "lens_model": meta.lens_model,
        "has_full_exif": 1 if meta.has_full_exif else 0,
        "effective_date": meta.effective_date,
        "date_source": meta.date_source,
        "quality_score": score,
        "last_stage_done": STAGE_ANALYZED,
    }


def analyze_one(path: Path, kind: str | None = None) -> dict:
    """Analyze a single file. Returns DB-column dict suitable for ``update_file``."""
    p = Path(path)
    k = kind or classify_kind(p)
    if k == "video":
        return _analyze_video(p)
    return _analyze_image(p)


def run_analyze_stage(conn, *, governor=None, on_progress=None, limit: int | None = None) -> dict:
    """Process every file with ``last_stage_done < STAGE_ANALYZED``."""
    sql = "SELECT id, path, kind FROM files WHERE last_stage_done < ? AND decode_ok IS NULL ORDER BY path"
    params: tuple = (STAGE_ANALYZED,)
    if limit:
        sql += " LIMIT ?"
        params = (*params, limit)
    rows = list(conn.execute(sql, params))

    done = 0
    errors = 0
    batch_size = 50
    pending: list[tuple[dict, int]] = []

    def _flush() -> None:
        if not pending:
            return
        with transaction(conn):
            for fields, fid in pending:
                update_file(conn, fid, **fields)
        pending.clear()

    for row in rows:
        if governor:
            governor.acquire()
        fid = int(row["id"])
        try:
            fields = analyze_one(Path(row["path"]), kind=row["kind"])
        except Exception as e:  # noqa: BLE001
            log.warning("analyze failed for %s: %s", row["path"], e)
            fields = {
                "decode_ok": 0,
                "decode_error": str(e)[:200],
                "error": f"analyze:{e}",
                "last_stage_done": STAGE_ANALYZED,
            }
            errors += 1
        pending.append((fields, fid))
        done += 1
        if len(pending) >= batch_size:
            _flush()
            if on_progress:
                on_progress(done, len(rows))
    _flush()
    if on_progress:
        on_progress(done, len(rows))
    log.info("analyze: done=%d errors=%d", done, errors)
    return {"done": done, "errors": errors, "total": len(rows)}
