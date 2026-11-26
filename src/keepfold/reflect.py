"""Reflect pack (docs/design/phase4-wiki.md §7.4): what to review with the learner. Read-only."""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter

from keepfold import learning_eval, planner
from keepfold.events import EventLog, local_day
from keepfold.index import EDGE_KEYS, _edge_targets
from keepfold.lint import old_fleeting
from keepfold.loops import parse_day
from keepfold.render import plain
from keepfold.session import AUTO_JOURNAL
from keepfold.vault import Vault, is_private

UNCONFIRMED = "## About me (unconfirmed)"
EMERGING_KINDS = ("note", "resource", "decision", "project")
EMERGING_DAYS = 14
MIN_SHARED = 2
MIN_CLUSTER = 3
PORTRAIT_DAYS = 30
PORTRAIT_SAMPLE = 10
PORTRAIT_MIN_LINES = 5  # journal lines in the window not written by a session close
_CONFIRMED = re.compile(r"\(confirmed (\d{4}-\d{2}-\d{2})\)")
# `(proposed DAY)`, since plan 5a `(proposed DAY, me/<page>)`: the page the app's Approve writes
_PROPOSED = re.compile(r"\s*\(proposed (\d{4}-\d{2}-\d{2})(?:, me/([a-z-]+))?\)\s*$")
UNCONFIRMED_DAYS = 60


