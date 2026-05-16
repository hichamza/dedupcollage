"""Click-based command-line interface.

Usage::

    dedupcollage scan       --source D:\\Recovery
    dedupcollage quickhash
    dedupcollage fullhash
    dedupcollage analyze
    dedupcollage cluster
    dedupcollage select
    dedupcollage organize   --output E:\\Clean
    dedupcollage all        --source D:\\Recovery --output E:\\Clean
    dedupcollage status
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click
from tqdm import tqdm

from dedupcollage import __version__
from dedupcollage import analyze as analyze_mod
from dedupcollage import cluster as cluster_mod
from dedupcollage import fingerprint as fp
from dedupcollage import organize as org_mod
from dedupcollage import scan as scan_mod
from dedupcollage import select as select_mod
from dedupcollage._paths import default_db_path
from dedupcollage.db import connect, file_counts, list_drives, mark_all_drives_offline
from dedupcollage.governor import PRESETS, Governor, ThrottleConfig
from dedupcollage.utils import format_bytes, setup_logging

log = logging.getLogger(__name__)


def _make_governor(throttle: str, cpu: float | None, ram: float | None) -> Governor:
    if throttle == "custom":
        cfg = ThrottleConfig.custom(cpu or 90.0, ram or 85.0, None)
    else:
        cfg = PRESETS.get(throttle, PRESETS["balanced"])
    gov = Governor(cfg)
    gov.start()
    return gov


def _progress_bar(label: str):
    bar: tqdm | None = None

    def cb(done: int, total: int) -> None:
        nonlocal bar
        if bar is None:
            bar = tqdm(total=total, desc=label, unit="file", file=sys.stderr)
        if total and bar.total != total:
            bar.total = total
            bar.refresh()
        bar.n = done
        bar.refresh()
        if done >= total and bar:
            bar.close()
            bar = None

    return cb


# ---------- global options ----------

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--db", "db_path", type=click.Path(dir_okay=False), default=None,
              help="SQLite database path (default: %LOCALAPPDATA%\\DedupCollage\\dedupcollage.db).")
@click.option("--log-level", default=None,
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
              help="Override log level. Default: DEBUG for pre-release builds, INFO for stable.")
@click.version_option(__version__, prog_name="dedupcollage")
@click.pass_context
def cli(ctx: click.Context, db_path: str | None, log_level: str | None) -> None:
    """DedupCollage — deduplicate photo & video recovery output and graft metadata."""
    setup_logging(level=log_level)
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = Path(db_path) if db_path else default_db_path()


def _open(ctx: click.Context):
    return connect(ctx.obj["db_path"])


# ---------- individual stages ----------

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
    if min_media_ratio is not None:
        import dedupcollage.discovery as _disc
        _disc.MEDIA_RATIO = min_media_ratio

    if list_only:
        root = scan_mod.discover(src, on_progress=_progress_bar("discover"))

        def _print(node, depth=0):
            tag = " [noise]" if node.flagged else ""
            name = node.name or src.name
            click.echo(f"{'  ' * depth}{name}  "
                       f"({node.media_files}/{node.total_files} media){tag}")
            for c in sorted(node.children.values(), key=lambda n: n.name):
                _print(c, depth + 1)
        _print(root)
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


@cli.command()
@click.option("--throttle", type=click.Choice(["background", "balanced", "fullspeed", "custom"]),
              default="balanced", show_default=True)
@click.option("--cpu", type=float, default=None, help="Custom CPU cap (0-100).")
@click.option("--ram", type=float, default=None, help="Custom RAM cap (0-100).")
@click.pass_context
def quickhash(ctx, throttle: str, cpu: float | None, ram: float | None) -> None:
    """Stage 1 — quick (xxhash) fingerprint of every un-hashed file."""
    conn = _open(ctx)
    gov = _make_governor(throttle, cpu, ram)
    try:
        r = fp.run_quickhash_stage(conn, governor=gov, on_progress=_progress_bar("quickhash"))
    finally:
        gov.stop()
    click.echo(f"quickhash: done={r['done']} errors={r['errors']} total={r['total']}")


@cli.command()
@click.option("--throttle", type=click.Choice(["background", "balanced", "fullspeed", "custom"]),
              default="balanced", show_default=True)
@click.option("--cpu", type=float, default=None)
@click.option("--ram", type=float, default=None)
@click.pass_context
def fullhash(ctx, throttle: str, cpu: float | None, ram: float | None) -> None:
    """Stage 2 — full SHA-256 for files whose quick_hash collides."""
    conn = _open(ctx)
    gov = _make_governor(throttle, cpu, ram)
    try:
        r = fp.run_fullhash_stage(conn, governor=gov, on_progress=_progress_bar("fullhash"))
    finally:
        gov.stop()
    click.echo(f"fullhash: done={r['done']} errors={r['errors']} total={r['total']}")


@cli.command()
@click.option("--throttle", type=click.Choice(["background", "balanced", "fullspeed", "custom"]),
              default="balanced", show_default=True)
@click.option("--cpu", type=float, default=None)
@click.option("--ram", type=float, default=None)
@click.option("--limit", type=int, default=None, help="Process at most N files (for testing).")
@click.pass_context
def analyze(ctx, throttle: str, cpu: float | None, ram: float | None, limit: int | None) -> None:
    """Stage 3 — decode, perceptual hash, EXIF, integrity score."""
    conn = _open(ctx)
    gov = _make_governor(throttle, cpu, ram)
    try:
        r = analyze_mod.run_analyze_stage(conn, governor=gov, on_progress=_progress_bar("analyze"), limit=limit)
    finally:
        gov.stop()
    click.echo(f"analyze: done={r['done']} errors={r['errors']} total={r['total']}")


@cli.command()
@click.option("--hamming", type=int, default=cluster_mod.DEFAULT_HAMMING_THRESHOLD, show_default=True,
              help="Hamming-distance threshold for near-duplicate clustering.")
@click.pass_context
def cluster(ctx, hamming: int) -> None:
    """Stage 4 — group exact + near-duplicate files into clusters."""
    conn = _open(ctx)
    r = cluster_mod.run_cluster_stage(conn, hamming_threshold=hamming)
    click.echo(f"cluster: clusters={r['clusters']} files={r['files']} non_singletons={r['non_singletons']}")


@cli.command()
@click.pass_context
def select(ctx) -> None:
    """Stage 5 — pick winner + donor + effective_date per cluster."""
    conn = _open(ctx)
    r = select_mod.run_select_stage(conn)
    click.echo(f"select: decided={r['decided']}")


@cli.command()
@click.option("--output", required=True, type=click.Path(file_okay=False))
@click.option("--dry-run", is_flag=True, help="Show what would happen without writing files.")
@click.pass_context
def organize(ctx, output: str, dry_run: bool) -> None:
    """Stage 6 — copy winners to OUTPUT and donate metadata."""
    conn = _open(ctx)
    r = org_mod.run_organize_stage(
        conn, Path(output), dry_run=dry_run, on_progress=_progress_bar("organize"),
    )
    click.echo(
        f"organize: written={r['written']} skipped={r['skipped']} "
        f"replaced={r['replaced']} offline={r['pending_offline']} errors={r['errors']}"
    )


# ---------- combined ----------

@cli.command(name="all")
@click.option("--source", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--output", required=True, type=click.Path(file_okay=False))
@click.option("--label", default=None)
@click.option("--throttle", type=click.Choice(["background", "balanced", "fullspeed", "custom"]),
              default="balanced", show_default=True)
@click.option("--cpu", type=float, default=None)
@click.option("--ram", type=float, default=None)
@click.option("--hamming", type=int, default=cluster_mod.DEFAULT_HAMMING_THRESHOLD, show_default=True)
@click.option("--dry-run", is_flag=True)
@click.pass_context
def all_(ctx, source: str, output: str, label: str | None, throttle: str,
         cpu: float | None, ram: float | None, hamming: int, dry_run: bool) -> None:
    """Run the entire pipeline end-to-end."""
    conn = _open(ctx)
    gov = _make_governor(throttle, cpu, ram)
    try:
        click.echo("=== Stage 0: scan ===")
        scan_mod.scan(conn, Path(source), label=label, on_progress=_progress_bar("scan"))
        click.echo("=== Stage 1: quickhash ===")
        fp.run_quickhash_stage(conn, governor=gov, on_progress=_progress_bar("quickhash"))
        click.echo("=== Stage 2: fullhash ===")
        fp.run_fullhash_stage(conn, governor=gov, on_progress=_progress_bar("fullhash"))
        click.echo("=== Stage 3: analyze ===")
        analyze_mod.run_analyze_stage(conn, governor=gov, on_progress=_progress_bar("analyze"))
        click.echo("=== Stage 4: cluster ===")
        cluster_mod.run_cluster_stage(conn, hamming_threshold=hamming)
        click.echo("=== Stage 5: select ===")
        select_mod.run_select_stage(conn)
        click.echo("=== Stage 6: organize ===")
        r = org_mod.run_organize_stage(
            conn, Path(output), dry_run=dry_run, on_progress=_progress_bar("organize"),
        )
    finally:
        gov.stop()
    click.echo(
        f"\nall: organize → written={r['written']} skipped={r['skipped']} "
        f"replaced={r['replaced']} offline={r['pending_offline']}"
    )


# ---------- reporting ----------

@cli.command()
@click.pass_context
def status(ctx) -> None:
    """Show pipeline progress and basic counts."""
    conn = _open(ctx)
    c = file_counts(conn)
    total_size = conn.execute("SELECT COALESCE(SUM(size), 0) FROM files").fetchone()[0]
    click.echo("DedupCollage status")
    click.echo("-------------------")
    click.echo(f"  total files       : {c['total']}")
    click.echo(f"  total bytes       : {format_bytes(total_size)}")
    click.echo(f"  scanned           : {c['scanned']}")
    click.echo(f"  quickhashed       : {c['quickhashed']}")
    click.echo(f"  fullhashed        : {c['fullhashed']}")
    click.echo(f"  analyzed          : {c['analyzed']}")
    click.echo(f"  clusters          : {c['clusters']}")
    click.echo(f"  winners chosen    : {c['winners']}")
    click.echo(f"  errors            : {c['errors']}")


@cli.command()
@click.pass_context
def drives(ctx) -> None:
    """List all drives known to the index."""
    conn = _open(ctx)
    mark_all_drives_offline(conn)
    rows = list_drives(conn)
    if not rows:
        click.echo("(no drives indexed yet)")
        return
    click.echo("id  serial    label                          source_root")
    click.echo("--  --------  -----------------------------  -------------------------")
    for r in rows:
        click.echo(
            f"{r['id']:<3} {r['volume_serial']:<9} "
            f"{(r['label'] or ''):<30} {r['source_root'] or ''}"
        )


def main() -> None:
    """Entry point referenced by ``[project.scripts]`` in pyproject.toml."""
    cli(prog_name="dedupcollage")


if __name__ == "__main__":
    main()
