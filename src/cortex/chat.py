"""The app's local chat: multi-turn questions over the archive (notes, saved AI chats, mistakes
and browsing, private pages included), answered by a LOCAL model only (`localllm`, never
cloudllm). Each turn retrieves afresh through `ask._passages`, wrapped as untrusted data; the
earlier turns go to the model as plain text only.

History is kept in `<vault>/.cortex/browse/chat/<ulid>.jsonl`, one JSON line per message. That
folder is inside the private browse tree on purpose (an answer can quote private browsing):
never served over MCP, never on the dashboard, never logged."""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta

from cortex import ask, chat_study, events, localllm, semantic, taint
from cortex.chat_study import study_answer  # noqa: F401  chat.study_answer, beside site_list
from cortex.index import STOPWORDS
from cortex.vault import Vault

DIR = ".cortex/browse/chat"
ID_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")  # a ULID, so a path can never leave DIR
MAX_TEXT = 4000
TITLE = 60
TURNS = 6  # earlier messages the model sees
HISTORY_BUDGET = 3000  # their characters, on top of ask.BUDGET of passages: a 2B model's context
LENGTH = {"short": 300, "normal": 700, "long": 1500}  # answer length -> max_tokens
DEFAULTS = {"length": "normal", "temperature": 0.2, "passages": 8,
            "sources": dict(ask.SOURCES), "history": True}
NO_ANSWER = "I don't know from your archive."
SEARCHED = 5  # pages listed as "searched, not cited" when the answer cites none
CITE = "\n\n(Answer from the passages above and cite each one you use as [n].)"  # 2B models forget
SYSTEM = ask.SYSTEM + (" Earlier messages are the conversation so far: use them to understand a "
                       "follow-up question, but answer only from the passages in the latest "
                       "message. Today is {today}.")
_live: dict[str, list] = {}  # chats of a session with history off: memory only, never written
_LIVE_LOCK = threading.Lock()  # the stream thread writes _live while the page lists chats


# ---- settings (`chat:` in ~/.cortex/config.yaml) -------------------------------------------

def _valid(key: str, v) -> bool:
    num = isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)
    return {"length": lambda: v in LENGTH,
            "temperature": lambda: num and 0 <= v <= 1,
            "passages": lambda: num and v == int(v) and 4 <= v <= 12,
            "history": lambda: isinstance(v, bool),
            "sources": lambda: isinstance(v, dict) and set(v) <= set(ask.SOURCES)
            and all(isinstance(x, bool) for x in v.values())}.get(key, lambda: False)()


def get_settings(config: dict | None = None) -> dict:
    """The chat settings; a missing or bad value is its default."""
    cfg = (taint.config() if config is None else config).get("chat")
    cfg = cfg if isinstance(cfg, dict) else {}
    out = {k: cfg[k] if k in cfg and _valid(k, cfg[k]) else v for k, v in DEFAULTS.items()}
    out["sources"] = {**ask.SOURCES, **out["sources"]}
    out["passages"] = int(out["passages"])
    return out


def save_settings(body: dict) -> dict:
    """Validate `body` (some of DEFAULTS' keys), merge it into the saved settings and return
    them; ValueError names the first bad field."""
    if extra := set(body) - set(DEFAULTS):
        raise ValueError(f"unknown setting {sorted(extra)[0]!r}")
    for k, v in body.items():
        if not _valid(k, v):
            raise ValueError({"length": "length must be short, normal or long",
                              "temperature": "temperature must be a number from 0 to 1",
                              "passages": "passages must be a whole number from 4 to 12",
                              "history": "history must be true or false",
                              "sources": "sources must map notes, chats, browsing and private "
                                         "to true or false"}[k])
    cur = get_settings()
    new = {**cur, **body, "sources": {**cur["sources"], **body.get("sources", {})}}
    old = taint.config().get("chat")
    taint.write_config({"chat": {**(old if isinstance(old, dict) else {}), **new}})
    return get_settings()


# ---- dates in a question --------------------------------------------------------------------

# whole month names or their short forms only: "market", "decisions" or "novel" are not months
_M = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
      r"|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?")
_DATE = (rf"(?:\d{{4}}-\d{{1,2}}-\d{{1,2}}|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_M}"
         rf"(?:,?\s+\d{{4}})?|{_M}\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?:,?\s+\d{{4}})?)")
_NUM = (r"(\d{1,3}|an?|one|two|three|four|five|six|seven|eight|nine|ten"
        r"|(?:a\s+)?couple(?:\s+of)?|(?:a\s+)?few)")
_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9, "ten": 10, "couple": 2, "few": 3}
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_UNIT = {"day": 1, "week": 7, "month": 30}
_PAD = {"day": 1, "week": 3, "month": 7}  # "N units ago" is fuzzy: wider for longer units


