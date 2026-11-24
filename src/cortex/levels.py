"""Levels as pure folds over events (SPEC §5, §15a.13, §15a.15-16, §15a.21, §0 U1).

Nothing here is stored: every state is recomputed from the event log on demand.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta

from fsrs import Card, Rating, Scheduler, State

from cortex import events as _events
from cortex.events import EventLog

STATES = ("not_started", "unverified", "learning", "needs_refresh", "solid")
GLYPH = {"not_started": "○", "unverified": "◌", "learning": "◐", "needs_refresh": "↻",
         "solid": "●"}
LABEL = {"not_started": "Not started", "unverified": "Unverified", "learning": "Learning",
         "needs_refresh": "Needs refresh", "solid": "Solid"}
RETENTION, HIGH_RETENTION = 0.85, 0.90
RESOLVE_EXPOSURES, RESOLVE_DAYS = 3, 2
SOLID_STABILITY, SOLID_PASS_DAYS = 10.0, 2  # Solid needs FSRS stability ≥ 10 d and 2 pass days
SOLID_GAP_DAYS = 7  # (default) Solid also needs a pass day ≥ 7 days after the first pass day
SOLID_REST_DAYS = 3  # (default) ... and ≥ 3 days after the previous graded day
REMEDIATION_MAX_STABILITY = 10.0  # wrong-then-fixed is Hard only below this stability
JITTER = 0.05  # deterministic ± spread of due dates for intervals ≥ 3 d
DEPTH = ("application", "transfer")
# `log` payload kinds _items folds into learning evidence; keep in step with its checks.
# The planner's warm-up gap counts only these logs, not always-on ledger lines (touch, need...).
EVIDENCE_LOG_KINDS = ("taught", "self_report", "problem_status")
LEECH_LAPSES = 5  # Anki's leech: forgotten this often after it was learned; rewrite, don't drill


@dataclass
class ConceptState:
    id: str
    state: str = "not_started"
    stability: float | None = None
    pass_days: int = 0
    depth_pass: bool = False
    last_pass_day: str | None = None
    interval_days: int | None = None
    next_due: str | None = None
    retrievability: float | None = None
    last_review: datetime | None = None  # the FSRS card's last review (in memory only)
    open_mistakes: list[str] = field(default_factory=list)
    unchecked: bool = False
    last_taught_day: str | None = None  # local day of the latest `taught` event
    asked_not_graded: int = 0
    attempts: int = 0
    last_seen: str | None = None
    confident_miss: str | None = None  # day of a recent confidence-4 wrong not yet passed
    gap_pass: bool = False  # a pass day ≥ SOLID_GAP_DAYS after the first, ≥ SOLID_REST_DAYS rest
    solid_after: str | None = None  # only the gap blocks Solid: a pass from this day would do
    lapses: int = 0  # days rated Again after the card was learned (`_schedule`); never cleared


def is_leech(st) -> bool:
    return getattr(st, "lapses", 0) >= LEECH_LAPSES


def gap_note(st) -> str:
    """The learner-facing reason when only the gap rule keeps a concept from Solid."""
    when = getattr(st, "solid_after", None)
    return f"one more check after a break (from {when})" if when else ""


@dataclass
class MistakeState:
    id: str
    status: str = "open"
    count: int = 0
    last_day: str | None = None
    concepts: list[str] = field(default_factory=list)
    exposures: list[str] = field(default_factory=list, repr=False)  # days since last occurrence


@dataclass
class _Item:
    """One normalised event: the concept it lands on and its verdict, if any."""
    ev: dict
    concept: str | None
    verdict: str | None = None
    hints: int = 0
    confidence: int | None = None
    taught: bool = False
    item_type: str | None = None  # recall | application | transfer; None reads as recall
    also: list[str] = field(default_factory=list)  # secondary concepts a correct credits
    credit: bool = False  # an `also` copy on a secondary concept: one Hard review, nothing else
    warm: bool = False  # the concept was already taught or exercised earlier this local day
    primed: bool = False  # ... taught or `also`-credited, not just graded (ends the cold block)


def _prefixed(value, prefix: str) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value if ":" in value else f"{prefix}:{value}"


def placed(events: Iterable[dict]) -> list[dict]:
    """The live events in (ts, id) order, each regrade (a self_report whose payload
    `regrade_of` names a voided grade: a spot-check flip, written by the server) moved to that
    grade's day, ts and place, so a verdict fixed later rates the day it was answered (B4)."""
    raw = sorted(events, key=lambda e: (e["ts"], e["id"]))
    voided = {e.get("ref") for e in raw if e.get("kind") == "void"}
    grades = {e["id"]: e for e in raw if e.get("kind") == "grade" and e["id"] in voided}
    out = []
    for e in _events.live(raw):
        pl = e.get("payload")
        r = pl.get("regrade_of") if e.get("kind") == "log" and isinstance(pl, dict) \
            and pl.get("kind") == "self_report" else None
        o = grades.get(r) if isinstance(r, str) else None
        out.append(((o or e)["ts"], (o or e)["id"],
                    {**e, "day": o["day"], "ts": o["ts"]} if o else e))
    return [e for *_, e in sorted(out, key=lambda t: t[:2])]


