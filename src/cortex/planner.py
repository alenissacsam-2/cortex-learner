"""Today planning (SPEC §5 "Today", §15a.5, §15a.9, §15a.14).

Order: unchecked (newest taught first, 7 days at most), asked, confident miss, warm-up, then
refreshes and plan steps grouped by subject (a named topic's subject, then the most recently
studied subject first), new.
Warm-up + refresh + new share the cap; the others are uncapped. A plan (a track note) is an
ordered list of steps with no dates: its next STEPS_SHOWN undone steps show, then a pointer to
the track. Switching subjects only pauses a plan: nothing is "behind". Real dates belong only
to assessments (exams, interviews, deadlines). `short` mode takes 5: confident misses, then
concepts an exam within 2 days tests, then the normal order. `sweep` mode is its own short
list (`sweep`). When a due concept builds on (`requires`, transitively) a concept that is
due, Needs refresh or Learning, that ancestor leads the refreshes with one reason line, at most
one a plan (RR-19).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta

from cortex import levels
from cortex.catalog import Catalog
from cortex.events import EventLog, live, local_day
from cortex.levels import ConceptState
from cortex.vault import CHECKED_RE, UNCHECKED_RE, VaultError, invalid, is_private

CAP, SHORT_CAP = 15, 5
UNCHECKED_DAYS = 7  # a concept leaves the "Taught, not yet checked" lane after this many days
SHORT_EXAM_DAYS = 2  # short mode pulls concepts an exam this close tests to the front
WINDOW = 14  # days: the exam clock and the fail / felt-weak boost
WARMUP_GAP, WARMUP_MAX, NEW_MAX = 2, 5, 2
STEPS_SHOWN = 3  # next undone steps per plan, in the learner's order; then a "more" item
NO_PLAN_MODES = ("home", "log", "about", "week", "capture", "reflect", "project", "save", "history",
                 "mock")
SWEEP_CAP = 10
# minutes per plan item: ESTIMATES, not measurements (council item 7 guessed recall ~1,
# application ~5, problem ~15). warmup is one recall question; asked and refresh are a recall
# question plus grading; unchecked and confident_miss are an application check that may re-teach
# a gap; track items are mostly problems; new means teaching a concept, then checking it.
# `problem` and `question` are mock-paper items with and without a problem anchor.
MINUTES = {"warmup": 1, "sweep": 1, "asked": 2, "refresh": 2, "unchecked": 5,
           "confident_miss": 5, "question": 5, "track": 15, "new": 15, "problem": 15}
MINUTES_MAX = 240  # the longest session a `minutes` input may ask for
PAPER_MINUTES, PAPER_PRESETS = 45, (15, 30, 45)  # the owner has not picked the default yet
UNKNOWN_DATE = "2026-10-01"  # the removed importer's placeholder day for undated rows
STUDY_KINDS = ("ask", "grade", "taught")  # plus `log` events with an evidence payload kind


def _studied(ev: dict) -> bool:
    """Learning evidence, not an always-on ledger line (touch, need, access, journal...)."""
    p = ev.get("payload")
    return ev.get("kind") in STUDY_KINDS or (
        ev.get("kind") == "log" and isinstance(p, dict)
        and p.get("kind") in levels.EVIDENCE_LOG_KINDS)
_LINK_RE = re.compile(r"^\[\[([^\]|#^]+)(?:[|#^][^\]]*)?\]\]\s*")  # cut as index._target


def _subject(catalog, id: str | None, pcs: dict | None = None) -> str | None:
    """The subject an id belongs to: a catalog entry's domain, a problem's first concept's
    domain (`pcs`: levels.problem_concepts), else the id itself."""
    if id in (pcs or {}) and pcs[id]:
        cs = pcs[id]
        id = cs if isinstance(cs, str) else cs[0]
    e = catalog.entries.get(id) if id else None
    return (e.domain or e.id) if e else id


def plans(vault, catalog, private: set[str] | None = None, pcs: dict | None = None,
          ) -> list[dict]:
    """The non-private plans (track notes): {id, title, subject, steps[{id, title}], done,
    links}, in vault order.

    A plan is its checklist in the note's order, with no dates: `steps` are the unchecked
    items, one per linked id or text (a repeat, such as an old `## Owed` copy, is dropped),
    solved problems and private ids left out; `done` counts the checked items. Headings (old
    `## Day N` ones too) and `current_day` or date fields are ignored, so old notes still read
    and nothing is rewritten. `subject` is the track's `subject` field (a domain, area or
    concept name or id) as its domain, else the domain most of its links fall in, else the
    track id. One plan per subject is the usual shape; several may share one."""
    from cortex.index import link_targets  # index imports planner

    private = hidden(vault, catalog) if private is None else private
    pcs = levels.problem_concepts(vault) if pcs is None else pcs
    solved = {n.id for n in vault.iter_notes("problem") if n.fields.get("status") == "solved"}
    out = []
    for track in vault.iter_notes("track"):
        if is_private(track) or track.id in private:  # D1: its title and lines too
            continue
        steps, seen, done = [], set(), 0
        for line in track.body.splitlines():
            done += bool(CHECKED_RE.match(line))
            if not UNCHECKED_RE.match(line):
                continue
            text = UNCHECKED_RE.sub("", line, count=1).strip()
            m = _LINK_RE.match(text)
            id = m.group(1).strip() if m else None
            if (id or text) in seen or id in solved or id in private:
                continue  # SPEC §5: never propose a solved problem
            seen.add(id or text)
            steps.append({"id": id, "title": (text[m.end():].strip() if m else text) or id})
        links = link_targets(track.body)
        subject, name = None, track.fields.get("subject")
        if isinstance(name, str) and name.strip():
            try:
                subject = _subject(catalog, catalog.resolve(
                    name.strip().rpartition(":")[2], ("domain", "area", "concept")))
            except VaultError:
                pass
        if subject is None:
            subs = [s for t in links if (s := _subject(catalog, t, pcs)) in catalog.entries]
            subject = max(dict.fromkeys(subs), key=subs.count) if subs else track.id
        out.append({"id": track.id, "title": str(track.fields.get("title") or track.id),
                    "subject": subject, "steps": steps, "done": done, "links": links})
    return out


DEADLINE_FIELDS = ("deadline", "placement_date")  # a track's real date, first one set wins


def deadlines(vault, day: date | str) -> list[dict]:
    """A plan's real deadline (its `deadline`, else old notes' `placement_date`), which stays
    beside exams although plan steps have no dates: {kind: "deadline", id, title, date, days}
    for each non-private track dated `day` or later, soonest first (ties by id)."""
    day = date.fromisoformat(day) if isinstance(day, str) else day
    out = []
    for track in vault.iter_notes("track"):
        d = next((x for f in DEADLINE_FIELDS if (x := _day(track.fields.get(f) or ""))), None)
        if d and d >= day and not is_private(track):
            out.append({"kind": "deadline", "id": track.id,
                        "title": str(track.fields.get("title") or track.id),
                        "date": d.isoformat(), "days": (d - day).days})
    return sorted(out, key=lambda x: (x["date"], x["id"]))


def last_active(vault, catalog, events: list[dict], pcs: dict | None = None,
                tracks: list[dict] | None = None) -> dict[str, dict]:
    """subject -> {ts, day} of the latest study on it: a grade, ask, taught or evidence log on
    one of its concepts or problems, or a touch naming it, one of its pages or its plan. A
    pure fold over events and notes; `tracks` is plans() when the caller already has it."""
    pcs = levels.problem_concepts(vault) if pcs is None else pcs
    tracks = plans(vault, catalog, pcs=pcs) if tracks is None else tracks
    of_track = {t["id"]: t["subject"] for t in tracks}
    out: dict[str, dict] = {}

    def see(subject, ev):
        subject = of_track.get(subject, subject)
        ts = str(ev.get("ts") or "")
        if subject and ts > out.get(subject, {}).get("ts", ""):
            out[subject] = {"ts": ts, "day": ev.get("day")}

    for it in levels._items([e for e in events if "ts" in e and "id" in e], pcs):
        p = it.ev.get("payload")
        if it.ev.get("kind") == "log" and isinstance(p, dict) and p.get("kind") == "touch":
            for id in p.get("ids") or []:
                if isinstance(id, str):
                    see(_subject(catalog, id, pcs), it.ev)
        elif it.concept and _studied(it.ev):
            see(_subject(catalog, it.concept), it.ev)
    return out


def _by_recency(subjects: list, last: dict[str, dict]) -> list:
    """`subjects` most recently studied first; never studied last; ties keep the given order."""
    return sorted(subjects, key=lambda s: last.get(s, {}).get("ts", ""), reverse=True)


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _concepts(catalog, key) -> list[str]:
    """Concept ids for a `domain:x` / `area:x` / `concept:x` key or a bare name ([] if unknown)."""
    id = str(key).rpartition(":")[2].strip()
    if id not in catalog.entries:
        try:
            id = catalog.resolve(id, ("concept", "area", "domain"))
        except VaultError:
            return []
    e = catalog.entries[id]
    if e.kind == "concept":
        return [id]
    if e.kind == "area":  # polyhierarchy: extra `areas` count as well as the parent
        return [c.id for c in catalog.entries.values() if c.kind == "concept"
                and (c.parent == id or id in c.areas)]
    return [c.id for c in catalog.entries.values() if c.kind == "concept"
            and (c.domain == id or any(catalog.entries[a].domain == id for a in c.areas
                                       if a in catalog.entries))]


def _weights(catalog, tests) -> dict[str, float]:
    """An assessment's `tests` map as concept -> weight; keys hitting one concept add up."""
    out: dict[str, float] = {}
    for key, w in (tests if isinstance(tests, dict) else {}).items():
        try:
            w = float(w)
        except (TypeError, ValueError):
            continue
        for c in _concepts(catalog, key):
            out[c] = out.get(c, 0.0) + w
    return out


