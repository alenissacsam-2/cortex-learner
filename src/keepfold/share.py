"""v5 PHASE4 V5 share tiers: which me/ pages may be disclosed to an audience; plus the kill
switch and the access log of acting as the user (`keepfold_as_me`).

A me/ page carries `share: self|close|work|public` in frontmatter; missing or unknown means
`self` (default-deny). The access event itself is written by the MCP server
(`Keepfold._access`); `access_rows` reads it back for `keepfold access`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from keepfold import people
from keepfold.vault import Vault

TAG_TIERS = {"friend": "close", "family": "close", "partner": "close",
             "professor": "work", "recruiter": "work", "colleague": "work", "manager": "work",
             "client": "work", "public": "public"}
SEEN_BY = {"public": {"public", "work", "close"}, "work": {"work", "close"},
           "close": {"close"}, "self": set()}  # page share -> audience tiers that may see it
KILL = ".keepfold/kill"


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


def killed(vault: Vault) -> bool:
    return (vault.root / KILL).is_file()


def set_kill(vault: Vault, on: bool) -> bool:
    """Turn the kill switch on or off; returns whether anything changed."""
    path = vault.root / KILL
    if not on:
        return vault.remove(path)
    if path.is_file():
        return False
    vault.write_text(path, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")
    return True


def access_rows(events, since_day: str | None = None) -> list[dict]:
    """The server-written `access` log events, oldest first: {day, ts, client, access,
    audience, task}. Voids do not hide them: this is an audit trail."""
    out = []
    for e in events:
        p = e.get("payload")
        if e.get("kind") != "log" or not isinstance(p, dict) or p.get("kind") != "access":
            continue
        if since_day and str(e.get("day", "")) < since_day:
            continue
        out.append({"day": e.get("day"), "ts": e.get("ts"),
                    "client": (e.get("grader") or {}).get("client"),
                    "access": p.get("access"), "audience": p.get("audience"),
                    "task": p.get("task")})
    return out
