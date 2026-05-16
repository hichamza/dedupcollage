from __future__ import annotations

from pathlib import Path

from dedupcollage import db as dbmod
from dedupcollage.db import connect


def test_scanned_dirs_helpers(tmp_db: Path) -> None:
    conn = connect(tmp_db)
    with dbmod.transaction(conn):
        d1 = dbmod.upsert_drive(conn, volume_serial="VS-A", label="a", source_root="C:\\a")
        d2 = dbmod.upsert_drive(conn, volume_serial="VS-B", label="b", source_root="C:\\b")
    assert dbmod.is_dir_scanned(conn, drive_id=d1, relpath="a/b") is False
    dbmod.mark_dir_scanned(conn, drive_id=d1, relpath="a/b", file_count=10, media_count=3)
    assert dbmod.is_dir_scanned(conn, drive_id=d1, relpath="a/b") is True
    # Idempotent upsert (no IntegrityError, counts updated).
    dbmod.mark_dir_scanned(conn, drive_id=d1, relpath="a/b", file_count=12, media_count=4)
    assert dbmod.scanned_relpaths(conn, drive_id=d1) == {"a/b"}
    assert dbmod.scanned_relpaths(conn, drive_id=d2) == set()


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
