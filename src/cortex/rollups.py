"""Rollup pages: one compact page per ISO week, month and year that holds anything, built by
code only, so the local Chat model and recall answer "what did I do in March / last week / in
2025" from one page instead of hundreds of passages.

Each page counts, per source, what Cortex holds for its days and lists the top items: notes and
saved AI chats written or changed (`chat._pages_in`, linked as [[id]]), journal lines, study
events per concept with their verdicts (event counts, never levels), and the most visited sites
by days visited (`chat_facts._visits`). Month and year pages also list their weeks or months.
A week page (R3-6) fits WEEK_MAX characters in fixed sections (`render_week`): totals and
hubs; projects and decisions; themes and searches; people (person pages its pages link to) and
calendar (the calendar import's events dated in the week); learning and journal.

Privacy: a public page (`rollups/rollup-2026-11.md`) holds only what non-private surfaces show:
no private notes or journals, no hidden subjects (`planner.hidden`). Private or local-only
browsing never reaches any rollup page (the store's own verdict; Chat reads it live). A period
with private notes, journals or hidden subjects gets a second page, `rollup-2026-11-private.md`
with `private: true`, holding only that material: the index keeps it out, so only the local
model under Chat's private switch reads it.

Writes go through `cortex.vault`; a page is rewritten only when its text changes, and the text
is a pure function of the items. `run` skips the whole pass when a stamp of the inputs (event,
note and store files, the catalog and the config) and of the rollup pages is unchanged. No model
is ever loaded or called."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import date, timedelta

from cortex import chat, chat_facts, chat_study, planner, render
from cortex.catalog import CATALOG, Catalog
from cortex.events import EventLog
from cortex.vault import Vault, VaultError

KIND = "rollup"
STAMP = ".cortex/rollups.json"
FORMAT = 2  # part of the stamp (2: R3-6's weeks): a new page format rebuilds every page once
TOP = 5  # items listed per section
CUT = 60  # characters of a title or journal line
SOURCES = ("notes", "chats", "journal", "study", "browsing")
NOUN = {"notes": "note", "chats": "saved AI chat", "journal": "journal line",
        "study": "study event", "browsing": "visit"}
HEAD = {"notes": "Notes", "chats": "Saved AI chats", "journal": "Journal", "study": "Study",
        "browsing": "Browsing"}
_INPUT_KINDS = ("note", "resource", "decision", "project", "loop", "journal", "domain", "area",
                "concept", "person")  # the pages read, and the ones whose private flag hides
# subjects; a person page's name links the week's people
WEEK_MAX = 1600  # R3-6: a week page's characters; lists are cut, never padded
WEEK_ROWS = 4  # rows per list on a week page before the cut
_CALENDAR = re.compile(r"^Source: Calendar · Date: ", re.M)  # sources' month page header
_EVENT = re.compile(r"^- (\d{4}-\d\d-\d\d)\b.*$", re.M)
_LINK = re.compile(r"\[\[([^\]|#]+)")


# ---- the items ---------------------------------------------------------------------------------

def _items(vault: Vault, lo: str, hi: str) -> list[tuple]:
    """Every dated item in [lo, hi]: (day, source, key, label, n, extra, private). Notes and
    chats: key the page id; journal: the line; study: the concept id, extra the verdict;
    browsing: key the site, n its visits that day."""
    out = []
    with chat._INDEX_LOCK:  # person pages of the shared index (a private one is not in it)
        people = {i: n["title"] for i, n in chat._index(vault)._notes.items()
                  if n["kind"] == "person"}
    named = {i: re.compile(rf"(?<!\w){re.escape(t)}(?!\w)") for i, t in people.items()
             if len(t) >= 4}
    pages = chat._pages_in(vault, lo, hi, {"notes": True, "chats": True, "private": True})
    for d, kind, id_, title, body, is_chat, private in pages:
        if kind == "journal":
            js = [ln for ln in body.splitlines() if chat._JOURNAL_LINE.match(ln)]
            out += [(d, "journal", ln[2:].strip(), "", 1, "", private) for ln in js]
            body = "\n".join(js)
        else:
            cal = _CALENDAR.search(body[:2000])  # extra: the kind, "calendar" for a calendar page
            out.append((d, "chats" if is_chat else "notes", id_, title, 1,
                        "calendar" if cal else kind, private))
            if cal:  # R3-6: each event on its own day, for the week pages
                out += [(m[1], "calendar", m[0][2:].strip(), "", 1, "", private)
                        for m in _EVENT.finditer(body) if lo <= m[1] <= hi]
                continue
        # people: the person pages the period's own pages and journal lines link to or name
        met = {m[1].strip() for m in _LINK.finditer(body)} & people.keys()
        out += [(d, "people", p, people[p], 1, "", private)
                for p in sorted(met | {i for i, rx in named.items() if rx.search(body)})
                if p != id_]
    # visible browsing only: private or local-only sites never reach a page outside the store
    # (Chat reads them live under its private switch)
    _, visits = chat_facts._visits(vault, lo, hi, {"browsing": True}, [], lambda _h: True)
    out += [(d, "browsing", site, site, n, "", False) for d, _ts, _s, _t, n, _m, site in visits]
    catalog = Catalog.load(vault)
    hide = planner.hidden(vault, catalog)
    events = list(EventLog(vault).iter_events())
    voided = {e.get("ref") for e in events if e.get("kind") == "void"}
    for ev in events:
        obj, d = str(ev.get("object") or ""), str(ev.get("day") or "")
        if not obj.startswith("concept:") or not lo <= d <= hi or ev.get("id") in voided \
                or ev.get("kind") in chat_facts.BOOKKEEPING:
            continue
        c = obj[8:]
        e = catalog.entries.get(c)
        title = chat_study._title(e.title if e else c) or c
        private = c in hide or any(str(a).split(":", 1)[-1] in hide for a in ev.get("also") or [])
        out.append((d, "study", c, title, 1, str(ev.get("verdict") or ""), private))
    return out


# ---- the periods -------------------------------------------------------------------------------

def _periods(day: str) -> list[tuple[str, str, str, str]]:
    """(id, unit, first day, last day) of the week, month and year holding `day`."""
    d = date.fromisoformat(day)
    y, w, _ = d.isocalendar()
    mon = d - timedelta(days=d.weekday())
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return [(f"rollup-{y}-w{w:02d}", "week", mon.isoformat(),
             (mon + timedelta(days=6)).isoformat()),
            (f"rollup-{day[:7]}", "month", day[:8] + "01", (nxt - timedelta(days=1)).isoformat()),
            (f"rollup-{day[:4]}", "year", day[:4] + "-01-01", day[:4] + "-12-31")]


def _title(id_: str, unit: str, lo: str, hi: str) -> str:
    a, b = date.fromisoformat(lo), date.fromisoformat(hi)
    if unit == "year":
        return f"{a.year} in review"
    if unit == "month":
        return f"{a:%B %Y}"
    return f"Week {int(id_.rsplit('w', 1)[1])} of {id_.split('-')[1]} " \
           f"({a.day} {a:%b} to {b.day} {b:%b %Y})"


# ---- the page ---------------------------------------------------------------------------------

def _esc(text: str) -> str:
    return render.esc(text, CUT)


def _totals(its: list) -> dict:
    by: Counter = Counter()
    for it in its:
        by[it[1]] += it[4]
    return {s: by[s] for s in SOURCES if by[s]}


def _say(by: dict) -> str:
    return ", ".join(chat_study._plural(n, NOUN[s]) for s, n in by.items())


def _section(src: str, its: list) -> list[str]:
    """A source's lines: its top TOP items and how many more."""
    rows, more = _rows(src, its, TOP)
    n = sum(it[4] for it in its)
    head = f"## {HEAD[src]} ({n})" if src != "browsing" else \
        f"## {HEAD[src]} ({chat_study._plural(n, 'visit')}, " \
        f"{chat_study._plural(len({it[2] for it in its}), 'site')})"
    return [head, *rows, *([f"- and {more} more"] if more > 0 else []), ""]


