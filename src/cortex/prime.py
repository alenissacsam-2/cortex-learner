"""The light part of cortex_prime: the pinned Core, Profile, claim legend, Preferences and the
teaching-contract core, built from a Vault and the event log alone. It never imports the `mcp`
SDK, numpy or `cortex.semantic` and never builds the Index, so the `cortex prime-hook` process
stays fast on a large vault; the MCP server adds the Related block on top
(mcp_server.Cortex.cortex_prime). Preferences rank by meaning only with an embedder that
process already loaded.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime

from cortex import claims, xray
from cortex.contract import MAX_CHARS as CONTRACT_MAX
from cortex.contract import scope_contract
from cortex.events import local_day
from cortex.index import STOPWORDS, TRUNCATED, _edge_targets, _wrap
from cortex.vault import ID_RE, Vault, VaultError, is_private

CONTEXT_MAX = 2400  # characters, about 600 tokens
# cortex_prime: one cheap first call per chat, about 500 tokens
PRIME_MAX, PRIME_PROMPT = 2000, 2000
PRIME_CAPS = {"profile": 250, "preferences": 800, "contract": 400, "related": 450,
              "mistakes": 250}
# me/core.md: the user's pinned lines, first in every prime, whole lines within this many chars
CORE_MAX, CORE_HEAD = 500, "Core"
VEC_CACHE = 5000  # line vectors kept for a long-running server; past it the cache starts over
# PHASE3 H3: served claim labels and ranking
LEGEND = "(ask) = check with the user before relying on it."
ASK_WEIGHT = 0.3
# phase4-wiki §7.2: the me/personality.md sections cortex_context serves, by scope
PERSONALITY_HEADINGS = {"all": ("communication", "how i think"),
                        "communication": ("communication",), "learning": ("how i think",)}
NOT_RECORDED = "not recorded yet; ask the user and save it with cortex_patch."
# Sections that say how to work with the user in any chat; when no line matches the prompt,
# these lead, and situational ones (Projects, Coursework, …) wait for a match.
GENERAL_SECTIONS = ("communication", "working with ai", "learning", "how i think")
# Words that say what to do, not what about: "revise" must not pull a line that says
# "revised a dissertation". Prime-only; index.STOPWORDS stays as search needs it.
TASK_WORDS = frozenset(("revise", "revision", "help", "make", "write", "solve",
                        "explain", "learn", "study", "practice", "practise", "today",
                        "tomorrow", "please", "want", "need", "can", "how", "what",
                        "why", "show", "give", "tell", "let", "lets", "get", "use"))
# Study abbreviations in a prompt -> the long words preference lines use. The match is a
# substring test, so only expansions of 4+ letters, never the short forms ("os" is in "most").
ABBREVIATIONS = {
    "dbms": ("database",), "sql": ("database",), "os": ("operating",),
    "oop": ("object", "oriented"), "dsa": ("data structure", "algorithm"),
    "ml": ("machine learning",), "dl": ("deep learning",), "ai": ("artificial intelligence",),
    "cn": ("network",), "dp": ("dynamic programming",), "llm": ("language model",),
    "genai": ("generative",), "nlp": ("language processing",), "js": ("javascript",),
    "py": ("python",)}
PROFILE_TEMPLATE = "Who I am"  # the me/profile.md summary `cortex init` writes (cli.ME_PAGES)
# a non-owner server leaves out me/ pages not shared with work (mcp_server.Cortex._withheld)
WITHHELD = "withheld: {n} me/ page(s) not shared with this server's role."

# ideas-r2 #2: one line, never a gate (forced work before AI help did worse, IZA DP 18338).
# Its block comes out of the preferences cap, so the prime budget stays the same.
ATTEMPT_HEAD = "Attempt first"
ATTEMPT = ("[[{id}]] is {status}: before solving, ask once for their approach (pattern and "
           "complexity) in a sentence or two and grade it with cortex_grade(item_type="
           "'application', problem='{id}'{concept}); if they say just show me, solve it.")
_LC_ID = re.compile(r"\blc-\d+\b")
_LC_URL = re.compile(r"leetcode\.(?:com|cn)/problems/([a-z0-9-]+)")
_FRONT_FIELD = re.compile(r"""^(title|url):[ \t]*["']?(.*?)["']?[ \t\r]*$""", re.M)

# relink's navigation lines (`Up: [[x]]`, `People: [[people]]`, `Topics: [[a]], [[b]]`)
_NAV_LINE = re.compile(r"\s*\w+:\s*\[\[[^\]]*\]\](?:\s*,\s*\[\[[^\]]*\]\])*\s*")


def _first_paragraph(body: str) -> str:
    """The first paragraph of a note body that is not only headings or navigation links."""
    for para in body.strip().split("\n\n"):
        text = "\n".join(ln for ln in para.splitlines()
                         if not ln.lstrip().startswith("#") and not _NAV_LINE.fullmatch(ln))
        if text.strip():
            return text.strip()
    return ""


def _sections(body: str) -> list[str]:
    """Each `## ` section of a note body, heading line included."""
    return [f"## {c}".strip() for c in ("\n" + body).split("\n## ")[1:]]


def me(vault: Vault, id: str):
    """A me/ note, or None if the file does not exist."""
    try:
        return vault.read("me", id)
    except VaultError as e:
        if e.code == "unknown_id":
            return None
        raise


def profile_summary(vault: Vault, today: str, served: dict | None = None,
                    held_ids: set[str] = frozenset()) -> str:
    """The `summary:` of me/profile.md, else its first paragraph, without lines past their
    `(until …)` day on local day `today` (agenda 4); "" when absent. The init template's
    summary carries no fact, so its live claim lines stand in for it, less those whose id is
    in `held_ids` (R3-3, `held`). A claim line gets its `served` label (B3: an agent or
    exposed line is ` (ask)`, as in claim_items)."""
    profile = me(vault, "profile")
    summary = str(profile.fields.get("summary") or "").strip() if profile else ""
    if summary == PROFILE_TEMPLATE:
        ids = claim_ids(vault) if held_ids else {}
        text = "\n".join(claims.strip_id(ln) for sec in _sections(profile.body)
                         if (head := sec.partition("\n")[0][3:].strip()).lower()
                         not in claims.RESERVED
                         for ln in sec.splitlines()[1:] if ln.startswith("- ") and ids.get(
                             ("profile", head, claims.strip_id(ln)[2:].strip())) not in held_ids)
    else:
        text = summary or (_first_paragraph(profile.body) if profile else "")
    labels = {t: lab for (p, _, t), (lab, _) in (served or {}).items() if p == "profile" and lab}

    def label(ln: str) -> str:
        lab = labels.get(claims.strip_id(ln)[2:].strip()) if ln.startswith("- ") else None
        return claims.strip_id(ln) + lab if lab else ln

    return "\n".join(label(ln) for ln in text.splitlines()
                     if not claims.expired(ln, today)).strip()


def contract(vault: Vault, domains: set) -> str:
    """me/teaching-contract.md with the overrides of `domains`, cut at CONTRACT_MAX."""
    p = vault.root / "me/teaching-contract.md"
    text = scope_contract(p.read_bytes().decode("utf-8") if p.is_file() else "", domains)
    if len(text) > CONTRACT_MAX:
        text = text[:CONTRACT_MAX - len(TRUNCATED)].rstrip() + TRUNCATED
    return text


_CORE_HEAD = re.compile(r"##\s+core(\s+rules)?\s*", re.I)
_RULE = re.compile(r"(\d+[.)]|[-*+])\s+(.*)")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def contract_core(text: str) -> list[str]:
    """CONSENSUS item 3: the contract's rules, not its title or preamble. The lines of a
    `## Core rules` (or `## Core`) section if there is one, else each top-level numbered or
    bulleted rule's lead (its leading bold text, else its first sentence), in order."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":  # frontmatter
        end = next((i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---"), 0)
        lines = lines[end + 1:]
    at = next((i for i, ln in enumerate(lines) if _CORE_HEAD.fullmatch(ln.rstrip())), None)
    if at is not None:
        out = []
        for ln in lines[at + 1:]:
            if ln.startswith("#"):
                break
            if ln.strip():
                out.append(ln.rstrip())
        return out
    out = []
    for ln in lines:
        if not (m := _RULE.fullmatch(ln.rstrip())):  # indented lines are sub-points
            continue
        body = m.group(2)
        bold = re.match(r"\*\*(.+?)\*\*", body)
        lead = bold.group(0) if bold else _SENTENCE_END.split(body, maxsplit=1)[0]
        out.append(f"{m.group(1)} {lead}")
    return out


def served_claims(vault: Vault, events: list[dict]) -> dict:
    """PHASE3 H3: {(page, section, text): (label, score)} for each live claim, by tier alone.
    The user's own lines (typed, legacy, confirmed) are served plain; (ask) for agent or
    exposed, or a yes the model relayed from claims.RELAYED_FROM, ranked below plain lines."""
    out = {}
    for r in claims.tiers(vault, events):
        ask = claims.late_relay(r) or r["tier"] in ("agent", "exposed")
        out[r["page"], r["section"], r["text"]] = (" (ask)" if ask else "",
                                                   0.5 * (ASK_WEIGHT if ask else 1))
    return out


def held(vault: Vault, events, today: str) -> set[str]:
    """R3-3: the `^c-…` ids of the older line of each recorded pair (claims.recorded_pairs:
    `update_of`, `supersedes`) that still waits in the app's Review, both lines live on
    non-private pages. Prime and cortex_context serve only the newer line until the user keeps
    both or one. Derived from events at read time; nothing stored."""
    pairs, _ = claims.recorded_pairs(events)
    if not pairs:
        return set()
    hidden = {n.id for n in vault.iter_notes("me") if is_private(n)}
    live = {(r["page"], r["id"]) for r in claims.rows(vault)
            if claims.is_live(r, today) and r["page"] not in hidden}
    return {xray.claim(old[1]) for old, new, _ in pairs.values() if old in live and new in live}


def claim_ids(vault: Vault) -> dict:
    """{(page, section, text): `^c-…`} for every claim line (xray's claim id form)."""
    return {(r["page"], r["section"], r["text"]): xray.claim(r["id"]) for r in claims.rows(vault)}


def claim_items(vault: Vault, page: str, keep, served: dict, today: str, ids: dict | None = None,
                mute: set[str] = frozenset()) -> list[tuple]:
    """Per kept `## ` section of me/<page> (heading line, other lines, [(score, labelled list
    line, labelled?, `^c-…` id or None)] best first); never `## Past`, `## Not me`, a line
    whose own `(until …)` day is before `today` (agenda 4) or one whose id (`ids`) is muted."""
    note = me(vault, page)
    out = []
    for sec in _sections(note.body if note else ""):
        head, _, rest = sec.partition("\n")
        name = head[3:].strip().lower()
        lines = [ln.rstrip() for ln in rest.splitlines() if ln.strip()]
        if lines and name not in claims.RESERVED and keep(name):
            items = []
            for ln in reversed([ln for ln in lines if ln.startswith("- ")
                                and not claims.expired(claims.strip_id(ln), today)]):
                key = (page, head[3:].strip(), claims.strip_id(ln)[2:].strip())
                cid = (ids or {}).get(key)
                if cid in mute:
                    continue
                label, score = served.get(key, ("", 0.5))
                items.append((score, claims.strip_id(ln) + label, bool(label), cid))
            items.sort(key=lambda x: -x[0])  # stable: ties stay newest first
            out.append((head, [ln for ln in lines if not ln.startswith("- ")], items))
    return out


def putter(parts: list[str], left: list[int]):
    """A `put(head, text, id, cap, tail)` that appends a wrapped block to `parts`, within `cap`
    and the remaining budget `left[0]`; `tail` (a `more` line) follows the wrapped text,
    outside the note, since it is Cortex's own word."""
    def put(head: str, text: str, id: str | None = None, cap: int = CONTEXT_MAX,
            tail: str = "") -> None:
        while True:  # trim until the wrapped block fits (escaping can lengthen the text)
            block = f"## {head}\n" + (_wrap(id, text) if id else text) + (
                f"\n{tail}" if tail else "")
            over = len(block) - min(cap, left[0])
            if over <= 0:
                break
            if len(text) - over < 40:
                return
            text = text[: len(text) - over - 1].rstrip() + "…"
        parts.append(block)
        left[0] -= len(block) + 2
    return put


def fit(lines: list[str], id: str, cap: int, left: int, what: str = "") -> list[str]:
    """`lines` minus the last ones until their wrapped block fits `cap` and `left`. With
    `what` (a MORE key) and lines cut, room is kept for the `more` line saying so (RR-13)."""
    keep, room = list(lines), 0
    while keep and len(_wrap(id, "\n".join(keep))) + 40 + room > min(cap, left):
        keep.pop()  # 40: room for the "## <head>\n" line
        if what and not room:
            room = len(more(len(lines), what)) + 1  # n has at most this many digits
    return keep


# RR-13: the line after a block fit() cut, naming the call that reads the rest
MORE = {"preference": "cortex_context(scope='all')",
        "contract": "cortex_read(id='me/teaching-contract')",
        "related": "cortex_recall(query=<the message>)",
        "mistake": "cortex_context(topic=<the topic>)"}


def more(n: int, what: str) -> str:
    """`(+N more <what> lines: <call> reads them)`, or "" when nothing was cut."""
    return f"(+{n} more {what} lines: {MORE[what]} reads them)" if n > 0 else ""


def named_problem(vault: Vault, prompt: str):
    """ideas-r2 #2: the `todo` or `attempted` problem note the prompt names by id (`lc-<n>`),
    LeetCode URL slug or title (2+ words; the longest match wins), else None. It regex-reads
    each front matter and parses only the match: YAML-parsing ~300 problems takes seconds,
    past the prime hook's budget."""
    low = prompt.lower()
    ids, slugs = set(_LC_ID.findall(low)), set(_LC_URL.findall(low))
    folder = vault.root / Vault.KIND_DIRS["problem"]
    best, rank = None, 0
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        try:
            front = path.read_text(encoding="utf-8").split("\n---", 1)[0]
        except (OSError, UnicodeDecodeError):
            continue
        f = dict(_FRONT_FIELD.findall(front))
        title = f.get("title", "").strip().lower()
        r = (10_000 if path.stem in ids else
             5_000 if set(_LC_URL.findall(f.get("url", "").lower())) & slugs else
             len(title) if len(title.split()) > 1 and re.search(
                 rf"(?<!\w){re.escape(title)}(?!\w)", low) else 0)
        if r > rank:
            best, rank = path.stem, r
    try:
        note = vault.read("problem", best) if best else None
    except VaultError:
        return None
    return note if note and note.fields.get("status") in ("todo", "attempted") \
        and not is_private(note) else None


def attempt_line(note) -> str:
    """The Attempt first line for a named problem note (ids and status only, no note text)."""
    cs = _edge_targets(note.fields.get("concepts") or [])
    concept = f", concept='{cs[0]}'" if cs and ID_RE.fullmatch(cs[0]) else ""
    return ATTEMPT.format(id=note.id, status=note.fields["status"], concept=concept)


def core_lines(vault: Vault, served: dict, today: str,
               withheld: set[str] = frozenset(), ids: dict | None = None,
               mute: set[str] = frozenset()) -> list[str]:
    """me/core.md's live lines the user wrote or confirmed (the ones `served` labels plain),
    verbatim (claim id stripped) in page order, as many whole lines as fit CORE_MAX; [] with no
    page, a withheld or private page. Agent, exposed or relayed lines are never pinned, and a
    line whose id (`ids`) is muted in the X-ray is left out."""
    note = None if "core" in withheld else me(vault, "core")
    if note is None or is_private(note):
        return []
    out, size = [], 0
    for (page, sec, text), (label, _) in served.items():
        line = f"- {text}"
        if page != "core" or label or claims.expired(text, today) or (
                ids or {}).get((page, sec, text)) in mute:
            continue
        if size + bool(out) + len(line) <= CORE_MAX:  # a line that does not fit is dropped whole
            size += bool(out) + len(line)
            out.append(line)
    return out


_VECS: dict = {}  # line text -> unit vector, from the embedder in _VECS_OF[0]
_VECS_OF: list = [None]


def similarity(prompt: str, texts: list[str]) -> list[float] | None:
    """Cosine of `prompt` to each of `texts` with the bge embedder, only if this process has it
    loaded already (the MCP server's warm-up): it never imports cortex.semantic, so the prime
    hook stays light, and never loads a model. None when it is not warm or fails."""
    sem = sys.modules.get("cortex.semantic")
    embed = getattr(sem, "_embedder", None)
    if embed is None:
        return None
    try:
        if _VECS_OF[0] is not embed or len(_VECS) > VEC_CACHE:
            _VECS.clear()
            _VECS_OF[0] = embed
        if new := [t for t in dict.fromkeys(texts) if t not in _VECS]:
            _VECS.update(zip(new, sem._embed(embed, new), strict=True))
        q = sem._embed(embed, [sem.QUERY_PREFIX + prompt])[0]
        return [float(_VECS[t] @ q) for t in texts]
    except Exception:  # a model error must never cost the prime: word overlap serves
        return None


def pref_rows(vault: Vault, prompt: str, served: dict, today: str, pinned: list[str],
              withheld: set[str] = frozenset(), allow=None, ids: dict | None = None,
              xs: dict | None = None) -> list[list]:
    """The preference and personality lines as prime ranks them for `prompt`, best first:
    [-match, situational?, -served score, "- [Section] line", labelled?, "Section: text", id].
    Never a `pinned` line again, a `withheld` page, a section `allow(page, heading)` (an
    agent profile, access.scope_section) refuses, or an X-ray muted claim; X-ray pins lead.
    `ids` (claim_ids) and `xs` (xray.settings) are read when not given."""
    ids = claim_ids(vault) if ids is None else ids
    xs = xray.settings(vault) if xs is None else xs
    words = re.findall(r"\w+", prompt.lower())
    terms = {t for t in words if len(t) > 2 and t not in STOPWORDS and t not in TASK_WORDS}
    terms |= {x for w in words for x in ABBREVIATIONS.get(w, ())}
    rows = []  # [-prompt match, situational?, -served score, line, labelled, text, id]; stable
    seen = {claims.norm(ln) for ln in pinned}  # a pinned line is not repeated
    for page, keep in (("preferences", lambda h: True),
                       ("personality", lambda h: h in PERSONALITY_HEADINGS["all"])):
        if page in withheld:
            continue
        ok = keep if allow is None else (lambda h, k=keep, p=page: k(h) and allow(p, h))
        for head, _, items in claim_items(vault, page, ok, served, today, ids, xs["mute"]):
            name = head[3:].strip()
            for score, line, lab, cid in items:  # a section name match counts (my presentation)
                bare = line[2:].removesuffix(" (ask)")
                if claims.norm(bare) in seen:
                    continue
                text = f"{name} {line}".lower()
                rows.append([-sum(t in text for t in terms),
                             name.lower() not in GENERAL_SECTIONS, -score,
                             f"- [{name}] {line[2:]}", lab, f"{name}: {bare}", cid])
    # by meaning when this process's embedder is warm: cosine times the tier weight, a line
    # under semantic.MIN_COS counting as no match; else by the prompt words a line shares
    if rows and (sims := similarity(prompt, [r[5] for r in rows])) is not None:
        floor = sys.modules["cortex.semantic"].MIN_COS
        for r, s in zip(rows, sims, strict=True):
            r[0] = s * r[2] / 0.5 if s >= floor else 0  # r[2] / 0.5: -1, or -ASK_WEIGHT
    for r in rows:  # the owner's X-ray pins lead, still within the preferences cap
        if r[6] in xs["pin"]:
            r[0] = float("-inf")
    rows.sort(key=lambda r: r[:3])
    return rows


def core(vault: Vault, prompt: str, events: list[dict], now: datetime,
         withheld: set[str] = frozenset(), xr: dict | None = None, allow=None
         ) -> tuple[list[str], list[int]]:
    """cortex_prime's blocks before Related, and the budget left: (parts, [chars left]).
    `withheld`: bare ids of me/ pages left out (a non-owner server or an agent profile);
    `allow(page, heading)`: the preference and personality sections an agent profile allows
    (None: all). The X-ray's pinned claims lead Preferences and muted ones are left out; `xr`,
    if given, gets the `served` and `trimmed` ids by block (cortex.xray)."""
    prompt = prompt[:PRIME_PROMPT]
    parts: list[str] = []
    left = [PRIME_MAX]
    put = putter(parts, left)
    served = served_claims(vault, events)
    xs, ids = xray.settings(vault), claim_ids(vault)
    waits = held(vault, events, local_day(now))  # R3-3: as muted while its pair waits
    xs = {**xs, "mute": xs["mute"] | waits}
    if pinned := core_lines(vault, served, local_day(now), withheld, ids, xs["mute"]):
        put(CORE_HEAD, "\n".join(pinned), "me/core", PRIME_MAX)  # first and whole, never shrunk
    # the core is the user's own pick of preferences, so it comes out of their cap
    pref_cap = max(0, PRIME_CAPS["preferences"] - (PRIME_MAX - left[0]))
    summary = "" if "profile" in withheld else profile_summary(vault, local_day(now), served,
                                                               waits)
    if summary:
        put("Profile", summary, "me/profile", PRIME_CAPS["profile"])
    rows = pref_rows(vault, prompt, served, local_day(now), pinned, withheld, allow,
                     ids, xs)
    # the legend only when an (ask) line can be served: the Preferences fit below has no more
    # room than this one, so it serves a prefix of these lines
    asks = {r[3] for r in rows if r[4]}
    shown = fit([r[3] for r in rows], "me", pref_cap, left[0])
    if asks.intersection(shown) or " (ask)" in summary:
        put("How to read the claims", LEGEND)
    cap, before = pref_cap, left[0]
    if problem := named_problem(vault, prompt):
        put(ATTEMPT_HEAD, attempt_line(problem))
        cap -= before - left[0]  # budget-neutral: the line comes out of the preferences cap
    if prefs := fit([r[3] for r in rows], "me", cap, left[0], "preference"):
        put("Preferences", "\n".join(prefs), "me", cap,
            more(len(rows) - len(prefs), "preference"))
    elif n := len(withheld & {"preferences", "personality"}):
        put("Preferences", WITHHELD.format(n=n))  # no "not recorded" nag: /cortex home asks
    if "teaching-contract" not in withheld and (rules := contract_core(contract(vault, set()))):
        # whole rules that fit, else the first one cut by put
        lines = fit(rules, "me/teaching-contract", PRIME_CAPS["contract"], left[0],
                    "contract") or rules[:1]
        put("Teaching contract (core)", "\n".join(lines), "me/teaching-contract",
            PRIME_CAPS["contract"], more(len(rules) - len(lines), "contract"))
    if xr is not None:
        heads = {b.split("\n", 1)[0][3:] for b in parts}
        xr["served"] = {"core": [c for (p, _, t), c in ids.items()
                                 if p == "core" and f"- {t}" in pinned],
                        "profile": ["me/profile"] * ("Profile" in heads),
                        "preferences": [r[6] for r in rows[:len(prefs)] if r[6]],
                        "contract": ["me/teaching-contract"] * (
                            "Teaching contract (core)" in heads)}
        xr["trimmed"] = {"preferences": [r[6] for r in rows[len(prefs):] if r[6]]}
    return parts, left
