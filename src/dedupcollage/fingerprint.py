"""Stages 1 and 2 — file content hashing.

Stage 1 (``quickhash``): an xxhash64 over the first 64 KB + last 64 KB + file
size. Roughly one HDD seek and 128 KB of read per file. Cheap. Files with
unique Stage 1 fingerprints cannot be exact duplicates and skip Stage 2.

Stage 2 (``fullhash``): a streaming SHA-256, but ONLY for files whose
Stage 1 fingerprint collides with another file's. The SHA-256 of unique
Stage 1 fingerprints is never computed because nothing useful would
come of it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import struct
from collections.abc import Iterable
from pathlib import Path

import xxhash

from dedupcollage.db import (
    STAGE_FULLHASHED,
    STAGE_QUICKHASHED,
    transaction,
)

log = logging.getLogger(__name__)

_HEAD_TAIL_BYTES = 64 * 1024
_FULL_HASH_CHUNK = 1 * 1024 * 1024


def quick_hash(path: Path | str, size: int) -> str:
    """64-bit xxhash of head + tail + size for ``path``.

    For files smaller than 2 * _HEAD_TAIL_BYTES we hash the whole content
    (it's still cheap) and append the size.
    """
    h = xxhash.xxh64()
    p = Path(path)
    with open(p, "rb") as f:
        if size <= 2 * _HEAD_TAIL_BYTES:
            h.update(f.read())
        else:
            h.update(f.read(_HEAD_TAIL_BYTES))
            f.seek(-_HEAD_TAIL_BYTES, os.SEEK_END)
            h.update(f.read(_HEAD_TAIL_BYTES))
    h.update(struct.pack("<Q", size))
    return h.hexdigest()


def full_sha256(path: Path | str) -> str:
    """Streaming SHA-256 of the file at ``path``."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_FULL_HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def run_quickhash_stage(conn, *, governor=None, on_progress=None) -> dict[str, int]:
    """Compute quick_hash for every file that doesn't have one yet."""
    todo = list(conn.execute(
        "SELECT id, path, size FROM files WHERE quick_hash IS NULL ORDER BY path"
    ))
    done = 0
    errors = 0
    pending_updates: list[tuple[str, int, int]] = []
    batch_size = 200
    for row in todo:
        if governor:
            governor.acquire()
        try:
            qh = quick_hash(row["path"], int(row["size"]))
            pending_updates.append((qh, STAGE_QUICKHASHED, int(row["id"])))
        except OSError as e:
            log.warning("quick_hash failed for %s: %s", row["path"], e)
            conn.execute(
                "UPDATE files SET error = ?, last_stage_done = ? WHERE id = ?",
                (f"quickhash:{e}", STAGE_QUICKHASHED, int(row["id"])),
            )
            errors += 1
        done += 1
        if len(pending_updates) >= batch_size:
            _flush_quick(conn, pending_updates)
            if on_progress:
                on_progress(done, len(todo))
    _flush_quick(conn, pending_updates)
    if on_progress:
        on_progress(done, len(todo))
    log.info("quickhash: done=%d errors=%d", done, errors)
    return {"done": done, "errors": errors, "total": len(todo)}


def _flush_quick(conn, batch: list[tuple[str, int, int]]) -> None:
    if not batch:
        return
    with transaction(conn):
        conn.executemany(
            "UPDATE files SET quick_hash = ?, last_stage_done = MAX(last_stage_done, ?) WHERE id = ?",
            batch,
        )
    batch.clear()


def _quickhash_collision_ids(conn) -> Iterable[int]:
    """File ids whose quick_hash collides with at least one other file."""
    cur = conn.execute("""
        SELECT id FROM files
        WHERE quick_hash IS NOT NULL
          AND sha256 IS NULL
          AND quick_hash IN (
              SELECT quick_hash FROM files
              WHERE quick_hash IS NOT NULL
              GROUP BY quick_hash
              HAVING COUNT(*) > 1
          )
        ORDER BY quick_hash, path
    """)
    return [int(row["id"]) for row in cur]


def run_fullhash_stage(conn, *, governor=None, on_progress=None) -> dict[str, int]:
    """SHA-256 every file in a Stage 1 collision group that doesn't have a sha256 yet."""
    ids = list(_quickhash_collision_ids(conn))
    # Also flag non-collision files as fully done at this stage (no full hash needed).
    with transaction(conn):
        conn.execute(
            "UPDATE files SET last_stage_done = MAX(last_stage_done, ?) "
            "WHERE quick_hash IS NOT NULL AND sha256 IS NULL "
            "AND id NOT IN (SELECT id FROM files WHERE quick_hash IN ("
            "  SELECT quick_hash FROM files WHERE quick_hash IS NOT NULL "
            "  GROUP BY quick_hash HAVING COUNT(*) > 1))",
            (STAGE_FULLHASHED,),
        )

    done = 0
    errors = 0
    pending: list[tuple[str, int, int]] = []
    batch_size = 50
    for file_id in ids:
        row = conn.execute("SELECT path FROM files WHERE id = ?", (file_id,)).fetchone()
        if row is None:
            continue
        if governor:
            governor.acquire()
        try:
            sha = full_sha256(row["path"])
            pending.append((sha, STAGE_FULLHASHED, file_id))
        except OSError as e:
            log.warning("full_sha256 failed for %s: %s", row["path"], e)
            conn.execute(
                "UPDATE files SET error = ?, last_stage_done = ? WHERE id = ?",
                (f"fullhash:{e}", STAGE_FULLHASHED, file_id),
            )
            errors += 1
        done += 1
        if len(pending) >= batch_size:
            _flush_full(conn, pending)
            if on_progress:
                on_progress(done, len(ids))
    _flush_full(conn, pending)
    if on_progress:
        on_progress(done, len(ids))
    log.info("fullhash: done=%d errors=%d (of %d collision files)", done, errors, len(ids))
    return {"done": done, "errors": errors, "total": len(ids)}


def _flush_full(conn, batch: list[tuple[str, int, int]]) -> None:
    if not batch:
        return
    with transaction(conn):
        conn.executemany(
            "UPDATE files SET sha256 = ?, last_stage_done = MAX(last_stage_done, ?) WHERE id = ?",
            batch,
        )
    batch.clear()