def _rows(src: str, its: list, top: int) -> tuple[list[str], int]:
    """A source's top `top` rows and how many more it has."""
    if src in ("notes", "chats", "people"):
        seen = {}
        for it in sorted(its, key=lambda it: (it[0], it[2])):
            seen[it[2]] = it  # a page once, at its day
        rows = [f"- {it[0] + ' ' if src != 'people' else ''}[[{it[2]}]] {_esc(it[3])}"
                for it in list(seen.values())[-top:]]
        more = len(seen) - len(rows)
    elif src in ("journal", "calendar"):
        picked = sorted(its, key=lambda it: (it[0], it[2]))[-top:]
        rows = [f"- {it[0]} {_esc(chat_study._title(it[2]))}" if src == "journal"
                else f"- {_esc(it[2])}" for it in picked]
        more = len(its) - len(rows)
    elif src == "study":
        per: dict = defaultdict(Counter)
        names, days = {}, defaultdict(set)
        for it in its:
            per[it[2]][it[5] or "other"] += 1
            names[it[2]] = it[3]
            days[it[2]].add(it[0])
        order = sorted(per, key=lambda c: (-sum(per[c].values()), c))
        rows = []
        for c in order[:top]:
            v = per[c]
            res = ", ".join(f"{v[k]} {k}" for k in ("correct", "partial", "wrong") if v[k])
            n = chat_study._plural(sum(v.values()), 'event')
            rows.append(f"- [[{c}]] {_esc(names[c])}: {n}"
                        f" on {chat_study._plural(len(days[c]), 'day')}"
                        + (f" ({res})" if res else ""))
        more = len(order) - len(rows)
    else:  # browsing: sites by days visited, then visits
        visits, days = Counter(), defaultdict(set)
        for it in its:
            visits[it[2]] += it[4]
            days[it[2]].add(it[0])
        order = sorted(visits, key=lambda s: (-len(days[s]), -visits[s], s))
        rows = [f"- {_esc(s)}: {chat_study._plural(len(days[s]), 'day')}, "
                f"{chat_study._plural(visits[s], 'visit')}" for s in order[:top]]
        more = len(order) - len(rows)
    return rows, more


