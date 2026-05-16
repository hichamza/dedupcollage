from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from dedupcollage import db as dbmod
from dedupcollage import scan as scan_mod
from dedupcollage.cli import cli
from dedupcollage.db import connect
from dedupcollage.discovery import build_tree
from dedupcollage.gui.selection import default_checked, make_include


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


def test_discover_counts_and_heartbeats(tmp_path: Path, image_factory) -> None:
    (tmp_path / "src/photos").mkdir(parents=True)
    (tmp_path / "src/junk/deep").mkdir(parents=True)
    image_factory("src/photos/a.jpg", color=(1, 2, 3))
    image_factory("src/photos/b.jpg", color=(4, 5, 6))
    for i in range(30):
        (tmp_path / f"src/junk/deep/f{i}.txt").write_text("x")

    beats: list[tuple[int, int]] = []
    root = scan_mod.discover(
        tmp_path / "src", on_progress=lambda done, total: beats.append((done, total))
    )

    assert root.media_files == 2
    assert root.total_files == 32
    assert root.child("photos").media_files == 2
    assert root.child("junk").flagged is True          # 30 files, 0 media
    assert root.child("photos").flagged is False
    assert beats, "heartbeat never fired"
    assert all(total == 0 for _, total in beats)        # unknown total
    assert any(done > 0 for done, _ in beats)


def _src(tmp_path: Path, image_factory) -> Path:
    (tmp_path / "src/keep").mkdir(parents=True)
    (tmp_path / "src/skip").mkdir(parents=True)
    image_factory("src/keep/k1.jpg", color=(1, 1, 1))
    image_factory("src/skip/s1.jpg", color=(2, 2, 2))
    return tmp_path / "src"


def test_index_respects_include_predicate(tmp_db: Path, tmp_path: Path, image_factory) -> None:
    src = _src(tmp_path, image_factory)
    conn = connect(tmp_db)
    res = scan_mod.index(
        conn, src, include=lambda rel: not rel.startswith("skip"),
    )
    assert res["inserted"] == 1
    rels = dbmod.indexed_relpaths(conn, drive_id=res["drive_id"])
    assert rels == {"keep/k1.jpg"}


def test_index_resume_skips_completed_dir(tmp_db: Path, tmp_path: Path, image_factory) -> None:
    src = _src(tmp_path, image_factory)
    conn = connect(tmp_db)
    r1 = scan_mod.index(conn, src)            # full pass; marks dirs complete
    assert r1["inserted"] == 2
    image_factory("src/keep/k2.jpg", color=(9, 9, 9))   # added after completion
    r2 = scan_mod.index(conn, src, resume=True)
    assert r2["inserted"] == 0                # 'keep' complete -> not re-walked
    r3 = scan_mod.index(conn, src, resume=True, force=True)
    assert r3["inserted"] == 1                # force re-walks, picks up k2


def test_index_incomplete_dir_skips_indexed_files(tmp_db: Path, tmp_path: Path, image_factory) -> None:
    src = _src(tmp_path, image_factory)
    conn = connect(tmp_db)
    drive_id = scan_mod.index(conn, src)["drive_id"]
    # Simulate interruption: drop the 'keep' completion row.
    conn.execute("DELETE FROM scanned_dirs WHERE relpath = 'keep'")
    conn.commit()
    image_factory("src/keep/k2.jpg", color=(7, 7, 7))
    res = scan_mod.index(conn, src, resume=True, skip_indexed=True)
    assert res["inserted"] == 1              # only the new k2; k1 skipped
    assert dbmod.is_dir_scanned(conn, drive_id=drive_id, relpath="keep")


def test_index_include_prune_keeps_subtree_resumable(
    tmp_db: Path, tmp_path: Path, image_factory
) -> None:
    (tmp_path / "src/a/b/c").mkdir(parents=True)
    image_factory("src/a/b/c/deep.jpg", color=(3, 3, 3))
    image_factory("src/a/top.jpg", color=(4, 4, 4))
    src = tmp_path / "src"
    conn = connect(tmp_db)
    r1 = scan_mod.index(conn, src, include=lambda rel: rel != "a/b/c")
    drive_id = r1["drive_id"]
    sc = dbmod.scanned_relpaths(conn, drive_id=drive_id)
    assert "a/b/c" not in sc
    assert "a/b" not in sc
    assert "" not in sc
    assert dbmod.indexed_relpaths(conn, drive_id=drive_id) == {"a/top.jpg"}
    r2 = scan_mod.index(conn, src, resume=True)
    assert r2["inserted"] == 1
    assert "a/b/c/deep.jpg" in dbmod.indexed_relpaths(conn, drive_id=drive_id)


