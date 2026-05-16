"""Discovery tree: per-directory media counts and the noise-flag rule.

Pure data + logic, no I/O. ``scan.discover()`` feeds it ``(relpath,
own_total, own_media)`` rows; counts roll up to ancestors and the
low-media-ratio flag is computed per node.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MIN_FILES = 20
MEDIA_RATIO = 0.01


@dataclass
class DirNode:
    relpath: str                       # "" = source root
    name: str                          # last path segment ("" for root)
    own_total: int = 0
    own_media: int = 0
    total_files: int = 0               # recursive (filled by build_tree)
    media_files: int = 0               # recursive
    children: dict[str, DirNode] = field(default_factory=dict)

    @property
    def flagged(self) -> bool:
        """True => low-media-ratio noise candidate (starts unchecked)."""
        if self.total_files < MIN_FILES:
            return False
        return (self.media_files / self.total_files) < MEDIA_RATIO

    def child(self, name: str) -> DirNode:
        return self.children[name]


def _ensure(root: DirNode, relpath: str) -> DirNode:
    if relpath == "":
        return root
    node = root
    acc = ""
    for seg in relpath.replace("\\", "/").split("/"):
        acc = seg if acc == "" else f"{acc}/{seg}"
        if seg not in node.children:
            node.children[seg] = DirNode(relpath=acc, name=seg)
        node = node.children[seg]
    return node


def build_tree(rows: list[tuple[str, int, int]]) -> DirNode:
    """Build the tree and roll up recursive counts.

    ``rows`` = list of (relpath, own_total_files, own_media_files).
    Intermediate dirs not present in rows are created with zero own counts.
    """
    root = DirNode(relpath="", name="")
    for relpath, own_total, own_media in rows:
        node = _ensure(root, relpath)
        node.own_total += own_total
        node.own_media += own_media

    def rollup(n: DirNode) -> tuple[int, int]:
        t, m = n.own_total, n.own_media
        for c in n.children.values():
            ct, cm = rollup(c)
            t += ct
            m += cm
        n.total_files, n.media_files = t, m
        return t, m

    rollup(root)
    return root
