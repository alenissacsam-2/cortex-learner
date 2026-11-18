import json
import shutil
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortex import events
from cortex.events import EventLog
from cortex.levels import fold, predicted_p, replay
from cortex.vault import Vault

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIOS = sorted(p.stem for p in (FIXTURES / "events").glob("*.jsonl"))
_local_day = events.local_day


@pytest.fixture
def log(tmp_path, monkeypatch):
    # Days follow UTC in tests so the expected dates do not depend on the machine's zone.
    monkeypatch.setattr(events, "local_day", lambda ts, tz=None: _local_day(ts, UTC))
    root = tmp_path / "vault"
    shutil.copytree(FIXTURES / "vault_mini", root)
    return EventLog(Vault(root))


def at(day: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, 12, minute, tzinfo=UTC)


def add(log, day, kind="grade", source="tutor", minute=0, **fields):
    return log.append(session="cli", kind=kind, source=source, now=at(day, minute),
                      fields=fields)


def grades(log, concept, day, verdicts, **extra):
    for n, v in enumerate(verdicts):
        add(log, day, minute=n, object=f"concept:{concept}", verdict=v, **extra)


def states(log, today):
    return fold(log.iter_events(), today)


# ---- golden scenarios (tests/fixtures/events/<name>.jsonl + <name>.expected.json) ----------

@pytest.mark.parametrize("name", SCENARIOS)
def test_golden_scenario(log, name):
    appended = []
    for line in (FIXTURES / "events" / f"{name}.jsonl").read_text("utf-8").splitlines():
        if not line.strip():
            continue
        spec = json.loads(line)
        fields = {k: appended[int(v[1:])]["id"] if isinstance(v, str) and v.startswith("@")
                  else v for k, v in spec["fields"].items()}
        appended.append(log.append(session="cli", kind=spec["kind"],
                                   source=spec.get("source", "tutor"), fields=fields,
                                   mode=spec.get("mode"),
                                   now=datetime.fromisoformat(spec["at"])))
    expected = json.loads((FIXTURES / "events" / f"{name}.expected.json").read_text("utf-8"))
    for cp in expected["checkpoints"]:
        concepts, mistakes = fold(appended[:cp["after"]], cp["today"],
                                  expected.get("problem_concepts"))
        for kind, got in (("concepts", concepts), ("mistakes", mistakes)):
            for id, want in cp.get(kind, {}).items():
                assert id in got, (cp["after"], id)
                have = asdict(got[id])
                assert {k: have[k] for k in want} == want, (cp["after"], id)
        for id in cp.get("absent", []):
            assert id not in concepts and id not in mistakes, (cp["after"], id)


# ---- ladder ----------------------------------------------------------------------------------

def test_not_started_and_unverified(log):
    add(log, 1, kind="ask", object="concept:arrays-basics", question="What is an array?")
    add(log, 1, kind="taught", minute=1, object="concept:two-pointers")
    concepts, _ = states(log, "2026-11-01")
    assert concepts["arrays-basics"].state == "not_started"
    assert concepts["two-pointers"].state == "unverified"
    assert "lc-1" not in concepts and concepts["two-pointers"].attempts == 0


def test_four_of_five_is_a_pass_day_three_of_five_is_not(log):
    for c in ("arrays-basics", "two-pointers"):
        grades(log, c, 1, ["correct"])
        grades(log, c, 2, ["correct"])
    grades(log, "arrays-basics", 3, ["correct"] * 4 + ["wrong"])
    grades(log, "two-pointers", 3, ["correct"] * 3 + ["wrong"] * 2)
    concepts, _ = states(log, "2026-11-03")
    assert (concepts["arrays-basics"].pass_days, concepts["arrays-basics"].last_pass_day) == (
        3, "2026-11-03")
    assert (concepts["two-pointers"].pass_days, concepts["two-pointers"].last_pass_day) == (
        2, "2026-11-02")


def test_hinted_correct_counts_half(log):
    grades(log, "arrays-basics", 1, ["correct"])
    grades(log, "arrays-basics", 2, ["correct"])
    grades(log, "arrays-basics", 3, ["correct"], hints=2)  # share 0.5 -> not a pass day
    concepts, _ = states(log, "2026-11-03")
    assert concepts["arrays-basics"].pass_days == 2


