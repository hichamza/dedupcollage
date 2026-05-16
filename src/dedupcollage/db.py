"""SQLite schema and connection helpers.

A single file holds the entire pipeline index. WAL mode lets the GUI read
while the pipeline writes. Pragmas tuned for batch-insert workloads.

See ARCHITECTURE.md for the full schema rationale.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dedupcollage._paths import default_db_path
from dedupcollage.utils import iso_now

log = logging.getLogger(__name__)

# Stage markers stored on files.last_stage_done.
STAGE_SCANNED = 0
STAGE_QUICKHASHED = 1
STAGE_FULLHASHED = 2
STAGE_ANALYZED = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS drives (
    id              INTEGER PRIMARY KEY,
    volume_serial   TEXT UNIQUE NOT NULL,
    label           TEXT,
    source_root     TEXT,
    first_seen      TEXT,
    last_seen       TEXT,
    online          INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
    id              INTEGER PRIMARY KEY,
    path            TEXT UNIQUE NOT NULL,
    drive_id        INTEGER REFERENCES drives(id),
    relpath         TEXT,
    size            INTEGER NOT NULL,
    mtime           REAL NOT NULL,
    kind            TEXT,

    quick_hash      TEXT,
    sha256          TEXT,

    decode_ok       INTEGER,
    decode_error    TEXT,
    width           INTEGER,
    height          INTEGER,
    valid_pixel_rows INTEGER,
    jpeg_eoi_ok     INTEGER,
    phash           BLOB,
    dhash           BLOB,

    capture_time    TEXT,
    camera_make     TEXT,
    camera_model    TEXT,
    camera_serial   TEXT,
    lens_model      TEXT,
    has_full_exif   INTEGER,
    effective_date        TEXT,
    date_source           TEXT,

    quality_score   REAL,
    cluster_id      INTEGER,
    is_winner       INTEGER DEFAULT 0,

    target_path     TEXT,

    error           TEXT,
    last_stage_done INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_quick_hash  ON files(quick_hash);
CREATE INDEX IF NOT EXISTS idx_sha256      ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_cluster     ON files(cluster_id);
CREATE INDEX IF NOT EXISTS idx_capture     ON files(effective_date);
CREATE INDEX IF NOT EXISTS idx_stage       ON files(last_stage_done);
CREATE INDEX IF NOT EXISTS idx_drive       ON files(drive_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_files_drive_relpath ON files(drive_id, relpath);

CREATE TABLE IF NOT EXISTS scanned_dirs (
    drive_id     INTEGER NOT NULL REFERENCES drives(id),
    relpath      TEXT    NOT NULL,
    file_count   INTEGER NOT NULL,
    media_count  INTEGER NOT NULL,
    completed_at TEXT    NOT NULL,
    PRIMARY KEY (drive_id, relpath)
);

CREATE TABLE IF NOT EXISTS clusters (
    id                    INTEGER PRIMARY KEY,
    winner_id             INTEGER REFERENCES files(id),
    donor_id              INTEGER REFERENCES files(id),
    member_count          INTEGER,
    kind                  TEXT,
    effective_date        TEXT,
    effective_date_source TEXT,
    organized_from_id     INTEGER REFERENCES files(id),
    organized_at          TEXT,
    target_path           TEXT
);

CREATE INDEX IF NOT EXISTS idx_clusters_winner ON clusters(winner_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

PRAGMAS = [
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA cache_size = -262144",       # 256 MB page cache
    "PRAGMA mmap_size = 1073741824",     # 1 GB mmap I/O
    "PRAGMA temp_store = MEMORY",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA foreign_keys = ON",
]


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open the SQLite database and apply tuning pragmas.

    Creates the schema if it does not exist. Connection has ``row_factory``
    set to ``sqlite3.Row`` so columns are accessible by name.
    """
    path = Path(db_path) if db_path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        path,
        isolation_level=None,  # we control transactions explicitly
        timeout=30.0,
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row

    cur = conn.cursor()
    for pragma in PRAGMAS:
        cur.execute(pragma)
    cur.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Run a block inside a deferred transaction; commit on success, rollback on error."""
    conn.execute("BEGIN")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------- drive helpers ----------

def upsert_drive(
    conn: sqlite3.Connection,
    *,
    volume_serial: str,
    label: str | None,
    source_root: str | None,
) -> int:
    """Insert or refresh a drive row. Returns its id."""
    now = iso_now()
    cur = conn.execute(
        "SELECT id, first_seen FROM drives WHERE volume_serial = ?",
        (volume_serial,),
    )
    row = cur.fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO drives (volume_serial, label, source_root, first_seen, last_seen, online) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (volume_serial, label, source_root, now, now),
        )
        return int(cur.lastrowid)
    drive_id = int(row["id"])
    conn.execute(
        "UPDATE drives SET label = COALESCE(?, label), source_root = COALESCE(?, source_root), "
        "last_seen = ?, online = 1 WHERE id = ?",
        (label, source_root, now, drive_id),
    )
    return drive_id


def mark_dir_scanned(
    conn: sqlite3.Connection, *, drive_id: int, relpath: str,
    file_count: int, media_count: int,
) -> None:
    """Record a directory subtree as fully scanned (idempotent upsert)."""
    conn.execute(
        "INSERT INTO scanned_dirs (drive_id, relpath, file_count, media_count, completed_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(drive_id, relpath) DO UPDATE SET "
        "file_count=excluded.file_count, media_count=excluded.media_count, "
        "completed_at=excluded.completed_at",
        (drive_id, relpath, file_count, media_count, iso_now()),
    )
    conn.commit()


def is_dir_scanned(conn: sqlite3.Connection, *, drive_id: int, relpath: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM scanned_dirs WHERE drive_id = ? AND relpath = ?",
        (drive_id, relpath),
    ).fetchone()
    return row is not None


def scanned_relpaths(conn: sqlite3.Connection, *, drive_id: int) -> set[str]:
    return {
        r[0] for r in conn.execute(
            "SELECT relpath FROM scanned_dirs WHERE drive_id = ?", (drive_id,)
        )
    }


def indexed_relpaths(conn: sqlite3.Connection, *, drive_id: int) -> set[str]:
    """relpaths of files already in the index for this drive."""
    return {
        r[0] for r in conn.execute(
            "SELECT relpath FROM files WHERE drive_id = ? AND relpath IS NOT NULL",
            (drive_id,),
        )
    }


def mark_all_drives_offline(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE drives SET online = 0")


def list_drives(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM drives ORDER BY first_seen"))


# ---------- file helpers ----------

def insert_scanned_files(
    conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]
) -> int:
    """Bulk-insert Stage 0 (scan) rows. Ignores rows whose ``path`` already exists.

    Returns the number of rows actually inserted (some may already be present).
    """
    sql = (
        "INSERT OR IGNORE INTO files "
        "(path, drive_id, relpath, size, mtime, kind, last_stage_done) "
        "VALUES (:path, :drive_id, :relpath, :size, :mtime, :kind, 0)"
    )
    cur = conn.executemany(sql, rows)
    return cur.rowcount or 0


def pending_for_stage(
    conn: sqlite3.Connection, stage: int, *, limit: int | None = None
) -> list[sqlite3.Row]:
    """Files that have completed ``stage - 1`` but not ``stage``.

    Stage indices match the ``STAGE_*`` constants above.
    """
    sql = "SELECT * FROM files WHERE last_stage_done = ? ORDER BY path"
    params: tuple[Any, ...] = (stage - 1,)
    if limit:
        sql += " LIMIT ?"
        params = (*params, limit)
    return list(conn.execute(sql, params))


def update_file(conn: sqlite3.Connection, file_id: int, **fields: Any) -> None:
    """Update arbitrary columns on a file row. Empty kwargs is a no-op."""
    if not fields:
        return
    cols = ", ".join(f"{k} = :{k}" for k in fields)
    params = {**fields, "_id": file_id}
    conn.execute(f"UPDATE files SET {cols} WHERE id = :_id", params)


def file_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Snapshot of pipeline progress for the status command."""
    out: dict[str, int] = {}
    out["total"] = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
    for stage, name in (
        (STAGE_SCANNED, "scanned"),
        (STAGE_QUICKHASHED, "quickhashed"),
        (STAGE_FULLHASHED, "fullhashed"),
        (STAGE_ANALYZED, "analyzed"),
    ):
        out[name] = int(
            conn.execute(
                "SELECT COUNT(*) FROM files WHERE last_stage_done >= ?", (stage,)
            ).fetchone()[0]
        )
    out["errors"] = int(
        conn.execute("SELECT COUNT(*) FROM files WHERE error IS NOT NULL").fetchone()[0]
    )
    out["clusters"] = int(conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0])
    out["winners"] = int(
        conn.execute("SELECT COUNT(*) FROM files WHERE is_winner = 1").fetchone()[0]
    )
    return out


# ---------- meta key/value ----------

def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
