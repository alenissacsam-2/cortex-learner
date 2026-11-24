"""Date phrases. `when` reads the day or range a question names (the app's Chat and its
helpers, moved here from chat.py unchanged); `pin` stamps a durable statement's relative dates
("met the recruiter yesterday", "exam next Monday") with the ISO day they meant when it was
written, so the line still says it a month later."""

from __future__ import annotations

import re
from datetime import date, timedelta

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
    (rf"\b(?:the\s+)?week\s+of\s+({_DATE})",  # Monday to Sunday of that day's week
     lambda m, t: (_monday(_day(m[1], t)), _monday(_day(m[1], t)) + 6 * _D)),
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


# ---- pinning relative dates in a statement --------------------------------------------------

def _this(m, t: date) -> tuple[date, date]:
    """"this Friday": that day of the current Monday-to-Sunday week, ahead or behind."""
    d = _monday(t) + _WEEKDAYS.index(m[1].lower()) * _D
    return d, d


def _next(m, t: date) -> tuple[date, date]:
    """"next Friday": the first such day after today (a week on, said on a Friday)."""
    d = t + ((_WEEKDAYS.index(m[1].lower()) - t.weekday() - 1) % 7 + 1) * _D
    return d, d


_DAYS = "|".join(_WEEKDAYS)
# a count that names no one day: "a few days ago", "3 or 4 days ago", "in a day or two"
_VAGUE = re.compile(r"couple|few", re.I)
_RANGE_BEFORE = re.compile(r"(?:\bor|\bto|[-–])\s*$", re.I)
_RANGE_AFTER = re.compile(r"\s+or\b", re.I)
# Only phrases that name one day or one clear range, in statements (`when`'s rules are for
# questions: "since", "past 3 days", "in October" or a bare month are not pinned). Order: the
# longer phrase first, so "the day before yesterday" is not read as "yesterday". The flag: a
# counted phrase, checked for vagueness.
_PIN_RULES = [(re.compile(p, re.I), f, counted) for p, f, counted in [
    (r"\b(?:the\s+)?day\s+before\s+yesterday\b", lambda m, t: (t - 2 * _D,) * 2, False),
    (r"\byesterday\b", lambda m, t: (t - _D,) * 2, False),
    (r"\btomorrow\b", lambda m, t: (t + _D,) * 2, False),
    (r"\b(?:today|tonight)\b", lambda m, t: (t,) * 2, False),
    (rf"\b{_NUM}\s+(day|week)s?\s+ago\b",
     lambda m, t: (t - _n(m[1]) * _UNIT[m[2].lower()] * _D,) * 2, True),
    (rf"\bin\s+{_NUM}\s+days?\b", lambda m, t: (t + _n(m[1]) * _D,) * 2, True),
    (r"\blast\s+week\b", lambda m, t: (_monday(t) - 7 * _D, _monday(t) - _D), False),
    (r"\bthis\s+weekend\b", lambda m, t: (_monday(t) + 5 * _D, _monday(t) + 6 * _D), False),
    (rf"\b(on|last)\s+({_DAYS})\b", _weekday, False),  # as `when`: the most recent one
    (rf"\bthis\s+({_DAYS})\b", _this, False),
    (rf"\bnext\s+({_DAYS})\b", _next, False),
]]
_CODE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.S)  # fences (even unclosed), inline code
# already dated: "(2026-11-23)" after it (a second save), or an absolute date ("on Monday, 30
# Nov"); a possessive ("yesterday's") is stepped over first
_DATED = re.compile(rf"(?:['’]s)?(?:\s*\(\d{{4}}-\d\d-\d\d|,?\s+(?:the\s+)?{_DATE})", re.I)
_POSSESSIVE = re.compile(r"['’]s\b")
_PINNED = re.compile(r" \(\d{4}-\d\d-\d\d(?: to \d{4}-\d\d-\d\d)?\)")


def unpin(text: str) -> str:
    """`text` without the days `pin` added: the words the person typed."""
    return _PINNED.sub("", text)


def pin(text: str, today: date) -> str:
    """`text` with the ISO day after each relative date phrase, the person's words kept:
    "met the recruiter yesterday (2026-11-23)", "away last week (2026-11-16 to 2026-11-22)".
    Not in inline or fenced code, and not where a date already follows, so pinning twice
    changes nothing."""
    if not text:
        return text
    taken = [m.span() for m in _CODE.finditer(text)]
    adds: list[tuple[int, str]] = []
    for rx, f, counted in _PIN_RULES:
        for m in rx.finditer(text):
            s, e = m.span()
            if any(s < b and a < e for a, b in taken) or _DATED.match(text, e):
                continue
            if counted and (_VAGUE.search(m[0]) or _RANGE_BEFORE.search(text[:s][-4:])
                            or _RANGE_AFTER.match(text, e)):
                continue
            lo, hi = f(m, today)
            taken.append((s, e))
            if p := _POSSESSIVE.match(text, e):
                e = p.end()
            adds.append((e, f" ({lo.isoformat()})" if lo == hi
                         else f" ({lo.isoformat()} to {hi.isoformat()})"))
    for e, add in sorted(adds, reverse=True):
        text = text[:e] + add + text[e:]
    return text
