"""Anki (R22): one `sources.Source` page per deck, so an agent sees what the owner studies in
Anki and how well it is sticking, without a page per card or a second scheduler. Anki's reviews
never become Cortex grades (levels.py is untouched).

What it reads, from a temp copy opened read-only (`mode=ro`), never Anki's own file:
- an `.apkg` or `.colpkg` export: its `collection.anki21`, else its `collection.anki2` (an
  export holding `collection.anki21b`, Anki's zstd format, keeps a stub `collection.anki2`
  beside it, so that one is refused with how to export again);
- Anki's profile collection (`collection.anki2`, given as the file or its profile folder), with
  its `-wal`, as the Zotero import copies Zotero's.
Both schemas: the legacy one (decks as JSON in `col.decks`, names split by `::`) and schema 18
(a `decks` table, names split by \\x1f). Tables read: decks, notes (first field and tags),
cards (deck, type, interval, lapses), revlog (time, card, button, type). A card in a filtered
deck counts in its home deck (`odid`).

A deck's page counts its cards with its subdecks' (as Anki's deck list does); each subdeck has
its own page too. `link` then points each page at its parent deck's (`related`) and at a concept
whose title or alias is the deck's last name part (`covers`).
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from cortex import importers, render, sources
from cortex.life_sources import html_text  # HTML to text, as mail bodies and Zotero notes
from cortex.vault import Vault, VaultError, is_private

LABEL = "Anki"
PACKAGES = (".apkg", ".colpkg")
COLLECTION = "collection.anki2"  # a profile's collection; an old export's too
MATURE = 21  # days: Anki's young/mature split
MIN_REVIEWS = 20  # review answers in 30 days under which no retention is given
LEECHES = 10  # leeches listed on a page
DAY_MS = 86_400_000
EXPORT_AGAIN = ('in Anki, choose File > Export, tick "Support older Anki versions" and export '
                "again")
_SOUND = re.compile(r"\[sound:[^\]]*\]")
_CLOZE = re.compile(r"\{\{c\d+::(.*?)(?:::[^{}]*?)?\}\}", re.S)
_STUB = sources.Source("anki", LABEL, "file", tuple)  # `source_pages`' labels only


def holds(path: Path | str) -> bool:
    """Whether `path` is an Anki export or collection, or a folder holding one (a profile
    folder, or .apkg/.colpkg files at its top)."""
    p = Path(path)
    try:
        if p.is_dir():
            return (p / COLLECTION).is_file() or any(
                c.suffix.lower() in PACKAGES and c.is_file() for c in p.iterdir())
    except OSError:
        return False
    return p.suffix.lower() in PACKAGES or p.name.lower() in (COLLECTION, "collection.anki21")


def _inputs(path: Path) -> list[Path]:
    if path.is_dir():
        if (path / COLLECTION).is_file():
            return [path / COLLECTION]
        got = sorted(c for c in path.iterdir() if c.suffix.lower() in PACKAGES and c.is_file())
    else:
        got = [path] if path.is_file() else []
    if not got:
        raise VaultError("invalid_argument", f"Found no Anki collection in {path.name}: give an "
                         ".apkg or .colpkg export, or Anki's profile folder (it holds "
                         f"{COLLECTION}).")
    return got


def _bad(name: str, why: object = "") -> VaultError:
    return VaultError("invalid_argument", f"Cannot read {name}: it is not an Anki collection "
                      f"Cortex can read{f' ({why})' if why else ''}.")


def _copy(f: Path, tmp: Path) -> Path:
    """A copy of `f`'s collection in `tmp`: the export's member, or the profile's file and its
    -wal. Anki's own files are only read."""
    out = tmp / "collection"
    if f.suffix.lower() not in PACKAGES:
        for name in (f.name, f.name + "-wal"):  # the WAL may hold reviews not checkpointed yet
            if name == f.name or (f.parent / name).is_file():
                try:
                    shutil.copyfile(f.parent / name, out.with_name(out.name + name[len(f.name):]))
                except OSError:
                    raise VaultError("busy", "Anki holds its collection; close Anki and try "
                                     "again.") from None
        return out
    try:
        with zipfile.ZipFile(f) as z:
            members = {i.filename: i for i in z.infolist()}
            if "collection.anki21b" in members and "collection.anki21" not in members:
                raise VaultError("invalid_argument", f"{f.name} is in Anki's newest export "
                                 f"format, which Cortex does not read: {EXPORT_AGAIN}.")
            i = members.get("collection.anki21") or members.get(COLLECTION)
            if i is None:
                raise _bad(f.name, "no collection in it")
            if i.file_size > importers.MAX_TOTAL:
                raise importers._too_big(f.name, importers.MAX_TOTAL)
            with z.open(i) as src, out.open("wb") as dst:  # stops at the declared size
                shutil.copyfileobj(src, dst, importers.STREAM_CHUNK)
    except (OSError, zipfile.BadZipFile, NotImplementedError, EOFError) as e:
        if isinstance(e, NotImplementedError):  # a zstd-compressed member
            raise VaultError("invalid_argument", f"{f.name} is in Anki's newest export "
                             f"format, which Cortex does not read: {EXPORT_AGAIN}.") from None
        raise _bad(f.name, e) from None
    return out


