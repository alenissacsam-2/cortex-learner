"""`cortex import`: notes (an Obsidian vault, a Notion "Markdown & CSV" export, any folder of
.md/.txt) and calendars (.ics), plus `detect`, which tells the CLI which importer an export
needs (chat exports go to `chats.import_export`, Google activity to `browse.takeout`).

Imported notes and calendar months become `resource` pages in `resources/`, the shape the
15 Nov import left (CORTEX-DESIGN §10.25): `raw/` is never indexed or embedded (§10.2), so a
note kept only there could not be found by find or recall. A `Source: <app> · Date: …` line
marks the page as imported (lint skips covers); it is not a `Timeline:` line, so recall does
not down-rank it as a chat page. Notes and calendars are `sources.Source`s: `sources.run`
filters, redacts, dedupes and writes them through the vault, at the tier the user chose.
"""

from __future__ import annotations

import codecs
import json
import os
import re
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path, PurePosixPath

from cortex import chats, render, sources
from cortex.vault import Vault, VaultError, truthy

MAX_BYTES = 5 * 1024 * 1024  # bigger files are attachments or dumps, not notes
MAX_ENTRY = 200 * 1024 * 1024  # one export file read whole (a calendar, a JSON non-list)
MAX_STREAM = 2 * 1024**3  # C11: one JSON list read element by element (conversations.json)
MAX_TOTAL = 2 * 1024**3  # everything one import decompresses, so a zip bomb fails early
STREAM_CHUNK = 1 << 20
NOTE_SUFFIXES = (".md", ".txt")
REPORT = ("read", "imported", "private", "duplicate", "reasons", "unreadable")  # report_line's
LABELS = {"obsidian": "Obsidian", "notion": "Notion", "notes": "Notes"}
# --source value -> the importer it forces
SOURCES = {**dict.fromkeys(chats.EXPORT_DIRS, "chats"), "google-activity": "takeout",
           **dict.fromkeys(LABELS, "notes"), "calendar": "calendar", "docs": "docs",
           "repos": "repos", "highlights": "highlights"}

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
    "docs": {
        "name": "Documents (PDF, Word, text)",
        "link": "",
        "steps": ["Nothing to export: put the PDFs, Word (.docx), .md and .txt files you want "
                  "Cortex to know into one folder (subfolders are fine).",
                  "Each document becomes one page: its title, its date and its first 1,500 "
                  "characters. Encrypted and scanned (image only) PDFs are skipped."],
        "wait": "instant",
        "pick": "paste the full path of the folder"},
    "repos": {
        "name": "Your code projects (git)",
        "link": "",
        "steps": ["Nothing to export: pick the folder that holds your git projects, such as "
                  "C:\\Users\\you\\Projects. Projects one or two folders down are found.",
                  "Each project becomes one page: its README's start, its languages (from file "
                  "names such as package.json), and your commit count, dates and last 20 commit "
                  "messages. Code, diffs and other files are never read."],
        "wait": "seconds",
        "pick": "paste the full path of the folder of projects"},
    "highlights": {
        "name": "Reading highlights (Kindle, Readwise, Pocket)",
        "link": "https://readwise.io/export",
        "steps": ["Kindle: plug it in by USB and copy documents/My Clippings.txt.",
                  "Readwise: open readwise.io/export and click Export to CSV.",
                  "Pocket: the export .csv (or its .zip), if you saved one."],
        "wait": "instant",
        "pick": "My Clippings.txt, the Readwise .csv, or the Pocket .csv or .zip"},
}

_NOTION_ID = re.compile(r" [0-9a-f]{32}(?=\.\w+$|$)")
# a lone activity file is Gemini's when a row's product (JSON header, HTML title) in its head names
# Gemini in any language, so a search for "gemini" does not count
_GEMINI_HEAD = re.compile(rb'(?:"header"\s*:\s*"|--title">)[^"<]*Gemini')
_FRONT = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_ESC = re.compile(r"\\([\\;,nN])")


