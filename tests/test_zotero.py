"""Zotero (R18): finding the library, reading a temp copy of a made-up zotero.sqlite (the
Zotero 5-7 tables cortex.zotero reads, cut to the columns it uses plus their keys), one page per
item, updates by item key, and a watched library that copies nothing while it is unchanged."""

import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_more_sources import _pdf
from typer.testing import CliRunner

from cortex import sources, watch, zotero
from cortex.cli import app
from cortex.vault import Vault, VaultError

runner = CliRunner()
C = sources.Audience.CLOUD

SCHEMA = """
CREATE TABLE libraries (libraryID INTEGER PRIMARY KEY, type TEXT NOT NULL);
CREATE TABLE itemTypesCombined (itemTypeID INT NOT NULL PRIMARY KEY, typeName TEXT NOT NULL);
CREATE TABLE fieldsCombined (fieldID INT NOT NULL PRIMARY KEY, fieldName TEXT NOT NULL);
CREATE TABLE baseFieldMappingsCombined (itemTypeID INT, baseFieldID INT, fieldID INT,
    PRIMARY KEY (itemTypeID, baseFieldID, fieldID));
CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INT NOT NULL,
    dateAdded TIMESTAMP NOT NULL, libraryID INT NOT NULL, key TEXT NOT NULL,
    UNIQUE (libraryID, key));
CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value UNIQUE);
CREATE TABLE itemData (itemID INT, fieldID INT, valueID, PRIMARY KEY (itemID, fieldID));
CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT,
    fieldMode INT);
CREATE TABLE itemCreators (itemID INT NOT NULL, creatorID INT NOT NULL,
    creatorTypeID INT NOT NULL DEFAULT 1, orderIndex INT NOT NULL DEFAULT 0,
    PRIMARY KEY (itemID, creatorID, creatorTypeID, orderIndex));
CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE itemTags (itemID INT NOT NULL, tagID INT NOT NULL, type INT NOT NULL DEFAULT 0,
    PRIMARY KEY (itemID, tagID));
CREATE TABLE itemNotes (itemID INTEGER PRIMARY KEY, parentItemID INT, note TEXT, title TEXT);
CREATE TABLE itemAttachments (itemID INTEGER PRIMARY KEY, parentItemID INT, linkMode INT,
    contentType TEXT, path TEXT);
CREATE TABLE itemAnnotations (itemID INTEGER PRIMARY KEY, parentItemID INT NOT NULL,
    type INTEGER NOT NULL, text TEXT, comment TEXT, pageLabel TEXT, sortIndex TEXT NOT NULL);
CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
INSERT INTO libraries VALUES (1, 'user'), (2, 'group');
INSERT INTO itemTypesCombined VALUES (1, 'journalArticle'), (2, 'note'), (3, 'attachment'),
    (4, 'annotation'), (5, 'case');
INSERT INTO fieldsCombined VALUES (1, 'title'), (2, 'date'), (3, 'publicationTitle'),
    (4, 'DOI'), (5, 'url'), (6, 'abstractNote'), (7, 'caseName'), (8, 'extra');
INSERT INTO baseFieldMappingsCombined VALUES (5, 1, 7);
"""
ABSTRACT = "We propose the Transformer, a model built on attention alone."


@contextmanager
def _db(d: Path):
    """The library's database, committed and closed after (Windows cannot delete an open one)."""
    con = sqlite3.connect(d / zotero.DB)
    try:
        with con:
            yield con
    finally:
        con.close()


def _set(con, iid: int, **fields) -> None:
    ids = dict(con.execute("SELECT fieldName, fieldID FROM fieldsCombined"))
    for name, value in fields.items():
        con.execute("INSERT OR IGNORE INTO itemDataValues (value) VALUES (?)", (value,))
        vid = con.execute("SELECT valueID FROM itemDataValues WHERE value = ?", (value,))
        con.execute("INSERT OR REPLACE INTO itemData VALUES (?, ?, ?)",
                    (iid, ids[name], vid.fetchone()[0]))


