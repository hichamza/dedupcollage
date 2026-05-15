"""End-to-end smoke tests for the pipeline.

These exercise the full chain on synthetic JPEGs: scan -> quickhash ->
fullhash -> analyze -> cluster -> select -> organize. The smoke test
intentionally does NOT depend on ExifTool or FFmpeg being installed; the
code paths that need them degrade gracefully.
"""

from __future__ import annotations

from pathlib import Path

from dedupcollage import analyze, cluster, fingerprint, organize, scan, select
from dedupcollage.db import connect, file_counts


def test_scan_on_progress_two_arg_contract(tmp_db: Path, image_factory, tmp_path: Path) -> None:
    """scan() must invoke on_progress(done, total) like every other stage.

    Regression: scan called on_progress(seen) with a single arg, crashing
    both the GUI closure and the CLI tqdm callback (which require two args)
    with "progress() missing 1 required positional argument: 'total'".
    scan has no known total, so it reports total=0 (consumers treat <=0 as
    an indeterminate/busy indicator).
    """
    src = tmp_path / "src"
    src.mkdir()
    image_factory("src/only.jpg", color=(1, 2, 3))

    calls: list[tuple[int, int]] = []

    def on_progress(done: int, total: int) -> None:  # strict 2-arg, as GUI/CLI
        calls.append((done, total))

    conn = connect(tmp_db)
    scan.scan(conn, src, on_progress=on_progress)

    assert calls, "on_progress was never invoked"
    for done, total in calls:
        assert done >= 1
        assert total == 0  # scan total is unknown -> 0


def test_pipeline_basic(tmp_db: Path, image_factory, tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out"

    # Two near-identical reds (same content -> exact dupes).
    image_factory("src/a1.jpg", color=(255, 0, 0))
    image_factory("src/a2.jpg", color=(255, 0, 0))
    # A truncated copy — should decode partially and still be clusterable.
    image_factory("src/a3_corrupt.jpg", color=(255, 0, 0), truncate_bytes=50)
    # A distinct image.
    image_factory("src/b1.jpg", color=(0, 100, 200))

    conn = connect(tmp_db)
    scan.scan(conn, src)
    fingerprint.run_quickhash_stage(conn)
    fingerprint.run_fullhash_stage(conn)
    analyze.run_analyze_stage(conn)
    cluster.run_cluster_stage(conn, hamming_threshold=8)
    select.run_select_stage(conn)
    organize.run_organize_stage(conn, out)

    counts = file_counts(conn)
    assert counts["total"] == 4
    assert counts["clusters"] >= 2  # at least the red cluster and the blue singleton
    assert counts["winners"] >= 2

    # Output tree should contain at least one folder and at least 2 files.
    assert out.exists()
    files = list(out.rglob("*.jpg"))
    assert len(files) >= 2


def test_save_date_marker(tmp_db: Path, image_factory, tmp_path: Path) -> None:
    """If no EXIF capture-date is available, the output folder must include -s."""
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out"

    # All four files lack camera EXIF -> all 'mtime' source -> -s expected.
    image_factory("src/m1.jpg", color=(10, 20, 30))
    image_factory("src/m2.jpg", color=(10, 20, 30))

    conn = connect(tmp_db)
    scan.scan(conn, src)
    fingerprint.run_quickhash_stage(conn)
    fingerprint.run_fullhash_stage(conn)
    analyze.run_analyze_stage(conn)
    cluster.run_cluster_stage(conn)
    select.run_select_stage(conn)
    organize.run_organize_stage(conn, out)

    # At least one folder name should contain the '-s-' marker.
    folder_names = [p.name for p in out.iterdir() if p.is_dir() and not p.name.startswith("_")]
    assert any("-s-" in n or n.endswith("-s") for n in folder_names), folder_names
