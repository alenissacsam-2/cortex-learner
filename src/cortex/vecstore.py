"""Vector stores: one SQLite file (WAL) per cache under `.cortex`, a row per page.

A row holds what a page's vectors were made from (`manifest`: semantic's path, mtime and
size; the browse search's content hash), whether every chunk of it is built, its chunk starts
(int32) and unit vectors (float16). A save upserts and deletes rows, so adding a page writes
that page only (the .npz caches before it rewrote every row). An upsert keeps the row with
more chunks built for the same manifest, so a server and a `cortex model build` never undo
each other's work. `meta` holds the settings the rows were made with (other settings empty
the table), an `epoch` made with the file and two counters a reader polls as its change stamp:
`gen` (each row carries the gen it was saved at, so a reader fetches only the newer rows) and
`dels` (a delete bumps it; a reader then drops the ids that went). A deleted row is overwritten
(secure_delete) and the WAL checkpointed, so a forgotten page leaves nothing behind. A
disposable cache: a damaged file (unreadable, or failing the first open's quick_check in a
process) is moved aside by cortex.quarantine and started again; its pages are embedded again.
Written by SQLite's transactions, not cortex.vault's temp files, as cortex.browse.store.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from cortex import quarantine

log = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS vecs(id TEXT PRIMARY KEY, manifest TEXT NOT NULL,
    full INTEGER NOT NULL, n INTEGER NOT NULL, starts BLOB NOT NULL, vecs BLOB NOT NULL,
    gen INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS vecs_gen ON vecs(gen);
"""
# the row with more chunks built wins for the same manifest (the chunking is deterministic)
_UPSERT = ("INSERT INTO vecs VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
           "manifest = excluded.manifest, full = excluded.full, n = excluded.n, "
           "starts = excluded.starts, vecs = excluded.vecs, gen = excluded.gen "
           "WHERE excluded.manifest != vecs.manifest "
           "OR (excluded.full, excluded.n) > (vecs.full, vecs.n)")
SUFFIXES = quarantine.SUFFIXES
COLS = ["id", "manifest", "full", "n", "starts", "vecs", "gen"]  # the vecs table, as _DDL makes it


def _open(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA secure_delete=ON")  # a forgotten page's row is overwritten
        con.executescript(_DDL)
        quarantine.check(con, path, "vecs", COLS)  # once per process
        if get_meta(con, "epoch") is None:
            with tx(con):
                con.execute("INSERT OR IGNORE INTO meta VALUES ('epoch', ?)",
                            (json.dumps(os.urandom(8).hex()),))
    except BaseException:
        con.close()
        raise
    return con


def connect(path: Path, create: bool = True, vault=None) -> sqlite3.Connection:
    """The store at `path`, made when missing; a damaged one (`quarantine.torn`) is moved
    aside through `vault` (the vault holding `path` when None) and made again. With `create`
    False (a reader) a damaged one raises sqlite3.DatabaseError instead, as does one that
    cannot be moved (held open by another program)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _open(path)
    except sqlite3.DatabaseError as e:
        if not create or not quarantine.torn(e):  # locked, busy, no access: not damaged
            raise
        if not heal(path, e, vault):
            raise
        return _open(path)


def heal(path: Path, e: BaseException, vault=None) -> bool:
    """Move the damaged store at `path` aside (`quarantine.heal`); False when it stayed."""
    return quarantine.heal(path, e, "its pages are embedded again", vault)


@contextmanager
def tx(con: sqlite3.Connection):
    """A write transaction that takes the write lock up front (a read-then-write one could
    not upgrade while another process writes)."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def get_meta(con: sqlite3.Connection, k: str, default=None):
    row = con.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return json.loads(row[0]) if row else default


def set_meta(con: sqlite3.Connection, k: str, v) -> None:
    con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, json.dumps(v)))


def _bump(con: sqlite3.Connection, k: str) -> int:
    n = int(get_meta(con, k, 0)) + 1
    set_meta(con, k, n)
    return n


def prepare(con: sqlite3.Connection, want: dict) -> bool:
    """Empty the rows when they were made with settings other than `want`; True if it did."""
    if get_meta(con, "settings") == want:
        return False
    with tx(con):
        if get_meta(con, "settings") == want:  # another process did it meanwhile
            return False
        if con.execute("DELETE FROM vecs").rowcount:
            _bump(con, "dels")
        set_meta(con, "settings", want)
    return True


