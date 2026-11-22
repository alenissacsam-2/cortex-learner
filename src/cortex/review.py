"""Plan 5a: the Cortex app's Review view, read-only. What waits for the user's own click:

- pending: the lines under `## About me (unconfirmed)` in inbox.md (about-me lines and
  teaching-contract rules), each with Approve and Dismiss;
- expired: live claim lines past their own `(until …)` day (Retire);
- stale: confirmed lines not reconfirmed or rewritten in STALE_DAYS (Still true or Retire),
  at most STALE_PER_MONTH a month, oldest first;
- conflicts (plan 5b, D2): pairs of live lines that clash (Keep both, Keep first, Keep second):
  two lines at least as similar as `scorer`'s cut where exactly one says no (`negated`), both
  the owner's or both inferred. Everything else `settle` resolves itself: near-duplicates
  (D1), recorded `possible_update_of` / `supersedes` pairs and an owner's line against an
  inferred one (D2); mcp_server.Cortex._tidy retires the loser through the retire path;
- rules (plan 5b, N2): a correction the user typed in two or more imported chats, offered as a
  me/core line (cortex.corrections; Approve or Dismiss);
- decisions (RR-9): decision-like commit subjects of imported git repos
  (more_sources.decision_proposals); Approve makes a decision page, Dismiss drops it;
- forget (R3-5): a `//forget:` the user typed that matched no line or several, with its
  candidate lines (cortex.tags.forget_cards; Retire a line, or Dismiss);
- imports (RR-18): an imported page edited by hand whose file changed, so the re-import wrote
  a newer page beside it (cortex.sources.edited_pages; Dismiss);
- unsafe (G6): a live claim line that reads as an instruction to an AI or holds invisible
  characters (`unsafe_lines`); it is not served until Keep it (Keep it out retires it);
- crowded (G7): lines the X-ray shows cut for space in most of the latest primes that offered
  them (`crowded`; Keep pins it, Retire, Skip), and unused playbooks (G7): no X-ray call served
  them and their file is UNUSED_DAYS old (`unused`; Keep asks again in UNUSED_DAYS, Open);
  together at most STALE_PER_MONTH a month (`_spare_left`).

Similarity (`scorer`): cosine with the bge embedder when this process has it loaded already
(never loaded or downloaded here), else word overlap: the Jaccard of the crudely stemmed words,
stop words and negation words left out.

The app's handlers (app/server.py) write through `mcp_server.Cortex` with `app_clicks` set,
so every write is cortex_patch's or cortex_log's own code path."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import sys

from cortex import claims
from cortex.reflect import _PROPOSED, UNCONFIRMED, UNCONFIRMED_DAYS
from cortex.render import plain
from cortex.vault import is_private

STALE_DAYS = {"preferences": 180, "personality": 180, "profile": 365}
STALE_PER_MONTH = 3
# the pages a proposal may wait for: mcp_server.CONFIRM_PAGES and, while harvesting, every
# claim page; the order is the guess for an old line with no `me/<page>` tag
PAGES = (*claims.PAGES, "portrait", "teaching-contract")
SHOWN = 300  # characters of a line shown
_CONFIRMED = re.compile(r"\(confirmed (\d{4}-\d\d-\d\d)\)")
COS = 0.72  # cosine (warm embedder) from which two lines say about the same thing
JACCARD = 0.6  # the same for word overlap, when the embedder is not loaded
SHOWN_PAIRS = 3  # conflict pairs shown at a time (D2)
INFERRED = ("agent", "exposed")  # D1/D2: the tiers of a line a chat inferred (claims.tiers)
TIDY_MAX = 200  # D1/D2: automatic retires in one sweep
TODAY_MAX = 5  # R3-4: Today's 5, cards a day
TODAY_CARDS = ("pending", "conflict", "rule")  # the kinds of card Today's 5 offers
SIGHTINGS = 2  # chats that must propose the same line before it is offered in Today's 5
SINGLE_DAYS = 14  # a line fewer chats proposed leaves the view this many days after its stamp
# RR-10: what makes Review cards, as the Status line names them
PRODUCERS = {"chats": "Lines chats suggest", "imports": "Lines from imported chats",
             "corrections": "Corrections you repeat", "decisions": "Decisions in your commits",
             "forget": "//forget cards"}
PAUSE_AFTER = 20  # decided cards before a producer whose rejects exceed its accepts pauses
LEGACY_DISMISS = ("corrections", "decisions", "forget")  # a dismiss change's id: its producer
PRIMES = ("cortex_prime", "prime-hook")  # the X-ray rows that offer the Preferences lines
WINDOW = 40  # G7: the latest primes a crowded line is judged over
CROWD_MIN = 5  # primes that must have offered a line before it counts as crowded out
UNUSED_DAYS = 60  # G7: a playbook's file this old and served by no X-ray call; Keep's quiet days
SPARE_CARDS = ("crowded", "unused")  # the card stamps that spend the G7 monthly cap
PLAYBOOK_TAG = "playbook"  # mcp_server.PLAYBOOK_TAG (it imports this module)
_WORD = re.compile(r"[a-z0-9']+")
_STOP = frozenset("a an the i me my mine to of and or in on at for with is are be am it its "
                  "this that so as by from you your".split())
# a line "says no" with one of these or any word ending n't: never/always, don't/do, avoid/prefer
_NEG = frozenset("no not never none nothing nor neither without avoid avoids avoiding stop "
                 "hate hates dislike dislikes dont cannot".split())
_VECS: dict = {}  # normalised line -> unit vector, from the embedder in _VECS_OF[0]
_VECS_OF: list = [None]


def _words(text: str) -> list[str]:
    return _WORD.findall(claims.norm(text).replace("’", "'"))


def negated(text: str) -> bool:
    """Does the line say no (a `_NEG` word, or a word ending n't)?"""
    return any(w in _NEG or w.endswith("n't") for w in _words(text))


def _stem(w: str) -> str:
    """shortcut: a crude suffix strip (using, uses, used, use -> us); a real stemmer if pairs
    are missed."""
    if len(w) > 4:
        w = re.sub(r"(?:ing|ed|es|s)$", "", w)
    return w.rstrip("e") if len(w) > 2 else w


def _bag(text: str) -> frozenset:
    return frozenset(_stem(w) for w in _words(text)
                     if w not in _STOP and w not in _NEG and not w.endswith("n't"))


def apart(a: str, b: str) -> bool:
    """Each line names something the other does not ("reads news" / "reads papers", "uses
    Python" / "uses Rust"): two facts however close they score, so Cortex never retires one
    for the other by itself (22 Nov QA: the sweep retired "Reads news every morning")."""
    x, y = _bag(a), _bag(b)
    return bool(x - y) and bool(y - x)


def _warm(texts: list[str]) -> list | None:
    """Unit vectors of `texts` from the bge embedder, only if this process has it loaded
    already: never imports cortex.semantic or loads a model. None when not warm or it fails."""
    sem = sys.modules.get("cortex.semantic")
    embed = getattr(sem, "_embedder", None)
    if embed is None:
        return None
    try:
        if _VECS_OF[0] is not embed or len(_VECS) > 20000:
            _VECS.clear()
            _VECS_OF[0] = embed
        if new := [t for t in dict.fromkeys(texts) if t not in _VECS]:
            _VECS.update(zip(new, sem._embed(embed, new), strict=True))
        return [_VECS[t] for t in texts]
    except Exception:  # a model error must never cost the write or the view: words serve
        return None


def scorer(texts: list[str], loose: bool = False):
    """(score(i, j), cut) over `texts`: cosine and COS with a warm embedder, else the Jaccard
    of `_bag`s and JACCARD. A pair scoring at least `cut` says about the same thing; an
    `apart` pair scores 0 unless `loose` (`collision`'s guess, which the agent settles)."""
    bags = [_bag(t) for t in texts]

    def off(i: int, j: int) -> bool:
        return not loose and bool(bags[i] - bags[j]) and bool(bags[j] - bags[i])

    vecs = _warm([claims.norm(t) for t in texts])
    if vecs is not None:
        return (lambda i, j: 0.0 if off(i, j) else float(vecs[i] @ vecs[j])), COS
    return (lambda i, j: 0.0 if off(i, j)
            else len(bags[i] & bags[j]) / (len(bags[i] | bags[j]) or 1)), JACCARD


def collision(vault, page: str, text: str, today: str) -> dict | None:
    """The live line of a non-private claim page (on a tie, `page`'s own) that `text`, a line
    about to be appended to me/<page>, most likely updates: {page, id, text (raw note text),
    apart}, or None under the cut (`scorer`, loose). An `apart` guess is left to the agent:
    `settle` never retires either line for it."""
    hidden = _private(vault)
    live = [r for r in claims.rows(vault) if claims.is_live(r, today) and r["page"] not in hidden]
    if not live or not claims.norm(text):
        return None
    score, cut = scorer([text, *(r["text"] for r in live)], loose=True)
    k = max(range(len(live)), key=lambda k: (score(0, k + 1), live[k]["page"] == page))
    if score(0, k + 1) < cut:
        return None
    return {"page": live[k]["page"], "id": live[k]["id"], "text": live[k]["text"],
            "apart": apart(text, live[k]["text"])}


def pair_key(a: dict, b: dict) -> str:
    """A conflict pair's key: both `page/id`, sorted, so either order is the same pair."""
    return claims.pair_key((a["page"], a["id"]), (b["page"], b["id"]))


def owned(r: dict) -> bool:
    """D1/D2: the owner's own line (typed, legacy, or confirmed/approved), not a chat's
    inference (`INFERRED`)."""
    return r.get("tier") not in INFERRED


def rank(r: dict) -> tuple:
    """D1: of near-duplicate lines the highest stays: the owner's over an inferred one, then
    the newer (its tier's day), then the longer, then the later line on its page."""
    return (owned(r), str(r.get("day") or ""), len(claims.norm(r["text"])), r.get("line_no", 0))


def known_texts(vault) -> dict:
    """(page, id) -> text of every claim line, any status: what `kept_cover` compares with."""
    return {(r["page"], r["id"]): r["text"] for r in claims.rows(vault)}


def kept_cover(rows: list[dict], events, known: dict | None = None):
    """D5: (covered(i, j), score, cut, no) over `rows`: covered when the user kept both of
    that pair (a `keep_both` change) or of a pair whose two lines are each near-duplicates of
    the two kept ones (`known`: `known_texts`)."""
    _, kept = claims.recorded_pairs(events)
    known = known or {}
    sides = [[tuple(s.split("/", 1)) for s in k.split("|")] for k in kept]
    sides = [s for s in sides if len(s) == 2 and all(x in known for x in s)]
    texts = [r["text"] for r in rows] + [known[x] for s in sides for x in s]
    score, cut = scorer(texts)
    no = [negated(t) for t in texts]
    n = len(rows)

    def near(i: int, k: int) -> bool:
        return no[i] == no[k] and score(i, k) >= cut

    def covered(i: int, j: int) -> bool:
        if pair_key(rows[i], rows[j]) in kept:
            return True
        return any((near(i, a) and near(j, a + 1)) or (near(i, a + 1) and near(j, a))
                   for a in range(n, len(texts), 2))

    return covered, score, cut, no


def settle(rows: list[dict], events, known: dict | None = None, skip=frozenset()) -> dict:
    """D1, D2, D5 over `rows` (tiered live claim rows, none private): {retire: [{retire, keep,
    why}] with raw rows, cards: [{key, why: "opposite", left, right}]}.
    - a recorded pair (claims.recorded_pairs) whose lines agree: the newer stays, unless the
      older is the owner's and the newer inferred (why `update` or `supersedes`); a guessed
      `update` pair that is `apart` is left alone;
    - lines on one page that say nearly the same (`scorer` cut, same `negated`): the one
      `rank` puts first stays (why `duplicate`), so n near-copies are one line;
    - two of the lines left, as similar but exactly one negated (or a recorded pair that
      is): the owner's stays over an inferred one (why `opposite`); two of the owner's or two
      inferred are a card, one per pair of near-copy groups.
    A pair the user kept both of (`kept_cover`) or whose key is in `skip` is left alone, and
    so is a line whose `page/id` is in `skip` (an automatic retire the user undid)."""
    events = list(events)
    covered, score, cut, no = kept_cover(rows, events, known)
    recorded, _ = claims.recorded_pairs(events)
    at = {(r["page"], r["id"]): i for i, r in enumerate(rows)}
    out, gone, forced = [], set(), []

    def free(i: int, j: int) -> bool:
        return pair_key(rows[i], rows[j]) not in skip and not covered(i, j)

    def kept(i: int) -> bool:  # the user undid Cortex retiring this line: never again
        return f"{rows[i]['page']}/{rows[i]['id']}" in skip

    def drop(i: int, k: int, why: str) -> None:
        out.append({"retire": rows[i], "keep": rows[k], "why": why})
        gone.add(i)

    for old, new, why in recorded.values():
        o, n = at.get(old), at.get(new)
        if o is None or n is None or o in gone or n in gone or not free(o, n):
            continue
        if why == "update" and apart(rows[o]["text"], rows[n]["text"]):
            continue  # a guessed pair of two facts: both stay (the agent may retire one)
        if no[o] != no[n]:
            forced.append((o, n))
        elif owned(rows[o]) and not owned(rows[n]):
            if not kept(n):
                drop(n, o, why)
        elif not kept(o):
            drop(o, n, why)
    rep: dict[int, int] = {}  # a line -> the kept line it near-duplicates
    keep: list[int] = []
    for i in sorted((i for i in range(len(rows)) if i not in gone),
                    key=lambda i: rank(rows[i]), reverse=True):
        k = next((k for k in keep if rows[k]["page"] == rows[i]["page"] and no[k] == no[i]
                  and score(i, k) >= cut and free(i, k)), None)
        if k is None or kept(i):
            keep.append(i)
        else:
            rep[i] = k
            drop(i, k, "duplicate")
    keep.sort()
    pairs = [(rep.get(a, a), rep.get(b, b)) for a, b in forced]
    pairs += [(a, b) for x, a in enumerate(keep) for b in keep[x + 1:]
              if no[a] != no[b] and score(a, b) >= cut]
    cards, seen = [], set()
    for a, b in pairs:
        if a == b or a in gone or b in gone or frozenset((a, b)) in seen or not free(a, b):
            continue
        seen.add(frozenset((a, b)))
        if owned(rows[a]) != owned(rows[b]):
            lose, win = (b, a) if owned(rows[a]) else (a, b)
            if not kept(lose):
                drop(lose, win, "opposite")
        else:
            cards.append((a, b))
    return {"retire": out, "cards": [
        {"key": pair_key(rows[a], rows[b]), "why": "opposite", "left": rows[a], "right": rows[b]}
        for a, b in cards if a not in gone and b not in gone]}


def conflicts(rows: list[dict], events: list[dict], known: dict | None = None) -> list[dict]:
    """D2: up to SHOWN_PAIRS pairs of `rows` (live, shown claim rows) that clash and Cortex
    does not settle itself (`settle`'s cards: an opposite pair of two of the owner's lines or
    two inferred ones), as {key, why, left, right} with raw rows; Keep first or second retires
    the other line, Keep both covers near-copies of the pair too (`kept_cover`)."""
    return settle(rows, events, known)["cards"][:SHOWN_PAIRS]


def copies(rows: list[dict], r: dict) -> list[dict]:
    """D5: `r` and the `rows` on its page that say nearly the same (the `scorer` cut, same
    `negated`), `r` first: what Keep first or second on a conflict card retires."""
    same = [x for x in rows if x["page"] == r["page"] and x["id"] != r["id"]
            and negated(x["text"]) == negated(r["text"])]
    score, cut = scorer([r["text"], *(x["text"] for x in same)])
    return [r, *(x for i, x in enumerate(same, 1) if score(0, i) >= cut)]


def collapse(items: list[dict], text=lambda x: x["text"]) -> list[dict]:
    """D5: near-identical `items` (the `scorer` cut, same `negated`) as one: the first of each
    group, with `members` (the group, itself first). A click on it covers every member."""
    if not items:
        return []
    texts = [text(x) for x in items]
    score, cut = scorer(texts)
    no = [negated(t) for t in texts]
    groups: list[list[int]] = []
    for i in range(len(items)):
        g = next((g for g in groups if no[g[0]] == no[i] and score(g[0], i) >= cut), None)
        if g is None:
            groups.append([i])
        else:
            g.append(i)
    return [{**items[g[0]], "members": [items[i] for i in g]} for g in groups]


def group(items: list[dict], want, field: str = "key", text=lambda x: x["text"]) -> list:
    """D5: the `field` values of the `collapse` group of `items` holding the item whose `field`
    is `want`, that one first; [want] when none holds it."""
    for g in collapse(items, text):
        got = [m[field] for m in g["members"]]
        if want in got:
            return [want, *(x for x in got if x != want)]
    return [want]


def _spare_left(events, today: str) -> int:
    """G7: how many crowded and unused cards this month still has: STALE_PER_MONTH minus the
    clicks this month stamped with a SPARE_CARDS card (one per click, its session)."""
    voided, said = _voided(events), set()
    for e in events:
        c = _payload(e).get("change") if e.get("kind") == "change" else None
        if (isinstance(c, dict) and c.get("card") in SPARE_CARDS and e.get("id") not in voided
                and str(e.get("day") or "")[:7] == today[:7]):
            said.add(e.get("session"))
    return max(0, STALE_PER_MONTH - len(said))


def _ids(blocks) -> set[str]:
    """The ids of an X-ray row's `served` or `trimmed` {block: [ids]}, whatever its shape."""
    return {i for v in (blocks.values() if isinstance(blocks, dict) else ())
            for i in (v if isinstance(v, list) else ()) if isinstance(i, str)}


def crowded(vault, rows: list[dict]) -> tuple[list[dict], int]:
    """G7: (the `rows` (live, shown claim rows) the X-ray shows cut for space in most of the
    latest WINDOW primes that offered them, at least CROWD_MIN, as {page, id, text (raw), cut,
    seen} most cut first; how many primes the window holds). A pinned line (Keep pins it) or a
    muted one is left out."""
    from collections import Counter

    from cortex import xray
    primes = [r for r in xray.rows(vault) if r.get("tool") in PRIMES][-WINDOW:]
    seen, cut = Counter(), Counter()
    for r in primes:
        s, t = _ids(r.get("served")), _ids(r.get("trimmed"))
        seen.update(s | t)
        cut.update(t - s)
    xs = xray.settings(vault)
    flagged = xs["pin"] | xs["mute"]
    out = [{"page": r["page"], "id": r["id"], "text": r["text"], "cut": cut[c], "seen": seen[c]}
           for r in rows if (c := xray.claim(r["id"])) not in flagged
           and seen[c] >= CROWD_MIN and 2 * cut[c] > seen[c]]
    out.sort(key=lambda x: (-x["cut"] / x["seen"], x["page"], x["id"]))
    return out, len(primes)


def unused(vault, events, today: str) -> tuple[list[dict], int, str | None]:
    """G7: (the playbooks (`note` pages tagged PLAYBOOK_TAG, none private or muted) that no
    X-ray call served and whose file is over UNUSED_DAYS old, not kept (a `dismiss` change on
    id `playbooks` holding the id's hash) in the last UNUSED_DAYS, oldest first, as {id, title,
    path (vault-relative), changed}; the X-ray calls seen; the first one's day). Nothing while
    the X-ray holds no call."""
    from cortex import xray
    calls = xray.rows(vault)
    if not calls:
        return [], 0, None
    served = {i for r in calls for i in _ids(r.get("served"))}
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=UNUSED_DAYS)).isoformat()
    voided, kept = _voided(events), set()
    for e in events:
        c = _payload(e).get("change") if e.get("kind") == "change" else None
        if (isinstance(c, dict) and c.get("tool") == "dismiss" and c.get("id") == "playbooks"
                and e.get("id") not in voided and str(e.get("day") or "") > since):
            kept.add(c.get("hash"))
    mute = xray.settings(vault)["mute"]
    out = []
    for n in vault.iter_notes("note"):
        tags = n.fields.get("tags")
        if (PLAYBOOK_TAG not in (tags if isinstance(tags, list) else [tags]) or is_private(n)
                or n.id in mute or n.id in served or text_hash(n.id) in kept):
            continue
        changed = dt.date.fromtimestamp(n.path.stat().st_mtime).isoformat()
        if changed < since:
            out.append({"id": n.id, "title": plain(str(n.fields.get("title") or n.id), SHOWN),
                        "path": n.path.relative_to(vault.root).as_posix(), "changed": changed})
    out.sort(key=lambda x: (x["changed"], x["id"]))
    try:  # the oldest call's Cortex day
        from cortex.events import local_day
        first = local_day(dt.datetime.fromisoformat(str(calls[0].get("ts"))))
    except (TypeError, ValueError):
        first = None
    return out, len(calls), first


