"""Conflict copies a sync tool leaves beside a file two devices changed (Syncthing, OneDrive,
Dropbox): `original` names the file a copy stands for, `find` lists a vault's copies for
`cortex doctor` and `cortex lint`. The event log reads its copies itself (events._merged)."""

from __future__ import annotations

import os
import re
from collections.abc import Container
from pathlib import Path

from cortex.vault import PRIVATE_BROWSE_DIR

# Syncthing: <name>.sync-conflict-<YYYYMMDD>-<HHMMSS>-<device id: 7 of A-Z, 0-9>.<ext>
_SYNCTHING = re.compile(
    r"(?P<stem>.+?)\.sync-conflict-\d{8}-\d{6}-[A-Z0-9]{7}(?P<ext>\.[^.]*)?", re.I)
# Dropbox: <name> (conflicted copy <YYYY-MM-DD>).<ext>, <name> (<Device>'s conflicted copy ...)
_DROPBOX = re.compile(r"(?P<stem>.+?) \((?:[^()]*['’]s )?conflicted copy(?: \d{4}-\d\d-\d\d)?"
                      r"(?: \(\d+\))?\)(?P<ext>\.[^.]*)?", re.I)
# OneDrive: <name>-<COMPUTERNAME>.<ext>, maybe -<n> after it. The computer name (15 at most)
# holds an upper-case letter, as Windows shows it; page ids are all lower case, so a page's
# own hyphens never read as one.
_PC = re.compile(r"(?=[^.]*[A-Z])[A-Za-z0-9-]{1,15}(?:-\d{1,3})?")


def original(name: str, siblings: Container[str] = ()) -> str | None:
    """The file name a sync conflict copy called `name` stands for, or None when it is not
    one. A OneDrive name counts only when that file is among `siblings` (the names in its
    folder): `<name>-<PC>` alone is an ordinary name too."""
    for rx in (_SYNCTHING, _DROPBOX):
        if m := rx.fullmatch(name):
            return m["stem"] + (m["ext"] or "")
    stem, ext = os.path.splitext(name)
    i = stem.find("-")
    while i > 0:
        if _PC.fullmatch(stem[i + 1:]) and stem[:i] + ext in siblings:
            return stem[:i] + ext
        i = stem.find("-", i + 1)
    return None


def find(root: Path) -> list[tuple[str, str]]:
    """(copy, original) vault-relative paths, `/`-separated and sorted, of every conflict copy
    under `root`: pages, events, me/ files, anything but dot folders (.cortex's internals,
    .git) and browse/private/, which is never read or listed."""
    root = Path(root)
    private = os.path.normcase(os.path.join(root, *PRIVATE_BROWSE_DIR.split("/")))
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [n for n in dirs if not n.startswith(".")
                   and os.path.normcase(os.path.join(d, n)) != private]
        names = set(files)
        rel = Path(d).relative_to(root).as_posix()
        pre = "" if rel == "." else rel + "/"
        out += [(pre + n, pre + o) for n in files if (o := original(n, names)) is not None]
    return sorted(out)


def find_visible(vault) -> tuple[list[tuple[str, str]], int]:
    """(`find`'s copies a report may name, how many it may not): a private page's copies are
    counted, never named. Private as the index has it: the copy's or the original's own front
    matter or folder (relink._hidden: `private: true`, people pages, browse/private/), or an
    original under a private subject (planner.hidden: a private domain, area or concept)."""
    from cortex import planner
    from cortex.catalog import Catalog
    from cortex.relink import _hidden

    found = find(vault.root)
    if not found:
        return [], 0
    hide = planner.hidden(vault, Catalog.load(vault))

    def private(rel: str) -> bool:
        p = vault.root / rel
        try:
            return p.suffix == ".md" and p.is_file() and _hidden(
                vault.root, p, p.read_bytes().decode("utf-8", errors="replace"))
        except OSError:
            return True

    shown = [(c, o) for c, o in found
             if Path(o).stem not in hide and not private(c) and not private(o)]
    return shown, len(found) - len(shown)
