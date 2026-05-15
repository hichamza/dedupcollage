"""Stage 4 — group exact duplicates and near-duplicates into clusters.

Two passes:
  1. Exact duplicates by SHA-256 (files in a Stage-2 collision group).
  2. Near-duplicates by perceptual hash, bucketing by top-16-bits of pHash
     and union-finding pairs whose Hamming distance is below the threshold.

Files not matched anywhere become their own singleton cluster — they pass
through as unique images.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from dedupcollage.db import transaction
from dedupcollage.perceptual import hamming_distance

log = logging.getLogger(__name__)

DEFAULT_HAMMING_THRESHOLD = 8


@dataclass
class _DSU:
    """Disjoint-set union over int file ids."""
    parent: dict[int, int]

    @classmethod
    def empty(cls) -> _DSU:
        return cls(parent={})

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def groups(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = defaultdict(list)
        for x in list(self.parent):
            out[self.find(x)].append(x)
        return out


def _cluster_exact(conn) -> _DSU:
    """Build DSU from sha256 equality groups."""
    dsu = _DSU.empty()
    cur = conn.execute("""
        SELECT id, sha256 FROM files
        WHERE sha256 IS NOT NULL
        ORDER BY sha256
    """)
    last_sha = None
    first_id: int | None = None
    for row in cur:
        sha = row["sha256"]
        fid = int(row["id"])
        if sha != last_sha:
            last_sha = sha
            first_id = fid
        elif first_id is not None:
            dsu.union(first_id, fid)
    return dsu


def _cluster_perceptual(conn, dsu: _DSU, threshold: int) -> None:
    """Bucket files by top 16 bits of phash, union near-duplicates within buckets."""
    rows = list(conn.execute("""
        SELECT id, phash, dhash FROM files
        WHERE phash IS NOT NULL
    """))
    if not rows:
        return

    buckets: dict[int, list[tuple[int, bytes, bytes]]] = defaultdict(list)
    for r in rows:
        ph = bytes(r["phash"])
        dh = bytes(r["dhash"]) if r["dhash"] is not None else ph
        prefix = int.from_bytes(ph[:2], "big")
        buckets[prefix].append((int(r["id"]), ph, dh))

    for bucket in buckets.values():
        n = len(bucket)
        if n < 2:
            continue
        for i in range(n):
            id_i, p_i, d_i = bucket[i]
            for j in range(i + 1, n):
                id_j, p_j, d_j = bucket[j]
                # Either hash being close is sufficient — they capture different
                # kinds of similarity (DCT-based vs gradient-based).
                if hamming_distance(p_i, p_j) <= threshold or hamming_distance(d_i, d_j) <= threshold:
                    dsu.union(id_i, id_j)


def run_cluster_stage(conn, *, hamming_threshold: int = DEFAULT_HAMMING_THRESHOLD) -> dict:
    """Build clusters across the entire current file index.

    Re-running this is safe — it clears existing cluster assignments first
    and recomputes from scratch.
    """
    with transaction(conn):
        conn.execute("UPDATE files SET cluster_id = NULL, is_winner = 0")
        conn.execute("DELETE FROM clusters")

    dsu = _cluster_exact(conn)
    _cluster_perceptual(conn, dsu, hamming_threshold)

    groups = dsu.groups()

    # Singletons too — every file gets a cluster.
    all_files = list(conn.execute("SELECT id, kind FROM files"))
    by_id = {int(r["id"]): r for r in all_files}

    cluster_assignments: list[tuple[int, list[int], str]] = []
    seen: set[int] = set()

    for members in groups.values():
        if not members:
            continue
        kind = by_id[members[0]]["kind"] if members[0] in by_id else None
        cluster_assignments.append((0, members, kind or "image"))
        seen.update(members)

    for fid, r in by_id.items():
        if fid not in seen:
            cluster_assignments.append((0, [fid], r["kind"] or "image"))

    # Insert clusters and update file rows in one transaction.
    cluster_count = 0
    with transaction(conn):
        for _, members, kind in cluster_assignments:
            cur = conn.execute(
                "INSERT INTO clusters (member_count, kind) VALUES (?, ?)",
                (len(members), kind),
            )
            cluster_id = int(cur.lastrowid)
            conn.executemany(
                "UPDATE files SET cluster_id = ? WHERE id = ?",
                [(cluster_id, fid) for fid in members],
            )
            cluster_count += 1

    log.info(
        "cluster: built %d clusters (exact+near pairs unioned) from %d files",
        cluster_count, len(by_id),
    )
    return {
        "clusters": cluster_count,
        "files": len(by_id),
        "non_singletons": sum(1 for ca in cluster_assignments if len(ca[1]) > 1),
    }