def _upcoming(vault, today: date) -> list[dict]:
    """Pending assessments dated today or later, soonest first, each with `days` to go."""
    out = []
    for a in assessments(vault):
        d = _day(a["date"]) if a["date"] else None
        if (a["outcome"] or "pending") == "pending" and d and d >= today:
            out.append({**a, "days": (d - today).days})
    return out


def exam_concepts(vault, catalog, today: str) -> set[str]:
    """Concepts tested by a pending assessment within 14 days: replay's `high_retention`."""
    return {c for a in _upcoming(vault, date.fromisoformat(today)) if a["days"] <= WINDOW
            for c in _weights(catalog, a["tests"])}


def current_states(vault, today: str, catalog=None, events=None,
                   ) -> tuple[dict[str, ConceptState], dict]:
    """The one "current states" path: levels.replay with exam-aware retention as of `today`."""
    catalog = Catalog.load(vault) if catalog is None else catalog
    return levels.replay(vault, today=today, events=events,
                         high_retention=exam_concepts(vault, catalog, today))


def _weak_boost(vault, catalog, events: list[dict], today: date) -> dict[str, float]:
    """§15a.5: for 14 days after an assessment_result, a fail boosts its tested concepts by
    their weights and each felt_weak entry boosts its concepts by 1."""
    tests = {a["id"]: a["tests"] for a in assessments(vault)}
    out: dict[str, float] = {}
    for e in live(events):
        p = e.get("payload")
        d = _day(e.get("day"))
        if (not isinstance(p, dict)
                or p.get("kind") != "assessment_result" or d is None
                or not 0 <= (today - d).days < WINDOW):
            continue
        w = _weights(catalog, tests.get(p.get("assessment"))) if p.get("outcome") == "fail" \
            else {}
        for name in p.get("felt_weak") or []:
            for c in _concepts(catalog, name):
                w[c] = max(w.get(c, 0.0), 1.0)
        for c, x in w.items():
            out[c] = max(out.get(c, 0.0), x)
    return out


