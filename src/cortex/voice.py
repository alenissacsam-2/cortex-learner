"""Voice (v5 PHASE4.md V1): the user's own messages (samples), one JSON row per line in
`voice/samples.jsonl` (an old vault may also hold `voice/drafts.jsonl`; only `people.forget`
still reads it). `voice/` is no kind folder, so the index, recall, vectors, the dashboard,
`index.md` and lint never see it. Writes go through `cortex.vault`; `id` and `day` are stamped
here, never taken from input."""

from __future__ import annotations

import json
import re
from datetime import datetime

from ulid import ULID

from cortex.events import local_day
from cortex.vault import invalid, scan_secrets

ACTS = ("request", "reply", "thanks", "apology", "update", "decline", "intro", "other")
CHANNELS = ("email", "chat", "sms", "social", "ai", "other")  # ai: typed to an AI, never voice
AUDIENCE_RE = re.compile(r"[a-z0-9-]{1,40}|people/[a-z0-9][a-z0-9.-]{0,80}")
LANG_RE = re.compile(r"[a-z]{2,3}(-[a-z0-9]{2,8})?")
TEXT_MAX = 4000
SAMPLES = "voice/samples.jsonl"
SPLIT_RE = re.compile(r"^---[ \t]*$", re.M)


def check_audience(audience) -> str:
    if not isinstance(audience, str) or not AUDIENCE_RE.fullmatch(audience):
        raise invalid("audience must be a short tag ([a-z0-9-], up to 40, e.g. professor) or a "
                      "person id people/<slug>.")
    return audience


def _text(text) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX:
        raise invalid(f"text must be the message itself, 1 to {TEXT_MAX:,} characters.")
    scan_secrets(text)
    return text


def fields(p: dict) -> dict:
    """The validated `{audience, act, channel, lang?, text}` of a sample."""
    if p.get("act") not in ACTS:
        raise invalid(f"act must be one of {', '.join(ACTS)}.")
    if p.get("channel") not in CHANNELS:
        raise invalid(f"channel must be one of {', '.join(CHANNELS)}.")
    out = {"audience": check_audience(p.get("audience")), "act": p["act"],
           "channel": p["channel"]}
    if p.get("lang") is not None:
        if not isinstance(p["lang"], str) or not LANG_RE.fullmatch(p["lang"]):
            raise invalid("lang must be a language tag such as en or pt-br.")
        out["lang"] = p["lang"]
    return {**out, "text": _text(p.get("text"))}


def rows(vault, name: str) -> list[dict]:
    """The rows of voice/<name>, oldest first; a line that is not a JSON object is skipped."""
    p = vault.root / name
    out = []
    for line in (p.read_bytes().decode("utf-8").splitlines() if p.is_file() else ()):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def add(vault, name: str, items: list[dict], now: datetime) -> list[str]:
    """Append `items` to voice/<name> with server ids and day; returns the ids."""
    base, day = int(ULID.from_datetime(now)), local_day(now)
    new = [{"id": str(ULID.from_int(base + i)), "day": day, **it} for i, it in enumerate(items)]
    path = vault.root / name
    with vault.lock():
        old = path.read_bytes().decode("utf-8") if path.is_file() else ""
        vault.write_text(path, old + "".join(json.dumps(r, ensure_ascii=False) + "\n"
                                             for r in new))
    return [r["id"] for r in new]


def import_text(vault, text: str, audience: str, act: str, channel: str,
                now: datetime) -> int:
    """`cortex voice import`: messages separated by lines of `---`, all or none."""
    items = [fields({"audience": audience, "act": act, "channel": channel, "text": m.strip()})
             for m in SPLIT_RE.split(text.replace("\r\n", "\n")) if m.strip()]
    return len(add(vault, SAMPLES, items, now)) if items else 0


def exemplars(vault, audiences: set[str], act: str | None, channel: str | None,
              n: int = 3) -> list[dict]:
    """Up to `n` samples scored audience 2, act 1, channel 0.5 (newest first on ties), never
    `channel: ai` and never a sample that matches nothing."""
    scored = []
    for i, s in enumerate(rows(vault, SAMPLES)):
        if s.get("channel") == "ai" or not isinstance(s.get("text"), str):
            continue
        score = (2 * (s.get("audience") in audiences) + (s.get("act") == act)
                 + 0.5 * (s.get("channel") == channel))
        if score:
            scored.append((-score, -i, s))
    return [s for *_, s in sorted(scored, key=lambda x: x[:2])[:n]]
