"""Open loops, decision follow-ups and idea resurfacing (SPEC §0 U7). Passive, read-only,
except `from_notes`: an imported note's TODO: and Action: lines become open loops (G5b-10)."""

from __future__ import annotations

import datetime as dt
import re

from keepfold.events import EventLog, live
from keepfold.vault import HEADING_RE, Vault, is_private

REVIEW_DAYS = 21
INTERVALS = (3, 7, 21, 60, 180)  # days after creation or the last time shown
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_day(value) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10]) if _DATE.match(str(value)[:10]) else None
    except ValueError:
        return None


def created(note) -> dt.date:
    # shortcut: notes carry no created date; the file time stands in, so an edit moves it
    return parse_day(note.fields.get("date")) or dt.date.fromtimestamp(note.path.stat().st_mtime)


def section_text(body: str, heading: str) -> str:
    """Text under `## heading` up to the next heading, stripped of blank lines and comments."""
    out, on = [], False
    for ln in body.splitlines():
        m = HEADING_RE.match(ln)
        if m:
            on = m.group(2) == heading
        elif on:
            out.append(ln)
    return re.sub(r"<!--.*?-->", "", "\n".join(out), flags=re.S).strip()


def loop_row(n) -> dict:
    f = n.fields
    return {"id": n.id, "kind": "loop", "title": str(f.get("title") or n.id),
            "status": str(f.get("status") or "open"), "loop_type": f.get("loop_type"),
            "due": str(f["due"]) if f.get("due") else None,
            **({"summary": str(f["summary"])} if f.get("summary") else {})}


def open_first(rows: list[dict]) -> list[dict]:
    """Open loops first, soonest due first (undated last), then done, then dropped."""
    rank = {"open": 0, "done": 1}
    return sorted(rows, key=lambda r: (rank.get(r["status"], 2), r["status"] == "open" and
                                       r["due"] is None, r["due"] or "", r["id"]))


def loops(vault: Vault, status: str | None = "open") -> list[dict]:
    rows = [loop_row(n) for n in vault.iter_notes("loop") if not is_private(n)]
    return open_first([r for r in rows if status in (None, r["status"])])


def follow_ups(vault: Vault, today: str) -> list[dict]:
    """Accepted decisions whose review date has come and whose `## Outcome` is still empty."""
    day, out = dt.date.fromisoformat(today), []
    for n in vault.iter_notes("decision"):
        if n.fields.get("status") != "accepted" or is_private(n):
            continue
        made = parse_day(n.fields.get("date"))
        review = parse_day(n.fields.get("review_on")) or (
            made + dt.timedelta(days=REVIEW_DAYS) if made else None)
        if review and review <= day and not section_text(n.body, "Outcome"):
            out.append({"id": n.id, "title": str(n.fields.get("title") or n.id),
                        "review_on": review.isoformat()})
    return sorted(out, key=lambda r: (r["review_on"], r["id"]))


def shown_days(events) -> dict[str, list[str]]:
    """page id -> days it was shown (non-voided `resurfaced` log events)."""
    out: dict[str, list[str]] = {}
    for e in live(events):
        p = e.get("payload")
        if e.get("kind") == "log" and isinstance(p, dict) and p.get("kind") == "resurfaced":
            for i in p.get("ids") or []:
                out.setdefault(str(i), []).append(str(e.get("day")))
    return out


def added_days(events) -> dict[str, dt.date]:
    """page id -> day of the first `change` event that added it: when it entered the vault."""
    out: dict[str, dt.date] = {}
    for e in events:
        p = e.get("payload") if e.get("kind") == "change" else None
        c = p.get("change") if isinstance(p, dict) and p.get("note") == "added" else None
        if isinstance(c, dict) and (d := parse_day(e.get("day"))):
            out[str(c.get("id"))] = min(d, out.get(str(c.get("id")), d))
    return out


def resurface(vault: Vault, today: str, limit: int = 3, events=None,
              created_of=None) -> list[dict]:
    """Ideas due to be shown again, most overdue first. Permanent notes, decisions, and
    resources with a summary; each shown n times waits INTERVALS[n] days after the last time,
    the first time counted from the day the page was added (a harvested page keeps its old
    `date`), else `created` (or `created_of`, a caller's memo of it)."""
    events = list(EventLog(vault).iter_events() if events is None else events)
    seen, added = shown_days(events), added_days(events)
    day, out, created_of = dt.date.fromisoformat(today), [], created_of or created
    for kind in ("note", "decision", "resource"):
        for n in vault.iter_notes(kind):
            f = n.fields
            if is_private(n) or (kind == "note" and f.get("note_type") != "permanent") or (
                    kind == "resource" and not f.get("summary")):
                continue
            days = sorted(d for d in map(parse_day, seen.get(n.id, [])) if d)
            due = (days[-1] if days else added.get(n.id) or created_of(n)) + dt.timedelta(
                days=INTERVALS[min(len(seen.get(n.id, [])), len(INTERVALS) - 1)])
            if due <= day:
                out.append({"id": n.id, "kind": kind, "title": str(f.get("title") or n.id),
                            "summary": str(f.get("summary") or ""),
                            "overdue_days": (day - due).days, "times_shown": len(days)})
    out.sort(key=lambda r: (-r["overdue_days"], r["id"]))
    return out[:max(1, int(limit))]