def fresh_unchecked(states, day: date | str) -> list[str]:
    """Taught, not yet checked: newest taught first (ties by id), without concepts last taught
    more than UNCHECKED_DAYS before `day` (their state and other lanes stay). One rule for the
    planner lane, the start card and the session end card."""
    day = date.fromisoformat(day) if isinstance(day, str) else day

    def fresh(st):
        t = getattr(st, "last_taught_day", None)
        return getattr(st, "unchecked", False) and (
            t is None or (day - date.fromisoformat(t)).days <= UNCHECKED_DAYS)

    ids = sorted(c for c, st in states.items() if fresh(st))
    # stable sort: equal days keep id order
    return sorted(ids, key=lambda c: getattr(states[c], "last_taught_day", None) or "",
                  reverse=True)


def last_questions(vault, events: list[dict] | None = None) -> dict[str, str]:
    """concept -> the last question asked on it (ask or grade events), raw untrusted text:
    Today.md's offline cue (ideas-r2 #13). The caller escapes it."""
    events = list(EventLog(vault).iter_events()) if events is None else events
    out: dict[str, str] = {}
    for it in levels._items(events, levels.problem_concepts(vault)):  # ts order: last wins
        q = it.ev.get("question")
        if it.concept and isinstance(q, str) and q.strip():
            out[it.concept] = q
    return out


