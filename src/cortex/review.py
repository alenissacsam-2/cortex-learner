"""Plan 5a: the Cortex app's Review view, read-only. What waits for the user's own click:

- pending: the lines under `## About me (unconfirmed)` in inbox.md (about-me lines and
  teaching-contract rules), each with Approve and Dismiss;
- expired: live claim lines past their own `(until …)` day (Retire);
- stale: confirmed lines not reconfirmed or rewritten in STALE_DAYS (Still true or Retire),
  at most STALE_PER_MONTH a month, oldest first.
Contradictions and repeated corrections come in a later task, as a fourth list in `view`.

The app's handlers (app/server.py) write through `mcp_server.Cortex` with `app_clicks` set,
so every write is cortex_patch's or cortex_log's own code path."""

from __future__ import annotations

import datetime as dt
import hashlib
import re

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


def view(vault, events, today: str) -> dict:
    """The Review view and its badge `count`: every text is shown text (`_shown`)."""
    events = list(events)
    hidden = _private(vault)
    rows = [r for r in claims.tiers(vault, events) if r["page"] not in hidden]  # writes nothing
    expired = [{"page": r["page"], "id": r["id"], "text": _shown(r["text"]), "until": until}
               for r in rows if (until := claims.expired(r["text"], today))]
    waiting = [{**p, "section": plain(p["section"], 80), "text": _shown(p["text"])}
               for p in pending(vault, today)]
    old = stale(rows, events, today)
    return {"pending": waiting, "expired": expired, "stale": old,
            "count": len(waiting) + len(expired) + len(old)}
