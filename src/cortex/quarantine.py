"""R29b: a damaged SQLite store under `.cortex/` (the vectors, the chat import's state) is moved
aside, never deleted, and started again. A sync tool that copies a store and its -wal/-shm
apart, or a crash mid-write, can leave a file SQLite reads as corrupt; before this a store
like that was deleted and made again (the vectors) or failed on every query.

`check` runs `PRAGMA quick_check` once per store per process (at its first open) and compares
the store's table with the one the code makes; `torn` tells damage from a locked or busy
file. `move_aside` renames the store and its sidecars to `<name>.damaged-<UTC stamp>[-wal|-shm]`
through cortex.vault (vault.move: lock, temp file, os.replace with retry), putting back what
moved when a file is held open. `found` lists the copies for `cortex doctor` (one WARN
naming them), `cortex backup` (left out: damaged, and the chat import's copy holds raw chat
turns) and `cortex forget` (deleted: a forgotten page's rows or chat may sit in them, and a
damaged file cannot be pruned reliably). No numpy here: backup and doctor import it.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

SUFFIXES = ("", "-wal", "-shm")  # a store's file and its WAL sidecars
STORE = re.compile(r".+\.damaged-\d{8}T\d{6}Z")  # a moved-aside store (its sidecars add -wal...)
DIRS = (".cortex", ".cortex/browse")  # where the stores live
_CHECKED: set[str] = set()  # stores that passed `check` in this process


class Damaged(sqlite3.DatabaseError):
    """quick_check failed, or the store's table is not the one the code makes."""


def torn(e: BaseException) -> bool:
    """`e` says the file is damaged; a locked, busy or unreachable one is not."""
    if isinstance(e, Damaged):
        return True
    code = (getattr(e, "sqlite_errorcode", None) or 0) & 0xFF
    return isinstance(e, sqlite3.DatabaseError) and code in (sqlite3.SQLITE_CORRUPT,
                                                             sqlite3.SQLITE_NOTADB)


def check(con: sqlite3.Connection, path: Path, table: str, cols: list[str]) -> None:
    """Damaged unless `table` has `cols` and `PRAGMA quick_check` passes; the check runs once
    per store per process (quick_check reads every page: tens of ms on a 5,000-page store)."""
    key = str(path)
    if key in _CHECKED:
        return
    have = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    if have != cols:
        raise Damaged(f"table {table} has columns {have}")
    got = [r[0] for r in con.execute("PRAGMA quick_check(3)")]
    if got != ["ok"]:
        raise Damaged("quick_check: " + "; ".join(got))
    _CHECKED.add(key)


def _vault(path: Path, vault=None):
    from cortex.vault import Vault

    if vault is not None:
        return vault
    return Vault(next((p.parent for p in path.parents if p.name == ".cortex"), path.parent))


def move_aside(path: Path, vault=None) -> Path:
    """Rename the store at `path` and its sidecars to `<name>.damaged-<UTC stamp>` through
    cortex.vault; the new path. A file held open by another program (VaultError) or an
    OSError puts back what moved, so a store never parts from its WAL, and raises."""
    from cortex.vault import VaultError

    v = _vault(path, vault)
    dst = path.with_name(f"{path.name}.damaged-{datetime.now(UTC):%Y%m%dT%H%M%SZ}")
    done: list[str] = []
    try:
        with v.lock():
            for s in SUFFIXES:
                if Path(f"{path}{s}").is_file():
                    v.move(Path(f"{path}{s}"), Path(f"{dst}{s}"))
                    done.append(s)
    except (OSError, VaultError):
        for s in SUFFIXES:
            try:
                if s in done:
                    v.move(Path(f"{dst}{s}"), Path(f"{path}{s}"))
                elif Path(f"{path}{s}").is_file():  # a copy made before the source stuck
                    v.remove(Path(f"{dst}{s}"))
            except (OSError, VaultError):
                log.warning("%s: could not put %s back", path.name, Path(f"{dst}{s}").name)
        raise
    _CHECKED.discard(str(path))
    return dst


def heal(path: Path, e: BaseException, what: str, vault=None) -> bool:
    """Move a damaged store aside (`move_aside`) and log one warning; False (with a warning)
    when it could not be moved, so the caller keeps raising `e`."""
    from cortex.vault import VaultError

    try:
        dst = move_aside(path, vault)
    except (OSError, VaultError) as err:
        log.warning("%s is damaged (%s) and could not be moved aside: %s", path.name, e, err)
        return False
    log.warning("%s was damaged (%s): moved aside as %s and started again; %s. Delete the "
                "copy once things work again (`cortex doctor` names it).", path.name, e,
                dst.name, what)
    return True


def found(root: Path) -> list[Path]:
    """Every moved-aside store under the vault at `root`, its sidecars included, sorted."""
    return sorted(p for d in DIRS for p in (Path(root) / d).glob("*.damaged-*")
                  if p.is_file() and STORE.fullmatch(p.name.removesuffix("-wal")
                                                     .removesuffix("-shm")))
