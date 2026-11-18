import json
import math
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import git
from typer.testing import CliRunner

from cortex import learning_eval
from cortex.cli import app
from cortex.events import EventLog
from cortex.vault import Vault

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 11, 1, 12, tzinfo=UTC)


def ev(n, kind="grade", day=None, sec=0, **fields):
    ts = T0 + timedelta(minutes=n, seconds=sec)
    return {"id": f"e{n:08d}" + (f"s{sec}" if sec else ""),"ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "day": day or ts.date().isoformat(), "session": "cli", "kind": kind,
            "source": "tutor", "mode": "learn", **fields}


def grade(n, verdict, p, concept=None, **fields):
    f = {"object": f"concept:{concept or f'c{n}'}", "verdict": verdict, **fields}
    if p is not None:
        f["predicted_p"] = p
    return ev(n, **f)


def test_constant_forecast_brier():
    log = [grade(i, "correct" if i % 2 else "wrong", 0.9) for i in range(100)]
    r = learning_eval.report(log)
    assert r["n"] == 100 and r["base_rate"] == 0.5
    assert r["brier"] == pytest.approx(0.5 * 0.1 ** 2 + 0.5 * 0.9 ** 2) == 0.41
    assert r["brier_base"] == 0.25 and r["skill"] == pytest.approx(1 - 0.41 / 0.25)
    assert r["log_loss"] == pytest.approx(-(math.log(0.9) + math.log(0.1)) / 2, abs=1e-4)
    assert r["gap"] == pytest.approx(0.4) and r["bins"][4]["n"] == 100


def test_calibrated_log_has_near_zero_gap():
    log, n = [], 0
    for p in (0.1, 0.3, 0.5, 0.7, 0.9):
        for i in range(100):
            log.append(grade(n, "correct" if i < round(p * 100) else "wrong", p))
            n += 1
    r = learning_eval.report(log)
    assert r["n"] == 500 and r["gap"] < 0.01
    assert [(b["p"], b["pass"]) for b in r["bins"]] == [
        (0.1, 0.1), (0.3, 0.3), (0.5, 0.5), (0.7, 0.7), (0.9, 0.9)]
    assert r["skill"] > 0


def test_void_skip_and_pass_rule():
    log = [grade(1, "correct", 0.8), grade(2, "wrong", 0.8), grade(3, "correct", None),
           grade(4, "correct", 0.6, hints=2), grade(5, "partial", 0.6),
           ev(6, kind="void", ref="e00000002"),
           ev(7, kind="ask", object="concept:c7", question="q", predicted_p=0.5)]
    r = learning_eval.report(log)
    assert r["n"] == 3 and r["skipped_no_p"] == 1  # voided wrong gone, the ask is no item
    assert r["base_rate"] == pytest.approx(1 / 3, abs=1e-4)  # hints 2 and partial are not passes
    assert r["brier_partial"] == pytest.approx(((0.8 - 1) ** 2 + 2 * (0.6 - 0.5) ** 2) / 3,
                                               abs=1e-4)


def test_splits_sum_to_total_and_grader_kinds():
    log = [ev(1, kind="ask", object="concept:a", question="q", answer_key="O(n)"),
           ev(2, ref="e00000001", object="concept:a", verdict="correct",
              learner_answer="o( n )", model_verdict="wrong", predicted_p=0.5),
           ev(3, kind="ask", object="concept:b", question="q", answer_key="yes"),
           ev(4, ref="e00000003", object="concept:b", verdict="correct", learner_answer="no",
              predicted_p=0.5, confidence=3, item_type="application"),
           grade(5, "wrong", 0.4, mode="ambient"),
           {**grade(6, "correct", 0.7), "source": "external", "mode": None},
           ev(7, kind="log", source="self", object="concept:c", predicted_p=0.5,
              payload={"kind": "self_report", "concept": "c", "verdict": "correct"}),
           grade(8, "correct", None),
           ev(9, kind="ask", object="concept:d", question="q", answer_key="a"),
           ev(10, ref="e00000009", object="concept:d", verdict="wrong", learner_answer="b",
              predicted_p=0.5)]
    r = learning_eval.report(log)
    assert r["n"] == 6 and r["skipped_no_p"] == 1
    for split, rows in r["splits"].items():
        assert sum(h["n"] for h in rows.values()) == r["n"], split
    s = {k: {v: h["n"] for v, h in rows.items()} for k, rows in r["splits"].items()}
    # a key match is correct; on a mismatch the model's verdict stands (yes vs no: correct)
    assert s["grader"] == {"key-agrees": 2, "key-disagrees": 1, "model": 1, "external": 1,
                           "self": 1}
    assert s["mode"] == {"study": 4, "ambient": 1, "none": 1}
    assert s["source"] == {"tutor": 4, "external": 1, "self": 1}
    assert s["item_type"] == {"recall": 5, "application": 1}
    assert s["confidence"] == {"given": 1, "none": 5}
    assert r["model_verdict"] == {"n": 1, "by": {"wrong -> correct": 1}}
    assert "wrong -> correct: 1" in learning_eval.text(r)
    later = learning_eval.report(log + [grade(9, "correct", 0.5, day="2026-11-05")],
                                 since="2026-11-05")
    assert later["n"] == 1 and later["skipped_no_p"] == 0
    assert "by grader" in learning_eval.text(r) and "(too few)" in learning_eval.text(r)