def _n(s: str) -> int:
    s = s.lower()
    return int(s) if s.isdigit() else next(_WORDS[w] for w in reversed(s.split()) if w in _WORDS)


def _day(s: str, today: date) -> date:
    """12 Nov, Nov 12, 12th of November 2025, 2026-11-12; a day with no year is the last one
    on or before `today`."""
    s = s.lower()
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s):
        return date(*map(int, m.groups()))
    mon = next(i for i, name in enumerate(_MONTHS, 1) if re.search(r"\b" + name, s))
    nums = [int(x) for x in re.findall(r"\d+", s)]
    d = date(nums[1] if len(nums) > 1 else today.year, mon, nums[0])
    return d.replace(year=d.year - 1) if len(nums) == 1 and d > today else d


def _month(mon: str, year: str | None, today: date) -> tuple[date, date]:
    m = _MONTHS.index(mon.lower()[:3]) + 1
    y = int(year) if year else today.year - (m > today.month)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return date(y, m, 1), nxt - timedelta(days=1)


def _ago(m, t: date) -> tuple[date, date]:
    unit = m[2].lower()
    mid = t - timedelta(days=_n(m[1]) * _UNIT[unit])
    return mid - timedelta(days=_PAD[unit]), min(mid + timedelta(days=_PAD[unit]), t)


def _monday(t: date) -> date:
    return t - timedelta(days=t.weekday())


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _weekday(m, t: date) -> tuple[date, date]:
    """The most recent such day on or before `t`; "last Monday" on a Monday is a week ago."""
    back = (t.weekday() - _WEEKDAYS.index(m[2].lower())) % 7 or 7 * (m[1].lower() == "last")
    return t - back * _D, t - back * _D


def _weekend(t: date) -> tuple[date, date]:
    sat = t - (t.weekday() - 5) % 7 * _D  # the last Saturday, today on a Saturday
    return sat, min(sat + _D, t)


def _last(n: int, unit: str, t: date) -> tuple[date, date]:
    """The last `n` units ending today: "last 3 days" is 3 days, today the third."""
    return t - (n * _UNIT[unit.lower()] - 1) * _D, t


_D = timedelta(days=1)
_RULES = [  # first match wins
    (rf"\bsince\s+({_DATE})", lambda m, t: (_day(m[1], t), t)),
    (r"\b(?:the\s+)?day\s+before\s+yesterday\b", lambda m, t: (t - 2 * _D, t - 2 * _D)),
    (r"\b(?:yesterday|last\s+night)\b", lambda m, t: (t - _D, t - _D)),
    (r"\btomorrow\b", lambda m, t: (t + _D, t + _D)),  # "what is due tomorrow?"
    (r"\b(?:today|tonight|this\s+morning|this\s+afternoon|this\s+evening)\b",
     lambda m, t: (t, t)),
    (rf"\b{_NUM}\s+(day|week|month)s?\s+ago\b", _ago),
    (rf"\b(?:last|past|previous)\s+{_NUM}\s*(?:to|-|–|or)\s*{_NUM}\s+(day|week|month)s?\b",
     lambda m, t: _last(max(_n(m[1]), _n(m[2])), m[3], t)),  # the wider
    (rf"\b(?:last|past|previous)\s+{_NUM}\s+(day|week|month)s?\b",
     lambda m, t: _last(_n(m[1]), m[2], t)),
    (r"\bpast\s+(week|month)\b", lambda m, t: _last(1, m[1], t)),
    (r"\b(?:this|last|over\s+the)\s+weekend\b", lambda m, t: _weekend(t)),
    (rf"\b(on|last|this|past)\s+({'|'.join(_WEEKDAYS)})\b", _weekday),
    (r"\bthis\s+week\b", lambda m, t: (_monday(t), t)),
    (r"\blast\s+week\b", lambda m, t: (_monday(t) - 7 * _D, _monday(t) - _D)),
    (r"\bthis\s+month\b", lambda m, t: (t.replace(day=1), t)),
    (r"\blast\s+month\b", lambda m, t: _month(_MONTHS[(t.month - 2) % 12],
                                              str(t.year - (t.month == 1)), t)),
    (rf"\bin\s+({_M})(?:\s+(\d{{4}}))?(?![\w-])", lambda m, t: _month(m[1], m[2], t)),
    (rf"\b(?:on\s+)?({_DATE})", lambda m, t: (_day(m[1], t), _day(m[1], t))),
]
_RULES = [(re.compile(p, re.I), f) for p, f in _RULES]