def test_confidence_four_wrong_is_not_a_pass_day(log):
    grades(log, "arrays-basics", 1, ["correct"])
    grades(log, "arrays-basics", 2, ["correct"])
    grades(log, "arrays-basics", 3, ["correct"] * 4)
    add(log, 3, minute=9, object="concept:arrays-basics", verdict="wrong", confidence=4,
        mistakes=["dsa.off-by-one"])
    concepts, mistakes = states(log, "2026-11-03")
    st = concepts["arrays-basics"]
    assert (st.pass_days, st.state, st.last_pass_day) == (2, "learning", "2026-11-02")
    # a confident miss rates the day Again even after a cold correct
    assert (st.interval_days, st.next_due) == (2, "2026-11-05")
    assert st.confident_miss == "2026-11-03"
    assert st.open_mistakes == ["dsa.off-by-one"] and mistakes["dsa.off-by-one"].count == 1


# ---- engine v2 states (§15a.21) ------------------------------------------------------------

def test_solid_needs_a_depth_pass(log):
    # Good on 11-01 and 11-08 gives stability ≥ 10, two pass days and the 7-day gap.
    for day in (1, 8):
        grades(log, "deep", day, ["correct"], item_type="application")
        grades(log, "shallow", day, ["correct"])
        grades(log, "hinted", day, ["correct"], item_type="transfer", hints=2)
    concepts, _ = states(log, "2026-11-08")
    deep, shallow = concepts["deep"], concepts["shallow"]
    assert deep.stability >= 10 and deep.pass_days == 2 and deep.depth_pass
    assert deep.state == "solid"
    assert shallow.stability == deep.stability and not shallow.depth_pass
    assert shallow.state == "learning"
    assert not concepts["hinted"].depth_pass  # depth needs hints <= 1


def test_solid_needs_stability_and_two_pass_days(log):
    grades(log, "one-day", 1, ["correct"], item_type="transfer", confidence=4)
    grades(log, "soft", 1, ["correct"], item_type="application")
    grades(log, "soft", 2, ["correct"], item_type="application")
    concepts, _ = states(log, "2026-11-02")
    assert concepts["one-day"].pass_days == 1 and concepts["one-day"].state == "learning"
    assert concepts["soft"].stability < 10 and concepts["soft"].state == "learning"


def test_last_graded_day_must_be_a_pass_day(log):
    for c in ("passed", "slipped"):
        for day in (1, 8):
            grades(log, c, day, ["correct"], item_type="application")
    grades(log, "passed", 9, ["correct"] * 3 + ["partial"] * 2)  # share 0.8 -> pass
    grades(log, "slipped", 9, ["correct"] * 2 + ["partial"] * 3)  # share 0.7 -> no pass
    concepts, _ = states(log, "2026-11-09")
    assert concepts["slipped"].stability >= 10  # Hard keeps a mature card's stability
    assert concepts["passed"].state == "solid" and concepts["slipped"].state == "learning"


def test_needs_refresh_only_when_solid_was_met(log):
    for day in (1, 8):
        grades(log, "deep", day, ["correct"], item_type="application")
        grades(log, "shallow", day, ["correct"])
    st = states(log, "2026-11-08")[0]["deep"]
    due = datetime.fromisoformat(st.next_due).date()
    assert st.interval_days == (due - datetime(2026, 11, 8).date()).days > 0
    assert states(log, due.isoformat())[0]["deep"].state == "solid"
    after = states(log, (due + timedelta(days=1)).isoformat())[0]
    assert after["deep"].state == "needs_refresh"
    assert after["shallow"].state == "learning"  # past due but never met Solid


# ---- FSRS scheduling (§15a.16, §15a.21) ----------------------------------------------------

