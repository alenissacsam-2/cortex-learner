import shutil
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from ulid import ULID

from cortex import events, levels, planner
from cortex.catalog import Catalog
from cortex.events import EventLog, log_assessment
from cortex.levels import ConceptState, replay
from cortex.planner import readiness, today, week_summary
from cortex.render import today_md
from cortex.vault import Vault

FIXTURES = Path(__file__).parent / "fixtures"
ORDER = ["unchecked", "asked", "confident_miss", "track", "warmup", "refresh", "new"]
_local_day = events.local_day


def at(day: int) -> datetime:
    return datetime(2026, 11, day, 12, tzinfo=UTC)


NOW = at(15)  # the fixture's Acme OA (2026-11-20) is 5 days away


@pytest.fixture
def v(tmp_path, monkeypatch):
    # Days follow UTC in tests so the expected dates do not depend on the machine's zone.
    utc_day = lambda ts, tz=None: _local_day(ts, UTC)  # noqa: E731
    monkeypatch.setattr(events, "local_day", utc_day)
    monkeypatch.setattr(planner, "local_day", utc_day)
    root = tmp_path / "vault"
    shutil.copytree(FIXTURES / "vault_mini", root)
    return Vault(root)


def no_exam(v):
    (v.root / "assessments/acme-oa-2026.md").unlink()


def cat(v, *concepts):
    """The fixture catalog plus concepts (title, requires) under dsa.arrays."""
    c = Catalog.load(v)
    for title, requires in concepts:
        c.add("concept", title, parent="dsa.arrays", requires=requires)
    return Catalog.load(v)


def due(id, r=None, **kw):
    return ConceptState(id, state="needs_refresh", retrievability=r, **kw)


def kinds(items):
    return [i["kind"] for i in items]


def plan(v, states, catalog=None, **kw):
    kw.setdefault("events", [])
    return today(v, catalog or Catalog.load(v), states, now=kw.pop("now", NOW), **kw)


# ---- ordering, cap, track ------------------------------------------------------------

def _mixed(v):
    no_exam(v)
    catalog = cat(v, ("Hash maps", []))
    states = {
        "arrays-basics": ConceptState("arrays-basics", "unverified", unchecked=True,
                                      last_seen="2026-11-10"),
        "two-pointers": ConceptState("two-pointers", "learning", asked_not_graded=1,
                                     last_seen="2026-11-10"),
        "seen": ConceptState("seen", "learning", last_seen="2026-11-09"),
        "old": due("old", 0.6, last_seen="2026-11-01"),
        "risky": due("risky", 0.2),
        "missed": ConceptState("missed", "learning", confident_miss="2026-11-14"),
    }
    evs = [{"id": "e1", "kind": "grade", "day": "2026-11-10"}]
    return catalog, states, evs


def test_kind_order(v):
    catalog, states, evs = _mixed(v)
    items = plan(v, states, catalog, events=evs)
    ks = kinds(items)
    assert set(ks) == set(ORDER)
    assert ks == sorted(ks, key=ORDER.index)
    assert [i["id"] for i in items if i["kind"] == "warmup"] == ["seen", "old"]
    assert [i["id"] for i in items if i["kind"] == "refresh"] == ["risky"]
    assert [(i["id"], i["why"]) for i in items if i["kind"] == "new"] == [
        ("hash-maps", "Ready to learn")]
    track = [i["why"] for i in items if i["kind"] == "track"]
    assert track == ["Sprint plan · Day 2"] * 4 + ["Owed"]


def test_cap_holds_with_40_due(v):
    no_exam(v)
    states = {f"c{n:02}": due(f"c{n:02}", n / 100) for n in range(40)}
    items = plan(v, states)
    capped = [i for i in items if i["kind"] in ("warmup", "refresh", "new")]
    assert len(capped) == 15
    refresh = [i["id"] for i in items if i["kind"] == "refresh"]
    assert refresh[:3] == ["c00", "c01", "c02"]  # lowest R = highest risk first
    assert [i["id"] for i in items if i["kind"] == "new"] == ["arrays-basics"]
    assert kinds(items).count("track") == 5


