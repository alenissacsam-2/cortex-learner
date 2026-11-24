"""R29b: a damaged SQLite store is moved aside (never deleted) and started again; doctor names
the copy, backup leaves it out and forget deletes it (cortex.quarantine)."""

import logging
import sqlite3
from contextlib import closing

import pytest
from conftest import FakeEmbed

from cortex import chat_import, doctor, forget, quarantine, semantic, vecstore
from cortex.catalog import Catalog
from cortex.index import Index
from cortex.vault import Vault, VaultError

pytestmark = pytest.mark.usefixtures("home")


def index(root):
    v = Vault(root)
    return Index(v, Catalog.load(v))


def new_process(monkeypatch):
    """What a fresh process starts with: no vectors in memory, no store checked yet."""
    monkeypatch.setattr(semantic, "_MEM", {})
    monkeypatch.setattr(quarantine, "_CHECKED", set())
    monkeypatch.setattr(quarantine, "_STUCK", set())


def header(p):  # unreadable from the first byte: SQLITE_NOTADB at open
    p.write_bytes(b"this is not a database" * 200)


def pages(p):  # page 1 (the schema) fine, every other page overwritten: only quick_check sees
    with closing(sqlite3.connect(p)) as con:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size = con.execute("PRAGMA page_size").fetchone()[0]
    data = p.read_bytes()
    assert len(data) > size
    p.write_bytes(data[:size] + b"\xff" * (len(data) - size))


@pytest.mark.parametrize("damage", [header, pages])
def test_a_damaged_vector_store_is_moved_aside_and_recall_works_after(root, monkeypatch,
                                                                       caplog, damage):
    semantic.vectors(index(root), FakeEmbed(), 4)
    db = root / semantic.CACHE
    for s in ("-wal", "-shm"):
        (root / f"{semantic.CACHE}{s}").unlink(missing_ok=True)
    damage(db)
    bad = db.read_bytes()
    new_process(monkeypatch)
    assert doctor.check_damaged_stores(root) == []
    with caplog.at_level(logging.WARNING):
        hits = semantic.recall(index(root), "two pointers", FakeEmbed())
    assert hits  # recall answers from a fresh store
    (copy,) = quarantine.found(root)
    assert quarantine.STORE.fullmatch(copy.name) and copy.name.startswith("vectors.db.damaged-")
    assert copy.read_bytes() == bad  # moved, never deleted
    assert sum("moved aside" in r.getMessage() for r in caplog.records) == 1
    with closing(vecstore.connect(db, create=False)) as con:
        assert vecstore.manifests(con)  # pages embedded again into the new store
    new_process(monkeypatch)
    fake = FakeEmbed()
    semantic.vectors(index(root), fake, 4)
    assert fake.texts == [] and len(quarantine.found(root)) == 1  # healthy now: no second move

    (row,) = doctor.check_damaged_stores(root)
    assert row[0] == "WARN" and row[2] == f".cortex/{copy.name}" and "Delete" in row[3]


def test_a_damaged_chat_import_state_is_moved_aside(root):
    p = root / chat_import.STATE
    p.parent.mkdir(exist_ok=True)
    header(p)
    with closing(chat_import.State(Vault(root))) as st:
        assert st.sigs() == {}
        st.put("k", (1, 2), {"offset": 3})
    (copy,) = quarantine.found(root)
    assert copy.name.startswith("chat-import.sqlite.damaged-")
    assert copy.read_bytes().startswith(b"this is not") and (root / chat_import.STATE).is_file()
    (row,) = doctor.check_damaged_stores(root)
    assert row[2].startswith(".cortex/chat-import.sqlite.damaged-") and "-wal" not in row[2]


def test_a_store_held_open_stays_whole_and_the_error_stands(root, monkeypatch, caplog):
    db = root / semantic.CACHE
    db.parent.mkdir(exist_ok=True)
    header(db)
    (root / f"{semantic.CACHE}-wal").write_bytes(b"frames")
    move = Vault.move

    def stuck(self, src, dst):
        if str(src).endswith("-wal"):
            raise VaultError("vault_locked", "held open")
        move(self, src, dst)
    monkeypatch.setattr(Vault, "move", stuck)
    with pytest.raises(VaultError):  # the store moved, its WAL stuck: the store comes back
        quarantine.move_aside(db)
    assert db.read_bytes().startswith(b"this is not") and quarantine.found(root) == []
    assert (root / f"{semantic.CACHE}-wal").read_bytes() == b"frames"
    with caplog.at_level(logging.WARNING):
        assert not quarantine.heal(db, sqlite3.DatabaseError("x"), "y")
    assert any("could not be moved aside" in r.getMessage() for r in caplog.records)


def test_a_store_that_cannot_be_moved_is_tried_once_per_process(root, monkeypatch, caplog):
    new_process(monkeypatch)
    db = root / semantic.CACHE
    db.parent.mkdir(exist_ok=True)
    header(db)
    tries, move = [], Vault.move

    def stuck(self, src, dst):
        tries.append(src)
        raise VaultError("vault_locked", "held open")
    monkeypatch.setattr(Vault, "move", stuck)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):  # recall still answers, by keyword and this call's embeddings
            semantic.vectors(index(root), FakeEmbed(), 4)
    assert len(tries) == 1 and db.read_bytes().startswith(b"this is not")
    assert sum("could not be moved aside" in r.getMessage() for r in caplog.records) == 1
    new_process(monkeypatch)  # the next start tries again
    monkeypatch.setattr(Vault, "move", move)
    semantic.vectors(index(root), FakeEmbed(), 4)
    assert len(quarantine.found(root)) == 1


def test_a_forget_deletes_the_damaged_copies_and_backup_leaves_them_out(root):
    from cortex import backup

    names = ["vectors.db.damaged-20261124T101500Z", "vectors.db.damaged-20261124T101500Z-wal",
             "chat-import.sqlite.damaged-20261124T101501Z"]
    (root / ".cortex").mkdir(exist_ok=True)
    for n in names:
        (root / ".cortex" / n).write_bytes(b"zqcanary rows")
    import zipfile
    with zipfile.ZipFile(backup.backup(root, root.parent / "zips")) as z:
        assert not [n for n in z.namelist() if ".damaged-" in n]
    receipt = forget.forget(Vault(root), "lc-167")
    assert receipt["removed"]["damaged"] == 3 and quarantine.found(root) == []
    assert "3 files of damaged database copies deleted" in forget.lines(receipt)[0]
    assert doctor.check_damaged_stores(root) == []