# R3-6: a week page's fixed sections, each (heading, its lists in order)
WEEK_SECTIONS = (("Totals and hubs", ("browsing",)),
                 ("Projects and decisions", ("decisions",)),
                 ("Themes and searches", ("notes", "chats")),
                 ("People and calendar", ("people", "calendar")),
                 ("Learning and journal", ("study", "journal")))


def render_week(id_: str, lo: str, hi: str, its: list, private: bool,
                up: str | None = None) -> str:
    """A week page in WEEK_SECTIONS, at most WEEK_MAX characters: each list starts at
    WEEK_ROWS rows and the longest loses its last row until the page fits ("- and N more").
    Totals and hubs: the counts, the active days and the most visited sites; projects and
    decisions: project and decision pages; themes and searches: the other notes and saved AI
    chats; people: person pages the week's pages link to; calendar: the events dated in it.
    Every row comes from the items: an empty list says "none", nothing is padded."""
    by = defaultdict(list)
    for it in its:
        src = "decisions" if it[1] == "notes" and it[5] in ("project", "decision") else it[1]
        if not (src == "notes" and it[5] == "calendar"):  # its events are listed instead
            by[src].append(it)
    lists = {src: list(_rows("notes" if src == "decisions" else src, by[src], WEEK_ROWS))
             for _, srcs in WEEK_SECTIONS for src in srcs}
    title = _title(id_, "week", lo, hi) + (": private items" if private else "")
    fm = [f"title: {json.dumps(title)}", f"kind: {KIND}", "period: week", f"date: {lo}",
          f"end: {hi}"] + (["private: true"] if private else [])
    days = sorted({it[0] for it in its})
    totals = _say(_totals(its))
    head = ["---", *fm, "---", f"# {title}", "", *([f"Up: [[{up}]]", ""] if up else []),
            "Built by Cortex from your data: counts and titles only, written by code, no model."
            + (" Private material only; the public page leaves it out." if private else ""), ""]

    def text() -> str:
        out = list(head)
        for name, srcs in WEEK_SECTIONS:
            out.append(f"## {name}")
            if name == "Totals and hubs":
                out += [f"- {totals or 'nothing counted'}",
                        f"- Active on {chat_study._plural(len(days), 'day')}, first {days[0]}, "
                        f"last {days[-1]}"]
            rows = [r for s in srcs for r in [*lists[s][0], *(
                [f"- and {lists[s][1]} more"] if lists[s][1] > 0 else [])]]
            out += rows or ([] if name == "Totals and hubs" else ["- none"])
            out.append("")
        return "\n".join(out).rstrip() + "\n"

    page = text()
    while len(page) > WEEK_MAX:
        src = max(lists, key=lambda s: len(lists[s][0]))
        if not lists[src][0]:
            break
        lists[src][0].pop(-1 if src in ("study", "browsing") else 0)  # the least or oldest
        lists[src][1] += 1
        page = text()
    return page