def test_cold_and_prior_splits():
    day2 = 24 * 60
    log = [grade(0, "correct", 0.5, "x"), grade(0, "correct", 0.6, "x", sec=1),  # one batch
           grade(1, "wrong", 0.6, "x"),  # after feedback
           ev(2, kind="taught", object="concept:y"), grade(3, "correct", 0.5, "y"),
           grade(4, "correct", 0.5, "w", also=["concept:z"]),
           grade(day2, "correct", 0.7, "x"), grade(day2 + 1, "correct", 0.6, "z")]
    r = learning_eval.report(log)
    s = {k: {v: h["n"] for v, h in rows.items()} for k, rows in r["splits"].items()}
    assert s["first"] == {"cold": 5, "later same day": 2}
    assert s["prior"] == {"first review": 3, "history": 4}  # z has also-credit history
    for split, rows in r["splits"].items():
        assert sum(h["n"] for h in rows.values()) == r["n"] == 7, split
    assert learning_eval.text(r).splitlines()[4].startswith("cold only")


@pytest.mark.parametrize("extra", [0, 1])
def test_calibrated_gate_matches_the_report(extra):
    # z gets also-credit, then 29 + extra stamped items with history; unstamped ones never count
    log = [grade(0, "correct", 0.5, "w", also=["concept:z"])]
    log += [grade(i, "correct", 0.7, "z") for i in range(1, 30 + extra)]
    log += [grade(i, "wrong", None, "z") for i in range(40, 80)]
    n = learning_eval.report(log)["splits"]["prior"]["history"]["n"]
    assert n == 29 + extra
    assert learning_eval.calibrated(log) is (n >= learning_eval.MIN_PAIRS)
    assert learning_eval.history_pairs(log) == n


def test_history_pairs_stops_at_its_cap():
    log = [grade(0, "correct", 0.5, "z")] + [grade(i, "correct", 0.7, "z") for i in range(1, 41)]
    assert learning_eval.history_pairs(log) == 40
    assert learning_eval.history_pairs(log, cap=30) == 30
    assert learning_eval.history_pairs(log[:5], cap=30) == 4  # under the cap: the full count


def test_cli_is_read_only(tmp_path, monkeypatch):
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "Test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "test@example.invalid")
    root = tmp_path / "vault"
    shutil.copytree(FIXTURES / "vault_mini", root)
    (root / ".gitignore").write_text(".cortex/*\n")
    log = EventLog(Vault(root))
    for i, v in enumerate(("correct", "wrong", "correct")):
        log.append(session="cli", kind="grade", source="tutor", mode="learn", predicted_p=0.6,
                   fields={"object": "concept:two-pointers", "verdict": v},
                   now=T0 + timedelta(days=i))
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    head = git(root, "rev-parse", "HEAD")
    mtimes = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}
    runner = CliRunner()
    out = runner.invoke(app, ["eval", "learning", "--vault", str(root)])
    assert out.exit_code == 0, out.output
    assert "3 graded item(s) paired" in out.output and "by mode" in out.output
    js = runner.invoke(app, ["eval", "learning", "--json", "--since", "2026-11-02",
                             "--vault", str(root)])
    assert js.exit_code == 0 and json.loads(js.output)["n"] == 2
    bad = runner.invoke(app, ["eval", "learning", "--since", "yesterday", "--vault", str(root)])
    assert bad.exit_code == 1
    # mtimes first: `git status` itself refreshes .git/index
    assert mtimes == {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}
    assert git(root, "status", "--porcelain") == "" and git(root, "rev-parse", "HEAD") == head


def test_confidence_mirror_bands_minimum_and_partial_credit():
    m = learning_eval.MIN_BAND
    log = [grade(i, "correct" if i < 9 else "wrong", None, confidence=4) for i in range(12)]
    log += [grade(100 + i, "partial", None, confidence=1) for i in range(m)]  # partial: not right
    log += [grade(200, "correct", None, confidence=3, hints=2),  # 2 hints: not right
            grade(201, "correct", None, confidence=2),
            grade(202, "correct", None, confidence=True),  # not a band
            grade(203, "correct", None, concept="hush", confidence=2),  # hidden concept
            grade(204, "correct", None)]  # no confidence
    rows = {r["confidence"]: r for r in learning_eval.confidence_mirror(log, hide={"hush"})}
    assert rows[4] == {"confidence": 4, "label": "4 (certain)", "n": 12, "right": 9,
                       "share": 0.75}
    assert (rows[1]["n"], rows[1]["right"], rows[1]["share"]) == (m, 0, 0.0)
    assert (rows[3]["n"], rows[3]["right"], rows[3]["share"]) == (1, 0, None)
    assert (rows[2]["n"], rows[2]["share"]) == (1, None)
    lines = learning_eval.mirror_lines(list(rows.values()))
    assert "When you said 4 (certain), you were right 9 of 12 times" in lines
    assert "When you said 2: not enough answers yet (1)" in lines
    assert learning_eval.mirror_lines(learning_eval.confidence_mirror([grade(1, "wrong", 0.5)])) \
        == []


def test_confidence_mirror_follows_voids():
    log = [grade(1, "correct", None, confidence=4), ev(2, kind="void", ref="e00000001")]
    assert all(r["n"] == 0 for r in learning_eval.confidence_mirror(log))