def today(vault, catalog, states, *, now: datetime, mode: str = "learn",
          topic: str | None = None, events: list[dict] | None = None,
          cap: int = CAP) -> list[dict]:
    """Today's plan: items {kind, id, title, state, why}; `state` is None for non-concepts.

    kind is unchecked | asked | confident_miss | track | warmup | refresh | new, in that order.
    """
    if mode in NO_PLAN_MODES:
        return []
    if mode == "sweep":
        return sweep(vault, catalog, states, now=now, events=events)
    day = date.fromisoformat(local_day(now))
    events = list(EventLog(vault).iter_events()) if events is None else list(events)

    def state_of(id):
        st = states.get(id)
        return st.state if st else ("not_started" if id in catalog.entries else None)

    def item(kind, id, why, title=None):
        if title is None:
            e = catalog.entries.get(id)
            title = e.title if e else id
        return {"kind": kind, "id": id, "title": title, "state": state_of(id), "why": why,
                "subject": _subject(catalog, id)}

    private = hidden(vault, catalog)  # no lane may show a private subject
    visible = sorted((c, st) for c, st in states.items() if c not in private)

    def taught_age(st):
        return (day - date.fromisoformat(st.last_taught_day)).days if st.last_taught_day else None

    def taught_why(n):
        when = None if n is None else "today" if n == 0 else "yesterday" if n == 1 \
            else f"{n} days ago"
        return f"Taught {when}, not yet checked" if when else "Taught, not yet checked"

    out = [item("unchecked", c, taught_why(taught_age(states[c])))
           for c in fresh_unchecked(dict(visible), day)]
    out += [item("asked", c, f"{st.asked_not_graded} asked, not graded")
            for c, st in visible if st.asked_not_graded]
    seen = {i["id"] for i in out}
    out += [item("confident_miss", c, f"confident miss on {st.confident_miss}")  # uncapped
            for c, st in visible if getattr(st, "confident_miss", None) and c not in seen]
    pcs = levels.problem_concepts(vault)
    tracks = plans(vault, catalog, private, pcs)
    active = last_active(vault, catalog, events, pcs, tracks)
    on_track: set[str] = set()
    steps = []  # each plan's next steps, then its pointer; plans in vault order
    if mode != "review":
        for p in tracks:
            on_track.update(p["links"])
            lane = [{**item("track", s["id"], p["title"], s["title"]), "subject": p["subject"]}
                    for s in p["steps"][:STEPS_SHOWN]]
            if len(p["steps"]) > STEPS_SHOWN:  # ideas-r2 #6: no count
                lane.append({**item("track", None, p["title"], f"More steps are in {p['title']}"),
                             "subject": p["subject"], "more": p["id"]})
            steps += lane

    taken = {i["id"] for i in out + steps} | private

    warm = []
    if mode != "review":
        studied = [e["day"] for e in events if _studied(e) and e.get("day")]
        last = _day(max(studied)) if studied else None
        if last and (day - last).days >= WARMUP_GAP:
            recent = sorted((st for st in states.values() if st.last_seen and st.id not in taken),
                            key=lambda st: (st.last_seen, st.id), reverse=True)
            warm = [item("warmup", st.id, "Warm-up after a break")
                    for st in recent[:min(WARMUP_MAX, cap)]]
            taken |= {i["id"] for i in warm}

    upcoming = _upcoming(vault, day)
    clock: dict[str, tuple[float, str]] = {}  # concept -> (max weight, soonest exam's why)
    for a in upcoming:
        if a["days"] > WINDOW:
            continue
        n = a["days"]
        when = "today" if n == 0 else "tomorrow" if n == 1 else f"in {n} days"
        why = f"a private test {when}" if a["private"] else f"{a['company'] or a['id']} test {when}"
        for c, w in _weights(catalog, a["tests"]).items():
            clock[c] = (max(w, clock[c][0]), clock[c][1]) if c in clock else (w, why)
    boost = _weak_boost(vault, catalog, events, day)
    for c, (w, _) in clock.items():
        boost[c] = max(boost.get(c, 0.0), w)

    def risk(c):
        st = states.get(c) or ConceptState(c)
        r = getattr(st, "retrievability", None)
        if r is not None:
            base = 1 - r
        elif st.last_pass_day and st.interval_days:
            base = (day - date.fromisoformat(st.last_pass_day)).days / st.interval_days
        else:
            base = 1.0  # never passed: treat as fully at risk
        return base * (1 + boost.get(c, 0.0))

    due = {c for c, st in states.items() if is_due(st, day.isoformat())}
    # only graded concepts; Not started / Unverified exam concepts enter through `new`
    pulled = {c for c in clock if state_of(c) in ("learning", "needs_refresh")}
    topic_ids = set(_concepts(catalog, topic)) if topic else set()
    ranked = sorted((c for c in due | pulled if c not in taken),
                    key=lambda c: (c not in topic_ids, -risk(c), c))

    ready = []
    if mode != "review":
        tested = {c for a in upcoming for c in _weights(catalog, a["tests"])}
        ready = [e.id for e in catalog.entries.values() if e.kind == "concept"
                 and e.id not in taken and is_ready(state_of(e.id), e, states)]
        ready.sort(key=lambda c: (c not in topic_ids, c not in on_track, c not in tested))
    room = max(cap - len(warm), 0)
    k = min(NEW_MAX, len(ready), room)  # keep 1-2 slots for new topics inside the cap

    def refresh_why(c):
        if c in clock:
            return clock[c][1]
        if getattr(states.get(c), "solid_after", None):  # only the gap check keeps it off Solid
            return "One more check after a break"
        nd = getattr(states.get(c), "next_due", None)
        return f"due {nd}" if nd else "Needs refresh"

    shown_ids = ranked[:room - k]
    lead = _weak_ancestor(catalog, states, shown_ids, due, taken, day.isoformat()) \
        if room - k >= 2 else None
    if lead:  # RR-19: the ancestor leads, inside the cap; the concept that lapsed stays
        a, c = lead
        rest = [x for x in shown_ids if x != a]
        if len(rest) >= room - k:
            rest.remove(next(x for x in reversed(rest) if x != c))
        shown_ids = [a] + rest

    # ideas-r2 #5: a refresh names one problem already solved under the concept, the oldest
    # `solved_on` first (undated last), ties by id
    models: dict[str, tuple[str, str]] = {}
    for n in vault.iter_notes("problem") if shown_ids else ():
        if n.fields.get("status") == "solved" and n.id not in private and not is_private(n):
            key = (str(n.fields.get("solved_on") or "~"), n.id)
            for c in pcs.get(n.id, []):
                models[c] = min(models.get(c, key), key)

    refresh = [{**item("refresh", c, refresh_why(c)), **({"model": models[c][1]} if c in models
                                                          else {})}
               for c in shown_ids]
    shown = {i["id"] for i in refresh}
    new = [item("new", c, "Ready to learn") for c in ready if c not in shown][:k]
    first = []
    if lead:
        first, refresh = refresh[:1], refresh[1:]
        first[0]["why"] = f"{first[0]['title']} first: {catalog.entries[lead[1]].title} " \
                          "builds on it"
    # refreshes and plan steps by subject: a named topic's subject first, then the most
    # recently studied (a paused subject just sinks); ties, and each subject's refreshes, keep
    # the risk order; a refresh in a subject with no plan is a group of its own
    groups: dict = {}
    for i in refresh + steps:
        groups.setdefault(i["subject"], []).append(i)
    named = {_subject(catalog, c) for c in topic_ids}
    order = sorted(_by_recency(list(groups), active), key=lambda s: s not in named)
    out += warm + first + [i for s in order for i in groups[s]] + new
    for i in out:  # a leech keeps its place; its why says to rewrite the note, not drill it
        if levels.is_leech(states.get(i["id"])):
            i["why"] = leech_why(states[i["id"]])
    if mode != "short":
        return out
    # tired evening: confident misses, then concepts an exam within SHORT_EXAM_DAYS tests, then
    # the normal order; one item per concept
    soon = {c for a in upcoming if a["days"] <= SHORT_EXAM_DAYS
            for c in _weights(catalog, a["tests"])}
    picked, ids = [], set()
    for n in ([n for n, i in enumerate(out) if i["kind"] == "confident_miss"]
              + [n for n, i in enumerate(out) if i["id"] in soon] + list(range(len(out)))):
        key = out[n]["id"] if out[n]["id"] is not None else n  # free-text plan steps: by slot
        if key not in ids and not out[n].get("more"):  # 5 items to do, not a pointer
            ids.add(key)
            picked.append(out[n])
    return picked[:SHORT_CAP]


