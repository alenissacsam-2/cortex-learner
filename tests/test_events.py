import json
import shutil
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from cortex import events
from cortex.events import EventLog, local_day
from cortex.vault import Vault, VaultError

FIXTURE = Path(__file__).parent / "fixtures" / "vault_mini"
NOON = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
GOOD = {"object": "concept:edit-distance", "item_type": "recall", "question": "What is dp[i][j]?"}


@pytest.fixture
def ev(tmp_path):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    return EventLog(Vault(root))


def files(ev):
    return sorted((ev.vault.root / "events").rglob("*.jsonl"))


def grade(ev, now=NOON, session="cli", key=None, **fields):
    return ev.append(session=session, kind="grade", source="external", now=now, key=key,
                     fields={"object": "concept:edit-distance", "verdict": "correct", **fields})


def test_append_stamps_server_fields(ev):
    e = ev.append(session="cli", kind="ask", source="tutor", mode="learn", fields=GOOD,
                  predicted_p=0.5, grader={"client": "claude-code", "model": "x"}, now=NOON)
    assert e["ts"] == "2026-11-13T12:00:00Z" and e["schema"] == 1 and e["key"] == e["id"]
    assert e["source"] == "tutor" and e["predicted_p"] == 0.5
    [path] = files(ev)
    assert path.relative_to(ev.vault.root).as_posix() == "events/2026/11/cli.jsonl"
    assert json.loads(path.read_text("utf-8")) == e


@pytest.mark.parametrize("field", ["id", "ts", "day", "source", "predicted_p", "grader",
                                   "schema", "key", "session"])
def test_server_fields_rejected(ev, field):
    with pytest.raises(VaultError) as e:
        ev.append(session="cli", kind="ask", source="tutor", fields={**GOOD, field: "x"})
    assert e.value.code == "invalid_argument"
    assert not files(ev)


@pytest.mark.parametrize("fields", [
    {"bogus": 1},
    {"item_type": "essay"},
    {"verdict": "right"},
    {"model_verdict": "maybe"},
    {"confidence": 0}, {"confidence": 5}, {"confidence": True}, {"confidence": "3"},
    {"hints": -1}, {"hints": 4},
    {"question": "q" * 2001},
    {"learner_answer": "a" * 2001},
    {"answer_key": 7},
    {"summary": "s" * 601},
    {"mistakes": ["Bad Id"]}, {"mistakes": ["ok\n"]},
    {"also": ["dp-2d"]},
    {"payload": {"note": "n" * 2001}},
    {"payload": "text"},
])
def test_field_validation(ev, fields):
    with pytest.raises(VaultError) as e:
        ev.append(session="cli", kind="log", source="self", fields=fields)
    assert e.value.code == "invalid_argument"


def test_limits_inclusive(ev):
    e = ev.append(session="cli", kind="close", source="tutor", now=NOON,
                  fields={"summary": "s" * 600, "question": "q" * 300, "hints": 3,
                          "confidence": 4, "mistakes": ["dp.min-vs-max-choice"],
                          "also": ["concept:dp-2d"], "problem": "problem:lc-72"})
    assert len(e["summary"]) == 600


@pytest.mark.parametrize("args", [
    {"session": "s_bad"}, {"session": "../x"}, {"session": "cli\n"},
    {"kind": "accept"},
    {"source": "claimed_external"}, {"mode": "party"}, {"predicted_p": 1.5},
    {"grader": {"client": "c", "evil": "x"}},
])
def test_call_validation(ev, args):
    base = {"session": "cli", "kind": "ask", "source": "tutor", "fields": GOOD}
    with pytest.raises(VaultError) as e:
        ev.append(**{**base, **args})
    assert e.value.code == "invalid_argument"


