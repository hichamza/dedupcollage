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
    """include(relpath) -> True if relpath is checked or under a checked dir."""
    norm = {c.strip("/") for c in checked}

    def include(relpath: str) -> bool:
        r = relpath.strip("/")
        if r in norm or "" in norm and r == "":
            return True
        return any(r == c or r.startswith(c + "/") for c in norm if c != "")

    return include