def test_item_rating():
    from fsrs import Rating

    from cortex.levels import _Item, _rating

    def r(verdict, hints=0, confidence=None, item_type=None):
        return _rating(_Item({}, "c", verdict, hints, confidence, item_type=item_type))

    assert r("wrong", confidence=4) == Rating.Again
    assert r("partial") == r("correct", hints=2) == Rating.Hard
    assert r("correct", confidence=4) == Rating.Easy  # U1
    assert r("correct", confidence=3, item_type="transfer") == Rating.Easy
    assert r("correct", confidence=3) == r("correct", confidence=3,
                                           item_type="application") == Rating.Good
    assert r("correct", hints=1, confidence=4, item_type="transfer") == Rating.Good
    assert r("correct", confidence=2, item_type="transfer") == Rating.Good


def test_remediation_hard_on_new_card_not_on_mature_lapse(log):
    grades(log, "fixed-new", 1, ["wrong", "correct"])
    grades(log, "hard-new", 1, ["partial"])
    grades(log, "fixed-hinted", 1, ["wrong", "correct"], hints=2)
    grades(log, "again-new", 1, ["wrong"])
    for c in ("lapse-fixed", "lapse"):
        grades(log, c, 1, ["correct"])
        grades(log, c, 5, ["correct"])  # stability ~16
    grades(log, "lapse-fixed", 6, ["wrong", "correct"])
    grades(log, "lapse", 6, ["wrong"])
    concepts, _ = states(log, "2026-11-06")
    assert concepts["fixed-new"].stability == concepts["hard-new"].stability
    assert concepts["fixed-hinted"].stability == concepts["again-new"].stability
    assert concepts["lapse-fixed"].stability == concepts["lapse"].stability < 10
    assert concepts["lapse-fixed"].next_due == concepts["lapse"].next_due


def test_also_credit_is_a_hard_review_without_pass_day(log):
    add(log, 1, object="concept:two-pointers", verdict="correct",
        also=["concept:arrays-basics"])
    add(log, 1, minute=1, object="concept:two-pointers", verdict="wrong",
        also=["concept:wrong-only"])
    grades(log, "hard", 1, ["partial"])
    concepts, _ = states(log, "2026-11-01")
    st = concepts["arrays-basics"]
    assert (st.state, st.pass_days, st.attempts, st.depth_pass) == ("learning", 0, 0, False)
    assert st.stability == concepts["hard"].stability
    assert "wrong-only" not in concepts  # a wrong never touches also-concepts
    p_also = predicted_p(log.iter_events(), "arrays-basics", at(4))
    assert p_also == predicted_p(log.iter_events(), "hard", at(4)) != 0.5


def test_problem_secondary_concepts_get_also_credit(log):
    add(log, 1, object="problem:lc-1", verdict="correct")
    evs = list(log.iter_events())
    primary_only = fold(evs, "2026-11-01", {"lc-1": "two-pointers"})[0]
    assert "arrays-basics" not in primary_only
    both = fold(evs, "2026-11-01", {"lc-1": ["two-pointers", "arrays-basics"]})[0]
    assert both["arrays-basics"].state == "learning" and both["arrays-basics"].attempts == 0
    assert both["two-pointers"].attempts == 1


def test_jitter_is_deterministic_and_within_five_percent():
    from cortex.levels import JITTER, _jitter
    assert all(_jitter("c", n, i) == 0 for n in range(20) for i in range(3))
    shifts = [(i, _jitter(f"c{n}", n, i)) for n in range(50) for i in range(3, 200, 7)]
    assert shifts == [(i, _jitter(f"c{n}", n, i)) for n in range(50) for i in range(3, 200, 7)]
    assert all(abs(j) <= i * JITTER + 0.5 for i, j in shifts)
    assert any(j > 0 for _, j in shifts) and any(j < 0 for _, j in shifts)


def test_jitter_shifts_due_but_not_retrievability(log):
    # The ladder fixture's arrays-basics: FSRS due 11-05 + 38 d, jittered to 37 d.
    for day, hints in ((1, 0), (1, 1), (2, 0), (5, 1)):
        add(log, day, minute=hints, object="concept:arrays-basics", verdict="correct",
            hints=hints)
    evs = list(log.iter_events())
    a, b = fold(evs, "2026-11-06")[0], fold(evs, "2026-11-06")[0]
    assert a == b and (a["arrays-basics"].interval_days, a["arrays-basics"].next_due) == (
        37, "2026-12-12")
    r = a["arrays-basics"].retrievability
    assert r == round(predicted_p(evs, "arrays-basics", datetime(2026, 11, 6, 12).astimezone(
        UTC)), 3)


