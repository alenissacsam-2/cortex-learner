"""The `mock` session mode (docs/design/v6/arena/interview-verdict.md)."""

import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

from conftest import events, run

from keepfold import events as events_mod
from keepfold import learning_eval, planner, session
from keepfold.catalog import Catalog
from keepfold.events import EventLog
from keepfold.vault import Vault

FIXTURES = Path(__file__).parent / "fixtures"


def test_mock_is_a_no_plan_non_study_mode():
    assert "mock" in events_mod.MODES and "mock" not in events_mod.STUDY_MODES
    assert "mock" in planner.NO_PLAN_MODES and session.ALLOWED["mock"] == session._ALL


def test_mock_start_writes_are_allowed_and_stamped(root):
    async def body(call):
        out = await call("keepfold_start", {"mode": "mock", "topic": "two pointers"})
        assert out["mode"] == "mock" and out["today"] == []
        assert "Mock interview" in out["display"] and "modes/mock.md" in out["next"]
        # review r4 M5: the rubric and keys reach a model-started mock without the skill
        assert "nothing planned" not in out["display"]
        for part in ("clarifying-questions", "mock-<stamp>-plan", "-check", "felt_weak",
                     "assessment_result"):
            assert part in out["brief"], part
        await call("keepfold_add", {"kind": "domain", "title": "Interview"})
        await call("keepfold_add", {"kind": "area", "title": "Skills", "parent": "interview"})
        await call("keepfold_add", {"kind": "concept", "title": "Clarifying questions",
                                  "parent": "interview.skills"})
        await call("keepfold_grade", {"concept": "clarifying-questions", "question": "q",
                                    "learner_answer": "a", "verdict": "partial",
                                    "item_type": "application"})
        await call("keepfold_patch", {"id": "clarifying-questions", "op": "append_to_section",
                                    "target": "Notes", "text": "- asks range first"})
        await call("keepfold_log", {"kind": "assessment_result", "payload": {
            "assessment": "mock-2026-11-16-coding", "assessment_kind": "mock",
            "outcome": "fail", "felt_weak": ["clarifying-questions"]}})
        await call("keepfold_end", {"summary": "mock"})

    run(root, body)
    evs = [e for e in events(root) if e["kind"] in ("grade", "log")]
    assert [e["kind"] for e in evs] == ["grade", "log"]
    assert {e["mode"] for e in evs} == {"mock"}
    assert evs[1]["payload"]["felt_weak"] == ["clarifying-questions"]


def test_same_keys_write_nothing(root):
    stamp = "mock-202611161200"
    plan = [{"question": "clarify", "answer": "a", "verdict": "partial",
             "concept": "arrays-basics", "item_type": "application"},
            {"question": "approach", "answer": "a", "verdict": "correct", "confidence": 3,
             "item_type": "transfer"},
            {"question": "complexity", "answer": "a", "verdict": "correct",
             "item_type": "recall"}]
    writes = [
        ("keepfold_grade", {"concept": "two pointers", "items": plan, "key": f"{stamp}-plan"}),
        ("keepfold_grade", {"concept": "two pointers", "question": "code", "learner_answer": "a",
                          "verdict": "correct", "item_type": "application", "problem": "lc-167",
                          "confidence": 3, "key": f"{stamp}-code"}),
        ("keepfold_grade", {"concept": "two pointers", "key": f"{stamp}-check", "items": [
            {"question": "test", "answer": "a", "verdict": "correct", "item_type": "application"},
            {"question": "explain", "answer": "a", "verdict": "partial",
             "item_type": "application"}]}),
        ("keepfold_log", {"kind": "problem_status", "key": f"{stamp}-status",
                        "payload": {"problem": "lc-167", "status": "solved", "hints": 0}}),
    ]

    async def body(call):
        await call("keepfold_start", {"mode": "mock"})
        for name, args in writes:
            await call(name, args)
        n = len(events(root))
        for name, args in writes:  # a retried mock: the same keys
            await call(name, args)
        assert len(events(root)) == n

    run(root, body)
    evs = events(root)
    assert sum(e["kind"] == "grade" for e in evs) == 6
    assert sum(e["kind"] == "log" for e in evs) == 1
    assert {e["key"] for e in evs if e["kind"] == "grade"} == {
        f"{stamp}-plan:1", f"{stamp}-plan:2", f"{stamp}-plan:3", f"{stamp}-code",
        f"{stamp}-check:1", f"{stamp}-check:2"}


def test_learning_eval_buckets_mock_under_its_own_name():
    rows = [{"id": f"e{n:08d}", "ts": f"2026-11-01T12:{n:02}:00Z", "day": "2026-11-01",
             "session": "cli", "kind": "grade", "source": "tutor", "mode": mode,
             "object": f"concept:c{n}", "verdict": "correct", "predicted_p": 0.5}
            for n, mode in enumerate(["mock", "mock", "learn", "sweep", "ambient"])]
    assert learning_eval.report(rows)["splits"]["mode"].keys() == {"mock", "study", "ambient"}
    assert learning_eval.report(rows)["splits"]["mode"]["mock"]["n"] == 2


def test_felt_weak_boost_lasts_through_day_13_and_is_gone_on_day_14(tmp_path, monkeypatch):
    monkeypatch.setattr(events_mod, "local_day",
                        lambda ts, tz=None, real=events_mod.local_day: real(ts, UTC))
    root = tmp_path / "vault"
    shutil.copytree(FIXTURES / "vault_mini", root)
    v = Vault(root)
    log = EventLog(v)
    for line in (FIXTURES / "events" / "mock.jsonl").read_text("utf-8").splitlines():
        spec = json.loads(line)
        log.append(session="cli", kind=spec["kind"], source=spec.get("source", "tutor"),
                   fields=spec["fields"], mode=spec.get("mode"),
                   now=datetime.fromisoformat(spec["at"]))
    c = Catalog.load(v)
    c.add("domain", "Interview")
    c.add("area", "Skills", parent="interview")
    for title in ("Clarifying questions", "Thinking aloud"):
        Catalog.load(v).add("concept", title, parent="interview.skills")
    cat, evs = Catalog.load(v), list(log.iter_events())
    weak = {"clarifying-questions": 1.0, "thinking-aloud": 1.0}
    assert planner._weak_boost(v, cat, evs, date(2026, 11, 23)) == weak  # day 13
    assert planner._weak_boost(v, cat, evs, date(2026, 11, 24)) == {}  # day 14