def _item(con, iid: int, kind: int, key: str, lib: int = 1, **fields) -> None:
    con.execute("INSERT INTO items VALUES (?, ?, '2024-05-06 12:00:00', ?, ?)",
                (iid, kind, lib, key))
    _set(con, iid, **fields)


def _library(d: Path) -> Path:
    """A made-up Zotero data folder: a paper with a note, a stored PDF and two annotations on
    it, a court case (its caseName is its title), a standalone note, a trashed paper, a trashed
    note, and a group library's paper."""
    (d / "storage" / "PDFK0001").mkdir(parents=True)
    (d / "storage" / "PDFK0001" / "attn.pdf").write_bytes(
        _pdf("Scaled dot product attention explained"))
    with _db(d) as con:
        con.executescript(SCHEMA)
        _item(con, 1, 1, "ABCD2345", title="Attention Is All You Need", date="2017-06-12",
              publicationTitle="Neural Information Processing Systems", DOI="10.1234/attn",
              url="https://example.org/attn", abstractNote=ABSTRACT, extra="IGNORED_EXTRA")
        con.executemany("INSERT INTO creators VALUES (?, ?, ?, ?)",
                        [(1, "Ashish", "Vaswani", 0), (2, None, "Google Brain", 1)])
        con.executemany("INSERT INTO itemCreators (itemID, creatorID, orderIndex) VALUES "
                        "(1, ?, ?)", [(2, 1), (1, 0)])
        con.executemany("INSERT INTO tags VALUES (?, ?)", [(1, "transformers"), (2, "to read")])
        con.executemany("INSERT INTO itemTags (itemID, tagID) VALUES (1, ?)", [(1,), (2,)])
        _item(con, 2, 2, "NOTE0002")
        con.execute("INSERT INTO itemNotes VALUES (2, 1, '<p>Read this <b>twice</b>: the key "
                    "idea is self-attention.</p>', 'Read this twice')")
        _item(con, 3, 3, "PDFK0001", title="Full Text PDF")
        con.execute("INSERT INTO itemAttachments VALUES (3, 1, 1, 'application/pdf', "
                    "'storage:attn.pdf')")
        _item(con, 4, 4, "ANNO0001")
        _item(con, 5, 4, "ANNO0002")
        con.executemany("INSERT INTO itemAnnotations VALUES (?, 3, ?, ?, ?, ?, ?)",
                        [(4, 1, "multi-head\n attention", "the core trick", "3", "00002"),
                         (5, 2, None, "compare with RNNs", "5", "00004")])
        _item(con, 6, 1, "TRSH0001", title="TRASHED_PAPER")
        _item(con, 7, 1, "GRUP0001", lib=2, title="GROUP_PAPER")
        _item(con, 8, 5, "CASE0001", caseName="Roe Example Case", date="1973")
        _item(con, 9, 2, "NOTE0009")
        con.execute("INSERT INTO itemNotes VALUES (9, NULL, '<div><p>Standalone thought about "
                    "graph search</p><p>BFS before DFS.</p></div>', 'Standalone thought about "
                    "graph search')")
        _item(con, 10, 2, "NOTE0010")
        con.execute("INSERT INTO itemNotes VALUES (10, 1, '<p>OLD_TRASHED_NOTE</p>', 'x')")
        con.executemany("INSERT INTO deletedItems VALUES (?)", [(6,), (10,)])
    return d


def _page(v: Vault, nid: str) -> str:
    return (v.root / "resources" / f"{nid}.md").read_bytes().decode("utf-8")


def _pages(v: Vault) -> list[str]:
    return sorted(p.stem for p in (v.root / "resources").glob("zotero-*.md"))


