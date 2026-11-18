"""v5 Phase 2 G4: letters for the bad day."""

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import events, run

from cortex import letters
from cortex import session as session_mod
from cortex.vault import Vault, VaultError

pytestmark = pytest.mark.usefixtures("home")

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_when_closed_vocabulary():
    assert letters.parse_when("late+energy_low:2") == [("late", None), ("energy_low", 2)]
    assert letters.parse_when("exam_within:14+gap_days:2+miss_streak:10")[0] == ("exam_within", 14)
    for bad in ("", "sad", "late:1", "energy_low", "energy_low:0", "energy_low:8",
                "exam_within:15", "gap_days:1", "miss_streak:11", "late+", "gap_days:x", 3):
        with pytest.raises(VaultError):
            letters.parse_when(bad)


F = {"hour": 12, "energy": [], "exam_days": None, "gap": None, "verdicts": []}


@pytest.mark.parametrize("name,n,facts,want", [
    ("late", None, {"hour": 23}, True), ("late", None, {"hour": 3}, True),
    ("late", None, {"hour": 4}, False), ("late", None, {"hour": 22}, False),
    ("energy_low", 2, {"energy": [5, 2, 1]}, True), ("energy_low", 2, {"energy": [1, 3]}, False),
    ("energy_low", 2, {"energy": [1]}, False),
    ("exam_within", 3, {"exam_days": 3}, True), ("exam_within", 3, {"exam_days": 4}, False),
    ("gap_days", 5, {"gap": 5}, True), ("gap_days", 5, {"gap": 4}, False),
    ("miss_streak", 2, {"verdicts": ["correct", "wrong", "wrong"]}, True),
    ("miss_streak", 2, {"verdicts": ["wrong", "partial"]}, False),
])
def test_triggers(name, n, facts, want):
    assert letters.holds(name, n, {**F, **facts}) is want


def test_facts_read_journal_exams_gap_and_grades(tmp_path):
    r = tmp_path / "v"
    shutil.copytree(FIXTURES / "vault_mini", r)
    v = Vault(r)
    v.append_journal("2026-11-12", "21:00", "long day · energy 1")
    v.append_journal("2026-11-13", "21:00", "ok · mood 2 · energy 2")
    evs = [{"id": "a", "ts": "2026-11-01T10:00:00Z", "day": "2026-11-01", "session": "s_old",
            "kind": "grade", "object": "concept:two-pointers", "verdict": "wrong"},
           {"id": "b", "ts": "2026-11-14T10:00:00Z", "day": "2026-11-14", "session": "s_now",
            "kind": "open"}]
    f = letters.facts(v, evs, datetime(2026, 11, 14, 23, 30).astimezone(), "2026-11-14", "s_now")
    assert f == {"hour": 23, "energy": [1, 2], "exam_days": 6, "gap": 13,
                 "verdicts": ["wrong"]}
    # B7: today's sweep closing an old session, or a topic touch, is no study: the gap stays
    evs += [{"id": "c", "ts": "2026-11-14T09:00:00Z", "day": "2026-11-14", "session": "s_old",
             "kind": "close"},
            {"id": "d", "ts": "2026-11-14T09:01:00Z", "day": "2026-11-14", "session": "s_x",
             "kind": "log", "payload": {"kind": "touch", "ids": ["two-pointers"]}}]
    f = letters.facts(v, evs, datetime(2026, 11, 14, 23, 30).astimezone(), "2026-11-14", "s_now")
    assert f["gap"] == 13


def _page(*rows):
    return "## Letters\n" + "".join(
        f'- "{t}" | when: {w} | written {d} ^{i}\n' for t, w, d, i in rows)


def _shown(lid, day, n=0):
    return {"id": f"e{day}{n}", "ts": f"{day}T10:0{n}:00Z", "day": day, "kind": "log",
            "payload": {"kind": "letter_shown", "letter": lid}}


def test_pick_oldest_daily_cap_and_cooldown():
    page = _page(("newer", "late", "2026-11-02", "l-bbbbb"), ("older", "late", "2026-11-01",
                                                               "l-aaaaa"),
                 ("never", "gap_days:9", "2026-10-01", "l-ccccc"))
    late = {**F, "hour": 23}
    assert letters.pick(page, late, [], "2026-11-14")["id"] == "l-aaaaa"
    assert letters.pick(page, {**F, "hour": 12}, [], "2026-11-14") is None
    assert letters.pick(page, late, [_shown("l-zzzzz", "2026-11-14")], "2026-11-14") is None
    recent = [_shown("l-aaaaa", "2026-11-12")]
    assert letters.pick(page, late, recent, "2026-11-14")["id"] == "l-bbbbb"
    assert letters.pick(page, late, recent, "2026-11-15")["id"] == "l-aaaaa"  # 3 days on
    voided = [*recent, {"id": "v", "kind": "void", "ref": "e2026-11-120"}]
    assert letters.pick(page, late, voided, "2026-11-14")["id"] == "l-aaaaa"