def decks(path: Path | str, now: float | None = None) -> list[dict]:
    """Every deck of the collections at `path` (see `_inputs`), with its own counts (`_read`)."""
    now_ms = int((time.time() if now is None else now) * 1000)
    out = []
    for f in _inputs(Path(path)):
        with tempfile.TemporaryDirectory(prefix="cortex-anki-") as tmp:
            db = _copy(f, Path(tmp))
            try:
                with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as con:
                    out += _read(con, now_ms)
            except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError) as e:
                raise _bad(f.name, e) from None
    return out


def _read(con: sqlite3.Connection, now_ms: int) -> list[dict]:
    q = con.execute
    tables = {r[0] for r in q("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"cards", "notes", "revlog"} <= tables or not tables & {"decks", "col"}:
        raise ValueError("no cards, notes, revlog or decks tables")
    if "decks" in tables:  # schema 18: a table, names split by \x1f
        names = {int(i): str(n).replace("\x1f", "::") for i, n in q("SELECT id, name FROM decks")}
    else:  # legacy: one JSON object in col.decks
        names = {int(d["id"]): str(d["name"])
                 for d in json.loads(q("SELECT decks FROM col").fetchone()[0]).values()}
    home = "CASE WHEN c.odid != 0 THEN c.odid ELSE c.did END"  # a filtered deck's card: its own
    own = {i: {"id": i, "name": n, "new": 0, "learning": 0, "young": 0, "mature": 0, "r30": 0,
               "r365": 0, "rev30": 0, "again30": 0, "last": 0, "newest": 0, "leeches": {}}
           for i, n in names.items()}
    for d, new, learning, young, mature, newest in q(f"""SELECT {home}, SUM(c.type = 0),
            SUM(c.type IN (1, 3)), SUM(c.type = 2 AND c.ivl < {MATURE}),
            SUM(c.type = 2 AND c.ivl >= {MATURE}), MAX(c.id) FROM cards c GROUP BY 1"""):
        if d in own:
            own[d].update(new=new, learning=learning, young=young, mature=mature, newest=newest)
    m30, m365 = now_ms - 30 * DAY_MS, now_ms - 365 * DAY_MS
    for d, r30, r365, rev30, again30, last in q(f"""SELECT {home},
            SUM(r.id >= ? AND r.ease BETWEEN 1 AND 4), SUM(r.id >= ? AND r.ease BETWEEN 1 AND 4),
            SUM(r.id >= ? AND r.type = 1), SUM(r.id >= ? AND r.type = 1 AND r.ease = 1),
            MAX(r.id) FROM revlog r JOIN cards c ON c.id = r.cid GROUP BY 1""",
                                                (m30, m365, m30, m30)):
        if d in own:
            own[d].update(r30=r30 or 0, r365=r365 or 0, rev30=rev30 or 0, again30=again30 or 0,
                          last=last or 0)
    from cortex.levels import LEECH_LAPSES  # levels loads fsrs: only when a collection is read
    for d, nid, flds, tags, lapses in q(f"""SELECT {home}, n.id, n.flds, n.tags, c.lapses
            FROM cards c JOIN notes n ON n.id = c.nid
            WHERE c.lapses >= ? OR lower(' ' || n.tags || ' ') LIKE '% leech %'""",
                                        (LEECH_LAPSES,)):
        if d in own:  # a note is one leech, however many of its cards lapse
            tagged = "leech" in str(tags).casefold().split()
            got = own[d]["leeches"].get(nid)
            if got is None or (tagged, lapses) > got[:2]:
                own[d]["leeches"][nid] = (tagged, lapses, card_text(str(flds).split("\x1f")[0]))
    return list(own.values())


def card_text(field: str) -> str:
    """A card's first field as one safe line: sounds dropped, clozes as their answers, HTML
    tags (scripts and styles with their content) stripped, entities decoded, links, images and
    URLs dropped, at most 80 characters."""
    return render.plain(html_text(_CLOZE.sub(r"\1", _SOUND.sub("", field))), limit=80) \
        or "(no text)"


def _tree(all_decks: list[dict]) -> list[dict]:
    """Each deck with its subdecks' counts added (Anki's deck list counts them so); decks with
    no cards in them or under them are dropped by `item`."""
    out = []
    for d in all_decks:
        low = d["name"].casefold()
        kids = [x for x in all_decks if x["name"].casefold().startswith(low + "::")]
        t = {**d, "subdecks": bool(kids), "leeches": dict(d["leeches"])}
        for k in kids:
            for f in ("new", "learning", "young", "mature", "r30", "r365", "rev30", "again30"):
                t[f] += k[f]
            t["last"], t["newest"] = max(t["last"], k["last"]), max(t["newest"], k["newest"])
            for nid, lee in k["leeches"].items():
                if nid not in t["leeches"] or lee[:2] > t["leeches"][nid][:2]:
                    t["leeches"][nid] = lee
        out.append(t)
    return out


def _day(ms: int) -> str:
    return importers._day(ms / 1000)


def item(d: dict) -> sources.Item | str:
    """One deck's page (see the module docstring), or "empty deck"."""
    cards = d["new"] + d["learning"] + d["young"] + d["mature"]
    if not cards:
        return "empty deck"
    name = render.plain(d["name"], limit=200) or "Default"
    lee = sorted(d["leeches"].values(), key=lambda x: (not x[0], -x[1], x[2]))
    n = len(lee)
    enough = d["rev30"] >= MIN_REVIEWS
    pct = round(100 * (d["rev30"] - d["again30"]) / d["rev30"]) if enough else 0
    recall = (f"{pct}% recalled in the last 30 days" if enough else
              "not enough reviews in the last 30 days to say how much is recalled")
    lines = [f"{cards:,} cards, {d['mature']:,} mature, {recall}, {n:,} "
             f"leech{'es' * (n != 1)}.", ""]
    if "::" in name:
        lines.append(f"Parent deck: {name.rsplit('::', 1)[0]}.")
    lines += [
        f"Cards{' (with its subdecks)' if d['subdecks'] else ''}: {d['new']:,} new, "
        f"{d['learning']:,} learning, {d['young']:,} young, {d['mature']:,} mature (an "
        f"interval of {MATURE} days or more).",
        f"Reviews: {d['r30']:,} in the last 30 days, {d['r365']:,} in the last 365 days.",
        f"Recalled in the last 30 days: {pct}% of {d['rev30']:,} review answers (not answered "
        "Again)." if enough else
        f"Recalled in the last 30 days: not enough reviews ({d['rev30']:,} review answers; "
        f"{MIN_REVIEWS} needed).",
        f"Last review: {_day(d['last'])}." if d["last"] else "Last review: never."]
    if lee:
        lines += ["", "## Leeches (cards forgotten again and again)",
                  *(f"- {text}" for _, _, text in lee[:LEECHES])]
        if n > LEECHES:
            lines.append(f"- and {n - LEECHES:,} more")
    day = _day(d["last"] or d["newest"]) if d["last"] or d["newest"] else \
        importers._day(datetime.now(UTC).timestamp())
    return sources.Item(f"{LABEL}: {name}", day, "\n".join(lines) + "\n", key=f"anki {d['id']}")


def source(path: Path | str, now: float | None = None) -> sources.Source:
    """Every deck of the collections at `path` as a page, keyed by the deck's id, so a re-import
    updates the page in place (unless the owner edited it). Read now: a bad file refuses here."""
    got = [item(d) for d in _tree(decks(path, now))]
    return sources.Source("anki", LABEL, "file", lambda: iter(got), prefix="deck",
                          skips=("empty deck",), updates=True, keeps_edits=True)


def link(vault: Vault) -> int:
    """After an import: each Anki page's `related` gets its parent deck's page and its `covers`
    a concept whose title or alias is the deck's last name part (case-insensitive). A public
    page never links a private one. Links are only added, never removed; the pages changed."""
    from cortex.index import _edge_targets, aliases
    pages = {}
    for p in sources.source_pages(vault, _STUB):
        try:
            n = vault.read_path(p)
        except VaultError:
            continue
        pages[str(n.fields.get("title") or "").casefold()] = n
    concepts: dict[str, object] = {}
    for c in vault.iter_notes("concept"):
        for name in (str(c.fields.get("title") or ""), *aliases(c.fields)):
            if name.strip():
                concepts.setdefault(name.strip().casefold(), c)
    head, made = f"{LABEL.casefold()}: ", 0
    for title, n in pages.items():
        name, hidden = title.removeprefix(head), is_private(n)
        want = {"related": pages.get(head + name.rsplit("::", 1)[0]) if "::" in name else None,
                "covers": concepts.get(name.rsplit("::", 1)[-1].strip())}
        values = {}
        for key, to in want.items():
            if to is None or (is_private(to) and not hidden):
                continue
            have = n.fields.get(key) or []
            have = [have] if isinstance(have, str) else list(have)
            if to.id not in _edge_targets(have):
                values[key] = [*have, f"[[{to.id}]]"]
        if values:
            vault._edit_fields("resource", n.id, values)
            made += 1
    return made