def _when(text: str, today: date) -> tuple[date, date, tuple[int, int]] | None:
    for rx, f in _RULES:
        if m := rx.search(text or ""):
            try:
                lo, hi = f(m, today)
            except (ValueError, StopIteration):  # 31 Feb and the like: try the next rule
                continue
            return lo, hi, m.span()
    return None


def when(text: str, today: date) -> tuple[date, date] | None:
    """(first day, last day) named in `text` (today, yesterday, 3 days ago, last week, this
    month, on 12 Nov, in October, since 2026-11-01, ...), or None."""
    got = _when(text, today)
    return got[:2] if got else None


_GENERIC = frozenset(
    "which what when where who whom did do does done i me my mine visit visited visiting see "
    "saw seen look looked looking site sites website websites page pages web read reading "
    "watched go went browse browsed open opened about on at the a an any anything something "
    "tell show find found was were have has had in of to for from there that this ago day "
    "days week weeks month months give example examples explain elaborate detail details "
    "please again".split())


def topic(text: str, span: tuple[int, int]) -> str:
    """`text` without the date phrase at `span` and without generic words: '' for "which
    sites did I visit yesterday?"."""
    rest = text[:span[0]] + " " + text[span[1]:]
    return " ".join(w for w in re.findall(r"\w+", rest.lower())
                    if w not in _GENERIC and w not in STOPWORDS)


_POINTS_BACK = frozenset(
    "it its it's that this these those they them their he she him his her same also else "
    "more former latter".split())
_OPENS_FOLLOW_UP = re.compile(r"\s*(?:and|but|so|also|what\s+about|how\s+about)\b", re.I)
# "the second one", "which one", "can a list be one?": a pick from the last answer ("one"
# elsewhere is too common: "one-hot encoding")
_PICKS_ONE = re.compile(
    r"\b(?:first|second|third|last|other|previous|which)\s+ones?\b|\bones?\W*$", re.I)
# "the algorithms that I know": a relative clause, not "that" pointing back
_RELATIVE_THAT = re.compile(r"\bthat\s+(?:i|you|we|they|he|she|my|your|our|their)\b", re.I)


def follow_up(text: str, got=None) -> bool:
    """Whether `text` leans on the previous question ("and its running time?", "why?", "what
    about heaps"): then both are searched together. A question that names its own topic is
    searched alone, so a new subject is not mixed with the last one. `got`: its date phrase,
    whose words ("this month") never point back."""
    own = text[:got[2][0]] + " " + text[got[2][1]:] if got else text
    own = _RELATIVE_THAT.sub(" ", own)
    if _OPENS_FOLLOW_UP.match(text) or _PICKS_ONE.search(own) \
            or _POINTS_BACK & set(re.findall(r"[\w']+", own.lower())):
        return True
    return not got and not topic(text, (0, 0))  # nothing of its own to search for


# ---- storage ---------------------------------------------------------------------------------

def _path(vault: Vault, id: str):
    if not isinstance(id, str) or not ID_RE.fullmatch(id):
        raise ValueError("not a chat id")
    return vault.root / DIR / f"{id}.jsonl"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def new() -> str:
    from ulid import ULID  # lazy, as in events: its import is slow
    return str(ULID())


def load(vault: Vault, id: str) -> list:
    """The messages of chat `id` ([] for a new one); torn or odd lines are skipped."""
    p = _path(vault, id)
    with _LIVE_LOCK:
        live = _live.get(id)
    if live is not None:
        return [*live]
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in raw.splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") \
                and isinstance(m.get("text"), str):
            out.append(m)
    return out


def _save(vault: Vault, id: str, msgs: list, keep: bool) -> None:
    with _LIVE_LOCK:
        # a chat touched while history was off stays in memory for the session, even after
        # history is turned on again: writing it then would save the history-off turns
        if not keep or id in _live:
            _live[id] = msgs
            while len(_live) > 50:
                _live.pop(next(iter(_live)))
            return
    vault.write_text(_path(vault, id), "".join(json.dumps(m, ensure_ascii=False) + "\n"
                                               for m in msgs))


def _summary(id: str, msgs: list) -> dict | None:
    first = next((m["text"] for m in msgs if m["role"] == "user"), None)
    if first is None:
        return None
    return {"id": id, "title": " ".join(first.split())[:TITLE],
            "updated": msgs[-1].get("ts", ""), "count": len(msgs)}


def list(vault: Vault) -> list:  # shadows the builtin on purpose: chat.list
    """[{id, title (the first question cut to TITLE characters), updated, count}], newest
    first."""
    folder = vault.root / DIR
    ids = {p.stem for p in folder.glob("*.jsonl") if ID_RE.fullmatch(p.stem)} \
        if folder.is_dir() else set()
    with _LIVE_LOCK:
        live = set(_live)
    out = [s for i in ids | live if (s := _summary(i, load(vault, i)))]
    return sorted(out, key=lambda s: (s["updated"], s["id"]), reverse=True)