def _files(path: Path, skip_dir: Callable[[str], bool] | None = None,
           errors: list[OSError] | None = None) -> list[tuple[str, int, float, partial]]:
    """(relative name, size, mtime, read) of every file in a .zip (and in zips inside it, one
    level: Notion nests its export parts), a folder, or one file. The one reader every
    importer and `detect` use. `read()` refuses a file over MAX_ENTRY (MAX_BYTES for a note)
    or past MAX_TOTAL for the whole call, before it decompresses it; `read(n)` reads only the
    first n bytes; `read(items=True)` streams a JSON list up to MAX_STREAM (json_value).
    In a folder, `skip_dir(relative folder path)` prunes a folder before it is walked
    (node_modules is never listed), and a subfolder that cannot be read goes to `errors`."""
    path, left = Path(path), [MAX_TOTAL]
    try:
        if path.suffix.lower() == ".zip":
            return _zip_files(zipfile.ZipFile(path), left)
        if path.is_dir():
            return [(p.relative_to(path).as_posix(), p.stat().st_size, p.stat().st_mtime,
                     partial(_read, p.relative_to(path).as_posix(), p.stat().st_size,
                             partial(p.open, "rb"), left))
                    for p in _walk(path, skip_dir, [] if errors is None else errors)
                    if p.is_file()]
        return [(path.name, path.stat().st_size, path.stat().st_mtime,
                 partial(_read, path.name, path.stat().st_size, partial(path.open, "rb"), left))]
    except (OSError, zipfile.BadZipFile) as e:
        raise VaultError("invalid_argument", f"Cannot read {path.name}: {e}") from None


def _walk(root: Path, skip_dir: Callable[[str], bool] | None, errors: list) -> list[Path]:
    """Every path under `root`, sorted, minus the folders `skip_dir` prunes; unreadable
    folders are appended to `errors` instead of stopping the walk."""
    out = []
    for d, dirs, names in os.walk(root, onerror=errors.append):
        rel = Path(d).relative_to(root)
        if skip_dir:
            dirs[:] = [x for x in dirs if not skip_dir((rel / x).as_posix())]
        out += [Path(d, n) for n in names]
    return sorted(out)


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
    from cortex import more_sources  # it imports this module
    from cortex.browse import takeout  # takeout imports this module
    if more_sources.holds_repos(path):  # before _files, which would walk every repo
        return ["repos"]
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
    if not kinds and any(more_sources.highlight_format(n, read) for n, _, _, read in files):
        kinds.append("highlights")
    if not kinds and any(_is_note(n) for n in names):
        kinds.append("notes")
    elif not kinds and any(n.endswith(more_sources.DOC_SUFFIXES) for n in names):
        kinds.append("docs")  # PDFs among notes (attachments) still mean notes
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


def _summary(front: str) -> str:
    """R3-6: a note's own one-line `summary:` or `description:` from its frontmatter, else ""."""
    from ruamel.yaml import YAML, YAMLError
    try:
        fm = YAML(typ="safe").load(front)
    except (YAMLError, ValueError):
        return ""
    if not isinstance(fm, dict):
        return ""
    return next((v for k in ("summary", "description") if isinstance(v := fm.get(k), str)), "")


def report_line(label: str, r: dict) -> str:
    """The line every import ends with: counts read, imported, private, duplicate and
    skipped, with the reason for each skip; then the items the quality gate kept out
    (sources.unreadable), when any: ". Skipped 3 as unreadable (2 garbled, 1 link list)"."""
    why = {k: n for k, n in r.get("reasons", {}).items() if n}
    reasons = f" ({', '.join(f'{n:,} {k}' for k, n in why.items())})" if why else ""
    updated = f", updated {r['updated']:,}" if r.get("updated") else ""
    gone = (f", {r['gone']:,} earlier page{'s' * (r['gone'] != 1)} not in the folder (kept)"
            if r.get("gone") else "")
    bad = {k: r["unreadable"][k] for k in sources.UNREADABLE if r.get("unreadable", {}).get(k)}
    gate = (f". Skipped {sum(bad.values()):,} as unreadable ("
            f"{', '.join(f'{n:,} {k}' for k, n in bad.items())})") if bad else ""
    return (f"{label}: read {r['read']:,}, imported {r['imported']:,}{updated}, private "
            f"{r['private']:,}, duplicate {r['duplicate']:,}{gone}, skipped "
            f"{sum(why.values()):,}{reasons}{gate}")


def _clean(name: str) -> str:
    """A path without Notion's 32-hex page ids."""
    return "/".join(_NOTION_ID.sub("", p) for p in name.replace("\\", "/").split("/"))