def test_effect_counts_shown_feedback_and_followed():
    evs = [_shown("l-aaaaa", "2026-11-10"),
           {"id": "o1", "ts": "2026-11-10T20:00:00Z", "day": "2026-11-10", "kind": "open"},
           _shown("l-aaaaa", "2026-11-13"),
           {"id": "o2", "ts": "2026-11-15T10:00:00Z", "day": "2026-11-15", "kind": "open"},
           {"id": "f1", "ts": "2026-11-13T11:00:00Z", "day": "2026-11-13", "kind": "log",
            "payload": {"kind": "letter_feedback", "letter": "l-aaaaa", "verdict": "helped"}},
           {"id": "f2", "ts": "2026-11-13T12:00:00Z", "day": "2026-11-13", "kind": "log",
            "payload": {"kind": "letter_feedback", "letter": "l-aaaaa", "verdict": "retire"}}]
    assert letters.effect(evs) == [{"id": "l-aaaaa", "shown": 2, "helped": 1, "not": 0,
                                    "followed": 1}]


def test_write_show_feedback_retire_over_mcp(root, monkeypatch):
    now = datetime(2026, 3, 5, 23, 30).astimezone()
    monkeypatch.setattr(session_mod.SessionManager, "_now", lambda self: now.astimezone(UTC))
    text = "You got through worse.\nSleep, then [one](http://x.example) <b>small</b> step."
    got = {}

    async def body(call):
        for bad in ({"text": "hi", "when": "late"}, {"text": "x" * 301, "when": "late"},
                    {"text": "Fine words", "when": "tired"}, {"text": 5, "when": "late"}):
            out = await call("cortex_log", {"kind": "letter", "payload": bad})
            assert out["error"] == "invalid_argument", bad
        out = await call("cortex_log", {"kind": "letter", "payload": {"text": text,
                                                                      "when": "late"}})
        got["id"] = lid = out["id"]
        assert lid.startswith("l-") and len(lid) == 7
        start = await call("cortex_start", {})
        assert start["letter"]["id"] == lid and start["letter"]["written"] == "2026-03-05"
        assert "<untrusted_note" in start["letter"]["text"]
        line = start["display"].splitlines()[-2]
        assert line.startswith('A note from you (written 5 Mar): "You got through worse. Sleep')
        assert "<b>" not in line and "http" not in line
        assert start["display"].splitlines()[-1].startswith("**What would you like to do?**")
        assert "letter_feedback" in start["next"]
        again = await call("cortex_start", {"mode": "learn"})
        assert "letter" not in again and "A note from you" not in again["display"]
        for bad in ({"letter": "l-zzzzz", "verdict": "helped"},
                    {"letter": lid, "verdict": "meh"}):
            out = await call("cortex_log", {"kind": "letter_feedback", "payload": bad})
            assert out["error"] == "invalid_argument", bad
        await call("cortex_log", {"kind": "letter_feedback",
                                  "payload": {"letter": lid, "verdict": "helped"}})
        await call("cortex_log", {"kind": "letter_feedback",
                                  "payload": {"letter": lid, "verdict": "retire"}})
        out = await call("cortex_log", {"kind": "letter_feedback",
                                        "payload": {"letter": lid, "verdict": "retire"}})
        assert out["error"] == "invalid_argument"  # already in ## Past
        out = await call("cortex_reflect")
        assert out["letters"] == [{"id": lid, "shown": 1, "helped": 1, "not": 0,
                                   "followed": 0}]  # frozen clock: no later start

    run(root, body)
    page = (root / "me/letters.md").read_text(encoding="utf-8")
    flat = "You got through worse. Sleep, then [one](http://x.example) <b>small</b> step."
    assert page.startswith('---\nid: "letters"')
    assert f'## Past\n- "{flat}" | when: late | written 2026-03-05 (retired 2026-03-05) ' \
           f'^{got["id"]}\n' in page
    assert page.count(got["id"]) == 1
    rows = letters.parse(page)
    assert [(r["live"], r["text"]) for r in rows] == [(False, flat)]
    shown = [e for e in events(root) if e.get("payload", {}).get("kind") == "letter_shown"]
    assert len(shown) == 1 and shown[0]["source"] == "tutor"


def test_letter_shows_at_the_end_of_a_mode_card(root, monkeypatch):
    now = datetime(2026, 3, 5, 1, 0).astimezone()  # 01:00 is late (and still 2026-03-04)
    monkeypatch.setattr(session_mod.SessionManager, "_now", lambda self: now.astimezone(UTC))

    async def body(call):
        await call("cortex_log", {"kind": "letter", "payload": {"text": "Go to bed.",
                                                                "when": "late"}})
        out = await call("cortex_start", {"mode": "learn"})
        assert out["display"].splitlines()[-1] == \
            'A note from you (written 4 Mar): "Go to bed."'

    run(root, body)
