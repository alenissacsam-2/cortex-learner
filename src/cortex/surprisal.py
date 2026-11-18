"""Surprisal (v5 PHASE3.md H4): how unlike a typical university student a claim is, from a
generic model's guess `p0` (`baseline` log events; the latest live one per claim wins; the
claim is resolved by `bets.resolve`). Pure folds over events, plus the reflect pack."""

from __future__ import annotations

import math

from cortex import claims, probes
from cortex.render import plain

GENERIC = 0.7  # p0 above this: true of almost anyone
UNPRICED_MAX = 5
PRICED_TIERS = ("typed", "confirmed", "legacy")  # act-grade (PHASE1.md) or legacy
PRICED_PAGES = ("personality", "preferences")  # profile facts (name, time zone) have no "typical"


def p0s(events) -> dict[str, float]:
    out = {}
    for e in probes._live(events):
        if e["payload"].get("kind") == "baseline":
            out[e["payload"]["claim"]] = e["payload"]["p0"]
    return out


def bits(events) -> dict[str, float]:
    return {c: -math.log2(p) for c, p in p0s(events).items()}


def generic(events) -> dict[str, float]:
    return {c: p for c, p in p0s(events).items() if p > GENERIC}


def pack(vault, events, today: str | None = None) -> dict:
    """`unpriced`: up to 5 live act-grade or legacy personality/preferences claims with no
    baseline; `generic`: live claims with p0 > 0.7. With `today`, none past its `(until …)`
    day. Text is plain here; the server wraps it."""
    live, p0 = claims.tiers(vault, events, today=today), p0s(events)
    return {"unpriced": [{"id": r["id"], "text": plain(r["text"], 200)} for r in live
                         if r["tier"] in PRICED_TIERS and r["page"] in PRICED_PAGES
                         and r["id"] not in p0][:UNPRICED_MAX],
            "generic": [{"id": r["id"], "text": plain(r["text"], 200), "p0": p0[r["id"]]}
                        for r in live if p0.get(r["id"], 0) > GENERIC]}
