"""Voice (v5 PHASE4.md V1): the user's own messages (samples), one JSON row per line in
`voice/samples.jsonl` (an old vault may also hold `voice/drafts.jsonl`; only `people.forget`
still reads it). `voice/` is no kind folder, so the index, recall, vectors, the dashboard,
`index.md` and lint never see it. Writes go through `cortex.vault`; `id` and `day` are stamped
here, never taken from input."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from datetime import datetime
from statistics import median

from ulid import ULID

from cortex.chats import drop_private
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
    text = drop_private(text)  # a private span is never saved, nor refused as a secret
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


# ---- the measured style (V1): counted numbers, no model, no network -------------------------

MIN_GROUP = 8  # samples an audience needs before its numbers say anything
SHARED = 0.3  # an opener or sign-off counts when over this share of messages use it
MEASURED = "Measured: "  # marks the lines `propose` writes, so a re-run finds its own
# a measured line waiting in inbox.md, as Cortex._propose writes it
_WAITING = re.compile(r"- (?P<section>[^:\n]+): (?P<text>Measured: .+?) "
                      r"\(proposed \d{4}-\d{2}-\d{2}, me/voice\)\s*$")
_SENT = re.compile(r"[^.!?\n]+[.!?]*")
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)?")
_SUFFIX = {"m", "re", "s", "ve", "ll", "d", "t"}  # shortcut: "Sam's" counts too
_SIGN = re.compile(r"[\w' ,.!-]{1,30}")  # an opener or sign-off fit to quote on me/voice
_CONFIRMED = re.compile(r"\s*\(confirmed \d{4}-\d{2}-\d{2}\)$")


def _common(items: list[str], n: int) -> str | None:
    """The most common non-empty item (case and trailing punctuation folded) when over SHARED
    of `n` messages use it, as first written; else None."""
    key = [i.rstrip(",.!").lower() for i in items]
    top = Counter(k for k in key if k).most_common(1)
    if not top or top[0][1] / n <= SHARED:
        return None
    word = items[key.index(top[0][0])]
    return word if _SIGN.fullmatch(word) else None


def _numbers(texts: list[str]) -> dict:
    sents, msg_words, firsts, lasts = [], [], [], []
    words = contractions = emoji = lower = 0
    for t in texts:
        w = _WORD.findall(t)
        msg_words.append(len(w))
        words += len(w)
        contractions += sum(x.replace("’", "'").partition("'")[2].lower() in _SUFFIX
                            for x in w)
        sents += [s.strip() for s in _SENT.findall(t) if _WORD.search(s)]
        emoji += sum(unicodedata.category(c) == "So" and ord(c) >= 0x2600 for c in t)
        lower += any(c.isalpha() for c in t) and t == t.lower()
        lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
        firsts.append(w[0] if w else "")
        lasts.append(lines[-1] if len(lines) > 1 and len(lines[-1].split()) <= 3 else "")
    n, ns = len(texts), max(len(sents), 1)
    return {"n": n,
            "sentence_words": median(len(_WORD.findall(s)) for s in sents) if sents else 0,
            "questions": round(sum(s.endswith("?") for s in sents) / ns, 2),
            "contractions": round(100 * contractions / max(words, 1), 1),
            "exclamations": round(100 * sum(s.count("!") for s in sents) / ns, 1),
            "opens": _common(firsts, n), "closes": _common(lasts, n),
            "lowercase": round(lower / n, 2), "emoji": round(emoji / n, 2),
            "message_words": median(msg_words)}


def measure(vault) -> dict:
    """{group: numbers} over the samples not typed to an AI: each audience and "all", a group
    with fewer than MIN_GROUP samples left out. Deterministic."""
    groups: dict[str, list[str]] = {}
    for s in rows(vault, SAMPLES):
        if s.get("channel") != "ai" and isinstance(s.get("text"), str):
            t = s["text"].replace("\r\n", "\n")
            groups.setdefault("all", []).append(t)
            a = s.get("audience")  # a hand-edited row may lack one; "all" is the whole set
            if isinstance(a, str) and AUDIENCE_RE.fullmatch(a) and a != "all":
                groups.setdefault(a, []).append(t)
    return {g: _numbers(ts) for g, ts in sorted(groups.items()) if len(ts) >= MIN_GROUP}


def style_lines(m: dict, group: str = "all") -> list[str]:
    """At most 4 plain lines on `group`'s numbers; a number near the middle says nothing."""
    g = m.get(group)
    if not g:
        return []
    out = []
    s, q = g["sentence_words"], g["questions"]
    size = "short" if s <= 8 else "long" if s >= 20 else None
    asks = "many questions" if q >= 0.3 else "few questions" if q <= 0.05 else None
    if size:
        out.append(f"Writes {size} sentences (median {s:g} words)"
                   + (f" and asks {asks}" if asks else "") + ".")
    elif asks:
        out.append(f"Asks {asks} ({round(q * 100)}% of sentences).")
    if g["opens"] and g["closes"]:
        out.append(f'Opens with "{g["opens"]}" and signs off "{g["closes"]}".')
    elif g["opens"] or g["closes"]:
        out.append(f'Opens with "{g["opens"]}".' if g["opens"] else
                   f'Signs off "{g["closes"]}".')
    tone = [t for t, ok in (
        ("uses exclamation marks often", g["exclamations"] >= 30),
        ("rarely uses exclamation marks", g["exclamations"] <= 3),
        ("often writes all lowercase", g["lowercase"] >= 0.5),
        ("uses contractions (I'm, don't)", g["contractions"] >= 3),
        ("avoids contractions", g["contractions"] <= 0.5)) if ok]
    shape = [t for t, ok in (
        (f"keeps messages short (median {g['message_words']:g} words)", g["message_words"] <= 12),
        (f"writes long messages (median {g['message_words']:g} words)", g["message_words"] >= 80),
        (f"uses emoji often ({g['emoji']:g} per message)", g["emoji"] >= 1),
        ("uses no emoji", g["emoji"] == 0)) if ok]
    out += [x[0].upper() + x[1:] + "." for x in ("; ".join(tone), "; ".join(shape)) if x]
    return out[:4]


