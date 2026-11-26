"""Anki (R22): made-up collections in both schemas (legacy `col.decks` JSON in an .apkg, schema
18's `decks` table in a profile folder with a -wal), one page per deck with its counts,
retention and leeches, subdeck and concept links, idempotent re-import, the refusals, and a
source left byte for byte as it was."""

import os
import sqlite3
import time
import zipfile
from contextlib import closing
from pathlib import Path

import pytest
from conftest import note
from typer.testing import CliRunner

from keepfold import anki, importers, sources
from keepfold.cli import app
from keepfold.vault import Vault, VaultError

runner = CliRunner()
C, L = sources.Audience.CLOUD, sources.Audience.LOCAL
NOW = time.time()
DAY = 86_400_000

TABLES = """
CREATE TABLE col (id integer primary key, crt integer, mod integer, scm integer, ver integer,
    dty integer, usn integer, ls integer, conf text, models text, decks text, dconf text,
    tags text);
CREATE TABLE notes (id integer primary key, guid text, mid integer, mod integer, usn integer,
    tags text, flds text, sfld integer, csum integer, flags integer, data text);
CREATE TABLE cards (id integer primary key, nid integer, did integer, ord integer, mod integer,
    usn integer, type integer, queue integer, due integer, ivl integer, factor integer,
    reps integer, lapses integer, left integer, odue integer, odid integer, flags integer,
    data text);
CREATE TABLE revlog (id integer primary key, cid integer, usn integer, ease integer,
    ivl integer, lastIvl integer, factor integer, time integer, type integer);
"""
DECKS = {1: "Default", 100: "Physics", 101: "Physics::Optics", 200: "Filtered Deck 1"}
NASTY = ('<b>Total</b> internal&nbsp;reflection <script>alert("x")</script><img src="a.png">'
         '<a href="https://evil.example/x">see here</a> [sound:a.mp3]')
# (card id, note id, deck, home deck of a filtered card, type, interval, lapses, first field,
# tags): Optics has 1 new, 2 learning (one relearning), 1 young, 2 mature (one in a filtered
# deck) and 2 leeches (one tagged, one by lapses); Physics itself 1 new and 1 mature (ivl 21)
CARDS = [(11, 1, 101, 0, 2, 30, 2, "Snell's law", " leech "),
         (12, 2, 101, 0, 2, 5, 6, NASTY, ""),
         (13, 3, 101, 0, 0, 0, 0, "Lens maker", ""),
         (14, 4, 101, 0, 1, 0, 0, "Focal length", ""),
         (15, 5, 101, 0, 3, 1, 1, "Prism", ""),
         (16, 6, 200, 101, 2, 40, 0, "Mirror", ""),
         (17, 7, 100, 0, 2, 21, 0, "Newton", ""),
         (18, 8, 100, 0, 0, 0, 0, "Joule", "")]


def _revlog() -> list[tuple]:
    """(id ms, card, ease, type): Optics has 25 review answers in 30 days (5 Again: 80%), 3
    learning answers, a manual reschedule (ease 0, not counted), 10 answers 100 days ago and 2
    400 days ago; Physics itself 5 good reviews in 30 days (with Optics: 25 of 30, 83%)."""
    now, rows = int(NOW * 1000), []
    rows += [(now - DAY - i, 11, 1 if i < 5 else 3, 1) for i in range(25)]
    rows += [(now - 2 * DAY - i, 14, 3, 0) for i in range(3)]
    rows += [(now - 3 * DAY, 12, 0, 4)]
    rows += [(now - 100 * DAY - i, 12, 3, 1) for i in range(10)]
    rows += [(now - 400 * DAY - i, 12, 3, 1) for i in range(2)]
    rows += [(now - 5 * DAY - i, 17, 3, 1) for i in range(5)]
    return rows


def _fill(con, schema: int, tables: str = TABLES) -> None:
    con.executescript(tables)
    if schema == 18:
        con.execute("CREATE TABLE decks (id integer primary key not null, name text not null, "
                    "mtime_secs integer, usn integer, common blob, kind blob)")
        con.executemany("INSERT INTO decks VALUES (?, ?, 0, 0, x'', x'')",
                        [(i, n.replace("::", "\x1f")) for i, n in DECKS.items()])
        decks = ""
    else:
        import json
        decks = json.dumps({str(i): {"id": i, "name": n} for i, n in DECKS.items()})
    con.execute("INSERT INTO col VALUES (1, 0, 0, 0, ?, 0, 0, 0, '{}', '{}', ?, '{}', '{}')",
                (schema, decks))
    for cid, nid, did, odid, kind, ivl, lapses, front, tags in CARDS:
        con.execute("INSERT INTO notes VALUES (?, 'g', 1, 0, 0, ?, ?, 0, 0, 0, '')",
                    (nid, tags, f"{front}\x1fthe back"))
        con.execute("INSERT INTO cards VALUES (?, ?, ?, 0, 0, 0, ?, ?, 0, ?, 2500, 3, ?, 0, 0, ?,"
                    " 0, '')", (cid, nid, did, kind, kind, ivl, lapses, odid))
    con.executemany("INSERT INTO revlog VALUES (?, ?, 0, ?, 1, 1, 2500, 5000, ?)", _revlog())