def test_pages_hold_the_item_its_notes_highlights_and_pdf(root_plain, tmp_path):
    v, d = Vault(root_plain), _library(tmp_path / "Zotero")
    before = sorted(os.listdir(d))
    r = sources.run(v, zotero.source(d), C)
    assert r["imported"] == 3 and r["read"] == 3
    assert _pages(v) == ["zotero-attention-is-all-you-need", "zotero-roe-example-case",
                         "zotero-standalone-thought-about-graph-search"]
    page = _page(v, "zotero-attention-is-all-you-need")
    assert 'title: "Attention Is All You Need"' in page and 'date: "2024-05-06"' in page
    assert "From: zotero://select/library/items/ABCD2345" in page
    assert ("Journal article in Neural Information Processing Systems (2017) by Ashish Vaswani, "
            "Google Brain.") in page
    assert "DOI: 10.1234/attn" in page and "Tags: to read, transformers" in page
    assert f"## Abstract\n{ABSTRACT}" in page
    assert "## My notes\nRead this twice: the key idea is self-attention." in page
    assert ('## My highlights\n- p. 3: "multi-head attention" (my note: the core trick)\n'
            "- p. 5: My note: compare with RNNs") in page
    assert "## File: attn.pdf" in page and "Scaled dot product attention explained" in page
    every = "".join(_page(v, n) for n in _pages(v))
    for gone in ("TRASHED_PAPER", "GROUP_PAPER", "OLD_TRASHED_NOTE", "IGNORED_EXTRA"):
        assert gone not in every
    assert "Case (1973)." in _page(v, "zotero-roe-example-case")
    assert "BFS before DFS." in _page(v, "zotero-standalone-thought-about-graph-search")
    assert sorted(os.listdir(d)) == before  # read from a copy: nothing written next to it


def test_an_edited_item_rewrites_its_page_unless_the_owner_edited_it(root_plain, tmp_path):
    v, d = Vault(root_plain), _library(tmp_path / "Zotero")
    sources.run(v, zotero.source(d), C)
    again = sources.run(v, zotero.source(d), C)
    assert again["imported"] == again["updated"] == 0 and again["duplicate"] == 3
    with _db(d) as con:
        _set(con, 1, abstractNote="A shorter abstract, rewritten.")
    r = sources.run(v, zotero.source(d), C)
    assert r["updated"] == 1 and r["imported"] == 0
    page = _page(v, "zotero-attention-is-all-you-need")
    assert "A shorter abstract, rewritten." in page and ABSTRACT not in page
    p = v.root / "resources" / "zotero-attention-is-all-you-need.md"
    p.write_bytes(page.replace("## My notes", "## My notes\nMY OWN EDIT").encode())
    with _db(d) as con:
        _set(con, 1, abstractNote="Third version.")
    r = sources.run(v, zotero.source(d), C)
    assert r["updated"] == 0 and r["reasons"] == {sources.EDITED: 1}
    assert "MY OWN EDIT" in p.read_bytes().decode()


def test_a_wal_database_zotero_holds_open_is_read_with_its_wal(tmp_path):
    d = _library(tmp_path / "Zotero")
    con = sqlite3.connect(d / zotero.DB)
    try:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA wal_autocheckpoint = 0")
        _set(con, 1, title="Retitled In The WAL")
        con.commit()
        assert (d / (zotero.DB + "-wal")).stat().st_size > 0
        recs = zotero.records(d)
        assert recs["ABCD2345"][1]["fields"]["title"] == "Retitled In The WAL"
        assert len(zotero.stat(d)) == 4  # the database's and its WAL's
    finally:
        con.close()