# ---- G5b-10: TODO: and Action: lines in imported notes become open loops ---------------------

LOOP_DAYS = 30  # only a note changed this recently makes loops: an old folder's first import
MAX_LOOPS = 25  # new loops per import run or watch pass, so one folder never floods the home map
TITLE_CHARS = 120
# after indentation, a list marker and a checkbox: the label, and its colon
_TODO = re.compile(r"[ \t]*(?:(?:[-*+]|\d{1,9}\.)[ \t]+)?(?:\[([ xX])\][ \t]*)?"
                   r"(?:todo|to[ \t-]do|action[ \t]item|action)[ \t]*:(.*)", re.I)
_ITEM = re.compile(r"[ \t]*(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)")
_FENCE = re.compile(r"[ \t]{0,3}(`{3,}|~{3,})")
_DUE_WORD = r"\b(?:by|before|due|until|deadline)\b"
# a deadline weekday ("by Friday"): dates.pin leaves a bare weekday alone, as it has no tense
_DUE_DAY = re.compile(rf"{_DUE_WORD}\s+(?:on\s+)?(monday|tuesday|wednesday|thursday|friday|"
                      r"saturday|sunday)\b", re.I)
# a due word, then an ISO day, or a phrase of up to 4 words that pinning dated
_DUE = re.compile(rf"{_DUE_WORD}:?\s+(?:(\d{{4}}-\d\d-\d\d)\b|(?:[\w'’]+\s+){{1,4}}"
                  r"\((\d{4}-\d\d-\d\d)(?: to (\d{4}-\d\d-\d\d))?\))", re.I)


def _plain(line: str) -> str:
    """A note line as a loop holds it: [[links]] as their text (embeds dropped), secrets
    redacted, as the import writes the note's page."""
    from keepfold import sources
    return sources.scrub(sources._WIKI.sub(lambda m: "" if m[1] else m[3] or m[2], line)).strip()


def _pin_due_days(text: str, day: dt.date) -> str:
    """"by Friday" -> "by Friday (2026-11-27)": the first such day on or after `day`."""
    from keepfold import dates

    def one(m):
        if dates._DATED.match(text, m.end()):  # "by Friday, 27 Nov" or pinned already
            return m[0]
        ahead = (dates._WEEKDAYS.index(m[1].lower()) - day.weekday()) % 7
        return f"{m[0]} ({(day + dt.timedelta(days=ahead)).isoformat()})"
    return _DUE_DAY.sub(one, text)