def text_hash(text: str) -> str:
    """A dismissed line's mark in its `dismiss` change event: never the text itself."""
    return hashlib.sha256(claims.norm(text).encode()).hexdigest()[:16]


def _voided(events: list[dict]) -> set:
    return {e.get("ref") for e in events if e.get("kind") == "void"}


def _payload(e: dict) -> dict:
    return e["payload"] if isinstance(e.get("payload"), dict) else {}


def dismissed(events, page: str, text: str) -> bool:
    """True when Dismiss in the app dropped `text` for me/<page> (a `dismiss` change event)."""
    events, h = list(events), text_hash(text)
    voided = _voided(events)
    for e in events:
        c = _payload(e).get("change") if e.get("kind") == "change" else None
        if (isinstance(c, dict) and c.get("tool") == "dismiss" and c.get("id") == page
                and c.get("hash") == h and e.get("id") not in voided):
            return True
    return False


def unsafe_lines(vault, events) -> list[dict]:
    """G6: the live claim lines (private pages too) whose text or section claims.unsafe flags
    and Keep has not kept, as {key, page, id, section, text (raw), what}. Keep is a `dismiss`
    change on id `unsafe` holding the key's hash (`dismissed`); the key hashes the page and the
    exact line, so a line edited since is checked again."""
    events = list(events)
    voided, kept = _voided(events), set()
    for e in events:
        c = _payload(e).get("change") if e.get("kind") == "change" else None
        if (isinstance(c, dict) and c.get("tool") == "dismiss" and c.get("id") == "unsafe"
                and e.get("id") not in voided):
            kept.add(c.get("hash"))
    out = []
    for r in claims.rows(vault):
        if r["status"] != "live" or not (what := claims.unsafe(r["text"])
                                         or claims.unsafe(r["section"])):
            continue
        key = hashlib.sha256(f"{r['page']}\n{r['section']}\n{r['text']}".encode()).hexdigest()[:16]
        if text_hash(key) not in kept:
            out.append({"key": key, "page": r["page"], "id": r["id"], "section": r["section"],
                        "text": r["text"], "what": what})
    return out


