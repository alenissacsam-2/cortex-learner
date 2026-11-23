"""Claims (v5 PHASE1.md F1-F3): the list lines under `## Section` of the me/ claim pages, their
ids, the revise/retire/reject text edits and the authorship-tier fold.

Everything but `tiers` is a pure function of page text; callers write through `cortex.vault`.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import re

from cortex.reflect import UNCONFIRMED
from cortex.vault import VaultError, invalid

# voice: PHASE4 V2; core: the pinned lines prime serves first (prime.core_lines); setup: the
# hardware and software, served with the preferences (G2)
PAGES = ("personality", "preferences", "goals", "profile", "voice", "core", "setup")
RESERVED = {"past": "past", "not me": "not_me"}  # lowercased heading -> claim status
TIERS = ("confirmed", "agent", "exposed", "typed", "legacy")
ACT_TIERS = ("typed", "confirmed")  # act-grade: may be said as the user (PHASE1.md)
# A yes the model relayed (no dialog click) from this day on is not act-grade: served (ask).
# Lines confirmed earlier were all relayed, since no dialog existed, and stay the user's.
RELAYED_FROM = "2026-11-17"
ID_TAIL = re.compile(r"\s+\^(c-[a-z2-7]{5})\s*$")
_ANY_ID = re.compile(r"\s+\^c-[a-z2-7]{5}(?![a-z2-7])")  # ids are server-stamped only
_MARK = re.compile(r"\s*\((?:(?:confirmed|proposed|rejected|until)\b[^)]*|since \d{4}-\d\d-\d\d)\)")
_UNTIL = re.compile(r"\s*\(until (\d{4}-\d\d-\d\d)\s*\)")  # a live line's own expiry (agenda 4)
# R13a: the day a line that replaced another started to hold (valid time; `stamp_since`)
_SINCE = re.compile(r"\s*\(since (\d{4}-\d\d-\d\d)\)")
_H2 = re.compile(r"^##[ \t]+(.*?)[ \t]*$")
_TOP = re.compile(r"^#{1,2}[ \t]")  # a heading that ends a ## section


# G6 (HERMES-GAPS): claim lines reach other apps' rules files word for word, so a line that reads
# as an instruction to an AI, or hides characters, is refused on write and not served. Phrases
# are matched on `index._key` folding with spaces kept (look-alikes, zero-width chars and
# dashes gone), tokens on the same folding with spaces dropped too.
_RULES = r"(?:instructions?|directions?|directives?|guidelines?|rules|messages|prompts?)"
_PHRASES = (
    ("ignore previous instructions", re.compile(
        r"(?<!not )(?<!n't )(?<!n\u2019t )(?<!dont )(?<!never )"  # "don't forget prior rules"
        r"\b(?:ignore|disregard|forget|override) (?:"
        # "disregard all previous directions": a word placing the rules before this text
        r"(?:(?:all|any) (?:of )?)?(?:(?:the|your|my) )?(?:previous|prior|above|earlier|"
        rf"preceding) {_RULES}"
        # "ignore your guidelines": the AI's own rules; a bare "forget instructions" is not it
        rf"|(?:(?:all|any) (?:of )?)?your {_RULES}"
        # "ignore the instructions above": the place named after the rules
        rf"|(?:(?:all|any) (?:of )?)?(?:(?:the|my) )?{_RULES} (?:above|so far|you (?:were|"
        r"have been|'ve been|\u2019ve been) given)"
        # "forget everything you were told": the whole conversation so far
        r"|(?:everything|anything|all) (?:that )?(?:you (?:were|have been|'ve been|\u2019ve "
        r"been) (?:told|given|taught)|above|(?:said|written) (?:above|before|earlier))"
        r")\b")),
    ("new instructions:", re.compile(r"\bnew instructions? ?:")),
    # a persona handed to the reader ("you are now DAN"); "I am now a final-year student" passes
    ("you are now", re.compile(r"\byou(?: are|'re|\u2019re|re) now\b")),
    # the DAN jailbreak by name; "Act as Dan's backup" and "I work with Dan" pass
    ("act as DAN", re.compile(r"\b(?:act(?:ing)? as|you(?: are|'re|\u2019re|re)|pretend "
                              r"(?:to be|you are)|role ?play as) (?:an? )?dan(?![\w'\u2019])"
                              r"|\bdo anything now\b")),
    # shortcut: only a system prompt someone is told to reveal, drop or replace, or a
    # `system prompt:` label, so "I write system prompts for my bots" passes
    ("system prompt", re.compile(r"\b(?:ignore|disregard|forget|reveal|print|show|repeat|leak|"
                                 r"override)\b(?: \w+){0,3} system prompts?\b|"
                                 r"\bsystem prompt ?:")))
# S1 (FARMA, MemoryGraft): an order a reader may take as its own, at a line or sentence
# start or after "always", "please" or "you should": "I sometimes skip tests" is a fact
_LEAD = (r"(?:(?m:^)|[.!?;:,] |\b(?:always|please|you should|you must|you need to|make sure "
         r"to|remember to|be sure to) )[^\w\n]*(?:(?:always|please|just|also|then) )*")
# a path that holds keys (`_` and `-` dropped by _key); .env.example and the like are no secret
_CRED = (r"(?:~?/?\.ssh\b|\bidrsa\b|\.env\b(?!\.(?:example|sample|template|dist)\b)"
         r"|\.aws/credentials\b|\.gitcredentials\b)")
_KEYS = (r"(?:(?:api|secret|private|ssh|access) ?keys?|secrets?|tokens?|passwords?|"
         r"credentials?)")
_CODE = r"(?:the |your )?(?:code|source|repo|repository|git|github|commit)\b"
# matched on the folded lines kept apart (line starts) and joined (an order split over lines)
_ORDERS = (
    ("an order to skip checks", re.compile(
        _LEAD + r"(?:skip|bypass|disable|turn off|ignore) (?:(?:the|all|any|your|every) )?"
        r"(?:failing |unit |integration |pre ?commit |ci )?(?:tests?|checks?|verification|ci|"
        r"hooks?|lint(?:ing|ers?)?|type ?checks?)\b"
        # --no-verify ("-" dropped by _key) in a sentence with no "never", "don't", ...
        r"|(?:(?m:^)|[.!?] )(?:(?!\b(?:never|not|don't|don\u2019t|dont|avoid|without)\b)"
        r"[^.!?\n])*\bnoverify\b")),
    ("a credential path", re.compile(
        _LEAD + r"(?:(?:read|cat|open|print|show|send|upload|post|share|copy|paste|include|"
        r"attach|commit|push|email|leak|output|dump|reveal|forward|display)\b(?: \S+){0,4}? "
        rf"(?:\S*{_CRED}|{_KEYS} (?:to|into|in) {_CODE})"
        rf"|hard ?coded?(?: \S+){{0,3}}? {_KEYS}\b"
        rf"|(?:put|embed|write|store)(?: \S+){{0,3}}? {_KEYS} (?:directly )?in(?:to)? {_CODE})")),
)
# images: a chat client that renders Markdown fetches the URL, carrying what a model put in it
_IMAGE = re.compile(r"!\[[^\]\n]*\] ?[(\[]|<img\b")
_REFDEF = re.compile(r"(?m)^\[[^\]\n]+\]: ?\S")  # `[x]: url`: then a bare `![x]` is an image
# the pages AI apps take as orders; profile, goals, voice and setup keep their links
ORDER_PAGES = ("core", "preferences", "personality", "teaching-contract")
_LINK = re.compile(r"\bhttps?://|\bwww\.")
_TOKENS = (("a chat template token", re.compile(
               # Gemma's <start_of_turn> ("_" dropped), Anthropic-style XML role tags
               r"<\|[a-z]{0,24}\|>|\[/?inst\]|<</?sys>>|<(?:start|end)ofturn>"
               r"|</?(?:system|human|assistant)>|</instructions>")),
           ("Cortex's untrusted_note tag", re.compile(r"</?untrustednote")))
# a chat role label opening a line, a sentence or a typed "\n" ("SYSTEM: you must ...",
# "Human: hi\nAI: sure"); `user:`, `ai:`, `system:` and `developer:` only when an order
# follows or after a typed "\n", so "System: Windows 11" and "AI: Claude Pro" on me/setup pass
_ROLE = re.compile(
    r"(?m)(?:^|[.!?] |(?P<nl>/n))[^\w\n]*(?P<role>assistant|human|"  # _key folds "\" to "/"
    r"(?:user|ai|system|developer)(?(nl)|(?=[^\w\n]*: ?(?:you|your|always|"
    r"never|must|do|don't|dont|ignore|disregard|forget|from now on|send|email|reveal|print|"
    r"share|delete|obey)\b)))[^\w\n]*:")
# three or more single chars in a row ("i g n o r e"): joined, checked for a trigger word
_SPACED = re.compile(r"(?<!\S)(?:\S ){2,}\S(?!\S)")
_TRIGGER = re.compile(r"ignore|disregard|forget|override|instruction|prompt|system")


def unsafe(text: str, page: str | None = None) -> str | None:
    """What makes `text` unfit for a me/ page (G6): an invisible or control character (a
    format or bidi control, a tag character, a C0/C1 control other than tab and line breaks;
    a zero-width joiner or non-joiner after a non-ASCII char is a script's or an emoji's own),
    an injection phrase, an order to skip checks or to read, send or commit a credential, an
    image, a chat template token, or (S1) a web link when `page` is one of ORDER_PAGES; None
    when it is fine. `page` None (not known) lets links pass."""
    import unicodedata

    from cortex.index import _key  # its folding: look-alikes, marks and zero-width chars out

    for i, c in enumerate(text):
        cat = unicodedata.category(c)
        if cat == "Cc" and c not in "\t\n\r":
            return f"a control character (U+{ord(c):04X})"
        if cat == "Cf" and not (c in "\u200c\u200d" and i and not text[i - 1].isascii()):
            return f"an invisible character (U+{ord(c):04X})"
    lines = [" ".join("".join(" " if c.isspace() else _key(c) for c in ln).split())
             for ln in text.splitlines()]
    loose = " ".join(filter(None, lines))
    tight = loose.replace(" ", "")
    split = "\n".join(lines)
    for name, rx in _PHRASES:
        if rx.search(loose):
            return f'the phrase "{name}"'
    for name, rx in _ORDERS:
        if rx.search(split) or rx.search(loose):
            return name
    if _IMAGE.search(loose) or ("![" in loose and _REFDEF.search(split)):
        return "an image"
    if page in ORDER_PAGES and _LINK.search(loose):
        return "a web link (links belong on a resource page, not in rules AI apps follow)"
    if m := _ROLE.search(split):
        return f'the role label "{m.group("role")}:"'
    for m in _SPACED.finditer(loose):
        if _TRIGGER.search(word := m.group().replace(" ", "")):
            return f'spaced-out letters ("{word}")'
    return next((name for name, rx in _TOKENS if rx.search(tight)), None)


def invisible(what: str) -> bool:
    """Whether an `unsafe` finding is a hidden character rather than an instruction."""
    return what.startswith(("an invisible", "a control"))


def check(text: str, page: str | None = None) -> None:
    """Refuse `text` for me/`page` when `unsafe` finds something; nothing is written."""
    if what := unsafe(text, page):
        raise invalid(f"Nothing was written: the text holds {what}. Lines on me/ pages go word "
                      "for word to other AI apps, so Cortex refuses text that reads as an "
                      "instruction to an AI or hides characters; reword it.")


def norm(text: str) -> str:
    """Claim text for hashing and matching: no id, no `(confirmed …)`-style markers, collapsed
    whitespace, lowercase."""
    return " ".join(_MARK.sub("", ID_TAIL.sub("", text)).removeprefix("- ").split()).lower()


def expired(text: str, today: str) -> str | None:
    """The `(until YYYY-MM-DD)` day of a claim line once `today` is past it, else None; the
    until day itself is still served."""
    m = _UNTIL.search(text)
    return m.group(1) if m and m.group(1) < today else None


def strip_id(line: str) -> str:
    return ID_TAIL.sub("", line)


def since(text: str) -> str | None:
    """R13a: a claim line's own `(since YYYY-MM-DD)` day, else None."""
    m = _SINCE.search(text)
    return m.group(1) if m else None


