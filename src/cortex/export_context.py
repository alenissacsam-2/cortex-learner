"""`cortex export context` (v6 agenda 7): the prime core as a plain Markdown snapshot that the
user can paste into other assistants (Gemini, Codex, Cursor). It reuses prime's selection
(cortex.prime) with as_me's act-grade rule: only live claims of tier typed or confirmed and not
labelled (ask), so never `## Past`, `## Not me`, inbox proposals, agent or exposed lines, or a
yes the model relayed from claims.RELAYED_FROM; lines carry no label. Profile is me/profile's
`summary:` field, else its act-grade claim lines. Private me/ pages and any line matching a
secret pattern are left out. It writes nothing in the vault.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from cortex import claims, prime, render, taint
from cortex.events import local_day
from cortex.vault import Vault, VaultError, is_private, secret_pattern

MAX_CHARS = 6000
LINE_MAX = 300
REPO = Path(__file__).resolve().parents[2]  # the Cortex code checkout (src/cortex/..)
HEADER = ("# About me (Cortex snapshot, {day})\n\n"
          "Exported from my Cortex vault on {day}; it does not update. It holds only lines I "
          "confirmed.")


def _clean(text: str, limit: int = LINE_MAX) -> str:
    """One plain line (no links, images, URLs, HTML or `(confirmed …)` markers), or "" when it
    looks like it holds a secret."""
    if secret_pattern(text):
        return ""
    s = render.plain(claims._MARK.sub("", text), limit)
    return "" if secret_pattern(s) else s


def _page_ok(vault: Vault, page: str) -> bool:
    note = prime.me(vault, page)
    return note is not None and not is_private(note)


def build(vault: Vault, events: list[dict], now: datetime) -> tuple[str, int]:
    """(the Markdown snapshot within MAX_CHARS, how many live claim lines were left out as not
    act-grade)."""
    served = prime.served_claims(vault, events)
    keeps = {"preferences": lambda h: True, "setup": lambda h: True,
             "personality": lambda h: h in prime.PERSONALITY_HEADINGS["all"],
             "goals": lambda h: h == "now", "profile": lambda h: True}
    rows: dict[str, list] = {"preferences": [], "goals": [], "profile": []}
    skipped = 0
    for i, r in enumerate(claims.tiers(vault, events)):  # live claims only
        keep = keeps.get(r["page"])
        if (keep is None or not keep(r["section"].lower()) or not _page_ok(vault, r["page"])
                or claims.expired(r["text"], local_day(now))):  # agenda 4
            continue
        label, score = served.get((r["page"], r["section"], r["text"]), ("", 0.5))
        if not claims.act_grade(r, label):
            skipped += 1
            continue
        if (text := _clean(r["text"])) and r["page"] == "profile":
            rows["profile"].append(f"- {text}")  # page order
        elif text:
            line = (f"- {text}" if r["page"] == "goals"
                    else f"- [{render.plain(r['section'], 60)}] {text}")
            rows["goals" if r["page"] == "goals" else "preferences"].append((-score, -i, line))
    sections = []
    if _page_ok(vault, "profile"):  # its summary: field, else its act-grade claim lines only
        summary = str(prime.me(vault, "profile").fields.get("summary") or "")
        summary = "" if summary.strip() == prime.PROFILE_TEMPLATE else summary  # no fact in it
        summary = "" if claims.expired(summary, local_day(now)) else _clean(
            claims._ANY_ID.sub("", summary), 600)
        sections.append(("Profile", [summary] if summary else rows["profile"]))
    if _page_ok(vault, "teaching-contract"):
        rules = [s for s in map(_clean, prime.contract_core(prime.contract(vault, set()))) if s]
        sections.append(("How to help me", rules))
    sections.append(("Current goals", [x[-1] for x in sorted(rows["goals"])]))
    sections.append(("Preferences", [x[-1] for x in sorted(rows["preferences"])]))
    return _fit(HEADER.format(day=local_day(now)), sections), skipped


def _fit(header: str, sections: list[tuple[str, list[str]]]) -> str:
    """Header plus non-empty sections, dropping whole lines from the end (the weakest
    preferences first) until the text is within MAX_CHARS."""
    secs = [(h, list(ls)) for h, ls in sections if ls]

    def text() -> str:
        return "\n\n".join([header, *(f"## {h}\n" + "\n".join(ls) for h, ls in secs)]) + "\n"

    while secs and len(text()) > MAX_CHARS:
        secs[-1][1].pop()
        if not secs[-1][1]:
            secs.pop()
    return text()


def check_target(path: Path, vault: Vault) -> Path:
    """The resolved output path; refused inside the vault, the Cortex repo or an `exclude:`
    folder, since the file holds the user's profile."""
    p = path.expanduser().resolve()
    for root, what in ((vault.root.resolve(), "the vault"), (REPO, "the Cortex repo")):
        if p == root or root in p.parents:
            raise VaultError("invalid_argument", f"Refusing to write inside {what}; pick a "
                             "path outside it, or omit it to print to stdout.")
    if folder := taint.excluded(p.parent):
        raise VaultError("invalid_argument", f"{folder} is excluded in ~/.cortex/config.yaml; "
                         "refusing to write your profile there.")
    return p
