"""Who may see what: the single place Cortex's privacy rules are stated
(docs/design/v7-review/SPEC-privacy-gate.md §1-2).

Every output asks one question, `may_see(viewer, audience, kind)`. A record's audience comes
from the `audience_of_*` helpers, which delegate to the existing rules (`vault.is_private`,
`planner.hidden`, `browse.privacy.audience`) instead of restating them. Nothing else may
re-implement these rules; new outputs call in here. Pure functions, no I/O.

Owner rules (fixed): Claude and cloud apps never see private notes, private subjects, private
mistakes or local-only browsing; the local model may see everything within Chat's switches; a
terminal counts as "maybe an AI app".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum

from cortex.browse import privacy
from cortex.vault import Note, is_private


class Audience(StrEnum):
    """SKIP: never stored or shown. LOCAL: local models (and the owner at a private terminal)
    only. CLOUD: anyone the owner lets read Cortex. Values match `browse.privacy.audience`."""
    SKIP = "skip"
    LOCAL = "local"
    CLOUD = "cloud"


KINDS = ("note", "chat", "browse", "study")
VIEWERS = ("mcp", "dashboard", "page_public", "today_md", "start_card", "terminal",
           "chat_local", "cloud_model", "app_home")
# Chat's switch (ask.SOURCES) for each kind's CLOUD records; studying sits under "notes"
KIND_SWITCH = {"note": "notes", "chat": "chats", "browse": "browsing", "study": "notes"}
_CLOUD_ONLY = ("mcp", "dashboard", "page_public", "today_md", "start_card")


def may_see(viewer: str, audience: Audience | str, kind: str,
            switches: Mapping[str, bool] | None = None, *, private_flag: bool = False,
            tty: bool = False, cloud_opt_in: bool = False) -> bool:
    """May `viewer` see a `kind` record of `audience`? SKIP: nobody. The other cells:

    - mcp, dashboard, page_public, today_md, start_card: CLOUD only.
    - terminal: CLOUD; LOCAL only with `private_flag` (`--private`) and `tty` (stdin and
      stdout are both a TTY; the caller checks them).
    - chat_local: LOCAL under `switches["private"]`; CLOUD under the kind's switch
      (KIND_SWITCH). `switches` None counts as every switch off.
    - cloud_model: CLOUD only, and only when `cloud_opt_in` (the user opted this data in).
    - app_home: the app's home page; CLOUD, and LOCAL only with `private_flag` (the app's
      Private switch is unlocked).

    An unknown viewer, audience or kind raises ValueError: a new one must be added here."""
    audience = Audience(audience)
    if viewer not in VIEWERS:
        raise ValueError(f"unknown viewer {viewer!r}")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if audience is Audience.SKIP:
        return False
    if viewer in _CLOUD_ONLY:
        return audience is Audience.CLOUD
    if viewer == "terminal":
        return audience is Audience.CLOUD or (private_flag and tty)
    if viewer == "app_home":
        return audience is Audience.CLOUD or private_flag
    if viewer == "chat_local":
        switches = switches or {}
        key = "private" if audience is Audience.LOCAL else KIND_SWITCH[kind]
        return bool(switches.get(key))
    return audience is Audience.CLOUD and cloud_opt_in  # cloud_model


def audience_of_note(note: Note, hidden: Iterable[str] | None = None) -> Audience:
    """A note or page, or an imported AI chat (owner: like a note). LOCAL when
    `vault.is_private` says so or its id is in `hidden` (`planner.hidden`: private subjects
    and what sits under them), else CLOUD."""
    return Audience.LOCAL if is_private(note) or note.id in (hidden or ()) else Audience.CLOUD


def audience_of_browse(category: str | None, settings: privacy.Settings, *,
                       host: str = "", private: bool = False) -> Audience:
    """A browse item or site: its category's audience (`browse.privacy.audience`), with a
    `private_domains` host or an item already marked private (`Kept.private` or `Kept.local`)
    LOCAL. A skipped category stays SKIP whatever the host."""
    a = Audience(privacy.audience(category, settings))
    if a is Audience.CLOUD and (private or (host and privacy.in_domains(host, settings.private))):
        return Audience.LOCAL
    return a


def audience_of_study(subjects: Iterable[str], hidden: Iterable[str], *,
                      note_private: bool = False) -> Audience:
    """A study item (concept, mistake, assessment): LOCAL when any of its subject ids is in
    `hidden` (`planner.hidden`) or its own note is private (`note_private`), else CLOUD."""
    hidden = set(hidden)
    return Audience.LOCAL if note_private or any(s in hidden for s in subjects) \
        else Audience.CLOUD


# ---- per-agent profiles (plan v9 item 8) ----------------------------------------------------
# A profile (cortex.profiles) names the context scopes an MCP client may get; None or "all" is
# everything. A me/ page sits under one scope whole, except preferences and personality, whose
# `## ` sections each sit under one (the cortex_context scopes: a preferences section is named
# after its scope). Journal and people pages are personal; project pages are projects.
SCOPES = ("all", "core", "coding", "communication", "learning", "projects", "environment",
          "career", "personal")
SECTIONED = ("preferences", "personality")
# me/ pages outside SECTIONED: their scope (the teaching contract is for tutors, not coding
# agents); a page not listed is "personal"
PAGE_SCOPE = {"core": "core", "teaching-contract": "learning"}
PERSONALITY_SCOPE = {"communication": "communication", "how i think": "learning"}
KIND_SCOPE = {"journal": "personal", "person": "personal", "project": "projects"}


def _all(scopes) -> bool:
    return scopes is None or "all" in scopes


def scope_page(scopes, page: str) -> bool:
    """May a client with `scopes` get me/<page>? A sectioned page: some of it (scope_section)."""
    if _all(scopes) or page in SECTIONED:
        return True
    need = PAGE_SCOPE.get(page, "personal")
    return need is None or need in scopes


def scope_section(scopes, page: str, heading: str) -> bool:
    """May a client with `scopes` get the `## heading` section of me/<page>?"""
    if _all(scopes):
        return True
    h = heading.strip().lower()
    if page == "personality":
        return PERSONALITY_SCOPE.get(h) in scopes
    if page == "preferences":
        return h in SCOPES[2:] and h in scopes
    return scope_page(scopes, page)


def scope_kind(scopes, kind: str) -> bool:
    """May a client with `scopes` get vault pages of `kind` (journal and people: personal;
    project: projects)?"""
    return _all(scopes) or KIND_SCOPE.get(kind) is None or KIND_SCOPE[kind] in scopes