def render_page(id_: str, unit: str, lo: str, hi: str, its: list, private: bool,
                parts: list | None = None, up: str | None = None) -> str:
    """The page text for one period variant: a pure function of its arguments. `parts`:
    (id, items) of the weeks of a month or the months of a year, each listed with its totals."""
    title = _title(id_, unit, lo, hi) + (": private items" if private else "")
    fm = [f"title: {json.dumps(title)}", f"kind: {KIND}", f"period: {unit}", f"date: {lo}",
          f"end: {hi}"] + (["private: true"] if private else [])
    days = sorted({it[0] for it in its})
    lines = ["---", *fm, "---", f"# {title}", ""]
    if up:
        lines += [f"Up: [[{up}]]", ""]
    lines += ["Built by Cortex from your data: counts and titles only, written by code, no model."
              + (" Private material only; the public page leaves it out." if private else ""),
              "",
              f"- {_say(_totals(its))}",
              f"- Active on {chat_study._plural(len(days), 'day')}, first {days[0]}, "
              f"last {days[-1]}", ""]
    if parts:
        lines.append("## " + ("Weeks" if unit == "month" else "Months"))
        lines += [f"- [[{pid}]]: {_say(_totals(pits))}" for pid, pits in parts]
        lines.append("")
    by = defaultdict(list)
    for it in its:
        by[it[1]].append(it)
    for src in SOURCES:
        if by[src]:
            lines += _section(src, by[src])
    return "\n".join(lines).rstrip() + "\n"


def build(items: list) -> dict[str, str]:
    """{page id: text} of every period variant that holds items."""
    groups: dict = defaultdict(list)
    meta = {}
    for it in items:
        for pid, unit, lo, hi in _periods(it[0]):
            groups[pid, it[6]].append(it)
            meta[pid] = (unit, lo, hi)
    # a month or year page needs a counted source; calendar events and people alone make only
    # week pages (an upcoming exam's week)
    groups = {k: its for k, its in groups.items()
              if meta[k[0]][0] == "week" or any(it[1] in SOURCES for it in its)}
    out = {}
    for (pid, private), its in groups.items():
        unit, lo, hi = meta[pid]
        up = None
        if not private and unit != "year":
            up = _periods(lo)[1 if unit == "week" else 2][0]
            up = up if (up, False) in groups else None
        name = pid + ("-private" if private else "")
        if unit == "week":
            out[name] = render_week(pid, lo, hi, its, private, up)
            continue
        sub = 0 if unit == "month" else 1
        its = [it for it in its if it[1] in SOURCES]
        kids: dict = defaultdict(list)
        for it in its:
            kids[_periods(it[0])[sub][0]].append(it)
        out[name] = render_page(pid, unit, lo, hi, its, private, sorted(kids.items()), up)
    return out