def test_a_locked_database_is_busy_and_a_bad_one_invalid(tmp_path, monkeypatch):
    d = _library(tmp_path / "Zotero")
    real = shutil.copyfile
    monkeypatch.setattr(shutil, "copyfile", lambda *a: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(VaultError) as e:
        zotero.records(d)
    assert e.value.code == "busy"
    monkeypatch.setattr(shutil, "copyfile", real)
    (d / zotero.DB).write_bytes(b"not a database" * 100)
    with pytest.raises(VaultError) as e:
        zotero.records(d)
    assert e.value.code == "invalid_argument"


def test_stored_file_names_never_leave_the_storage_folder(tmp_path):
    (tmp_path / "storage" / "K").mkdir(parents=True)
    (tmp_path / "storage" / "K" / "a.pdf").write_bytes(b"%PDF")
    (tmp_path / "outside.pdf").write_bytes(b"%PDF")
    assert zotero._file(tmp_path, 1, "storage:a.pdf", "K")[0] == str(
        tmp_path / "storage" / "K" / "a.pdf")
    for bad in ("storage:../../outside.pdf", "storage:..", "storage:", "storage:.a.pdf",
                "storage:sub\\a.pdf"):
        assert zotero._file(tmp_path, 1, bad, "K") is None
    assert zotero._file(tmp_path, 2, str(tmp_path / "outside.pdf"), "K") is not None
    assert zotero._file(tmp_path, 2, "attachments:outside.pdf", "K") is None  # base-relative
    assert zotero._file(tmp_path, 3, "https://example.org/a.pdf", "K") is None  # a link
    (tmp_path / "storage" / "K" / "a.txt").write_bytes(b"x")
    assert zotero._file(tmp_path, 0, "storage:a.txt", "K") is None


def _profile(home: Path, data: Path | None, use: bool = True) -> None:
    prof = zotero._profiles() / "Profiles" / "abc123.default"
    prof.mkdir(parents=True)
    (zotero._profiles() / "profiles.ini").write_text(
        "[General]\nStartWithLastProfile=1\n\n[Profile0]\nName=default\nIsRelative=1\n"
        "Path=Profiles/abc123.default\nDefault=1\n", encoding="utf-8")
    prefs = '// Mozilla User Preferences\nuser_pref("extensions.zotero.lastViewedFolder", "L1");\n'
    if data is not None:
        prefs += (f'user_pref("extensions.zotero.dataDir", {json.dumps(str(data))});\n'
                  f'user_pref("extensions.zotero.useDataDir", {"true" if use else "false"});\n')
    (prof / "prefs.js").write_text(prefs, encoding="utf-8")


def test_the_library_is_found_where_zotero_documents_it(home, tmp_path):
    with pytest.raises(VaultError, match="Found no Zotero library"):
        zotero.find()
    default = _library(home / "Zotero")
    assert zotero.data_dir() == default and zotero.find() == default
    elsewhere = _library(tmp_path / "Elsewhere Data")
    _profile(home, elsewhere)
    assert zotero.data_dir() == elsewhere
    shutil.rmtree(zotero._profiles())
    _profile(home, elsewhere, use=False)  # a dataDir left over, not in use
    assert zotero.data_dir() == default
    assert zotero.find(elsewhere / zotero.DB) == elsewhere == zotero.find(str(elsewhere))
    with pytest.raises(VaultError, match="Found no zotero.sqlite"):
        zotero.find(tmp_path)


def test_cli_import_finds_the_library_by_itself(root_plain, home):
    _library(home / "Zotero")
    out = runner.invoke(app, ["import", "--source", "zotero", "--vault", str(root_plain),
                              "--tier", "private"]).output
    assert "Zotero: tier private" in out and "Zotero: read 3, imported 3, private 3" in out
    out = runner.invoke(app, ["import", "--vault", str(root_plain)]).output
    assert "Give the file or folder to import" in out


def test_a_watched_library_copies_nothing_while_it_is_unchanged(root_plain, home, monkeypatch):
    v, d = Vault(root_plain), _library(home / "Zotero")
    w = watch.add(v, None, "zotero", "normal")
    assert w == {"path": str(d.resolve()), "source": "zotero", "tier": "cloud"}
    copies = []
    real = zotero.records
    monkeypatch.setattr(zotero, "records", lambda d: copies.append(d) or real(d))
    first = watch.run(v)
    assert first[0]["read"] == 3 and len(copies) == 1 and len(_pages(v)) == 3
    assert watch.run(v)[0]["read"] == 0 and len(copies) == 1  # one stat, no copy
    with _db(d) as con:
        _set(con, 8, date="1974")
    os.utime(d / zotero.DB, ns=(1, 2_000_000_000_000_000_000))  # a later mtime, surely
    r = watch.run(v)
    assert r[0]["read"] == 1 and len(copies) == 2  # copied again; only the changed item read
    assert "Case (1974)." in _page(v, "zotero-roe-example-case")
    assert watch.listing(v)["rows"][0]["last"]["line"].startswith("Zotero: read 1, imported 0")
    (d / zotero.DB).unlink()
    assert watch.run(v)[0]["line"] == watch.GONE
    assert watch.listing(v)["rows"][0]["missing"] is True
