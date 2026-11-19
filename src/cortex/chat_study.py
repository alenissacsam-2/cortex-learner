"""Chat's study questions ("what should I study today?", "what's due?", "where am I weak?",
"what did I study yesterday?", "how am I doing?") answered from Cortex's own learner model,
never from a few retrieved passages and never by a model. Each answer reuses the planner and
the levels fold, hides what the planner hides (private domains, areas, concepts, tracks and
mistake notes: D1) and shows state words, never internal numbers. Anything else is None, so
the passages and the local model answer it."""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, time

from cortex import levels, planner, render
from cortex.catalog import Catalog
from cortex.events import EventLog
from cortex.vault import Vault, is_private

_MD = re.compile(r"[*_`\[\]<>#|]")  # Chat renders **bold**, `code` and [n]: titles stay text
SHOWN = 15

_LEAD = r"(?:(?:so|ok|okay|hey|hi|cortex|please|and|then|now|alright)\s+)*"
_TAIL = r"(?:\s+(?:please|then|now|right now|today|for today|so far|overall|again))*"
_STUDY = r"(?:study|learn|work on|practi[cs]e|focus on)"
_ASKS = {
    "plan": [
        rf"what should i {_STUDY}(?: next| first)?",
        rf"what (?:do|can) i {_STUDY}(?: next)?",
        r"what is (?:my|the) (?:study )?plan",
        r"(?:show|give|tell)(?: me)? (?:my|the) (?:study )?plan",
        r"(?:my |the )?(?:study )?plan|today'?s (?:study )?plan",
        r"what is next|what next|what is on (?:my|the) plan",
    ],
    "due": [
        r"what is due(?: for (?:review|revision))?",
        r"(?:is )?anything due(?: for (?:review|revision))?",
        r"what (?:should|do|must) i (?:review|revise)",
        r"what do i (?:need|have) to (?:review|revise)",
        r"what needs (?:review|reviewing|revision|revising|a refresh|refreshing)",
        r"what (?:reviews|concepts|topics) are due",
        r"what are my (?:reviews|due reviews|due concepts|due topics)",
    ],
    "weak": [
        r"what are my weak(?:est)? (?:topics|areas|concepts|spots|points|subjects)",
        r"(?:which|what) (?:topics|areas|concepts|subjects) am i weak(?:est)? (?:at|in|on)",
        r"where am i weak(?:est)?|what am i weak(?:est)? (?:at|in|on)",
        r"what are my weak(?:ness|nesse)s|what am i bad at|what do i struggle with",
        r"what mistakes do i keep making|what do i keep getting wrong",
        r"what (?:are|were) my (?:(?:common|recurring|repeated|frequent|open|recent|usual) )?"
        r"mistakes",
    ],
    "progress": [
        r"how am i (?:doing|progressing)(?: (?:with|in|on) (?:my )?(?:studies|studying|learning"
        r"|prep|preparation))?",
        r"how is my (?:progress|studying|learning|prep|preparation)(?: going)?",
        r"how are my studies going|what is my progress|my progress|progress report",
        r"(?:show|give|tell)(?: me)? my progress|how far (?:have i got|am i)",
    ],
}
_ASKS = {k: re.compile(_LEAD + "(?:" + "|".join(v) + ")" + _TAIL) for k, v in _ASKS.items()}
# the question with its date phrase cut out; a topic left over ("what did I study about
# graphs yesterday?") does not match, so the passages answer it
_RANGE = re.compile(_LEAD + r"(?:what|which (?:topics|concepts)) (?:did|have) i (?:study|studied"
                    r"|practi[cs]ed?|revised?|reviewed|review|worked on|work on)|what was i "
                    r"(?:studying|practi[cs]ing|revising|reviewing)")
STEP = {"plan": "Reading your study plan", "due": "Reading your due reviews",
        "weak": "Reading your weak spots and mistakes", "range": "Reading what you studied",
        "progress": "Reading your progress"}


