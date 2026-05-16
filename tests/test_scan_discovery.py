from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dedupcollage import db as dbmod
from dedupcollage.db import connect
from dedupcollage.discovery import build_tree


def test_scanned_dirs_helpers(tmp_db: Path) -> None:
    conn = connect(tmp_db)
    with dbmod.transaction(conn):
        d1 = dbmod.upsert_drive(conn, volume_serial="VS-A", label="a", source_root="C:\\a")
        d2 = dbmod.upsert_drive(conn, volume_serial="VS-B", label="b", source_root="C:\\b")
    assert dbmod.is_dir_scanned(conn, drive_id=d1, relpath="a/b") is False
    with dbmod.transaction(conn):
        dbmod.mark_dir_scanned(conn, drive_id=d1, relpath="a/b", file_count=10, media_count=3)
    assert dbmod.is_dir_scanned(conn, drive_id=d1, relpath="a/b") is True
    # Idempotent upsert (no IntegrityError, counts updated).
    with dbmod.transaction(conn):
        dbmod.mark_dir_scanned(conn, drive_id=d1, relpath="a/b", file_count=12, media_count=4)
    assert dbmod.scanned_relpaths(conn, drive_id=d1) == {"a/b"}
    assert dbmod.scanned_relpaths(conn, drive_id=d2) == set()


def test_scanned_dirs_fk_rejects_unknown_drive(tmp_db: Path) -> None:
    conn = connect(tmp_db)
    with pytest.raises(sqlite3.IntegrityError), dbmod.transaction(conn):
        dbmod.mark_dir_scanned(conn, drive_id=999, relpath="x", file_count=1, media_count=0)


def test_indexed_relpaths(tmp_db: Path) -> None:
    conn = connect(tmp_db)
    with dbmod.transaction(conn):
        drive_id = dbmod.upsert_drive(
            conn, volume_serial="VS1", label="d", source_root="C:\\x"
        )
    with dbmod.transaction(conn):
        dbmod.insert_scanned_files(conn, [{
            "path": "C:\\x\\p1.jpg", "drive_id": drive_id, "relpath": "p1.jpg",
            "size": 1, "mtime": 1.0, "kind": "image",
        }])
    assert dbmod.indexed_relpaths(conn, drive_id=drive_id) == {"p1.jpg"}


def test_build_tree_rolls_up_and_flags() -> None:
    # (relpath, own_total_files, own_media_files) — own = files directly in dir.
    rows = [
        ("", 0, 0),
        ("photos", 50, 50),
        ("node_modules", 0, 0),
        ("node_modules/pkg", 800, 1),
    ]
    root = build_tree(rows)
    assert root.total_files == 850
    assert root.media_files == 51
    photos = root.child("photos")
    assert photos.total_files == 50 and photos.media_files == 50
    assert photos.flagged is False
    nm = root.child("node_modules")
    assert nm.total_files == 800 and nm.media_files == 1
    assert nm.flagged is True  # 800>=20 and 1/800 < 0.01
    # Root is not flagged: 51/850 ~ 6% media.
    assert root.flagged is False


def test_flag_thresholds_are_exact() -> None:
    # 19 files -> below MIN_FILES, never flagged even with 0 media.
    assert build_tree([("d", 19, 0)]).child("d").flagged is False
    # 20 files, 0 media -> flagged.
    assert build_tree([("d", 20, 0)]).child("d").flagged is True
    # 100 files, exactly 1 media -> ratio 0.01, NOT < 0.01 -> not flagged.
    assert build_tree([("d", 100, 1)]).child("d").flagged is False
    # 100 files, 0 media -> flagged.
    assert build_tree([("d", 100, 0)]).child("d").flagged is True
