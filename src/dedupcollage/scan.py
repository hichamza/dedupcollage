"""Stage 0 — recursive walk of a source root into the SQLite index.

We do NOT open or read the file content here; only ``size`` and ``mtime`` are
recorded from the directory listing. This is the cheap stage — it should
complete in minutes on the full 1 TB so the user can see the file inventory
before committing to the long stages.

Drive identity is captured here too: the volume serial number is read from
the filesystem (Windows) so the file is later locatable even after a
remount with a different drive letter.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from dedupcollage.db import (
    indexed_relpaths,
    insert_scanned_files,
    mark_dir_scanned,
    scanned_relpaths,
    transaction,
    upsert_drive,
)
from dedupcollage.discovery import DirNode, build_tree
from dedupcollage.utils import DEFAULT_EXTENSIONS, classify_kind

log = logging.getLogger(__name__)

_BATCH_SIZE = 1000

# Directory names shown as a muted hint label in the GUI tree. NOT used
# to skip or flag — selection is evidence-based (see discovery.py).
NAME_HINTS = {
    "node_modules": "cache", ".bun": "cache", ".git": "cache",
    ".cache": "cache", "__pycache__": "cache", ".venv": "cache",
    "venv": "cache", "appdata": "system",
    "system volume information": "system",
}
_HEARTBEAT_SECS = 0.5
_HEARTBEAT_EVERY = 2000


def name_hint(dirname: str) -> str | None:
    return NAME_HINTS.get(dirname.lower())


def _walk(
    root: Path, *, prune: Callable[[str, str], bool] | None = None
) -> Iterator[tuple[str, list[str], dict[str, int]]]:
    """Yield ``(dirpath, filenames, counts)`` per directory under ``root``.

    ``prune(dirpath, dirname) -> bool`` returns True to skip descending
    into a subdirectory. ``counts`` is the same dict object on every
    iteration; read ``counts["inaccessible"]`` after the loop for the
    final tally of directories that errored (each is logged and skipped).
    """
    counts = {"inaccessible": 0}

    def _onerr(e: OSError) -> None:
        counts["inaccessible"] += 1
        log.warning("walk: %s", e)

    for dirpath, dirnames, filenames in os.walk(root, onerror=_onerr):
        kept = []
        for d in dirnames:
            if d.startswith(("$", "_replaced")):
                continue
            if prune is not None and prune(dirpath, d):
                continue
            kept.append(d)
        dirnames[:] = kept
        yield dirpath, filenames, counts


def get_volume_serial(path: Path) -> str:
    """Return a stable identifier for the volume containing ``path``.

    On Windows we use ``GetVolumeInformationW``. On other platforms we fall
    back to ``stat().st_dev`` formatted as hex. The result is opaque — it
    just needs to be stable across remounts.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            drive = os.path.splitdrive(os.path.abspath(str(path)))[0] + "\\"
            vol_name = ctypes.create_unicode_buffer(256)
            fs_name = ctypes.create_unicode_buffer(256)
            serial = wintypes.DWORD()
            max_comp = wintypes.DWORD()
            flags = wintypes.DWORD()
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(drive),
                vol_name, 256,
                ctypes.byref(serial),
                ctypes.byref(max_comp),
                ctypes.byref(flags),
                fs_name, 256,
            )
            if ok:
                return f"{serial.value:08X}"
        except OSError as e:
            log.warning("could not read volume serial for %s: %s", path, e)

    try:
        return f"dev-{os.stat(path).st_dev:x}"
    except OSError:
        return "unknown"


