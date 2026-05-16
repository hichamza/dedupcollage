# Scan Discovery, Noise-Dir Selection & Resume — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rework Stage 0 into Discover→Select→Index with live progress, evidence-based noise-dir selection, and drive-stable resume.

**Architecture:** A count-only `discover()` walk builds an in-memory directory tree with recursive media counts; the GUI/CLI lets the user pick included dirs; `index()` walks only kept dirs, skips completed dirs and indexed files via a new `scanned_dirs` table, and emits a throttled heartbeat. Pure logic lives in `discovery.py`/`scan.py`/`db.py`; GUI only renders.

**Tech Stack:** Python 3.12, SQLite (stdlib `sqlite3`), Click, PySide6, pytest. Spec: `docs/superpowers/specs/2026-05-16-scan-discovery-resume-design.md`.

---

## File Structure

- `src/dedupcollage/db.py` — add `scanned_dirs` DDL + `idx_files_drive_relpath`; helpers `mark_dir_scanned`, `is_dir_scanned`, `scanned_relpaths`, `indexed_relpaths`.
- `src/dedupcollage/discovery.py` — **new**: `DirNode` dataclass, tree assembly from `(relpath, total, media)` rows, recursive rollup, `flag` rule.
- `src/dedupcollage/scan.py` — extract `_walk()`; add `discover()`, `index()`; keep `scan()` as a back-compat wrapper.
- `src/dedupcollage/cli.py` — `scan` gains resume/skip/ratio/exclude/force/list-only flags.
- `src/dedupcollage/gui/worker.py` — add `DiscoveryWorker`; `PipelineWorker` accepts selection + control flags.
- `src/dedupcollage/gui/main_window.py` — discovery tree widget, control checkboxes, two-step flow, running-count label.
- `tests/test_scan_discovery.py` — **new**: discovery, flags, resume, identity, heartbeat, CLI.
- `tests/test_smoke.py` — must stay green (existing `scan.scan(conn, src)` calls).

Constants (module-level in `discovery.py`): `MIN_FILES = 20`, `MEDIA_RATIO = 0.01`.

---

## Task 1: DB — `scanned_dirs` table, drive-stable index, helpers

**Files:**
- Modify: `src/dedupcollage/db.py` (SCHEMA block ends line 99; helpers after `upsert_drive`)
- Test: `tests/test_scan_discovery.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_scan_discovery.py
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
    with dbmod.transaction(conn):
        dbmod.mark_dir_scanned(conn, drive_id=d1, relpath="a/b", file_count=10, media_count=3)
    assert dbmod.is_dir_scanned(conn, drive_id=d1, relpath="a/b") is True
    # Idempotent upsert (no IntegrityError, counts updated).
    with dbmod.transaction(conn):
        dbmod.mark_dir_scanned(conn, drive_id=d1, relpath="a/b", file_count=12, media_count=4)
    assert dbmod.scanned_relpaths(conn, drive_id=d1) == {"a/b"}
    assert dbmod.scanned_relpaths(conn, drive_id=d2) == set()


def test_scanned_dirs_fk_rejects_unknown_drive(tmp_db: Path) -> None:
    import sqlite3

    import pytest
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_scan_discovery.py -q`
Expected: FAIL — `AttributeError: module 'dedupcollage.db' has no attribute 'is_dir_scanned'`.

- [ ] **Step 3: Add DDL to the `SCHEMA` string**

In `src/dedupcollage/db.py`, immediately after the `files` indexes block (after line 85 `CREATE INDEX IF NOT EXISTS idx_drive ON files(drive_id);`) add:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_files_drive_relpath ON files(drive_id, relpath);

CREATE TABLE IF NOT EXISTS scanned_dirs (
    drive_id     INTEGER NOT NULL REFERENCES drives(id),
    relpath      TEXT    NOT NULL,
    file_count   INTEGER NOT NULL,
    media_count  INTEGER NOT NULL,
    completed_at TEXT    NOT NULL,
    PRIMARY KEY (drive_id, relpath)
);
```

- [ ] **Step 4: Add helpers after `upsert_drive`**

Append to `src/dedupcollage/db.py` (module scope; `iso_now` is already imported line 19):

```python
# ---------- scan-discovery helpers ----------

