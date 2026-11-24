"""R29a: a sync tool's conflict copies (Syncthing, OneDrive, Dropbox) never double-count events
or vanish silently: the event log counts each event once, doctor and lint name the copies."""

import json
import logging
import shutil
from datetime import UTC, datetime

import pytest
from conftest import FIXTURES

from cortex import doctor, lint, sync_copies
from cortex.events import EventLog
from cortex.vault import Vault

NOON = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
SID = "s_01JCDE0000000000000000ABCD"
OTHER = "s_01JCDE0000000000000000WXYZ"


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "vault"
    shutil.copytree(FIXTURES / "vault_mini", r)
    return r


@pytest.mark.parametrize("name, siblings, orig", [
    ("two-pointers.sync-conflict-20261124-101010-ABCDEF7.md", (), "two-pointers.md"),
    (f"{SID}.sync-conflict-20261124-101010-Q2W3E4R.jsonl", (), f"{SID}.jsonl"),
    ("inbox.SYNC-CONFLICT-20261124-101010-abcdef7.md", (), "inbox.md"),
    ("notes (conflicted copy 2026-11-24).md", (), "notes.md"),
    ("notes (Alen's conflicted copy 2026-11-24).md", (), "notes.md"),
    (f"{SID} (Conflicted Copy 2026-11-24).jsonl", (), f"{SID}.jsonl"),
    ("profile-DESKTOP-AB12CD3.md", {"profile.md"}, "profile.md"),
    ("graph-theory-LAPTOP-2.md", {"graph-theory.md"}, "graph-theory.md"),
    (f"{SID}-DESKTOP-AB12CD3.jsonl", {f"{SID}.jsonl"}, f"{SID}.jsonl"),
    # ordinary names: a page id's own hyphens, a session file, no original beside it
    ("graph-theory.md", {"graph.md"}, None),
    ("README-FIRST.md", (), None),
    (f"{SID}.jsonl", (), None),
    ("cli.jsonl", (), None),
    ("profile.md", (), None),
])
def test_original_names_the_file_a_copy_stands_for(name, siblings, orig):
    assert sync_copies.original(name, siblings) == orig


def _copy(root, rel, new):
    src = root / rel
    shutil.copy(src, src.with_name(new))
    return src.with_name(new)


def test_find_lists_copies_but_never_dot_folders_or_private_browsing(root):
    _copy(root, "concepts/two-pointers.md", "two-pointers (conflicted copy 2026-11-24).md")
    _copy(root, "me/profile.md", "profile-DESKTOP-AB12CD3.md")
    for d in (".cortex", "browse/private"):
        (root / d).mkdir(parents=True, exist_ok=True)
        (root / d / "x.sync-conflict-20261124-101010-ABCDEF7.md").write_text("x")
    assert sync_copies.find(root) == [
        ("concepts/two-pointers (conflicted copy 2026-11-24).md", "concepts/two-pointers.md"),
        ("me/profile-DESKTOP-AB12CD3.md", "me/profile.md")]


# ---- events: one copy of each event ------------------------------------------------------


def _log(root):
    return EventLog(Vault(root))


def _grade(log, session=SID, key=None, now=NOON):
    return log.append(session=session, kind="grade", source="external", key=key, now=now,
                      fields={"object": "concept:two-pointers", "verdict": "correct"})


def _session_file(root, sid=SID):
    return next((root / "events").glob(f"*/*/{sid}.jsonl"))


def _line(ev):
    return (json.dumps(ev, separators=(",", ":")) + "\n").encode()


def test_a_conflict_copy_adds_only_the_events_the_session_file_lacks(root):
    log = _log(root)
    a, b = _grade(log, key="ka"), _grade(log, key="kb")
    main = _session_file(root)
    # the other device's copy: the same two events, plus one appended there
    c = {**b, "id": "01JCDE00000000000000000ZZZ", "key": "kc", "ts": "2026-11-13T12:05:00Z"}
    copy = main.with_name(f"{SID}.sync-conflict-20261124-101010-ABCDEF7.jsonl")
    copy.write_bytes(main.read_bytes() + _line(c))
    fresh = _log(root)
    assert [e["id"] for e in fresh.iter_events()] == [a["id"], b["id"], c["id"]]
    assert [e["id"] for e in fresh.iter_events(session=SID)] == [a["id"], b["id"], c["id"]]
    assert fresh.has_key("kc") and fresh.has_key("ka")
    # our own appends go to the session file, never the copy
    before = copy.read_bytes()
    d = _grade(fresh, key="kd", now=NOON.replace(minute=10))
    assert copy.read_bytes() == before and d["id"] in main.read_text(encoding="utf-8")
    assert [e["id"] for e in fresh.iter_events()] == [a["id"], b["id"], c["id"], d["id"]]