def test_track_items_uncapped(v):
    no_exam(v)
    path = v.root / "tracks/sprint-test.md"
    text = path.read_text(encoding="utf-8")
    extra = "".join(f"- [ ] Drill {n}\n" for n in range(20))
    path.write_text(text.replace("## Owed", extra + "\n## Owed"), encoding="utf-8")
    items = plan(v, {f"c{n}": due(f"c{n}", 0.5) for n in range(10)}, cap=3)
    assert kinds(items).count("track") == 25
    assert len([i for i in items if i["kind"] != "track"]) == 3


def test_owed_folds_to_first_three_and_a_pointer(v):
    """ideas-r2 #6: Day items stay uncapped; Owed shows its first 3 unchecked items in the
    track's order, then one pointer to the track (no count, no id); the page is untouched."""
    no_exam(v)
    path = v.root / "tracks/sprint-test.md"
    owed = "".join(f"- [ ] Owed {n}\n" for n in range(6))
    text = path.read_text(encoding="utf-8").replace("## Buffer", owed + "\n## Buffer")
    path.write_text(text, encoding="utf-8")
    items = plan(v, {})
    owed_items = [i for i in items if i["why"] == "Owed"]
    assert [i["title"] for i in owed_items] == [
        "Two Sum II (owed copy)", "Owed 0", "Owed 1", "More owed items are in Test sprint"]
    more = owed_items[-1]
    assert more == {"kind": "track", "id": None, "title": "More owed items are in Test sprint",
                    "state": None, "why": "Owed", "more": "sprint-test"}
    assert len([i for i in items if i["why"].startswith("Sprint plan")]) == 4
    assert path.read_text(encoding="utf-8") == text
    assert not any(i.get("more") for i in plan(v, {}, mode="short"))
    md = today_md(today=items, sprint_day=2, assessments=[], as_of="2026-11-15")
    assert "- ▫ · More owed items are in [[sprint-test]] · Owed" in md.splitlines()
    three = text.replace("".join(f"- [ ] Owed {n}\n" for n in range(2, 6)), "")
    path.write_text(three, encoding="utf-8")  # exactly 3 unchecked Owed items: no pointer
    assert [i["title"] for i in plan(v, {}) if i["why"] == "Owed"] == [
        "Two Sum II (owed copy)", "Owed 0", "Owed 1"]


def test_solved_problem_skipped(v):
    v.set_field("problem", "lc-167", "status", "solved")
    track = [i for i in plan(v, {}) if i["kind"] == "track"]
    assert "lc-167" not in {i["id"] for i in track}
    assert [i["title"] for i in track] == ["Drill set A", "Drill set B",
                                           "Free text item: read the notes"]


# ---- warm-up ----------------------------------------------------------------------------

def test_warmup_after_three_day_gap_only(v):
    no_exam(v)
    log = EventLog(v)
    log.append(session="cli", kind="grade", source="tutor", now=at(12),
               fields={"object": "concept:arrays-basics", "verdict": "correct"})

    def warm(day):
        states, _ = replay(v, today=f"2026-11-{day}")
        return [(i["id"], i["why"]) for i in today(v, Catalog.load(v), states, now=at(day))
                if i["kind"] == "warmup"]

    assert warm(15) == [("arrays-basics", "Warm-up after a break")]
    assert warm(13) == []


def test_warmup_gap_ignores_ledger_logs_but_not_evidence(v):
    no_exam(v)
    log = EventLog(v)
    log.append(session="cli", kind="grade", source="tutor", now=at(12),
               fields={"object": "concept:arrays-basics", "verdict": "correct"})
    for kind in ("touch", "need"):  # always-on ledger lines after the 3-day break
        log.append(session="cli", kind="log", source="tutor", now=at(15),
                   fields={"object": "concept:arrays-basics", "payload": {"kind": kind}})

    def warm():
        states, _ = replay(v, today="2026-11-15")
        return [i["id"] for i in today(v, Catalog.load(v), states, now=at(15))
                if i["kind"] == "warmup"]

    assert warm() == ["arrays-basics"]
    log.append(session="cli", kind="log", source="self", now=at(15),
               fields={"object": "concept:arrays-basics",
                       "payload": {"kind": "self_report", "verdict": "correct"}})
    assert warm() == []