def _private(vault) -> set[str]:
    return {n.id for n in vault.iter_notes("me") if is_private(n)}


def _page_of(vault, section: str) -> str:
    """An old line with no `me/<page>` tag: the first page with its `## section`, a YYYY-MM
    section the portrait, else preferences."""
    if re.fullmatch(r"\d{4}-\d\d", section):
        return "portrait"
    want = f"## {section}".casefold()
    for page in PAGES:
        p = vault.root / "me" / f"{page}.md"
        if p.is_file() and want in (ln.strip().casefold()
                                    for ln in p.read_bytes().decode("utf-8").splitlines()):
            return page
    return "preferences"


def pending(vault, today: str) -> list[dict]:
    """The waiting lines as {key, page, section, text, since} with raw note text, one per
    (page, section, text) with its latest stamp. Left out as reflect.unconfirmed leaves them
    (stamped over UNCONFIRMED_DAYS ago), and lines with no `<section>: ` or for a private page."""
    p = vault.root / "inbox.md"
    cut = (dt.date.fromisoformat(today) - dt.timedelta(days=UNCONFIRMED_DAYS)).isoformat()
    hidden, out, on = _private(vault), {}, False
    for ln in p.read_bytes().decode("utf-8").splitlines() if p.is_file() else []:
        if ln.startswith("#"):
            on = ln.strip() == UNCONFIRMED
            continue
        if not on or not ln.startswith("- "):
            continue
        m = _PROPOSED.search(ln)
        section, sep, text = _PROPOSED.sub("", ln)[2:].strip().partition(": ")
        section, text, since = section.strip(), text.strip(), m.group(1) if m else None
        if not (sep and section and text) or (since and since < cut):
            continue
        page = (m and m.group(2)) or _page_of(vault, section)
        if page not in PAGES or page in hidden:
            continue
        key = hashlib.sha256(f"{page}\n{claims.norm(section)}\n{claims.norm(text)}".encode()
                             ).hexdigest()[:16]
        if key not in out or (since or "") > (out[key]["since"] or ""):
            out[key] = {"key": key, "page": page, "section": section, "text": text,
                        "since": since}
    return list(out.values())


