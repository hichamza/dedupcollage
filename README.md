# DedupCollage

**Recover the best version of every photo from a sea of partial copies.**

DedupCollage is a Windows desktop app for deduplicating photo and video files produced by hard-drive recovery software. Unlike plain duplicate finders, it doesn't just delete extras — it clusters every version of the same image, picks the *least corrupt* one, and grafts metadata (capture date, camera, GPS) from sibling copies that have intact EXIF where the winner doesn't. The result is a clean library where each photo is the best possible reconstruction from all the broken versions you have.

Runs entirely offline. Never sends data anywhere. Source code is GPL-3.0.

---

## Why this exists

If you've ever formatted a drive by mistake and run recovery software, you know the output: tens of thousands of files with names like `f0029481.jpg`, many of them duplicates of each other, but the duplicates aren't byte-identical. Some have intact metadata but corrupt pixels. Some have clean pixels but no EXIF. Some have gray-bar corruption in the bottom third. Plain dedupers can't help — they only catch byte-equal duplicates and have no opinion about quality.

DedupCollage works differently: it groups near-duplicate copies into clusters (using perceptual hashing on the image content), scores each member by integrity (decode-ability, pixel coverage, EXIF completeness), picks the highest-quality version as the winner, and copies it to a clean output tree organized by capture date — donating EXIF metadata from siblings into the winner when needed.

## Features

- Scans JPEG, PNG, HEIC, RAW (CR2/NEF/ARW/DNG), and video (MP4/MOV).
- Two-stage hashing (quick xxhash + full SHA-256) for cheap exact-duplicate detection.
- Perceptual hashing (pHash + dHash) for near-duplicate clustering across corruption.
- Per-file integrity scoring: decode test, JPEG EOI marker, valid-pixel-row count.
- Metadata grafting from sibling copies when the winner has stripped EXIF.
- Save-date fallback with explicit `-s` marker when no capture date exists anywhere in the cluster.
- Multi-drive support: scan additional drives later, the tool re-clusters and prompts to reconnect drives needed to apply upgrades.
- Resource throttling: keeps your machine usable while it runs (Background / Balanced / Full-speed / Custom).
- Read-only on source. Output goes to a new tree on a different disk.
- Resumable: kill the process mid-run, restart, continues from the SQLite checkpoint.

## Status

**v0** — internal validation. Pipeline complete, GUI complete, packaged installer builds via CI, no code signing yet. Use this for personal recovery jobs and report bugs.

**v1** — coming after v0 validation: public release with code signing via [SignPath Foundation](https://signpath.org/).

## Install (v0)

Download the latest installer from [Releases](https://github.com/hichamza/dedupcollage/releases).

Windows SmartScreen will show "Unknown publisher" on the first run. Click **More info → Run anyway** to proceed. This is normal for unsigned open-source apps and goes away in v1 once the project is signed via SignPath Foundation.

System requirements:

- Windows 10 or later (64-bit)
- 8 GB RAM recommended (4 GB minimum)
- Free disk space on the output volume roughly equal to the deduplicated total — typically 30–60% of source size

## Quick start

1. Launch DedupCollage from the Start Menu.
2. Click **Source** and pick the folder with your recovery output.
3. Click **Output** and pick a folder on a *different* disk to receive the clean library.
4. Press **Start**. Walk away for a few hours.
5. When it finishes, browse the output folder — files are organized by `YYYY-MM-DD-Camera-Model-Serial/`.

To add more drives later: click **Add drive**, scan, then **Apply** — the tool tells you which existing output files have been upgraded and which drives you need to reconnect to apply the rest.

## Build from source

```powershell
git clone https://github.com/hichamza/dedupcollage.git
cd dedupcollage
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
pip install -e .

# Run the GUI
python -m dedupcollage.gui

# Or the CLI
python -m dedupcollage --help
```

VS Code users: open the repo folder in VS Code, accept the recommended extensions, and use the bundled launch configurations to debug the GUI or CLI directly.

To build the installer locally:

```powershell
python packaging/fetch_third_party.py
pyinstaller packaging/dedupcollage.spec
# then run Inno Setup on packaging/installer.iss
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design — pipeline stages, SQLite schema, memory model, throttling, multi-drive lifecycle, and metadata-donation logic.

## Privacy

DedupCollage runs entirely on your machine. It does not connect to the internet. It does not phone home. It does not collect telemetry. It does not upload files anywhere. The full source code is in this repository — you can verify all of the above.

## Contributing

Bug reports and pull requests welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: see [SECURITY.md](SECURITY.md) for responsible disclosure.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

This is copyleft: if you modify and redistribute DedupCollage, your changes must also be released under GPL-3.0 with source code available.