def test_rating_orders_due_dates(log):
    last = {"again": {"verdict": "wrong"}, "hard": {"verdict": "partial"},
            "good": {"verdict": "correct", "hints": 1},
            "easy": {"verdict": "correct", "hints": 0, "confidence": 4}}
    for c, fields in last.items():
        grades(log, c, 1, ["correct"])
        grades(log, c, 2, ["correct"])
        add(log, 3, object=f"concept:{c}", **fields)
    concepts, _ = states(log, "2026-11-03")
    dues = [concepts[c].next_due for c in last]
    assert dues == sorted(dues) and len(set(dues)) == 4


def test_day_is_rated_by_its_first_cold_item(log):
    for c in ("quiz", "slip", "cold-miss", "taught", "right", "hard", "wrong"):
        grades(log, c, 1, ["correct"])
        grades(log, c, 8, ["correct"])  # stability ≥ 10: no remediation
    grades(log, "quiz", 20, ["correct", "correct", "wrong", "correct", "correct", "correct"])
    grades(log, "slip", 20, ["correct", "wrong"])  # cold correct, later slip, not a pass day
    grades(log, "cold-miss", 20, ["wrong", "correct"])
    log.append(session="cli", kind="taught", source="tutor", now=at(20) - timedelta(hours=1),
               fields={"object": "concept:taught"})  # taught first: no cold item, worst rating
    grades(log, "taught", 20, ["correct", "wrong", "correct", "correct", "correct"])
    for c, v in (("right", "correct"), ("hard", "partial"), ("wrong", "wrong")):
        grades(log, c, 20, [v])
    concepts, _ = states(log, "2026-11-20")
    s = {c: st.stability for c, st in concepts.items()}
    assert s["quiz"] == s["right"] and concepts["quiz"].last_pass_day == "2026-11-20"
    assert s["slip"] == s["hard"] and concepts["slip"].last_pass_day == "2026-11-08"
    assert s["cold-miss"] == s["wrong"] < 10  # a mature lapse stays Again
    assert concepts["cold-miss"].last_pass_day == "2026-11-08"  # cold miss + fix: no pass
    assert s["taught"] == s["wrong"] and concepts["taught"].last_pass_day == "2026-11-20"


def test_a_miss_after_a_long_gap_never_lengthens_the_interval(log):
    # Imported 4/5 on 10-01 (a pass day, rated by its first item), then 44 days later a
    # wrong and a fixed answer: a lapse across a gap is Again, confident (4) or not (2).
    for conf, c in ((4, "confident"), (2, "unsure")):
        for n, v in enumerate(["correct"] * 4 + ["wrong"]):
            log.append(session="import", kind="grade", source="imported",
                       now=datetime(2026, 10, 1, 12, tzinfo=UTC),
                       fields={"object": f"concept:{c}", "verdict": v, "question": f"q{n}"})
        for minute, v in ((0, "wrong"), (2, "correct")):
            ask = add(log, 14, kind="ask", minute=minute, object=f"concept:{c}", question="q",
                      item_type="application")
            add(log, 14, minute=minute + 1, ref=ask["id"], verdict=v, hints=0, confidence=conf)
    concepts, _ = states(log, "2026-11-14")
    for c in ("confident", "unsure"):
        assert concepts[c].next_due <= "2026-11-16", c  # old engine: 11-15; bug: 2027-01-01
    # SPEC §15a.20: only a correct on a later day clears a confident miss
    assert concepts["confident"].confident_miss == "2026-11-14"
    assert concepts["unsure"].confident_miss is None