def leech_why(st) -> str:
    return f"Leech: forgotten {st.lapses} times after you learned it. Simplify or split its note."


def sweep(vault, catalog, states, *, now: datetime, events: list[dict] | None = None,
          ) -> list[dict]:
    """A cold re-check of up to SWEEP_CAP concepts (ideas-r2 #3), as plan items.

    First the concepts whose only grade evidence is imported: every graded item (grade,
    self_report, solved problem_status) on them has `source: imported` or the UNKNOWN_DATE day.
    Then the most overdue other due concepts. Each group by placement weight (the highest
    `tests` weight any assessment gives the concept), then lowest recall / earliest due. The
    pick is interleaved so no two neighbours share a domain where the counts allow it.
    """
    day = local_day(now)
    events = list(EventLog(vault).iter_events()) if events is None else list(events)
    private = hidden(vault, catalog)
    concepts = {c for c, e in catalog.entries.items() if e.kind == "concept" and c not in private}
    real: dict[str, bool] = {}  # graded concept -> has evidence that is not imported
    for it in levels._items(events, levels.problem_concepts(vault)):
        if it.verdict is not None and it.concept in concepts:
            real[it.concept] = real.get(it.concept, False) or (
                it.ev.get("source") != "imported" and it.ev.get("day") != UNKNOWN_DATE)
    weight: dict[str, float] = {}
    for a in assessments(vault):
        for c, w in _weights(catalog, a["tests"]).items():
            weight[c] = max(weight.get(c, 0.0), w)

    def r(c):
        x = getattr(states.get(c), "retrievability", None)
        return 0.0 if x is None else x

    imported = sorted((c for c, has in real.items() if not has),
                      key=lambda c: (-weight.get(c, 0.0), r(c), c))
    overdue = sorted((c for c in concepts if real.get(c) and is_due(states.get(c), day)),
                     key=lambda c: (-weight.get(c, 0.0), str(states[c].next_due or ""), c))
    out = []
    for c in _interleave(catalog, (imported + overdue)[:SWEEP_CAP]):
        st = states.get(c)
        why = ("Only imported evidence" if c in imported
               else f"due {st.next_due}" if st.next_due else "Needs refresh")
        out.append({"kind": "sweep", "id": c, "title": catalog.entries[c].title,
                    "state": st.state if st else "not_started", "why": why})
    return out


def _interleave(catalog, ids: list[str]) -> list[str]:
    """`ids` reordered so no two neighbours share a domain where the counts allow it; otherwise
    the given (priority) order is kept."""
    left, out, last = list(ids), [], None
    while left:
        doms = [catalog.entries[c].domain for c in left]
        top = max(dict.fromkeys(doms), key=doms.count)  # first-listed domain wins a tie
        # a domain holding over half of what is left must go now, or two of it end up adjacent
        if top != last and 2 * doms.count(top) > len(left):
            n = doms.index(top)
        else:
            n = next((i for i, d in enumerate(doms) if d != last), 0)
        out.append(left.pop(n))
        last = catalog.entries[out[-1]].domain
    return out