def _sqlite(p: Path, schema: int) -> Path:
    with closing(sqlite3.connect(p)) as con:
        _fill(con, schema)
        con.commit()
    return p


def _apkg(d: Path, members: dict[str, bytes | Path], name: str = "Physics.apkg") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(d / name, "w", zipfile.ZIP_DEFLATED) as z:
        for m, data in members.items():
            z.writestr(m, data.read_bytes() if isinstance(data, Path) else data)
        z.writestr("media", "{}")
    return d / name


def _legacy_apkg(tmp: Path) -> Path:
    """A legacy export as modern Anki writes it: the data in collection.anki21, with a stub
    collection.anki2 (an empty collection) beside it."""
    _sqlite(tmp / "real.db", 11)
    with closing(sqlite3.connect(tmp / "stub.db")) as con:
        con.executescript(TABLES)
        con.execute("INSERT INTO col VALUES (1,0,0,0,11,0,0,0,'{}','{}','{}','{}','{}')")
        con.commit()
    return _apkg(tmp / "export", {"collection.anki21": tmp / "real.db",
                                  "collection.anki2": tmp / "stub.db"})


def _profile(tmp: Path) -> Path:
    d = tmp / "Anki2" / "User 1"
    d.mkdir(parents=True)
    (d / "collection.media").mkdir()
    _sqlite(d / anki.COLLECTION, 18)
    return d


def _page(v: Vault, nid: str) -> str:
    return (v.root / "resources" / f"{nid}.md").read_bytes().decode("utf-8")


def _pages(v: Vault) -> list[str]:
    return sorted(p.stem for p in (v.root / "resources").glob("deck-*.md"))


def _bodies(v: Vault) -> dict[str, str]:
    return {n: _page(v, n).split("· sha: ", 1)[1].split("\n", 1)[1] for n in _pages(v)}


def test_a_deck_page_holds_counts_retention_and_cleaned_leeches(root_plain, tmp_path):
    v = Vault(root_plain)
    r = sources.run(v, anki.source(_legacy_apkg(tmp_path), NOW), C)
    # Default has no cards; the filtered deck's card counts in Optics
    assert r["imported"] == 2 and r["reasons"] == {"empty deck": 2}
    assert _pages(v) == ["deck-anki-physics", "deck-anki-physics-optics"]
    optics = _page(v, "deck-anki-physics-optics")
    assert 'title: "Anki: Physics::Optics"' in optics
    assert 'summary: "6 cards, 2 mature, 80% recalled in the last 30 days, 2 leeches."' in optics
    assert "Parent deck: Physics." in optics
    assert ("Cards: 1 new, 2 learning, 1 young, 2 mature (an interval of 21 days or more)."
            in optics)
    assert "Reviews: 28 in the last 30 days, 38 in the last 365 days." in optics
    assert "Recalled in the last 30 days: 80% of 25 review answers" in optics
    assert f"Last review: {importers._day(NOW - 86_400)}." in optics
    assert "## Leeches (cards forgotten again and again)\n- Snell's law\n" \
           "- Total internal reflection see here\n" in optics  # tagged first
    for gone in ("<", "alert", "a.png", "evil.example", "sound:", "the back"):
        assert gone not in optics.split("## Leeches")[1], gone
    physics = _page(v, "deck-anki-physics")
    assert "8 cards, 3 mature, 83% recalled in the last 30 days, 2 leeches." in physics
    assert "Cards (with its subdecks): 2 new, 2 learning, 1 young, 3 mature" in physics
    assert "Reviews: 33 in the last 30 days" in physics and "Parent deck" not in physics


def test_both_schemas_and_a_profile_wal_give_the_same_pages(root_plain, tmp_path):
    a, b = Vault(root_plain), Vault(_vault_copy(root_plain, tmp_path))
    sources.run(a, anki.source(_legacy_apkg(tmp_path), NOW), C)
    d = _profile(tmp_path)
    with closing(sqlite3.connect(d / anki.COLLECTION)) as con:  # the newest review taken out
        con.execute("DELETE FROM revlog WHERE id = ?", (int(NOW * 1000) - DAY,))
        con.commit()
    con = sqlite3.connect(d / anki.COLLECTION)  # Anki holds it open, that review in its WAL
    try:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA wal_autocheckpoint = 0")
        con.execute("INSERT INTO revlog VALUES (?, 11, 0, 1, 1, 1, 2500, 5000, 1)",
                    (int(NOW * 1000) - DAY,))
        con.commit()
        assert (d / (anki.COLLECTION + "-wal")).stat().st_size > 0
        assert importers.detect(d) == ["anki"]
        sources.run(b, anki.source(d, NOW), C)
    finally:
        con.close()
    assert _bodies(a) == _bodies(b) and len(_bodies(a)) == 2


