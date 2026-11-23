"""What other agents get (plan v9 item 8): the Cortex block Connect writes into the Cursor,
VS Code (Copilot) and Codex rule files (`rule_block`), and the context packs the owner pastes
into web chats with no MCP (`pack`; `cortex pack`, the app's Connect section): General, Coding
and Writing, and the role packs Interview, Research and Review, each at 1,500 or 5,000
characters. R32a: the app records each Copy (`copied` folds them) so Connect can say a pack
changed since.

Built by code from non-private me/ pages only: the pinned core first (prime.core_lines), then
the pack's preference and personality lines in prime's order for an empty prompt
(prime.pref_rows), only lines the user wrote or confirmed (never an (ask) line), whole lines
within the cap. A line naming a hidden subject (planner.hidden: its id or title) is left out.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from cortex import access, planner, prime, xray
from cortex.catalog import Catalog
from cortex.events import EventLog, live, local_day
from cortex.vault import Vault, is_private

SIZES = (1500, 5000)  # characters: the custom-instructions boxes people paste into differ
# each pack's scopes (access.scope_section); None: every section
PACKS = {"general": None, "coding": frozenset(("core", "coding", "environment")),
         "writing": frozenset(("core", "communication")),
         "interview": frozenset(("core", "career", "communication")),
         "research": frozenset(("core", "learning", "projects", "communication")),
         "review": frozenset(("core", "coding", "communication"))}
PACK_HEAD = ("About me, from my own notes (Cortex). Use it to tailor your answers; ask me when "
             "something seems out of date.")
PACK_FOR = {"interview": "For a job interview practice chat.",
            "research": "For a research collaboration chat.",
            "review": "For a code review chat."}
PRIME_LINE = ("Call cortex_prime(prompt=<the user's message>) first when the cortex MCP tools "
              "are available.")
BLOCK_HEAD = "## Cortex\nAbout me (my pinned core, from Cortex):"


def _hidden_words(vault: Vault) -> list[re.Pattern]:
    cat = Catalog.load(vault)
    hide = planner.hidden(vault, cat)
    words = {*hide, *(cat.entries[i].title for i in hide if i in cat.entries)}
    return [re.compile(rf"(?<!\w){re.escape(w.lower())}(?!\w)") for w in words if len(w) >= 4]


def lines(vault: Vault, scopes, now: datetime | None = None) -> tuple[list[str], list[str]]:
    """(core lines, preference lines) for a pack of `scopes` (None: all)."""
    now = now or datetime.now(UTC)
    today = local_day(now)
    events = list(EventLog(vault).iter_events())
    served = prime.served_claims(vault, events)
    private = {p for p in ("core", "setup", *access.SECTIONED)
               if (n := prime.me(vault, p)) is not None and is_private(n)}
    # as prime.core: X-ray muted lines, and R3-3 held ones while their pair waits, stay out
    xs, ids = xray.settings(vault), prime.claim_ids(vault)
    xs = {**xs, "mute": xs["mute"] | prime.held(vault, events, today)}
    core = prime.core_lines(vault, served, today, private, ids, xs["mute"])
    allow = None if scopes is None else (lambda p, h: access.scope_section(scopes, p, h))
    prefs = [r[3] for r in prime.pref_rows(vault, "", served, today, core, private, allow,
                                           ids, xs) if not r[4]]
    bad = _hidden_words(vault)
    ok = [[ln for ln in got if not any(b.search(ln.lower()) for b in bad)] for got in (core, prefs)]
    return ok[0], ok[1]


def sized(vault: Vault, name: str, now: datetime | None = None) -> dict[int, str]:
    """The `name` pack (PACKS) at each of SIZES, its lines read once: {size: text}; "" when
    nothing is recorded."""
    if name not in PACKS:
        raise ValueError(f"pack must be one of {', '.join(PACKS)}")
    core, prefs = lines(vault, PACKS[name], now)
    return {size: _cut(name, core, prefs, size) for size in SIZES}


def _cut(name: str, core: list[str], prefs: list[str], size: int) -> str:
    out = [PACK_HEAD, *([PACK_FOR[name]] if name in PACK_FOR else [])]
    first = len(out)
    for title, got in (("Core (always true):", core), ("Preferences:", prefs)):
        head = ["", title]
        for ln in got:  # whole lines: one that does not fit is dropped, a shorter one may fit
            if len("\n".join([*out, *head, ln])) <= size:
                out += [*head, ln]
                head = []
    return "\n".join(out) if len(out) > first else ""


def pack(vault: Vault, name: str, size: int = SIZES[0], now: datetime | None = None) -> str:
    """The `name` pack (PACKS), at most `size` (one of SIZES) characters; "" when nothing is
    recorded."""
    if size not in SIZES:
        raise ValueError(f"size must be one of {', '.join(map(str, SIZES))}")
    return sized(vault, name, now)[size]


def digest(text: str) -> str:
    """The hash Connect compares: sha256 hex of the exact text served, its first 16 chars."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def copied(events) -> dict[tuple[str, int], str]:
    """A fold over events: the last copied hash per (pack, size), from the app's Copy."""
    out = {}
    for e in live(events):
        pl = e.get("payload")
        if e.get("kind") == "log" and isinstance(pl, dict) and pl.get("kind") == "pack_copied":
            out[(pl.get("pack"), pl.get("chars"))] = pl.get("hash")
    return out


def rule_block(vault: Vault | None, now: datetime | None = None, scopes=None) -> str:
    """The Markdown block for agent rule files: the pinned core and the prime line. `scopes`:
    the app's profile (cortex.profiles; None: all); without `core` in it, the prime line only."""
    core = lines(vault, frozenset(("core",)), now)[0] \
        if vault is not None and access.scope_page(scopes, "core") else []
    # a line may not close the block's HTML-comment markers early
    core = [ln.replace("<!--", "<!-").replace("-->", "->") for ln in core]
    return "\n".join([BLOCK_HEAD, *core] if core else ["## Cortex"]) + "\n" + PRIME_LINE + "\n"