def _shown(text: str) -> str:
    """Note text for the page: no `(confirmed …)`-style markers, links, images or HTML."""
    return plain(claims._MARK.sub("", text), SHOWN)


def stale(rows: list[dict], events: list[dict], today: str) -> list[dict]:
    """Live confirmed lines on the STALE_DAYS pages whose last confirm (a `(confirmed DAY)`
    marker, the confirming change event or a yes reconfirm) is older than the page's days,
    oldest first; at most STALE_PER_MONTH minus this month's Review answers (clicked
    reconfirms and retires; shortcut: an expired line retired in the Review counts too). D5:
    near-identical lines are one card, its `ids` every member's (Still true or Retire covers
    them all)."""
    voided, said, answered = _voided(events), {}, 0
    for e in events:
        if e.get("id") in voided:
            continue
        pl, day = _payload(e), str(e.get("day") or "")
        this_month = day[:7] == today[:7]
        if e.get("kind") == "log" and pl.get("kind") == "reconfirm":
            if pl.get("verdict") == "yes":
                said[pl.get("claim")] = max(said.get(pl.get("claim"), ""), day)
            answered += this_month and pl.get("confirm") == "clicked"
        c = pl.get("change") if e.get("kind") == "change" else None
        if isinstance(c, dict) and c.get("op") == "retire" and c.get("confirm") == "clicked":
            answered += this_month
    out = []
    for r in rows:
        days = STALE_DAYS.get(r["page"])
        if days is None or r["tier"] not in ("confirmed", "legacy", "typed") or claims.expired(
                r["text"], today):
            continue
        last = max([*_CONFIRMED.findall(r["text"]), said.get(r["id"], ""),
                    str(r["day"] or "") if r["tier"] == "confirmed" else ""])
        if last and last < (dt.date.fromisoformat(today) - dt.timedelta(days=days)).isoformat():
            out.append({"page": r["page"], "id": r["id"], "text": _shown(r["text"]),
                        "since": last})
    out.sort(key=lambda x: (x["since"], x["page"], x["id"]))
    out = [{**{k: v for k, v in g.items() if k != "members"},
            "ids": [m["id"] for m in g["members"]]} for g in collapse(out)]
    return out[:max(0, STALE_PER_MONTH - answered)]