def strip_since(line: str) -> str:
    """A served line without its `(since …)` stamp: rules files and tool text carry no dates,
    so the clients' prompt caches keep hitting."""
    return _SINCE.sub("", line)


def stamp_since(text: str, page: str, target: str, day: str) -> tuple[str, str | None]:
    """R13a: page `text` with ` (since DAY)` before the id of its live claim `target`, and that
    line; (text, None) when the line is not live there or already has a since or until day."""
    c = next((r for r in parse(page, text) if r["id"] == target and r["status"] == "live"), None)
    if c is None or since(c["text"]) or re.search(r"\(until\b", c["text"]):
        return text, None
    lines = text.splitlines(keepends=True)
    body = lines[c["line_no"]].rstrip("\r\n")
    m = ID_TAIL.search(body)
    at = m.start() if m else len(body.rstrip())
    new = f"{body[:at]} (since {day}){body[at:]}"
    lines[c["line_no"]] = new + lines[c["line_no"]][len(body):]
    return "".join(lines), new


def make_id(page: str, text: str, day: str, taken=()) -> str:
    """`c-` + 5 base32 chars of sha256("me/<page>\\n<norm text>\\n<day>"); the next 5 on a clash."""
    salt = ""
    for n in range(1000):  # identical lines on one page: salt the hash once the 10 slots clash
        digest = hashlib.sha256(f"me/{page}\n{norm(text)}\n{day}{salt}".encode()).digest()
        h = base64.b32encode(digest).decode().lower().rstrip("=")
        for i in range(0, len(h) - 4, 5):
            if (cid := f"c-{h[i:i + 5]}") not in taken:
                return cid
        salt = f"\n{n + 1}"
    raise invalid("No free claim id; reword the line.")