def stamp(con: sqlite3.Connection) -> tuple:
    """(epoch, gen, dels): changes whenever any process saves or deletes a row."""
    got = dict(con.execute("SELECT k, v FROM meta WHERE k IN ('epoch', 'gen', 'dels')"))
    return tuple(json.loads(got[k]) if k in got else 0 for k in ("epoch", "gen", "dels"))


def rows(con: sqlite3.Connection, dim: int, since: int | None = None):
    """(id, manifest, full, starts int32, vecs float16 (n, dim)) of every row, or of those
    saved after gen `since`; the arrays are views of the stored bytes."""
    q = "SELECT id, manifest, full, starts, vecs FROM vecs"
    for i, m, full, s, v in (con.execute(q) if since is None
                             else con.execute(q + " WHERE gen > ?", (since,))):
        yield (i, m, bool(full), np.frombuffer(s, np.int32),
               np.frombuffer(v, np.float16).reshape(-1, dim))


def heads(con: sqlite3.Connection, dim: int) -> tuple[list[str], np.ndarray]:
    """(ids, float32 (n, dim)) of each row's first vector, the rest of each blob unread."""
    got = con.execute("SELECT id, substr(vecs, 1, ?) FROM vecs", (2 * dim,)).fetchall()
    m = np.frombuffer(b"".join(v for _, v in got), np.float16).reshape(-1, dim)
    return [i for i, _ in got], m.astype(np.float32)


def manifests(con: sqlite3.Connection) -> dict[str, str]:
    return dict(con.execute("SELECT id, manifest FROM vecs"))


def sync(con: sqlite3.Connection, dim: int, st0, cache: dict, parse) -> tuple[tuple, bool]:
    """Bring `cache` ({id: (parse(manifest), full, starts, vecs)}, a process's copy as of
    stamp `st0`) up to the store: every row on the first call or after the file was made
    again, else only the rows saved and the ids deleted since. (stamp, whether it changed)."""
    st = stamp(con)
    if st == st0:
        return st, False
    if st0 is None or st0[0] != st[0]:
        cache.clear()
        new = rows(con, dim)
    else:
        new = rows(con, dim, st0[1])
        if st[2] != st0[2]:
            have = manifests(con)
            for i in [i for i in cache if i not in have]:
                del cache[i]
    for i, m, full, s, v in new:
        cache[i] = (parse(m), full, s, v)
    return st, True


def put(con: sqlite3.Connection, items: dict) -> None:
    """Upsert {id: (manifest, full, starts, vecs)} in one transaction."""
    if not items:
        return
    with tx(con):
        g = _bump(con, "gen")
        con.executemany(_UPSERT, [
            (i, m, int(full), len(s), np.asarray(s, np.int32).tobytes(),
             np.asarray(v, np.float16).tobytes(), g)
            for i, (m, full, s, v) in items.items()])


def delete(con: sqlite3.Connection, ids) -> int:
    """Delete the rows of `ids`; how many went. Their old page images leave the WAL too."""
    ids = list(ids)
    if not ids:
        return 0
    with tx(con):
        n = sum(con.execute("DELETE FROM vecs WHERE id = ?", (i,)).rowcount for i in ids)
        if n:
            _bump(con, "dels")
    if n:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return n


def stack(cache: dict, dim: int) -> tuple:
    """(ids, row -> page index, row chunk start, float32 rows) of `cache` ({id: (manifest,
    full, starts, vecs)}) as one contiguous matrix; each entry is rebound to views of it, so
    memory holds the matrix once."""
    ids = list(cache)
    sizes = [len(cache[i][2]) for i in ids]
    cut = np.concatenate([[0], np.cumsum(sizes, dtype=np.int64)])
    m = np.empty((int(cut[-1]), dim), np.float32)
    start = np.empty(int(cut[-1]), np.int32)
    for k, i in enumerate(ids):
        meta, full, s, v = cache[i]
        a, b = cut[k], cut[k + 1]
        m[a:b], start[a:b] = v, s
        cache[i] = (meta, full, start[a:b], m[a:b])
    return ids, np.repeat(np.arange(len(ids), dtype=np.int32), sizes), start, m
