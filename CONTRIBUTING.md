# Contributing to DedupCollage

Thanks for your interest. This project is GPL-3.0 — by contributing you agree your work will be licensed the same way.

## Reporting bugs

Open an issue at <https://github.com/hichamza/dedupcollage/issues>. Useful information to include:

- DedupCollage version (Help → About, or `dedupcollage --version`)
- Windows version
- A short description of what you expected vs. what happened
- The contents of `%LOCALAPPDATA%\DedupCollage\logs\dedupcollage.log` (last 200 lines)
- If reproducible: a minimal set of file fingerprints (run `dedupcollage debug fingerprint <path>` — does NOT upload any image data)

Do not attach real photos to public issues. If a bug only reproduces with a specific file, mention that and we'll arrange private channels.

## Pull requests

1. Fork, create a feature branch from `main`.
2. Run `ruff check src tests` and `pytest` before pushing — CI runs both and will reject failing PRs.
3. Write a clear PR description: what problem, what change, how tested.
4. One logical change per PR. Smaller PRs ship faster.

## Development setup

```powershell
git clone https://github.com/<your-fork>/dedupcollage.git
cd dedupcollage
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
pip install -e .
```

VS Code is the supported editor — the `.vscode/` folder contains debug configurations, tasks, and recommended extensions. Open the repo and accept the extension recommendations on first launch.

## Code style

- Python 3.10+ syntax (`X | None`, not `Optional[X]`).
- Type hints on all public functions.
- Docstrings on modules and public functions, not on every helper.
- Ruff with the rules in `pyproject.toml` is the formatter and linter.
- Logging through `logging.getLogger(__name__)`, not `print()`.

## Where things live

- `src/dedupcollage/` — Python source
- `tests/` — pytest
- `packaging/` — PyInstaller spec, Inno Setup script
- `.github/workflows/` — CI/CD
- `.vscode/` — VS Code workspace config

## What's in scope vs. out of scope

In scope: improving cluster precision, smarter metadata donation, additional file format support, performance tuning, GUI improvements.

Out of scope (for now): pixel-level repair of corrupted JPEGs, cloud sync, automatic photo tagging via ML, Linux/macOS support (PRs welcome but not maintained by default).

## Security disclosures

See [SECURITY.md](SECURITY.md).