def check_minutes(minutes, presets: tuple[int, ...] | None = None) -> int | None:
    """A `minutes` input checked at the boundary: None, or a whole number 1..MINUTES_MAX (one
    of `presets` when given)."""
    if minutes is None:
        return None
    if isinstance(minutes, bool) or not isinstance(minutes, int) \
            or not 1 <= minutes <= MINUTES_MAX:
        raise invalid(f"minutes must be a whole number from 1 to {MINUTES_MAX}.")
    if presets and minutes not in presets:
        raise invalid(f"minutes must be one of {', '.join(map(str, presets))}.")
    return minutes


def estimate(items: list[dict]) -> int:
    """Estimated minutes for plan items (MINUTES; a 'more' pointer is no task: 0)."""
    return sum(i["minutes"] if "minutes" in i else 0 if i.get("more")
               else MINUTES.get(i["kind"], MINUTES["question"]) for i in items)


def fit(items: list[dict], minutes: int) -> list[dict]:
    """The plan cut to a time budget: items in their given order while the estimates fit, then
    the tail is dropped (never reordered). A first item longer than the whole budget is kept
    alone, so the top priority is never dropped for being big."""
    out = []
    for i in items:
        if out and estimate(out) + estimate([i]) > minutes:
            break
        out.append(i)
    return out


def budget_note(items: list[dict], minutes: int) -> str:
    return f"about {estimate(items)} of {minutes} min (estimate)"


def mock_paper(vault, catalog, states, *, now: datetime,
               minutes: int = PAPER_MINUTES) -> list[dict]:
    """A practice paper's questions for the active assistant to write, as items {kind: paper,
    id, title, state, why, anchor, minutes}, fitted to `minutes`, domains interleaved.

    Concepts come from the soonest pending non-private assessment's `tests` weights, else (no
    such assessment) every concept with evidence. Rank: weight x (1 - recall now, + 1 with an
    open mistake), then lowest recall, then id. `anchor` is a non-private problem on the concept
    (unsolved first, each used once; the item is then a problem), else a resource covering it,
    else None. Private and hidden concepts never appear.
    """
    from cortex.index import _edge_targets  # index imports planner

    day = date.fromisoformat(local_day(now))
    private = hidden(vault, catalog)
    target, weight = None, {}
    for a in _upcoming(vault, day):
        weight = {c: w for c, w in _weights(catalog, a["tests"]).items()
                  if c not in private and w > 0}
        if not a["private"] and weight:
            target = a
            break
    if target is None:
        weight = {c: 1.0 for c, e in catalog.entries.items()
                  if e.kind == "concept" and c in states and c not in private}

    def recall(c):
        return levels.recall_at(states.get(c), now)

    def open_m(c):
        return bool(getattr(states.get(c), "open_mistakes", None))

    ranked = sorted(weight, key=lambda c: (-weight[c] * (1 - recall(c) + open_m(c)),
                                           recall(c), c))
    problems: dict[str, list[tuple[bool, str]]] = {}
    pcs = levels.problem_concepts(vault)
    for n in vault.iter_notes("problem"):
        if n.id not in private and not is_private(n):
            for c in pcs.get(n.id, []):
                problems.setdefault(c, []).append((n.fields.get("status") == "solved", n.id))
    resources: dict[str, str] = {}
    for n in vault.iter_notes("resource"):
        if not is_private(n):
            for c in _edge_targets(n.fields.get("covers") or []):
                resources[c] = min(resources.get(c, n.id), n.id)
    if target:
        n = target["days"]
        when = "today" if n == 0 else "tomorrow" if n == 1 else f"in {n} days"
        exam = f"{target['company'] or target['id']} test {when}"
    items, used = [], set()
    for c in ranked:
        prob = next((p for _, p in sorted(problems.get(c, [])) if p not in used), None)
        used.add(prob)
        st = states.get(c)
        why = ([f"{exam} (weight {weight[c]:g})"] if target else []) \
            + (["open mistake"] if open_m(c) else []) \
            + [f"recall {recall(c):.0%}" if getattr(st, "last_review", None) else "not yet checked"]
        items.append({"kind": "paper", "id": c, "title": catalog.entries[c].title,
                      "state": st.state if st else "not_started", "why": " · ".join(why),
                      "anchor": prob or resources.get(c),
                      "minutes": MINUTES["problem" if prob else "question"]})
    kept = {i["id"]: i for i in fit(items, minutes)}
    return [kept[c] for c in _interleave(catalog, list(kept))]


