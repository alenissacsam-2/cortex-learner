"""Open loops, decision follow-ups and idea resurfacing (SPEC §0 U7). Passive, read-only."""

from __future__ import annotations

import datetime as dt
import re

from cortex.events import EventLog, live
from cortex.vault import HEADING_RE, Vault, is_private

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


def resurface(vault: Vault, today: str, limit: int = 3, events=None) -> list[dict]:
    """Ideas due to be shown again, most overdue first. Permanent notes, decisions, and
    resources with a summary; each shown n times waits INTERVALS[n] days after the last time,
    the first time counted from the day the page was added (a harvested page keeps its old
    `date`), else `created`."""
    events = list(EventLog(vault).iter_events() if events is None else events)
    seen, added = shown_days(events), added_days(events)
    day, out = dt.date.fromisoformat(today), []
    for kind in ("note", "decision", "resource"):
        for n in vault.iter_notes(kind):
            f = n.fields
            if is_private(n) or (kind == "note" and f.get("note_type") != "permanent") or (
                    kind == "resource" and not f.get("summary")):
                continue
            days = sorted(d for d in map(parse_day, seen.get(n.id, [])) if d)
            due = (days[-1] if days else added.get(n.id) or created(n)) + dt.timedelta(
                days=INTERVALS[min(len(seen.get(n.id, [])), len(INTERVALS) - 1)])
            if due <= day:
                out.append({"id": n.id, "kind": kind, "title": str(f.get("title") or n.id),
                            "summary": str(f.get("summary") or ""),
                            "overdue_days": (day - due).days, "times_shown": len(days)})
    out.sort(key=lambda r: (-r["overdue_days"], r["id"]))
    return out[:max(1, int(limit))]
