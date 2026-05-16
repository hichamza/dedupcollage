# Changelog

All notable changes to DedupCollage are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0-alpha.2] — 2026-05-16

### Fixed
- Scanning from the GUI crashed with `progress() missing 1 required positional argument: 'total'`. `scan` now honors the pipeline-wide `on_progress(done, total)` contract (reports `total=0` for its unbounded walk).

### Changed
- Pre-release builds now log at DEBUG by default for troubleshooting; stable releases stay at INFO automatically. CLI `--log-level` still overrides. Startup logs the version and log-file path.

### Added
- Regression test for the `scan` two-arg progress contract.

## [0.1.0-alpha] — 2026-05-15

First alpha. Internal v0 validation release; published as a GitHub pre-release.

### Added
- Initial pipeline: scan, quickhash, fullhash, analyze, cluster, select, organize.
- PySide6 GUI: source/output picker, throttle control, live progress, cluster tree, preview pane.
- Multi-drive support with reconnect prompts.
- Save-date fallback with `-s` folder marker when no EXIF capture date is available across a cluster.
- Resource governor for CPU/RAM/IO throttling (Background / Balanced / Full-speed / Custom).
- Windows installer via PyInstaller + Inno Setup.
- GitHub Actions CI for tests and tagged-release installer builds.