def mark_dir_scanned(
    conn: sqlite3.Connection, *, drive_id: int, relpath: str,
    file_count: int, media_count: int,
) -> None:
    """Record a directory subtree as fully scanned (idempotent upsert).

    Follows db.py's explicit-transaction model: no internal commit; the
    caller wraps this in ``transaction()`` (or relies on autocommit).
    """
    conn.execute(
        "INSERT INTO scanned_dirs (drive_id, relpath, file_count, media_count, completed_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(drive_id, relpath) DO UPDATE SET "
        "file_count=excluded.file_count, media_count=excluded.media_count, "
        "completed_at=excluded.completed_at",
        (drive_id, relpath, file_count, media_count, iso_now()),
    )


def is_dir_scanned(conn: sqlite3.Connection, *, drive_id: int, relpath: str) -> bool:
    """True iff a completed scanned_dirs row exists for this (drive, relpath)."""
    row = conn.execute(
        "SELECT 1 FROM scanned_dirs WHERE drive_id = ? AND relpath = ?",
        (drive_id, relpath),
    ).fetchone()
    return row is not None


def scanned_relpaths(conn: sqlite3.Connection, *, drive_id: int) -> set[str]:
    """Relpaths of subtrees already marked complete for this drive."""
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_scan_discovery.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add src/dedupcollage/db.py tests/test_scan_discovery.py
git commit -m "feat(db): scanned_dirs table + drive-stable file index + helpers"
```

---

## Task 2: `discovery.py` — directory tree model + flag rule

**Files:**
- Create: `src/dedupcollage/discovery.py`
- Test: `tests/test_scan_discovery.py`

- [ ] **Step 1: Write failing test**

```python
# append to tests/test_scan_discovery.py
from dedupcollage.discovery import build_tree


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_scan_discovery.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'dedupcollage.discovery'`.

- [ ] **Step 3: Create `src/dedupcollage/discovery.py`**

```python
"""Discovery tree: per-directory media counts and the noise-flag rule.

Pure data + logic, no I/O. ``scan.discover()`` feeds it ``(relpath,
own_total, own_media)`` rows; counts roll up to ancestors and the
low-media-ratio flag is computed per node.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MIN_FILES = 20
MEDIA_RATIO = 0.01


@dataclass
class DirNode:
    relpath: str                       # "" = source root
    name: str                          # last path segment ("" for root)
    own_total: int = 0
    own_media: int = 0
    total_files: int = 0               # recursive (filled by build_tree)
    media_files: int = 0               # recursive
    children: dict[str, DirNode] = field(default_factory=dict)

    @property
    def flagged(self) -> bool:
        """True => low-media-ratio noise candidate (starts unchecked)."""
        if self.total_files < MIN_FILES:
            return False
        return (self.media_files / self.total_files) < MEDIA_RATIO

    def child(self, name: str) -> DirNode:
        return self.children[name]


def _ensure(root: DirNode, relpath: str) -> DirNode:
    if relpath == "":
        return root
    node = root
    acc = ""
    for seg in relpath.replace("\\", "/").split("/"):
        acc = seg if acc == "" else f"{acc}/{seg}"
        if seg not in node.children:
            node.children[seg] = DirNode(relpath=acc, name=seg)
        node = node.children[seg]
    return node


def build_tree(rows: list[tuple[str, int, int]]) -> DirNode:
    """Build the tree and roll up recursive counts.

    ``rows`` = list of (relpath, own_total_files, own_media_files).
    Intermediate dirs not present in rows are created with zero own counts.
    """
    root = DirNode(relpath="", name="")
    for relpath, own_total, own_media in rows:
        node = _ensure(root, relpath)
        node.own_total += own_total
        node.own_media += own_media

    def rollup(n: DirNode) -> tuple[int, int]:
        t, m = n.own_total, n.own_media
        for c in n.children.values():
            ct, cm = rollup(c)
            t += ct
            m += cm
        n.total_files, n.media_files = t, m
        return t, m

    rollup(root)
    return root
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_scan_discovery.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/dedupcollage/discovery.py tests/test_scan_discovery.py
git commit -m "feat(discovery): DirNode tree + low-media-ratio flag rule"
```

---

## Task 3: `scan.py` — extract shared `_walk()` (behavior-preserving)

**Files:**
- Modify: `src/dedupcollage/scan.py` (`iter_candidate_files` lines 94-108; `scan()` loop 156-179)
- Test: `tests/test_smoke.py` (must stay green)

- [ ] **Step 1: Add `_walk()` and a name-hint set; keep `iter_candidate_files` working**

In `src/dedupcollage/scan.py`, add `Callable` to the existing `from collections.abc import ...` line (so it reads `from collections.abc import Callable, Iterator`), then add after `log = logging.getLogger(__name__)` / near `_BATCH_SIZE`:

```python
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
```

- [ ] **Step 2: Rewrite `iter_candidate_files` to use `_walk`**

Replace the body of `iter_candidate_files` (lines 94-108) with:

```python
def iter_candidate_files(
    root: Path, extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
) -> Iterator[Path]:
    """Yield media files under ``root`` (suffix in ``extensions``)."""
    ext_lower = tuple(e.lower() for e in extensions)
    for dirpath, filenames, _counts in _walk(root):
        for name in filenames:
            if name.lower().endswith(ext_lower):
                yield Path(dirpath) / name
```

- [ ] **Step 3: Run existing smoke + scan regression tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_smoke.py -q`
Expected: PASS (3 passed) — behavior unchanged (`$`/`_replaced` prune preserved).

- [ ] **Step 4: Run ruff**

Run: `.venv/Scripts/python.exe -m ruff check src tests`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/dedupcollage/scan.py
git commit -m "refactor(scan): extract shared _walk() generator"
```

---

## Task 4: `scan.py` — `discover()` count-only walk + heartbeat

**Files:**
- Modify: `src/dedupcollage/scan.py`
- Test: `tests/test_scan_discovery.py`

- [ ] **Step 1: Write failing test**

```python
# append to tests/test_scan_discovery.py
from dedupcollage import scan as scan_mod


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_scan_discovery.py::test_discover_counts_and_heartbeats -q`
Expected: FAIL — `AttributeError: module 'dedupcollage.scan' has no attribute 'discover'`.

- [ ] **Step 3: Implement `discover()`**

Add to `src/dedupcollage/scan.py`. Add `import time` to the stdlib import block (correct I001 order), and import the tree builder at top: `from dedupcollage.discovery import DirNode, build_tree`. Then add:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_scan_discovery.py::test_discover_counts_and_heartbeats -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dedupcollage/scan.py tests/test_scan_discovery.py
git commit -m "feat(scan): discover() count-only walk with heartbeat"
```

---

## Task 5: `scan.py` — `index()` with resume, skip-indexed, force, post-order completion

**Files:**
- Modify: `src/dedupcollage/scan.py` (replace `scan()` body lines 111-185)
- Test: `tests/test_scan_discovery.py`

- [ ] **Step 1: Write failing tests**

```python
# append to tests/test_scan_discovery.py
from dedupcollage import db as dbmod


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
    assert "a/b/c" not in sc          # excluded dir not complete
    assert "a/b" not in sc            # ancestor of include-pruned not complete
    assert "" not in sc               # root has a pruned descendant
    assert dbmod.indexed_relpaths(conn, drive_id=drive_id) == {"a/top.jpg"}
    # Resume without filter must now pick up the previously-excluded file.
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
    assert tuple(keep_row) == (1, 1)      # 'keep' must NOT absorb 'keeper'
    assert tuple(keeper_row) == (2, 2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_scan_discovery.py -q -k index`
Expected: FAIL — `AttributeError: ... has no attribute 'index'`.

- [ ] **Step 3: Implement `index()` and a back-compat `scan()`**

Replace the entire `def scan(...)` (lines 111-185) in `src/dedupcollage/scan.py` with:

```python
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
    for dirpath, filenames, counts in _walk(source, prune=_prune):
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
```

Add the imports at top of `scan.py`:
`from dedupcollage.db import (insert_scanned_files, transaction, upsert_drive, mark_dir_scanned, scanned_relpaths, indexed_relpaths)` (extend the existing import line 21).

- [ ] **Step 4: Run new + smoke tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_scan_discovery.py tests/test_smoke.py -q`
Expected: PASS (all green; `scan.scan` wrapper keeps smoke tests passing).

- [ ] **Step 5: Run ruff, fix any findings**

Run: `.venv/Scripts/python.exe -m ruff check src tests`
Expected: `All checks passed!` (fix lint inline if not).

- [ ] **Step 6: Commit**

```bash
git add src/dedupcollage/scan.py tests/test_scan_discovery.py
git commit -m "feat(scan): index() with resume, skip-indexed, force, post-order completion"
```

---

## Task 6: CLI — scan flags + `--list-only`

**Files:**
- Modify: `src/dedupcollage/cli.py` (scan command lines 92-100)
- Test: `tests/test_scan_discovery.py`

- [ ] **Step 1: Write failing test**

```python
# append to tests/test_scan_discovery.py
from click.testing import CliRunner

from dedupcollage.cli import cli


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_scan_discovery.py::test_cli_scan_list_only -q`
Expected: FAIL — `No such option: --list-only`.

- [ ] **Step 3: Replace the `scan` command**

Replace lines 92-100 of `src/dedupcollage/cli.py` with:

```python
@cli.command()
@click.option("--source", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--label", default=None, help="Friendly label for this drive.")
@click.option("--resume/--no-resume", default=True, help="Skip completed dirs.")
@click.option("--skip-indexed/--no-skip-indexed", default=True,
              help="Skip files already indexed (within re-walked dirs).")
@click.option("--min-media-ratio", type=float, default=None,
              help="Override noise flag ratio (default 0.01). Affects --list-only.")
@click.option("--exclude", "excludes", multiple=True,
              help="Relpath under SOURCE to exclude (repeatable).")
@click.option("--force-rescan", is_flag=True, default=False,
              help="Ignore resume/skip-indexed; re-walk everything.")
@click.option("--list-only", is_flag=True, default=False,
              help="Run discovery only; print the tree and exit.")
@click.pass_context
def scan(ctx, source: str, label: str | None, resume: bool, skip_indexed: bool,
         min_media_ratio: float | None, excludes: tuple[str, ...],
         force_rescan: bool, list_only: bool) -> None:
    """Stage 0 — discover, then index SOURCE (cheap, no content read)."""
    conn = _open(ctx)
    src = Path(source)

    if list_only:
        # --min-media-ratio only affects the noise flag shown here. Scope the
        # override to this block and always restore it (CliRunner is in-process;
        # an unrestored global mutation pollutes other tests/invocations).
        import dedupcollage.discovery as _disc
        _orig_ratio = _disc.MEDIA_RATIO
        if min_media_ratio is not None:
            _disc.MEDIA_RATIO = min_media_ratio
        try:
            root = scan_mod.discover(src, on_progress=_progress_bar("discover"))

            def _print(node, depth=0):
                tag = " [noise]" if node.flagged else ""
                name = node.name or src.name
                click.echo(f"{'  ' * depth}{name}  "
                           f"({node.media_files}/{node.total_files} media){tag}")
                for c in sorted(node.children.values(), key=lambda n: n.name):
                    _print(c, depth + 1)
            _print(root)
        finally:
            _disc.MEDIA_RATIO = _orig_ratio
        return

    ex = {e.replace("\\", "/").strip("/") for e in excludes}

    def _include(rel: str) -> bool:
        return not any(rel == e or rel.startswith(e + "/") for e in ex)

    result = scan_mod.index(
        conn, src, label=label, include=_include if ex else None,
        resume=resume, skip_indexed=skip_indexed, force=force_rescan,
        on_progress=_progress_bar("scan"),
    )
    click.echo(
        f"scan: inserted={result['inserted']} seen={result['seen']} "
        f"drive_id={result['drive_id']} "
        f"inaccessible_dirs={result['inaccessible_dirs']}"
    )
```

- [ ] **Step 4: Run test + full suite**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: PASS (all).

- [ ] **Step 5: Run ruff**

Run: `.venv/Scripts/python.exe -m ruff check src tests`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/dedupcollage/cli.py tests/test_scan_discovery.py
git commit -m "feat(cli): scan resume/skip/exclude/force/list-only flags"
```

---

## Task 7: GUI — selection helper (pure, testable)

**Files:**
- Create: `src/dedupcollage/gui/selection.py`
- Test: `tests/test_scan_discovery.py`

- [ ] **Step 1: Write failing test**

```python
# append to tests/test_scan_discovery.py
from dedupcollage.discovery import build_tree
from dedupcollage.gui.selection import default_checked, make_include


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_scan_discovery.py -q -k "checked or include"`
Expected: FAIL — `ModuleNotFoundError: No module named 'dedupcollage.gui.selection'`.

- [ ] **Step 3: Create `src/dedupcollage/gui/selection.py`**

```python
"""Pure helpers mapping the discovery tree <-> include selection.

