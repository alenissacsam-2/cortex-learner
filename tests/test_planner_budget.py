"""Council items 7 (plans that fit a time budget) and 8 (a mock paper from weak spots)."""

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import run
from typer.testing import CliRunner

from keepfold import planner
from keepfold.catalog import Catalog
from keepfold.cli import app
from keepfold.levels import ConceptState
from keepfold.vault import Vault, VaultError

NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)  # the fixture's Acme OA is on 2026-11-20
LATER = datetime(2026, 11, 25, 12, tzinfo=UTC)  # no pending test left


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    shutil.copytree(Path(__file__).parent / "fixtures" / "vault_mini", root)
    return Vault(root)


def it(kind, id=None, **extra):
    return {"kind": kind, "id": id, "title": id, "state": None, "why": "", **extra}


def recalled(c, days_ago, **kw):
    """A state whose FSRS recall at NOW/LATER falls with `days_ago` (stability 1 day)."""
    return ConceptState(c, "learning", stability=1.0,
                        last_review=LATER - timedelta(days=days_ago), **kw)


# ---- time budget -----------------------------------------------------------------

def test_fit_keeps_order_and_drops_the_tail():
    items = [it("refresh", "a"), it("unchecked", "b"), it("track", "c"), it("warmup", "d")]
    kept = planner.fit(items, 10)
    assert [i["id"] for i in kept] == ["a", "b"]  # stops at the track item; warmup not pulled up
    assert planner.estimate(kept) == 7
    assert planner.budget_note(kept, 10) == "about 7 of 10 min (estimate)"
    assert planner.fit(items, 240) == items


def test_fit_keeps_an_oversized_first_item_alone_and_pointers_are_free():
    assert [i["id"] for i in planner.fit([it("new", "a"), it("warmup", "b")], 5)] == ["a"]
    more = it("track", None, more="t")
    assert planner.estimate([more]) == 0
    assert planner.fit([it("warmup", "a"), more, it("warmup", "b")], 2)[-1]["id"] == "b"


@pytest.mark.parametrize("bad", [0, -5, 241, True, 2.5, "30"])
def test_check_minutes_rejects(bad):
    with pytest.raises(VaultError) as e:
        planner.check_minutes(bad)
    assert e.value.code == "invalid_argument"


def test_check_minutes_presets():
    assert planner.check_minutes(None) is None and planner.check_minutes(240) == 240
    assert planner.check_minutes(30, planner.PAPER_PRESETS) == 30
    with pytest.raises(VaultError):
        planner.check_minutes(20, planner.PAPER_PRESETS)
    assert planner.PAPER_MINUTES in planner.PAPER_PRESETS == (15, 30, 45)


# ---- mock paper ------------------------------------------------------------------

def test_paper_follows_the_next_test_and_never_shows_private(v):
    cat = Catalog.load(v)
    # Acme weighs two-pointers 1.0 (domain 0.6 + concept 0.4), arrays-basics 0.6, and the
    # domain covers secret-diary too, which is private
    paper = planner.mock_paper(v, cat, {}, now=NOW, minutes=45)
    assert [i["id"] for i in paper] == ["two-pointers", "arrays-basics"]
    first, second = paper
    assert first["anchor"] == "lc-1" and first["minutes"] == 15  # unsolved, by id
    assert second["anchor"] is None and second["minutes"] == 5  # lc-1 is used once
    assert "Acme test in 4 days (weight 1)" in first["why"] and "not yet checked" in first["why"]
    assert {i["kind"] for i in paper} == {"paper"}
    assert [i["id"] for i in planner.mock_paper(v, cat, {}, now=NOW, minutes=15)] \
        == ["two-pointers"]