def _items(events: Iterable[dict], problem_concepts: dict[str, str | list[str]] | None,
           ) -> Iterator[_Item]:
    """Apply void, regrade and remap, resolve each event's concept, and pull out its verdict."""
    evs = placed(events)
    remap = {e["ref"]: e["object"] for e in evs
             if e.get("kind") == "remap" and e.get("ref") and e.get("object")}
    asks = {e["id"]: e for e in evs if e.get("kind") == "ask"}
    problems = problem_concepts or {}
    touched: set[tuple[str | None, str | None]] = set()  # (concept, day) taught or graded
    primed: set[tuple[str | None, str | None]] = set()  # (concept, day) taught or also-credited
    solved: dict[str, str] = {}  # problem id -> first day it had a correct item
    graded: set[tuple[str | None, str | None]] = set()  # (problem id, day) with a grade

    def follow(obj: str | None) -> str | None:
        seen = set()
        while obj in remap and obj not in seen:  # transitive, cycle-safe
            seen.add(obj)
            obj = remap[obj]
        return obj

    def concept_of(obj: str | None) -> str | None:
        obj = follow(obj)
        return obj.split(":", 1)[1] if isinstance(obj, str) and obj.startswith(
            "concept:") else None

    def linked(pid: str | None) -> list[str]:  # a problem's concepts, primary first
        cs = problems.get(pid.split(":", 1)[1]) if pid else None
        return [cs] if isinstance(cs, str) else list(cs or [])

    for ev in evs:
        kind = ev.get("kind")
        if kind in ("remap", "resume", "close", "open", "change"):
            continue
        if kind == "grade" and ev.get("ref") in asks:  # grade of a registered ask item
            ask = asks[ev["ref"]]
            ev = {**{k: ask[k] for k in ("object", "problem", "also", "targets_mistakes",
                                         "item_type") if k in ask}, **ev}
            if ask.get("targets_mistakes"):  # a grade may name its own too: take the union
                ev["targets_mistakes"] = list(dict.fromkeys(
                    [*ask["targets_mistakes"], *(ev.get("targets_mistakes") or [])]))
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        pkind = payload.get("kind") if kind == "log" else None
        if pkind is not None and pkind not in EVIDENCE_LOG_KINDS:
            pkind = None  # touch, need, journal...: never taught, verdict or problem evidence

        def get(name, ev=ev, payload=payload):
            return ev.get(name, payload.get(name))

        obj = (ev.get("object") or _prefixed(payload.get("concept"), "concept")
               or ev.get("problem") or _prefixed(payload.get("problem"), "problem"))
        obj = follow(obj)
        on_problem = isinstance(obj, str) and obj.startswith("problem:")
        pid = obj if on_problem else _prefixed(get("problem"), "problem")
        concept = concept_of(_prefixed((linked(pid) or [None])[0], "concept") if on_problem
                             else obj)
        raw = get("also") if isinstance(get("also"), list) else []
        also = [concept_of(_prefixed(a, "concept")) for a in [*raw, *linked(pid)[1:]]]

        item = _Item(ev, concept, taught=kind == "taught" or pkind == "taught",
                     also=[a for a in dict.fromkeys(also) if a and a != concept])
        day = ev.get("day")
        if kind == "grade" or pkind == "self_report":
            item.verdict = get("verdict")
            graded.add((pid, day))
        elif pkind == "problem_status" and payload.get("status") == "solved":
            # after that day's graded answer (a mock code phase) it is bookkeeping (board L2)
            item.verdict = None if pid and (pid, day) in graded else "correct"
        elif pkind == "problem_status" and payload.get("status") == "todo":
            continue  # a plan, not evidence
        if item.verdict not in ("correct", "partial", "wrong"):
            item.verdict = None
        item.hints = get("hints") if isinstance(get("hints"), int) else 0
        item.confidence = get("confidence")
        item.item_type = ev.get("item_type")
        if item.item_type is None and (get("problem") or pkind == "problem_status"):
            # solving a problem applies the concept; re-solving one already solved correctly
            # on an earlier day recalls a remembered solution (LEARN-7)
            item.item_type = ("recall" if day and pid in solved and solved[pid] < day
                              else "application")
        if kind != "ask":
            item.warm = (concept, day) in touched
            item.primed = (concept, day) in primed
            if item.taught or item.verdict is not None:
                touched.add((concept, day))
            if item.taught:
                primed.add((concept, day))
            if item.verdict == "correct":
                touched.update((a, day) for a in item.also)
                primed.update((a, day) for a in item.also)
                if pid:
                    solved.setdefault(pid, day)
        yield item


