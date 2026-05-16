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

from dedupcollage.db import insert_scanned_files, transaction, upsert_drive
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


def scan(
    conn,
    source: Path,
    *,
    label: str | None = None,
    extensions: tuple[str, ...] = DEFAULT_EXTENSIONS,
    on_progress=None,
) -> dict[str, int]:
    """Walk ``source`` and insert all matching files into the index.

    Returns a dict with ``inserted`` (new rows), ``seen`` (total files walked),
    and ``drive_id``.

    ``on_progress`` is an optional callable matching the pipeline-wide
    ``on_progress(done, total)`` contract. The total file count is unknown
    until the walk completes, so scan always reports ``total=0``; consumers
    treat ``total <= 0`` as an indeterminate/busy indicator.
    """
    source = Path(source).resolve()
    if not source.is_dir():
        raise NotADirectoryError(f"source root is not a directory: {source}")

    serial = get_volume_serial(source)
    drive_label = label or get_volume_label(source) or source.anchor.rstrip("\\/").rstrip(":") or "drive"

    with transaction(conn):
        drive_id = upsert_drive(
            conn,
            volume_serial=serial,
            label=drive_label,
            source_root=str(source),
        )

    log.info("scan: source=%s drive_id=%d serial=%s label=%s", source, drive_id, serial, drive_label)

    inserted = 0
    seen = 0
    batch: list[dict] = []

    def _flush() -> None:
        nonlocal inserted
        if not batch:
            return
        with transaction(conn):
            inserted += insert_scanned_files(conn, batch)
        batch.clear()

    for p in iter_candidate_files(source, extensions):
        try:
            st = p.stat()
        except OSError as e:
            log.warning("stat failed for %s: %s", p, e)
            continue
        seen += 1
        try:
            relpath = str(p.relative_to(source))
        except ValueError:
            relpath = str(p)
        batch.append({
            "path": str(p),
            "drive_id": drive_id,
            "relpath": relpath,
            "size": int(st.st_size),
            "mtime": float(st.st_mtime),
            "kind": classify_kind(p),
        })
        if len(batch) >= _BATCH_SIZE:
            _flush()
            if on_progress:
                on_progress(seen, 0)

    _flush()
    if on_progress:
        on_progress(seen, 0)

    log.info("scan: seen=%d inserted=%d", seen, inserted)
    return {"inserted": inserted, "seen": seen, "drive_id": drive_id}
