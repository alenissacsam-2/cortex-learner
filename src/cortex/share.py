"""v5 PHASE4 V5 share tiers: which me/ pages may be disclosed to an audience.

A me/ page carries `share: self|close|work|public` in frontmatter; missing or unknown means
`self` (default-deny).
"""

from __future__ import annotations

from cortex import people
from cortex.vault import Vault

TAG_TIERS = {"friend": "close", "family": "close", "partner": "close",
             "professor": "work", "recruiter": "work", "colleague": "work", "manager": "work",
             "client": "work", "public": "public"}
SEEN_BY = {"public": {"public", "work", "close"}, "work": {"work", "close"},
           "close": {"close"}, "self": set()}  # page share -> audience tiers that may see it


def tier(vault: Vault, audience: str) -> str:
    """close, work or public: a person id maps to its page tier, a tag by TAG_TIERS, else
    work (an unknown people id too)."""
    if str(audience).startswith(people.PREFIX):
        p = people.get(vault, audience)
        return p["tier"] if p else "work"
    return TAG_TIERS.get(str(audience), "work")


def allowed(vault: Vault, audience: str) -> set[str]:
    """Bare ids of the me/ pages (as `claims` rows name them) disclosable to `audience`."""
    t = tier(vault, audience)
    return {n.id for n in vault.iter_notes("me")
            if t in SEEN_BY.get(str(n.fields.get("share") or "self"), set())}
