"""Probes (v5 Phase 1 F6, SKEP-2 sealed per SCI-5): twin vs stranger forecasts, settled by the
user. Pure folds over events; voided events do not count."""

from __future__ import annotations

from collections.abc import Iterable

from cortex.events import live as _live_events

AREAS = ("values", "decisions", "preferences", "habits", "opinions", "work", "learning",
         "social", "taste", "other")
HITS = {"yes": 1.0, "partly": 0.5, "no": 0.0}


def _live(events: Iterable[dict]) -> list[dict]:
    return [e for e in _live_events(events) if e.get("kind") == "log"
            and isinstance(e.get("payload"), dict)]


def state(events: Iterable[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    """(live forecast events by id, live settle events by the forecast id they settle)."""
    live = _live(events)
    forecasts = {e["id"]: e for e in live if e["payload"].get("kind") == "forecast"}
    settles: dict[str, dict] = {}
    for e in live:
        if e["payload"].get("kind") == "settle":
            settles.setdefault(e["payload"].get("forecast"), e)
    return forecasts, settles


def _row(n: int, twin: float, paired: int, twin_paired: float, stranger: float,
         misses: int = 0, same: int = 0) -> dict:
    row = {"n": n, "twin": round(twin / n, 3), "paired": paired}
    if paired:
        row |= {"stranger": round(stranger / paired, 3),
                "uplift": round((twin_paired - stranger) / paired, 3)}
    if misses:
        row |= {"misses": misses, "pull": round(same / misses, 3)}
    return row


def summary(events: Iterable[dict]) -> dict:
    """Per area and overall: n settled and the twin hit rate over all of them (partly = 0.5);
    `paired` settles had a stranger arm, and `stranger` and `uplift` are over those only.
    `taps` counts settled tell-me taps (forecasts with a `need`, PHASE2.md G2). `pull`
    (PHASE3.md H5): among `misses`, the twin misses on forecasts with a stranger and a `same`
    flag, the share where twin and stranger had guessed the same. Taps count in `n`, `twin`
    and `taps` (and still feed `bets`) but never in `paired`, `stranger`, `uplift` or `pull`:
    their in-chat stranger guess may be anchored on the vault."""
    forecasts, settles = state(events)
    sums: dict[str, list[float]] = {}
    taps = 0
    for fid, s in settles.items():
        f = forecasts.get(fid)
        if f is None:
            continue
        p, fp = s["payload"], f["payload"]
        tap = "need" in fp
        taps += tap
        twin = HITS[p["twin_hit"]]
        # the server stores stranger_hit only with a stranger arm; a tap's stranger guess was
        # made in a chat that may have seen the vault, so taps stay out of the benchmark (Q3)
        paired = p.get("stranger_hit") in HITS and not tap
        # pre-H1 forecasts have no `same` and stay out of `pull`
        miss = (p["twin_hit"] == "no" and "stranger" in fp and isinstance(fp.get("same"), bool)
                and not tap)
        for key in (fp.get("area", "other"), "_all"):
            r = sums.setdefault(key, [0, 0.0, 0, 0.0, 0.0, 0, 0])
            r[0] += 1
            r[1] += twin
            if paired:
                r[2] += 1
                r[3] += twin
                r[4] += HITS[p["stranger_hit"]]
            if miss:
                r[5] += 1
                r[6] += fp["same"]
    total = sums.pop("_all", None)
    return {"overall": _row(*total) if total else {"n": 0},
            "areas": {a: _row(*sums[a]) for a in sorted(sums)},
            "open": sum(1 for fid in forecasts if fid not in settles), "taps": taps}