def _norm(text: str) -> str:
    s = text.lower().replace("’", "'")
    s = re.sub(r"[^\w' ]+", " ", s)
    for short, long in ((r"what'?s", "what is"), (r"how'?s", "how is"), (r"i'?m", "i am")):
        s = re.sub(rf"\b{short}\b", long, s)
    return " ".join(s.split())


def kind(text: str, got=None) -> str | None:
    """Which study question `text` is (plan, due, weak, range, progress), or None. `got` is
    chat._when's (first, last, span) for its own date phrase; a range question needs one."""
    if got and _RANGE.fullmatch(_norm(text[:got[2][0]] + " " + text[got[2][1]:])):
        return "range"
    s = _norm(text)
    return next((k for k, rx in _ASKS.items() if rx.fullmatch(s)), None)


def _title(t) -> str:
    return _MD.sub("", render.plain(str(t or ""))).strip()


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' * (n != 1)}"


def _day(d: date) -> str:
    return f"{d.day} {d:%b}"  # as chat._span


def _state(st) -> str:
    s = st.state if st else "not_started"
    return f"{levels.GLYPH[s]} {levels.LABEL[s]}"


def study_answer(vault: Vault, text: str, got, today: date) -> str | None:
    """The answer to a study question about the learner's own plan, due reviews, weak spots,
    a date range's study, or progress; None for any other question."""
    k = kind(text, got)
    return None if k is None else answer(vault, k, got, today)