def test_index_post_order_prefix_collision(
    tmp_db: Path, tmp_path: Path, image_factory
) -> None:
    (tmp_path / "src/keep").mkdir(parents=True)
    (tmp_path / "src/keeper").mkdir(parents=True)
    image_factory("src/keep/a.jpg", color=(1, 1, 1))
    image_factory("src/keeper/b.jpg", color=(2, 2, 2))
    image_factory("src/keeper/c.jpg", color=(3, 3, 3))
    src = tmp_path / "src"
    conn = connect(tmp_db)
    drive_id = scan_mod.index(conn, src)["drive_id"]
    keep_row = conn.execute(
        "SELECT file_count, media_count FROM scanned_dirs "
        "WHERE drive_id=? AND relpath='keep'", (drive_id,)
    ).fetchone()
    keeper_row = conn.execute(
        "SELECT file_count, media_count FROM scanned_dirs "
        "WHERE drive_id=? AND relpath='keeper'", (drive_id,)
    ).fetchone()
    assert tuple(keep_row) == (1, 1)
    assert tuple(keeper_row) == (2, 2)


def test_cli_scan_list_only(tmp_db: Path, tmp_path: Path, image_factory) -> None:
    (tmp_path / "src/photos").mkdir(parents=True)
    image_factory("src/photos/a.jpg", color=(1, 2, 3))
    r = CliRunner().invoke(
        cli, ["--db", str(tmp_db), "scan", "--source", str(tmp_path / "src"),
              "--list-only"],
    )
    assert r.exit_code == 0, r.output
    assert "photos" in r.output
    # --list-only must not write ANY file rows (drive-agnostic check).
    assert connect(tmp_db).execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_default_checked_unchecks_flagged() -> None:
    root = build_tree([("", 0, 0), ("photos", 50, 50), ("junk", 30, 0)])
    checked = default_checked(root, skip_noise=True)
    assert "photos" in checked and "" in checked
    assert "junk" not in checked            # flagged -> unchecked
    all_checked = default_checked(root, skip_noise=False)
    assert "junk" in all_checked            # skip_noise off -> everything


def test_make_include_predicate() -> None:
    inc = make_include({"", "photos", "photos/2024"})
    assert inc("photos") is True
    assert inc("photos/2024") is True       # individually selected
    assert inc("junk") is False
    # Unchecked child excluded even though its parent 'photos' is checked.
    assert inc("photos/raw") is False


def test_make_include_membership_is_exact() -> None:
    inc = make_include({"", "a", "a/keepme"})
    assert inc("") is True
    assert inc("a") is True
    assert inc("a/keepme") is True
    assert inc("a/raw") is False            # not selected -> excluded
    assert inc("b") is False


def test_default_checked_nested_recursion() -> None:
    root = build_tree([("", 0, 0), ("a", 0, 0), ("a/b", 30, 0), ("a/keepme", 5, 5)])
    checked = default_checked(root, skip_noise=True)
    assert "" in checked and "a" in checked
    assert "a/keepme" in checked            # unflagged child kept
    assert "a/b" not in checked             # flagged (30 files, 0 media)


def test_pipeline_worker_scan_param_defaults() -> None:
    import inspect

    from dedupcollage.gui.worker import PipelineWorker
    params = inspect.signature(PipelineWorker.__init__).parameters
    assert params["include"].default is None
    assert params["resume"].default is False
    assert params["skip_indexed"].default is False
    assert params["force"].default is False


def test_heartbeat_fires_on_media_sparse_tree(tmp_db: Path, tmp_path: Path) -> None:
    """Regression for the 'no activity' defect: index() emits progress
    with examined>0 even when there are zero media files."""
    src = tmp_path / "src"
    (src / "devjunk").mkdir(parents=True)
    for i in range(50):
        (src / "devjunk" / f"f{i}.txt").write_text("x")

    calls: list[tuple[int, int]] = []
    conn = connect(tmp_db)
    scan_mod.index(conn, src, on_progress=lambda d, t: calls.append((d, t)))

    assert calls, "on_progress never fired"
    assert all(t == 0 for _, t in calls)
    assert any(d > 0 for d, _ in calls)     # examined climbs with zero media