def test_a_differing_copy_keeps_the_session_files_event_and_warns_once(root, caplog):
    log = _log(root)
    a = _grade(log)
    main = _session_file(root)
    copy = main.with_name(f"{SID}-DESKTOP-AB12CD3.jsonl")
    copy.write_bytes(_line({**a, "verdict": "wrong"}))
    with caplog.at_level(logging.WARNING, logger="cortex.events"):
        got = list(_log(root).iter_events())
        list(_log(root).iter_events())
    assert [(e["id"], e["verdict"]) for e in got] == [(a["id"], "correct")]
    assert sum("sync conflict copy" in r.message for r in caplog.records) == 1


def test_a_session_read_skips_another_sessions_events_in_its_copy(root):
    log = _log(root)
    mine, theirs = _grade(log), _grade(log, session=OTHER)
    main = _session_file(root)
    main.with_name(f"{SID} (conflicted copy 2026-11-24).jsonl").write_bytes(
        _line(mine) + _line(theirs))
    fresh = _log(root)
    assert [e["id"] for e in fresh.iter_events(session=SID)] == [mine["id"]]
    assert [e["id"] for e in fresh.iter_events(session=OTHER)] == [theirs["id"]]
    assert [e["id"] for e in fresh.iter_events()] == [mine["id"], theirs["id"]]


def test_a_copy_appearing_later_is_seen_by_a_warm_log(root):
    log = _log(root)
    a = _grade(log, key="ka")
    assert [e["id"] for e in log.iter_events()] == [a["id"]] and not log.has_key("kz")
    z = {**a, "id": "01JCDE00000000000000000ZZZ", "key": "kz", "ts": "2026-11-13T12:09:00Z"}
    _session_file(root).with_name(f"{SID}.sync-conflict-20261124-101010-ABCDEF7.jsonl") \
        .write_bytes(_line(a) + _line(z))
    assert [e["id"] for e in log.iter_events()] == [a["id"], z["id"]] and log.has_key("kz")


# ---- doctor and lint name the copies ---------------------------------------------------------


def test_doctor_warns_with_the_count_and_up_to_five_paths(root):
    assert doctor.check_sync_copies(root) == ("PASS", "Sync conflict copies", "none", "")
    for i in range(7):
        (root / "concepts" / f"n{i}.sync-conflict-20261124-10101{i}-ABCDEF7.md").write_text("x")
    status, name, detail, fix = doctor.check_sync_copies(root)
    assert status == "WARN" and detail.startswith("7 found: concepts/n0.sync-conflict-")
    assert detail.count("concepts/") == 5 and detail.endswith(" and 2 more")
    assert fix.startswith(doctor.SYNC_COPY_FIX)


def test_the_apps_doctor_shows_the_warn_line(root_committed):
    from cortex.app import actions

    root = root_committed
    (root / ".cortex").mkdir(exist_ok=True)
    _copy(root, "concepts/two-pointers.md", "two-pointers-DESKTOP-AB12CD3.md")

    class Job:
        def say(self, line):
            pass

    lines = actions.doctor(Job(), root)["lines"]
    assert "WARN  Sync conflict copies: 1 found: concepts/two-pointers-DESKTOP-AB12CD3.md" \
        in lines, lines


def test_lint_reports_each_copy_but_not_a_private_pages(root):
    _copy(root, "concepts/two-pointers.md", "two-pointers.sync-conflict-20261124-101010-ABCDEF7.md")
    _copy(root, "concepts/secret-diary.md",
          "secret-diary.sync-conflict-20261124-101010-ABCDEF7.md")
    (root / "events" / "2026" / "11").mkdir(parents=True)
    (root / "events" / "2026" / "11" / "cli (conflicted copy 2026-11-24).jsonl").write_text("")
    findings, totals = lint.lint_report(Vault(root))
    got = [(f["id"], f["detail"]) for f in findings if f["kind"] == "sync_conflict"]
    assert got == [
        ("concepts/two-pointers.sync-conflict-20261124-101010-ABCDEF7.md",
         "a sync conflict copy of concepts/two-pointers.md: open both, keep what you want in "
         "the original, delete the copy"),
        ("events/2026/11/cli (conflicted copy 2026-11-24).jsonl",
         "a sync conflict copy of events/2026/11/cli.jsonl: open both, keep what you want in "
         "the original, delete the copy")]
    assert totals["sync_conflict"] == 2 and findings[0]["kind"] == "sync_conflict"
