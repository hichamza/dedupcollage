"""Pure helpers mapping the discovery tree <-> include selection.

Kept out of the Qt widgets so it is unit-testable headlessly.
"""

from __future__ import annotations

from dedupcollage.discovery import DirNode


def default_checked(root: DirNode, *, skip_noise: bool) -> set[str]:
    """Relpaths checked by default: all dirs, minus flagged when skip_noise."""
    checked: set[str] = set()

    def visit(n: DirNode) -> None:
        if not (skip_noise and n.flagged):
            checked.add(n.relpath)
        for c in n.children.values():
            visit(c)

    visit(root)
    return checked


def make_include(checked: set[str]):
    """Build the scan ``include(relpath)`` predicate from a checked set.

    ``checked`` is the full set of selected directory relpaths. The
    discovery tree enumerates every directory and ``default_checked`` /
    the GUI list each one individually, so membership is **exact**: a
    directory is walked iff its own relpath is in the set. This means
    unchecking a child excludes it even when its parent stays checked
    (no subtree-prefix expansion, so no accidental re-inclusion).
    """
    norm = {c.strip("/") for c in checked}

    # NOTE: intentionally EXACT membership (not prefix). The CLI uses a
    # prefix predicate (cli.py scan --exclude); do not unify — the GUI
    # enumerates every dir, so prefix matching would re-include unchecked
    # children of a checked parent.
    def include(relpath: str) -> bool:
        return relpath.strip("/") in norm

    return include
