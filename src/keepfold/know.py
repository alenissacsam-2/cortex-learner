"""`keepfold claims --summary` (v6 agenda 9): how well the user model knows the user, per page.
Live claims by authorship tier. A pure read: every number is folded from the
claim pages and the event log on the spot, nothing is stored or written. Private me/ pages
show only as `private`."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from keepfold import claims
from keepfold.vault import is_private


def report(vault, events: Iterable[dict], today: str) -> dict:
    """{pages: [{page, private} | {page, live, tiers, clicked, reflected}], total: {live,
    tiers, clicked, reflected}}. `clicked`: confirmed lines the user ticked in Keepfold's
    own dialog (idea 11); `reflected`: a yes in a clean reflect. Texts are raw note text:
    callers escape."""
    events = list(events)
    private = {n.id for n in vault.iter_notes("me") if is_private(n)}
    rows = claims.tiers(vault, events, today=today)  # no log: never records a baseline
    pages, total_t, how = [], Counter(), Counter()
    for page in claims.PAGES:
        if page in private:
            pages.append({"page": page, "private": True})
            continue
        mine = [r for r in rows if r["page"] == page]
        tiers = Counter(r["tier"] for r in mine)
        total_t += tiers
        n = Counter(r["confirm"] for r in mine)
        how += n
        pages.append({
            "page": page, "private": False, "live": len(mine),
            "tiers": {t: tiers[t] for t in claims.TIERS}, "clicked": n["clicked"],
            "reflected": n["reflected"]})
    return {"pages": pages,
            "total": {"live": sum(total_t.values()),
                      "tiers": {t: total_t[t] for t in claims.TIERS}, "clicked": how["clicked"],
                      "reflected": how["reflected"]}}
