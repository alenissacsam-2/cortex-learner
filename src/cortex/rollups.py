"""Rollup pages: one compact page per ISO week, month and year that holds anything, built by
code only, so the local Chat model and recall answer "what did I do in March / last week / in
2025" from one page instead of hundreds of passages.

Each page counts, per source, what Cortex holds for its days and lists the top items: notes and
saved AI chats written or changed (`chat._pages_in`, linked as [[id]]), journal lines, study
events per concept with their verdicts (event counts, never levels), and the most visited sites
by days visited (`chat_facts._visits`). Month and year pages also list their weeks or months.

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
from collections import Counter, defaultdict
from datetime import date, timedelta

from cortex import chat, chat_facts, chat_study, planner, render
from cortex.catalog import CATALOG, Catalog
from cortex.events import EventLog
from cortex.vault import Vault, VaultError

KIND = "rollup"
STAMP = ".cortex/rollups.json"
FORMAT = 1  # part of the stamp: a new page format rebuilds every page once
TOP = 5  # items listed per section
CUT = 60  # characters of a title or journal line
SOURCES = ("notes", "chats", "journal", "study", "browsing")
NOUN = {"notes": "note", "chats": "saved AI chat", "journal": "journal line",
        "study": "study event", "browsing": "visit"}
HEAD = {"notes": "Notes", "chats": "Saved AI chats", "journal": "Journal", "study": "Study",
        "browsing": "Browsing"}
_INPUT_KINDS = ("note", "resource", "decision", "project", "loop", "journal", "domain", "area",
                "concept")  # the pages read, and the ones whose private flag hides subjects


# ---- the items ---------------------------------------------------------------------------------

def _items(vault: Vault, lo: str, hi: str) -> list[tuple]:
    """Every dated item in [lo, hi]: (day, source, key, label, n, extra, private). Notes and
    chats: key the page id; journal: the line; study: the concept id, extra the verdict;
    browsing: key the site, n its visits that day."""
    out = []
    pages = chat._pages_in(vault, lo, hi, {"notes": True, "chats": True, "private": True})
    for d, kind, id_, title, body, is_chat, private in pages:
        if kind == "journal":
            out += [(d, "journal", ln[2:].strip(), "", 1, "", private) for ln in body.splitlines()
                    if chat._JOURNAL_LINE.match(ln)]
        else:
            out.append((d, "chats" if is_chat else "notes", id_, title, 1, "", private))
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
                or ev.get("kind") in ("void", "remap", "open", "resume", "close", "change"):
            continue
        c = obj[8:]
        e = catalog.entries.get(c)
        title = chat_study._title(e.title if e else c) or c
        private = c in hide or any(a in hide for a in ev.get("also") or [])
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
    if src in ("notes", "chats"):
        seen = {}
        for it in sorted(its, key=lambda it: (it[0], it[2])):
            seen[it[2]] = it  # a page once, at its day
        rows = [f"- {it[0]} [[{it[2]}]] {_esc(it[3])}" for it in list(seen.values())[-TOP:]]
        more = len(seen) - len(rows)
    elif src == "journal":
        picked = sorted(its, key=lambda it: (it[0], it[2]))[-TOP:]
        rows = [f"- {it[0]} {_esc(chat_study._title(it[2]))}" for it in picked]
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
        for c in order[:TOP]:
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
                f"{chat_study._plural(visits[s], 'visit')}" for s in order[:TOP]]
        more = len(order) - len(rows)
    n = sum(it[4] for it in its)
    head = f"## {HEAD[src]} ({n})" if src != "browsing" else \
        f"## {HEAD[src]} ({chat_study._plural(n, 'visit')}, " \
        f"{chat_study._plural(len({it[2] for it in its}), 'site')})"
    return [head, *rows, *([f"- and {more} more"] if more > 0 else []), ""]


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
    out = {}
    for (pid, private), its in groups.items():
        unit, lo, hi = meta[pid]
        parts = None
        if unit != "week":
            sub = 0 if unit == "month" else 1
            kids: dict = defaultdict(list)
            for it in its:
                kids[_periods(it[0])[sub][0]].append(it)
            parts = sorted(kids.items())
        up = None
        if not private and unit != "year":
            up = _periods(lo)[1 if unit == "week" else 2][0]
        name = pid + ("-private" if private else "")
        out[name] = render_page(pid, unit, lo, hi, its, private, parts, up)
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
