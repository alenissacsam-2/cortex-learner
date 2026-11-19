"""`cortex import`: notes (an Obsidian vault, a Notion "Markdown & CSV" export, any folder of
.md/.txt) and calendars (.ics), plus `detect`, which tells the CLI which importer an export
needs (chat exports go to `chats.import_export`, Google activity to `browse.takeout`).

Imported notes and calendar months become `resource` pages in `resources/`, the shape the
15 Nov import left (CORTEX-DESIGN §10.25): `raw/` is never indexed or embedded (§10.2), so a
note kept only there could not be found by find or recall. A `Source: <app> · Date: …` line
marks the page as imported (lint skips covers); it is not a `Timeline:` line, so recall does
not down-rank it as a chat page. Text is secret-redacted, every write goes through the vault.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import re
import zipfile
from collections import defaultdict
from datetime import UTC, datetime
from functools import partial
from pathlib import Path, PurePosixPath

from cortex import chats, render
from cortex.catalog import slugify
from cortex.vault import Vault, VaultError, truthy

MAX_BYTES = 5 * 1024 * 1024  # bigger files are attachments or dumps, not notes
MAX_ENTRY = 200 * 1024 * 1024  # one export file read whole (a calendar, a JSON non-list)
MAX_STREAM = 2 * 1024**3  # C11: one JSON list read element by element (conversations.json)
MAX_TOTAL = 2 * 1024**3  # everything one import decompresses, so a zip bomb fails early
STREAM_CHUNK = 1 << 20
NOTE_SUFFIXES = (".md", ".txt")
LABELS = {"obsidian": "Obsidian", "notion": "Notion", "notes": "Notes"}
# --source value -> the importer it forces
SOURCES = {**dict.fromkeys(chats.EXPORT_DIRS, "chats"), "google-activity": "takeout",
           **dict.fromkeys(LABELS, "notes"), "calendar": "calendar"}

GUIDES = {
    "notes": {
        "name": "Notes (Obsidian, Notion or a folder)",
        "link": "https://www.notion.com/help/export-your-content",
        "steps": ["Obsidian: nothing to export; pick your vault folder (the one holding "
                  ".obsidian). Hidden folders and attachments are skipped.",
                  "Notion: open Settings, then General, and click Export all workspace "
                  "content.",
                  "Set Export format to Markdown & CSV, include everything, and click Export.",
                  "Download the .zip from Notion's email (or the download that starts).",
                  "Any other notes app: export or copy your notes as .md or .txt files into "
                  "one folder."],
        "wait": "instant for a folder; minutes to hours for a large Notion workspace",
        "pick": "the Obsidian vault folder, the Notion .zip as it is, or the folder of notes; "
                "a note with private: true in its frontmatter stays private"},
    "calendar": {
        "name": "Calendar (Google, Outlook or Apple)",
        "link": "https://support.google.com/calendar/answer/37111",
        "steps": ["Google Calendar (on a computer): Settings (gear) > Settings > Import & "
                  "export > Export; a .zip of .ics files downloads.",
                  "Outlook (classic, Windows): open Calendar, File > Save Calendar, click More "
                  "Options, set Date Range to Whole calendar and Detail to Full details, then "
                  "save as iCalendar (.ics).",
                  "Outlook.com: Settings > Calendar > Shared calendars > Publish a calendar, "
                  "pick All details, Publish, then open the ICS link to download the file.",
                  "Apple Calendar (Mac): select a calendar in the list, then File > Export > "
                  "Export and save the .ics (one calendar at a time)."],
        "wait": "instant",
        "pick": "the .ics file, or Google's .zip as it is; attendees' emails are left out"},
}

_NOTION_ID = re.compile(r" [0-9a-f]{32}(?=\.\w+$|$)")
# a lone activity file is Gemini's when a row's product (JSON header, HTML title) in its head names
# Gemini in any language, so a search for "gemini" does not count
_GEMINI_HEAD = re.compile(rb'(?:"header"\s*:\s*"|--title">)[^"<]*Gemini')
_HASH = re.compile(r"· sha: ([0-9a-f]{16})$", re.M)
_FRONT = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_WIKI = re.compile(r"(!?)\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_ESC = re.compile(r"\\([\\;,nN])")


def _files(path: Path) -> list[tuple[str, int, float, partial]]:
    """(relative name, size, mtime, read) of every file in a .zip (and in zips inside it, one
    level: Notion nests its export parts), a folder, or one file. The one reader every
    importer and `detect` use. `read()` refuses a file over MAX_ENTRY (MAX_BYTES for a note)
    or past MAX_TOTAL for the whole call, before it decompresses it; `read(n)` reads only the
    first n bytes; `read(items=True)` streams a JSON list up to MAX_STREAM (json_value)."""
    path, left = Path(path), [MAX_TOTAL]
    try:
        if path.suffix.lower() == ".zip":
            return _zip_files(zipfile.ZipFile(path), left)
        if path.is_dir():
            return [(p.relative_to(path).as_posix(), p.stat().st_size, p.stat().st_mtime,
                     partial(_read, p.relative_to(path).as_posix(), p.stat().st_size,
                             partial(p.open, "rb"), left))
                    for p in sorted(path.rglob("*")) if p.is_file()]
        return [(path.name, path.stat().st_size, path.stat().st_mtime,
                 partial(_read, path.name, path.stat().st_size, partial(path.open, "rb"), left))]
    except (OSError, zipfile.BadZipFile) as e:
        raise VaultError("invalid_argument", f"Cannot read {path.name}: {e}") from None


def _zip_files(z: zipfile.ZipFile, left: list[int], nested: bool = False) -> list:
    out = []
    for i in z.infolist():
        if i.is_dir():
            continue
        if i.filename.lower().endswith(".zip") and not nested:
            out += _zip_files(zipfile.ZipFile(z.open(i)), left, True)
        else:
            out.append((i.filename, i.file_size, datetime(*i.date_time).timestamp(),
                        partial(_read, i.filename, i.file_size, partial(z.open, i), left)))
    return out


def _too_big(name: str, one: int) -> VaultError:
    return VaultError("invalid_argument", f"{name} is too big to import: the limit is "
                      f"{one // 2**20:,} MB for one file and {MAX_TOTAL // 2**20:,} MB "
                      "for a whole export, uncompressed.")


def _read(name: str, size: int, opener, left: list[int], n: int | None = None,
          items: bool = False):
    """A file's bytes, streamed and capped so a zip bomb fails before it fills memory. With
    `items`, an iterator over the elements of its top-level JSON array (see _items)."""
    if items:
        return _items(name, size, opener, left)
    with opener() as f:
        if n is not None:
            return f.read(n)
        one = MAX_BYTES if name.lower().endswith(NOTE_SUFFIXES) else MAX_ENTRY
        cap = min(one, left[0])
        # the declared size is checked first; reading cap + 1 catches a size that lies
        data = f.read(cap + 1) if size <= cap else b""
        if size > cap or len(data) > cap:
            raise _too_big(name, one)
        left[0] -= len(data)
        return data


def _items(name: str, size: int, opener, left: list[int]):
    """C11: the elements of a file's top-level JSON array, decoded one at a time from chunked
    reads, so an export up to MAX_STREAM never sits in memory whole: only one element (at
    most MAX_ENTRY) and one chunk do. Malformed or truncated JSON raises ValueError."""
    cap = min(MAX_STREAM, left[0])
    if size > cap:
        raise _too_big(name, MAX_STREAM)
    dec, utf8 = json.JSONDecoder(), codecs.getincrementaldecoder("utf-8-sig")()
    buf, pos, eof, got = "", 0, False, 0
    with opener() as f:

        def more(n: int = STREAM_CHUNK) -> None:  # keeps buf[pos:], appends the next chunk
            nonlocal buf, pos, eof, got
            chunk = f.read(n)
            got += len(chunk)
            if got > cap:  # a declared size that lies
                raise _too_big(name, MAX_STREAM)
            left[0] -= len(chunk)
            eof = not chunk
            buf, pos = buf[pos:] + utf8.decode(chunk, final=eof), 0

        def peek() -> str:  # the next character that is not whitespace, "" at the end
            nonlocal pos
            while True:
                while pos < len(buf) and buf[pos] in " \t\r\n":
                    pos += 1
                if pos < len(buf) or eof:
                    return buf[pos:pos + 1]
                more()

        if peek() != "[":
            raise ValueError("not a JSON list")
        pos += 1
        if peek() == "]":
            return
        while True:
            peek()
            try:
                item, end = dec.raw_decode(buf, pos)
            except json.JSONDecodeError:
                if eof:
                    raise
                if len(buf) - pos > MAX_ENTRY:
                    raise ValueError(f"an item over {MAX_ENTRY // 2**20:,} MB, or malformed "
                                     "JSON") from None
                more(max(STREAM_CHUNK, len(buf) - pos))  # doubles: a big item parses O(n)
                continue
            if end == len(buf) and not eof:  # a number may go on in the next chunk
                more()
                continue
            pos = end
            sep = peek()
            if sep not in (",", "]"):
                raise ValueError("expected , or ] after an item: the file is cut short or "
                                 "malformed")
            yield item
            if sep == "]":
                return
            pos += 1


def json_value(read):
    """C11: a chat or activity export file's JSON. A top-level array comes back as a lazy
    iterator over its elements (_items: up to MAX_STREAM, memory bounded); anything else,
    including a UTF-16 file, is parsed whole (up to MAX_ENTRY)."""
    if read(64).lstrip(b"\xef\xbb\xbf \t\r\n")[:1] == b"[":
        return read(items=True)
    return json.loads(read())


def detect(path: Path) -> list[str]:
    """The importers an export needs, in order: "chats" (claude.ai, ChatGPT, Gemini,
    Perplexity), "takeout" (Search, YouTube, Chrome activity), "calendar", "notes"."""
    from cortex.browse import takeout  # takeout imports this module
    files = _files(path)
    names = [n.lower() for n, *_ in files]
    # an HTML activity file counts only when its head holds a row (`html_rows` can read it)
    heads = {n: read(takeout.HEAD) for n, (_, _, _, read) in zip(names, files, strict=True)
             if takeout.activity_file(n) == "html"}
    acts = [n for n in names if takeout.activity_file(n) == "json"
            or b'class="outer-cell' in heads.get(n, b"")]
    gemini = [n for n, (_, _, _, read) in zip(names, files, strict=True) if n in acts
              and ("gemini" in n or ("/" not in n and _GEMINI_HEAD.search(
                  heads[n] if n in heads else read(4096))))]
    head = next((read(300).lstrip() for n, _, _, read in files if n.endswith(".md")), b"")
    pplx = head.startswith(b"<img") and b"perplexity" in head  # its export starts with a logo
    json1 = len(names) == 1 and names[0].endswith(".json") and "myactivity" not in names[0]
    kinds = ["chats"] if gemini or pplx or json1 or any(
        n.rsplit("/", 1)[-1].startswith("conversations") and n.endswith(".json")
        for n in names) else []
    if any(takeout.wanted(n) for n in acts if n not in gemini):
        kinds.append("takeout")
    if any(n.endswith(".ics") for n in names):
        kinds.append("calendar")
    if not kinds and heads:
        raise VaultError("invalid_argument", f"{Path(path).name} holds HTML activity but no "
                         "Search, YouTube, Chrome or Gemini Apps rows Cortex can read. "
                         + takeout.READ_HINT)
    if not kinds and any(_is_note(n) for n in names):
        kinds.append("notes")
    return kinds


def _is_note(name: str) -> bool:
    parts = PurePosixPath(name.replace("\\", "/")).parts
    return name.lower().endswith(NOTE_SUFFIXES) and not any(p.startswith(".") for p in parts)


def _is_private(front: str) -> bool:
    """C4: a note is private when its frontmatter sets `private` to anything truthy (true,
    "true", yes, on, 1) or tags it `private`; frontmatter that does not parse is private too,
    as the note may have meant to be (fail closed)."""
    from ruamel.yaml import YAML, YAMLError  # cortex.vault loads it anyway
    try:
        fm = YAML(typ="safe").load(front)
    except (YAMLError, ValueError):
        return True
    if fm is None:
        return False
    if not isinstance(fm, dict):
        return True
    flag = fm.get("private")
    if truthy(flag):
        return True
    tags = fm.get("tags", fm.get("tag"))
    tags = tags.replace(",", " ").split() if isinstance(tags, str) else \
        tags if isinstance(tags, list) else []
    return any(str(t).strip().lstrip("#").casefold() == "private" for t in tags)


def report_line(label: str, r: dict) -> str:
    """The line every import ends with: counts read, imported, private, duplicate and
    skipped, with the reason for each skip."""
    why = {k: n for k, n in r.get("reasons", {}).items() if n}
    reasons = f" ({', '.join(f'{n:,} {k}' for k, n in why.items())})" if why else ""
    return (f"{label}: read {r['read']:,}, imported {r['imported']:,}, private "
            f"{r['private']:,}, duplicate {r['duplicate']:,}, skipped "
            f"{sum(why.values()):,}{reasons}")


def _clean(name: str) -> str:
    """A path without Notion's 32-hex page ids."""
    return "/".join(_NOTION_ID.sub("", p) for p in name.replace("\\", "/").split("/"))


