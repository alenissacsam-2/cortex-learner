"""Letters for the bad day (v5 PHASE2.md G4): the user's own lines in me/letters.md, each with a
`when` trigger; on a day the trigger holds, cortex_start shows one.

Reading and the folds are pure; `write` and `retire` go through cortex.vault.
"""

from __future__ import annotations

import datetime as dt
import re

from cortex import claims, levels, planner
from cortex.events import live
from cortex.render import esc
from cortex.vault import Vault, VaultError, invalid

REL = "me/letters.md"
LIVE, PAST = "Letters", "Past"
TEXT_MIN, TEXT_MAX, WHEN_MAX = 5, 300, 120
COOLDOWN_DAYS = 3
VERDICTS = ("helped", "not", "retire")
# trigger -> allowed n range (None: takes no n)
TRIGGERS = {"late": None, "energy_low": (1, 7), "exam_within": (1, 14), "gap_days": (2, 60),
            "miss_streak": (2, 10)}
NEW_PAGE = '---\nid: "letters"\ntitle: "Letters"\n---\n# Letters\n\n## Letters\n'
_LINE = re.compile(r'^- "(?P<text>.*)" \| when: (?P<when>\S+) \| written '
                   r"(?P<written>\d{4}-\d{2}-\d{2})(?: \(retired [\d-]+\))? "
                   r"\^(?P<id>l-[a-z2-7]{5})\s*$")
_ENERGY = re.compile(r"· energy ([1-5])\b")


def parse_when(when) -> list[tuple[str, int | None]]:
    """`late+energy_low:2` -> [("late", None), ("energy_low", 2)]; anything else is refused."""
    if not isinstance(when, str) or not when.strip() or len(when) > WHEN_MAX:
        raise invalid("letter needs payload.when: triggers joined by +, e.g. late+energy_low:2.")
    out = []
    for part in when.strip().split("+"):
        name, colon, n = part.strip().partition(":")
        if name not in TRIGGERS:
            raise invalid(f"Unknown trigger '{part}'; use {', '.join(TRIGGERS)}.")
        rng = TRIGGERS[name]
        if rng is None and colon:
            raise invalid("late takes no number.")
        if rng is not None and not (n.isdigit() and rng[0] <= int(n) <= rng[1]):
            raise invalid(f"{name} needs :n with n from {rng[0]} to {rng[1]}.")
        out.append((name, None if rng is None else int(n)))
    return out


def read(vault: Vault) -> str:
    p = vault.root / REL
    return p.read_bytes().decode("utf-8") if p.is_file() else ""


def parse(text: str) -> list[dict]:
    """{id, text, when, written, live, line_no} per letter line; other lines are ignored."""
    out, section = [], None
    for no, line in enumerate(text.splitlines()):
        if m := claims._H2.match(line):
            section = m.group(1).lower()
        elif section in (LIVE.lower(), PAST.lower()) and (m := _LINE.match(line)):
            out.append({**m.groupdict(), "live": section == LIVE.lower(), "line_no": no})
    return out


def write(vault: Vault, text, when, day: str) -> str:
    """Append `- "<text>" | when: <when> | written <day> ^l-xxxxx` under ## Letters."""
    if not isinstance(text, str):
        raise invalid("letter needs payload.text.")
    text = re.sub(r"\s*[\r\n]+\s*", " ", text).strip()  # newlines collapsed, nothing else
    if not TEXT_MIN <= len(text) <= TEXT_MAX:
        raise invalid(f"letter text must be {TEXT_MIN} to {TEXT_MAX} characters.")
    when = "+".join(n if v is None else f"{n}:{v}" for n, v in parse_when(when))
    with vault.lock():
        page = read(vault) or NEW_PAGE
        nl = "\r\n" if "\r\n" in page else "\n"
        lid = claims.make_id("letters", text, day, {r["id"] for r in parse(page)}, prefix="l")
        lines = page.splitlines(keepends=True)
        claims._append_under(lines, LIVE, f'- "{text}" | when: {when} | written {day} ^{lid}', nl)
        vault.write_text(vault.root / REL, "".join(lines))
    return lid