def _cut(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= TITLE_CHARS:
        return text
    return text[:TITLE_CHARS - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def todos(text: str, day: dt.date) -> list[dict]:
    """The to-do lines of a note's text (frontmatter already off): {line, title, due, done}.
    A line is one when, after indentation, a list marker (-, *, +, 1.) and a checkbox ([ ],
    [x]), it starts with TODO:, To do:, To-do:, Action: or Action item: (any case; the colon is
    required); never in a fenced or indented code block. The title is the rest, its relative
    dates pinned against `day` (dates.pin, and a deadline weekday), whitespace collapsed, cut
    at a word to TITLE_CHARS with "…"; `due` is the day after by/before/due/until/deadline (an
    ISO day or a pinned one); `done` when the box is ticked. An empty rest is no to-do."""
    from keepfold import dates
    out, fence, code, blank, in_list = [], "", False, True, False
    for raw in text.splitlines():
        if fence:
            s = raw.strip()
            if s.startswith(fence) and not s.strip(fence[0]):
                fence = ""
            continue
        if m := _FENCE.match(raw):
            fence, blank = m.group(1), False
            in_list = in_list and raw[:1] in " \t"  # a fence at the margin ends a list
            continue
        if not raw.strip():
            blank = True
            continue
        wide = raw.expandtabs(4)
        indent = len(wide) - len(wide.lstrip())
        # an indented code block starts after a blank line, never inside a list
        code = indent >= 4 and (blank or code) and not in_list
        was_blank, blank = blank, False
        if code:
            continue
        if _ITEM.match(raw):
            in_list = True
        elif indent == 0 and was_blank:
            in_list = False
        m = _TODO.match(raw)
        if not m:
            continue
        line, rest = _plain(raw), _plain(m[2])
        if not rest:
            continue
        pinned = dates.pin(_pin_due_days(rest, day), day)
        d = _DUE.search(pinned)
        due = parse_day(d[1] or d[3] or d[2]) if d else None
        out.append({"line": line, "title": _cut(pinned), "due": due and due.isoformat(),
                    "done": m[1] in ("x", "X")})
    return out


def _fold(title: str) -> str:
    """A loop title for matching (claims.norm's folding), its pinned days out, so a note
    re-imported on another day still matches."""
    from keepfold import claims, dates
    # shortcut: a title cut at TITLE_CHARS after a pinned day may fold differently on another
    # day; match on the line's text before pinning if long dated to-dos double up
    return claims.norm(dates.unpin(title)).rstrip("…").rstrip()


def _origin(vault: Vault, pid: str, labels: set[str]) -> str:
    """The `From:` origin of the notes import page `pid`, else ""."""
    from keepfold import sources
    from keepfold.vault import VaultError
    try:
        text = vault.path_for("resource", pid).read_text("utf-8", errors="replace")
    except (OSError, VaultError):
        return ""
    m = sources.HEADER.search(text)
    return m.group(3) if m and m.group(3) and m.group(1) in labels else ""


def _known(vault: Vault) -> dict:
    """("p", page id, folded title) and ("o", that page's origin, folded title) -> {id,
    status} of every loop, by its `related` pages: a to-do matches a loop from the same page,
    or (RR-18's second page of a hand-edited note) from a page of the same note file."""
    from keepfold import importers
    from keepfold.index import _edge_targets
    labels, origins, out = set(importers.LABELS.values()), {}, {}
    for n in vault.iter_notes("loop"):
        rec = {"id": n.id, "status": str(n.fields.get("status") or "open")}
        f = _fold(str(n.fields.get("title") or ""))
        for pid in _edge_targets(n.fields.get("related")):
            if pid not in origins:
                origins[pid] = _origin(vault, pid, labels)
            out.setdefault(("p", pid, f), rec)
            if origins[pid]:
                out.setdefault(("o", origins[pid], f), rec)
    return out


def from_notes():
    """The notes source's hook (sources.Source.made) for one import run or watch pass: each
    note page written or rewritten gives its to-do lines (`todos`) to open loops. A to-do that
    matches a loop (`_known`) is never added again; ticked, it closes that loop if still open
    (a done or dropped loop is never reopened; a removed line changes nothing). Otherwise, in a
    note whose day is within LOOP_DAYS of today and not ticked, it becomes a `loop` page
    (commitment, open, related to the note's page, dated the note's day, `due` when it has one,
    private when the page is), at most MAX_LOOPS per run; one claims.unsafe flags makes none.
    Counts go to the run's report: loops, loops_over (past the cap), loops_unsafe."""
    known, taken = None, set()

    def made(vault: Vault, it, pid: str, private: bool, r: dict) -> None:
        nonlocal known
        day = parse_day(it.date)
        found = todos(it.text, day) if day else []
        if not found:
            return
        from keepfold import claims, sources
        from keepfold.catalog import slugify
        from keepfold.events import local_day
        from keepfold.vault import VaultError
        known = _known(vault) if known is None else known
        recent = (dt.date.fromisoformat(local_day(dt.datetime.now(dt.UTC))) - day).days \
            <= LOOP_DAYS
        origin = sources.scrub(it.origin) if it.origin else ""
        for t in found:
            f = _fold(t["title"])
            hit = known.get(("p", pid, f)) or (origin and known.get(("o", origin, f)))
            if hit:
                if t["done"] and hit["status"] == "open":
                    try:
                        vault.set_field("loop", hit["id"], "status", "done")
                        hit["status"] = "done"
                    except VaultError:  # the loop's page broke or went meanwhile
                        pass
                continue
            if t["done"] or not recent:
                continue
            why = "loops_unsafe" if claims.unsafe(t["line"]) else \
                "loops_over" if r.get("loops", 0) >= MAX_LOOPS else None
            if why:
                r[why] = r.get(why, 0) + 1
                continue
            fields = {"title": t["title"], "kind": "loop", "loop_type": "commitment",
                      "status": "open", "related": [f"[[{pid}]]"], "date": it.date,
                      **({"due": t["due"]} if t["due"] else {}),
                      **({"private": True} if private else {})}
            lid = sources._free_id(vault, (slugify(t["title"]) or "todo")[:81].rstrip("-."),
                                   taken)
            try:  # the loop is private whenever its page is, so the link never leaks it
                vault.create_note("loop", lid, fields, f"From [[{pid}]]: {t['line']}\n")
            except VaultError:  # a secret the write scan refuses, or the id taken meanwhile
                r["loops_unsafe"] = r.get("loops_unsafe", 0) + 1
                continue
            known[("p", pid, f)] = rec = {"id": lid, "status": "open"}
            if origin:
                known[("o", origin, f)] = rec
            r["loops"] = r.get("loops", 0) + 1

    return made