def parse(page: str, text: str) -> list[dict]:
    """Every `- ` line under a `## ` heading: {page, section, id, explicit_id, text, line_no,
    status}. `text` is the line without `- ` and id; a line without an id gets a derived one."""
    rows, section = [], None
    for no, line in enumerate(text.splitlines()):
        if m := _H2.match(line):
            section = m.group(1)
        elif line.startswith("# "):
            section = None
        elif section is not None and line.startswith("- "):
            m = ID_TAIL.search(line)
            rows.append({"page": page, "section": section, "id": m.group(1) if m else None,
                         "explicit_id": bool(m), "text": strip_id(line)[2:].strip(),
                         "line_no": no, "status": RESERVED.get(section.lower(), "live")})
    taken = {r["id"] for r in rows if r["id"]}
    for r in rows:  # derived ids after the explicit ones, so they never take an explicit id
        if r["id"] is None:
            r["id"] = make_id(page, r["text"], "", taken)
            taken.add(r["id"])
    return rows


def add_ids(page: str, page_text: str, text: str, day: str) -> tuple[str, list[str]]:
    """`text` to append with a server id on each `- ` line (one bare line becomes a list line);
    an id the caller wrote is replaced."""
    if "\n" not in text.strip() and not text.lstrip().startswith("- "):
        text = "- " + text.strip()
    taken = {r["id"] for r in parse(page, page_text)}
    ids, out = [], []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("- "):
            line = _ANY_ID.sub("", line).rstrip()
            ids.append(make_id(page, line, day, taken))
            taken.add(ids[-1])
            line += f" ^{ids[-1]}"
        out.append(line)
    return "\n".join(out), ids