# ---- exam clock and boosts ----------------------------------------------------------------

def test_exam_in_five_days_pulls_in_and_boosts(v):
    catalog = cat(v, ("Heaps", []))
    states = {"heaps": due("heaps", 0.5), "other": due("other", 0.4)}
    items = plan(v, states, catalog)
    refresh = [(i["id"], i["why"]) for i in items if i["kind"] == "refresh"]
    # heaps 0.5 x 1.6 beats other 0.6; never-graded arrays-basics goes to `new`, not refresh
    assert refresh == [("heaps", "Acme test in 5 days"), ("other", "Needs refresh")]
    assert "arrays-basics" in [i["id"] for i in items if i["kind"] == "new"]
    no_exam(v)
    assert [i["id"] for i in plan(v, states, catalog) if i["kind"] == "refresh"] == [
        "other", "heaps"]


def test_private_exam_is_hidden_from_claude_but_still_planned(v):
    from cortex.render import start_card
    path = v.root / "assessments/acme-oa-2026.md"
    path.write_text(path.read_text(encoding="utf-8").replace('outcome: "pending"\n',
                                                             'outcome: "pending"\nprivate: true\n'),
                    encoding="utf-8")
    catalog = cat(v, ("Heaps", []))
    states = {"heaps": due("heaps", 0.5), "other": due("other", 0.4)}
    items = plan(v, states, catalog)
    # scheduling is unchanged (owner Q2 a); only the company leaves the reason
    assert [(i["id"], i["why"]) for i in items if i["kind"] == "refresh"] == [
        ("heaps", "a private test in 5 days"), ("other", "Needs refresh")]
    rows = planner.assessments(v)
    card = start_card(mode="learn", sprint_day=None, today=items, assessments=rows,
                      open_mistakes=[], date="2026-11-15")
    md = today_md(today=items, sprint_day=None, assessments=rows, as_of="2026-11-15")
    for text in (card, md):
        assert "Acme" not in text and "acme" not in text and "2026-11-20" not in text


def test_domain_exam_does_not_flood_refresh(v):
    catalog = cat(v, *[(f"C{n:02}", []) for n in range(10)])
    states = {"c00": ConceptState("c00", "learning", retrievability=0.81),
              "c01": due("c01", 0.84)}
    items = plan(v, states, catalog)
    assert [i["id"] for i in items if i["kind"] == "refresh"] == ["c00", "c01"]
    assert all(i["state"] == "not_started" for i in items if i["kind"] == "new")


def test_exam_tested_ready_concepts_lead_new(v):
    (v.root / "tracks/sprint-test.md").unlink()  # track concepts would rank first
    catalog = cat(v, ("Hash maps", []), ("Sorting", []))
    path = v.root / "assessments/acme-oa-2026.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('tests: {"domain:dsa": 0.6, "concept:two-pointers": 0.4}',
                                 'tests: {"concept:sorting": 1}'), encoding="utf-8")
    items = plan(v, {}, catalog)
    assert [i["id"] for i in items if i["kind"] == "new"] == ["sorting", "arrays-basics"]
    assert not [i for i in items if i["kind"] == "refresh"]


def test_undated_exam_has_no_clock(v):
    path = v.root / "assessments/acme-oa-2026.md"
    path.write_text(path.read_text(encoding="utf-8").replace('date: "2026-11-20"\n', ""),
                    encoding="utf-8")
    items = plan(v, {})
    assert not [i for i in items if i["kind"] == "refresh"]
    assert not any("test in" in i["why"] for i in items)


def test_felt_weak_boost_lasts_14_days(v):
    catalog = cat(v, ("Heaps", []))
    log_assessment(EventLog(v), session="cli", source="external", assessment="acme-oa-2026",
                   outcome="pass", felt_weak=["heaps"], now=at(1))  # pass: no exam clock
    evs = list(EventLog(v).iter_events())
    states = {"heaps": due("heaps", 0.5), "other": due("other", 0.4)}

    def first(now):
        return [i["id"] for i in plan(v, states, catalog, events=evs, now=now)
                if i["kind"] == "refresh"][0]

    assert first(at(1) + timedelta(days=13)) == "heaps"
    assert first(at(1) + timedelta(days=14)) == "other"