def propose(vault, m: dict | None = None) -> dict:
    """Propose each group's style lines for me/voice (`all` under Everyone, an audience under
    its own section, where cortex_as_me serves them) the way an inferred line on a page that
    waits is proposed (cortex_patch's queued path, as tags.act): under `## About me
    (unconfirmed)` in inbox.md, until the user's Approve in the Cortex app writes it. Measured
    lines still waiting that the new numbers no longer say are withdrawn; lines on the page
    (approved) are never touched, and one under `## Not me` is not proposed again. Returns
    counts: proposed, kept, withdrawn, not_me."""
    from cortex import claims, review
    from cortex.mcp_server import Cortex  # the SDK: only when writing
    from cortex.render import plain

    m = measure(vault) if m is None else m
    want = {("Everyone" if g == "all" else g): [MEASURED + x for x in style_lines(m, g)]
            for g in m}
    every = set(want.get("Everyone", ()))  # an audience does not repeat what all says
    want = {k: v if k == "Everyone" else [x for x in v if x not in every]
            for k, v in want.items()}
    core = Cortex(vault, build=False)  # me/ pages only: no index (as fact_harvest._save)
    core._ready.set()
    day, page = local_day(core._now()), core._raw("me/voice.md")
    approved = {(r["section"], _CONFIRMED.sub("", r["text"]))
                for r in claims.parse("voice", page) if r["status"] == "live"}
    waiting = {(x["section"], x["text"]) for x in map(_WAITING.match,
                                                       core._raw("inbox.md").splitlines()) if x}
    n: Counter = Counter()
    core.session = core.sessions.start("save")
    core._reset()
    try:
        for section, text in waiting:
            if text not in want.get(section, ()):
                core._unpropose(f"{section}: {text}")
                n["withdrawn"] += 1
        for section, lines in want.items():
            for line in lines:
                if (section, line) in approved | waiting:
                    n["kept"] += 1
                elif claims.is_not_me("voice", page, line):
                    n["not_me"] += 1
                else:
                    core._propose(f"- {section}: {line} (proposed {day}, me/voice)")
                    core.proposed.append(plain(f"{section}: {line}", 200))
                    core.sessions.note("patched", change={
                        "tool": "propose", "id": "voice", "kind": "me",
                        "op": "append_to_section", "target": section,
                        "hash": review.text_hash(line)})
                    n["proposed"] += 1
    finally:
        if core.sessions.current is not None:
            core.sessions.close(summary="Cortex measured how you write")
    return dict(n)
