# DedupCollage — Photo & Video Recovery Pipeline

## Problem

A 1TB pool of files produced by hard-disk recovery software. The recovery process generated heavy duplication, but the duplicates are not byte-identical — they exhibit three independent failure modes:

1. **Right metadata, corrupt image.** EXIF and headers survived, but pixel data is truncated, scrambled, or has gray-bar corruption below some row.
2. **Right image, wrong/missing metadata.** Pixels are clean but EXIF was stripped or replaced.
3. **Right image, partial corruption + partial metadata.** Mixed bag, common when the same JPEG was rescued from two separate disk regions.

The goal: from any cluster of versions of "the same picture," produce a single output file that has the **largest valid pixel region** combined with **the best available metadata donated from sibling copies**, organized chronologically by capture time.

## Non-goals

- Not editing or reconstructing pixel data — we pick the best existing copy, we don't repair JPEG streams.
- Not modifying source files — source is read-only.
- Not a database server — single SQLite file, embedded.
- Not real-time — this is an offline batch pipeline.

## Top-level design

A six-stage pipeline. Each stage reads from and writes to a single SQLite database, so any stage can be re-run, resumed after a crash, or examined manually. Files on disk are touched only during scan (read) and the final organize step (read source, write to a new tree on a different disk).

```
[source 1TB across N drives]
     │
     ▼
 (0) scan        — walk filesystem, insert (path, size, mtime, drive_id)
     │
     ▼
 (1) quickhash   — xxhash of head+tail+size (cheap dedup filter)
     │
     ▼
 (2) fullhash    — SHA-256 on quickhash collisions only
     │
     ▼
 (3) analyze     — perceptual hash, integrity score, EXIF, effective_date
     │
     ▼
 (4) cluster     — group exact dupes (sha256) and near-dupes (phash)
     │
     ▼
 (5) select      — pick winner + metadata donor per cluster
     │
     ▼
 (6) organize    — copy winners to clean tree, inject donor metadata
     │
     ▼
[output: YYYY-MM-DD[-s]-device-serial/]
```

## Why this shape

**SQLite as the durable index.** A flat schema with one row per file makes the pipeline crash-safe and inspectable. At ~500 bytes per row, even 2 million files would be a ~1 GB database — small enough to keep on an SSD scratch disk. WAL mode handles concurrent read-while-write from worker threads without ceremony.

**Two-stage hashing.** Computing SHA-256 over 1 TB takes hours on an HDD. Most files in a recovery pool are *not* exact duplicates of each other, so doing a full hash on everything wastes I/O. Stage 1 reads only 128 KB per file (64 KB head + 64 KB tail) plus the file size and produces an xxhash fingerprint. Files with unique Stage 1 fingerprints can never be exact duplicates and skip Stage 2 entirely. Only the surviving collision groups get a full SHA-256.

**Perceptual hash for near-duplicate detection.** Exact-dupe SHA-256 catches files that are byte-identical. It will NOT catch the case where two recovered copies of the same photo differ by a few bytes of corruption. For that we compute a 64-bit perceptual hash (`pHash`) and a 64-bit difference hash (`dHash`) on a downscaled thumbnail of the decoded image. Two files are near-duplicates when their Hamming distance on either hash is ≤ 8 bits.

**Integrity scoring, not integrity gating.** Rather than "is this file good or bad," we score each file on a continuous scale and let the winner emerge. A file that decodes cleanly with full EXIF scores higher than one that decodes but has gray-bar corruption in the bottom 20% of rows; that one in turn scores higher than one that won't decode at all. The scoring formula is in `integrity.py` and is meant to be tuned.

**Metadata donation.** The "best pixels" file and the "best metadata" file inside a cluster are often different files. After picking a winner by image quality, we scan its siblings for the most complete EXIF (capture time, camera serial, lens, GPS) and write that into a copy of the winner via `exiftool`. The original winner file is never modified.

**Effective date with explicit fallback.** Per-file chain: `EXIF DateTimeOriginal` → `EXIF CreateDate` → file `mtime`, tagged with which one was used. Per-cluster: take the *best* source across all members (donation works for dates too). If and only if every member of a cluster falls back to `mtime`, the output folder gets an `-s` marker (e.g. `2023-08-14-s-unknown-device/`) so the user always knows which dates are real and which are saves.

## Pipeline stages in detail

### Stage 0 — scan (`scan.py`)

Recursive walk of the source root, filtered by extension allowlist. For each file we record `path`, `size`, `mtime`, `drive_id`, `relpath`. We do NOT open or read file content here.

Drive identity is captured by reading the volume serial number from the filesystem (Windows: `GetVolumeInformationW` via ctypes). Files store both their absolute path and a relpath relative to the drive root, so they can be located again after the drive is reconnected at a different mount letter.

### Stage 1 — quickhash (`fingerprint.py`)

For each file without a `quick_hash`, read first 64 KB + last 64 KB + size, hash with xxhash64. Files with unique quick hashes cannot be exact duplicates.

### Stage 2 — fullhash (`fingerprint.py`)

