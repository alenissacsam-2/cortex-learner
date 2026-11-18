"""Vault backup copies: one zip of the whole vault, .git included; old backups, the browse
receiver's folder (.cortex/browse: raw extension records, the pairing token), the rebuilt
dashboard and stray *.tmp files excluded. The last KEEP zips are kept."""

from __future__ import annotations

import os
import zipfile
from datetime import datetime
from pathlib import Path

KEEP = 10  # owner's choice (verdict Q11 a)


def backup(root: Path, dest_dir: Path | None = None) -> Path:
    """Zip `root` into dest_dir (default <root>/.cortex/backups); returns the zip path."""
    root = Path(root).resolve()
    skip = root / ".cortex" / "backups"
    browse = root / ".cortex" / "browse"
    dest_dir = Path(dest_dir).resolve() if dest_dir else skip
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"cortex-{datetime.now():%Y%m%d-%H%M%S}.zip"
    tmp = dest.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for dirpath, dirnames, filenames in os.walk(root):
                d = Path(dirpath)
                dirnames[:] = [n for n in dirnames if d / n not in (skip, dest_dir, browse)]
                for name in filenames:
                    p = d / name
                    if p not in (tmp, dest, root / ".cortex" / "lock",
                                 root / ".cortex" / "dashboard.html") and p.suffix != ".tmp":
                        z.write(p, p.relative_to(root).as_posix())
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    for old in sorted(dest_dir.glob("cortex-*.zip"))[:-KEEP]:  # names sort by time
        old.unlink(missing_ok=True)
    return dest


def same_drive(a: Path, b: Path) -> bool:
    return Path(a).stat().st_dev == Path(b).stat().st_dev