def retire(vault: Vault, lid: str, day: str) -> None:
    """Move a live letter to ## Past with ` (retired <day>)`; its id is kept."""
    with vault.lock():
        page = read(vault)
        row = next((r for r in parse(page) if r["id"] == lid and r["live"]), None)
        if row is None:
            raise invalid(f"No live letter ^{lid} in {REL}.")
        nl = "\r\n" if "\r\n" in page else "\n"
        lines = page.splitlines(keepends=True)
        old = re.sub(r"\s+\^l-[a-z2-7]{5}\s*$", "", lines.pop(row["line_no"]).rstrip("\r\n"))
        claims._append_under(lines, PAST, f"{old} (retired {day}) ^{lid}", nl)
        vault.write_text(vault.root / REL, "".join(lines))


def _log(e: dict, kind: str) -> dict | None:
    p = e.get("payload")
    return p if e.get("kind") == "log" and isinstance(p, dict) and p.get("kind") == kind \
        else None


def facts(vault: Vault, events: list[dict], now: dt.datetime, today: str, session: str) -> dict:
    """What the triggers read: the local hour, journal energy values (oldest first), days to
    the next pending assessment, days since the last study event (planner._studied) outside
    `session`, and the verdicts of graded answers (oldest first)."""
    day = dt.date.fromisoformat(today)
    energy = []
    for n in sorted(vault.iter_notes("journal"), key=lambda n: (str(n.fields.get("date")), n.id)):
        energy += [int(v) for v in _ENERGY.findall(n.body)]
    up = planner._upcoming(vault, day)
    before = [str(e.get("day")) for e in events if e.get("session") != session and e.get("day")
              and planner._studied(e)]  # B7: a sweep's close today is no study
    return {"hour": now.astimezone().hour, "energy": energy,
            "exam_days": min((a["days"] for a in up), default=None),
            "gap": (day - dt.date.fromisoformat(max(before))).days if before else None,
            "verdicts": [it.verdict for it in levels._items(events, None)
                         if it.ev.get("kind") == "grade" and it.verdict]}


def holds(name: str, n: int | None, f: dict) -> bool:
    if name == "late":
        return f["hour"] >= 23 or f["hour"] < 4
    if name == "energy_low":
        return len(f["energy"]) >= n and all(v <= 2 for v in f["energy"][-n:])
    if name == "exam_within":
        return f["exam_days"] is not None and f["exam_days"] <= n
    if name == "gap_days":
        return f["gap"] is not None and f["gap"] >= n
    return len(f["verdicts"]) >= n and all(v == "wrong" for v in f["verdicts"][-n:])


def pick(page: str, f: dict, events, today: str) -> dict | None:
    """The letter to show today: none if one was shown today already; else the oldest written
    live letter whose triggers all hold and that was not shown in the last COOLDOWN_DAYS."""
    last: dict[str, str] = {}
    for e in live(events):
        if (p := _log(e, "letter_shown")) is not None:
            if e.get("day") == today:
                return None
            last[str(p.get("letter"))] = max(last.get(str(p.get("letter")), ""), str(e["day"]))
    day = dt.date.fromisoformat(today)
    for r in sorted((r for r in parse(page) if r["live"]), key=lambda r: r["written"]):
        try:
            trig = parse_when(r["when"])
        except VaultError:  # a hand-edited line with a bad trigger never shows
            continue
        seen = last.get(r["id"])
        if seen and (day - dt.date.fromisoformat(seen)).days < COOLDOWN_DAYS:
            continue
        if all(holds(name, n, f) for name, n in trig):
            return r
    return None


def card_line(r: dict) -> str:
    d = dt.date.fromisoformat(r["written"])
    return f'A note from you (written {d.day} {d:%b}): "{esc(r["text"], TEXT_MAX)}"'


def effect(events) -> list[dict]:
    """Per letter: times shown, helped / not feedback, and `followed`: shows after which a
    session started within 24 hours. Counts only."""
    evs = live(events)
    opens = [dt.datetime.fromisoformat(e["ts"]) for e in evs if e.get("kind") == "open"]
    out: dict[str, dict] = {}

    def row(lid) -> dict:
        return out.setdefault(str(lid), {"id": str(lid), "shown": 0, "helped": 0, "not": 0,
                                         "followed": 0})

    for e in evs:
        if (p := _log(e, "letter_shown")) is not None:
            r, t = row(p.get("letter")), dt.datetime.fromisoformat(e["ts"])
            r["shown"] += 1
            r["followed"] += any(t < o <= t + dt.timedelta(hours=24) for o in opens)
        elif (p := _log(e, "letter_feedback")) is not None and p.get("verdict") in ("helped",
                                                                                   "not"):
            row(p.get("letter"))[p["verdict"]] += 1
    return sorted(out.values(), key=lambda r: r["id"])