def _append_under(lines: list[str], heading: str, new: str, nl: str) -> None:
    """Add line `new` at the end of `## heading` in `lines` (keepends), creating it at the end."""
    start = next((i for i, ln in enumerate(lines) if (m := _H2.match(ln.rstrip("\r\n")))
                  and m.group(1).lower() == heading.lower()), None)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += nl
    if start is None:
        lines += [nl, f"## {heading}{nl}", f"{new}{nl}"]
        return
    end = next((j for j in range(start + 1, len(lines)) if _TOP.match(lines[j])), len(lines))
    at = next((j + 1 for j in range(end - 1, start, -1) if lines[j].strip()), start + 1)
    lines.insert(at, f"{new}{nl}" + (nl if at < len(lines) and _TOP.match(lines[at]) else ""))


def move_to_past(text: str, page: str, target: str, day: str,
                 new_line: str | None = None, until: str | None = None) -> tuple[str, str]:
    """retire (no `new_line`) or revise: the live claim `target` moves to `## Past` with
    ` (until UNTIL[ → ^new])` (UNTIL defaults to the line's own past `(until …)` day, else
    `day`; the live marker is dropped) and keeps its id; a revise puts
    `- new_line ^new` in its place. Returns (new text, the new id or the retired one)."""
    cid = target.strip().lstrip("^")
    c = next((r for r in parse(page, text) if r["id"] == cid and r["status"] == "live"), None)
    if c is None:
        raise invalid(f"No live claim ^{cid} on me/{page}; read the page and use a line's ^c-… id.")
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    out_id = cid
    if new_line is None:
        del lines[c["line_no"]]
    else:
        new_line = _ANY_ID.sub("", new_line)
        out_id = make_id(page, new_line, day, {r["id"] for r in parse(page, text)})
        lines[c["line_no"]] = f"- {new_line} ^{out_id}{nl}"
    arrow = f" → ^{out_id}" if new_line is not None else ""
    own = _UNTIL.search(c["text"])  # the line's own expiry dates the Past line, not twice
    until = until or (own.group(1) if own and own.group(1) <= day else day)
    _append_under(lines, "Past", f"- {_UNTIL.sub('', c['text'])} (until {until}{arrow}) ^{cid}",
                  nl)
    return "".join(lines), out_id