def notes_source(path: Path, flavor: str | None = None) -> sources.Source:
    """One item per .md/.txt note (≤ MAX_BYTES, UTF-8 text, outside hidden folders such as
    .obsidian); anything else is skipped with its reason. A note whose frontmatter marks it
    private (any truthy `private`, a `private` tag, or frontmatter that does not parse) is a
    private item. Dedupe hashes the whole text, frontmatter too; RR-18: a changed note rewrites
    its page (by its path, `From:`) when that page is as the import wrote it, else comes in as
    a new page (`sources.Source.by_origin`)."""
    errors: list[OSError] = []
    files = _files(path, errors=errors)
    names = [n.replace("\\", "/") for n, *_ in files]
    flavor = flavor or ("obsidian" if any(".obsidian" in n.split("/") for n in names) or
                        (Path(path).is_dir() and (Path(path) / ".obsidian").is_dir())
                        else "notion" if any(_NOTION_ID.search(p) for n in names
                                             for p in n.split("/")) else "notes")

    def items():
        for f in files:
            yield note_item(*f)

    return sources.Source("notes", LABELS[flavor], "folder", items, prefix=flavor,
                          skips=("not a note", "over 5 MB", "not UTF-8 text"), by_origin=True,
                          walk_errors=errors)


def note_item(name: str, size: int, mtime: float, read) -> sources.Item | str:
    """One `_files` entry as a note Item, or the reason it is skipped. Its key is the whole
    text, so the docs source's .md/.txt dedupe against an earlier notes import."""
    data = read() if _is_note(name) and size <= MAX_BYTES else b"\0"
    try:
        text = data.decode("utf-8-sig") if b"\0" not in data else None
    except UnicodeDecodeError:
        text = None
    if text is None:
        return ("not a note" if not _is_note(name) else "over 5 MB" if size > MAX_BYTES
                else "not UTF-8 text")
    rel = _clean(name)
    fm = _FRONT.match(text)
    return sources.Item(PurePosixPath(rel).stem,
                        datetime.fromtimestamp(mtime, UTC).date().isoformat(),
                        text[fm.end():] if fm else text, rel,
                        private=bool(fm and _is_private(fm.group(1))), key=text,
                        summary=_summary(fm.group(1)) if fm else "")


def _tier(private: bool, tier: str | None) -> sources.Audience:
    return sources.tier(tier or ("private" if private else "normal"))


def import_notes(vault: Vault, path: Path, flavor: str | None = None, dry_run: bool = False,
                 private: bool = False, tier: str | None = None) -> dict:
    """`notes_source` through `sources.run`: one resource page per new note, every one private
    with `private` (or `tier` "private"). A note whose text was imported before is skipped and a
    changed one updates its page (see notes_source), so re-importing is safe."""
    src = notes_source(path, flavor)
    r = sources.run(vault, src, _tier(private, tier), dry_run)
    return {"source": src.prefix, "new": r["imported"], "same": r["duplicate"],
            "skipped": sum(r["reasons"].values()), **{k: r[k] for k in REPORT},
            "updated": r["updated"], "gone": r.get("gone")}


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


def calendar_source(path: Path) -> sources.Source:
    """One month-page line per event: date, time, title, location and the RRULE as text (not
    expanded). Attendees, organizers and descriptions are never read."""
    texts = [read().decode("utf-8-sig", errors="replace") for n, _, _, read in _files(path)
             if n.lower().endswith(".ics")]
    if not texts:
        raise VaultError("invalid_argument", f"Found no .ics calendar in {Path(path).name}.")

    def items():
        for ev in (e for t in texts for e in _events(t)):
            try:
                day, time = _when(ev["DTSTART"])
            except (KeyError, ValueError):
                yield "no readable start date"
                continue
            rule, title = ev.get("RRULE"), _text(ev.get("SUMMARY")) or "(no title)"
            yield sources.Item(title, day, " · ".join(x for x in (
                f"{day} {time}", title, _text(ev.get("LOCATION")),
                f"repeats {rule[1].strip()}" if rule else "") if x))

    return sources.Source("calendar", "Calendar", "file", items, page="month", noun="events",
                          skips=("no readable start date",))


def import_calendar(vault: Vault, path: Path, dry_run: bool = False,
                    private: bool = False, tier: str | None = None) -> dict:
    """`calendar_source` through `sources.run`: one resource page per month (`calendar-YYYY-MM`,
    or a private `calendar-private-YYYY-MM` with `private`, so a private calendar never joins a
    public month page). Lines already on a page are skipped, so re-importing is safe."""
    r = sources.run(vault, calendar_source(path), _tier(private, tier), dry_run)
    return {"events": r["items"], "months": r["pages"], "new": r["imported"],
            **{k: r[k] for k in REPORT}}