def delete(vault: Vault, id: str) -> bool:
    p = _path(vault, id)
    with _LIVE_LOCK:
        gone = _live.pop(id, None) is not None
    return vault.remove(p) or gone


def delete_all(vault: Vault) -> int:
    folder = vault.root / DIR
    with _LIVE_LOCK:
        n = len(_live)
        _live.clear()
    for p in folder.glob("*.jsonl") if folder.is_dir() else ():
        if ID_RE.fullmatch(p.stem) and vault.remove(p):
            n += 1
    return n


# ---- a turn ------------------------------------------------------------------------------------

# "which sites did I visit in the past 4 months?": answered from the stored day counts, every
# site in the range, never from a few passages (a small model lists some and repeats them)
_SITES_Q = re.compile(r"\b(?:sites?|websites?|domains?)\b", re.I)
# "which sites did I visit", "sites I've been on", "my most visited sites": the person's own
# browsing, never "the domain of every function" or "the binding sites used by hemoglobin"
_LIST_Q = re.compile(r"\b(?:i|i've|ive|me|my)\b[\w\s',-]{0,40}?\b(?:visit\w*|went|go|gone|been"
                     r"|open\w*|brows\w*|use[ds]?|surf\w*)\b", re.I)
# a browser filter only as "in Comet", "on Edge": "sites to learn edge detection" is no browser
_BROWSER = re.compile(r"\b(?:in|on|using|from|with)\s+(?:the\s+)?(?:google\s+|microsoft\s+)?"
                      r"(chrome|edge|brave|comet|firefox|opera|vivaldi|arc)\b", re.I)
# "what's a good site to practise SQL?", "can you suggest websites I could use?": advice
_SUGGEST = re.compile(r"\b(?:good|best|better|suggest\w*|recommend\w*|should\s+i\s+use"
                      r"|could\s+use)\b", re.I)
# words a plain "list my sites" question holds besides its date and browser; any other word is
# a topic ("which sites do I use for DSA?"), for the passages
_LIST_WORDS = frozenset("list all unique distinct every most top many often frequently usually "
                        "visits use used using been ve s go gone surf surfed browser".split())
NOTES_OFF = ("Notes and mistakes are switched off in Chat settings, so I can't read your study "
             "plan.")
SITES_SHOWN = 2000  # "all the sites": every one, short of a runaway answer
_MD = re.compile(r"[*_`\[\]<>#|]")  # a model-written description stays plain text


def _count(x) -> int | None:
    """A stored count as an int (missing: 0), or None when it is not a number."""
    try:
        return int(x or 0)
    except (TypeError, ValueError, OverflowError):
        return None


def _site_counts(vault: Vault, got, sources: dict, browser: str | None = None):
    """(seen {site: {n, days, hidden, cat}}, visits left out for want of a stored browser,
    days, llm cache, browse settings) for `got`'s days (None: all days), under Chat's
    `browsing` and `private` switches and the browsing settings (`sites._sides`): the one count
    behind the site list and the digest."""
    from cortex.browse import load_settings, sites

    days = sites.lean_days(vault)
    llm = sites.load_cache(vault)["llm"]
    settings = load_settings()
    side, scat = sites._sides(settings, llm, days)
    lo, hi = (got[0].isoformat(), got[1].isoformat()) if got else ("", "9999")
    seen: dict[str, dict] = {}
    meta: dict[str, tuple[str, str]] = {}  # key -> (site, category)
    unknown = 0  # visits of a named browser's range stored before Cortex kept the browser
    for day, keys in days.items():
        if not lo <= day <= hi or not isinstance(keys, dict):
            continue
        for key, a in keys.items():
            h = side(day, key) if isinstance(a, dict) else None
            if h is None or not sources.get("private" if h else "browsing"):
                continue
            n = a.get("n", 0)
            if type(n) is not int and (n := _count(n)) is None:
                continue  # a malformed entry is skipped, never the whole answer
            if browser:
                w = a.get("w")
                if not isinstance(w, dict):
                    unknown += n
                    continue
                # ids are source keys ("comet/Default"); a bare browser name is accepted too
                got = [_count(c) for k, c in w.items()
                       if isinstance(k, str) and k.split("/")[0] == browser]
                if None in got:
                    continue
                b = sum(got)
                n = min(n, b)  # a forget lowers "n" only
                if n <= 0:
                    continue
            if (m := meta.get(key)) is None:
                site, cat, _pd = sites._split(key)
                m = meta[key] = (site, cat or scat(site))
            if (e := seen.get(m[0])) is None:
                e = seen[m[0]] = {"n": 0, "days": set(), "hidden": False, "cat": m[1]}
            e["n"] += n
            e["days"].add(day)
            e["hidden"] |= bool(h)
    return seen, unknown, days, llm, settings