For files whose `quick_hash` collides with another, stream the full file in 1 MB chunks and compute SHA-256. Files with unique quick hashes skip this stage entirely.

### Stage 3 — analyze (`analyze.py` + `integrity.py` + `metadata.py` + `perceptual.py`)

Per-file:
1. Decode via Pillow / pillow-heif / rawpy / ffmpeg (for video).
2. Pixel integrity heuristic — scan rows bottom-up, count rows with non-degenerate variance.
3. JPEG EOI marker check for JPEGs.
4. pHash + dHash on the decoded thumbnail.
5. EXIF extraction via ExifTool (with Pillow fallback for JPEG when ExifTool isn't installed).
6. Compute `effective_date` and `date_source` from the fallback chain.
7. Compute `quality_score`.

### Stage 4 — cluster (`cluster.py`)

Two passes:
1. Exact dupes by SHA-256 (Union-Find).
2. Near dupes by pHash, bucket by top 16 bits, pairwise Hamming distance within buckets, union pairs within threshold.

Singletons get their own cluster — they represent unique images and pass through.

### Stage 5 — select (`select.py`)

Per cluster:
- Winner = max `quality_score` (ties: larger size, then has_full_exif).
- Donor = best EXIF completeness (has_full_exif + serial + lens, then quality_score tiebreak).
- Cluster `effective_date` = best `date_source` rank across members.

### Stage 6 — organize (`organize.py`)

Per winner: resolve target path under output root, copy via temp + atomic rename, optionally inject donor metadata via exiftool, write manifest CSV row. On re-organize: if winner changed, move old target to `_replaced/YYYY-MM-DD/`.

## Schema

See `src/dedupcollage/db.py` for the canonical schema definition. Three tables:

- `drives` — volume serial-keyed identity for source drives.
- `files` — one row per file with everything from scan to selection.
- `clusters` — winner_id, donor_id, member_count, effective_date.

Indexes on `quick_hash`, `sha256`, `cluster_id`, `effective_date`, `last_stage_done`, `drive_id` — covering every query the pipeline issues.

## Throttling

A `Governor` (in `governor.py`) samples CPU / RAM / I/O every 500 ms and gates worker token acquisition. Three presets (`background`, `balanced`, `fullspeed`) plus `custom` with explicit caps. Default `balanced` = 90% CPU cap, below-normal process priority, 85% RAM cap.

Workers call `governor.acquire()` before each unit of work. The sampler thread updates measurements atomically; workers spin-sleep when measurements exceed caps.

## Resumability

Every long stage updates `files.last_stage_done` per row. Re-running any subcommand picks up where it left off. Killing the process loses at most the in-flight worker batch.

## Multi-drive lifecycle

After organizing one drive's worth of files, the user adds a second drive (`add-drive`), re-runs `cluster` and `select` to re-pick winners across the union, then runs `organize` again. Clusters whose new winner is on an offline drive are flagged "pending"; the GUI surfaces which drive labels are needed and applies updates only when those drives are online.

## File layout

```
dedupcollage/
├── ARCHITECTURE.md             # this file
├── README.md
├── LICENSE                     # GPL-3.0
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
├── .gitignore
├── .gitattributes              # normalizes line endings
├── .github/
│   ├── workflows/
│   │   ├── ci.yml              # lint + test on push
│   │   └── release.yml         # build installer on tag
│   ├── ISSUE_TEMPLATE/
│   └── PULL_REQUEST_TEMPLATE.md
├── .vscode/                    # workspace config: settings, launch, tasks, extensions
├── src/dedupcollage/
│   ├── __init__.py             # __version__, __app_name__
│   ├── __main__.py             # CLI entry point
│   ├── _paths.py               # bundled-binary + AppData resolution
│   ├── utils.py                # logging, formatting, classify_kind
│   ├── db.py                   # SQLite schema + helpers
│   ├── governor.py             # CPU/RAM/IO throttle
│   ├── scan.py                 # Stage 0
│   ├── fingerprint.py          # Stages 1 + 2
│   ├── perceptual.py           # pHash, dHash, video frame hash
│   ├── integrity.py            # decode test + quality score
│   ├── metadata.py             # EXIF extraction + effective_date
│   ├── analyze.py              # Stage 3 orchestration
│   ├── cluster.py              # Stage 4
│   ├── select.py               # Stage 5
│   ├── organize.py             # Stage 6 + manifest CSV
│   ├── cli.py                  # click commands
│   └── gui/
│       ├── __init__.py
│       ├── __main__.py
│       ├── app.py              # QApplication bootstrap
│       ├── main_window.py      # UI layout + actions
│       └── worker.py           # background QThread
├── tests/
│   ├── conftest.py
│   └── test_smoke.py           # end-to-end smoke test
└── packaging/
    ├── dedupcollage.spec       # PyInstaller
    ├── installer.iss           # Inno Setup
    ├── fetch_third_party.py    # downloads exiftool + ffmpeg
    └── third_party/bin/        # binaries (gitignored, fetched at build)
```
