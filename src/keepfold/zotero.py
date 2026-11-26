"""Zotero (R18): one `sources.Source` page per item of the owner's Zotero library, read from a
temp copy of its zotero.sqlite (Zotero holds the live file while it runs; its WAL file is copied
with it), read-only. `keepfold import --source zotero`, and a watched source (keepfold.watch).

Where the library is (Zotero's documentation: zotero.org/support/zotero_data and
zotero.org/support/kb/profile_directory): the data directory the default profile's prefs.js sets
(`extensions.zotero.useDataDir` true and `extensions.zotero.dataDir`), else `~/Zotero`. Profiles
are listed in profiles.ini under %APPDATA%\\Zotero\\Zotero (Windows), ~/Library/Application
Support/Zotero (macOS) or ~/.zotero/zotero (Linux).

Tables read (the Zotero 5-7 schema): items, libraries (the 'user' library only: a group library
holds other people's items), deletedItems (the trash is left out), itemTypesCombined, itemData,
itemDataValues, fieldsCombined and baseFieldMappingsCombined (title, date, publication, DOI, URL,
abstract), creators and itemCreators, tags and itemTags, itemNotes (the owner's notes, HTML),
itemAttachments (a stored file is storage/<its key>/<name>; a linked file is its own path) and,
from Zotero 6, itemAnnotations (highlights, underlines and notes on a PDF, with comments).

A page per top-level item: its kind, venue, year and authors in the first line, DOI, URL and tags,
then `## Abstract`, `## My notes`, `## My highlights` and each attached PDF's or .docx's full text
through the documents import's reader (more_sources: the parse child, OCR for scans). Keyed by the
item's key with `updates`, so an edited item rewrites its page, unless the owner edited the page
(`keeps_edits`). Nothing is written to Zotero's files and nothing leaves the computer.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from keepfold import importers, more_sources, sources
from keepfold.life_sources import html_text  # HTML to text, as mail bodies
from keepfold.vault import VaultError

DB = "zotero.sqlite"
LABEL = "Zotero"
FIND = ("Zotero's data folder (in Zotero: Settings, Advanced, Files and Folders, Show Data "
        "Directory)")
AUTHORS = 10  # authors named on a page; the rest are counted
_PREF = re.compile(r'user_pref\("extensions\.zotero\.(dataDir|useDataDir)",\s*(.+?)\);')
_FIELDS = ("title", "date", "publicationTitle", "DOI", "url", "abstractNote")
_MARKED = (1, 5)  # highlight, underline: their text is kept; a note, image or ink: its comment
_TRASH = "SELECT itemID FROM deletedItems"


def _profiles() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / "Zotero" / "Zotero"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Zotero"
    return home / ".zotero" / "zotero"


def _custom() -> Path | None:
    """The data directory the default profile's prefs.js sets, or None."""
    root, ini = _profiles(), configparser.ConfigParser(interpolation=None)
    try:
        ini.read(root / "profiles.ini", encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError):
        return None
    rows = [ini[s] for s in ini.sections() if ini[s].get("path")]
    if not rows:
        return None
    p = next((r for r in rows if r.get("default") == "1"), rows[-1])
    prof = root.joinpath(*p["path"].split("/")) if p.get("isrelative") == "1" else Path(p["path"])
    try:
        prefs = dict(_PREF.findall((prof / "prefs.js").read_text("utf-8", errors="replace")))
        if prefs.get("useDataDir") == "true" and prefs.get("dataDir"):
            return Path(json.loads(prefs["dataDir"]))
    except (OSError, ValueError, TypeError):
        pass
    return None


def data_dir() -> Path | None:
    """Where this computer's Zotero keeps zotero.sqlite, or None."""
    return next((d for d in (_custom(), Path.home() / "Zotero")
                 if d is not None and (d / DB).is_file()), None)


def find(path: Path | str | None = None) -> Path:
    """The data folder holding zotero.sqlite: `path` (the folder or the database itself), else
    `data_dir()`."""
    if path is None:
        if (d := data_dir()) is None:
            raise VaultError("invalid_argument", f"Found no Zotero library on this computer; "
                             f"give {FIND}.")
        return d
    path = Path(path)
    d = path.parent if path.name == DB else path
    if not (d / DB).is_file():
        raise VaultError("invalid_argument", f"Found no {DB} in {d}; give {FIND}.")
    return d


def stat(d: Path) -> list[int] | None:
    """The database's [mtime_ns, size] with its WAL's, a watched library's quick check."""
    out = []
    for name in (DB, DB + "-wal"):
        try:
            st = (d / name).stat()
        except OSError:
            if name == DB:
                return None
            continue
        out += [st.st_mtime_ns, st.st_size]
    return out


def _sha(rec: dict) -> str:
    return hashlib.sha256(json.dumps(rec, sort_keys=True).encode()).hexdigest()[:16]