def portrait(vault: Vault, catalog, now: dt.datetime) -> dict:
    """me/portrait.md is due when it has no `## YYYY-MM` section in the last 30 days and the
    window holds PORTRAIT_MIN_LINES journal lines besides session-close ones; `pack` is
    the data to draft the next one from (SPEC §0 U7). Read-only."""
    today = dt.date.fromisoformat(local_day(now))
    start = today - dt.timedelta(days=PORTRAIT_DAYS)

    def recent(value) -> bool:
        d = value if isinstance(value, dt.date) else parse_day(value)
        return bool(d) and start < d <= today

    p = vault.root / "me" / "portrait.md"
    text = p.read_bytes().decode("utf-8") if p.is_file() else ""
    months = re.findall(r"^## (\d{4}-\d{2})\s*$", text, re.M)
    due = not any(m >= start.isoformat()[:7] for m in months)
    pers = vault.root / "me" / "personality.md"
    lines = [plain(ln.removeprefix("- "), 200)
             for ln in (pers.read_bytes().decode("utf-8").splitlines() if pers.is_file() else [])
             if (m := _CONFIRMED.search(ln)) and recent(m.group(1))]
    notes = [n for k in ("decision", "journal", "loop") for n in vault.iter_notes(k)
             if not is_private(n)]
    journal = [ln.strip() for n in notes if n.kind == "journal" and recent(n.fields.get("date"))
               for ln in n.body.splitlines() if re.match(r"- (\d\d:\d\d|—) ", ln)]  # — = back-dated
    own = sum(not AUTO_JOURNAL.fullmatch((ln.split(" ", 2) + [""])[2]) for ln in journal)
    due = due and own >= PORTRAIT_MIN_LINES  # session-close lines alone are no material
    step = max(1, -(-len(journal) // PORTRAIT_SAMPLE))
    events = list(EventLog(vault).iter_events())
    cut = start.isoformat()
    before, _ = planner.current_states(vault, cut, catalog,
                                       [e for e in events if str(e.get("day", "")) <= cut])
    after, _ = planner.current_states(vault, today.isoformat(), catalog, events)
    hide = planner.hidden(vault, catalog)  # the pack reaches MCP: no private subject in it
    moved = [{"id": c, "title": getattr(catalog.entries.get(c), "title", c), "to": after[c].state}
             for c in sorted(after) if after[c].state in ("solid", "learning") and c not in hide
             and getattr(before.get(c), "state", "not_started") != after[c].state]
    loops = [n for n in notes if n.kind == "loop"]
    return {"due": due, "pack": {
        "personality": lines,
        "decisions": [{"id": n.id, "title": plain(n.fields.get("title") or n.id, 80)}
                      for n in notes if n.kind == "decision" and recent(n.fields.get("date"))],
        "journal_lines": len(journal), "journal_sample": [plain(t, 200) for t in journal[::step]
                                                          [:PORTRAIT_SAMPLE]],
        "moved": moved,
        "loops_opened": [plain(n.fields.get("title") or n.id, 80) for n in loops
                         if recent(n.fields.get("date"))],
        "loops_closed": [plain(n.fields.get("title") or n.id, 80) for n in loops
                         if n.fields.get("status") in ("done", "dropped")
                         and recent(dt.date.fromtimestamp(n.path.stat().st_mtime))]}}


def unconfirmed(vault: Vault, today: str) -> tuple[list[dict], int]:
    """The lines under `## About me (unconfirmed)` in inbox.md as [{text, since}] (since: the
    `(proposed YYYY-MM-DD)` stamp, else None), deduped (latest stamp kept), and the count of
    stamped lines older than UNCONFIRMED_DAYS left out (COACH-4). The file is not changed."""
    p = vault.root / "inbox.md"
    seen: dict[str, str | None] = {}
    on = False
    for ln in (p.read_bytes().decode("utf-8").splitlines() if p.is_file() else []):
        if ln.startswith("#"):
            on = ln.strip() == UNCONFIRMED
        elif on and ln.strip():
            m = _PROPOSED.search(ln)
            text = plain(_PROPOSED.sub("", ln).strip().removeprefix("- "), 200)
            since = m.group(1) if m else None
            seen[text] = max(filter(None, (seen.get(text), since)), default=None)
    cut = (dt.date.fromisoformat(today) - dt.timedelta(days=UNCONFIRMED_DAYS)).isoformat()
    rows = [{"text": t, "since": d} for t, d in seen.items() if d is None or d >= cut]
    return rows, len(seen) - len(rows)


def emerging(vault: Vault, now: dt.datetime) -> list[dict]:
    """Recent notes that share >= 2 links or tags, grouped by connection (clusters of 3+)."""
    keys: dict[str, set[str]] = {}
    titles: dict[str, str] = {}
    for n in vault.iter_notes():
        titles[n.id] = str(n.fields.get("title") or n.id)
        if (n.kind not in EMERGING_KINDS or is_private(n) or now - dt.datetime.fromtimestamp(
                n.path.stat().st_mtime) > dt.timedelta(days=EMERGING_DAYS)):
            continue
        tags = n.fields.get("tags") or []
        tags = tags if isinstance(tags, list) else str(tags).replace(",", " ").split()
        keys[n.id] = ({t for k in EDGE_KEYS if k in n.fields for t in _edge_targets(n.fields[k])}
                      | {"#" + str(t).lstrip("#") for t in tags if str(t).strip("#")})
    root = {i: i for i in keys}

    def find(i: str) -> str:
        while root[i] != i:
            root[i] = i = root[root[i]]
        return i

    ids = sorted(keys)
    for a, x in enumerate(ids):
        for y in ids[a + 1:]:
            if len(keys[x] & keys[y]) >= MIN_SHARED:
                root[find(x)] = find(y)
    groups: dict[str, list[str]] = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)
    out = []
    for members in groups.values():
        if len(members) < MIN_CLUSTER:
            continue
        count = Counter(k for m in members for k in keys[m])
        shared = [k for k, c in count.most_common() if c >= 2]
        top = shared[0] if shared else members[0]
        out.append({"members": members, "shared": [plain(titles.get(k, k), 60) for k in shared],
                    "suggested_title": plain(titles.get(top, top.lstrip("#")), 60)})
    return out


def reflect(vault: Vault, catalog, now: dt.datetime) -> dict:
    """`now` is aware (the server clock); file times and inbox dates are local."""
    from keepfold import claims  # it imports this module

    local = now.astimezone().replace(tzinfo=None)
    rows, dropped = unconfirmed(vault, local_day(now))
    events = list(EventLog(vault).iter_events())
    saved = claims.saved(vault, events, local_day(now))  # v6 T4 + U3
    return {"unconfirmed": rows, "unconfirmed_dropped": dropped,
            "saved": {"claims": [{k: plain(r[k], 200) if k in ("text", "section") else r[k]
                                  for k in ("id", "page", "section", "text", "tier", "day",
                                            "undo")} for r in saved["claims"]],
                      "changes": [{**c, "target": plain(c["target"], 80)}
                                  for c in saved["changes"]]},
            "emerging": emerging(vault, local),
            "old_fleeting": [{"text": plain(t, 120), "since": d}
                             for t, d in old_fleeting(vault, local)],
            "week": planner.week_summary(vault, catalog, now=now),
            "portrait": portrait(vault, catalog, now),
            "spot_check": [{**r, "question": plain(r["question"], 300),
                            "answer": plain(r["answer"], 300)}
                           for r in learning_eval.spot_checks(
                               events, hide=planner.hidden(vault, catalog))]}