def test_confident_miss_clears_only_on_a_later_day(log):
    add(log, 3, object="concept:c", verdict="wrong", confidence=4)
    add(log, 3, minute=5, object="concept:c", verdict="correct", confidence=4)  # retest
    assert states(log, "2026-11-03")[0]["c"].confident_miss == "2026-11-03"
    add(log, 4, object="concept:c", verdict="correct", hints=2)  # too many hints
    assert states(log, "2026-11-04")[0]["c"].confident_miss == "2026-11-03"
    add(log, 5, object="concept:c", verdict="correct", hints=1)
    assert states(log, "2026-11-05")[0]["c"].confident_miss is None


def test_solid_waits_for_a_gap_pass(log):
    from cortex.levels import gap_note
    for day in (1, 2):
        grades(log, "c", day, ["correct"], item_type="application", confidence=4)
    st = states(log, "2026-11-02")[0]["c"]
    assert st.stability >= 10 and st.pass_days == 2 and st.depth_pass  # the old Solid rule
    # solid_after = max(first pass 11-01 + 7, last graded 11-02 + 3)
    assert (st.state, st.gap_pass, st.solid_after) == ("learning", False, "2026-11-08")
    assert gap_note(st) == "one more check after a break (from 2026-11-08)"
    # FSRS alone would wait weeks; the check is due on solid_after
    assert (st.next_due, st.interval_days) == ("2026-11-08", 6)
    grades(log, "c", 8, ["correct"])  # 7 days after the first pass, 6 days off
    st = states(log, "2026-11-08")[0]["c"]
    assert (st.state, st.gap_pass, st.solid_after, gap_note(st)) == ("solid", True, None, "")


def test_gap_pass_needs_seven_days_from_the_first_pass_and_three_days_off(log):
    for day in (1, 4, 8):  # FSRS-like 1 -> 3 -> 7 spacing: day 8 earns the gap pass
        grades(log, "spaced", day, ["correct"], item_type="application", confidence=4)
    for day in range(1, 9):  # daily passes: never 3 days off
        grades(log, "daily", day, ["correct"], item_type="application", confidence=4)
    concepts, _ = states(log, "2026-11-08")
    assert concepts["spaced"].gap_pass and concepts["spaced"].state == "solid"
    daily = concepts["daily"]
    assert not daily.gap_pass and daily.state == "learning"
    assert daily.solid_after == daily.next_due == "2026-11-11"  # last graded 11-08 + 3


def _raw(day, c, rows):
    """Grade events built by hand so ids (the order within one ts) are under test control."""
    return [{"id": f"{c}-{n}", "ts": f"2026-11-{day:02}T12:00:00Z", "day": f"2026-11-{day:02}",
             "kind": "grade", "object": f"concept:{c}", **row} for n, row in enumerate(rows)]


def test_same_timestamp_batch_is_rated_by_pass_share():
    ok, bad = {"verdict": "correct"}, {"verdict": "wrong"}
    sure_ok, sure_bad = {**ok, "confidence": 4}, {**bad, "confidence": 4}
    batches = {  # concept -> (rows, single-item reference with the expected rating)
        "good-wrong-first": ([bad, ok, ok, ok, ok], ok),  # 4/5 -> Good
        "good-wrong-last": ([ok, ok, ok, ok, bad], ok),
        "hard-wrong-first": ([bad, bad, ok, ok, ok], {"verdict": "partial"}),  # 3/5 -> Hard
        "hard-wrong-last": ([ok, ok, ok, bad, bad], {"verdict": "partial"}),
        "again-sure-first": ([sure_bad, ok, ok, ok, ok], bad),  # a confident wrong -> Again
        "again-sure-last": ([ok, ok, ok, ok, sure_bad], bad),
        "again-low": ([ok, bad, bad], bad),  # 1/3 -> Again; the batch's own correct is no fix
        "easy": ([sure_ok] * 3, sure_ok),
    }
    evs = []
    for c, (rows, ref) in batches.items():
        evs += _raw(1, c, rows) + _raw(1, f"{c}-ref", [ref])
    # a Good batch stands even when later items (after feedback) spoil the pass day
    evs += _raw(1, "later", [ok, ok, ok, ok, bad]) + _raw(1, "later-ref", [ok])
    evs += [{**e, "id": f"later-x{n}", "ts": "2026-11-01T12:30:00Z"}
            for n, e in enumerate(_raw(1, "later", [bad, bad]))]
    s = {c: st.stability for c, st in fold(evs, "2026-11-01")[0].items()}
    for c in [*batches, "later"]:
        assert s[c] == s[f"{c}-ref"], c
    assert len({s["good-wrong-first"], s["hard-wrong-first"], s["again-low"], s["easy"]}) == 4