def _score(it: _Item) -> float:
    if it.verdict == "correct":
        return 1.0 if it.hints <= 1 else 0.5
    return 0.5 if it.verdict == "partial" else 0.0


def _rating(it: _Item) -> Rating:
    if it.verdict == "wrong":
        return Rating.Again
    if it.verdict == "partial" or it.hints >= 2:
        return Rating.Hard
    if it.hints == 0 and (it.confidence == 4 or (
            it.item_type == "transfer" and isinstance(it.confidence, int) and it.confidence >= 3)):
        return Rating.Easy
    return Rating.Good


def _pass_day(primary: list[_Item]) -> bool:
    """Primary share ≥ 0.8 and no confidence-4 wrong."""
    return (sum(map(_score, primary)) / len(primary) >= 0.8
            and not any(i.verdict == "wrong" and i.confidence == 4 for i in primary))


def _review_at(day: str) -> datetime:
    """One FSRS review per local day at 12:00 UTC of that day, so consecutive days are exactly
    1 d apart (py-fsrs floors elapsed days; under 24 h is a same-day review)."""
    return datetime.combine(date.fromisoformat(day), time(12), UTC)


def _cold_block(primary: list[_Item]) -> list[_Item]:
    """The leading primary items before the first teaching (a `taught` or `also` credit on
    the concept that day); empty when the day starts warm."""
    if primary[0].warm:
        return []
    n = 1
    while n < len(primary) and not primary[n].primed:
        n += 1
    return primary[:n]