def _free_id(vault: Vault, base: str, taken: set[str]) -> str:
    """`base`, or `base-2`, `-3`… when a page of any kind (ids are unique across kinds) or this
    import already has it."""
    nid, n = base, 2
    while nid in taken or any((vault.root / d / f"{nid}.md").exists()
                              for d in vault.KIND_DIRS.values()):
        nid = f"{base[:78].rstrip('-.')}-{n}"
        n += 1
    taken.add(nid)
    return nid


def _known_hashes(vault: Vault) -> set[str]:
    return {h for p in (vault.root / "resources").glob("*.md")
            for h in _HASH.findall(p.read_text("utf-8", errors="replace"))}


def import_notes(vault: Vault, path: Path, flavor: str | None = None, dry_run: bool = False,
                 private: bool = False) -> dict:
    """One resource page per .md/.txt note (≤ MAX_BYTES, UTF-8 text, outside hidden folders
    such as .obsidian). A note whose frontmatter marks it private (any truthy `private`, a
    `private` tag, or frontmatter that does not parse; every note, with
    `private`) is a private page. A note whose text was imported before (same sha256) is
    skipped, so re-importing is safe; an edited note comes in as a new page."""
    files = _files(path)
    names = [n.replace("\\", "/") for n, *_ in files]
    flavor = flavor or ("obsidian" if any(".obsidian" in n.split("/") for n in names) or
                        (Path(path).is_dir() and (Path(path) / ".obsidian").is_dir())
                        else "notion" if any(_NOTION_ID.search(p) for n in names
                                             for p in n.split("/")) else "notes")
    known, taken, notes, skipped, same = _known_hashes(vault), set(), [], 0, 0
    why = dict.fromkeys(("not a note", "over 5 MB", "not UTF-8 text"), 0)
    for name, size, mtime, read in files:
        data = read() if _is_note(name) and size <= MAX_BYTES else b"\0"
        try:
            text = data.decode("utf-8-sig") if b"\0" not in data else None
        except UnicodeDecodeError:
            text = None
        if text is None:
            skipped += 1
            why["not a note" if not _is_note(name) else "over 5 MB" if size > MAX_BYTES
                else "not UTF-8 text"] += 1
            continue
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        if h in known:
            same += 1
            continue
        known.add(h)
        rel = _clean(name)
        title = PurePosixPath(rel).stem
        fm = _FRONT.match(text)
        hidden = private or bool(fm and _is_private(fm.group(1)))
        nid = _free_id(vault, f"{flavor}-{slugify(title) or 'untitled'}"[:81].rstrip("-."),
                       taken)
        notes.append((nid, title, rel, mtime, h, hidden, text[fm.end():] if fm else text))
    if not dry_run:
        # [[Note]] links between imported public notes keep working; others become plain text
        ids = {slugify(t): nid for nid, t, *_, hidden, _ in notes if not hidden}

        def link(m):
            if m.group(1):  # an embedded image or file
                return ""
            to = ids.get(slugify(PurePosixPath(m.group(2)).stem))
            text = m.group(3) or m.group(2)
            return f"[[{to}|{text}]]" if to else text

        for nid, title, rel, mtime, h, hidden, body in notes:
            day = datetime.fromtimestamp(mtime, UTC).date().isoformat()
            body = chats.redact(_WIKI.sub(link, body)).strip().removeprefix(f"# {title}\n")
            first = next((s.strip("# ").strip() for s in body.splitlines()
                          if s.strip("# ").strip()), title)
            fields = {"title": chats.redact(title), "kind": "resource",
                      "resource_type": "other", "status": "reference", "covers": [],
                      "related": [], "date": day, "summary": first[:160]}
            if hidden:
                fields["private"] = True
            vault.create_note("resource", nid, fields,
                              f"# {fields['title']}\n\nSource: {LABELS[flavor]} · Date: {day} · "
                              f"From: {chats.redact(rel)} · sha: {h}\n\n{body}\n")
    return {"source": flavor, "new": len(notes), "same": same, "skipped": skipped,
            "read": len(files), "imported": len(notes),
            "private": sum(n[5] for n in notes), "duplicate": same, "reasons": why}