def _vault_copy(root: Path, tmp: Path) -> Path:
    import shutil
    return Path(shutil.copytree(root, tmp / "second-vault"))


def test_subdecks_and_concepts_are_linked_and_a_public_page_skips_private_ones(root_plain,
                                                                             tmp_path):
    note(root_plain, "concepts/light-basics.md", 'id: "light-basics"\ntitle: "Light basics"\n'
         'aliases: ["OPTICS"]\n', "# Light basics")
    note(root_plain, "concepts/physics-diary.md", 'id: "physics-diary"\ntitle: "Physics"\n'
         "private: true\n", "# Physics")
    v, apkg = Vault(root_plain), _legacy_apkg(tmp_path)
    sources.run(v, anki.source(apkg, NOW), C)
    assert anki.link(v) == 1
    f = v.read_path(v.root / "resources" / "deck-anki-physics-optics.md").fields
    assert list(f["related"]) == ["[[deck-anki-physics]]"]
    assert list(f["covers"]) == ["[[light-basics]]"]
    assert not v.read_path(v.root / "resources" / "deck-anki-physics.md").fields["covers"]
    assert anki.link(v) == 0  # already linked
    # a private import may link the private concept
    w = Vault(_vault_copy(root_plain, tmp_path))
    for p in (w.root / "resources").glob("deck-*.md"):
        p.unlink()
    sources.run(w, anki.source(apkg, NOW), L)
    anki.link(w)
    f = w.read_path(w.root / "resources" / "deck-anki-physics.md").fields
    assert f["private"] is True and list(f["covers"]) == ["[[physics-diary]]"]


def test_a_second_import_updates_in_place(root_plain, tmp_path):
    v, apkg = Vault(root_plain), _legacy_apkg(tmp_path)
    sources.run(v, anki.source(apkg, NOW), C)
    anki.link(v)
    linked = _page(v, "deck-anki-physics-optics")
    again = sources.run(v, anki.source(apkg, NOW), C)
    assert again["imported"] == again["updated"] == 0 and again["duplicate"] == 2
    assert _page(v, "deck-anki-physics-optics") == linked
    later = sources.run(v, anki.source(apkg, NOW + 40 * 86_400), C)  # the 30-day window moved
    assert later["imported"] == 0 and later["updated"] == 2
    assert _pages(v) == ["deck-anki-physics", "deck-anki-physics-optics"]
    page = _page(v, "deck-anki-physics-optics")
    assert "not enough reviews in the last 30 days" in page
    assert "related:" in page and "[[deck-anki-physics]]" in page  # the link stays


def test_the_newest_format_and_broken_files_are_refused(root_plain, tmp_path):
    stub = _sqlite(tmp_path / "stub.db", 11)
    new = _apkg(tmp_path / "new", {"collection.anki21b": b"\x28\xb5\x2f\xfdzstd",
                                   "collection.anki2": stub})
    assert importers.detect(new) == ["anki"]
    with pytest.raises(VaultError) as e:
        anki.source(new)
    assert 'tick "Support older Anki versions"' in str(e.value)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "notzip.apkg").write_bytes(b"not a zip at all")
    _apkg(bad, {"collection.anki2": b"SQLite format 3\x00 but truncated"}, "garbage.apkg")
    with closing(sqlite3.connect(tmp_path / "empty.db")) as con:
        con.execute("CREATE TABLE other (x)")
        con.commit()
    _apkg(bad, {"collection.anki21": tmp_path / "empty.db"}, "other.apkg")
    _apkg(bad, {"meta": b""}, "nothing.apkg")
    for f in sorted(bad.iterdir()):
        with pytest.raises(VaultError) as e:
            anki.source(f)
        assert f"Cannot read {f.name}: it is not an Anki collection" in str(e.value), f.name
    with pytest.raises(VaultError):
        anki.source(tmp_path / "missing-folder")