def hidden(vault, catalog, private: set[str] | None = None) -> set[str]:
    """Ids generated views must not show: private domains, areas and concepts, and every
    catalog entry under a private domain or area (as the dashboard hides them). `private`, the
    flagged domain/area/concept ids, saves the vault walk when the caller already has them.
    Also every mistake note that is private or about a hidden concept (OQ-22: a mistake is a
    study item, `access.audience_of_study`)."""
    from cortex import access
    from cortex.index import _edge_targets  # index imports planner

    if private is None:
        private = {n.id for k in ("domain", "area", "concept") for n in vault.iter_notes(k)
                   if is_private(n)}
    out = private | {e.id for e in catalog.entries.values() if e.parent in private
                     or e.domain in private or any(a in private for a in e.areas)}
    return out | {n.id for n in vault.iter_notes("mistake") if access.audience_of_study(
        _edge_targets(n.fields.get("concepts") or []), out, note_private=is_private(n))
        is access.Audience.LOCAL}


def hidden_mistakes(mistakes: dict, hide: set[str]) -> set[str]:
    """The folded mistakes (levels.fold) a view must not name: hidden ones (`hidden`) and any
    graded under a hidden concept, even with no note linking it (OQ-22)."""
    return {m.id for m in mistakes.values()
            if m.id in hide or any(c in hide for c in m.concepts)}


def is_due(st, day: str) -> bool:
    """Due on local day `day`: Needs refresh, or a next_due on or before it. Shared by the
    planner and cortex_find(due)."""
    return st is not None and (st.state == "needs_refresh"
                               or bool(st.next_due and str(st.next_due)[:10] <= day))


def is_ready(state: str | None, concept, states: dict) -> bool:
    """Ready to learn: not yet learned (not Solid, not Needs refresh) and every prerequisite
    of the catalog entry `concept` is Solid or has SOLID_PASS_DAYS pass days (unlocking needs
    "learned", not the gap-checked "retained", so the sprint never waits a week). Shared by
    the planner and cortex_find(ready)."""
    return state in ("not_started", "unverified", "learning") and all(
        getattr(states.get(r), "state", None) == "solid"
        or getattr(states.get(r), "pass_days", 0) >= levels.SOLID_PASS_DAYS
        for r in concept.requires)


def _weak_ancestor(catalog, states, ids: list[str], due: set[str], taken: set[str], day: str
                   ) -> tuple[str, str] | None:
    """RR-19: (ancestor, concept) for the first due concept in `ids` with a `requires` ancestor
    (nearest first) that is due, Needs refresh or Learning, and not already listed or private
    (`taken`): a lapse often sits in what the concept builds on. One per plan."""
    for c in ids:
        if c not in due:
            continue
        seen, queue = {c}, list(getattr(catalog.entries.get(c), "requires", []))
        while queue:
            a = queue.pop(0)
            if a in seen or (e := catalog.entries.get(a)) is None:
                continue
            seen.add(a)
            st = states.get(a)
            if a not in taken and (is_due(st, day) or getattr(st, "state", None) == "learning"):
                return a, c
            queue += e.requires
    return None


def readiness(vault, catalog, states, assessment) -> float | None:
    """Weighted share of the assessment's tested concepts that are Solid (§15a.14).

    `assessment` is an id or a dict from assessments(); None when it tests nothing known.
    """
    return readiness_parts(vault, catalog, states, assessment)["weighted"]


def readiness_parts(vault, catalog, states, assessment) -> dict:
    """D3: {solid, total, weighted}: the plain count of tested concepts that are Solid, of how
    many, and readiness's weighted share (None when it tests nothing known), so a view can
    label each honestly. Hidden concepts count in none of them."""
    _, w, total = _tested(vault, catalog, assessment)
    solid = [c for c in w if getattr(states.get(c), "state", None) == "solid"]
    return {"solid": len(solid), "total": len(w),
            "weighted": sum(w[c] for c in solid) / total if total else None}


def recall_on_day(vault, catalog, states, assessment, now: datetime) -> float | None:
    """Forecast: the weighted mean FSRS recall of the tested concepts at local noon of the
    assessment day (or `now`, if later), with readiness's weights. None when it tests nothing
    known or has no date. A prediction from default FSRS parameters, never a score."""
    a, w, total = _tested(vault, catalog, assessment)
    d = _day(a["date"]) if a.get("date") else None
    if not total or d is None:
        return None
    when = max(datetime.combine(d, time(12)).astimezone(UTC),  # naive -> system zone, as fold
               now.replace(tzinfo=UTC) if now.tzinfo is None else now)
    # shortcut: a never-reviewed concept counts as 0, not predicted_p's 0.5 prior: there is no
    # memory to forecast, and first reviews passed only 27% (eval/learning-baseline.md); this
    # undercounts what a student knows from before Cortex; revisit at >= 30 first-review pairs
    return sum(x * levels.recall_at(states.get(c), when) for c, x in w.items()) / total


def _tested(vault, catalog, assessment) -> tuple[dict, dict[str, float], float]:
    """(assessment dict, positive weights of its known, non-hidden concepts, their sum);
    `assessment` is an id or dict."""
    a = assessment if isinstance(assessment, dict) else next(
        (x for x in assessments(vault) if x["id"] == assessment), {})
    hide = hidden(vault, catalog)
    w = {c: x for c, x in _weights(catalog, a.get("tests")).items()
         if x > 0 and c not in hide}  # <=0 breaks 0..1
    return a, w, sum(w.values())