def _events(text: str):
    """(properties) per VEVENT: name -> (params, value), the first of each name."""
    ev = None
    for line in re.sub(r"\r?\n[ \t]", "", text).splitlines():  # unfold (RFC 5545 §3.1)
        if line.strip() == "BEGIN:VEVENT":
            ev = {}
        elif line.strip() == "END:VEVENT" and ev is not None:
            yield ev
            ev = None
        elif ev is not None and ":" in line:
            # shortcut: a sub-component's (VALARM) lines land here too; first-wins keeps the
            # event's own, which come first in every export seen
            head, value = line.split(":", 1)
            name, *params = head.split(";")
            ev.setdefault(name.upper(), (params, value))


def _text(prop) -> str:
    v = _ESC.sub(lambda m: " " if m.group(1) in "nN" else m.group(1), prop[1]) if prop else ""
    # other people write these: no addresses, and no remote images or links Obsidian would fetch
    return _EMAIL.sub("[email]", render.plain(v, limit=10_000))


def _when(prop) -> tuple[str, str]:
    """(YYYY-MM-DD, "HH:MM", "HH:MM (TZID)" or "all day"); a UTC time is shown as local."""
    params, v = prop
    v = v.strip()
    if len(v) == 8 or any(p.upper() == "VALUE=DATE" for p in params):
        return datetime.strptime(v[:8], "%Y%m%d").date().isoformat(), "all day"
    t = datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
    if v.endswith("Z"):
        t = t.replace(tzinfo=UTC).astimezone()
    tz = next((p[5:].strip('"') for p in params if p.upper().startswith("TZID=")), "")
    return t.date().isoformat(), t.strftime("%H:%M") + (f" ({tz})" if tz else "")


