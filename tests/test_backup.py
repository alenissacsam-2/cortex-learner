"""`cortex backup` (verdict item 30): a round trip restores the vault; rebuilt and temp files
stay out; only the last 10 zips are kept."""

import shutil
import sqlite3
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from conftest import note
from typer.testing import CliRunner

from cortex import backup, semantic
from cortex.browse import store, vectors
from cortex.cli import app
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
            "vectors-private.npz", "store.db", "vectors-private.db",
            "inbox-archive/inbox-2026-11-01.jsonl",
            "inbox-2026-11-19.jsonl"]  # filtered on arrival, like the archive
    drop = ["token", "lock", "chat/01JABCDEFGHJKMNPQRSTVWXYZ0.jsonl"]
    for name in keep + drop:
        if not name.endswith(".db"):
            (b / name).write_text("x", encoding="utf-8")
    assert set(backup.DBS) == {store.PATH, vectors.CACHE, semantic.CACHE}
    live = []
    for rel in backup.DBS:  # open, with rows in the WAL: the copy still holds them
        con = sqlite3.connect(root / rel)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t(x)")
        con.execute("INSERT INTO t VALUES ('in the wal')")
        con.commit()
        live.append(con)
    try:
        with zipfile.ZipFile(backup.backup(root)) as z:
            names = set(z.namelist())
            z.extractall(tmp_path / "out")
    finally:
        for con in live:
            con.close()
    assert {f".cortex/browse/{n}" for n in keep} | {".cortex/vectors.db"} <= names
    assert not [n for n in names if n.startswith(".cortex/browse/")
                and n.removeprefix(".cortex/browse/") not in keep]
    assert not [n for n in names if n.endswith(("-wal", "-shm"))]
    for rel in backup.DBS:
        with closing(sqlite3.connect(tmp_path / "out" / rel)) as con:
            assert con.execute("SELECT x FROM t").fetchall() == [("in the wal",)]


def test_no_private_leaves_out_private_pages_and_stores(tmp_path, home):
    """`cortex backup --no-private`: private pages, a mistake about a private concept (privacy
    by inheritance), a private resource's raw source, browse/private, the browse store and
    .git stay out; public pages, the events and the public vectors stay in."""
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    note(root, "notes/diary.md", 'title: "Diary"\nkind: note\nprivate: true\n', "# Diary\n")
    note(root, "notes/pasta.md", 'title: "Pasta"\nkind: note\n', "# Pasta\n")
    note(root, "mistakes/dsa.diary-slip.md", 'id: "dsa.diary-slip"\ntitle: "Slip"\n'
         'concepts: ["[[secret-diary]]"]\n', "# Slip\n")
    note(root, "resources/letter.md", 'title: "Letter"\nkind: resource\nprivate: true\n'
         'source: "raw/letter.txt"\n', "# Letter\n")
    (root / "raw").mkdir()
    (root / "raw" / "letter.txt").write_text("private letter", encoding="utf-8")
    (root / "raw" / "public.txt").write_text("public text", encoding="utf-8")
    note(root, "browse/private/browse-private-2026-11-01.md", "kind: browse\nprivate: true\n",
         "# Browsing (private)\n")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    b = root / ".cortex" / "browse"
    b.mkdir(parents=True)
    (b / "sites.json").write_text("{}", encoding="utf-8")
    for s in ("", "-wal", "-shm"):  # Audit 21 Nov I3: raw chat turns, private ones too
        (root / f".cortex/chat-import.sqlite{s}").write_text("health chat", encoding="utf-8")
    for rel in (store.PATH, semantic.CACHE):
        with closing(sqlite3.connect(root / rel)) as con:
            con.execute("CREATE TABLE t(x)")
            con.commit()
    EventLog(Vault(root)).append(session="cli", kind="grade", source="external",
                                 now=datetime(2026, 11, 13, 12, tzinfo=UTC),
                                 fields={"object": "concept:two-pointers", "verdict": "correct"})
    with zipfile.ZipFile(backup.backup(root, tmp_path / "full")) as z:  # the default: all
        assert {"notes/diary.md", ".git/HEAD", ".cortex/browse/sites.json",
                "browse/private/browse-private-2026-11-01.md"} <= set(z.namelist())
    r = CliRunner().invoke(app, ["backup", "--no-private", "--to", str(tmp_path / "np"),
                                 "--vault", str(root)])
    assert r.exit_code == 0, r.output
    (zp,) = (tmp_path / "np").glob("cortex-*.zip")
    with zipfile.ZipFile(zp) as z:
        names = set(z.namelist())
    assert {"notes/pasta.md", "concepts/two-pointers.md", "mistakes/dsa.off-by-one.md",
            "raw/public.txt", ".cortex/vectors.db"} <= names, names
    assert any(n.startswith("events/") for n in names)
    for gone in ("notes/diary.md", "concepts/secret-diary.md", "mistakes/dsa.diary-slip.md",
                 "resources/letter.md", "raw/letter.txt", ".cortex/chat-import.sqlite",
                 ".cortex/chat-import.sqlite-wal", ".cortex/chat-import.sqlite-shm"):
        assert gone not in names, gone
    assert not [n for n in names if n.startswith((".git/", ".cortex/browse/", "browse/private/"))]
