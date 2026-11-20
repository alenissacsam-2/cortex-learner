"""Plan 5b (N2): repeated corrections. The user's own short corrective turns in imported AI
chats ("don't …", "stop …", "always …", "never …", "no, I said …", "please use …"; at most
MAX_CHARS, not a question), grouped by `review.scorer`, and a group seen in MIN_CHATS or more
different chats offered in the app's Review as a me/core line ("You told 3 chats: …").

Read-only over the vault; reads no model and sends nothing anywhere. The scan is cached in
CACHE (git-ignored, tool-private): per chat page its (mtime, size) and its correction lines, so
a rescan reads only the pages changed since. Private chats (`access.audience_of_note`: LOCAL)
are never scanned; their cache rows hold no text."""

from __future__ import annotations

import json
import re
from collections import Counter

from cortex import access, claims, harvest, review, topics
from cortex.vault import VaultError

CACHE = ".cortex/corrections.json"
VERSION = 1
MAX_CHARS = 200
MIN_CHATS = 2
SHOWN = 5
_START = re.compile(r"^(?:(?:no|nope|again)\b[\s,.!:;-]*)?(?:please\s+)?(?:don't|dont|do not|"
                    r"stop|always|never|i (?:already )?said|i told you)\b|^please use\b", re.I)


def is_correction(turn: str) -> bool:
    """A short corrective turn: a `_START` opening, at most MAX_CHARS, not a question."""
    t = turn.replace("’", "'").strip()
    return 0 < len(t) <= MAX_CHARS and not t.endswith("?") and bool(_START.match(t))


def _lines(body: str) -> list[str]:
    return [t for t in (" ".join(x.split()) for x in harvest.user_turns(body) or [])
            if is_correction(t)]


def scan(vault) -> dict[str, list[str]]:
    """{chat page id: its correction lines} over the non-private imported chat pages; reads only
    pages whose (mtime, size) changed since the cached scan, and rewrites CACHE only then."""
    path = vault.root / CACHE
    try:
        old = json.loads(path.read_bytes().decode("utf-8"))
        old = old["chats"] if old.get("version") == VERSION else {}
    except (OSError, ValueError, AttributeError, KeyError):
        old = {}
    rows, dirty = {}, False
    for p in vault.note_paths("resource"):
        try:
            st = p.stat()
        except OSError:
            continue
        fp = [st.st_mtime_ns, st.st_size]
        hit = old.get(p.stem)
        if isinstance(hit, dict) and hit.get("fp") == fp and isinstance(hit.get("lines"), list):
            rows[p.stem] = hit
            continue
        dirty = True
        try:
            n = vault.read_path(p)
        except VaultError:
            n = None
        ok = (n is not None and bool(topics.page_source(n.body))  # semantic.is_chat, no numpy
              and access.audience_of_note(n) is access.Audience.CLOUD)
        rows[p.stem] = {"fp": fp, "lines": _lines(n.body) if ok else []}
    if dirty or old.keys() - rows.keys():
        vault.write_text(path, json.dumps({"version": VERSION, "chats": rows},
                                          ensure_ascii=False))
    return {k: v["lines"] for k, v in rows.items() if v["lines"]}


def proposals(vault, events, rows: list[dict]) -> list[dict]:
    """Up to SHOWN groups of similar correction lines from MIN_CHATS or more chats, most chats
    first, as {key (`review.text_hash`), text (raw: the most common wording, then the
    shortest), chats}. Left out: a group the user dismissed (any member's hash) and one already
    said by a live line of `rows`."""
    items = [(cid, line) for cid, lines in sorted(scan(vault).items())
             for line in dict.fromkeys(lines)]
    if not items:
        return []
    voided = {e.get("ref") for e in events if e.get("kind") == "void"}
    gone = {c.get("hash") for e in events if e.get("kind") == "change" and e.get("id") not in voided
            and isinstance(c := review._payload(e).get("change"), dict)
            and c.get("tool") == "dismiss" and c.get("id") == "corrections"}
    live = [r["text"] for r in rows]
    score, cut = review.scorer([line for _, line in items] + live)
    groups: list[list[int]] = []
    for i in range(len(items)):  # greedy: the first group whose first line is close enough
        g = next((g for g in groups if score(g[0], i) >= cut), None)
        if g is None:
            groups.append([i])
        else:
            g.append(i)
    out = []
    for g in groups:
        chats = {items[i][0] for i in g}
        texts = [items[i][1] for i in g]
        if len(chats) < MIN_CHATS or any(review.text_hash(t) in gone for t in texts):
            continue
        if any(score(g[0], len(items) + k) >= cut for k in range(len(live))):
            continue  # already a live line about the user
        said = Counter(claims.norm(t) for t in texts)
        text = min(texts, key=lambda t: (-said[claims.norm(t)], len(t), t))
        out.append({"key": review.text_hash(text), "text": text, "chats": len(chats)})
    out.sort(key=lambda x: (-x["chats"], x["text"]))
    return out[:SHOWN]