def answer(vault: Vault, k: str, got, today: date) -> str:
    """The answer for study question kind `k` (see `kind`) as of local day `today`."""
    day = today.isoformat()
    catalog = Catalog.load(vault)
    events = list(EventLog(vault).iter_events())
    states, mistakes = planner.current_states(vault, day, catalog, events)
    hide = planner.hidden(vault, catalog)

    def name(cid: str) -> str:
        e = catalog.entries.get(cid)
        return _title(e.title if e else cid) or cid

    if k == "plan":
        now = datetime.combine(today, time(12)).astimezone()  # the planner's local `today`
        items = [i for i in planner.today(vault, catalog, states, now=now, events=events)
                 if i["id"] not in hide]
        if not items:
            return "Nothing is planned for today: nothing is due and nothing new is ready."
        lines = [f"**Today's plan** ({_plural(len(items), 'item')}):", ""]
        for i in items[:SHOWN]:
            head = f"{levels.GLYPH[i['state']]} " if i["state"] else ""
            label = f" ({levels.LABEL[i['state']]})" if i["state"] else ""
            why = _title(i["why"])
            lines.append(f"- {head}**{_title(i['title'])}**{label}" + (f" · {why}" if why else ""))
        if len(items) > SHOWN:
            lines.append(f"- and {len(items) - SHOWN} more in Today.md")
        return "\n".join(lines)

    if k == "due":
        due = sorted(((c, st) for c, st in states.items()
                      if c not in hide and planner.is_due(st, day)),  # as the home card
                     key=lambda kv: (kv[1].state != "needs_refresh", str(kv[1].next_due or ""),
                                     kv[0]))
        if not due:
            later = min((str(st.next_due)[:10] for c, st in states.items()
                         if c not in hide and st.next_due), default=None)
            return "Nothing is due for review today." + (
                f" The next review is due on {later}." if later else "")
        lines = [f"**{_plural(len(due), 'concept')} due for review** today:", ""]
        lines += [f"- {_state(st)}: **{name(c)}**" for c, st in due[:SHOWN]]
        if len(due) > SHOWN:
            lines.append(f"- and {len(due) - SHOWN} more")
        return "\n".join(lines)

    notes = list(vault.iter_notes("mistake"))
    hush = {n.id for n in notes if is_private(n)}  # D1: a private mistake is never named
    open_m = sorted((m for m in mistakes.values() if m.status == "open" and m.id not in hush),
                    key=lambda m: (-m.count, m.id))  # as the start card

    if k == "weak":
        titles = {n.id: _title(n.fields.get("title") or n.id) for n in notes}
        weak = []
        for c, st in sorted(states.items()):
            if c in hide:
                continue
            why = [f"a confident miss on {st.confident_miss}"] if st.confident_miss else []
            if n := len([m for m in st.open_mistakes if m not in hush]):
                why.append(_plural(n, "open mistake"))
            if st.state == "needs_refresh":
                why.append("needs a refresh")
            if why:
                weak.append((not st.confident_miss, -n, c, st, why))
        if not weak and not open_m:
            return "No weak spots yet: no confident misses, open mistakes or concepts to refresh."
        lines = []
        if weak:
            lines += [f"**{_plural(len(weak), 'weak concept')}**, most urgent first:", ""]
            lines += [f"- {_state(st)}: **{name(c)}** · {', '.join(why)}"
                      for *_, c, st, why in sorted(weak, key=lambda w: w[:3])[:SHOWN]]
        if open_m:
            lines += [*([""] if lines else []),
                      f"**{_plural(len(open_m), 'open mistake')}**, most often first:", ""]
            lines += [f"- ⚑ {titles.get(m.id, _title(m.id))} · seen {m.count}×"
                      + (f", last on {m.last_day}" if m.last_day else "") for m in open_m[:SHOWN]]
        return "\n".join(lines)

    if k == "range":
        lo, hi = got[0].isoformat(), got[1].isoformat()
        when = f"on {_day(got[0])}" if lo == hi else f"from {_day(got[0])} to {_day(got[1])}"
        did: dict[str, Counter] = {}
        pc = levels.problem_concepts(vault)
        for it in levels._items(events, pc):  # void, regrade and remap applied, as the fold
            c, d = it.concept, str(it.ev.get("day") or "")
            if c is None or c in hide or not lo <= d <= hi:
                continue
            tally = did.setdefault(c, Counter())
            if it.taught:
                tally["taught"] += 1
            if it.ev.get("kind") == "ask":
                tally["asked"] += 1
            if it.verdict is not None:
                tally[it.verdict] += 1
        if not did:
            return f"Cortex has no study recorded {when}."
        lines = [f"**{_plural(len(did), 'concept')}** studied {when}:", ""]
        for c in sorted(did, key=lambda c: (-sum(did[c].values()), c))[:SHOWN]:
            t = did[c]
            graded = t["correct"] + t["partial"] + t["wrong"]
            parts = (["taught"] if t["taught"] else []) + (
                [f"asked {_plural(t['asked'], 'question')}"] if t["asked"] else [])
            if graded:
                parts.append(_plural(graded, "answer") + " graded (" + ", ".join(
                    f"{t[v]} {v}" for v in ("correct", "partial", "wrong") if t[v]) + ")")
            lines.append(f"- {_state(states.get(c))}: **{name(c)}** · {'; '.join(parts)}")
        if len(did) > SHOWN:
            lines.append(f"- and {len(did) - SHOWN} more")
        return "\n".join(lines)

    # progress: the dashboard's counts over the concepts it shows
    tally = Counter(states[e.id].state if e.id in states else "not_started"
                    for e in catalog.entries.values() if e.kind == "concept" and e.id not in hide)
    if not tally:
        return "No concepts yet: add a subject or import your notes to start."
    lines = [f"**{_plural(sum(tally.values()), 'concept')}** in Cortex:", ""]
    lines += [f"- {levels.GLYPH[s]} {levels.LABEL[s]}: {tally[s]}"
              for s in ("solid", "learning", "needs_refresh", "unverified", "not_started")
              if tally[s]]
    due = sum(1 for c, st in states.items() if c not in hide and planner.is_due(st, day))
    lines += ["", f"{_plural(due, 'concept')} due for review today, "
                  f"{_plural(len(open_m), 'open mistake')}."]
    return "\n".join(lines)
