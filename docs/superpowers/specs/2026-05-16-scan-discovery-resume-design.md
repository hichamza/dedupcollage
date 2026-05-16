# Scan: discovery, noise-dir selection & resume — design

**Date:** 2026-05-16
**Status:** Approved (brainstorming) — pending spec review
**Scope:** Stage 0 (scan) only. Combines three pieces shipped together:
- **A** — scan progress feedback ("no activity" defect)
- **B** — quick discovery walk + evidence-based noise-dir selection
- **C** — resume / incremental scan (skip completed dirs & indexed files)

## Problem

Scanning `C:\Users\hicha` from the GUI looked frozen: the walk ran for
4+ minutes with no UI or log feedback. Root causes:

1. `scan` called `on_progress` only every 1000 *media* files or at
   end-of-walk; media-sparse dev trees never trigger it.
2. GUI `_on_stage_progress` early-returns when `total <= 0` (scan's
   case), so nothing renders.
3. At DEBUG, scan logs only inaccessible-dir warnings — no activity.
4. The user pointed it at a huge mixed tree (profile dir) with no way
   to see, or trim, what gets walked, and no way to resume a repeated
   or interrupted scan.

## Goals

- Scanning always shows observable progress (UI + log).
- Before indexing, the user sees a live directory tree with real
  per-folder media counts and chooses what to include.
- No directory is excluded by name alone — only by measured content.
- Repeated/interrupted scans skip work already done.

## Non-goals (YAGNI)

- Persisting the discovery tree between runs.
- Detecting added/changed files inside an already-completed directory
  (only `Force full re-scan` revisits them).
- GUI threshold sliders (CLI flag only this cycle).
- Filesystem watching / auto-refresh.
- Changing any stage other than scan.

## Architecture

Stage 0 becomes three steps:

```
Discover  →  Select  →  Index
```

- **Discover** — a count-only walk: `os.walk` + extension test, **no
  `stat`, no hashing**. Produces a directory tree where every node
  carries recursive `total_files` and `media_files`. Streams nodes to
  the GUI as it walks.
- **Select** — GUI renders the tree; user adjusts include/exclude;
  control checkboxes (below). CLI uses flags / `--list-only`.
- **Index** — existing scan logic (`stat` + `INSERT OR IGNORE`) over
  **included** directories only, applying resume rules, emitting the
  heartbeat. Writes a `scanned_dirs` row per directory completed.

`scan.py` refactors into:
- `_walk(root, *, prune)` — shared generator over `os.walk` with a
  directory-prune hook and the inaccessible-dir warning (current
  behaviour preserved).
- `discover(root, *, on_progress, on_node) -> DiscoveryTree` —
  count-only; builds/streams the tree.
- `index(conn, root, *, include, resume, skip_indexed, force,
  on_progress, label) -> dict` — the current scan body, parameterised.

All logic lives in non-GUI modules; the GUI only renders and collects
selections, so everything is headless-testable.

## B — noise heuristic (evidence only)

Computed bottom-up per subtree:

```
media_ratio = media_files / total_files   # recursive counts
flagged = (total_files >= MIN_FILES) and (media_ratio < RATIO)
```

Defaults: `MIN_FILES = 20`, `RATIO = 0.01` (1%). Module constants this
cycle; CLI `--min-media-ratio` overrides `RATIO`.

- Flagged subtrees start **unchecked**; all others start **checked**
  ("all included; flagged pre-unchecked").
- Directory **name patterns** (`node_modules`, `.bun`, `.git`,
  `.cache`, `__pycache__`, `.venv`, `venv`, `AppData`, `System Volume
  Information`) render only as a muted `(cache)` / `(system)` label.
  They are **never** an input to `flagged` and **never** cause an
  auto-skip. A name-matched directory containing media is not flagged.

## C — resume / incremental

New table (drive-letter-independent identity):

```sql
CREATE TABLE IF NOT EXISTS scanned_dirs (
  drive_id     INTEGER NOT NULL REFERENCES drives(id),
  relpath      TEXT    NOT NULL,        -- '' = source root
  file_count   INTEGER NOT NULL,        -- total files at completion
  media_count  INTEGER NOT NULL,
  completed_at TEXT    NOT NULL,        -- ISO-8601 UTC
  PRIMARY KEY (drive_id, relpath)
);
```

Plus `CREATE UNIQUE INDEX IF NOT EXISTS idx_files_drive_relpath ON
files(drive_id, relpath)` so per-file skip is volume-stable, not tied
to the absolute `path`. Migration note: this is additive
(`IF NOT EXISTS`); pre-existing alpha DBs with duplicate
`(drive_id, relpath)` rows would fail index creation — acceptable this
cycle (internal alpha, disposable DBs); a fresh DB resolves it. No
data backfill is performed. `drive_id` derives from the existing
`drives.volume_serial`, which is already captured for remount
stability.

