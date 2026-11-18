"""Circling map (v5 PHASE2.md G5): what Cortex has no evidence of. Read-only, no LLM.

Only the reflect pack shows it; the wording is always "Cortex has no evidence of…".
"""

from __future__ import annotations

import datetime as dt

from cortex import levels, loops, planner
from cortex.events import live
from cortex.render import plain
from cortex.vault import Vault, is_private

MAX_ITEMS = 3
EXAM_DAYS, LOOP_DAYS, FOLLOW_UP_DAYS = 60, 21, 14
MIN_TAUGHT = 2
VERDICTS = ("loop", "dropped", "outside")


def _verdicts(events) -> list[dict]:
    return [e["payload"] for e in live(events) if e.get("kind") == "log"
            and isinstance(e.get("payload"), dict) and e["payload"].get("kind") == "circling"]


def items(vault: Vault, catalog, events, today: str) -> list[dict]:
    """At most 3 {key, kind, text, ids}: unvisited exam topics first, then the oldest of
    taught-but-unchecked concepts, stale loops and skipped decision follow-ups. Keys marked
    dropped or outside, private notes and hidden concepts are left out."""
    day = dt.date.fromisoformat(today)
    evs = list(events)
    gone = {p.get("key") for p in _verdicts(evs) if p.get("verdict") in ("dropped", "outside")}
    hide = planner.hidden(vault, catalog)
    its = list(levels._items(evs, levels.problem_concepts(vault)))
    seen = {c for it in its for c in (it.concept, *it.also) if c}

    def title(cid: str) -> str:
        e = catalog.entries.get(cid)
        return plain(e.title if e else cid, 60)

    first, rest = [], []
    private = {n.id for n in vault.iter_notes("assessment") if is_private(n)}
    for a in planner._upcoming(vault, day):
        if a["days"] > EXAM_DAYS or a["id"] in private:
            continue
        tested = [c for c in planner._weights(catalog, a["tests"]) if c not in hide]
        scope = list(dict.fromkeys([*tested, *(
            r for c in tested for r in catalog.entries[c].requires
            if getattr(catalog.entries.get(r), "kind", None) == "concept" and r not in hide)]))
        missing = [c for c in scope if c not in seen]
        if missing:
            name = plain(f"{a.get('company') or ''} {a.get('kind') or ''}".strip() or a["id"], 60)
            more = " …" if len(missing) > 3 else ""
            first.append({"key": f"unvisited:{a['id']}", "kind": "unvisited", "ids": missing,
                          "text": f"Cortex has no evidence on {len(missing)} of {len(scope)} "
                                  f"topics for {name} ({', '.join(map(title, missing[:3]))}"
                                  f"{more})"})

    taught: dict[str, list] = {}  # concept -> [count, last taught index, its day]
    checked: dict[str, int] = {}
    for n, it in enumerate(its):
        c = it.concept
        if c is None:
            continue
        if it.taught:
            t = taught.setdefault(c, [0, 0, ""])
            t[0], t[1], t[2] = t[0] + 1, n, str(it.ev.get("day") or "")
        elif it.verdict is not None or it.ev.get("kind") == "ask":
            checked[c] = n
    for c, (count, last, when) in taught.items():
        if count >= MIN_TAUGHT and checked.get(c, -1) < last and c not in hide:
            rest.append((when, {"key": f"taught:{c}", "kind": "taught_unchecked", "ids": [c],
                                "text": f"Cortex has no evidence of a check on {title(c)} "
                                        f"since it was taught ({count} times)"}))

    for n in vault.iter_notes("loop"):
        made = loops.created(n)
        if (is_private(n) or str(n.fields.get("status") or "open") != "open"
                or n.fields.get("loop_type") == "idea" or (day - made).days <= LOOP_DAYS):
            continue
        rest.append((made.isoformat(), {
            "key": f"loop:{n.id}", "kind": "stale_loop", "ids": [n.id],
            "text": f"Cortex has no evidence of progress on the loop "
                    f"\"{plain(n.fields.get('title') or n.id, 80)}\" (open since {made})"}))

    for r in loops.follow_ups(vault, today):  # accepted, review date come, Outcome empty
        if (day - dt.date.fromisoformat(r["review_on"])).days > FOLLOW_UP_DAYS:
            rest.append((r["review_on"], {
                "key": f"decision:{r['id']}", "kind": "skipped_follow_up", "ids": [r["id"]],
                "text": f"Cortex has no evidence of a follow-up on the decision "
                        f"\"{plain(r['title'], 80)}\" (review was due {r['review_on']})"}))

    ordered = first + [i for _, i in sorted(rest, key=lambda t: (t[0], t[1]["key"]))]
    return [i for i in ordered if i["key"] not in gone][:MAX_ITEMS]


def stats(events) -> dict:
    """{loop, dropped, outside} counts of circling verdicts; the acted-on share is loop / total
    (signal threshold 30%)."""
    out = dict.fromkeys(VERDICTS, 0)
    for p in _verdicts(events):
        if p.get("verdict") in out:
            out[p["verdict"]] += 1
    return out