def test_resolving_a_solved_problem_is_recall(log):
    from cortex.levels import _items
    add(log, 1, object="concept:two-pointers", problem="problem:p1", verdict="wrong")
    add(log, 1, minute=1, object="concept:two-pointers", problem="problem:p1",
        verdict="correct")  # same day: still the first solve
    add(log, 2, kind="log", object="problem:p1",
        payload={"kind": "problem_status", "problem": "p1", "status": "solved"})
    add(log, 3, object="concept:two-pointers", problem="problem:p1", verdict="correct",
        item_type="application")  # an explicit item_type wins
    add(log, 3, minute=1, object="concept:two-pointers", problem="problem:p2",
        verdict="correct")
    got = [it.item_type for it in _items(log.iter_events(), {})]
    assert got == ["application", "application", "recall", "application", "application"]


def test_high_retention_brings_due_earlier(log):
    for day in (1, 2, 3):
        grades(log, "arrays-basics", day, ["correct"])
    evs = list(log.iter_events())
    default = fold(evs, "2026-11-03")[0]["arrays-basics"]
    high = fold(evs, "2026-11-03", high_retention={"arrays-basics"})[0]["arrays-basics"]
    assert high.next_due < default.next_due and high.interval_days < default.interval_days
    concepts, _ = replay(log.vault, "2026-11-03", high_retention={"arrays-basics"})
    assert concepts["arrays-basics"].next_due == high.next_due


def test_retrievability(log):
    add(log, 1, kind="taught", object="concept:arrays-basics")
    assert states(log, "2026-11-01")[0]["arrays-basics"].retrievability is None
    grades(log, "arrays-basics", 2, ["correct"])
    r = [states(log, f"2026-11-{d:02d}")[0]["arrays-basics"].retrievability for d in (5, 15, 30)]
    assert 1 >= r[0] > r[1] > r[2] > 0 and r[0] == round(r[0], 3)


# ---- unchecked, asks -----------------------------------------------------------------------

def test_unchecked_after_taught(log):
    grades(log, "arrays-basics", 1, ["correct"])
    add(log, 2, kind="log", source="self", object="concept:arrays-basics",
        payload={"kind": "taught", "concept": "arrays-basics", "summary": "indexing"})
    assert states(log, "2026-11-02")[0]["arrays-basics"].unchecked is True
    grades(log, "arrays-basics", 3, ["correct"])
    assert states(log, "2026-11-03")[0]["arrays-basics"].unchecked is False
    add(log, 4, kind="taught", object="concept:arrays-basics")
    assert states(log, "2026-11-04")[0]["arrays-basics"].unchecked is True


def test_asked_not_graded(log):
    a1 = add(log, 1, kind="ask", object="concept:two-pointers", question="q1",
             targets_mistakes=["dsa.off-by-one"])
    add(log, 1, kind="ask", minute=1, object="concept:two-pointers", question="q2")
    add(log, 1, minute=2, ref=a1["id"], verdict="correct")  # graded by item id only
    st = states(log, "2026-11-01")[0]["two-pointers"]
    assert st.asked_not_graded == 1 and st.attempts == 1 and st.pass_days == 1


# ---- determinism, replay, FSRS shadow ------------------------------------------------------