def live_rows(vault, events, today: str) -> list[dict]:
    """The tiered live claim rows the Review works on: none private, none past its until."""
    hidden = _private(vault)
    return [r for r in claims.tiers(vault, events, today=today) if r["page"] not in hidden]


def _proposals(events) -> dict:
    """{(page, text_hash): {sessions, producer}} of the unvoided `propose` changes that hold
    their line's hash; `producer` (RR-10) is the latest one's, `chats` when it names none."""
    events = list(events)
    voided, out = _voided(events), {}
    for e in events:
        c = _payload(e).get("change") if e.get("kind") == "change" else None
        if (isinstance(c, dict) and c.get("tool") == "propose" and isinstance(c.get("hash"), str)
                and e.get("id") not in voided):
            x = out.setdefault((c.get("id"), c["hash"]), {"sessions": set()})
            x["sessions"].add(e.get("session"))
            x["producer"] = c.get("producer") if c.get("producer") in PRODUCERS else "chats"
    return out


def sightings(events) -> dict:
    """R3-4: {(page, text_hash): sessions}: the chats (one session each) that proposed the
    same line."""
    return {k: v["sessions"] for k, v in _proposals(events).items()}


def producer_of(proposals: dict, page: str, text: str) -> str:
    """RR-10: which producer proposed a waiting line (`_proposals`); an old line: `chats`."""
    return proposals.get((page, text_hash(text)), {}).get("producer", "chats")