def _day_rating(items: list[_Item], card: Card) -> Rating:
    """Rate the day by its cold retrieval (LEARN-2, G1): the cold block is every primary item
    before the first teaching that day (a `taught` or `also` credit on the concept), starting
    only when nothing on the concept came earlier that local day. Later quiz questions are
    usually different questions, not retries, so the block is rated by its pass share: all Easy
    → Easy, ≥ 0.8 → Good, ≥ 0.5 → Hard, else Again; items after the teaching do not cap it.
    Items after teaching follow it, so they do not measure retention across the gap. A single
    cold item is rated alone (hints ≥ 2 already make it Hard), and Good/Easy on a day that is
    not a pass day is capped at Hard. With no cold item (taught first) the worst primary rating
    is used, as before. A day of `also` credit only is Hard. A day with a confidence-4 wrong is
    Again: a confident miss is never softened, so it never lengthens the interval. A wrong
    fixed later the same day (last primary correct at hints ≤ 1, after the cold block) is Hard
    while the card is new or below REMEDIATION_MAX_STABILITY and the last review is under
    SOLID_GAP_DAYS old: Hard after a long gap is read by FSRS as a hard-won recall at low R and
    inflates stability, so a lapse across a gap or on a mature card stays Again. The same guards
    hold inside the block: a Hard share that includes a wrong is Again across a gap or on a
    mature card (a cold right-then-wrong pair is still a lapse there)."""
    primary = [i for i in items if not i.credit]
    if not primary:
        return Rating.Hard
    if any(i.verdict == "wrong" and i.confidence == 4 for i in primary):
        return Rating.Again
    after_gap = card.last_review is not None and (  # last_review is `_review_at` its day
        date.fromisoformat(primary[0].ev["day"]) - card.last_review.date()).days >= SOLID_GAP_DAYS
    guarded = after_gap or (card.stability or 0) >= REMEDIATION_MAX_STABILITY
    batch = _cold_block(primary)
    if len(batch) >= 2:
        share = sum(map(_score, batch)) / len(batch)
        rating = (Rating.Easy if all(_rating(i) == Rating.Easy for i in batch)
                  else Rating.Good if share >= 0.8
                  else Rating.Hard if share >= 0.5 else Rating.Again)
        if rating == Rating.Hard and guarded and any(i.verdict == "wrong" for i in batch):
            rating = Rating.Again  # a lapse in the block: the same guards as remediation
    elif primary[0].warm:
        rating = min(map(_rating, primary))
    else:
        rating = _rating(primary[0])
        if rating > Rating.Hard and not _pass_day(primary):
            rating = Rating.Hard
    if (rating == Rating.Again and len(primary) > len(batch)  # the fix came after the block
            and primary[-1].verdict == "correct" and primary[-1].hints <= 1
            and not guarded):
        return Rating.Hard
    return rating


def _schedule(per_day: dict[str, list[_Item]], retention: float = RETENTION,
              ) -> tuple[Scheduler, Card, int]:
    """One FSRS-6 card replayed with one review per local day (see `_day_rating`), and its
    lapses: days rated Again on a card in Review state that had a day rated above Again before.
    With no learning steps py-fsrs puts a card in Review after its first day whatever the
    rating, so the earlier non-Again day is what marks it learned (a miss while learning is not
    a lapse)."""
    scheduler = Scheduler(desired_retention=retention, learning_steps=(), relearning_steps=(),
                          enable_fuzzing=False)
    card = Card(card_id=1)
    lapses, learned = 0, False
    for day in sorted(per_day):
        rating = _day_rating(per_day[day], card)
        lapses += rating == Rating.Again and learned and card.state == State.Review
        learned = learned or rating != Rating.Again
        card, _ = scheduler.review_card(card, rating, _review_at(day))
    return scheduler, card, lapses