@pytest.mark.parametrize("obj,ok", [
    ("concept:edit-distance", True), ("inbox:dijkstra", True), ("domain:dsa", True),
    ("area:graphs", True), ("mistake:dp.off-by-one", True), ("problem:lc-72", True),
    ("assessment:oa-1", True), ("application:acme", True),
    ("edit-distance", False), ("concept:", False), ("concept:Upper", False),
    ("track:x", False), ("concept:a/b", False), ("concept:x\n", False), (3, False),
])
def test_object_regex(ev, obj, ok):
    if ok:
        assert ev.append(session="cli", kind="log", source="self",
                         fields={"object": obj})["object"] == obj
    else:
        with pytest.raises(VaultError):
            ev.append(session="cli", kind="log", source="self", fields={"object": obj})


def test_secret_rejected(ev):
    with pytest.raises(VaultError) as e:
        grade(ev, learner_answer="my key is sk-" + "a" * 30)
    assert e.value.code == "secret_detected"
    assert not files(ev)


def test_duplicate_key_returns_first(ev):
    first = grade(ev, key="k1")
    again = grade(ev, key="k1", now=NOON + timedelta(hours=1), verdict="wrong")
    assert again == first
    [path] = files(ev)
    assert path.read_bytes().count(b"\n") == 1
    assert ev.has_key("k1") and not ev.has_key("k2")
    fresh = EventLog(ev.vault)  # rebuilds the key set from the files
    assert fresh.has_key("k1") and grade(fresh, key="k1") == first
    assert path.read_bytes().count(b"\n") == 1


def test_torn_tail_skipped_and_never_truncated(ev):
    first = grade(ev)
    [path] = files(ev)
    with open(path, "ab") as f:
        f.write(b'{"id":"01TORN","ts":"2026-11-13T12:00:01Z","ki')
    assert list(ev.iter_events()) == [first]
    second = grade(ev, now=NOON + timedelta(minutes=1))
    lines = path.read_bytes().split(b"\n")
    assert lines[-1] == b"" and len(lines) == 4  # the torn line stays, closed by a newline
    assert lines[1].startswith(b'{"id":"01TORN"')
    assert list(ev.iter_events()) == [first, second]


def test_whole_last_line_without_newline_survives_an_append(ev):
    first = grade(ev)
    [path] = files(ev)
    path.write_bytes(path.read_bytes().rstrip(b"\n"))  # a complete event, no trailing newline
    second = grade(ev, now=NOON + timedelta(minutes=1))
    assert list(ev.iter_events()) == [first, second]


def test_invalid_lines_skipped(ev):
    first = grade(ev)
    [path] = files(ev)
    with open(path, "ab") as f:
        f.write(b"not json\n[1,2]\n\xff\xfe\n{\"no\":\"ts\"}\n")
    second = grade(ev, now=NOON + timedelta(minutes=1))
    assert list(ev.iter_events()) == [first, second]


def test_cached_reads_match_a_fresh_parse(ev):
    first = grade(ev)
    again = list(ev.iter_events())  # parsed once, then served from the cache
    again[0]["kind"] = "edited by a caller"  # a copy: the cache is not touched
    assert list(ev.iter_events()) == [first]
    second = grade(ev, now=NOON + timedelta(minutes=1))  # an append re-parses that file
    cached = list(ev.iter_events())
    events._PARSED.clear()
    assert cached == list(ev.iter_events()) == [first, second]


IST = timezone(timedelta(hours=5, minutes=30))


@pytest.mark.parametrize("utc,tz,day", [
    (datetime(2026, 11, 13, 22, 29, tzinfo=UTC), IST, "2026-11-13"),  # 03:59 IST
    (datetime(2026, 11, 13, 22, 30, tzinfo=UTC), IST, "2026-11-14"),  # 04:00 IST
    (datetime(2026, 11, 13, 3, 59, tzinfo=UTC), UTC, "2026-11-12"),
    (datetime(2026, 11, 13, 4, 0, tzinfo=UTC), UTC, "2026-11-13"),
    (datetime(2026, 11, 13, 0, 30), UTC, "2026-11-12"),  # naive means UTC
    (datetime(2027, 1, 1, 3, 0, tzinfo=UTC), UTC, "2026-12-31"),
])
def test_local_day_rollover(utc, tz, day):
    assert local_day(utc, tz) == day


