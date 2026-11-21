"""Plan 5a: the Cortex app's Review view, read-only. What waits for the user's own click:

- pending: the lines under `## About me (unconfirmed)` in inbox.md (about-me lines and
  teaching-contract rules), each with Approve and Dismiss;
- expired: live claim lines past their own `(until …)` day (Retire);
- stale: confirmed lines not reconfirmed or rewritten in STALE_DAYS (Still true or Retire),
  at most STALE_PER_MONTH a month, oldest first;
- conflicts (plan 5b): pairs of live lines that likely clash (Keep both, Keep left, Keep
  right): every `possible_update_of` or `supersedes` (RR-4) pair cortex_patch recorded and not
  yet resolved, and any
  two lines at least as similar as `scorer`'s cut where exactly one says no (`negated`);
- rules (plan 5b, N2): a correction the user typed in two or more imported chats, offered as a
  me/core line (cortex.corrections; Approve or Dismiss);
- decisions (RR-9): decision-like commit subjects of imported git repos
  (more_sources.decision_proposals); Approve makes a decision page, Dismiss drops it;
- forget (R3-5): a `//forget:` the user typed that matched no line or several, with its
  candidate lines (cortex.tags.forget_cards; Retire a line, or Dismiss);
- imports (RR-18): an imported page edited by hand whose file changed, so the re-import wrote
  a newer page beside it (cortex.sources.edited_pages; Dismiss);
- unsafe (G6): a live claim line that reads as an instruction to an AI or holds invisible
  characters (`unsafe_lines`); it is not served until Keep it (Keep it out retires it).

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
SHOWN_PAIRS = 5  # conflict pairs shown at a time
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


def scorer(texts: list[str]):
    """(score(i, j), cut) over `texts`: cosine and COS with a warm embedder, else the Jaccard
    of `_bag`s and JACCARD. A pair scoring at least `cut` says about the same thing."""
    vecs = _warm([claims.norm(t) for t in texts])
    if vecs is not None:
        return (lambda i, j: float(vecs[i] @ vecs[j])), COS
    bags = [_bag(t) for t in texts]
    return (lambda i, j: len(bags[i] & bags[j]) / (len(bags[i] | bags[j]) or 1)), JACCARD


def collision(vault, page: str, text: str, today: str) -> dict | None:
    """The live line of a non-private claim page (on a tie, `page`'s own) that `text`, a line
    about to be appended to me/<page>, most likely updates: {page, id, text (raw note text)},
    or None under the cut (`scorer`)."""
    hidden = _private(vault)
    live = [r for r in claims.rows(vault) if claims.is_live(r, today) and r["page"] not in hidden]
    if not live or not claims.norm(text):
        return None
    score, cut = scorer([text, *(r["text"] for r in live)])
    k = max(range(len(live)), key=lambda k: (score(0, k + 1), live[k]["page"] == page))
    if score(0, k + 1) < cut:
        return None
    return {"page": live[k]["page"], "id": live[k]["id"], "text": live[k]["text"]}


def pair_key(a: dict, b: dict) -> str:
    """A conflict pair's key: both `page/id`, sorted, so either order is the same pair."""
    return claims.pair_key((a["page"], a["id"]), (b["page"], b["id"]))


def conflicts(rows: list[dict], events: list[dict]) -> list[dict]:
    """Up to SHOWN_PAIRS pairs of `rows` (live, shown claim rows) that likely clash, as
    {key, why, left, right} with raw rows: recorded pairs first (claims.recorded_pairs: why
    `update` for a `possible_update_of`, `supersedes` for RR-4's explicit one; left the old
    line), then pairs at least as similar as the cut with exactly one `negated` line (why
    `opposite`). A pair the user kept both of (a `keep_both` change) is left out; Keep left or
    right retires the other line, so the pair is gone."""
    recorded, kept = claims.recorded_pairs(events)
    by, out = {(r["page"], r["id"]): r for r in rows}, {}
    for key, (old, new, why) in recorded.items():
        if (o := by.get(old)) and (n := by.get(new)):
            out[key] = {"why": why, "left": o, "right": n}
    score, cut = scorer([r["text"] for r in rows])
    no = [negated(r["text"]) for r in rows]
    for i in range(len(rows)):
        if len(out) >= SHOWN_PAIRS + len(kept):  # enough to fill the list after `kept`
            break
        for j in range(i + 1, len(rows)):
            if no[i] != no[j] and score(i, j) >= cut:
                out.setdefault(pair_key(rows[i], rows[j]),
                               {"why": "opposite", "left": rows[i], "right": rows[j]})
    return [{"key": k, **v} for k, v in out.items() if k not in kept][:SHOWN_PAIRS]


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
    reconfirms and retires; shortcut: an expired line retired in the Review counts too)."""
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
                and e.get("id") not in voided):
            said.add(e.get("session"))
    return len(said)


def view(vault, events, today: str) -> dict:
    """The Review view and its badge `count`: every text is shown text (`_shown`). R3-4: a
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
    old = stale(rows, events, today)

    def side(r: dict) -> dict:
        return {"page": r["page"], "id": r["id"], "text": _shown(r["text"])}

    current = [r for r in rows if not claims.expired(r["text"], today)]  # = live_rows
    pairs = [{"key": c["key"], "why": c["why"], "left": side(c["left"]),
              "right": side(c["right"])} for c in conflicts(current, events)]
    from cortex import corrections, more_sources  # read imported pages: only the Review asks
    rules = [{"key": x["key"], "text": _shown(x["text"]), "chats": x["chats"],
              "page": x["page"]}
             for x in ([] if "corrections" in off else corrections.proposals(vault, events,
                                                                              current))]
    decisions = [{**x, "project": plain(x["project"], 80), "subject": plain(x["subject"], SHOWN)}
                 for x in ([] if "decisions" in off
                           else more_sources.decision_proposals(vault, events))]
    from cortex import tags  # it imports this module
    forget = [{"key": x["key"], "text": plain(x["text"], SHOWN),
               "candidates": [side(c) for c in x["candidates"]]}
              for x in ([] if "forget" in off else tags.forget_cards(vault, events, today))]
    from cortex import sources
    imports = [{**x, "title": plain(x["title"], SHOWN)}
               for x in sources.edited_pages(vault, events)]
    unsafe = [{"key": x["key"], "page": x["page"], "id": x["id"], "what": x["what"],
               "invisible": claims.invisible(x["what"]),
               "text": _shown("".join(c for c in x["text"] if c.isprintable()))}
              for x in unsafe_lines(vault, events) if x["page"] not in hidden]
    left = max(0, TODAY_MAX - answered_today(events, today))
    return {"pending": waiting, "expired": expired, "stale": old, "conflicts": pairs,
            "rules": rules, "decisions": decisions, "forget": forget, "imports": imports,
            "unsafe": unsafe, "today": _today(waiting, pairs, rules)[:left], "today_left": left,
            "paused": [{"producer": p, "label": PRODUCERS[p], **led[p]} for p in sorted(off)],
            "count": len(waiting) + len(expired) + len(old) + len(pairs) + len(rules)
            + len(decisions) + len(forget) + len(imports) + len(unsafe)}


def _today(waiting: list[dict], pairs: list[dict], rules: list[dict]) -> list[dict]:
    """R3-4's order, as {type, key} of cards in the boards: teaching-rule proposals, then
    contradiction pairs, repeated corrections, and last the other waiting lines that 2+ chats
    proposed (a single inference never)."""
    rule = [p for p in waiting if p["page"] == "teaching-contract"]
    return ([{"type": "pending", "key": p["key"]} for p in rule]
            + [{"type": "conflict", "key": c["key"]} for c in pairs]
            + [{"type": "rule", "key": x["key"]} for x in rules]
            + [{"type": "pending", "key": p["key"]} for p in waiting
               if p not in rule and not p["single"]])
