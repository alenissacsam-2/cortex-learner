"""`cortex backup` (verdict item 30): a round trip restores the vault; rebuilt and temp files
stay out; only the last 10 zips are kept."""

import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from cortex import backup
from cortex.events import EventLog
from cortex.vault import Vault

FIXTURE = Path(__file__).parent / "fixtures" / "vault_mini"


def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and (p.suffix == ".md" or "events" in p.parts)}


def test_round_trip_skips_rebuilt_and_temp_files(tmp_path):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    EventLog(Vault(root)).append(session="cli", kind="grade", source="external",
                                 now=datetime(2026, 11, 13, 12, tzinfo=UTC),
                                 fields={"object": "concept:two-pointers", "verdict": "correct"})
    cache = root / ".cortex"
    cache.mkdir(exist_ok=True)
    (cache / "dashboard.html").write_text("<html>" + "x" * 1000, encoding="utf-8")
    (cache / "vectors.npy").write_bytes(b"keep me")
    (root / "notes").mkdir()
    (root / "notes" / ".x.md.123.tmp").write_text("stray", encoding="utf-8")
    zp = backup.backup(root)
    out = tmp_path / "restored"
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
        z.extractall(out)
    assert _files(out) == _files(root) and any(n.startswith("events/") for n in names)
    assert ".cortex/vectors.npy" in names  # re-embedding takes minutes
    assert ".cortex/dashboard.html" not in names
    assert not [n for n in names if n.endswith(".tmp")]


def test_keeps_the_last_ten_zips(tmp_path):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    dest = tmp_path / "bk"
    dest.mkdir()
    old = [dest / f"cortex-20000101-0000{n:02}.zip" for n in range(12)]
    for p in old:
        p.write_bytes(b"old")
    (dest / "notes.txt").write_text("not a backup", encoding="utf-8")
    new = backup.backup(root, dest)
    left = sorted(p.name for p in dest.glob("cortex-*.zip"))
    assert len(left) == 10 and new.name == left[-1]
    assert left[:9] == [p.name for p in old[3:]]
    assert (dest / "notes.txt").exists()


def test_keeps_durable_browse_stores_and_leaves_out_secrets(tmp_path):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    b = root / ".cortex" / "browse"
    (b / "inbox-archive").mkdir(parents=True)
    (b / "chat").mkdir()
    keep = ["site-days.json", "sites.json", "verdicts.json", "state.json",
            "vectors-private.npz", "inbox-archive/inbox-2026-11-01.jsonl",
            "inbox-2026-11-19.jsonl"]  # filtered on arrival, like the archive
    drop = ["token", "lock", "chat/01JABCDEFGHJKMNPQRSTVWXYZ0.jsonl"]
    for name in keep + drop:
        (b / name).write_text("x", encoding="utf-8")
    with zipfile.ZipFile(backup.backup(root)) as z:
        names = set(z.namelist())
    assert {f".cortex/browse/{n}" for n in keep} <= names
    assert not [n for n in names if n.startswith(".cortex/browse/")
                and n.removeprefix(".cortex/browse/") not in keep]