def test_failed_assessment_boosts_tested_concepts(v):
    catalog = cat(v, ("Heaps", []))
    log_assessment(EventLog(v), session="cli", source="external", assessment="acme-oa-2026",
                   outcome="fail", now=at(10))
    states = {"heaps": due("heaps", 0.5), "other": due("other", 0.4)}
    items = plan(v, states, catalog, events=list(EventLog(v).iter_events()))
    assert [i["id"] for i in items if i["kind"] == "refresh"][0] == "heaps"


# ---- confident miss -----------------------------------------------------------------------

def _grades(v, *rows):
    log = EventLog(v)
    for day, verdict, conf in rows:
        log.append(session="cli", kind="grade", source="tutor", now=at(day),
                   fields={"object": "concept:arrays-basics", "verdict": verdict, "hints": 0,
                           "confidence": conf})


def test_confident_miss_listed_after_asked_even_if_not_due(v):
    no_exam(v)
    _grades(v, (1, "correct", 3), (4, "correct", 3), (8, "correct", 3), (14, "wrong", 4))
    states, _ = replay(v, today="2026-11-15")
    st = states["arrays-basics"]
    assert st.confident_miss == "2026-11-14" and st.next_due > "2026-11-15"
    states["two-pointers"] = ConceptState("two-pointers", "learning", asked_not_graded=1)
    items = plan(v, states, events=list(EventLog(v).iter_events()))
    assert [(i["kind"], i["id"], i["why"]) for i in items[:2]] == [
        ("asked", "two-pointers", "1 asked, not graded"),
        ("confident_miss", "arrays-basics", "confident miss on 2026-11-14")]
    assert [i["id"] for i in items].count("arrays-basics") == 1


def test_confident_miss_cleared_by_a_later_day_correct_or_after_7_days(v):
    _grades(v, (8, "wrong", 4))
    assert replay(v, today="2026-11-14")[0]["arrays-basics"].confident_miss == "2026-11-08"
    assert replay(v, today="2026-11-15")[0]["arrays-basics"].confident_miss is None
    _grades(v, (14, "wrong", 4), (14, "partial", 3))
    assert replay(v, today="2026-11-15")[0]["arrays-basics"].confident_miss == "2026-11-14"
    _grades(v, (14, "correct", 4))  # a same-day retest does not clear it (§15a.20)
    states, _ = replay(v, today="2026-11-15")
    assert states["arrays-basics"].confident_miss == "2026-11-14"
    assert ("confident_miss", "arrays-basics") in [
        (i["kind"], i["id"]) for i in plan(v, states, events=list(EventLog(v).iter_events()))]
    _grades(v, (15, "correct", 2))
    assert replay(v, today="2026-11-15")[0]["arrays-basics"].confident_miss is None


# ---- modes ------------------------------------------------------------------------------

def test_review_and_short_modes(v):
    catalog, states, evs = _mixed(v)
    review = plan(v, states, catalog, events=evs, mode="review")
    assert kinds(review) == ["unchecked", "asked", "confident_miss", "refresh", "refresh"]
    short = plan(v, states, catalog, events=evs, mode="short")
    assert len(short) == 5 and kinds(short)[:2] == ["confident_miss", "unchecked"]
    for mode in ("log", "about", "week"):
        assert plan(v, states, catalog, events=evs, mode=mode) == []


# ---- taught, not yet checked: newest first, 7-day expiry, short mode ----------------------

def taught(c, day, **kw):
    return ConceptState(c, "unverified", unchecked=True, last_taught_day=f"2026-11-{day:02}", **kw)


def test_unchecked_newest_first_and_expires_after_7_days(v):
    no_exam(v)
    states = {"a-old": taught("a-old", 7), "b-mid": taught("b-mid", 8),
              "c-new": taught("c-new", 14), "d-today": taught("d-today", 15)}
    lane = [(i["id"], i["why"]) for i in plan(v, states) if i["kind"] == "unchecked"]
    assert lane == [("d-today", "Taught today, not yet checked"),
                    ("c-new", "Taught yesterday, not yet checked"),
                    ("b-mid", "Taught 7 days ago, not yet checked")]  # a-old: 8 days, gone