_PAST_STAMP = re.compile(r"\s*\(until [^)]*\)\s*$")  # the stamp move_to_past adds


def restore_from_past(text: str, page: str, target: str, section: str | None) -> str:
    """move_to_past's inverse for a retire: the `## Past` line `target` goes back, unchanged
    but for its `(until …)` stamp and with its id, to the end of `## section` when the page has
    it, else of the page's first section."""
    cid = target.strip().lstrip("^")
    c = next((r for r in parse(page, text) if r["id"] == cid and r["status"] == "past"), None)
    if c is None:
        raise invalid(f"No past claim ^{cid} on me/{page}.")
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    del lines[c["line_no"]]
    heads = [m.group(1) for ln in lines if (m := _H2.match(ln.rstrip("\r\n")))
             and m.group(1).lower() not in RESERVED]
    want = next((h for h in heads if section and h.lower() == section.lower()), None)
    _append_under(lines, want or (heads[0] if heads else section or "Notes"),
                  f"- {_PAST_STAMP.sub('', c['text'])} ^{cid}", nl)
    return "".join(lines)


def unpropose(inbox: str, target: str) -> tuple[str, str]:
    """Drop the `## About me (unconfirmed)` lines matching `target` (the full proposed line or
    its text after `<section>: `). Returns (new inbox text, the claim text without section)."""
    want, found, on, keep = norm(target), None, False, []
    for line in inbox.splitlines(keepends=True):
        s = line.rstrip("\r\n")
        if s.startswith("#"):
            on = s.strip() == UNCONFIRMED
        elif on and s.startswith("- "):
            body = _MARK.sub("", s[2:]).strip()
            rest = body.split(": ", 1)[-1]  # _propose writes `- <section>: <text>`
            if want in (norm(body), norm(rest)):
                found = found or rest
                continue
        keep.append(line)
    if found is None:
        raise invalid("No line under '## About me (unconfirmed)' in inbox.md matches; pass its "
                      "text as cortex_reflect shows it.")
    return "".join(keep), found


def add_not_me(text: str, claim: str, day: str) -> str:
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    _append_under(lines, "Not me", f"- {claim} (rejected {day})", nl)
    return "".join(lines)


def is_not_me(page: str, text: str, claim: str) -> bool:
    """True when `claim` matches a `## Not me` line of the page (normalised)."""
    return norm(claim) in {norm(r["text"]) for r in parse(page, text) if r["status"] == "not_me"}


def drop(text: str, page: str, cid: str) -> tuple[str, list[dict]]:
    """R10 forget: `text` without claim `cid`'s lines (any section) and the `## Past` lines
    that led to it (`→ ^cid)`, followed back); the rows removed, each with its raw `line`."""
    lines, rows = text.splitlines(keepends=True), parse(page, text)
    ids, gone = {cid}, {}
    while more := [r for r in rows if r["line_no"] not in gone and (r["id"] in ids or any(
            f"→ ^{i})" in r["text"] for i in ids))]:
        for r in more:
            gone[r["line_no"]] = {**r, "line": lines[r["line_no"]].rstrip("\r\n")}
            ids.add(r["id"])
    return "".join(ln for n, ln in enumerate(lines) if n not in gone), list(gone.values())


def rows(vault) -> list[dict]:
    """The claim rows (`parse`) of every claim page, any status."""
    out = []
    for page in PAGES:
        p = vault.root / "me" / f"{page}.md"
        out += parse(page, p.read_bytes().decode("utf-8") if p.is_file() else "")
    return out


def is_live(r: dict, today: str | None = None) -> bool:
    """A live claim row; with `today`, also not past its own `(until …)` day (agenda 4), so
    every consumer that passes `today` agrees with prime and the export (review r4 L1)."""
    return r["status"] == "live" and not (today and expired(r["text"], today))