def asks_sites(text: str, today: date | None = None) -> bool:
    """Whether `text` asks for a list of the sites the person visited (`site_list`): their own
    browsing, not a suggestion ("a good site for SQL") and no topic beyond dates and a browser."""
    if not (_SITES_Q.search(text) and _LIST_Q.search(text)) or _SUGGEST.search(text):
        return False
    b = _BROWSER.search(text)
    rest = text[:b.start()] + " " + text[b.end():] if b else text
    dated = _when(rest, today or _today())
    return not set(topic(rest, dated[2] if dated else (0, 0)).split()) - _LIST_WORDS


def site_list(vault: Vault, text: str, got, sources: dict,
              today: date | None = None) -> str | None:
    """The answer to a question asking for a list of sites, or None for any other question:
    every site visited in `got`'s days (None: all days), most visits first, with its category
    and the local model's one-line description, under Chat's own source switches (`browsing`,
    `private`) and the browsing settings (a denied or Don't save site is never stored)."""
    today = today or _today()
    if not asks_sites(text, today):
        return None
    b = _BROWSER.search(text)
    if not (sources.get("browsing") or sources.get("private")):
        return "Browsing is switched off in Chat settings, so I cannot list sites."
    browser = b[1].lower() if b else None
    seen, unknown, days, llm, settings = _site_counts(vault, got, sources, browser)
    lo = got[0].isoformat() if got else ""
    when = f"from {_span(got[0], got[1], today)}" if got else "in everything Cortex has stored"
    if browser:
        when = f"in {browser.title()} {when}"
    notes = []
    if unknown:
        notes.append(f"{unknown} visit{'s' * (unknown != 1)} in this range were imported "
                     "before Cortex kept which browser they came from, so they are left out. "
                     "The next history import (Browsing, or the automatic one) records it for "
                     "the days your browsers still hold.")
    stored = min((d for d in days if isinstance(days[d], dict)), default=None)
    if stored and lo < stored:
        notes.append(f"Cortex has browsing from {stored} on; browsers keep about 90 days of "
                     "history, so older days are here only if they were imported then.")
    if skipped := sorted(settings.skip_categories):
        notes.append(f"Categories set to Don't save ({', '.join(skipped)}) are never stored, so "
                     "they cannot be listed; change that under Browsing in the app.")
    if not seen:
        return " ".join([f"No sites are stored {when}."] + notes)
    order = sorted(seen.items(), key=lambda kv: (-kv[1]["n"], kv[0]))
    lines = [f"**{len(order)} site{'s' * (len(order) != 1)}** {when}, most visits first:", ""]
    for site, e in order[:SITES_SHOWN]:
        entry = llm.get(site) if isinstance(llm.get(site), dict) else {}
        # a visible site never shows what the model wrote from private titles ("h")
        said = (entry.get("h") or entry.get("v")) if e["hidden"] else entry.get("v")
        said = said if isinstance(said, dict) else {}
        desc = _MD.sub("", str(said.get("desc") or "")).strip()
        cat = "" if e["cat"] in ("", "other") else f" · {e['cat']}"
        nd = len(e["days"])
        lines.append(f"- **{site}**{cat} · {e['n']} visit{'s' * (e['n'] != 1)} on {nd} "
                     f"day{'s' * (nd != 1)}" + (f": {desc}" if desc else ""))
    if len(order) > SITES_SHOWN:
        lines.append(f"- and {len(order) - SITES_SHOWN} more with fewer visits")
    return "\n".join(lines + ([""] + notes if notes else []))


DIGEST_SHOWN = 10  # lines per digest section
_plural = chat_study._plural
_NOTE_KINDS = ("note", "resource", "decision", "project", "loop")  # resource: chats too
_JOURNAL_LINE = re.compile(r"- (?:\d\d:\d\d|—) ")  # as reflect.portrait; — is back-dated
_INDEX: dict = {}  # vault root -> the digest's Index, kept between answers
_INDEX_LOCK = threading.Lock()  # two chat streams may digest at once
_DAYS: dict = {}  # vault root -> {(path, its stat): (ask._note_day, semantic.is_chat)}


def _capped(head: str, items: list, more: str = "more") -> list:
    return [head, "", *[f"- {x}" for x in items[:DIGEST_SHOWN]],
            *([f"- and {len(items) - DIGEST_SHOWN} {more}"] if len(items) > DIGEST_SHOWN else [])]