def test_expired_unchecked_concept_can_still_come_back_as_refresh(v):
    no_exam(v)
    st = taught("arrays-basics", 1, retrievability=0.3)
    st.state = "needs_refresh"
    items = plan(v, {"arrays-basics": st})
    assert [(i["kind"], i["id"]) for i in items if i["id"] == "arrays-basics"] == [
        ("refresh", "arrays-basics")]


def test_short_mode_old_alphabetical_unchecked_do_not_crowd_out_a_confident_miss(v):
    no_exam(v)
    states = {f"a{n}": taught(f"a{n}", 9) for n in range(8)}
    states["zz-miss"] = ConceptState("zz-miss", "learning", confident_miss="2026-11-14")
    short = plan(v, states, mode="short")
    assert len(short) == 5 and short[0]["id"] == "zz-miss"


def test_short_mode_pulls_concepts_of_an_exam_within_2_days(v):
    (v.root / "tracks/sprint-test.md").unlink()
    path = v.root / "assessments/acme-oa-2026.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('tests: {"domain:dsa": 0.6, "concept:two-pointers": 0.4}',
                                 'tests: {"concept:two-pointers": 1}'), encoding="utf-8")
    states = {f"a{n}": taught(f"a{n}", 14) for n in range(8)}
    states["two-pointers"] = due("two-pointers", 0.9)
    states["zz-miss"] = ConceptState("zz-miss", "learning", confident_miss="2026-11-14")
    ids = lambda now: [i["id"] for i in plan(v, states, mode="short", now=now)]  # noqa: E731
    assert ids(at(18))[:2] == ["zz-miss", "two-pointers"]  # exam on the 20th: 2 days away
    assert "two-pointers" not in ids(at(17))  # 3 days away: normal order, unchecked first
    full = plan(v, states, now=at(18))
    assert kinds(full)[:8] == ["unchecked"] * 8  # full plan keeps its lane order


# ---- ready to learn ---------------------------------------------------------------------

def test_ready_to_learn_needs_solid_prereqs_and_prefers_topic(v):
    no_exam(v)
    catalog = cat(v, ("Hash maps", ["arrays-basics"]), ("Sorting", []))

    def new(states, topic=None):
        return [i["id"] for i in plan(v, states, catalog, topic=topic) if i["kind"] == "new"]

    assert new({}) == ["arrays-basics", "sorting"]
    assert new({}, topic="Sorting") == ["sorting", "arrays-basics"]
    solid = {"arrays-basics": ConceptState("arrays-basics", "solid")}
    assert new(solid) == ["hash-maps", "sorting"]


# ---- readiness and week ---------------------------------------------------------------------

def test_readiness(v):
    catalog = Catalog.load(v)
    states = {"two-pointers": ConceptState("two-pointers", "solid")}
    # weights: arrays-basics 0.6, secret-diary 0.6, two-pointers 0.6 + 0.4
    assert readiness(v, catalog, states, "acme-oa-2026") == pytest.approx(1.0 / 2.2)
    assert readiness(v, catalog, {}, planner.assessments(v)[0]) == 0.0
    assert readiness(v, catalog, {}, {"id": "x", "tests": {}}) is None
    neg = {"id": "x", "tests": {"two-pointers": 1, "arrays-basics": -1, "hash-maps": 0}}
    assert readiness(v, catalog, states, neg) == 1.0


def _exam(when: str, **tests):
    return {"id": "x", "date": when, "tests": tests or {"two-pointers": 1}}


def test_recall_on_day_is_the_fsrs_curve_on_the_exam_day(v):
    catalog = Catalog.load(v)
    noon15 = datetime.combine(date(2026, 11, 15), time(12)).astimezone(UTC)  # local noon
    st = {"two-pointers": ConceptState("two-pointers", "learning", stability=5.0,
                                       last_review=noon15)}
    # FSRS defines stability as the days until R falls to 0.9
    assert planner.recall_on_day(v, catalog, st, _exam("2026-11-20"), NOW) == pytest.approx(0.9)
    # a never-reviewed concept counts as 0 (not predicted_p's 0.5), weighted like readiness
    both = _exam("2026-11-20", **{"two-pointers": 3, "arrays-basics": 1})
    assert planner.recall_on_day(v, catalog, st, both, NOW) == pytest.approx(3 * 0.9 / 4)
    assert planner.recall_on_day(v, catalog, {}, both, NOW) == 0.0
    assert planner.recall_on_day(v, catalog, st, {"id": "x", "tests": {}}, NOW) is None
    assert planner.recall_on_day(v, catalog, st, {"id": "x", "date": None,
                                                  "tests": {"two-pointers": 1}}, NOW) is None