def records(d: Path) -> dict[str, tuple[str, dict]]:
    """Item key -> (a hash of everything its page holds but the files' text, its record): read
    from a temp copy of the library's database, deleted after."""
    with tempfile.TemporaryDirectory(prefix="keepfold-zotero-") as tmp:
        for name in (DB, DB + "-wal"):  # the WAL may hold changes not in the database yet
            if name == DB or (d / name).is_file():
                try:
                    shutil.copyfile(d / name, Path(tmp) / name)
                except OSError:
                    raise VaultError("busy", "Zotero holds its database; close Zotero and try "
                                     "again.") from None
        try:
            with closing(sqlite3.connect(Path(tmp) / DB)) as con:
                con.execute("PRAGMA query_only = ON")
                recs = _read(con, d)
        except sqlite3.Error as e:
            raise VaultError("invalid_argument", f"Cannot read {DB}: {e}") from None
    return {r["key"]: (_sha(r), r) for r in recs}


def _read(con: sqlite3.Connection, d: Path) -> list[dict]:
    q = con.execute
    items = q(f"""SELECT i.itemID, i.key, t.typeName, i.dateAdded FROM items i
        JOIN itemTypesCombined t ON t.itemTypeID = i.itemTypeID
        JOIN libraries l ON l.libraryID = i.libraryID
        WHERE l.type = 'user' AND t.typeName != 'annotation' AND i.itemID NOT IN ({_TRASH})
        AND i.itemID NOT IN (SELECT itemID FROM itemNotes WHERE parentItemID IS NOT NULL)
        AND i.itemID NOT IN (SELECT itemID FROM itemAttachments WHERE parentItemID IS NOT NULL)
        ORDER BY i.itemID""").fetchall()
    fields: dict[int, dict] = {}
    # a type's own field (a case's name) under its base field's name (title)
    for iid, name, value in q("""SELECT d.itemID, COALESCE(bf.fieldName, f.fieldName), v.value
            FROM itemData d JOIN items i ON i.itemID = d.itemID
            JOIN fieldsCombined f ON f.fieldID = d.fieldID
            JOIN itemDataValues v ON v.valueID = d.valueID
            LEFT JOIN baseFieldMappingsCombined b
                ON b.itemTypeID = i.itemTypeID AND b.fieldID = d.fieldID
            LEFT JOIN fieldsCombined bf ON bf.fieldID = b.baseFieldID"""):
        if name in _FIELDS and value is not None:
            fields.setdefault(iid, {}).setdefault(name, str(value).strip())
    people: dict[int, list[str]] = {}
    for iid, first, last, mode in q("""SELECT c.itemID, r.firstName, r.lastName, r.fieldMode
            FROM itemCreators c JOIN creators r ON r.creatorID = c.creatorID
            ORDER BY c.itemID, c.orderIndex"""):
        name = (last or "") if mode == 1 else f"{first or ''} {last or ''}"
        if name := " ".join(name.split()):
            people.setdefault(iid, []).append(name)
    tags: dict[int, list[str]] = {}
    for iid, name in q("""SELECT it.itemID, t.name FROM itemTags it
            JOIN tags t ON t.tagID = it.tagID ORDER BY t.name"""):
        tags.setdefault(iid, []).append(name)
    notes: dict[int, list[str]] = {}
    for iid, parent, html, head in q(f"""SELECT n.itemID, n.parentItemID, n.note, n.title
            FROM itemNotes n JOIN items i ON i.itemID = n.itemID
            JOIN itemTypesCombined t ON t.itemTypeID = i.itemTypeID
            WHERE t.typeName = 'note' AND n.itemID NOT IN ({_TRASH}) ORDER BY n.itemID"""):
        if text := html_text(html or "").strip():
            notes.setdefault(parent or iid, []).append(text)
            if parent is None and (head or "").strip():  # a standalone note: its first line
                fields.setdefault(iid, {}).setdefault("title", head.strip())
    files: dict[int, list[tuple[int, list]]] = {}  # parent -> (attachment, [path, mtime, size])
    for aid, parent, mode, path, key in q(f"""SELECT a.itemID, a.parentItemID, a.linkMode,
            a.path, i.key FROM itemAttachments a JOIN items i ON i.itemID = a.itemID
            WHERE a.itemID NOT IN ({_TRASH}) ORDER BY a.itemID"""):
        if (f := _file(d, mode, path or "", key)) is not None:
            files.setdefault(parent or aid, []).append((aid, f))
    marks: dict[int, list[list]] = {}
    if q("SELECT 1 FROM sqlite_master WHERE name = 'itemAnnotations'").fetchone():
        for att, kind, text, comment, page in q(f"""SELECT a.parentItemID, a.type, a.text,
                a.comment, a.pageLabel FROM itemAnnotations a
                WHERE a.itemID NOT IN ({_TRASH}) ORDER BY a.parentItemID, a.sortIndex"""):
            text = " ".join((text or "").split()) if kind in _MARKED else ""
            if text or (comment or "").strip():
                marks.setdefault(att, []).append([page or "", text, " ".join(
                    (comment or "").split())])
    out = []
    for iid, key, kind, added in items:
        f = fields.get(iid, {})
        atts = files.get(iid, [])
        out.append({"key": key, "kind": kind, "added": added or "", "fields": f,
                    "authors": people.get(iid, []), "tags": tags.get(iid, []),
                    "notes": notes.get(iid, []),
                    "marks": [m for aid, _ in atts for m in marks.get(aid, [])]
                    + marks.get(iid, []),  # a standalone PDF's own highlights
                    "files": [x for _, x in atts]})
    return out