def test_month_crossing_two_files(ev):
    grade(ev, now=datetime(2026, 11, 30, 12, tzinfo=UTC))
    grade(ev, now=datetime(2026, 12, 1, 12, tzinfo=UTC))
    rel = [p.relative_to(ev.vault.root).as_posix() for p in files(ev)]
    assert rel == ["events/2026/11/cli.jsonl", "events/2026/12/cli.jsonl"]


def test_iter_events_order_and_session_filter(ev):
    s = "s_01JD0000000000000000000000"
    c = grade(ev, now=NOON + timedelta(hours=2))
    a = grade(ev, session=s, now=NOON)
    b1 = grade(ev, session="import", now=NOON + timedelta(hours=1))
    b2 = grade(ev, session=s, now=NOON + timedelta(hours=1))  # same ts: id breaks the tie
    assert [e["id"] for e in ev.iter_events()] == [a["id"], b1["id"], b2["id"], c["id"]]
    assert [e["id"] for e in ev.iter_events(s)] == [a["id"], b2["id"]]
    with pytest.raises(VaultError):
        list(ev.iter_events("../x"))


def test_keys_are_shared_across_logs_and_follow_a_revert(ev):
    a, b = EventLog(ev.vault), EventLog(ev.vault)
    assert not b.has_key("k1")  # b reads the log before a writes
    first = grade(a, key="k1")
    assert grade(b, key="k1", now=NOON + timedelta(hours=1)) == first
    [path] = files(ev)
    assert path.read_bytes().count(b"\n") == 1
    path.write_bytes(b"")  # cortex undo reverts the file
    again = grade(a, key="k1", now=NOON + timedelta(hours=2))
    assert again["id"] != first["id"] and path.read_bytes().count(b"\n") == 1


def problem_status(ev, pid, status, now):
    """What mcp_server._log_problem does: log the event, then set the note's fields."""
    e = ev.append(session="cli", kind="log", source="tutor", now=now, fields={
        "object": f"problem:{pid}",
        "payload": {"kind": "problem_status", "problem": pid, "status": status}})
    ev.vault.set_field("problem", pid, "status", status)
    if status == "solved":
        ev.vault.set_field("problem", pid, "solved_on", e["day"])
    return e


def void(ev, e, now=NOON + timedelta(days=1)):
    return events.void_event(ev, session="cli", event_id=e["id"], reason="wrong", source="self",
                             now=now)


def test_void_solve_resets_problem_to_todo(ev):
    e = problem_status(ev, "lc-167", "solved", NOON)
    void(ev, e)
    f = ev.vault.read("problem", "lc-167").fields
    assert f["status"] == "todo" and "solved_on" not in f


def test_void_solve_restores_the_latest_live_status(ev):
    problem_status(ev, "lc-1", "attempted", NOON)
    e = problem_status(ev, "lc-1", "solved", NOON + timedelta(hours=1))
    void(ev, e)
    f = ev.vault.read("problem", "lc-1").fields
    assert f["status"] == "attempted" and "solved_on" not in f


def test_void_solve_keeps_a_later_hand_edit(ev):
    e = problem_status(ev, "lc-167", "solved", NOON)
    ev.vault.set_field("problem", "lc-167", "status", "attempted")  # the owner's hand edit
    void(ev, e)
    assert ev.vault.read("problem", "lc-167").fields["status"] == "attempted"


def test_void_assessment_result_resets_outcome(ev):
    e = events.log_assessment(ev, session="cli", source="self", assessment="acme-oa-2026",
                              outcome="pass", now=NOON)
    assert ev.vault.read("assessment", "acme-oa-2026").fields["outcome"] == "pass"
    void(ev, e)
    assert ev.vault.read("assessment", "acme-oa-2026").fields["outcome"] == "pending"
