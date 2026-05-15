"""Stage 6 — materialize the clean output tree.

For each cluster's winner we resolve a target path under the user's output
root, copy the source file via a temporary name (atomic rename), optionally
inject donor metadata via exiftool, and write a row to ``manifest.csv``.

Re-runs are idempotent. If a cluster's winner has changed since the last
organize (because a new drive was added and re-clustered), the old target
file is moved to ``_replaced/YYYY-MM-DD/`` before the new winner is written.
"""

from __future__ import annotations

import csv
import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from dedupcollage._paths import exiftool_path
from dedupcollage.db import transaction
from dedupcollage.metadata import DATE_SOURCE_MTIME
from dedupcollage.utils import iso_now, safe_filename

log = logging.getLogger(__name__)

MANIFEST_HEADER = [
    "cluster_id", "winner_path", "donor_path", "target_path",
    "quality_score", "date_source", "organized_at",
]


def _parse_effective_date(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _device_slug(make: str | None, model: str | None, serial: str | None) -> str:
    parts: list[str] = []
    if make:
        parts.append(safe_filename(make))
    if model:
        m = safe_filename(model)
        if parts and m.lower().startswith(parts[-1].lower()):
            # Avoid "Canon Canon EOS R5"
            parts[-1] = m
        else:
            parts.append(m)
    if serial:
        parts.append(safe_filename(serial))
    return "-".join(p for p in parts if p) or "unknown-device"


def resolve_target_path(
    output_root: Path,
    cluster_effective_date: str | None,
    cluster_effective_date_source: str | None,
    donor_make: str | None,
    donor_model: str | None,
    donor_serial: str | None,
    winner_sha: str | None,
    winner_ext: str,
    winner_mtime: float | None = None,
) -> Path:
    """Build the canonical target path for a winner file.

    Layout: ``{output_root}/{YYYY-MM-DD}{-s if mtime}-{device}/{HHMMSS}_{sha8}.{ext}``.
    Falls back to ``unknown-date-unknown-device/`` when nothing usable is available.
    """
    dt = _parse_effective_date(cluster_effective_date)
    save_marker = "-s" if cluster_effective_date_source == DATE_SOURCE_MTIME else ""

    if dt:
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H%M%S")
    elif winner_mtime:
        dt = datetime.fromtimestamp(winner_mtime)
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H%M%S")
        save_marker = "-s"
    else:
        date_str = "unknown-date"
        time_str = "000000"
        save_marker = ""

    device = _device_slug(donor_make, donor_model, donor_serial)
    folder_name = f"{date_str}{save_marker}-{device}" if date_str != "unknown-date" else f"{date_str}-{device}"
    folder = output_root / folder_name

    sha8 = (winner_sha or "00000000")[:8]
    ext = winner_ext.lower().lstrip(".") or "bin"
    return folder / f"{time_str}_{sha8}.{ext}"


def _inject_donor_metadata(donor: Path, target: Path) -> bool:
    """Run exiftool to copy all writable tags from donor into target. Returns success."""
    et = exiftool_path()
    if et is None:
        log.debug("exiftool not available, skipping metadata donation for %s", target)
        return False
    try:
        subprocess.run(
            [
                str(et), "-overwrite_original",
                "-tagsFromFile", str(donor),
                "-all:all", str(target),
            ],
            check=True, capture_output=True, timeout=30,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        log.warning("exiftool donation failed for %s: %s", target, e)
        return False


def _move_to_replaced(target: Path, replaced_root: Path) -> None:
    """Move an existing target file to ``replaced_root/YYYY-MM-DD/{name}``."""
    if not target.exists():
        return
    day_folder = replaced_root / datetime.now().strftime("%Y-%m-%d")
    day_folder.mkdir(parents=True, exist_ok=True)
    dest = day_folder / target.name
    n = 1
    while dest.exists():
        dest = day_folder / f"{target.stem}_{n}{target.suffix}"
        n += 1
    shutil.move(str(target), str(dest))
    log.info("moved replaced winner to %s", dest)


def run_organize_stage(
    conn,
    output_root: Path,
    *,
    dry_run: bool = False,
    on_progress=None,
) -> dict:
    """Copy winners to ``output_root`` and inject donor metadata.

    Returns a dict with counts of written / skipped / replaced / pending_offline.
    """
    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    replaced_root = output_root / "_replaced"

    manifest_path = output_root / "manifest.csv"
    new_manifest = not manifest_path.exists()

    written = 0
    skipped = 0
    replaced = 0
    pending_offline = 0
    errors = 0

    cur = conn.execute("""
        SELECT c.id AS cluster_id,
               c.winner_id,
               c.donor_id,
               c.effective_date,
               c.effective_date_source,
               c.organized_from_id,
               c.target_path AS current_target,
               w.path AS winner_path,
               w.sha256 AS winner_sha,
               w.mtime AS winner_mtime,
               d.path AS donor_path,
               d.camera_make AS donor_make,
               d.camera_model AS donor_model,
               d.camera_serial AS donor_serial
        FROM clusters c
        JOIN files w ON w.id = c.winner_id
        LEFT JOIN files d ON d.id = c.donor_id
        WHERE c.winner_id IS NOT NULL
    """)
    clusters = list(cur)

    manifest_rows: list[dict] = []
    done = 0

    for c in clusters:
        try:
            winner_path = Path(c["winner_path"])
            if not winner_path.exists():
                pending_offline += 1
                continue

            ext = winner_path.suffix or ""
            target = resolve_target_path(
                output_root,
                c["effective_date"],
                c["effective_date_source"],
                c["donor_make"], c["donor_model"], c["donor_serial"],
                c["winner_sha"], ext, c["winner_mtime"],
            )

            current = Path(c["current_target"]) if c["current_target"] else None

            if (current and current == target and current.exists()
                    and int(c["organized_from_id"] or 0) == int(c["winner_id"])):
                skipped += 1
                continue

            if dry_run:
                log.info("DRY would write %s", target)
                done += 1
                continue

            target.parent.mkdir(parents=True, exist_ok=True)

            if current and current != target and current.exists():
                _move_to_replaced(current, replaced_root)
                replaced += 1

            tmp = target.with_suffix(target.suffix + ".tmp")
            shutil.copy2(str(winner_path), str(tmp))
            if c["donor_path"] and c["donor_path"] != c["winner_path"]:
                _inject_donor_metadata(Path(c["donor_path"]), tmp)
            if target.exists():
                target.unlink()
            tmp.rename(target)

            with transaction(conn):
                conn.execute(
                    "UPDATE clusters SET organized_from_id = ?, organized_at = ?, target_path = ? WHERE id = ?",
                    (int(c["winner_id"]), iso_now(), str(target), int(c["cluster_id"])),
                )
                conn.execute(
                    "UPDATE files SET target_path = ? WHERE id = ?",
                    (str(target), int(c["winner_id"])),
                )

            manifest_rows.append({
                "cluster_id": c["cluster_id"],
                "winner_path": str(winner_path),
                "donor_path": c["donor_path"] or "",
                "target_path": str(target),
                "quality_score": "",  # filled in below from a quick lookup if needed
                "date_source": c["effective_date_source"] or "",
                "organized_at": iso_now(),
            })
            written += 1
        except Exception as e:  # noqa: BLE001
            log.warning("organize failed for cluster %s: %s", c["cluster_id"], e)
            errors += 1
        finally:
            done += 1
            if on_progress:
                on_progress(done, len(clusters))

    if manifest_rows and not dry_run:
        mode = "w" if new_manifest else "a"
        with open(manifest_path, mode, newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=MANIFEST_HEADER)
            if new_manifest:
                writer.writeheader()
            writer.writerows(manifest_rows)

    log.info(
        "organize: written=%d skipped=%d replaced=%d offline=%d errors=%d",
        written, skipped, replaced, pending_offline, errors,
    )
    return {
        "written": written,
        "skipped": skipped,
        "replaced": replaced,
        "pending_offline": pending_offline,
        "errors": errors,
        "total_clusters": len(clusters),
    }
