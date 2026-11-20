"""Agent X-ray (v9 plan item 6, D5): what cortex_prime and cortex_context served each AI app,
and the owner's Pin and Mute.

`.cortex/xray.jsonl` (tool-private, gitignored, never an event): the last CAP calls, one row
each `{ts, tool, client, served: {block: [ids]}, trimmed: {block: [ids]}, chars}`. Ids and
counts only, never text or the prompt. A page is its bare id (`me/profile` for a me/ page), a
me/ claim is `^c-…`.

`.cortex/xray.json`: the owner's settings `{"pin": [ids], "mute": [ids]}`, same id forms. Only
the Cortex app's own clicks set them (app.server); no MCP tool takes them. Pinned pages lead
prime's Related and pinned claims lead its Preferences; a muted id is never served by prime or
context. Stdlib and cortex.vault only, so the prime hook stays light.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime

from cortex.vault import ID_RE, Vault, VaultError, is_private

log = logging.getLogger(__name__)
_CLAIM = re.compile(r"\^c-[a-z2-7]{5}")

RING = ".cortex/xray.jsonl"
SETTINGS = ".cortex/xray.json"
CAP = 500  # calls kept
LOCK_S = 0.2  # a busy vault skips the row rather than slow the tool call


def claim(cid: str) -> str:
    return f"^{cid}"


def record(vault: Vault, tool: str, client: str | None, served: dict, trimmed: dict,
           chars: int) -> None:
    """Append one call row, best effort: any error is logged and swallowed."""
    try:
        row = {"ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "tool": tool,
               "client": client or "unknown",
               "served": {k: list(v) for k, v in served.items() if v},
               "trimmed": {k: list(v) for k, v in trimmed.items() if v}, "chars": int(chars)}
        path = vault.root / RING
        with vault.lock(LOCK_S):  # the lock creates .cortex/
            old = path.read_text("utf-8").splitlines() if path.is_file() else []
            vault.write_text(path, "\n".join([*old[-(CAP - 1):], json.dumps(
                row, separators=(",", ":"))]) + "\n")
    except Exception as e:  # noqa: BLE001  (a view of the past: never fail the tool over it)
        log.warning("x-ray row not written: %s", e)


def rows(vault: Vault) -> list[dict]:
    path = vault.root / RING
    out = []
    for line in path.read_text("utf-8").splitlines() if path.is_file() else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def settings(vault: Vault) -> dict[str, set[str]]:
    """{"pin": ids, "mute": ids}; empty sets when the file is missing or broken."""
    try:
        d = json.loads((vault.root / SETTINGS).read_text("utf-8"))
    except (OSError, ValueError):
        d = {}
    d = d if isinstance(d, dict) else {}
    return {k: {x for x in d.get(k) or () if isinstance(x, str)} for k in ("pin", "mute")}


def token(id: str) -> str:
    """An opaque handle for an id the view must not show (a private page), to undo its flag."""
    return hashlib.sha256(id.encode()).hexdigest()[:16]


def set_flag(vault: Vault, which: str, id: str, on: bool) -> dict[str, set[str]]:
    """Pin or mute `id` (on) or undo it; pinning unmutes and muting unpins. The app's own
    click path calls this; nothing over MCP does. ValueError for anything but a page or
    `^c-…` claim id."""
    if which not in ("pin", "mute") or not isinstance(id, str) or not (
            ID_RE.fullmatch(id) or _CLAIM.fullmatch(id)):
        raise ValueError("Pin or mute takes one page or claim id.")
    with vault.lock():
        s = settings(vault)
        s[which].discard(id)
        if on:
            s[which].add(id)
            s["mute" if which == "pin" else "pin"].discard(id)
        vault.write_text(vault.root / SETTINGS, json.dumps(
            {k: sorted(v) for k, v in s.items()}, indent=1) + "\n")
    return s


def resolve_token(vault: Vault, tok: str) -> str | None:
    """The pinned or muted id whose `token` is `tok`, else None."""
    s = settings(vault)
    return next((i for i in sorted(s["pin"] | s["mute"]) if token(i) == tok), None)


CLIENTS = {"claude-ai": "Claude Desktop", "claude-code": "Claude Code", "cursor": "Cursor"}
ME_TITLES = {"me/profile": "Profile", "me/core": "Core lines",
             "me/teaching-contract": "Teaching contract"}
PER_CLIENT = 20  # calls shown per AI app; the ring keeps CAP in all
PRIVATE = "a private page"


def view(vault: Vault) -> dict:
    """The app's X-ray: recent calls grouped by AI app, newest first, ids resolved to titles
    now. A private or hidden page (planner.hidden) or a line of a private me/ page shows as
    "a private page", without its id; its pin or mute keeps only a `token` to undo it."""
    from cortex import claims, planner
    from cortex.catalog import Catalog

    hide = planner.hidden(vault, Catalog.load(vault))
    s = settings(vault)
    lines = {claim(r["id"]): r for r in claims.rows(vault)}
    seen: dict[str, dict] = {}

    def private_me(page: str) -> bool:
        try:
            return is_private(vault.read("me", page))
        except VaultError:
            return False

    def item(id: str) -> dict:
        if id in seen:
            return seen[id]
        flags = {"pinned": id in s["pin"], "muted": id in s["mute"]}
        if id.startswith("^"):
            r = lines.get(id)
            out = ({"title": PRIVATE, "kind": "claim", "token": token(id), **flags}
                   if r and private_me(r["page"]) else
                   {"id": id, "kind": "claim", **flags,
                    "title": r["text"] if r else "a line no longer there",
                    "where": f"{r['page']} · {r['section']}" if r else ""})
        elif id.startswith("me/"):
            out = ({"title": PRIVATE, "kind": "me"} if private_me(id[3:])
                   else {"id": id, "kind": "me", "title": ME_TITLES.get(id, id)})
        else:
            note = None
            for kind in Vault.KIND_DIRS:
                try:
                    note = vault.read(kind, id)
                    break
                except VaultError:
                    continue
            if note is not None and (is_private(note) or id in hide):
                out = {"title": PRIVATE, "kind": "page", "token": token(id), **flags}
            else:  # shortcut: a browse site page (browse/sites/) is not looked up; its id shows
                out = {"id": id, "kind": "page", **flags, "title": str(
                    note.fields.get("title") or id) if note else f"{id} (not found now)"}
        seen[id] = out
        return out

    def blocks(d) -> list[dict]:
        return [{"block": b, "items": [item(i) for i in ids if isinstance(i, str)]}
                for b, ids in (d if isinstance(d, dict) else {}).items()
                if isinstance(ids, list) and ids]

    groups: dict[str, list] = {}
    for r in reversed(rows(vault)):
        groups.setdefault(str(r.get("client") or "unknown"), []).append(r)
    return {"clients": [{"client": CLIENTS.get(c, c), "total": len(rs), "calls": [
        {"ts": r.get("ts"), "tool": r.get("tool"), "chars": r.get("chars"),
         "served": blocks(r.get("served")), "trimmed": blocks(r.get("trimmed"))}
        for r in rs[:PER_CLIENT]]} for c, rs in groups.items()],
        "pinned": [item(i) for i in sorted(s["pin"])],
        "muted": [item(i) for i in sorted(s["mute"])]}