def ledger(events) -> dict:
    """RR-10: {producer: {accept, reject, paused}} over the Review clicks on each producer's
    cards since its latest `resume` change: a click's changes carry the `producer` the app
    stamped (one verdict per click, its session); before that stamp, a commit decision's
    `decide` and a `dismiss` of corrections, decisions or forget still count. A producer whose
    rejects exceed its accepts over PAUSE_AFTER or more decided cards is `paused`."""
    events = list(events)
    voided, said = _voided(events), {p: {} for p in PRODUCERS}
    for e in events:
        c = _payload(e).get("change") if e.get("kind") == "change" else None
        if not isinstance(c, dict) or e.get("id") in voided or c.get("confirm") != "clicked":
            continue
        if c.get("tool") == "resume":
            if c.get("producer") in PRODUCERS:
                said[c["producer"]] = {}  # counting starts again
            continue
        p = c.get("producer")
        if p not in PRODUCERS:  # a click from before the stamp
            p = ("decisions" if c.get("tool") == "decide" else c.get("id")
                 if c.get("tool") == "dismiss" and c.get("id") in LEGACY_DISMISS else None)
        if p is None:
            continue
        no = c.get("tool") == "dismiss" or c.get("op") == "reject"
        said[p][e.get("session") or e.get("id")] = "reject" if no else "accept"
    out = {}
    for p, votes in said.items():
        n = {v: list(votes.values()).count(v) for v in ("accept", "reject")}
        out[p] = {**n, "paused": n["accept"] + n["reject"] >= PAUSE_AFTER
                  and n["reject"] > n["accept"]}
    return out