Rules (when `resume` enabled):
- A directory is **complete** iff a `scanned_dirs` row exists. The row
  is written in **post-order**: only after that directory *and its
  entire subtree* have been fully walked and indexed without
  interruption. (`os.walk` is top-down; the index pass tracks subtree
  exit and finalises parents after all children.) This makes
  "skip a complete dir wholesale" safe — a complete row guarantees the
  whole subtree was done.
- **Complete** dir → skipped wholesale during Index (not even walked).
- **Incomplete** dir (interrupted) → re-walked; files already indexed
  (matched by `(drive_id, relpath)`) are skipped when `skip_indexed`
  is on; only new files are stat'd + inserted.
- New files added to a **complete** dir are **not** re-detected.
- **Force full re-scan** bypasses all `scanned_dirs` skips and
  re-walks everything. It never deletes `scanned_dirs` rows; inserts
  remain idempotent (`INSERT OR IGNORE`), so no duplicate file rows.

Discovery always walks the full included tree regardless of resume
state (it must, to show current counts); resume only affects Index.

## A — feedback heartbeat

A throttled heartbeat in both `discover()` and `index()`:
- Fires at most every ~0.5 s, and at least every 2000 entries
  examined (whichever comes first), plus once at completion.
- Emits `log.debug("scan: <phase> dir=%s examined=%d media=%d", …)`
  and `on_progress(examined, 0)` (two-arg contract; `total=0` =
  unknown — unchanged contract).
- GUI `_on_stage_progress`: when `total <= 0`, update the stage label
  with the running count
  (`Discovering… 34,120 files (512 media)` / `Indexing… 1,203 files`)
  instead of early-returning. The busy bar still pulses.

This alone resolves the "no activity" symptom.

## Controls

GUI, shown before the run starts:
- `[x] Skip noise dirs (low-ratio)` — apply the flag → pre-uncheck.
- `[x] Resume (skip completed dirs)`
- `[x] Skip already-indexed files`
- `[ ] Force full re-scan` — disables Resume + Skip-indexed for the run.

The discovery tree (checkboxes per directory) is the precise control;
the "Skip noise dirs" checkbox only governs whether flagged dirs start
unchecked.

CLI `scan` gains:
`--resume/--no-resume` (default on), `--skip-indexed/--no-skip-indexed`
(default on), `--min-media-ratio FLOAT` (default 0.01),
`--exclude PATH` (repeatable; relpath under source),
`--force-rescan`, `--list-only` (run Discover, print the tree to
stdout, exit without indexing).

## Data flow

```
Discover: _walk(root, prune=accessible) → per-dir (total, media)
          → DiscoveryTree (in memory) → stream nodes to GUI / stdout
Select:   user toggles → set of included relpaths (+ control flags)
Index:    for dir in _walk(root, prune=included & resume):
            if complete and resume and not force: skip subtree
            else: stat media files; INSERT OR IGNORE;
                  on dir exit → upsert scanned_dirs row
          heartbeat throughout
```

## Error handling

- Inaccessible dirs (`WinError 448`/`5`): keep warn-and-continue.
  Aggregate count surfaced in the stage summary
  (`inaccessible_dirs=N`) instead of only per-line warnings.
- Interrupt (stop / app close): no `scanned_dirs` row for unfinished
  dirs ⇒ they are re-done next run; idempotent inserts ⇒ no
  corruption or duplicates.
- Empty / all-excluded selection: Index is a no-op returning
  `{inserted: 0, seen: 0}`; GUI warns "nothing selected".

## Testing (headless)

- `discover()` recursive `total`/`media` counts incl. nesting and
  non-media-only subtrees.
- Flag computation exactly at `MIN_FILES` / `RATIO` boundaries.
- Resume: dir with a `scanned_dirs` row is skipped; incomplete dir is
  re-walked and indexed files skipped; `Force` bypasses both.
- Identity stability: same `(volume_serial, relpath)` resolves across
  a simulated drive-letter change.
- Heartbeat: `on_progress` fires with `examined > 0` on a media-sparse
  tree (regression for the original defect) and the two-arg contract
  holds.
- `--list-only` prints a tree and writes no `files` rows.
- Existing smoke tests and the scan two-arg regression test stay
  green.
- GUI tree/checkbox wiring: manual (no Qt test harness); logic is in
  non-GUI modules and covered above.

## Ship

Combined release after implementation: bump to the next pre-release
(`v0.1.0-alpha.N` / `0.1.0aN`), CHANGELOG entry, CI green, GitHub
pre-release — consistent with the alpha.2/3 flow.