def _jitter(concept: str, review_count: int, interval: int) -> int:
    """Days to shift a due date: ±JITTER of the interval, seeded by sha256 (`hash()` changes
    per process), only for intervals of 3 days or more."""
    if interval < 3:
        return 0
    u = int(hashlib.sha256(f"{concept}:{review_count}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return round(interval * (2 * u - 1) * JITTER)


def _credit(it: _Item, concept: str) -> _Item:
    return replace(it, concept=concept, verdict=None, also=[], credit=True)


def _retrievability(scheduler: Scheduler, card: Card, now: datetime) -> float:
    """FSRS-6 forgetting curve over fractional elapsed days (py-fsrs floors to whole days,
    so a review under 24 h ago would read R = 1)."""
    if card.last_review is None or card.stability is None:
        return 0.0
    t = max((now - card.last_review).total_seconds() / 86400, 0.0)
    decay = -scheduler.parameters[20]
    factor = 0.9 ** (1 / decay) - 1
    return (1 + factor * t / card.stability) ** decay


_CURVE = Scheduler()  # default FSRS-6 parameters: retention changes intervals, not the curve


def recall_at(st: ConceptState | None, when: datetime) -> float:
    """Retrievability of the fold's card (unjittered) at `when`; 0 with no reviews (no memory
    to forecast; `predicted_p` keeps its 0.5 prior for single-item forecasts)."""
    if st is None or st.last_review is None or st.stability is None:
        return 0.0
    card = Card(card_id=1, stability=st.stability, last_review=st.last_review)
    return _retrievability(_CURVE, card, when.replace(tzinfo=UTC) if when.tzinfo is None else when)


def fold(events: Iterable[dict], today: str,
         problem_concepts: dict[str, str | list[str]] | None = None,
         high_retention: set[str] | None = None,
         ) -> tuple[dict[str, ConceptState], dict[str, MistakeState]]:
    """Replay events into concept and mistake states as of local day `today`.

    `problem_concepts` maps a problem id to its concepts (primary first) or just its primary.

    Concepts in `high_retention` (tested by an exam soon) are scheduled at 0.90, others 0.85.
    """
    events = list(events)
    concepts: dict[str, ConceptState] = {}
    mistakes: dict[str, MistakeState] = {}
    days: dict[str, dict[str, list[_Item]]] = {}  # concept -> day -> verdict/credit items
    evidence: set[str] = set()
    last_taught: dict[str, int] = {}
    last_verdict: dict[str, int] = {}
    # a voided grade still settles its ask (B1): a void or a flip must not reopen it for good
    graded_asks = {e.get("ref") for e in events if e.get("kind") == "grade"}
    asks: list[_Item] = []

    for n, it in enumerate(_items(events, problem_concepts)):
        ev, c = it.ev, it.concept
        kind, day = ev.get("kind"), ev.get("day")
        if c is None:
            st = None
        else:
            st = concepts.setdefault(c, ConceptState(c))
            st.last_seen = max(filter(None, (st.last_seen, day)), default=None)
        if kind == "ask":
            asks.append(it)
            continue
        if c is not None:
            evidence.add(c)
        if it.taught and c is not None:
            last_taught[c] = n
            st.last_taught_day = day
        if it.verdict is None:
            continue
        if st is not None:
            st.attempts += 1
            last_verdict[c] = n
            days.setdefault(c, {}).setdefault(day, []).append(it)
        if it.verdict == "correct":  # a wrong never touches the also-concepts
            for a in it.also:
                sa = concepts.setdefault(a, ConceptState(a))
                sa.last_seen = max(filter(None, (sa.last_seen, day)), default=None)
                evidence.add(a)
                days.setdefault(a, {}).setdefault(day, []).append(_credit(it, a))
        if it.verdict in ("wrong", "partial"):
            for m in ev.get("mistakes") or []:
                ms = mistakes.setdefault(m, MistakeState(m))
                ms.status, ms.count, ms.last_day, ms.exposures = "open", ms.count + 1, day, []
                if c is not None and c not in ms.concepts:
                    ms.concepts.append(c)
        elif it.verdict == "correct":
            for m in ev.get("targets_mistakes") or []:
                ms = mistakes.get(m)
                if ms is None or ms.status != "open":
                    continue
                ms.exposures.append(day)
                if (len(ms.exposures) >= RESOLVE_EXPOSURES
                        and len(set(ms.exposures)) >= RESOLVE_DAYS):
                    ms.status = "resolved"

    for it in asks:
        if it.concept is not None and it.ev["id"] not in graded_asks:
            concepts[it.concept].asked_not_graded += 1

    today_d = date.fromisoformat(today)
    noon = datetime.combine(today_d, time(12)).astimezone(UTC)  # naive -> system zone
    high = high_retention or set()
    for c, st in concepts.items():
        last_graded = None  # last day with primary items; `also` credit never passes a day
        first_pass = None
        for day in sorted(days.get(c, {})):
            items = [i for i in days[c][day] if not i.credit]
            if not items:
                continue
            d = date.fromisoformat(day)
            # a gap pass: ≥ 7 days after the first pass day and ≥ 3 days off before it (§15a.25)
            gap = first_pass is not None and (
                (d - date.fromisoformat(first_pass)).days >= SOLID_GAP_DAYS
                and (d - date.fromisoformat(last_graded)).days >= SOLID_REST_DAYS)
            last_graded = day
            for i in items:  # a correct on a later day clears a confident miss (§15a.20)
                if i.verdict == "wrong" and i.confidence == 4:
                    st.confident_miss = day
                elif (i.verdict == "correct" and i.hints <= 1 and st.confident_miss
                      and day > st.confident_miss):
                    st.confident_miss = None
                if i.verdict == "correct" and i.hints <= 1 and i.item_type in DEPTH:
                    st.depth_pass = True
            if _pass_day(items):
                st.pass_days, st.last_pass_day = st.pass_days + 1, day
                st.gap_pass = st.gap_pass or gap
                first_pass = first_pass or day
        if st.confident_miss and (today_d - date.fromisoformat(st.confident_miss)).days >= 7:
            st.confident_miss = None
        if c in days:
            scheduler, card, st.lapses = _schedule(days[c],
                                                   HIGH_RETENTION if c in high else RETENTION)
            last = date.fromisoformat(max(days[c]))
            interval = (card.due.date() - last).days  # due is _review_at(last) + interval
            st.interval_days = interval + _jitter(c, len(days[c]), interval)
            st.next_due = (last + timedelta(days=st.interval_days)).isoformat()
            st.stability = round(card.stability, 2)
            st.retrievability = round(_retrievability(scheduler, card, noon), 3)  # unjittered
            st.last_review = card.last_review
        st.unchecked = c in last_taught and last_taught[c] > last_verdict.get(c, -1)
        st.open_mistakes = sorted(m for m, ms in mistakes.items()
                                  if ms.status == "open" and c in ms.concepts)
        if c not in evidence:
            st.state = "not_started"
        elif c not in days:
            st.state = "unverified"
        elif (st.stability >= SOLID_STABILITY and st.pass_days >= SOLID_PASS_DAYS
              and last_graded is not None and st.last_pass_day == last_graded
              and st.depth_pass):
            if st.gap_pass:  # Solid is shown after a real gap, not only predicted (LEARN-1)
                st.state = ("solid" if today_d <= date.fromisoformat(st.next_due)
                            else "needs_refresh")
            else:  # the label waits for the check, and the check is due on solid_after
                st.state = "learning"
                st.solid_after = max(
                    date.fromisoformat(first_pass) + timedelta(days=SOLID_GAP_DAYS),
                    date.fromisoformat(last_graded) + timedelta(days=SOLID_REST_DAYS)).isoformat()
                if st.solid_after < st.next_due:  # one place, so every due view agrees
                    st.next_due = st.solid_after
                    st.interval_days = (date.fromisoformat(st.next_due)
                                        - date.fromisoformat(max(days[c]))).days
        else:
            st.state = "learning"
    return concepts, mistakes


def predicted_p(events: Iterable[dict], concept: str, now: datetime,
                problem_concepts: dict[str, str | list[str]] | None = None) -> float:
    """Retrievability of the same FSRS-6 card `fold` builds (unjittered) from the days before
    `now`'s local day; 0.5 with no reviews. Today's items are left out: they follow feedback, so
    they do not measure what was retained (board L6)."""
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now
    today = _events.local_day(now)
    per_day: dict[str, list[_Item]] = {}
    for it in _items(events, problem_concepts):
        if it.verdict is None or it.ev.get("kind") == "ask" or it.ev["day"] >= today:
            continue
        if it.concept == concept:
            per_day.setdefault(it.ev["day"], []).append(it)
        elif it.verdict == "correct" and concept in it.also:
            per_day.setdefault(it.ev["day"], []).append(_credit(it, concept))
    if not per_day:
        return 0.5
    scheduler, card, _ = _schedule(per_day)
    return _retrievability(scheduler, card, now)


def calibration(events: Iterable[dict],
                problem_concepts: dict[str, str | list[str]] | None = None,
                ) -> dict[str, tuple[int, int]]:
    """state -> (concept-days, passes): for each local day with a graded primary item on a
    concept, its state from the events before that day, and whether the day's first item was
    a pass (correct at hints ≤ 1). Read-only; one fold per graded day.

    shortcut: exam-aware retention is ignored for past states, fine for a coarse table."""
    events = list(events)
    first: dict[str, dict[str, _Item]] = {}  # day -> concept -> first primary graded item
    for it in _items(events, problem_concepts):
        if it.verdict is not None and it.concept and it.ev.get("kind") != "ask":
            first.setdefault(it.ev["day"], {}).setdefault(it.concept, it)
    out = {s: (0, 0) for s in STATES}
    evs = placed(events)  # voids and remaps apply whenever they were logged
    for day in sorted(first):
        before, _ = fold([e for e in evs if (e.get("day") or "") < day
                          or e.get("kind") == "remap"], day, problem_concepts)
        for c, it in first[day].items():
            s = before[c].state if c in before else "not_started"
            n, ok = out[s]
            out[s] = (n + 1, ok + (it.verdict == "correct" and it.hints <= 1))
    return out


# F1, in memory only, one vault at a time (a server has one; tests make hundreds), each slot
# replaced whole by one assignment:
# "replay": (every input of the last whole-log fold, that fold). The inputs are the vault root,
#   the event files' stat keys, problem_concepts, today and high_retention; our own writes and
#   edits outside Cortex change a stat key, so the next call folds again.
# "problems": ((the vault root, its problem files' (path, mtime_ns, size)), problem_concepts)
_LAST: dict[str, tuple] = {}


def replay(vault, today: str | None = None, events: Iterable[dict] | None = None,
           high_retention: set[str] | None = None,
           ) -> tuple[dict[str, ConceptState], dict[str, MistakeState]]:
    """Fold the vault's event log (or `events`); problem primaries come from problems/*.md."""
    today = today or _events.local_day(datetime.now(UTC))
    if events is not None:
        return fold(events, today, problem_concepts(vault), high_retention)
    log = EventLog(vault)
    # keys are read before the files: a change in between only makes the next call miss
    key = (vault.root, log.signature(), problem_concepts(vault), today,
           frozenset(high_retention or ()))
    hit = _LAST.get("replay")
    if hit is None or hit[0] != key:
        hit = _LAST["replay"] = (key, fold(log.iter_events(), today, key[2], high_retention))
    return _own(hit[1][0]), _own(hit[1][1])


def _own(states: dict) -> dict:
    """A copy the caller may change (new state objects, their own lists, dicts and sets): the
    cache keeps its states as folded. A fraction of deepcopy's cost; the field types are pinned
    by tests/test_replay_cache.py so a new mutable kind cannot slip past."""
    out = {}
    for k, st in states.items():
        st = out[k] = copy.copy(st)
        for name, v in vars(st).items():
            if isinstance(v, (list, dict, set)):
                setattr(st, name, copy.copy(v))
    return out


def problem_concepts(vault) -> dict[str, list[str]]:
    """problem id -> its concept ids, primary first (the rest earn `also` credit)."""
    from cortex.index import _edge_targets  # index imports levels

    files = []
    for e in vault.note_entries("problem"):  # the listing's data, as Index._scan reads it
        try:
            st = e.stat()  # before the parse: a change after it misses next time
        except OSError:
            continue
        files.append((e.path, st.st_mtime_ns, st.st_size))
    sig = (vault.root, files)
    hit = _LAST.get("problems")
    if hit is None or hit[0] != sig:
        hit = _LAST["problems"] = (sig, {
            note.id: cs for note in vault.iter_notes("problem")
            if (cs := _edge_targets(note.fields.get("concepts") or []))})
    return {k: list(v) for k, v in hit[1].items()}  # the caller's own copy