def resolve(vault, item, today: str | None = None) -> str:
    """A claim id (`c-…`, `^` optional) or a live claim line's text as served (an `(ask)` or an
    old `(said)` label allowed) -> that live claim's id, by id or by `norm` equality; else
    `invalid_argument`."""
    if isinstance(item, str) and item.strip():
        item = item.strip().removesuffix("(said)").removesuffix("(ask)")  # serving labels
        live = [r for r in rows(vault) if is_live(r, today)]
        cid = item.strip().lstrip("^")
        found = {r["id"] for r in live if r["id"] == cid} or {
            r["id"] for r in live if norm(r["text"]) == norm(item)}
        if len(found) == 1:
            return found.pop()
    raise VaultError("invalid_argument", "A claim must be one live claim's ^c-… id or its line "
                     "text as served; read the me/* page first.")


def late_relay(r: dict) -> bool:
    """A confirmed row whose yes the model relayed on or after RELAYED_FROM: not act-grade."""
    return (r["tier"] == "confirmed" and r["confirm"] in (None, "relayed")
            and str(r["day"] or "") >= RELAYED_FROM)


def act_grade(row: dict, label: str) -> bool:
    """May this claim row be said as the user: an act-grade tier, not served as (ask)."""
    return row["tier"] in ACT_TIERS and label != " (ask)"


def tiers(vault, events, log=None, today: str | None = None) -> list[dict]:
    """The live claims of every claim page (with `today`, none past its `(until …)` day), each
    with its `tier` (F3): the latest change event that wrote its id decides; else `legacy` if
    the `claims_baseline` holds it, else `typed`.
    With no baseline yet, the current lines are the baseline; with `log` it is recorded once.
    A `confirmed` row's `confirm` (idea 11) is `clicked` when the user ticked yes in Cortex's
    own dialog or clicked Approve or Still true in the Cortex app (plan 5a: the strongest tier,
    never a late relay), `reflected` for a yes in a clean reflect (a `promote` reconfirm),
    `tagged` for a //pref or //fact line the user typed (cortex.tags; never a late relay), else
    `relayed` (the model's word, as every yes before the dialog)."""
    all_rows = rows(vault)
    events = list(events)
    voided = {e.get("ref") for e in events if e.get("kind") == "void"}
    wrote, base = {}, None  # (page, claim id) -> (tier, session, day, confirm) of the decider
    for ev in events:
        pl = ev.get("payload")
        if not isinstance(pl, dict):
            continue
        if (ev.get("kind") == "log" and pl.get("kind") == "reconfirm" and pl.get("confirm") ==
                "clicked" and pl.get("verdict") == "yes" and ev.get("id") not in voided):
            for r in all_rows:  # plan 5a: Still true clicked in the Cortex app, any tier
                if r["id"] == pl.get("claim"):
                    wrote[(r["page"], r["id"])] = ("confirmed", ev.get("session"), ev.get("day"),
                                                   "clicked")
            continue
        if (ev.get("kind") == "log" and pl.get("kind") == "reconfirm" and pl.get("promote") is True
                and ev.get("id") not in voided):  # v6 U3: a user's yes in a clean reflect
            for k, w in wrote.items():  # review r4 M3: an exposed line too, now asked clean
                if k[1] == pl.get("claim") and (w[0] in ("agent", "exposed") or late_relay(
                        {"tier": w[0], "confirm": w[3], "day": w[2]})):
                    wrote[k] = ("confirmed", *w[1:3], "reflected")
            continue
        if ev.get("kind") != "change":
            continue
        if pl.get("note") == "claims_baseline" and base is None:
            base = pl.get("claims") if isinstance(pl.get("claims"), dict) else {}
        c = pl.get("change")
        if isinstance(c, dict) and c.get("tier") in TIERS and c.get("claim"):
            for cid in c["claim"] if isinstance(c["claim"], list) else [c["claim"]]:
                wrote[(c.get("id"), cid)] = (c["tier"], ev.get("session"), ev.get("day"),
                                             c.get("confirm"))
    if base is None:
        base = {}
        for r in all_rows:
            base.setdefault(r["page"], []).append(r["id"])
        if log is not None:
            log.append(session="cli", kind="change", source="tutor",
                       fields={"payload": {"note": "claims_baseline", "claims": base}})
    out = []
    for r in all_rows:
        if is_live(r, today):
            tier, sid, day, how = wrote.get((r["page"], r["id"])) or (
                "legacy" if r["id"] in base.get(r["page"], ()) else "typed", None, None, None)
            if tier == "confirmed":  # `reflected` is only stamped by the promote branch above
                how = how if how in ("clicked", "reflected", "tagged") else "relayed"
            else:
                how = None
            out.append({**r, "tier": tier, "confirm": how, "session": sid, "day": day})
    return out


