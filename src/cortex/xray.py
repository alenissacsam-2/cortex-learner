"""Agent X-ray (v9 plan item 6, D5): what cortex_prime and cortex_context served each AI app,
and the owner's Pin and Mute. R27b: also what cortex_find, cortex_recall and cortex_read
returned, a broad-read flag, a pasted reply traced to the served items, two apps compared.

`.cortex/xray.jsonl` (tool-private, gitignored, never an event): the last CAP calls, and apart
the last READ_CAP find, recall and read calls, one row each `{ts, tool, client, served:
{block: [ids]}, trimmed: {block: [ids]}, chars}`. Ids and counts only, never text, the prompt,
the query or a pasted reply. A page is its bare id (`me/profile` for a me/ page), a me/ claim
is `^c-…`.

`.cortex/xray.json`: the owner's settings `{"pin": [ids], "mute": [ids], "ack": {client: ts}}`,
same id forms; `ack` holds the end of each app's broad-read window the owner said Got it to.
Only the Cortex app's own clicks set them (app.server); no MCP tool takes them. Pinned pages
lead prime's Related and pinned claims lead its Preferences; a muted id is never served by
prime or context. Stdlib and cortex.vault only at import, so the prime hook stays light.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import Counter
from datetime import UTC, datetime, timedelta

from cortex.vault import ID_RE, Vault, VaultError, is_private

log = logging.getLogger(__name__)
_CLAIM = re.compile(r"\^c-[a-z2-7]{5}")

RING = ".cortex/xray.jsonl"
SETTINGS = ".cortex/xray.json"
CAP = 500  # calls kept, find, recall and read apart (READ_CAP)
LOCK_S = 0.2  # a busy vault skips the row rather than slow the tool call
# R27b: what an app pulls itself, kept apart up to READ_CAP so a busy agent's searches never
# push the prime, context, chat-turn, hook and resource rows (review's crowded, unused and use
# folds read them) out of the ring
READS = ("cortex_find", "cortex_recall", "cortex_read")
READ_CAP = 1000
_READ_ROW = re.compile(r'\{"ts":"[^"]*","tool":"cortex_(?:find|recall|read)"')
# the broad-read flag: over WIDE_PAGES distinct pages through READS within WIDE_MINUTES. A
# chat's few finds (8 hits each), a recall and some reads come to 10-30 pages; over 50 in a
# quarter hour is a dump, or an app reading on its own
WIDE_PAGES = 50
WIDE_MINUTES = 15
WIDE_DAYS = 7  # the windows looked at: the last week's
TS = "%Y-%m-%dT%H:%M:%SZ"


def claim(cid: str) -> str:
    return f"^{cid}"


def record(vault: Vault, tool: str, client: str | None, served: dict, trimmed: dict,
           chars: int) -> None:
    """Append one call row, best effort: any error is logged and swallowed."""
    try:
        row = {"ts": datetime.now(UTC).strftime(TS), "tool": tool,
               "client": client or "unknown",
               "served": {k: list(v) for k, v in served.items() if v},
               "trimmed": {k: list(v) for k, v in trimmed.items() if v}, "chars": int(chars)}
        path = vault.root / RING
        with vault.lock(LOCK_S):  # the lock creates .cortex/
            old = path.read_text("utf-8").splitlines() if path.is_file() else []
            vault.write_text(path, "\n".join(_trim([*old, json.dumps(
                row, separators=(",", ":"))])) + "\n")
    except Exception as e:  # noqa: BLE001  (a view of the past: never fail the tool over it)
        log.warning("x-ray row not written: %s", e)


def _trim(lines: list[str]) -> list[str]:
    """The last READ_CAP find, recall and read rows and the last CAP others, in order."""
    read = [bool(_READ_ROW.match(x)) for x in lines]
    drop = {True: sum(read) - READ_CAP, False: len(lines) - sum(read) - CAP}
    out = []
    for x, r in zip(lines, read, strict=True):
        if drop[r] > 0:
            drop[r] -= 1
        else:
            out.append(x)
    return out


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


def _stored(vault: Vault) -> dict:
    """The settings file as stored; {} when it is missing or broken."""
    try:
        d = json.loads((vault.root / SETTINGS).read_text("utf-8"))
    except (OSError, ValueError):
        d = {}
    return d if isinstance(d, dict) else {}


def settings(vault: Vault) -> dict[str, set[str]]:
    """{"pin": ids, "mute": ids}; empty sets when the file is missing or broken."""
    d = _stored(vault)
    return {k: {x for x in d.get(k) or () if isinstance(x, str)} for k in ("pin", "mute")}


def _acks(d: dict) -> dict[str, str]:
    """The stored Got it window ends by client (the ring's `client`)."""
    a = d.get("ack")
    return {k: v for k, v in a.items() if isinstance(v, str)} if isinstance(a, dict) else {}


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
        vault.write_text(vault.root / SETTINGS, json.dumps(  # the Got it ends kept
            {**_stored(vault), **{k: sorted(v) for k, v in s.items()}}, indent=1) + "\n")
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
OFTEN = 5  # "served most often" rows per AI app


def _items(vault: Vault):
    """(item(id), blocks({block: ids}), settings) as the app shows ids: resolved to titles
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

    return item, blocks, s


def present(vault: Vault, served: dict, trimmed: dict, chars: int) -> dict:
    """One call (the app's dry run) as `view` shows a call: titles, never text."""
    _, blocks, _ = _items(vault)
    return {"served": blocks(served), "trimmed": blocks(trimmed), "chars": chars}


def view(vault: Vault) -> dict:
    """The app's X-ray: recent calls grouped by AI app, newest first (`_items` shows the ids).
    Per app also its raw `name` ("" when it sent none), the `scopes` its profile gives it
    (cortex.profiles), that profile's `match` as `rule` (None: it gets `unknown`), the other
    apps here the same rule covers (`shared`), and the OFTEN pages and lines served to it most
    often (`often`, with a `count`; me/ pages, which no one pins or mutes, and READS rows'
    hits left out). Also the
    owner's `profiles`, their cap and `all_scopes` (access.SCOPES), for the scope bar's own
    save through /api/profiles. R27b: `wide`, the app's broad-read window (`wide`) not yet
    said Got it to, else None."""
    from cortex import profiles
    from cortex.access import SCOPES

    item, blocks, s = _items(vault)
    acks = _acks(_stored(vault))
    groups: dict[str, list] = {}
    for r in reversed(rows(vault)):
        groups.setdefault(str(r.get("client") or "unknown"), []).append(r)
    data = profiles.load(vault)
    names = {c: "" if c == "unknown" else c for c in groups}  # record() had no name
    rule = {c: profiles.match(data, n) for c, n in names.items()}

    def often(rs: list[dict]) -> list[dict]:
        # what prime and context served, which Pin and Mute act on; never a search's hits
        n = Counter(i for r in rs if r.get("tool") not in READS
                    and isinstance(r.get("served"), dict)
                    for ids in r["served"].values()
                    if isinstance(ids, list) for i in ids
                    if isinstance(i, str) and not i.startswith("me/"))
        return [{**item(i), "count": k} for i, k in n.most_common(OFTEN)]

    return {"clients": [{
        "client": CLIENTS.get(c, c), "name": names[c], "total": len(rs),
        "scopes": profiles.scopes_of(data, names[c]),
        "rule": None if rule[c] is None else data["profiles"][rule[c]]["match"],
        "shared": [CLIENTS.get(o, o) for o in groups if o != c and rule[c] is not None
                   and rule[o] == rule[c]],
        "often": often(rs), "wide": wide(rs, acks.get(c, "")), "calls": [
            {"ts": r.get("ts"), "tool": r.get("tool"), "chars": r.get("chars"),
             "served": blocks(r.get("served")), "trimmed": blocks(r.get("trimmed"))}
            for r in rs[:PER_CLIENT]]} for c, rs in groups.items()],
        "pinned": [item(i) for i in sorted(s["pin"])],
        "muted": [item(i) for i in sorted(s["mute"])],
        "profiles": data, "profiles_max": profiles.MAX_PROFILES, "all_scopes": list(SCOPES)}


# ---- R27b (G5b-5): the broad-read flag --------------------------------------------------------
def _when(r: dict) -> datetime | None:
    try:
        return datetime.strptime(str(r.get("ts")), TS).replace(tzinfo=UTC)
    except ValueError:
        return None


def wide(rs: list[dict], after: str = "", now: datetime | None = None) -> dict | None:
    """The most distinct pages one app's rows `rs` got through READS (resource reads serve
    packs, not pages) within any WIDE_MINUTES of the last WIDE_DAYS, counting only calls after
    `after` (the end of the window it said Got it to), as {n, day (local), end (that window's
    last call)} when over WIDE_PAGES, else None. A private page counts, by its id only."""
    from cortex.events import local_day

    since = (now or datetime.now(UTC)) - timedelta(days=WIDE_DAYS)
    calls = sorted(((t, {i for ids in r["served"].values() if isinstance(ids, list)
                         for i in ids if isinstance(i, str)})
                    for r in rs if r.get("tool") in READS and isinstance(r.get("served"), dict)
                    and str(r.get("ts")) > after and (t := _when(r)) and t >= since),
                   key=lambda c: c[0])
    have: Counter = Counter()
    lo, best, end = 0, 0, None
    for t, ids in calls:
        have.update(ids)
        while calls[lo][0] < t - timedelta(minutes=WIDE_MINUTES):
            have.subtract(calls[lo][1])
            have += Counter()  # drops the pages no call left in the window got
            lo += 1
        if len(have) > best:
            best, end = len(have), t
    if best <= WIDE_PAGES:
        return None
    return {"n": best, "day": local_day(end), "end": end.strftime(TS)}


def ack(vault: Vault, client: str, end: str) -> None:
    """Got it on an app's broad-read warning (the app's own click only; `client` as the view's
    `name`): `end`, the shown window's, is kept as its `ack`, so only later calls raise it
    again. ValueError when that app shows no such warning."""
    key = client or "unknown"
    with vault.lock():
        d = _stored(vault)
        acks = _acks(d)
        w = wide([r for r in rows(vault) if str(r.get("client") or "unknown") == key],
                 acks.get(key, ""))
        if w is None or w["end"] != end:
            raise ValueError("That warning is no longer shown.")
        vault.write_text(vault.root / SETTINGS, json.dumps(
            {**d, "ack": {**acks, key: end}}, indent=1) + "\n")


# ---- R27b (C2): trace a reply -----------------------------------------------------------------
TRACE_CALLS = 10  # an app's latest calls a pasted reply is matched against
TRACE_HITS = 5
TRACE_WORDS = 8  # shared words shown per item
TRACE_BODY = 1500  # characters of a page's body matched, after its title and summary
TRACE_MAX = 4000  # characters of a pasted reply
_WORD = re.compile(r"[^\W_]+")


def _fold(w: str) -> str:
    """A light plural fold: policies -> policy, tools -> tool, class stays."""
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _words(text: str) -> list[str]:
    """Content words in order: lowercase, 3 or more characters, not an index.STOPWORDS one."""
    from cortex.index import STOPWORDS
    return [w for w in _WORD.findall(text.lower()) if len(w) >= 3 and w not in STOPWORDS]


def _rarity(vault: Vault, words: list[str]) -> dict[str, float]:
    """log((N + 1) / (df + 0.5)) per folded word, N and df over the vault's indexed pages (the
    FTS table's document counts; private pages are not in it): a word few pages hold weighs
    more, one every page holds next to nothing. A throwaway index that never saves."""
    from cortex.catalog import Catalog
    from cortex.index import Index

    forms = sorted(set(words) | {_fold(w) for w in words})
    idx = Index(vault, Catalog.load(vault), save=False)
    try:
        n = idx.db.execute("SELECT count(*) FROM fts").fetchone()[0]
        idx.db.execute("CREATE VIRTUAL TABLE temp.vocab USING fts5vocab(main, fts, row)")
        df = dict(idx.db.execute("SELECT term, doc FROM temp.vocab WHERE term IN (SELECT value "
                                 "FROM json_each(?))", (json.dumps(forms),)))
    finally:
        idx.db.close()
    most: dict[str, int] = {}
    for w in words:
        f = _fold(w)
        most[f] = max(most.get(f, 0), df.get(w, 0), df.get(f, 0))
    return {f: math.log((n + 1) / (d + 0.5)) for f, d in most.items()}


def _note(vault: Vault, id: str):
    """The page a served id names (`me/x`: a me/ page), else None."""
    kinds, id = (("me",), id[3:]) if id.startswith("me/") else (Vault.KIND_DIRS, id)
    for kind in kinds:
        try:
            return vault.read(kind, id)
        except VaultError:
            continue
    return None


def _text(vault: Vault, id: str, lines: dict[str, str]) -> str:
    """What a served id said: a claim's line; a page's title, summary and first TRACE_BODY
    characters of its body; "" once it is gone."""
    if id.startswith("^"):
        return lines.get(id, "")
    note = _note(vault, id)
    if note is None:
        return ""
    f = note.fields
    return f"{f.get('title') or ''}\n{f.get('summary') or ''}\n{note.body[:TRACE_BODY]}"


def trace(vault: Vault, client: str, text: str) -> dict:
    """Which items Cortex served the app `client` (the view's `name`; "": none named) in its
    last TRACE_CALLS calls (any tool, resource reads included) a pasted AI reply `text` echoes,
    ranked here with no model or network: the summed rarity (`_rarity`) of the distinct content
    words (`_words`, plural-folded) an item's text (`_text`) shares with the reply. Up to
    TRACE_HITS scoring above 0, best first (the newer call first on a tie), each as `_items`
    shows its id plus its newest serving call's `tool` and `ts` and up to TRACE_WORDS shared
    `words` in the reply's order; a private page is matched but shows only as PRIVATE, with
    its call and no words, id or token. `calls`: the rows matched against.
    `text` is never written, logged or kept."""
    from cortex import claims

    key = client or "unknown"
    rs = [r for r in rows(vault) if str(r.get("client") or "unknown") == key][-TRACE_CALLS:]
    seen: dict[str, tuple[str, str]] = {}  # id -> (tool, ts) of the newest call serving it
    for r in rs:
        got = r.get("served")
        for ids in got.values() if isinstance(got, dict) else ():
            for i in ids if isinstance(ids, list) else ():
                if isinstance(i, str):
                    seen.pop(i, None)  # re-inserted: the newest call's last
                    seen[i] = (str(r.get("tool") or ""), str(r.get("ts") or ""))
    words = _words(text)
    if not seen or not words:
        return {"calls": len(rs), "hits": []}
    rare = _rarity(vault, words)
    order = list(dict.fromkeys(_fold(w) for w in words))
    form: dict[str, str] = {}
    for w in words:
        form.setdefault(_fold(w), w)
    lines = {claim(r["id"]): r["text"] for r in claims.rows(vault)}
    scored = []
    for i in reversed(seen):  # newest first; the stable sort keeps that order on a tie
        have = {_fold(w) for w in _words(_text(vault, i, lines))}
        shared = [f for f in order if f in have]
        if (score := sum(rare[f] for f in shared)) > 0:
            scored.append((score, i, shared))
    scored.sort(key=lambda x: -x[0])
    item, _, _ = _items(vault)
    hits = []
    for _, i, shared in scored[:TRACE_HITS]:
        x, (tool, ts) = item(i), seen[i]
        hits.append({**x, "tool": tool, "ts": ts,
                     "words": [form[f] for f in shared[:TRACE_WORDS]]} if "id" in x
                    else {"title": PRIVATE, "kind": x["kind"], "tool": tool, "ts": ts})
    return {"calls": len(rs), "hits": hits}


# ---- R27b (G5b-7): compare two apps -----------------------------------------------------------
def _gives(vault: Vault, scopes, id: str, lines: dict[str, dict]) -> bool:
    """May an MCP client with `scopes` be served `id`, by the rules the tools use: a claim by its
    section (access.scope_section), a me/ page whole (scope_page), a vault page by its kind
    (scope_kind) and a playbook by its own scope (scope_own), as the index's withhold applies
    them; no vault page for a core-only client (mcp_server.Cortex._core_only, asked)."""
    from types import SimpleNamespace

    from cortex import access
    from cortex.mcp_server import Cortex
    from cortex.review import PLAYBOOK_TAG

    if id.startswith("^"):
        r = lines.get(id)
        return r is not None and access.scope_section(scopes, r["page"], r["section"])
    if id.startswith("me/"):
        return access.scope_page(scopes, id[3:])
    note = _note(vault, id)
    if note is None or Cortex._core_only(SimpleNamespace(_scopes=lambda: scopes)):
        return False
    tags = note.fields.get("tags")
    book = note.kind == "note" and PLAYBOOK_TAG in (tags if isinstance(tags, list) else [tags])
    return access.scope_kind(scopes, note.kind) and (
        not book or access.scope_own(scopes, note.fields.get("scope")))


def _not_given(vault: Vault, scopes, full: dict | None, run: dict,
               lines: dict[str, dict]) -> list[str]:
    """The scopes (access.SCOPES order) a client with `scopes` lacks that would give it an item
    the run with every scope (`full`) serves and its own `run` does not: each scope that alone
    gives one back, or "all" when every one it lacks would (a core-only app's pages)."""
    from cortex.access import SCOPES

    if scopes is None or full is None:
        return []
    got = {i for d in (run["served"], run["trimmed"]) for ids in d.values() for i in ids}
    lacks = [s for s in SCOPES[1:] if s not in scopes]  # SCOPES[0] is "all"
    names: set[str] = set()
    for i in {i for ids in full["served"].values() for i in ids} - got:
        if _gives(vault, scopes, i, lines):
            continue  # its profile allows it: cut for space, or held for another reason
        back = [s for s in lacks if _gives(vault, scopes | {s}, i, lines)]
        names |= {"all"} if len(back) > 1 and len(back) == len(lacks) else set(back)
    return [s for s in SCOPES if s in names]


def compare(vault: Vault, prompt: str, a: str, b: str) -> dict:
    """What a new chat's cortex_prime would serve the apps named `a` and `b` (the view's
    `name`s) for `prompt` (mcp_server.dry_prime), each as `present` shows a call plus its
    `withheld` scopes (`_not_given`, against one run with every scope, made only when a profile
    limits either app). Nothing is written or logged, the prompt included."""
    from cortex import claims, profiles
    from cortex.mcp_server import dry_prime

    data = profiles.load(vault)
    runs = {n: dry_prime(vault, n, prompt) for n in (a, b)}
    scopes = {n: profiles.scopes_for(data, "" if n == "unknown" else n) for n in (a, b)}
    full = dry_prime(vault, "", prompt, every=True) if any(
        s is not None for s in scopes.values()) else None
    lines = {claim(r["id"]): r for r in claims.rows(vault)}
    _, blocks, _ = _items(vault)
    return {k: {"served": blocks(runs[n]["served"]), "trimmed": blocks(runs[n]["trimmed"]),
                "chars": runs[n]["chars"],
                "withheld": _not_given(vault, scopes[n], full, runs[n], lines)}
            for k, n in (("a", a), ("b", b))}
