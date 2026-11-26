"""The packaged sample vault (a made-up learner, Sam): `seed` copies it into a vault where
files are missing and logs a few seed events; `remove` deletes only `sample: true` pages,
drops their catalog entries and voids the seed events (history is never deleted)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from keepfold.catalog import Catalog
from keepfold.catalog import sync as catalog_sync
from keepfold.events import EventLog, live, void_event
from keepfold.vault import Vault, VaultError

SAMPLE_DIR = Path(__file__).parent / "sample_vault"
KEY = "sample:"
# (key, kind, days ago, fields): server-stamped fields come from EventLog.append
SEED = [
    ("sample:1", "grade", 9, {"object": "concept:loops", "verdict": "wrong", "hints": 1,
                              "mistakes": ["programming.off-by-one-loop"]}),
    ("sample:2", "grade", 6, {"object": "concept:loops", "verdict": "correct", "hints": 0}),
    ("sample:3", "grade", 3, {"object": "concept:loops", "verdict": "correct", "hints": 0}),
    ("sample:4", "grade", 5, {"object": "concept:functions", "verdict": "correct",
                              "hints": 0}),
    ("sample:5", "grade", 1, {"object": "concept:recursion", "verdict": "partial",
                              "hints": 1}),
    ("sample:6", "taught", 2, {"object": "concept:big-o-notation"}),
]


def files() -> list[str]:
    """Vault-relative paths of the sample pages."""
    return sorted(p.relative_to(SAMPLE_DIR).as_posix() for p in SAMPLE_DIR.rglob("*.md"))


def seed(vault: Vault, now: datetime | None = None) -> tuple[list[str], list[str]]:
    """Copy missing sample pages, sync the catalog, log the seed events once (keyed)."""
    now = now or datetime.now(UTC)
    did, skipped = [], []
    for rel in files():
        dest = vault.root / rel
        if dest.exists():
            skipped.append(rel)
        else:
            vault.write_text(dest, (SAMPLE_DIR / rel).read_bytes().decode("utf-8"))
            did.append(rel)
    catalog_sync(vault)
    log = EventLog(vault)
    new = [s for s in SEED if not log.has_key(s[0])]
    for key, kind, days, fields in new:
        log.append(session="cli", kind=kind, source="external", key=key, fields=fields,
                   now=now - timedelta(days=days))
    (did if new else skipped).append(f"{len(SEED)} events")
    return did, skipped


def _is_sample(vault: Vault, path: Path) -> bool:
    try:
        return vault.read_path(path).fields.get("sample") is True
    except VaultError:
        return False


def remove(vault: Vault) -> tuple[list[str], int]:
    """Delete `sample: true` pages and their catalog entries; void the live seed events.
    Returns (removed paths, events voided)."""
    root = vault.root
    paths = [p for p in sorted(root.rglob("*.md"))
             if not p.relative_to(root).parts[0].startswith(".") and _is_sample(vault, p)]
    gone = set()
    for p in paths:
        vault.remove(p)
        if p.parent.name in ("concepts", "areas", "domains"):
            gone.add(p.stem)
    _drop_entries(vault, gone)
    log = EventLog(vault)
    n = 0
    for e in live(log.iter_events()):
        if str(e.get("key", "")).startswith(KEY):
            void_event(log, session="cli", event_id=e["id"], reason="sample removed",
                       source="external")
            n += 1
    return [p.relative_to(root).as_posix() for p in paths], n


def _drop_entries(vault: Vault, ids: set[str]) -> None:
    """Take removed pages out of catalog.yaml (sync would otherwise re-create them). An area
    or domain that still holds other entries stays."""
    with vault.lock():
        cat = Catalog.load(vault)
        cat._reload()
        changed = False
        for kind in ("concept", "area", "domain"):  # children first
            for id in sorted(ids):
                e = cat.entries.get(id)
                if e is None or e.kind != kind or cat.children(id):
                    continue
                seq, node = cat._node(id)
                seq.remove(node)
                cat._index()
                changed = True
        if changed:
            cat._save()
