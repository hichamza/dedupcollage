"""Stage 5 — winner + metadata donor + effective_date per cluster.

For each cluster:
  * winner = highest ``quality_score``, ties broken by larger ``size`` then by
    has_full_exif preference.
  * donor  = the member with the most complete EXIF (capture + serial + GPS,
    in that priority order). May coincide with the winner.
  * effective_date / effective_date_source = best across all members
    (``exif_taken`` > ``exif_created`` > ``mtime``).

Files that were the loser get ``is_winner = 0``; the winner gets ``is_winner = 1``.
"""

from __future__ import annotations

import logging

from dedupcollage.db import transaction
from dedupcollage.metadata import DATE_SOURCE_RANK

log = logging.getLogger(__name__)


def _member_donor_rank(row) -> tuple[int, int, int]:
    """Rank for donor selection. Higher tuple wins.

    Components, in priority order:
      1. has_full_exif (0/1)
      2. has GPS-like extras — approximated by has lens_model + serial (0/1/2)
      3. quality_score (rounded) as a final tiebreak
    """
    has_full = int(row["has_full_exif"] or 0)
    extras = int(bool(row["camera_serial"])) + int(bool(row["lens_model"]))
    return (has_full, extras, int(row["quality_score"] or 0))


def _member_winner_rank(row) -> tuple[float, int, int]:
    """Rank for winner selection. Higher tuple wins."""
    score = float(row["quality_score"] or 0.0)
    size = int(row["size"] or 0)
    has_full = int(row["has_full_exif"] or 0)
    return (score, size, has_full)


def _best_effective_date(members: list) -> tuple[str | None, str | None]:
    best_rank = 0
    best_date: str | None = None
    best_source: str | None = None
    for r in members:
        rank = DATE_SOURCE_RANK.get(r["date_source"], 0)
        if rank > best_rank and r["effective_date"]:
            best_rank = rank
            best_date = r["effective_date"]
            best_source = r["date_source"]
    return best_date, best_source


def run_select_stage(conn) -> dict:
    """Re-pick winners and donors across every cluster."""
    clusters = list(conn.execute("SELECT id FROM clusters"))
    decided = 0
    with transaction(conn):
        conn.execute("UPDATE files SET is_winner = 0")
        for c in clusters:
            cid = int(c["id"])
            members = list(conn.execute(
                "SELECT * FROM files WHERE cluster_id = ?", (cid,)
            ))
            if not members:
                continue
            winner = max(members, key=_member_winner_rank)
            donor = max(members, key=_member_donor_rank)
            edate, esource = _best_effective_date(members)
            conn.execute(
                "UPDATE clusters SET winner_id = ?, donor_id = ?, "
                "effective_date = ?, effective_date_source = ? WHERE id = ?",
                (int(winner["id"]), int(donor["id"]), edate, esource, cid),
            )
            conn.execute(
                "UPDATE files SET is_winner = 1 WHERE id = ?", (int(winner["id"]),),
            )
            decided += 1
    log.info("select: decided winners for %d clusters", decided)
    return {"decided": decided}
