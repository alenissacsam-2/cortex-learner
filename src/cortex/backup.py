"""Vault backup copies: one zip of the whole vault, .git included, and the durable browse stores
in .cortex/browse (site-days.json, sites.json, verdicts.json, state.json, inbox-archive/, the
receiver's not-yet-imported inbox-*.jsonl, filtered like the archive), which hold browsing the
browsers have already forgotten. The SQLite stores (DBS: the browsing store and the two vector
stores, which take minutes to embed again) are copied by SQLite's own backup, a consistent
snapshot, never the live file and its WAL. Left out: old backups, the pairing token, the local
chat history (.cortex/browse/chat), lock files, the rebuilt dashboard and stray *.tmp files.
A zip holds private browsing: keep it as private as the vault. The last KEEP zips are kept."""

from __future__ import annotations

import os
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

KEEP = 10  # owner's choice (verdict Q11 a)
# store.PATH, vectors.CACHE and semantic.CACHE (not imported: numpy is slow to load)
DBS = (".cortex/browse/store.db", ".cortex/browse/vectors-private.db", ".cortex/vectors.db")


def backup(root: Path, dest_dir: Path | None = None) -> Path:
    """Zip `root` into dest_dir (default <root>/.cortex/backups); returns the zip path."""
    root = Path(root).resolve()
    skip = root / ".cortex" / "backups"
    browse = root / ".cortex" / "browse"
    dest_dir = Path(dest_dir).resolve() if dest_dir else skip
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"cortex-{datetime.now():%Y%m%d-%H%M%S}.zip"
    tmp = dest.with_suffix(".zip.tmp")
    skip_dirs = (skip, dest_dir, browse / "chat")
    dbs = [root / d for d in DBS]
    skip_files = (tmp, dest, root / ".cortex" / "lock", root / ".cortex" / "dashboard.html",
                  browse / "lock", browse / "token",
                  *(Path(f"{db}{s}") for db in dbs for s in ("", "-wal", "-shm")))
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for dirpath, dirnames, filenames in os.walk(root):
                d = Path(dirpath)
                dirnames[:] = [n for n in dirnames if d / n not in skip_dirs]
                for name in filenames:
                    p = d / name
                    if p in skip_files or p.suffix == ".tmp":
                        continue
                    z.write(p, p.relative_to(root).as_posix())
            for db in [db for db in dbs if db.is_file()]:
                snap = tmp.with_suffix(".db.tmp")
                try:
                    with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as src, \
                            closing(sqlite3.connect(snap)) as dst:
                        src.backup(dst)
                    z.write(snap, db.relative_to(root).as_posix())
                finally:
                    snap.unlink(missing_ok=True)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    for old in sorted(dest_dir.glob("cortex-*.zip"))[:-KEEP]:  # names sort by time
        old.unlink(missing_ok=True)
    return dest


def same_drive(a: Path, b: Path) -> bool:
    return Path(a).stat().st_dev == Path(b).stat().st_dev