def _pages_in(vault: Vault, lo: str, hi: str, sources: dict) -> list:
    """[(day, kind, id, title, body, is_chat)] of notes, saved AI chats and journal pages dated
    in [lo, hi] (`ask._note_day`: a date, else a note's last change), under Chat's `notes` and
    `chats` switches. Private pages (their own `private: true`, or under a private subject)
    never appear: the index keeps them out of Chat's search too, and the `private` switch is
    about private browsing, not notes (whether Chat may read private notes is the owner's
    open decision)."""
    from cortex.catalog import Catalog
    from cortex.index import Index

    catalog = Catalog.load(vault)
    with _INDEX_LOCK:  # the pages ask._passages searches; refreshed, not rebuilt (~3 s a build)
        idx = _INDEX.get(vault.root)
        if idx is None:
            idx = _INDEX[vault.root] = Index(vault, catalog)
        else:
            idx.catalog = catalog
            idx.refresh()
        paths = {i: p for p, i in idx._ids.items()}
        stats = dict(idx._stats)
        pages = [*idx._notes.items()]  # chat.list shadows the builtin
    # (day, is_chat) per file version: both read the whole body (~1 ms a note), so they are
    # kept between answers for the files that did not change
    old, memo = _DAYS.get(vault.root, {}), {}
    out = []
    for i, n in pages:
        if n["kind"] not in (*_NOTE_KINDS, "journal") or i not in paths:
            continue
        k = (paths[i], stats.get(paths[i]))
        d, is_chat = memo[k] = old.get(k) or (ask._note_day(vault, i, n, paths[i]),
                                              semantic.is_chat(n))
        if lo <= d <= hi and sources.get("chats" if is_chat else "notes"):
            out.append((d, n["kind"], i, chat_study._title(n["title"]) or i, n["body"], is_chat))
    _DAYS[vault.root] = memo
    return sorted(out, key=lambda p: (p[0], p[3]))


def digest(vault: Vault, lo: date, hi: date, sources: dict, today: date,
           unit: str | None = None) -> str:
    """What Cortex holds about the days `lo` to `hi`, read from its own data with no model: what
    was studied (chat_study's range answer), the most visited sites (`_site_counts`), notes and
    saved AI chats written or changed, and journal lines, each section capped and only when it
    has something, then one line on what is left out. `unit`: "day", "week" or "days" for the
    heading (chat_study.digest_days), else by the range."""
    when = f"on {_span(lo, hi, today)}" if lo == hi else f"from {_span(lo, hi, today)}"
    unit = unit or ("day" if lo == hi else "days")
    if unit == "week" and (lo, hi) == (_monday(today), today):
        unit = "week so far"  # on a Monday too, when it is one day so far
    sections = []
    studied = chat_study.answer(vault, "range", (lo, hi, None), today) \
        if sources.get("notes") else chat_study.NO_STUDY
    if not studied.startswith(chat_study.NO_STUDY):
        sections.append(studied.splitlines())
    skipped: list = []
    if sources.get("browsing") or sources.get("private"):
        seen, _, _, _, settings = _site_counts(vault, (lo, hi), sources)
        skipped = sorted(settings.skip_categories)
        if seen:
            order = sorted(seen.items(), key=lambda kv: (-kv[1]["n"], kv[0]))
            visits = sum(e["n"] for e in seen.values())
            sections.append(_capped(
                f"**{_plural(len(order), 'site')}** visited ({_plural(visits, 'visit')}), "
                "most first:",
                [f"**{s}**" + ("" if e["cat"] in ("", "other") else f" · {e['cat']}")
                 + f" · {_plural(e['n'], 'visit')}" for s, e in order], "more sites"))
    pages = _pages_in(vault, lo.isoformat(), hi.isoformat(), sources)
    notes = [p[3] for p in reversed(pages) if p[1] != "journal" and not p[5]]
    if notes:
        sections.append(_capped(f"**{_plural(len(notes), 'note')}** written or changed:", notes))
    chats = [p[3] for p in reversed(pages) if p[5]]
    if chats:
        sections.append(_capped(f"**{_plural(len(chats), 'saved AI chat')}**:", chats))
    journal = [(_span(d := date.fromisoformat(p[0]), d, today) + " " if lo != hi else "")
               + chat_study._title(ln[2:].strip())
               for p in pages if p[1] == "journal"
               for ln in p[4].splitlines() if _JOURNAL_LINE.match(ln)]
    if journal:
        sections.append(_capped(f"**Journal** ({_plural(len(journal), 'line')}):", journal))
    off = [label for k, label in (("notes", "notes and journal"), ("chats", "saved AI chats"),
                                  ("browsing", "browsing"), ("private", "private pages"))
           if not sources.get(k)]
    left = ["private notes and study subjects"]
    left += [f"{', '.join(off)} (switched off in Chat settings)"] if off else []
    if skipped:
        left.append(f"site categories set to Don't save ({', '.join(skipped)}), never stored")
    tail = "Not included: " + "; ".join(left) + "."
    if not sections:
        return f"Cortex has nothing recorded {when}. {tail}"
    out = [f"**Your {unit}** {when}:"]
    for sec in sections:
        out += ["", *sec]
    return "\n".join([*out, "", tail])


