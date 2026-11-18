"""`cortex claims --summary` (v6 agenda 9): how well the user model knows the user, per page.
Live claims by authorship tier and bet state, calibration behind the minimum-pairs gate, and
open needs. A pure read: every number is folded from the claim pages and the event log on the
spot, nothing is stored or written. Private me/ pages show only as `private`."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from cortex import bets, claims, needs
from cortex.vault import is_private

CONTESTED_MAX = 3  # contested lines shown per page


def _touches(e: dict, ids: set) -> bool:
    """False only for a forecast whose claims are all off this page (it then drops out)."""
    p = e.get("payload")
    if e.get("kind") != "log" or not isinstance(p, dict) or p.get("kind") != "forecast":
        return True
    cs = p.get("claims")
    return any(isinstance(c, str) and c in ids for c in (cs if isinstance(cs, list) else [cs]))


def report(vault, events: Iterable[dict], today: str) -> dict:
    """{pages: [{page, private} | {page, live, tiers, clicked, reflected, states, calibration,
    contested}], total: {live, tiers, clicked, reflected, states}, calibration, needs}.
    `clicked`: confirmed lines the user ticked in Cortex's own dialog (idea 11); `reflected`:
    a yes in a clean reflect. Texts are raw note text: callers escape."""
    events = list(events)
    private = {n.id for n in vault.iter_notes("me") if is_private(n)}
    cards = bets.fold(events, today)
    rows = claims.tiers(vault, events, today=today)  # no log: never records a baseline
    pages, total_t, total_s, how = [], Counter(), Counter(), Counter()
    for page in claims.PAGES:
        if page in private:
            pages.append({"page": page, "private": True})
            continue
        mine = [r for r in rows if r["page"] == page]
        ids = {r["id"] for r in mine}
        tiers = Counter(r["tier"] for r in mine)
        states = Counter(cards.get(r["id"], bets.Bet()).state for r in mine)
        total_t, total_s = total_t + tiers, total_s + states
        n = Counter(r["confirm"] for r in mine)
        how += n
        pages.append({
            "page": page, "private": False, "live": len(mine),
            "tiers": {t: tiers[t] for t in claims.TIERS}, "clicked": n["clicked"],
            "reflected": n["reflected"],
            "states": {s: states[s] for s in bets.GLYPH},
            "calibration": bets.calibration(e for e in events if _touches(e, ids)),
            "contested": [r["text"] for r in mine if cards.get(r["id"], bets.Bet()).state
                          == "contested"][:CONTESTED_MAX]})
    return {"pages": pages,
            "total": {"live": sum(total_t.values()),
                      "tiers": {t: total_t[t] for t in claims.TIERS}, "clicked": how["clicked"],
                      "reflected": how["reflected"],
                      "states": {s: total_s[s] for s in bets.GLYPH}},
            "calibration": bets.calibration(events), "needs": needs.open(events, today)}


def calibration_line(cal: dict) -> str:
    if not cal["enough"]:
        return f"calibration: not enough yet ({cal['n']} of {bets.MIN_PAIRS} settled forecasts)"
    return (f"calibration ({cal['n']} settled, log loss {cal['log_loss']}): " + ", ".join(
        f"{b['lo']:.1f}-{b['hi']:.1f} said {b['p']:.2f}, hit {b['hit']:.2f} (n {b['n']})"
        for b in cal["bins"] if b["n"]))