def get_volume_label(path: Path) -> str | None:
    """Best-effort volume label, Windows only."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        drive = os.path.splitdrive(os.path.abspath(str(path)))[0] + "\\"
        vol_name = ctypes.create_unicode_buffer(256)
        fs_name = ctypes.create_unicode_buffer(256)
        serial = wintypes.DWORD()
        max_comp = wintypes.DWORD()
        flags = wintypes.DWORD()
        if ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive),
            vol_name, 256,
            ctypes.byref(serial),
            ctypes.byref(max_comp),
            ctypes.byref(flags),
            fs_name, 256,
        ):
            return vol_name.value or None
    except OSError:
        return None
    return None


def iter_candidate_files(
    root: Path, extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
) -> Iterator[Path]:
    """Yield media files under ``root`` (suffix in ``extensions``)."""
    ext_lower = tuple(e.lower() for e in extensions)
    for dirpath, filenames, _counts in _walk(root):
        for name in filenames:
            if name.lower().endswith(ext_lower):
                yield Path(dirpath) / name


def _heartbeat_gate(state: dict[str, float]) -> bool:
    now = time.monotonic()
    if state["examined"] - state["last_n"] >= _HEARTBEAT_EVERY or \
       now - state["last_t"] >= _HEARTBEAT_SECS:
        state["last_n"] = state["examined"]
        state["last_t"] = now
        return True
    return False


def discover(
    root: Path,
    *,
    extensions: tuple[str, ...] = DEFAULT_EXTENSIONS,
    on_progress=None,
) -> DirNode:
    """Count-only walk: build the directory tree with media counts.

    No ``stat``/hashing. Emits ``on_progress(examined, 0)`` and
    ``log.debug`` on a throttled heartbeat (total unknown -> 0).
    """
    root = Path(root).resolve()
    ext_lower = tuple(e.lower() for e in extensions)
    rows: list[tuple[str, int, int]] = []
    state = {"examined": 0, "last_n": 0, "last_t": time.monotonic()}

    for dirpath, filenames, _counts in _walk(root):
        try:
            rel = str(Path(dirpath).relative_to(root)).replace("\\", "/")
        except ValueError:
            rel = ""  # symlink/junction escaping root -> attribute to root
        rel = "" if rel == "." else rel
        own_total = len(filenames)
        own_media = sum(1 for n in filenames if n.lower().endswith(ext_lower))
        rows.append((rel, own_total, own_media))
        state["examined"] += own_total
        if _heartbeat_gate(state):
            log.debug("scan: discover dir=%s examined=%d", dirpath, state["examined"])
            if on_progress:
                on_progress(state["examined"], 0)

    if on_progress:
        on_progress(state["examined"], 0)
    return build_tree(rows)


def _mark_completed(
    conn, drive_id: int, walked: list[str],
    tally: dict[str, list[int]], include_pruned: set[str],
) -> None:
    """Write ``scanned_dirs`` rows post-order, O(number of dirs).

    A directory is marked complete only if **no** ``include``-pruned
    directory lies within its subtree — such a subtree is intentionally
    left without a completion row so a later resume still re-walks it.
    Resume-pruned children do NOT block completion: they already carry
    their own completion rows from a prior run. Subtree counts are
    aggregated single-pass from each dir's immediate children
    (deepest-first; root processed last), avoiding the O(D^2) scan.
    """
    def _depth(r: str) -> int:
        return 0 if r == "" else r.count("/") + 1

    order = sorted(set(walked), key=_depth, reverse=True)
    children: dict[str, list[str]] = {}
    for rel in set(walked):
        if rel == "":
            continue
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        children.setdefault(parent, []).append(rel)

    subtree: dict[str, list[int]] = {}
    for rel in order:
        own = tally.get(rel, [0, 0])
        t, m = own[0], own[1]
        for c in children.get(rel, ()):
            ct, cm = subtree.get(c, [0, 0])
            t += ct
            m += cm
        subtree[rel] = [t, m]

    def _blocked(rel: str) -> bool:
        if not include_pruned:
            return False
        if rel == "":
            return True
        return any(pr == rel or pr.startswith(rel + "/") for pr in include_pruned)

    with transaction(conn):
        for rel in order:
            if _blocked(rel):
                continue
            t, m = subtree.get(rel, [0, 0])
            mark_dir_scanned(
                conn, drive_id=drive_id, relpath=rel,
                file_count=t, media_count=m,
            )


def index(
    conn,
    source: Path,
    *,
    label: str | None = None,
    extensions: tuple[str, ...] = DEFAULT_EXTENSIONS,
    include=None,
    resume: bool = False,
    skip_indexed: bool = False,
    force: bool = False,
    on_progress=None,
) -> dict[str, int]:
    """Index media files under ``source`` into the DB.

    ``include(relpath) -> bool``: walk a directory only if True (default
    all). ``resume``: skip dirs with a scanned_dirs row. ``skip_indexed``:
    within walked dirs, skip files already indexed for this drive.
    ``force``: ignore resume + skip_indexed. Directories are marked
    complete post-order, EXCEPT any whose subtree had an ``include``-
    pruned directory (left resumable). Heartbeat via ``on_progress``.
    """
    source = Path(source).resolve()
    if not source.is_dir():
        raise NotADirectoryError(f"source root is not a directory: {source}")

    serial = get_volume_serial(source)
    drive_label = label or get_volume_label(source) or \
        source.anchor.rstrip("\\/").rstrip(":") or "drive"
    with transaction(conn):
        drive_id = upsert_drive(
            conn, volume_serial=serial, label=drive_label,
            source_root=str(source),
        )

    if force:
        resume = skip_indexed = False
    done_dirs = scanned_relpaths(conn, drive_id=drive_id) if resume else set()
    seen_rel = indexed_relpaths(conn, drive_id=drive_id) if skip_indexed else set()

    ext_lower = tuple(e.lower() for e in extensions)
    inserted = 0
    seen = 0
    batch: list[dict] = []
    # Per-dir tallies for post-order scanned_dirs rows: relpath -> [tot, media]
    tally: dict[str, list[int]] = {}
    state = {"examined": 0, "last_n": 0, "last_t": time.monotonic()}

    include_pruned: set[str] = set()

    def _rel(p: Path) -> str:
        try:
            r = str(p.relative_to(source)).replace("\\", "/")
        except ValueError:
            return ""  # symlink/junction escaping source -> attribute to root
        return "" if r in (".", "") else r

    def _prune(dirpath: str, dirname: str) -> bool:
        child_rel = _rel(Path(dirpath) / dirname)
        if include is not None and not include(child_rel):
            include_pruned.add(child_rel)
            return True
        return bool(resume and child_rel in done_dirs)

    def _flush() -> None:
        nonlocal inserted
        if not batch:
            return
        with transaction(conn):
            inserted += insert_scanned_files(conn, batch)
        batch.clear()

    walked: list[str] = []
    counts: dict[str, int] = {"inaccessible": 0}  # bound even if _walk yields nothing
    for dirpath, filenames, walk_counts in _walk(source, prune=_prune):
        counts = walk_counts
        rel = _rel(Path(dirpath))
        if resume and rel in done_dirs:
            continue
        walked.append(rel)
        tally.setdefault(rel, [0, 0])
        for name in filenames:
            tally[rel][0] += 1
            state["examined"] += 1
            if not name.lower().endswith(ext_lower):
                continue
            tally[rel][1] += 1
            p = Path(dirpath) / name
            file_rel = _rel(p)
            if skip_indexed and file_rel in seen_rel:
                continue
            try:
                st = p.stat()
            except OSError as e:
                log.warning("stat failed for %s: %s", p, e)
                continue
            seen += 1
            batch.append({
                "path": str(p), "drive_id": drive_id, "relpath": file_rel,
                "size": int(st.st_size), "mtime": float(st.st_mtime),
                "kind": classify_kind(p),
            })
            if len(batch) >= _BATCH_SIZE:
                _flush()
        if _heartbeat_gate(state):
            log.debug("scan: index dir=%s examined=%d", dirpath, state["examined"])
            if on_progress:
                on_progress(state["examined"], 0)

    _flush()
    if on_progress:
        on_progress(state["examined"], 0)

    _mark_completed(conn, drive_id, walked, tally, include_pruned)

    log.info("scan: seen=%d inserted=%d inaccessible=%d",
             seen, inserted, counts["inaccessible"])
    return {
        "inserted": inserted, "seen": seen, "drive_id": drive_id,
        "inaccessible_dirs": counts["inaccessible"],
    }


def scan(conn, source: Path, *, label: str | None = None,
         extensions: tuple[str, ...] = DEFAULT_EXTENSIONS,
         on_progress=None) -> dict[str, int]:
    """Back-compat Stage 0 entry: index everything, no resume.

    Preserved so existing callers/tests keep working.
    """
    return index(conn, source, label=label, extensions=extensions,
                 on_progress=on_progress)