def _looping(text: str) -> bool:
    """A small model caught in a loop: some line of 20+ characters said three times."""
    seen = Counter(ln.strip() for ln in text.splitlines() if len(ln.strip()) >= 20)
    return bool(seen) and seen.most_common(1)[0][1] >= 3


def _once(text: str) -> str:
    """`text` with each repeated line of 20+ characters kept only the first time."""
    out, seen = [], set()
    for ln in text.splitlines():
        k = ln.strip()
        if len(k) >= 20 and k in seen:
            continue
        seen.add(k)
        out.append(ln)
    return "\n".join(out)


def _today(now: datetime | None = None) -> date:
    """The local day as events, site days and the dashboard count it: it starts at 04:00."""
    return date.fromisoformat(events.local_day(now or datetime.now(UTC)))


def _span(a: date, b: date, today: date | None = None) -> str:
    """'17 Nov', '10 Nov to 16 Nov' or '11 Sep 2001' (a year other than `today`'s)."""
    today = today or _today()
    day = chat_study._day
    return day(a, today) if a == b else f"{day(a, today)} to {day(b, today)}"


def _local(runner) -> bool:
    """Only a plain loopback localllm.Runner hears private passages: cloudllm's CloudRunner
    subclasses Runner, so isinstance would let it through."""
    if type(runner) is not localllm.Runner:
        return False
    try:
        localllm._check_url(runner.base)
    except ValueError:
        return False
    return True


