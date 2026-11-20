"""v5 PHASE2.md G2: needs, the cap and dedupe, the fold, and answering one from home."""

import pytest
from conftest import events, run
from typer.testing import CliRunner

from cortex import needs
from cortex.cli import app

pytestmark = pytest.mark.usefixtures("home")

EDITOR = {"question": "Which code editor do you prefer?", "area": "preferences",
          "options": ["VS Code", "Vim"], "task": "writing a setup guide"}


def test_needs_cap_dedupe_answer_and_skip(root):
    seen = {}

    async def body(call):  # an ambient chat: no cortex_start
        a = seen["a"] = await call("cortex_log", {"kind": "need", "payload": EDITOR})
        assert "error" not in a and "dup_of" not in a
        for bad in ({**EDITOR, "area": "health"}, {**EDITOR, "question": "q" * 201},
                    {**EDITOR, "options": ["one"]}, {**EDITOR, "options": ["a", "b" * 61]},
                    {**EDITOR, "task": "t" * 121}, {"area": "taste"}):
            assert (await call("cortex_log", {"kind": "need", "payload": bad}))["error"]
        dup = await call("cortex_log", {"kind": "need", "payload": {
            "question": "Which editor do you prefer for code?", "area": "preferences"}})
        assert dup["dup_of"] == a["id"]
        b = await call("cortex_log", {"kind": "need", "payload": {
            "question": "What time do you usually study?", "area": "habits"}})
        capped = await call("cortex_log", {"kind": "need", "payload": {
            "question": "Tea or coffee?", "area": "taste"}})
        assert capped["refused"] is True and capped["recorded"] == 0 and "error" not in capped
        rows = needs.open(events(root), events(root)[-1]["day"])
        assert [(r["id"], r["count"]) for r in rows] == [(a["id"], 2), (b["id"], 1)]
        assert (await call("cortex_log", {"kind": "need_skip", "payload": {
            "need": "nope", "forever": True}}))["error"] == "invalid_argument"
        await call("cortex_log", {"kind": "need_skip", "payload": {"need": b["id"],
                                                                     "forever": False}})

        start = await call("cortex_start")
        assert "1 question for you" in start["display"]
        assert start["need"]["id"] == a["id"]
        assert start["need"]["question"].startswith('<untrusted_note id="need">')
        assert len(start["need"]["options"]) == 2 and "VS Code" in start["need"]["options"][0]
        assert "user_confirmed=true" in start["next"] and "answered: true" in start["next"]
        done = await call("cortex_log", {"kind": "need_skip", "payload": {
            "need": dup["id"], "forever": True, "answered": True}})  # a dup resolves to its root
        assert done["display_line"] == "Need answered"

        start = await call("cortex_start")
        assert "need" not in start and "question for you" not in start["display"]
        pack = await call("cortex_reflect")
        assert pack["needs"] == {"open": 0, "answered": 1, "skipped": 1}

    run(root, body)
    skips = [e["payload"] for e in events(root)
             if e.get("payload", {}).get("kind") == "need_skip"]
    assert skips[-1] == {"kind": "need_skip", "need": seen["a"]["id"], "forever": True,
                         "answered": True}


def _need(id, day, question="What is your goal?", **extra):
    return {"id": id, "kind": "log", "day": day,
            "payload": {"kind": "need", "question": question, "area": "values", **extra}}


def _log(id, day, **payload):
    return {"id": id, "kind": "log", "day": day, "payload": payload}


def test_fold_scores_snoozes_and_voids():
    evs = [_need("n1", "2026-01-01", "Old question one?"),
           _need("n1b", "2026-01-01", "Old question one?", dup_of="n1"),
           _need("n2", "2026-01-29", "Fresh question two?"),
           _need("n3", "2026-01-20", "Snoozed three?"),
           _log("k3", "2026-01-25", kind="need_skip", need="n3", forever=False),
           _need("n4", "2026-01-20", "Dropped four?"),
           _log("k4", "2026-01-20", kind="need_skip", need="n4", forever=True),
           _need("n5", "2026-01-20", "Voided root five?"),
           _need("n5b", "2026-01-21", "Voided root five?", dup_of="n5"),
           {"id": "v", "kind": "void", "ref": "n5"},
           # an old vault's tell-me tap (forecast and settle, kinds since removed) answered n6
           _need("n6", "2026-01-20", "Tapped six?"),
           _log("f6", "2026-01-20", kind="forecast", need="n6", twin="t"),
           _log("s6", "2026-01-20", kind="settle", forecast="f6", twin_hit="yes")]
    rows = needs.open(evs, "2026-01-29")
    # n1: 2 x 0.5^(28/14) = 0.5; n2: 1 today; n5b stands in for its voided root
    assert [(r["id"], r["count"], r["score"]) for r in rows] == [
        ("n2", 1, 1.0), ("n5b", 1, round(0.5 ** (8 / 14), 3)), ("n1", 2, 0.5)]
    assert "n3" in [r["id"] for r in needs.open(evs, "2026-02-02")]  # the 7-day snooze ended
    assert needs.counts(evs, "2026-01-29") == {"open": 3, "answered": 1, "skipped": 2}
    assert needs.match(evs, "  old QUESTION one? ", "2026-01-29") == "n1"
    assert needs.match(evs, "Dropped four?", "2026-01-29") is None  # closed for good


def test_cli_lists_open_needs(root):
    async def body(call):
        await call("cortex_log", {"kind": "need", "payload": EDITOR})

    run(root, body)
    out = CliRunner().invoke(app, ["needs", "--vault", str(root)])
    assert out.exit_code == 0, out.output
    assert "1x" in out.output and "Which code editor do you prefer?" in out.output
