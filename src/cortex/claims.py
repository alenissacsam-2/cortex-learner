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
_MARK = re.compile(r"\s*\((?:confirmed|proposed|rejected|until)\b[^)]*\)")
_UNTIL = re.compile(r"\s*\(until (\d{4}-\d\d-\d\d)\s*\)")  # a live line's own expiry (agenda 4)
_H2 = re.compile(r"^##[ \t]+(.*?)[ \t]*$")
_TOP = re.compile(r"^#{1,2}[ \t]")  # a heading that ends a ## section


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