def test_replay_is_deterministic_and_reads_problem_primaries(log):
    add(log, 1, object="problem:lc-1", verdict="correct")
    add(log, 2, kind="log", source="self", object="problem:lc-1",
        payload={"kind": "problem_status", "problem": "lc-1", "status": "solved"})
    evs = list(log.iter_events())
    assert fold(evs, "2026-11-02") == fold(list(reversed(evs)), "2026-11-02")
    assert fold(evs, "2026-11-09", None, {"two-pointers"}) == fold(evs, "2026-11-09", None,
                                                                    {"two-pointers"})
    concepts, mistakes = replay(log.vault, "2026-11-02")
    assert (concepts, mistakes) == replay(log.vault, "2026-11-02")
    assert concepts["two-pointers"].pass_days == 2
    # lc-1's second concept gets `also` credit: graded, but no attempt and no pass day
    assert (concepts["arrays-basics"].state, concepts["arrays-basics"].attempts) == (
        "learning", 0)


def test_predicted_p(log):
    assert predicted_p(log.iter_events(), "arrays-basics", at(1)) == 0.5
    grades(log, "arrays-basics", 1, ["correct"])
    assert predicted_p(log.iter_events(), "arrays-basics", at(1, 30)) == 0.5  # today's
    soon = predicted_p(log.iter_events(), "arrays-basics", at(2))
    later = predicted_p(log.iter_events(), "arrays-basics", at(1) + timedelta(days=60))
    assert 0.9 < soon <= 1 and 0 < later < soon


def test_predicted_p_uses_worst_rating_of_the_day(log):
    grades(log, "arrays-basics", 1, ["correct"])
    grades(log, "two-pointers", 1, ["correct", "wrong"])
    when = at(5)
    good = predicted_p(log.iter_events(), "arrays-basics", when)
    again = predicted_p(log.iter_events(), "two-pointers", when)
    assert again < good


def test_items_item_type_inherited_and_derived(log):
    from cortex.levels import _items
    ask = add(log, 1, kind="ask", object="concept:two-pointers", question="q",
              item_type="transfer")
    add(log, 1, minute=1, object="concept:two-pointers", ref=ask["id"], verdict="correct")
    add(log, 1, minute=2, object="concept:two-pointers", problem="problem:p1",
        verdict="correct")
    add(log, 1, minute=3, object="concept:two-pointers", verdict="correct")
    add(log, 1, minute=4, kind="log", object="problem:p1",
        payload={"kind": "problem_status", "problem": "p1", "status": "solved"})
    got = [(it.ev["kind"], it.item_type) for it in _items(log.iter_events(), {})]
    assert got == [("ask", "transfer"), ("grade", "transfer"), ("grade", "application"),
                   ("grade", None), ("log", "application")]


# ---- one review per local day at a fixed time (board L1, L6), solved after a grade (L2) ----

def test_an_earlier_hour_the_next_day_is_still_a_day_later():
    def stability(hours):
        evs = [{"id": f"e{d}", "ts": f"2026-10-0{d}T{h:02}:00:00Z", "day": f"2026-10-0{d}",
                "kind": "grade", "object": "concept:c", "verdict": "correct"}
               for d, h in zip((1, 2, 3), hours, strict=True)]
        return fold(evs, "2026-10-03")[0]["c"].stability
    assert stability((10, 9, 8)) == stability((10, 11, 11))


def test_predicted_p_ignores_todays_items(log):
    add(log, 1, object="concept:arrays-basics", verdict="wrong")
    assert predicted_p(log.iter_events(), "arrays-basics", at(1, 1)) == 0.5
    grades(log, "arrays-basics", 1, ["correct"])
    assert predicted_p(log.iter_events(), "arrays-basics", at(1, 30)) == 0.5


def test_solved_after_a_graded_code_phase_is_plain_evidence(log):
    add(log, 1, object="problem:p1", verdict="partial", item_type="application")
    add(log, 1, minute=1, kind="log", object="problem:p1",
        payload={"kind": "problem_status", "problem": "p1", "status": "solved"})
    st = fold(log.iter_events(), "2026-11-01", {"p1": "two-pointers"})[0]["two-pointers"]
    assert (st.depth_pass, st.attempts) == (False, 1)
    # with no grade that day, solved is still a correct item (SPEC §15a.5)
    add(log, 2, kind="log", object="problem:p1",
        payload={"kind": "problem_status", "problem": "p1", "status": "solved"})
    st = fold(log.iter_events(), "2026-11-02", {"p1": "two-pointers"})[0]["two-pointers"]
    assert st.attempts == 2
