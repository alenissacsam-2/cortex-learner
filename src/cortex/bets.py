"""Claim cards (v5 PHASE3.md H1): one FSRS card per claim, a pure fold over events like
`levels`; no confidence is ever stored. Plus the claim resolver, calibration and the reflect
pack (H2)."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from fsrs import Card, Rating, Scheduler

from cortex import claims, levels, probes
from cortex import events as _events
from cortex.render import plain
from cortex.vault import VaultError

GLYPH = {"untested": "◌", "learning": "◐", "reliable": "●", "recheck": "↻", "contested": "⚑"}
RELIABLE_STABILITY, RELIABLE_HIT_DAYS = 10.0, 2
SLIP_DAYS = 30  # a miss on a card with stability ≥ 10 and no miss in this many days is Hard
MISS_RUN = 2  # (default) this many settle days in a row with a miss: contested
CONFIRM_DAYS = 14  # (default) a miss this many days or fewer after a confirmation: contested
RECHECK_MAX = 3
MIN_PAIRS = 20  # settled forecasts before calibration is shown
RATING = {"hit": Rating.Good, "partly": Rating.Hard, "weak": Rating.Hard}
SETTLED = {"yes": "hit", "partly": "partly", "no": "miss"}


@dataclass
class Bet:
    state: str = "untested"
    R: float | None = None  # None: no card yet
    stability: float | None = None
    due: str | None = None
    hits: int = 0  # discriminating
    plain_hits: int = 0  # twin right on a forecast the stranger matched or had no stranger
    misses: int = 0
    first_miss: str | None = None  # the first miss after the last hit
    last_day: str | None = None


def live(vault, today: str | None = None) -> list[dict]:
    """The live claim rows (`claims.parse`) of every claim page; with `today`, none past its
    own `(until …)` day (`claims.is_live`)."""
    return [r for r in claims.rows(vault) if claims.is_live(r, today)]


def resolve(vault, item, today: str | None = None) -> str:
    """A claim id (`c-…`, `^` optional) or a claim line's text as served, without the id ->
    that live claim's id, by id or by `claims.norm` equality; else `invalid_argument`."""
    if isinstance(item, str) and item.strip():
        item = item.strip().removesuffix("(said)").removesuffix("(ask)")  # H3 serving labels
        rows, cid = live(vault, today), item.strip().lstrip("^")
        found = {r["id"] for r in rows if r["id"] == cid} or {
            r["id"] for r in rows if claims.norm(r["text"]) == claims.norm(item)}
        if len(found) == 1:
            return found.pop()
    raise VaultError("invalid_argument", "A claim must be one live claim's ^c-… id or its line "
                     "text as served; read the me/* page first.")


def _items(events: list[dict]) -> dict[str, dict[str, list[tuple[str, str]]]]:
    """claim -> local day -> [(ts, what)], what: hit | plain | partly | miss | weak."""
    out: dict[str, dict[str, list[tuple[str, str]]]] = {}

    def add(cids, ev: dict, what: str) -> None:
        for c in cids if isinstance(cids, list) else [cids]:
            if isinstance(c, str):
                out.setdefault(c, {}).setdefault(ev["day"], []).append((ev["ts"], what))

    forecasts, settles = probes.state(events)
    for fid, s in settles.items():
        fp = forecasts[fid]["payload"] if fid in forecasts else {}
        what = SETTLED.get(s["payload"].get("twin_hit"))
        if fp.get("claims") and what:
            sharp = "stranger" in fp and fp.get("same") is False  # discriminating
            add(fp["claims"], s, "plain" if what == "hit" and not sharp else what)
    for e in probes._live(events):  # decision 7: a yes is weak evidence
        p = e["payload"]
        if p.get("kind") == "reconfirm" and p.get("verdict") == "yes":
            add(p.get("claim"), e, "weak")
    for e in _events.live(events):
        pl = e.get("payload") if e.get("kind") == "change" else None
        c = pl.get("change") if isinstance(pl, dict) else None
        if isinstance(c, dict) and c.get("tier") == "confirmed" and c.get("op") != "retire":
            add(c.get("claim"), e, "weak")
    return out