def test_paper_without_a_test_falls_back_to_weakest_with_mistakes_first(v):
    cat = Catalog.load(v)
    states = {"two-pointers": recalled("two-pointers", 0),  # fresh: recall ~1
              "arrays-basics": recalled("arrays-basics", 30),  # long ago: recall low
              "secret-diary": recalled("secret-diary", 60)}
    ids = [i["id"] for i in planner.mock_paper(v, cat, states, now=LATER)]
    assert ids == ["arrays-basics", "two-pointers"]
    states["two-pointers"] = recalled("two-pointers", 0, open_mistakes=["dsa.off-by-one"])
    paper = planner.mock_paper(v, cat, states, now=LATER)
    assert [i["id"] for i in paper] == ["two-pointers", "arrays-basics"]
    assert "open mistake" in paper[0]["why"] and "recall" in paper[0]["why"]
    assert planner.mock_paper(v, cat, {}, now=LATER) == []  # no test, no evidence: nothing


def test_paper_skips_a_private_test(v):
    p = v.root / "assessments" / "acme-oa-2026.md"
    p.write_text(p.read_text(encoding="utf-8").replace('outcome: "pending"\n',
                                                      'outcome: "pending"\nprivate: true\n'),
                 encoding="utf-8")
    paper = planner.mock_paper(v, Catalog.load(v), {"arrays-basics": recalled("arrays-basics", 9)},
                               now=NOW)
    assert [i["id"] for i in paper] == ["arrays-basics"] and "Acme" not in paper[0]["why"]


def test_paper_interleaves_domains(v):
    from test_sweep import concepts

    a, b = concepts(v, "algebra", 3), concepts(v, "logic", 3)
    states = {c: ConceptState(c, "learning") for c in a + b}
    cat = Catalog.load(v)
    paper = planner.mock_paper(v, cat, states, now=LATER, minutes=30)
    doms = [cat.entries[i["id"]].domain for i in paper]
    assert len(doms) == 6 and all(x != y for x, y in zip(doms, doms[1:], strict=False)), doms


# ---- over MCP and the CLI --------------------------------------------------------

def test_start_minutes_over_mcp(root):
    async def body(call):
        full = await call("keepfold_start", {"mode": "learn"})
        cut = await call("keepfold_start", {"mode": "learn", "minutes": 1})
        assert "**Time:**" not in full["display"]
        assert cut["today"] == full["today"][:len(cut["today"])] and len(cut["today"]) <= 1
        assert "(estimate)" in cut["display"]
        for args in ({"mode": "learn", "minutes": 0}, {"mode": "home", "minutes": 30},
                     {"mode": "mock", "minutes": 30},  # an interview, not a paper
                     {"mode": "mock", "topic": "paper", "minutes": 20}):
            bad = await call("keepfold_start", args)
            assert bad["error"] == "invalid_argument", args
        await call("keepfold_grade", {"concept": "two pointers", "question": "q",
                                    "learner_answer": "a", "verdict": "partial",
                                    "item_type": "recall"})
        out = await call("keepfold_start", {"mode": "mock", "topic": "paper", "minutes": 15})
        ids = [i["id"] for i in out["paper"]]
        # real clock: the Acme test (to 20 Nov) or, after it, the graded concept
        assert ids and set(ids) <= {"two-pointers", "arrays-basics"}
        assert "secret-diary" not in str(out["paper"])
        assert planner.estimate(out["paper"]) <= 15 and out["today"] == []
        assert "Practice paper" in out["display"] and "of 15 min (estimate)" in out["display"]
        assert "Paper section" in out["next"] and "Paper (topic 'paper')" in out["brief"]
        dflt = await call("keepfold_start", {"mode": "mock", "topic": "Paper"})
        assert f"of {planner.PAPER_MINUTES} min" in dflt["display"]
        plain = await call("keepfold_start", {"mode": "mock"})
        assert "paper" not in plain and "Practice paper" not in plain["display"]

    run(root, body)


@pytest.mark.usefixtures("home")
def test_cli_today_minutes(v):
    runner = CliRunner()
    r = runner.invoke(app, ["today", "--minutes", "1", "--vault", str(v.root)])
    assert r.exit_code == 0, r.output
    assert "Time: about" in r.output and "(estimate)" in r.output
    assert "Drill set A" in (v.root / "Today.md").read_text(encoding="utf-8")  # whole plan
    bad = runner.invoke(app, ["today", "--minutes", "0", "--vault", str(v.root)])
    assert bad.exit_code == 1 and "invalid_argument" in bad.output