def week_summary(vault, catalog, *, now: datetime) -> dict:
    """The last 7 local days as plain data (§15a.9 `week` mode)."""
    to = date.fromisoformat(local_day(now))
    start = to - timedelta(days=6)
    cut = (start - timedelta(days=1)).isoformat()  # the state just before the week
    events = list(EventLog(vault).iter_events())
    week = [e for e in events if start.isoformat() <= str(e.get("day", "")) <= to.isoformat()]
    # live grades, and a spot-check flip's regrade as one grade on its original day (B8)
    verdicts = [e.get("verdict") if e.get("kind") == "grade" else e["payload"].get("verdict")
                for e in levels.placed(events)
                if start.isoformat() <= str(e.get("day", "")) <= to.isoformat()
                and (e.get("kind") == "grade" or isinstance(e.get("payload"), dict)
                     and "regrade_of" in e["payload"])]
    before, m_before = current_states(vault, cut, catalog,
                                      [e for e in events if str(e.get("day", "")) <= cut])
    after, m_after = current_states(vault, to.isoformat(), catalog, events)

    def st(states, c):
        return states[c].state if c in states else "not_started"

    def status(ms, m):
        return ms[m].status if m in ms else None

    hide = hidden(vault, catalog)  # the week summary reaches MCP: no private subject in it
    hide |= hidden_mistakes(m_after, hide) | hidden_mistakes(m_before, hide)
    changed = [{"id": c, "title": getattr(catalog.entries.get(c), "title", c),
                "before": st(before, c), "after": st(after, c)}
               for c in sorted(set(before) | set(after))
               if st(before, c) != st(after, c) and c not in hide]
    return {
        "from": start.isoformat(), "to": to.isoformat(),
        "sessions": len({e["session"] for e in week if e.get("kind") != "open"
                         and str(e.get("session")).startswith("s_")}),
        "grades": len(verdicts),
        "correct": verdicts.count("correct"),
        "changed": changed,
        "mistakes_opened": sorted(m for m in m_after if status(m_after, m) == "open"
                                  and status(m_before, m) != "open" and m not in hide),
        "mistakes_resolved": sorted(m for m in m_after if status(m_after, m) == "resolved"
                                    and status(m_before, m) != "resolved" and m not in hide),
        "upcoming": [{k: a[k] for k in ("id", "company", "kind", "date", "days")}
                     | {"readiness": readiness(vault, catalog, after, a),
                        "recall_on_day": recall_on_day(vault, catalog, after, a, now)}
                     for a in _upcoming(vault, to) if a["days"] <= WINDOW],
    }


def next_up(vault, catalog, states, *, now: datetime, events: list[dict] | None = None,
            ) -> list[dict]:
    """What to do next, by subject: the home page's "Next up".

    today()'s learn plan (private subjects already left out) grouped by subject, the most
    recently studied subject first (last_active: grades, asks, taught, touches), never studied
    last. Each group is {subject, title, last_day, items}, items in plan order: anything in
    the subject taught but unchecked, asked, missed or a warm-up, its due reviews, its plan's
    next undone steps (no dates), then new concepts. A subject left alone for a while
    just drops down: nothing is "behind" or overdue. Items with no subject (ids outside the
    catalog) form a last group with subject None. Deterministic for the same notes and events.
    `states` is current_states()[0] for the same day.
    """
    events = list(EventLog(vault).iter_events()) if events is None else list(events)
    items = today(vault, catalog, states, now=now, events=events)
    pcs = levels.problem_concepts(vault)
    tracks = plans(vault, catalog, pcs=pcs)
    last = last_active(vault, catalog, events, pcs, tracks)
    names = {t["id"]: t["title"] for t in tracks}
    groups: dict = {}
    for i in items:
        groups.setdefault(i["subject"], []).append(i)
    order = _by_recency([s for s in groups if s is not None], last) + (
        [None] if None in groups else [])
    return [{"subject": s,
             "title": (catalog.entries[s].title if s in catalog.entries
                       else names.get(s, s) if s else "Other"),
             "last_day": last.get(s, {}).get("day"), "items": groups[s]} for s in order]


def assessments(vault) -> list[dict]:
    """Assessment notes as {id, company, kind, date, outcome} strings plus `tests` and
    `private`, by date."""
    out = []
    for note in vault.iter_notes("assessment"):
        f = note.fields
        tests = f.get("tests")
        out.append({"id": note.id, **{k: str(f[k]) if f.get(k) is not None else None
                                      for k in ("company", "kind", "date", "outcome")},
                    "tests": dict(tests) if isinstance(tests, dict) else {},
                    "private": is_private(note)})
    return sorted(out, key=lambda a: (a["date"] is None, a["date"] or "", a["id"]))