def history(msgs: list) -> list:
    """The last TURNS messages as plain {role, content}, oldest dropped first to fit
    HISTORY_BUDGET, same-role neighbours joined (a failed turn leaves two user messages; some
    chat templates refuse that), starting with a user message."""
    out, left = [], HISTORY_BUDGET
    for m in reversed(msgs[-TURNS:]):
        text = m["text"].strip()
        if not text:
            continue
        if len(text) > left:
            break
        left -= len(text)
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] = text + "\n\n" + out[-1]["content"]
        else:
            out.append({"role": m["role"], "content": text})
    out.reverse()
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def reply(vault: Vault, convo_id: str | None, text: str, runner, emit, cancel,
          settings: dict | None = None, today: date | None = None) -> str:
    """One turn of chat `convo_id` (None: a new chat). Saves the question, retrieves, streams
    the answer through `emit`: {"t": "start", id, model}, {"t": "step", "s"} as it searches and
    as the model starts reading (the app shows each with a live count), {"t": "think"} while a
    reasoning model only thinks, {"t": "tok", "s"} per piece, {"t": "src", sources: [{n, path,
    title, kind}]} (the passages cited), {"t": "done", id, model, seconds, stopped}; or {"t": "err",
    code (not_set_up, no_local_model, no_answer), message}. A set `cancel` stops the stream;
    the partial answer is saved with `stopped: true`. Returns the chat id. `runner` is a local
    `localllm.Runner` or None; never a cloud one."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ValueError(f"Type a message (up to {MAX_TEXT:,} characters).")
    s = settings or get_settings()
    convo_id = convo_id or new()
    msgs = load(vault, convo_id)  # validates the id
    t0, today = time.monotonic(), today or _today()
    prev = next((m["text"] for m in reversed(msgs) if m["role"] == "user"), "")
    got, said = _when(text, today), text
    leans = follow_up(text, got)
    if leans and not got and prev and (got_prev := _when(prev, today)):
        got, said = got_prev, prev  # "which of those was hardest?": the last dates still hold
    query = f"{text.strip()}\n{prev}".strip() if leans else text.strip()
    # a site list or a study answer is read from Cortex's own data with no model, so it needs
    # neither the search model nor a local runner; it keeps to its own dates (a follow-up's
    # borrowed ones are for passages only), and so does its step line
    own = got if said is text else None
    span = f" from {_span(own[0], own[1], today)}" if own else ""
    # which data answer, if any, is decided first and cheaply; it is read after `start` and its
    # step are sent, so the app shows the step (and Stop) while a heavy vault is counted
    step = data = None
    if asks_sites(text, today):
        step = "Counting the sites you visited" + span

        def data():
            return site_list(vault, text, own, s["sources"], today)
    elif (asked := chat_study.kind(text, own, today, not prev)) == "digest":
        lo, hi, unit = chat_study.digest_days(text, own, today)
        step = "Putting together your " + ("day" if (unit, lo) == ("day", today) else
                                           f"{unit} {'on' if lo == hi else 'from'} "
                                           f"{_span(lo, hi, today)}")

        def data():
            return digest(vault, lo, hi, s["sources"], today, unit)
    elif asked is not None:
        step = chat_study.STEP[asked] + (span if asked == "range" else "")

        def data():
            return chat_study.study_answer(vault, text, own, today, not prev) \
                if s["sources"].get("notes") else NOTES_OFF
    listed = None if data is None else ""  # a data answer: filled in after its step
    if listed is None and semantic.load_embedder() is None:
        emit({"t": "err", "code": "not_set_up", "message": semantic.NOT_SET_UP, "id": convo_id})
        return convo_id
    if listed is None and callable(runner) and not isinstance(runner, localllm.Runner):
        try:  # the app passes localllm.find: it may start LM Studio, so only when needed
            runner = runner()
        except ValueError as e:  # a refused localllm setting (cloud model, non-loopback url)
            emit({"t": "err", "code": "no_local_model", "message": str(e), "id": convo_id})
            return convo_id
    if listed is None and not _local(runner):
        emit({"t": "err", "code": "no_local_model", "message": ask.NO_MODEL, "id": convo_id})
        return convo_id
    model = "Cortex" if listed is not None else runner.model
    msgs.append({"role": "user", "text": text.strip(), "ts": _now()})
    _save(vault, convo_id, msgs, s["history"])
    emit({"t": "start", "id": convo_id, "model": model})
    found: list = []
    parts: list = []
    if listed is not None:
        emit({"t": "step", "s": step})
        listed = data()
        if not (cancel is not None and cancel.is_set()):  # Stop while counting: nothing shown
            parts.append(listed)
            emit({"t": "tok", "s": listed})
    else:
        # pages are dated by when they were written: a future range ("tomorrow's exam") keeps
        # no page, so it does not narrow the search; a range running past today stops at today
        past = got and got[0] <= today
        emit({"t": "step", "s": "Searching your notes and browsing" + (
            f" from {_span(got[0], min(got[1], today), today)}" if past else "")})
        found = ask._passages(vault, query, s["passages"],
                              between=(got[0].isoformat(), min(got[1], today).isoformat())
                              if past else None,
                              sources=s["sources"], topic=topic(said, got[2]) if got else "")
    if listed is not None:
        pass
    elif not found:
        parts.append(NO_ANSWER)
        emit({"t": "tok", "s": NO_ANSWER})
    else:
        emit({"t": "step", "s": f"Reading {len(found)} page{'s' * (len(found) != 1)} with "
                                f"{runner.model}"})
        messages = [{"role": "system",
                     "content": SYSTEM.format(today=f"{today:%A}, {today.isoformat()}")},
                    *history(msgs[:-1]),
                    {"role": "user", "content": ask.prompt(found, text) + CITE}]
        thought = []

        def thinking():
            if not thought and not parts:
                thought.append(1)
                emit({"t": "think"})
        try:
            for piece in runner.stream(messages, temperature=s["temperature"],
                                       max_tokens=LENGTH[s["length"]], cancel=cancel,
                                       on_thinking=thinking):
                parts.append(piece)
                emit({"t": "tok", "s": piece})
                if "\n" in piece and _looping("".join(parts)):  # stop a loop, each line once
                    parts[:] = [_once("".join(parts))]
                    emit({"t": "text", "s": parts[0]})
                    break
        except (OSError, ValueError):  # no answer, a timeout, a refused url
            pass
    answer = "".join(parts).strip()
    stopped = cancel is not None and cancel.is_set()
    if not answer and not stopped:
        emit({"t": "err", "code": "no_answer", "id": convo_id,
              "message": f"{runner.describe()} did not answer; try again."})
        return convo_id
    cited = ask.cites(answer)
    sources = [{"n": n, "path": p["path"], "title": p["title"], "kind": p["kind"]}
               for n, p in enumerate(found, 1) if n in cited]
    # a small model often cites nothing: then show what it read, marked as not cited
    searched = [] if sources else [{"path": p["path"], "title": p["title"], "kind": p["kind"]}
                                   for p in found[:SEARCHED]]
    msgs.append({"role": "assistant", "text": answer, "ts": _now(), "sources": sources,
                 **({"searched": searched} if searched else {}),
                 "model": model, **({"stopped": True} if stopped else {})})
    _save(vault, convo_id, msgs, s["history"])
    emit({"t": "src", "sources": sources, "searched": searched})
    emit({"t": "done", "id": convo_id, "model": model, "stopped": stopped,
          "seconds": round(time.monotonic() - t0, 1)})
    return convo_id