SAVED_DAYS = 14
SAVED_CHANGES = 15  # r1-trust P4: cap the noise; claim rows are never capped


def saved(vault, events, today: str, days: int = SAVED_DAYS) -> dict:
    """v6 T4 + U3: what was saved about the user without their say. `claims`: the live claim
    lines of tier `exposed` (first) or `agent`, or a relayed yes from RELAYED_FROM
    (`late_relay`); `changes`: the latest `me/` change events of the last `days` days that
    wrote no such line. Each row has its `undo`. Read-only; texts
    are raw note text, so callers escape or wrap them."""
    events = list(events)
    live = tiers(vault, events, today=today)
    rows = sorted((r for r in live if r["tier"] in ("exposed", "agent") or late_relay(r)),
                  key=lambda r: (r["tier"] != "exposed", r["day"] or "", r["page"]))
    for r in rows:
        r["undo"] = (f"cortex undo {r['session']}, or delete the line in me/{r['page']}.md"
                     if r["session"] else f"delete the line in me/{r['page']}.md")
    shown = {(r["page"], r["id"]) for r in rows}
    cut = (dt.date.fromisoformat(today) - dt.timedelta(days=days)).isoformat()
    voided = {e.get("ref") for e in events if e.get("kind") == "void"}
    changes = []
    for e in events:
        pl = e.get("payload") if e.get("kind") == "change" else None
        c = pl.get("change") if isinstance(pl, dict) else None
        if (not isinstance(c, dict) or c.get("kind") != "me" or str(e.get("day", "")) < cut
                or e.get("id") in voided):
            continue
        cids = c.get("claim") if isinstance(c.get("claim"), list) else [c.get("claim")]
        if any((c.get("id"), cid) in shown for cid in cids):
            continue
        op = "propose" if c.get("tool") == "propose" else str(c.get("op"))
        changes.append({"page": str(c.get("id")), "op": op,
                        "target": str(c.get("target") or ""), "tier": c.get("tier"),
                        "exposed": pl.get("exposed") is True, "day": e.get("day"),
                        "session": e.get("session"),
                        "undo": f"cortex undo {e.get('session')}"})
    changes = changes[::-1][:SAVED_CHANGES]  # newest first
    changes.sort(key=lambda r: not r["exposed"])  # stable: exposed rows first
    return {"claims": rows, "changes": changes}


def pair_key(a: tuple[str, str], b: tuple[str, str]) -> str:
    """A pair's key from its two (page, claim id): both `page/id`, sorted, so either order is
    the same pair (the Review's conflict key, review.pair_key)."""
    return "|".join(sorted(f"{p}/{i}" for p, i in (a, b)))


def recorded_pairs(events) -> tuple[dict, set]:
    """({key: (old (page, id), new (page, id), why)}, kept keys): each pair a change event
    recorded, unvoided and not kept both: cortex_patch's `update_of` (why `update`) and RR-4's
    `supersedes` (why `supersedes`), old first; and the keys of the app's Keep both clicks.
    Whether both lines are still live is the caller's to check."""
    events = list(events)
    voided = {e.get("ref") for e in events if e.get("kind") == "void"}
    kept, out = set(), {}
    for e in events:
        pl = e.get("payload") if e.get("kind") == "change" else None
        c = pl.get("change") if isinstance(pl, dict) else None
        if not isinstance(c, dict) or e.get("id") in voided:
            continue
        if c.get("tool") == "keep_both" and isinstance(c.get("pair"), str):
            kept.add(c["pair"])
        if not isinstance(c.get("claim"), str):
            continue
        sup = c.get("supersedes") if isinstance(c.get("supersedes"), list) else []
        for why, u in [("update", c.get("update_of")), *(("supersedes", s) for s in sup)]:
            if isinstance(u, dict) and isinstance(u.get("claim"), str):
                old, new = (u.get("page"), u["claim"]), (c.get("id"), c["claim"])
                if old != new:
                    out.setdefault(pair_key(old, new), (old, new, why))
    return {k: v for k, v in out.items() if k not in kept}, kept

