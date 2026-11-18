"""The light part of cortex_prime: Profile, claim legend, Preferences and the teaching-contract
core, built from a Vault and the event log alone. It never imports the `mcp` SDK, numpy or
`cortex.semantic` and never builds the Index, so the `cortex prime-hook` process stays fast on
a large vault; the MCP server adds the Related block on top (mcp_server.Cortex.cortex_prime).
"""

from __future__ import annotations

import re
from datetime import datetime

from cortex import bets, claims, probes, surprisal
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
# PHASE3 H3: served claim labels, ranking and the average-pull line
LEGEND = "(ask) = check with the user before relying on it."
PULL_LINE = "In {area}, my guesses drift to the typical student; ask instead."
PULL_MIN, PULL_MISSES, ASK_WEIGHT = 0.6, 5, 0.3
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


def profile_summary(vault: Vault, today: str) -> str:
    """The `summary:` of me/profile.md, else its first paragraph, without lines past their
    `(until …)` day on local day `today` (agenda 4); "" when absent. The init template's
    summary carries no fact, so its live claim lines stand in for it."""
    profile = me(vault, "profile")
    summary = str(profile.fields.get("summary") or "").strip() if profile else ""
    if summary == PROFILE_TEMPLATE:
        text = "\n".join(claims.strip_id(ln) for sec in _sections(profile.body)
                         if sec.partition("\n")[0][3:].strip().lower() not in claims.RESERVED
                         for ln in sec.splitlines()[1:] if ln.startswith("- "))
    else:
        text = summary or (_first_paragraph(profile.body) if profile else "")
    return "\n".join(ln for ln in text.splitlines() if not claims.expired(ln, today)).strip()


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


def served_claims(vault: Vault, events: list[dict], now: datetime) -> tuple[dict, list[str]]:
    """PHASE3 H3: {(page, section, text): (label, score)} for each live claim, and the probe
    areas whose guesses drift to the typical student. The user's own lines (typed, legacy,
    confirmed) are served plain whatever their bet state; (ask) for recheck or contested, agent
    or exposed, or a yes the model relayed from claims.RELAYED_FROM."""
    cards, bits = bets.fold(events, local_day(now), now), surprisal.bits(events)
    out = {}
    for r in claims.tiers(vault, events):
        b = cards.get(r["id"]) or bets.Bet()
        label = " (ask)" if (b.state in ("recheck", "contested") or claims.late_relay(r)
                             or r["tier"] in ("agent", "exposed")) else ""
        score = bits.get(r["id"], 1.0) * (0.5 if b.R is None else b.R)
        out[r["page"], r["section"], r["text"]] = (label, score * (
            ASK_WEIGHT if label == " (ask)" else 1))
    pull = [a for a, row in probes.summary(events)["areas"].items()
            if row.get("pull", 0) > PULL_MIN and row.get("misses", 0) >= PULL_MISSES]
    return out, pull


def claim_items(vault: Vault, page: str, keep, served: dict, today: str) -> list[tuple]:
    """Per kept `## ` section of me/<page> (heading line, other lines, [(score, labelled list
    line, labelled?)] best first); never `## Past`, `## Not me` or a line whose own
    `(until …)` day is before `today` (agenda 4)."""
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
                label, score = served.get(
                    (page, head[3:].strip(), claims.strip_id(ln)[2:].strip()), ("", 0.5))
                items.append((score, claims.strip_id(ln) + label, bool(label)))
            items.sort(key=lambda x: -x[0])  # stable: ties stay newest first
            out.append((head, [ln for ln in lines if not ln.startswith("- ")], items))
    return out


def putter(parts: list[str], left: list[int]):
    """A `put(head, text, id, cap)` that appends a wrapped block to `parts`, within `cap`
    and the remaining budget `left[0]`."""
    def put(head: str, text: str, id: str | None = None, cap: int = CONTEXT_MAX) -> None:
        while True:  # trim until the wrapped block fits (escaping can lengthen the text)
            block = f"## {head}\n" + (_wrap(id, text) if id else text)
            over = len(block) - min(cap, left[0])
            if over <= 0:
                break
            if len(text) - over < 40:
                return
            text = text[: len(text) - over - 1].rstrip() + "…"
        parts.append(block)
        left[0] -= len(block) + 2
    return put


def fit(lines: list[str], id: str, cap: int, left: int) -> list[str]:
    """`lines` minus the last ones until their wrapped block fits `cap` and `left`."""
    lines = list(lines)
    while lines and len(_wrap(id, "\n".join(lines))) + 40 > min(cap, left):
        lines.pop()  # 40: room for the "## <head>\n" line
    return lines


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


def core(vault: Vault, prompt: str, events: list[dict], now: datetime,
         withheld: set[str] = frozenset()) -> tuple[list[str], list[int]]:
    """cortex_prime's blocks before Related, and the budget left: (parts, [chars left]).
    `withheld`: bare ids of me/ pages left out (a non-owner server)."""
    prompt = prompt[:PRIME_PROMPT]
    parts: list[str] = []
    left = [PRIME_MAX]
    put = putter(parts, left)
    if "profile" not in withheld and (summary := profile_summary(vault, local_day(now))):
        put("Profile", summary, "me/profile", PRIME_CAPS["profile"])
    served, pull = served_claims(vault, events, now)
    words = re.findall(r"\w+", prompt.lower())
    terms = {t for t in words if len(t) > 2 and t not in STOPWORDS and t not in TASK_WORDS}
    terms |= {x for w in words for x in ABBREVIATIONS.get(w, ())}
    rows = []  # (-prompt words matched, situational?, -served score, line, labelled); stable
    for page, keep in (("preferences", lambda h: True),
                       ("personality", lambda h: h in PERSONALITY_HEADINGS["all"])):
        if page in withheld:
            continue
        for head, _, items in claim_items(vault, page, keep, served, local_day(now)):
            name = head[3:].strip()
            for score, line, lab in items:  # a section name match counts (my presentation)
                text = f"{name} {line}".lower()
                rows.append((-sum(t in text for t in terms),
                             name.lower() not in GENERAL_SECTIONS, -score,
                             f"- [{name}] {line[2:]}", lab))
    rows.sort(key=lambda r: r[:3])
    # the legend only when an (ask) line can be served: the Preferences fit below has no more
    # room than this one, so it serves a prefix of these lines
    asks = {r[3] for r in rows if r[4]}
    shown = fit([r[3] for r in rows], "me", PRIME_CAPS["preferences"], left[0])
    note = ([LEGEND] if asks.intersection(shown) else []) + [
        PULL_LINE.format(area=a) for a in pull]
    if note:
        put("How to read the claims", "\n".join(note))
    cap, before = PRIME_CAPS["preferences"], left[0]
    if problem := named_problem(vault, prompt):
        put(ATTEMPT_HEAD, attempt_line(problem))
        cap -= before - left[0]  # budget-neutral: the line comes out of the preferences cap
    if lines := fit([r[3] for r in rows], "me", cap, left[0]):
        put("Preferences", "\n".join(lines), "me", cap)
    elif n := len(withheld & {"preferences", "personality"}):
        put("Preferences", WITHHELD.format(n=n))  # no "not recorded" nag: /cortex home asks
    if "teaching-contract" not in withheld and (rules := contract_core(contract(vault, set()))):
        # whole rules that fit, else the first one cut by put
        lines = fit(rules, "me/teaching-contract", PRIME_CAPS["contract"], left[0]) or rules[:1]
        put("Teaching contract (core)", "\n".join(lines), "me/teaching-contract",
            PRIME_CAPS["contract"])
    return parts, left
