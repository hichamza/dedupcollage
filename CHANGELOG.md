# Changelog

All notable changes to DedupCollage are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Initial pipeline: scan, quickhash, fullhash, analyze, cluster, select, organize.
- PySide6 GUI: source/output picker, throttle control, live progress, cluster tree, preview pane.
- Multi-drive support with reconnect prompts.
- Save-date fallback with `-s` folder marker when no EXIF capture date is available across a cluster.
- Resource governor for CPU/RAM/IO throttling (Background / Balanced / Full-speed / Custom).
- Windows installer via PyInstaller + Inno Setup.
- GitHub Actions CI for tests and tagged-release installer builds.

## [0.1.0] — planned

Initial v0 internal validation release.