def test_recall_on_day_falls_with_distance_and_readiness_stays(v):
    log = EventLog(v)
    for d in (1, 3, 7, 14):  # spaced passes: a well-reviewed concept, last reviewed yesterday
        log.append(session="cli", kind="grade", source="tutor", now=at(d),
                   fields={"object": "concept:two-pointers", "verdict": "correct"})
    catalog = Catalog.load(v)
    states, _ = replay(v, today="2026-11-15")
    st = states["two-pointers"]
    assert st.last_review is not None and st.stability > 10
    near = planner.recall_on_day(v, catalog, states, _exam("2026-11-17"), NOW)
    far = planner.recall_on_day(v, catalog, states, _exam("2027-03-01"), NOW)
    assert near > 0.9 > far > 0
    assert readiness(v, catalog, states, _exam("2026-11-17")) == 0.0  # not Solid: both kept
    w = week_summary(v, catalog, now=NOW)
    assert w["upcoming"][0]["readiness"] is not None
    assert 0 < w["upcoming"][0]["recall_on_day"] < 1


def test_fresh_unchecked_is_one_rule(v):
    states = {"a-old": taught("a-old", 7), "b-mid": taught("b-mid", 8),
              "c-new": taught("c-new", 14), "d-tie": taught("d-tie", 14),
              "e-none": ConceptState("e-none", unchecked=True),
              "f-checked": ConceptState("f-checked", last_taught_day="2026-11-15")}
    assert planner.fresh_unchecked(states, "2026-11-15") == ["c-new", "d-tie", "b-mid",
                                                             "e-none"]
    assert planner.fresh_unchecked(states, date(2026, 11, 21)) == ["c-new", "d-tie", "e-none"]
    assert planner.fresh_unchecked(states, date(2026, 11, 22)) == ["e-none"]


def test_week_summary_counts(v):
    log = EventLog(v)
    s1, s2 = f"s_{ULID()}", f"s_{ULID()}"
    log.append(session="cli", kind="grade", source="tutor", now=at(1),
               fields={"object": "concept:arrays-basics", "verdict": "correct"})
    log.append(session=s1, kind="grade", source="tutor", now=at(10),
               fields={"object": "concept:two-pointers", "verdict": "correct"})
    log.append(session=s2, kind="grade", source="tutor", now=at(12),
               fields={"object": "concept:two-pointers", "verdict": "wrong",
                       "mistakes": ["dsa.off-by-one"]})
    w = week_summary(v, Catalog.load(v), now=NOW)
    assert (w["from"], w["to"]) == ("2026-11-09", "2026-11-15")
    assert (w["sessions"], w["grades"], w["correct"]) == (2, 2, 1)
    assert [(c["id"], c["before"], c["after"]) for c in w["changed"]] == [
        ("two-pointers", "not_started", "learning")]
    assert w["mistakes_opened"] == ["dsa.off-by-one"] and w["mistakes_resolved"] == []
    assert [(a["id"], a["days"], a["readiness"]) for a in w["upcoming"]] == [
        ("acme-oa-2026", 5, 0.0)]