Kept out of the Qt widgets so it is unit-testable headlessly.
"""

from __future__ import annotations

from dedupcollage.discovery import DirNode


def default_checked(root: DirNode, *, skip_noise: bool) -> set[str]:
    """Relpaths checked by default: all dirs, minus flagged when skip_noise."""
    checked: set[str] = set()

    def visit(n: DirNode) -> None:
        if not (skip_noise and n.flagged):
            checked.add(n.relpath)
        for c in n.children.values():
            visit(c)

    visit(root)
    return checked


def make_include(checked: set[str]):
    """Build the scan ``include(relpath)`` predicate from a checked set.

    ``checked`` is the full set of selected directory relpaths. The
    discovery tree enumerates every directory and ``default_checked`` /
    the GUI list each one individually, so membership is **exact**: a
    directory is walked iff its own relpath is in the set. This means
    unchecking a child excludes it even when its parent stays checked
    (no subtree-prefix expansion, so no accidental re-inclusion).
    """
    norm = {c.strip("/") for c in checked}

    def include(relpath: str) -> bool:
        return relpath.strip("/") in norm

    return include
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_scan_discovery.py -q -k "checked or include"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dedupcollage/gui/selection.py tests/test_scan_discovery.py
git commit -m "feat(gui): pure discovery-selection helpers"
```

---

## Task 8: GUI — DiscoveryWorker + PipelineWorker selection params

**Files:**
- Modify: `src/dedupcollage/gui/worker.py`

- [ ] **Step 1: Add `DiscoveryWorker` (after imports, before `PipelineWorker`)**

```python
class DiscoveryWorker(QThread):
    """Runs scan.discover() off the UI thread, emits the tree when done."""

    progress = Signal(int, int)          # examined, 0
    finished_tree = Signal(object)       # DirNode root
    failed = Signal(str)

    def __init__(self, source: Path) -> None:
        super().__init__()
        self._source = source

    def run(self) -> None:
        try:
            from dedupcollage import scan as scan_mod
            root = scan_mod.discover(
                self._source, on_progress=lambda d, t: self.progress.emit(d, t)
            )
            self.finished_tree.emit(root)
        except Exception as e:  # noqa: BLE001
            log.error("discovery failed: %s\n%s", e, traceback.format_exc())
            self.failed.emit(f"{type(e).__name__}: {e}")
```

- [ ] **Step 2: Make `PipelineWorker` accept selection/control flags**

In `PipelineWorker.__init__` (lines 38-55) add params with defaults and store
them. **Defaults are `False`** so the (not-yet-wired) GUI Start button keeps
its pre-feature full-reindex behavior; Task 9 passes the checkbox values
explicitly. Add `from collections.abc import Callable` to worker.py imports
(I001 order) and annotate `include`:

```python
        include: Callable[[str], bool] | None = None,
        resume: bool = False,
        skip_indexed: bool = False,
        force: bool = False,
```
Store: `self._include = include`, `self._resume = resume`, `self._skip_indexed = skip_indexed`, `self._force = force`.

- [ ] **Step 3: Pass them into the scan stage**

In `PipelineWorker.run`, replace the `("scan", lambda: scan_mod.scan(...))` tuple (lines 72-74) with:

```python
                ("scan", lambda: scan_mod.index(
                    conn, self._source, label=self._label,
                    include=self._include, resume=self._resume,
                    skip_indexed=self._skip_indexed, force=self._force,
                    on_progress=progress,
                )),
```

- [ ] **Step 4: Add a defaults-pinning test** (GUI-free, signature introspection only — no QApplication)

Append to `tests/test_scan_discovery.py`:

```python
def test_pipeline_worker_scan_param_defaults() -> None:
    import inspect

    from dedupcollage.gui.worker import PipelineWorker
    params = inspect.signature(PipelineWorker.__init__).parameters
    assert params["include"].default is None
    assert params["resume"].default is False
    assert params["skip_indexed"].default is False
    assert params["force"].default is False
```

- [ ] **Step 5: Run full suite (no GUI regressions in importable code)**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: PASS (all, incl. the new test). Run `.venv/Scripts/python.exe -c "import dedupcollage.gui.worker"` — exit 0. Run `.venv/Scripts/python.exe -m ruff check src tests` — clean.

- [ ] **Step 6: Commit**

```bash
git add src/dedupcollage/gui/worker.py tests/test_scan_discovery.py
git commit -m "feat(gui): DiscoveryWorker + PipelineWorker selection params"
```

---

## Task 9: GUI — discovery tree, control checkboxes, two-step flow

**Files:**
- Modify: `src/dedupcollage/gui/main_window.py`

- [ ] **Step 1: Add widgets in `_build_ui`**

After the inputs row (around line 86) add a controls row and a tree:

```python
        from PySide6.QtWidgets import QCheckBox, QTreeWidget, QTreeWidgetItem
        self._QTreeWidgetItem = QTreeWidgetItem
        self.cb_skip_noise = QCheckBox("Skip noise dirs (low-ratio)")
        self.cb_skip_noise.setChecked(True)
        self.cb_resume = QCheckBox("Resume (skip completed dirs)")
        self.cb_resume.setChecked(True)
        self.cb_skip_indexed = QCheckBox("Skip already-indexed files")
        self.cb_skip_indexed.setChecked(True)
        self.cb_force = QCheckBox("Force full re-scan")
        self.cb_force.setChecked(False)
        controls_row = QHBoxLayout()
        for w in (self.cb_skip_noise, self.cb_resume,
                  self.cb_skip_indexed, self.cb_force):
            controls_row.addWidget(w)
        controls_row.addStretch(1)
        layout.addLayout(controls_row)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Folder", "media / total"])
        self.tree.setColumnWidth(0, 420)
        layout.addWidget(self.tree, stretch=1)
```

- [ ] **Step 2: Repurpose Start button into two-step flow**

Replace the `start_btn`/`stop_btn` wiring (lines 108-111) with:

```python
        self.discover_btn = QPushButton("Scan (discover)")
        self.discover_btn.clicked.connect(self.start_discovery)
        self.index_btn = QPushButton("Start indexing")
        self.index_btn.clicked.connect(self.start_pipeline)
        self.index_btn.setEnabled(False)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_pipeline)
```
(Adjust the buttons row to add `discover_btn`, `index_btn`, `stop_btn`.)

- [ ] **Step 3: Add discovery start + tree population**

Add methods to `MainWindow`:

Use module-level scoped-enum imports: `from PySide6.QtCore import Qt` and
`from dedupcollage.gui.worker import DiscoveryWorker, PipelineWorker` at the
top of the file (not local). Declare `self._disc: DiscoveryWorker | None = None`
in `__init__` next to `self._worker`.

```python
    def start_discovery(self) -> None:
        if self._disc is not None and self._disc.isRunning():
            return
        source = self.source_edit.text().strip()
        if not source:
            self.statusBar().showMessage("Pick a source folder first", 5000)
            return
        self.tree.clear()
        self.index_btn.setEnabled(False)
        self.discover_btn.setEnabled(False)
        self.stage_label.setText("Discovering…")
        self.progress.setRange(0, 0)
        self._disc = DiscoveryWorker(Path(source))
        self._disc.progress.connect(self._on_stage_progress)
        self._disc.finished_tree.connect(self._on_discovered)
        self._disc.failed.connect(self._on_pipeline_error)
        self._disc.start()

    def _on_discovered(self, root) -> None:
        from dedupcollage import scan as scan_mod
        from dedupcollage.gui.selection import default_checked
        checked = default_checked(root, skip_noise=self.cb_skip_noise.isChecked())

        def add(node, parent_item):
            label = node.name or self.source_edit.text().strip()
            hint = scan_mod.name_hint(node.name) if node.name else None
            text = label + (f"  ({hint})" if hint else "")
            it = QTreeWidgetItem(parent_item, [
                text, f"{node.media_files}/{node.total_files}"])
            it.setData(0, Qt.ItemDataRole.UserRole, node.relpath)
            if node.relpath == "":
                # The source root is always scanned (index() never gates the
                # root via include); a root checkbox would silently do nothing.
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            else:
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(
                    0,
                    Qt.CheckState.Checked if node.relpath in checked
                    else Qt.CheckState.Unchecked,
                )
            for c in sorted(node.children.values(), key=lambda n: n.name):
                add(c, it)
            return it

        # QTreeWidgetItem(self.tree, ...) already inserts as a top-level
        # item; do NOT also call addTopLevelItem (double-add).
        top = add(root, self.tree)
        top.setExpanded(True)
        self.stage_label.setText("Select folders, then Start indexing")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.index_btn.setEnabled(True)
        self.discover_btn.setEnabled(True)

    def _checked_relpaths(self) -> set[str]:
        out: set[str] = set()

        def walk(item):
            if item.checkState(0) == Qt.CheckState.Checked:
                out.add(item.data(0, Qt.ItemDataRole.UserRole))
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))
        return out

    def closeEvent(self, event) -> None:
        for w in (self._worker, self._disc):
            if w is not None and w.isRunning():
                if hasattr(w, "request_stop"):
                    w.request_stop()
                w.quit()
                w.wait(3000)
        event.accept()
```

- [ ] **Step 4: Wire selection into `start_pipeline`**

In `start_pipeline`, add a re-entrancy guard at the very top:
```python
        if self._worker is not None and self._worker.isRunning():
            return
```
and before constructing `PipelineWorker` (after source/output validation):
```python
        checked = self._checked_relpaths()
        if not checked:
            self.statusBar().showMessage(
                "Run discovery and select folders first.", 5000)
            return
        include = make_include(checked)
```
(with `from dedupcollage.gui.selection import make_include` at module top).
Pass to the constructor:
`include=include, resume=self.cb_resume.isChecked(), skip_indexed=self.cb_skip_indexed.isChecked(), force=self.cb_force.isChecked(),`

- [ ] **Step 5: Manual verification**

Run: `.venv/Scripts/python.exe -m dedupcollage.gui`
- Pick a small source with a `node_modules` and a `photos` subfolder.
- Click "Scan (discover)": label shows "Discovering… N files", tree fills, `node_modules` row unchecked, `photos` checked.
- Click "Start indexing": pipeline runs only on checked dirs; progress shows running count.
Confirm no crash; check the log file shows `scan: discover` then `scan: index` debug lines.

- [ ] **Step 6: Commit**

```bash
git add src/dedupcollage/gui/main_window.py
git commit -m "feat(gui): discovery tree, control checkboxes, two-step scan flow"
```

---

## Task 10: GUI — running-count label when total unknown (A visible fix)

**Files:**
- Modify: `src/dedupcollage/gui/main_window.py` (`_on_stage_progress` lines ~204-208)

- [ ] **Step 1: Replace `_on_stage_progress`**

```python
    def _on_stage_progress(self, done: int, total: int) -> None:
        if total <= 0:
            base = self.stage_label.text().split("…")[0] or "Working"
            self.stage_label.setText(f"{base}… {done:,} files")
            return
        self.progress.setRange(0, total)
        self.progress.setValue(done)
```

- [ ] **Step 2: Manual verification**

Run: `.venv/Scripts/python.exe -m dedupcollage.gui`, run discovery on a large dir.
Expected: label increments live ("Discovering… 12,345 files"); never appears frozen.

- [ ] **Step 3: Commit**

```bash
git add src/dedupcollage/gui/main_window.py
git commit -m "fix(gui): show running count while total is unknown"
```

---

## Task 11: Integration regression + full verification

**Files:**
- Test: `tests/test_scan_discovery.py`

- [ ] **Step 1: Add the original-defect regression test**

```python
# append to tests/test_scan_discovery.py
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
```

- [ ] **Step 2: Run the whole suite**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: PASS (all green, incl. `tests/test_smoke.py`).

- [ ] **Step 3: Ruff**

Run: `.venv/Scripts/python.exe -m ruff check src tests`
Expected: `All checks passed!`

- [ ] **Step 4: Commit**

```bash
git add tests/test_scan_discovery.py
git commit -m "test: heartbeat regression on media-sparse tree"
```

---

## Task 12: Ship v0.2.0-alpha.2

**Files:**
- Modify: `pyproject.toml:7`, `src/dedupcollage/__init__.py:8`, `packaging/installer.iss:7`, `CHANGELOG.md`

- [ ] **Step 1: Bump versions**

- `pyproject.toml` line 7: `version = "0.2.0a2"`
- `src/dedupcollage/__init__.py` line 8: `__version__ = "0.2.0a2"`
- `packaging/installer.iss` line 7: `#define MyAppVersion "0.2.0-alpha.2"`

- [ ] **Step 2: Add CHANGELOG entry**

In `CHANGELOG.md`, under `## [Unreleased]`, add:

```markdown
## [0.2.0-alpha.2] — 2026-05-16

### Added
- Scan discovery phase: a count-only walk builds a live directory tree
  with per-folder media counts; low-media-ratio folders are flagged.
- GUI two-step scan: discover → pick folders → index. Control
  checkboxes: skip noise dirs, resume, skip already-indexed, force.
- Resume/incremental scan via drive-stable `scanned_dirs`; completed
  subtrees are skipped on repeat/interrupted scans.
- CLI `scan` flags: `--resume/--no-resume`, `--skip-indexed/...`,
  `--min-media-ratio`, `--exclude`, `--force-rescan`, `--list-only`.

### Fixed
- Scan no longer appears frozen: a throttled heartbeat reports a
  running file count to the GUI and DEBUG log even on media-sparse
  trees.
```

- [ ] **Step 3: Reinstall, verify version, full suite, ruff**

Run:
```bash
uv pip install --python .venv/Scripts/python.exe -e . -q
.venv/Scripts/python.exe -c "import importlib.metadata as m; print(m.version('dedupcollage'))"
.venv/Scripts/python.exe -m pytest tests/ -q
.venv/Scripts/python.exe -m ruff check src tests
```
Expected: `0.2.0a2`; all tests pass; ruff clean.

- [ ] **Step 4: Commit, push, tag, watch CI**

```bash
git add -A
git commit -m "Release v0.2.0-alpha.2: scan discovery, noise selection, resume"
git push origin main
git tag -a v0.2.0-alpha.2 -m "DedupCollage v0.2.0-alpha.2"
git push origin v0.2.0-alpha.2
```
Then watch: `gh run watch $(gh run list --workflow=release.yml --limit 1 --json databaseId --jq '.[0].databaseId') --exit-status --interval 30`
Expected: workflow conclusion `success`; `gh release view v0.2.0-alpha.2` shows a prerelease with `dedupcollage-setup-0.2.0-alpha.2.exe`.

- [ ] **Step 5: Verify CI green on main too**

Run: `gh run list --workflow=ci.yml --limit 1`
Expected: latest CI run `success` (3.10/3.11/3.12).

---

## Self-Review

**Spec coverage:**
- Discover→Select→Index architecture → Tasks 3,4,5,9 ✓
- Count-only discovery + live tree → Task 4 + Task 9 streaming ✓
- Low-media-ratio flag (MIN_FILES=20, RATIO=0.01), names label-only → Task 2 + Task 3 (`name_hint`) ✓
- `scanned_dirs` table, `UNIQUE(drive_id, relpath)`, post-order completion → Tasks 1,5 ✓
- Resume / skip-indexed / force semantics → Task 5 tests ✓
- Heartbeat (A) in both walks; GUI running count → Tasks 4,5,10 ✓
- Controls (3 checkboxes + force) GUI + CLI parity → Tasks 6,9 ✓
- Errors: inaccessible aggregate in summary → Task 5 (`inaccessible_dirs`) ✓
- Testing matrix → Tasks 1,2,4,5,6,7,11 ✓
- Ship as next prerelease → Task 12 ✓

**Placeholder scan:** none — every code step has full code.

**Type consistency:** `DirNode` (relpath/name/own_total/own_media/total_files/media_files/children/flagged/child) consistent across Tasks 2,4,7,9. `index()` signature consistent across Tasks 5,6,8. `discover()` signature consistent across Tasks 4,8,9. `default_checked`/`make_include` consistent Tasks 7,9. Helper names (`mark_dir_scanned`, `is_dir_scanned`, `scanned_relpaths`, `indexed_relpaths`) consistent Tasks 1,5.

No gaps found.
