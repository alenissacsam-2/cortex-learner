"""v5 PHASE4 V5: the `me/delegation.md` boundaries, the kill switch and the access log.

The access event itself is written by the MCP server (`Cortex._access`); `access_rows` reads
it back for `cortex access`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from cortex.vault import HEADING_RE, Vault, VaultError

PAGE = "delegation"
SECTIONS = {"alone": "alone", "ask first": "ask", "never": "never"}  # heading -> key
KILL = ".cortex/kill"
_CONFIRMED = re.compile(r"\s*\(confirmed \d{4}-\d{2}-\d{2}\)\s*$")


def read(vault: Vault) -> dict[str, list[str]]:
    """{alone, ask, never}: the list lines of each section, as plain text."""
    out: dict[str, list[str]] = {"alone": [], "ask": [], "never": []}
    try:
        body = vault.read("me", PAGE).body
    except VaultError as e:
        if e.code == "unknown_id":
            return out
        raise
    key = None
    for line in body.splitlines():
        if m := HEADING_RE.match(line):
            if len(m.group(1)) <= 2:  # a ### subheading stays in its section
                key = SECTIONS.get(m.group(2).lower()) if len(m.group(1)) == 2 else None
            continue
        if key and line.lstrip().startswith("- ") and (
                text := _CONFIRMED.sub("", line.lstrip()[2:]).strip()):
            out[key].append(text)
    return out


def check_write(exposed: bool, confirmed: bool, role: str = "owner") -> None:
    """Decision 3: chat may write me/delegation.md only with the user's OK, in an owner
    session that has not read outside text."""
    if role != "owner":
        raise VaultError("role_refused", f"The {role} role cannot change me/delegation.md.")
    if exposed:
        raise VaultError("refused_exposed", "This chat has read outside text (a source, a raw "
                         "file or a past chat), so it cannot change me/delegation.md.")
    if not confirmed:
        raise VaultError("invalid_argument", "me/delegation.md changes need "
                         "user_confirmed=true; ask the user first.")


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