def test_last_night_failure_outranks_older_due(tmp_path):
    # Local days and local noon on purpose (no UTC patch): R must count the hours since 21:00.
    root = tmp_path / "vault"
    shutil.copytree(FIXTURES / "vault_mini", root)
    v = Vault(root)
    no_exam(v)
    Catalog.load(v).add("concept", "Prefix sums", parent="dsa.arrays", requires=[])
    day = date(2026, 11, 15)

    def local(d, hour):
        return datetime.combine(d, time(hour)).astimezone(UTC)

    log = EventLog(v)
    for c, when, verdict in [("arrays-basics", local(day - timedelta(days=30), 10), "correct"),
                             ("arrays-basics", local(day - timedelta(days=28), 10), "correct"),
                             ("prefix-sums", local(day - timedelta(days=1), 21), "wrong")]:
        log.append(session="cli", kind="grade", source="tutor", now=when,
                   fields={"object": f"concept:{c}", "verdict": verdict, "hints": 0})
    evs = list(log.iter_events())
    states, _ = planner.current_states(v, day.isoformat())
    r_new, r_old = states["prefix-sums"].retrievability, states["arrays-basics"].retrievability
    assert r_new < r_old < 1  # py-fsrs whole days would give the 15-hour-old review R = 1
    assert levels.predicted_p(evs, "prefix-sums", local(day, 12)) == pytest.approx(r_new, abs=1e-3)
    refresh = [i["id"] for i in plan(v, states, events=evs, now=local(day, 12))
               if i["kind"] == "refresh"]
    assert refresh == ["prefix-sums", "arrays-basics"]


# ---- privacy and the shared "due" (CORTEX-DESIGN §15.2 N5, N8) --------------------------

def _lanes(c):
    return ConceptState(c, "learning", unchecked=(c == "secret-diary"),
                        asked_not_graded=1, confident_miss="2026-11-14")


def test_private_concepts_stay_out_of_unchecked_asked_and_confident_miss(v):
    no_exam(v)
    ids = ("two-pointers", "secret-diary")
    items = plan(v, {c: _lanes(c) for c in ids})
    assert "secret-diary" not in [i["id"] for i in items]
    assert ("asked", "two-pointers") in [(i["kind"], i["id"]) for i in items]
    area = v.root / "areas/dsa.arrays.md"  # a private area hides every concept under it
    area.write_text(area.read_text(encoding="utf-8").replace(
        'kind: "area"\n', 'kind: "area"\nprivate: true\n'), encoding="utf-8")
    left = {i["id"] for i in plan(v, {c: _lanes(c) for c in ids})}
    assert not left & {"two-pointers", "secret-diary", "arrays-basics"}  # track lane too
    assert planner.hidden(v, Catalog.load(v)) >= {"dsa.arrays", "two-pointers", "arrays-basics"}


def test_private_concept_never_on_the_start_card(v):
    from cortex.render import start_card

    no_exam(v)
    states = {c: taught(c, 14) for c in ("two-pointers", "secret-diary")}
    card = start_card(mode="learn", sprint_day=None, today=plan(v, states), assessments=[],
                      open_mistakes=[], date="2026-11-15")
    assert "**Taught, not yet checked:** Two pointers" in card
    assert "secret-diary" not in card and "Private concept" not in card


def test_find_due_and_planner_share_one_definition(v):
    from cortex.index import Index

    no_exam(v)
    states = {"arrays-basics": ConceptState("arrays-basics", "learning", next_due="2026-11-10"),
              "two-pointers": ConceptState("two-pointers", "learning", next_due="2026-12-01")}
    idx = Index(v, Catalog.load(v), states)
    idx.day = "2026-11-15"
    found = [h["id"] for h in idx.find("", "due")]
    planned = [i["id"] for i in plan(v, states) if i["kind"] == "refresh"]
    assert found == planned == ["arrays-basics"]  # Learning, past next_due: due in both
    assert planner.is_due(states["arrays-basics"], "2026-11-15")
    assert not planner.is_due(states["two-pointers"], "2026-11-15")


def test_gap_check_is_the_refresh_reason(v):
    no_exam(v)
    states = {"heaps": ConceptState("heaps", "learning", next_due="2026-11-14",
                                    solid_after="2026-11-09"),
              "arrays-basics": due("arrays-basics", 0.5, next_due="2026-11-14")}
    refresh = {i["id"]: i["why"] for i in plan(v, states) if i["kind"] == "refresh"}
    assert refresh == {"heaps": "One more check after a break",
                       "arrays-basics": "due 2026-11-14"}


