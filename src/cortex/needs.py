"""Needs and tell-me taps (v5 PHASE2.md G2): personal facts that real work lacked, logged as
`need` events and asked back one at a time as a sealed tap (forecast with `need`, then settle).
Pure folds over events; voided events do not count."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable

from cortex import claims, probes

CAP = 3  # needs per session
SNOOZE_DAYS = 7
HALF_LIFE = 14  # days: a need's score halves every 14 days since it was last logged
JACCARD = 0.6
Q_MAX, OPTION_MAX, TASK_MAX = 200, 60, 120


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", claims.norm(text)))


def fold(events: Iterable[dict], today: str) -> tuple[dict[str, dict], dict[str, str]]:
    """(root needs by id, any need event id -> its root id). A root is {id, question, area,
    options, task, count, last, status: open|snoozed|skipped|answered}; duplicates add to
    `count` and `last` (a duplicate of a voided root becomes a root itself)."""
    evs = list(events)
    live = probes._live(evs)
    rows: dict[str, dict] = {}
    alias: dict[str, str] = {}
    for e in live:
        p = e["payload"]
        if p.get("kind") != "need":
            continue
        r = rows.get(p.get("dup_of"))
        if r is None:
            r = rows[e["id"]] = {"id": e["id"], "question": p.get("question", ""),
                                 "area": p.get("area", "other"), "options": p.get("options", []),
                                 "task": p.get("task"), "count": 0, "last": "",
                                 "status": "open"}
        r["count"] += 1
        r["last"] = max(r["last"], str(e.get("day", "")))
        alias[e["id"]] = r["id"]
    forecasts, settles = probes.state(evs)
    for fid in settles:
        if (root := alias.get((forecasts.get(fid) or {}).get("payload", {}).get("need"))):
            rows[root]["status"] = "answered"
    snooze_cut = (dt.date.fromisoformat(today) - dt.timedelta(days=SNOOZE_DAYS)).isoformat()
    for e in live:
        p = e["payload"]
        r = rows.get(alias.get(p.get("need"))) if p.get("kind") == "need_skip" else None
        if r is None or r["status"] in ("answered", "skipped"):
            continue
        if p.get("forever") is True:
            r["status"] = "skipped"
        elif str(e.get("day", "")) > snooze_cut:
            r["status"] = "snoozed"
    return rows, alias


def open(events: Iterable[dict], today: str) -> list[dict]:  # the spec's name; no file I/O here
    """Open root needs, best first: score = count x 0.5^(days since last / 14)."""
    t = dt.date.fromisoformat(today)
    out = []
    for r in fold(events, today)[0].values():
        if r["status"] == "open":
            days = max(0, (t - dt.date.fromisoformat(r["last"])).days) if r["last"] else 0
            out.append({**r, "score": round(r["count"] * 0.5 ** (days / HALF_LIFE), 3)})
    return sorted(out, key=lambda r: (-r["score"], r["id"]))


def match(events: Iterable[dict], question: str, today: str) -> str | None:
    """The root id of an open or snoozed need asking the same thing (same normalised text, or
    token Jaccard >= 0.6), else None."""
    q, toks = claims.norm(question), _tokens(question)
    for r in fold(events, today)[0].values():
        if r["status"] not in ("open", "snoozed"):
            continue
        other = _tokens(r["question"])
        if claims.norm(r["question"]) == q or (
                toks and len(toks & other) / len(toks | other) >= JACCARD):
            return r["id"]
    return None


def counts(events: Iterable[dict], today: str) -> dict:
    """The reflect pack's `needs`: root needs open, answered and skipped (or snoozed)."""
    out = {"open": 0, "answered": 0, "skipped": 0}
    for r in fold(events, today)[0].values():
        out["skipped" if r["status"] == "snoozed" else r["status"]] += 1
    return out