# ---- writing ----------------------------------------------------------------------------------

def _listing(vault: Vault, inputs: bool) -> list:
    """The input files (`inputs`), else the rollup pages, as (name, mtime_ns, size) listings,
    as the home's version stamp reads them: cheap, no file is opened."""
    from cortex import home, taint
    from cortex.browse import store

    r = str(vault.root)
    if not inputs:
        return sorted(home._md(os.path.join(r, Vault.KIND_DIRS[KIND])))
    db = os.path.join(r, *store.PATH.split("/"))
    return [FORMAT, sorted(home._events(r)), home._stat(os.path.join(r, CATALOG)),
            home._stat(db), home._stat(db + "-wal"), home._stat(str(taint._config_path())),
            *(sorted(x for d in Vault.dirs(k) for x in home._md(os.path.join(r, *d.split("/"))))
              for k in _INPUT_KINDS)]


def _stamp(inputs: list, pages: list) -> str:
    return hashlib.sha256(repr([inputs, pages]).encode()).hexdigest()[:32]


def _read_stamp(vault: Vault) -> dict:
    try:
        return json.loads((vault.root / STAMP).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def run(vault: Vault, since: str | None = None) -> dict | None:
    """Write the rollup pages whose text changed and remove the ones whose period holds nothing
    now; `since` ("YYYY-MM") limits both to periods ending on or after that month's first day.
    {written, removed, kept} counts, or None when the stamp says nothing changed."""
    first = f"{since}-01" if since else chat_facts.FIRST_DAY
    if since:
        try:
            date.fromisoformat(first)
        except ValueError:
            raise VaultError("invalid_argument", "--since must be a month like 2026-03.") \
                from None
    old = _read_stamp(vault)
    inputs = _listing(vault, True)  # before reading: a change during the run is seen next time
    covered = old.get("since") is None or bool(since and old["since"] <= since)
    if covered and old.get("stamp") == _stamp(inputs, _listing(vault, False)):
        return None
    # a year page needs the whole year, and a week may start in the year before
    jan = date(int(first[:4]), 1, 1)
    lo = (jan - timedelta(days=jan.weekday())).isoformat() if since else first
    pages = build(_items(vault, lo, "9999-12-31"))

    folder = vault.root / Vault.KIND_DIRS[KIND]
    written = kept = removed = 0
    for name, text in sorted(pages.items()):
        if _end(text) < first:
            continue
        path = folder / f"{name}.md"
        try:
            if path.read_bytes().decode("utf-8") == text:
                kept += 1
                continue
        except OSError:
            pass
        try:
            vault.write_text(path, text)
        except VaultError as e:  # a title that looks like a key: that page waits for an edit
            if e.code != "secret_detected":
                raise
            continue
        written += 1
    for path in sorted(folder.glob("rollup-*.md")) if folder.is_dir() else []:
        if path.stem not in pages and _end_of(path) >= first:
            vault.remove(path)
            removed += 1
    stamp = _stamp(inputs, _listing(vault, False))
    vault.write_text(vault.root / STAMP, json.dumps({"stamp": stamp, "since": since}))
    return {"written": written, "kept": kept, "removed": removed}


def _end(text: str) -> str:
    return next(ln[5:].strip() for ln in text.splitlines() if ln.startswith("end: "))


def _end_of(path) -> str:
    """The last day of a rollup page's period, from its file; '9999' (always in range) when it
    cannot be read."""
    try:
        return _end(path.read_text(encoding="utf-8"))
    except (OSError, StopIteration, UnicodeDecodeError):
        return "9999"