def answered_today(events, today: str) -> int:
    """R3-4: the Today's 5 kinds of card (TODAY_CARDS) the user answered on `today`: one per
    app click, each its own session stamped with the card's kind (app/server.py `_click`)."""
    voided, said = _voided(events), set()
    for e in events:
        c = _payload(e).get("change") if e.get("kind") == "change" else None
        if (isinstance(c, dict) and c.get("card") in TODAY_CARDS and e.get("day") == today
                and e.get("id") not in voided and not c.get("auto")):  # D1/D2: not a click
            said.add(e.get("session"))
    return len(said)


def view(vault, events, today: str) -> dict:
    """The Review view: every text is shown text (`_shown`). D4: the badge `count` is the
    Today's 5 cards still unanswered; `more` counts every board's cards. R3-4: a
    single inference (a waiting line other than a teaching rule that fewer than SIGHTINGS
    chats proposed) leaves the view SINGLE_DAYS after its stamp (inbox.md keeps it), and
    `today` orders at most TODAY_MAX cards a day (`_today`). RR-10: a `paused` producer's
    cards (`ledger`) are not offered; `paused` lists them for the Status line."""
    events = list(events)
    led = ledger(events)
    off = {p for p, x in led.items() if x["paused"]}
    hidden = _private(vault)
    rows = [r for r in claims.tiers(vault, events) if r["page"] not in hidden]  # writes nothing
    expired = [{"page": r["page"], "id": r["id"], "text": _shown(r["text"]), "until": until}
               for r in rows if (until := claims.expired(r["text"], today))]
    seen, cut = _proposals(events), (dt.date.fromisoformat(today)
                                     - dt.timedelta(days=SINGLE_DAYS)).isoformat()

    def single(p: dict) -> bool:
        return p["page"] != "teaching-contract" and len(
            seen.get((p["page"], text_hash(p["text"])), {}).get("sessions", ())) < SIGHTINGS

    waiting = [{**p, "section": plain(p["section"], 80), "text": _shown(p["text"]),
                "single": single(p)}
               for p in pending(vault, today) if not (single(p) and (p["since"] or "~") < cut)
               and producer_of(seen, p["page"], p["text"]) not in off]
    waiting = [{**{k: v for k, v in g.items() if k != "members"},
                "keys": [m["key"] for m in g["members"]]}  # D5: one card per near-copy group
               for page in dict.fromkeys(p["page"] for p in waiting)
               for g in collapse([p for p in waiting if p["page"] == page])]
    old = stale(rows, events, today)

    def side(r: dict) -> dict:
        return {"page": r["page"], "id": r["id"], "text": _shown(r["text"])}

    current = [r for r in rows if not claims.expired(r["text"], today)]  # = live_rows
    pairs = [{"key": c["key"], "why": c["why"], "left": side(c["left"]),
              "right": side(c["right"])} for c in conflicts(current, events, known_texts(vault))]
    from cortex import corrections, more_sources  # read imported pages: only the Review asks
    rules = [{"key": x["key"], "text": _shown(x["text"]), "chats": x["chats"],
              "page": x["page"]}
             for x in ([] if "corrections" in off else corrections.proposals(vault, events,
                                                                              current))]
    decisions = [{**x, "project": plain(x["project"], 80), "subject": plain(x["subject"], SHOWN)}
                 for x in ([] if "decisions" in off
                           else more_sources.decision_proposals(vault, events))]
    from cortex import tags  # it imports this module
    forget = [{"key": g["key"], "keys": [m["key"] for m in g["members"]],
               "text": plain(g["text"], SHOWN),
               "candidates": list({c["id"]: side(c) for m in g["members"]
                                   for c in m["candidates"]}.values())}
              for g in collapse([] if "forget" in off else tags.forget_cards(vault, events,
                                                                             today))]
    from cortex import sources
    imports = [{**x, "title": plain(x["title"], SHOWN)}
               for x in sources.edited_pages(vault, events)]
    unsafe = [{"key": g["key"], "page": g["page"], "id": g["id"], "what": g["what"],
               "keys": [m["key"] for m in g["members"]], "ids": [m["id"] for m in g["members"]],
               "invisible": claims.invisible(g["what"]),
               "text": _shown("".join(c for c in g["text"] if c.isprintable()))}
              for g in collapse([x for x in unsafe_lines(vault, events)
                                 if x["page"] not in hidden])]
    spare = _spare_left(events, today)  # G7: crowded first, then unused, within the month's cap
    lines, window = crowded(vault, current) if spare else ([], 0)
    crowd = [{**side(x), "cut": x["cut"], "seen": x["seen"], "window": window}
             for x in lines[:spare]]
    books, calls, since = unused(vault, events, today) if spare > len(crowd) else ([], 0, None)
    books = [{**x, "calls": calls, "since": since} for x in books[:spare - len(crowd)]]
    left = max(0, TODAY_MAX - answered_today(events, today))
    first = [{"type": "unsafe", "key": x["key"]} for x in unsafe]  # G6: always asked, first
    today_ = first + _today(waiting, pairs, rules)[:max(0, left - len(first))]
    return {"pending": waiting, "expired": expired, "stale": old, "conflicts": pairs,
            "rules": rules, "decisions": decisions, "forget": forget, "imports": imports,
            "unsafe": unsafe, "crowded": crowd, "unused": books,
            "today": today_, "today_left": left,
            "paused": [{"producer": p, "label": PRODUCERS[p], **led[p]} for p in sorted(off)],
            "count": len(today_),  # D4: the badge counts only Today's 5 still unanswered
            "more": len(waiting) + len(expired) + len(old) + len(pairs) + len(rules)
            + len(decisions) + len(forget) + len(imports) + len(unsafe) + len(crowd)
            + len(books)}


def _today(waiting: list[dict], pairs: list[dict], rules: list[dict]) -> list[dict]:
    """R3-4's order, as {type, key} of cards in the boards (after G6's unsafe lines, which
    `view` puts first): teaching-rule proposals, then contradiction pairs, repeated
    corrections, and last the other waiting lines that 2+ chats proposed (a single inference
    never)."""
    rule = [p for p in waiting if p["page"] == "teaching-contract"]
    return ([{"type": "pending", "key": p["key"]} for p in rule]
            + [{"type": "conflict", "key": c["key"]} for c in pairs]
            + [{"type": "rule", "key": x["key"]} for x in rules]
            + [{"type": "pending", "key": p["key"]} for p in waiting
               if p not in rule and not p["single"]])
