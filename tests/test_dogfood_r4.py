"""Fixes from the synthetic-day test (docs/design/v6/dogfood-r4.md): each test drives the
tools the way a literal model did and checks the result it should have got."""

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import run

from keepfold.events import EventLog
from keepfold.mcp_server import (
    DESCRIPTIONS,
    MOCK_NEXT,
    MORE_NEXT,
    REFLECT_HARVEST_NEXT,
    REFLECT_NEXT,
    SWEEP_NEXT,
)
from keepfold.render import start_card
from keepfold.vault import Vault

SKILL = Path(__file__).resolve().parent.parent / "skills" / "keepfold"


def test_skill_harvest_row_names_the_history_mode():
    """#2: the SKILL table is what a model reads first; `harvest` is no keepfold_start mode."""
    row = next(ln for ln in (SKILL / "SKILL.md").read_text("utf-8").splitlines()
               if ln.startswith("| `harvest` |"))
    assert 'keepfold_start(mode="history")' in row and "modes/harvest.md" in row


@pytest.mark.parametrize("op, want", [("reject", "the proposed line's text"),
                                      ("append_to_section", "the section heading"),
                                      ("set_field", "the field name"),
                                      ("check_item", "the section heading")])
def test_patch_without_target_gets_the_helpful_message(root, op, want):
    """#3: a missing target reached pydantic first, so the model never saw which argument."""
    async def body(call):
        out = await call("keepfold_patch", {"id": "me/preferences" if op == "reject"
                                          else "two-pointers", "op": op, "text": "x"})
        assert out["error"] == "invalid_argument" and f"needs target: {want}" in out["message"]

    run(root, body)


def test_mode_docs_name_the_reject_target():
    for page in ("harvest", "reflect", "graph"):
        assert "target=<the proposed line>" in (SKILL / "modes" / f"{page}.md").read_text(
            "utf-8"), page


def test_mock_grades_stay_in_role_and_the_start_gives_the_stamp(root):
    """#4, #5: no teaching or confidence nudge mid-interview; the server owns the stamp."""
    async def body(call):
        start = await call("keepfold_start", {"mode": "mock"})
        assert "no teaching or talk of grades" in start["next"]
        assert re.fullmatch(r"\d{12}", start["stamp"])
        at = datetime.strptime(start["stamp"], "%Y%m%d%H%M")
        assert abs(datetime.now() - at) < timedelta(minutes=5)  # local time
        assert "`stamp`" in start["brief"]
        key = f"mock-{start['stamp']}"
        plan = await call("keepfold_grade", {"concept": "two pointers", "key": f"{key}-plan",
                                           "items": [{"question": "q", "answer": "a",
                                                      "verdict": "wrong"}]})
        code = await call("keepfold_grade", {"concept": "two pointers", "question": "code",
                                           "learner_answer": "a", "verdict": "partial",
                                           "item_type": "application", "key": f"{key}-code"})
        for out in (plan, code):
            assert out["next"].startswith(MOCK_NEXT), out["next"]
            assert "Teach" not in out["next"] and "how sure" not in out["next"]

    run(root, body)


def test_sweep_header_counts_the_plan_and_misses_are_explained_not_retaught(root):
    """#7: the card promised 10 questions over a 1-item plan; `next` contradicted sweep.md."""
    EventLog(Vault(root)).append(  # an imported-only result, so the sweep has an item
        session="import", kind="grade", source="imported",
        now=datetime(2026, 10, 1, 12).astimezone(),
        fields={"object": "concept:arrays-basics", "question": "imported", "verdict": "correct"})

    async def body(call):
        start = await call("keepfold_start", {"mode": "sweep"})
        n = len(start["today"])
        assert n and f"Sweep ({n} cold question" in start["display"].splitlines()[0]
        out = await call("keepfold_grade", {"concept": "arrays-basics", "items": [
            {"question": "q", "answer": "a", "verdict": "wrong", "confidence": 2}]})
        assert out["next"].startswith(SWEEP_NEXT) and "re-test" not in out["next"]

    run(root, body)
    one = [{"kind": "sweep", "id": "c", "title": "C", "state": "learning"}]
    assert start_card(mode="sweep", today=one, assessments=[],
                      open_mistakes=[], date="2026-11-16").startswith(
        "### Keepfold · Sweep (1 cold question) ·")


def test_a_retest_after_teaching_is_graded():
    """#8: 'grade only cold answers' read as 'never grade the re-test'."""
    assert "re-test after teaching is graded too" in (SKILL / "modes" / "grading.md").read_text(
        "utf-8")
    assert "re-test after teaching is graded too" in DESCRIPTIONS["keepfold_grade"]


def test_reflect_next_is_the_five_minute_default_and_a_harvest_check(root):
    """#12: the pack's next pushed the portrait and every part, inside harvest too."""
    async def body(call):
        out = await call("keepfold_reflect")
        assert out["next"].startswith(REFLECT_NEXT)
        assert "draft this month's section" not in out["next"]
        await call("keepfold_start", {"mode": "history"})
        assert (await call("keepfold_reflect"))["next"] == REFLECT_HARVEST_NEXT

    run(root, body)


def test_smaller_next_strings(root):
    """#13: read's closed-book nudge, an empty touch, ambient 'call keepfold_end', home branches."""
    async def body(call):
        concept = await call("keepfold_read", {"id": "two-pointers"})
        problem = await call("keepfold_read", {"id": "lc-167"})
        assert "closed-book" in concept["next"] and "closed-book" not in problem["next"]
        prime = await call("keepfold_prime", {"prompt": "hello there"})
        assert prime["touch"] == [] and "Nothing to touch." in prime["next"]
        add = await call("keepfold_add", {"kind": "domain", "title": "Physics"})
        assert add["next"].startswith("Continue.") and MORE_NEXT not in add["next"]
        home = await call("keepfold_start", {"mode": "home"})
        assert "need" not in home and home["unlearned"] == 0
        assert "sealed tap" not in home["next"] and "unlearned=true" not in home["next"]
        assert len(home["next"]) < 800

    run(root, body)
    assert "/keepfold harvest" in start_card(mode="history", today=[],
                                           assessments=[], open_mistakes=[], date="2026-11-16")