def test_a_strangers_collection_is_only_read(root_plain, tmp_path):
    """A shared deck is untrusted: a generated column calling a function and triggers that
    would write never run (trusted_schema off, a read-only copy, plain SELECTs): the import
    reads it like any other, or refuses it in one line."""
    calls = []
    tables = TABLES.replace("flags integer,\n    data text);\nCREATE TABLE revlog",
                            "flags integer,\n    data text, doubled integer GENERATED ALWAYS AS "
                            "(evil(ivl)) VIRTUAL);\nCREATE TABLE revlog")
    assert "evil(ivl)" in tables
    with closing(sqlite3.connect(tmp_path / "hostile.db")) as con:
        con.create_function("evil", 1, lambda x: calls.append(x) or x, deterministic=True)
        _fill(con, 11, tables)
        con.executescript("""
            CREATE TRIGGER wipe_notes AFTER UPDATE ON cards BEGIN DELETE FROM notes; END;
            CREATE TRIGGER wipe_cards BEFORE DELETE ON revlog BEGIN DELETE FROM cards; END;
            CREATE VIEW every_card AS SELECT evil(ivl) FROM cards;""")
        con.commit()
    made = len(calls)
    apkg = _apkg(tmp_path / "shared", {"collection.anki21": tmp_path / "hostile.db"})
    try:
        src = anki.source(apkg, NOW)
    except VaultError as e:
        assert "Cannot read Physics.apkg: it is not an Anki collection" in str(e)
    else:
        v = Vault(root_plain)
        assert sources.run(v, src, C)["imported"] == 2
        assert "6 cards, 2 mature, 80% recalled" in _page(v, "deck-anki-physics-optics")
    assert len(calls) == made  # the function was never called by the import


def test_a_folder_with_a_stray_export_keeps_its_other_kind(tmp_path):
    d = tmp_path / "Downloads"
    d.mkdir()
    (d / "lecture notes.md").write_text("# Optics\n\nSnell's law.\n", encoding="utf-8")
    _legacy_apkg(tmp_path).replace(d / "Physics.apkg")
    assert importers.detect(d) == ["notes", "anki"]
    (d / "lecture notes.md").unlink()
    assert importers.detect(d) == ["anki"]
    assert importers.detect(_profile(tmp_path)) == ["anki"]  # collection.media is not walked
    assert importers.detect(d / "Physics.apkg") == ["anki"]


def test_import_never_changes_the_source(root_plain, tmp_path):
    apkg, d = _legacy_apkg(tmp_path), _profile(tmp_path)
    con = sqlite3.connect(d / anki.COLLECTION)
    try:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA wal_autocheckpoint = 0")
        con.execute("UPDATE cards SET ivl = 50 WHERE id = 11")
        con.commit()
        files = [apkg, *sorted(d.iterdir())]
        before = {f: (f.read_bytes() if f.is_file() else None, f.stat().st_mtime_ns)
                  for f in files}
        listing = sorted(os.listdir(d))
        v = Vault(root_plain)
        sources.run(v, anki.source(apkg, NOW), C)
        sources.run(v, anki.source(d, NOW), C)
        assert {f: (f.read_bytes() if f.is_file() else None, f.stat().st_mtime_ns)
                for f in files} == before
        assert sorted(os.listdir(d)) == listing  # no -shm or temp file beside Anki's
    finally:
        con.close()


def test_card_text_is_one_safe_line():
    assert anki.card_text(NASTY) == "Total internal reflection see here"
    assert anki.card_text("{{c1::Paris::city}} is the capital of {{c2::France}}") == \
        "Paris is the capital of France"
    assert anki.card_text('<img src="only.png">') == "(no text)"
    assert anki.card_text("&lt;script&gt;x&lt;/script&gt; [x](http://a.b) " + "w " * 60) \
        .startswith("x x w w")
    assert len(anki.card_text("long " * 40)) <= 80


def test_cli_imports_an_export_and_links_it(root_plain, tmp_path):
    note(root_plain, "concepts/light-basics.md", 'id: "light-basics"\ntitle: "Optics"\n', "# O")
    apkg = _legacy_apkg(tmp_path)
    out = runner.invoke(app, ["import", str(apkg), "--vault", str(root_plain), "--tier",
                              "normal", "--dry-run"]).output
    assert "Anki: read 4, imported 2" in out and "dry run" in out
    assert not _pages(Vault(root_plain))
    out = runner.invoke(app, ["import", str(apkg), "--vault", str(root_plain), "--tier",
                              "normal"]).output
    assert "Anki: tier normal" in out and "Anki: read 4, imported 2, private 0" in out
    f = Vault(root_plain).read_path(root_plain / "resources" / "deck-anki-physics-optics.md")
    assert list(f.fields["covers"]) == ["[[light-basics]]"]
    out = runner.invoke(app, ["import", str(apkg), "--vault", str(root_plain), "--source",
                              "anki"]).output
    assert "Anki: read 4, imported 0, private 0, duplicate 2" in out