def test_gap_check_is_scheduled_on_solid_after(v):
    no_exam(v)
    log = EventLog(v)
    for day in (2, 3):  # the old Solid rule a day apart: FSRS alone waits weeks; first pass + 7
        log.append(session="cli", kind="grade", source="tutor", now=at(day),
                   fields={"object": "concept:heaps", "verdict": "correct", "confidence": 4,
                           "item_type": "application"})
    states = replay(v, "2026-11-08")[0]
    assert states["heaps"].solid_after == states["heaps"].next_due == "2026-11-09"
    assert "heaps" not in [i["id"] for i in plan(v, states, now=at(8))]
    states = replay(v, "2026-11-09")[0]
    refresh = {i["id"]: i["why"] for i in plan(v, states, now=at(9)) if i["kind"] == "refresh"}
    assert refresh["heaps"] == "One more check after a break"
    assert planner.is_due(states["heaps"], "2026-11-09")


# ---- ideas-r2 #5 model problem, #13 past-question cue ----------------------------------

def test_refresh_names_oldest_solved_problem(v):
    no_exam(v)
    states = {"two-pointers": due("two-pointers", 0.2), "arrays-basics": due("arrays-basics", 0.3)}
    # review mode: the fixture track would otherwise take two-pointers
    assert not any("model" in i for i in plan(v, states, mode="review"))  # nothing solved
    v.set_field("problem", "lc-1", "status", "solved")
    v.set_field("problem", "lc-1", "solved_on", "2026-10-20")
    v.set_field("problem", "lc-167", "status", "solved")
    v.set_field("problem", "lc-167", "solved_on", "2026-10-05")
    items = plan(v, states, mode="review")
    refresh = {i["id"]: i.get("model") for i in items if i["kind"] == "refresh"}
    assert refresh == {"two-pointers": "lc-167", "arrays-basics": "lc-1"}
    md = today_md(today=items, sprint_day=None, assessments=[], as_of="2026-11-15").splitlines()
    assert "  - Re-derive [[lc-167]]: approach and complexity in 2 minutes, no code" in md


def test_refresh_model_skips_a_private_problem(v):
    no_exam(v)
    states = {"two-pointers": due("two-pointers", 0.2), "arrays-basics": due("arrays-basics", 0.3)}
    for id, day in (("lc-1", "2026-10-20"), ("lc-167", "2026-10-05")):
        v.set_field("problem", id, "status", "solved")
        v.set_field("problem", id, "solved_on", day)
    p = v.root / "problems/lc-167.md"
    p.write_text(p.read_text(encoding="utf-8").replace("---\n#", "private: true\n---\n#"),
                 encoding="utf-8")
    models = {i.get("model") for i in plan(v, states, mode="review") if i["kind"] == "refresh"}
    assert "lc-167" not in models and "lc-1" in models


def test_past_question_cue_is_escaped(v):
    no_exam(v)
    hostile = ("![x](http://evil.example/a.png) [click](javascript:alert(1)) "
               "<script>alert(1)</script> [[secret-diary]] **Why** is `two pointers` O(n)? "
               "https://evil.example/x " + "pad " * 60)
    log = EventLog(v)
    log.append(session="cli", kind="grade", source="tutor", now=at(10),
               fields={"object": "concept:two-pointers", "verdict": "correct",
                       "question": "old question"})
    log.append(session="cli", kind="grade", source="tutor", now=at(11),
               fields={"object": "concept:two-pointers", "verdict": "wrong",
                       "question": hostile})
    cues = planner.last_questions(v)
    assert cues["two-pointers"] == hostile
    items = plan(v, {"two-pointers": due("two-pointers", 0.2)}, mode="review")
    md = today_md(today=items, sprint_day=None, assessments=[], as_of="2026-11-15", cues=cues)
    line = next(x for x in md.splitlines() if "Past question" in x)
    assert line.startswith("  - Past question (answer in your own words first): ")
    for bad in ("](", "<", ">", "http", "evil", "javascript", "[[", "**", " `"):
        assert bad not in line
    assert "secret-diary" in line and "\\*\\*Why\\*\\*" in line and "\\`two pointers\\`" in line
    assert line.endswith("…")
    assert len(line) < 260
    assert "Past question" not in today_md(today=items, sprint_day=None, assessments=[],
                                           as_of="2026-11-15")