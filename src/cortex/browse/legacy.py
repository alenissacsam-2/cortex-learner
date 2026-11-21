"""The browse files from before the browsing store, read in once (F3).

`.cortex/browse/site-days.json` (the per-day site counts), `.cortex/browse/verdicts.json` (the
local model's verdicts by text hash) and `.cortex/browse/vectors-private.npz` (the private
search vectors) were replaced by `store.db` and `vectors-private.db`. `migrate` runs at the
app's start and before each browse import: a file still there is read into its store (a
torn site-days.json is set aside as `.bad-*`, as before) and renamed `<name>.migrated`, never
deleted; no other code reads these files. A forget still scrubs the `.migrated` backups
(`forget`, `vectors.prune`). `pending` lists what is left for `cortex doctor`.
"""

from __future__ import annotations

import json
import logging
from contextlib import closing

from cortex import vecstore
from cortex.browse import sites, store, vectors
from cortex.vault import Vault, VaultError

log = logging.getLogger(__name__)

DONE = ".migrated"
FILES = (sites.DAYS, sites.VERDICTS, vectors.NPZ)


def pending(vault: Vault) -> list[str]:
    """The legacy files not read in yet (vault-relative)."""
    return [rel for rel in FILES if (vault.root / rel).is_file()]


def migrate(vault: Vault) -> dict[str, str]:
    """Read every legacy file still there into its store, then rename it `.migrated`.
    {rel: "migrated" | "failed" | the name a torn site-days.json was set aside as}; {} when
    there was nothing to do. A failure leaves the file in place for the next start (and
    `cortex doctor`)."""
    out: dict[str, str] = {}
    for rel, read_in in ((sites.DAYS, _days), (sites.VERDICTS, _verdicts),
                         (vectors.NPZ, _vectors)):
        p = vault.root / rel
        if not p.is_file():
            continue
        try:
            if bad := read_in(vault):  # a torn site-days.json: set aside, nothing to rename
                out[rel] = bad
                continue
            vault.move(p, p.with_name(p.name + DONE))
            out[rel] = "migrated"
        except (OSError, ValueError, VaultError) as e:
            log.warning("browse: %s not read in: %s", p.name, type(e).__name__)
            out[rel] = "failed"
    return out


def _days(vault: Vault) -> str | None:
    """site-days.json into the store's site_days (`store.migrate_days`, once per store); the
    name a torn file was set aside as (R2-7), else None."""
    with closing(store.connect(vault)) as con:
        if store.get_meta(con, "days_migrated"):  # read in by an earlier version
            return None
        days, bad = sites._json_days(vault)  # before the store's write lock: a big file is slow
        store.migrate_days(con, days, sites._rows(days))
    return bad


def _verdicts(vault: Vault) -> None:
    """verdicts.json into the store's verdicts; a label the store already has wins."""
    try:
        old = json.loads((vault.root / sites.VERDICTS).read_text(encoding="utf-8"))
    except ValueError:
        old = {}
    have = store.verdicts(vault)
    got = {k: v for k, v in (old.items() if isinstance(old, dict) else ())
           if isinstance(k, str) and isinstance(v, str) and k not in have}
    with closing(store.connect(vault)) as con:
        store.put_verdicts(con, got)


def _vectors(vault: Vault) -> None:
    """vectors-private.npz into vectors-private.db, once (the store's `migrated` mark). A file
    built by another model or chunking, or unreadable, has nothing to read in."""
    old, sig, dim = vectors._npz(vault)
    if not old:
        return
    with closing(vecstore.connect(vault.root / vectors.CACHE)) as con:
        if vecstore.prepare(con, vectors._want(dim)):
            with vecstore.tx(con):
                vecstore.set_meta(con, "sig", None)
        if vecstore.get_meta(con, "migrated"):
            return
        vecstore.put(con, {p: (h, True, s, v) for p, (h, s, v) in old.items()})
        with vecstore.tx(con):
            vecstore.set_meta(con, "sig", sig)
            vecstore.set_meta(con, "migrated", True)