def import_calendar(vault: Vault, path: Path, dry_run: bool = False,
                    private: bool = False) -> dict:
    """One resource page per month (`calendar-YYYY-MM`, or a private `calendar-private-YYYY-MM`
    with `private`, so a private calendar never joins a public month page) with a line per
    event: date, time, title, location and the RRULE as text (not expanded). Attendees,
    organizers and descriptions are never read. Lines already on a page are skipped, so
    re-importing is safe."""
    texts = [read().decode("utf-8-sig", errors="replace") for n, _, _, read in _files(path)
             if n.lower().endswith(".ics")]
    if not texts:
        raise VaultError("invalid_argument", f"Found no .ics calendar in {Path(path).name}.")
    months, events, undated = defaultdict(set), 0, 0
    for ev in (e for t in texts for e in _events(t)):
        try:
            day, time = _when(ev["DTSTART"])
        except (KeyError, ValueError):
            undated += 1
            continue
        rule = ev.get("RRULE")
        line = " · ".join(x for x in (f"{day} {time}", _text(ev.get("SUMMARY")) or "(no title)",
                                      _text(ev.get("LOCATION")),
                                      f"repeats {rule[1].strip()}" if rule else "") if x)
        months[day[:7]].add(f"- {chats.redact(line)}")
        events += 1
    new = 0
    for month, lines in sorted(months.items()):
        nid = f"calendar-private-{month}" if private else f"calendar-{month}"
        p = vault.path_for("resource", nid)
        have = set(p.read_text("utf-8").splitlines()) if p.is_file() else set()
        add = sorted(lines - have)
        new += len(add)
        if dry_run or not add:
            continue
        if have:
            # shortcut: later imports append, so a month is sorted within each import only
            vault.append_to_section("resource", nid, "Events", "\n".join(add))
            continue
        fields = {"title": f"Calendar {month}", "kind": "resource", "resource_type": "other",
                  "status": "reference", "covers": [], "related": [], "date": f"{month}-01",
                  "summary": f"Calendar events in {month}"}
        if private:
            fields["private"] = True
        vault.create_note("resource", nid, fields, f"# Calendar {month}\n\nSource: Calendar · "
                          f"Date: {month}-01\n\n## Events\n" + "\n".join(add) + "\n")
    return {"events": events, "months": len(months), "new": new, "read": events + undated,
            "imported": new, "private": new if private else 0, "duplicate": events - new,
            "reasons": {"no readable start date": undated}}