def fold(events: Iterable[dict], today: str, now: datetime | None = None) -> dict[str, Bet]:
    """Every claim any settle, reconfirm or confirmed write touched, as of local day `today`.
    One review per claim per local day, the lowest rating winning; `R` at `now` (default:
    noon of `today`), from the same scheduler and forgetting curve as `levels`.
    Contested (cleared by the next hit): an Again after the claim was once reliable; a miss on
    the MISS_RUN-th settle day in a row with a miss (a day with only hits, plain hits or
    partly breaks the run); a miss within CONFIRM_DAYS of a confirmation (a confirmed write or
    a reconfirm yes), which is also never softened by the slip rule."""
    today_d = date.fromisoformat(today)
    now = now or datetime.combine(today_d, time(12)).astimezone(UTC)  # naive -> system zone
    out: dict[str, Bet] = {}
    for cid, days in _items(list(events)).items():
        b = out[cid] = Bet()
        scheduler = Scheduler(desired_retention=levels.RETENTION, learning_steps=(),
                              relearning_steps=(), enable_fuzzing=False)
        card, hit_days, last_miss, confirmed, run = Card(card_id=1), set(), None, None, 0
        last_hit = was_reliable = contested = reliable = False
        for day in sorted(days):
            items, d = sorted(days[day]), date.fromisoformat(day)
            whats = {w for _, w in items}
            b.last_day = day
            if "weak" in whats:  # a confirmed write or a reconfirm yes, same day included
                confirmed = d
            fresh = confirmed is not None and (d - confirmed).days <= CONFIRM_DAYS
            if whats & {"hit", "plain", "partly", "miss"}:  # a settle day; partly breaks a run
                run = run + 1 if "miss" in whats else 0
            slip = not fresh and (card.stability or 0) >= RELIABLE_STABILITY and (
                last_miss is None or (d - last_miss).days >= SLIP_DAYS)
            rate = {**RATING, "miss": Rating.Hard if slip else Rating.Again}
            for _, what in items:
                if what == "hit":
                    b.hits, b.first_miss = b.hits + 1, None
                    hit_days.add(day)
                elif what == "miss":
                    b.misses, b.first_miss, last_miss = b.misses + 1, b.first_miss or day, d
                b.plain_hits += what == "plain"
            ratings = [rate[w] for _, w in items if w != "plain"]
            if not ratings:
                continue
            rating = min(ratings)
            settled = [rate[w] for _, w in items if w in ("hit", "partly", "miss")]
            if settled:  # the last rated settlement (the day's lowest)
                last_hit = min(settled) == Rating.Good
            if rating == Rating.Again and was_reliable or "miss" in whats and (
                    fresh or run >= MISS_RUN):
                contested = True
            elif "hit" in whats:
                contested = False
            card, _ = scheduler.review_card(card, rating, levels._review_at(day))
            reliable = (card.stability >= RELIABLE_STABILITY
                        and len(hit_days) >= RELIABLE_HIT_DAYS and last_hit)
            was_reliable = was_reliable or reliable
        if card.last_review is not None:
            b.stability = round(card.stability, 2)
            b.due = card.due.date().isoformat()  # due is _review_at(day) + interval
            b.R = round(levels._retrievability(scheduler, card, now), 3)
        if contested:  # before untested: two misses contest a claim that never had a hit
            b.state = "contested"
        elif not b.hits:
            b.state = "untested"
        elif today > b.due:
            b.state = "recheck"
        else:
            b.state = "reliable" if reliable else "learning"
    return out


def calibration(events: Iterable[dict]) -> dict:
    """Server-stamped `predicted_p` of settled forecasts with claims against the twin hit
    (yes 1, partly 0.5, no 0): 5 bins and the log loss."""
    forecasts, settles = probes.state(events)
    pairs = [(fp["predicted_p"], probes.HITS[s["payload"]["twin_hit"]])
             for fid, s in settles.items() if fid in forecasts
             and (fp := forecasts[fid]["payload"]).get("claims")
             and isinstance(fp.get("predicted_p"), (int, float))]
    bins = [{"lo": i / 5, "hi": (i + 1) / 5, "n": 0, "p": 0.0, "hit": 0.0} for i in range(5)]
    for p, y in pairs:
        row = bins[min(int(p * 5), 4)]
        row["n"], row["p"], row["hit"] = row["n"] + 1, row["p"] + p, row["hit"] + y
    for row in bins:
        if row["n"]:
            row["p"], row["hit"] = round(row["p"] / row["n"], 3), round(row["hit"] / row["n"], 3)
        else:
            del row["p"], row["hit"]
    eps = 1e-6
    loss = [-(y * math.log(max(p, eps)) + (1 - y) * math.log(max(1 - p, eps)))
            for p, y in pairs]
    return {"n": len(pairs), "enough": len(pairs) >= MIN_PAIRS, "bins": bins,
            "log_loss": round(sum(loss) / len(loss), 3) if loss else None}


def pack(vault, events: Iterable[dict], today: str) -> dict:
    """The reflect pack's `bets` (H2): contested and recheck (≤ 3, most overdue first) live
    claims, the ones whose latest reconfirm was `no`, calibration and counts per state.
    Text is plain here; the server wraps it."""
    events = list(events)
    cards, rows = fold(events, today), live(vault, today)
    said_no = {}
    for e in probes._live(events):
        if e["payload"].get("kind") == "reconfirm":
            said_no[e["payload"].get("claim")] = e["payload"].get("verdict") == "no"

    def row(r: dict, **more) -> dict:
        return {"id": r["id"], "page": r["page"], "text": plain(r["text"], 200), **more}

    counts = dict.fromkeys(GLYPH, 0)
    for r in rows:
        counts[cards.get(r["id"], Bet()).state] += 1
    recheck = sorted((row(r, due=cards[r["id"]].due) for r in rows
                      if cards.get(r["id"], Bet()).state == "recheck"), key=lambda x: x["due"])
    return {"contested": [row(r, first_miss=cards[r["id"]].first_miss) for r in rows
                          if cards.get(r["id"], Bet()).state == "contested"],
            "recheck": recheck[:RECHECK_MAX],
            "retire": [row(r) for r in rows if said_no.get(r["id"])],
            "calibration": calibration(events), "counts": counts}
