"""`keepfold digest` (council item 9): an on-demand review of chats imported since the last digest.

Never scheduled and no model call (KEEPFOLD-DESIGN.md:48). Each imported chat (claude.ai,
ChatGPT, Gemini, Perplexity: not Claude Code transcripts, whose chats log touches live) that no
earlier digest listed is mapped to at most MAX_CONCEPTS catalog concepts by the local search
(`semantic.recall`: the embedder when it is downloaded, keywords otherwise). The index already
leaves private and hidden concepts out. Nothing is written without the student's yes: each
confirmed concept is one `touch` log (context, never mastery: planner._studied skips it), and
one `digest` log names the chats listed, so the next digest starts after them.
"""

from __future__ import annotations

from collections.abc import Iterable

from keepfold import chats, render, semantic
from keepfold.events import EventLog, live

MAX_CONCEPTS = 3  # concepts proposed per chat
POOL = 20  # pages recalled per chat before keeping concepts only
QUERY_CHARS = 600  # the chat's user turns, cut to this, are the search query
LIMIT = 10  # chats per digest; the rest wait for the next one


def reviewed(events: Iterable[dict]) -> set[str]:
    """Chat ids every live `digest` log listed."""
    out: set[str] = set()
    for e in live(list(events)):
        p = e.get("payload")
        if e.get("kind") == "log" and isinstance(p, dict) and p.get("kind") == "digest" \
                and isinstance(p.get("chats"), list):
            out.update(c for c in p["chats"] if isinstance(c, str))
    return out


def new_chats(seen: set[str], limit: int = LIMIT, since: str | None = None) -> list[dict]:
    """Imported chats not in `seen`, newest first: {id, source, updated, title, query}."""
    out = []
    for p in chats._files():
        if len(out) >= limit:
            break
        source, cid = chats.source_of(p), chats._cid(p)
        if source == "claude-code" or cid in seen:
            continue
        ts = chats.turns(p)
        users = [t for r, _, t in ts if r == "user"]
        if not users or (since and ts[-1][1][:10] < since):
            continue
        out.append({"id": cid, "source": source, "updated": ts[-1][1],
                    "title": render.plain(chats.redact(users[0].strip().splitlines()[0]),
                                          chats.FIRST_LINE_CHARS),
                    "query": chats.redact(" ".join(" ".join(users).split()))[:QUERY_CHARS]})
    return out


def concepts_for(index, query: str, embed=None, quiet: dict | None = None) -> list[dict]:
    """Up to MAX_CONCEPTS visible catalog concepts the query is about: [{id, title}]."""
    entries = getattr(index.catalog, "entries", {})
    hits = semantic.recall(index, query, embed, POOL, 0, quiet=quiet)
    return [{"id": h["id"], "title": render.plain(h["title"], 80)} for h in hits
            if h["kind"] == "concept" and h["id"] in entries][:MAX_CONCEPTS]


def record(vault, confirmed: list[tuple[str, str]], listed: list[str]) -> None:
    """One `touch` per confirmed (chat id, concept id), then the `digest` marker."""
    log = EventLog(vault)
    for chat_id, concept in confirmed:
        log.append(session="cli", kind="log", source="self", fields={
            "payload": {"kind": "touch", "ids": [concept], "chat": chat_id}})
    if listed:
        log.append(session="cli", kind="log", source="self",
                   fields={"payload": {"kind": "digest", "chats": listed}})