def _file(d: Path, mode: int | None, path: str, key: str) -> list | None:
    """[path, mtime_ns, size] of an attachment's PDF or .docx on this computer, or None: a
    stored file is storage/<key>/<name> (a bare name after "storage:"); a linked file its own
    absolute path. A file not downloaded yet, a link and a base-folder-relative path are left
    out."""
    if mode in (0, 1) and path.startswith("storage:"):
        name = path[len("storage:"):]
        if not name or "/" in name or "\\" in name or name in ("..", ".") or name.startswith("."):
            return None
        p = d / "storage" / key / name
    elif mode == 2 and Path(path).is_absolute():
        p = Path(path)
    else:
        return None
    if p.suffix.lower() not in (".pdf", ".docx"):
        return None
    try:
        st = p.stat()
    except OSError:
        return None
    return [str(p), st.st_mtime_ns, st.st_size]


def _day(added: str) -> str:
    """Zotero's dateAdded (UTC "YYYY-MM-DD HH:MM:SS") as a Keepfold day."""
    try:
        return importers._day(datetime.strptime(added[:19], "%Y-%m-%d %H:%M:%S")
                              .replace(tzinfo=UTC).timestamp())
    except ValueError:
        return added[:10] or importers._day(datetime.now(UTC).timestamp())


def _kind(name: str) -> str:
    """"journalArticle" -> "journal article"."""
    return re.sub(r"(?<=[a-z])([A-Z])", lambda m: " " + m.group(1).lower(), name) or "item"


def _mark(m: list) -> str:
    """A highlight's line: its page, the text it marks and the owner's comment."""
    page, text, note = m
    said = (f'"{text}"' + (f" (my note: {note})" if note else "")) if text else f"My note: {note}"
    return f"- {'p. ' + page + ': ' if page else ''}{said}"


def item(rec: dict, parse) -> sources.Item:
    """One record's page (see the module docstring); `parse` is more_sources' parse child."""
    f, names = rec["fields"], rec["authors"]
    title = f.get("title") or "Untitled"
    year = m.group(0) if (m := re.search(r"\b\d{4}\b", f.get("date", ""))) else ""
    who = ", ".join(names[:AUTHORS]) + (f" and {len(names) - AUTHORS} more"
                                         if len(names) > AUTHORS else "")
    venue = f.get("publicationTitle", "")
    head = [_kind(rec["kind"]).capitalize() + (f" in {venue}" if venue else "")
            + (f" ({year})" if year else "") + (f" by {who}" if who else "") + "."]
    head += [f"{k}: {v}" for k, v in (("DOI", f.get("DOI")), ("URL", f.get("url")),
                                     ("Tags", ", ".join(rec["tags"]))) if v]
    parts = ["\n".join(head)]
    if abstract := f.get("abstractNote"):
        parts.append(f"## Abstract\n{abstract}")
    if rec["notes"]:
        parts.append("## My notes\n" + "\n\n".join(rec["notes"]))
    if rec["marks"]:
        parts.append("## My highlights\n" + "\n".join(map(_mark, rec["marks"])))
    for path, _, size in rec["files"]:
        p = Path(path)
        read = partial(importers._read, p.name, size, partial(p.open, "rb"),
                       [importers.MAX_TOTAL])
        got = next(more_sources._doc_items(p, [(p.name, size, 0.0, read)], (), parse), None)
        if isinstance(got, sources.Item):
            parts.append(f"## File: {p.name}\n{got.text}")
    return sources.Item(title, _day(rec["added"]), "\n\n".join(parts),
                        f"zotero://select/library/items/{rec['key']}", key=f"zotero {rec['key']}",
                        who=who, summary=f.get("abstractNote", ""))


def source(path: Path | str | None = None, recs: list[dict] | None = None) -> sources.Source:
    """The library at `path` (else found, `find`): every item, or only `recs` (a watched pass's
    new and changed ones)."""
    d = find(path)
    if recs is None:
        recs = [r for _, r in records(d).values()]

    def items():
        parse = more_sources._Parser()
        try:
            for rec in recs:
                yield item(rec, parse)
        finally:
            parse.close()

    return sources.Source("zotero", LABEL, "folder", items, updates=True, keeps_edits=True)
