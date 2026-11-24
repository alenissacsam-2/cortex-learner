"""F1: levels.replay keeps its last whole-log fold, and problem_concepts its last parse, in
memory; both must give exactly the uncached answers after our own writes, edits made outside
Cortex and a day change. A warm cortex_find stays quick on the 5k-note vault. Made-up vaults
only."""

import dataclasses
import json
import os
import shutil
import statistics
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from test_home import _big, _note

from cortex import levels, mcp_server, planner
from cortex.catalog import Catalog
from cortex.events import EventLog, local_day
from cortex.index import Index
from cortex.vault import Vault

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = local_day(datetime.now(UTC))
BUDGET = 0.3  # s; measured 90 ms on a quiet PC (docs/benchmarks/memory.md, F1): 3x headroom


def _day(n: int, base: str = TODAY) -> str:
    return (date.fromisoformat(base) + timedelta(days=n)).isoformat()


def _uncached():
    levels._LAST.clear()


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURES / "vault_mini", root)
    return Vault(root)


def _grade(v: Vault, obj: str, verdict: str, days_ago: int) -> dict:
    return EventLog(v).append(session="cli", kind="grade", source="external",
                              now=datetime.now(UTC) - timedelta(days=days_ago),
                              fields={"object": obj, "verdict": verdict, "item_type": "recall"})


def _touch_later(path: Path) -> None:
    """An outside edit can keep the size; move the mtime on so the edit is a real change."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))


def _states(v: Vault, day: str = TODAY):
    """current_states from the cache (as left by the last call), checked against a cold fold."""
    cat = Catalog.load(v)
    warm = planner.current_states(v, day, cat)
    _uncached()
    assert warm == planner.current_states(v, day, cat)
    assert planner.current_states(v, day, cat) == warm  # and the hit it now makes
    return warm[0]


def test_the_cached_fold_follows_every_change_it_reads(vault):
    root = vault.root
    _grade(vault, "concept:two-pointers", "correct", 9)
    first = _states(vault)

    # an event appended through EventLog (folded into its parse cache)
    _grade(vault, "concept:two-pointers", "wrong", 1)
    after_append = _states(vault)
    assert after_append != first

    # an event file edited outside Cortex: the wrong becomes a partial
    path = next((root / "events").glob("*/*/cli.jsonl"))
    text = path.read_bytes()
    assert b'"verdict":"wrong"' in text
    path.write_bytes(text.replace(b'"verdict":"wrong"', b'"verdict":"partial"'))
    _touch_later(path)
    edited = _states(vault)
    assert edited != after_append

    # a problem note's concepts changed outside Cortex: its grades land elsewhere
    _grade(vault, "problem:lc-167", "correct", 2)
    on_two_pointers = _states(vault)
    lc = root / "problems" / "lc-167.md"
    lc.write_bytes(lc.read_bytes().replace(b'["[[two-pointers]]"]', b'["[[arrays-basics]]"]'))
    moved = _states(vault)
    assert moved["arrays-basics"] != on_two_pointers.get("arrays-basics")
    assert levels.problem_concepts(vault)["lc-167"] == ["arrays-basics"]

    # an assessment's date moved into the 14-day window and out again (high retention)
    oa = root / "assessments" / "acme-oa-2026.md"
    original = oa.read_bytes()
    oa.write_bytes(original.replace(b'date: "2026-11-20"', f'date: "{_day(5)}"'.encode()))
    inside = _states(vault)
    assert "two-pointers" in planner.exam_concepts(vault, Catalog.load(vault), TODAY)
    oa.write_bytes(original.replace(b'date: "2026-11-20"', f'date: "{_day(40)}"'.encode()))
    outside = _states(vault)
    assert inside["two-pointers"].interval_days != outside["two-pointers"].interval_days

    # the day rolling over
    later = _states(vault, _day(30))
    assert later != outside
    assert _states(vault) == outside

    # a problem note added and deleted outside Cortex
    _note(root, "problems", "lc-2", {"id": "lc-2", "concepts": ["[[two-pointers]]"]})
    _grade(vault, "problem:lc-2", "correct", 0)
    _states(vault)
    (root / "problems" / "lc-2.md").unlink()
    assert "lc-2" not in levels.problem_concepts(vault)
    _states(vault)


def test_callers_get_their_own_copies(vault):
    _grade(vault, "concept:two-pointers", "wrong", 1)
    states, _ = planner.current_states(vault, TODAY)
    states["two-pointers"].open_mistakes.append("x")
    states["two-pointers"].state = "solid"
    states.pop("arrays-basics", None)
    again, _ = planner.current_states(vault, TODAY)
    assert "x" not in again["two-pointers"].open_mistakes
    assert again["two-pointers"].state != "solid"
    pcs = levels.problem_concepts(vault)
    pcs["lc-1"].append("x")
    assert "x" not in levels.problem_concepts(vault)["lc-1"]


@pytest.mark.parametrize("cls", [levels.ConceptState, levels.MistakeState])
def test_state_fields_are_immutable_or_copied_containers(cls):
    """levels._own copies list, dict and set fields; anything else must be immutable, or a
    caller's change would reach the cached fold."""
    ok = {"str", "int", "float", "bool", "None", "datetime", "date", "list", "dict", "set"}
    for f in dataclasses.fields(cls):
        kinds = {part.strip().split("[")[0] for part in str(f.type).split("|")}
        assert kinds <= ok, (cls.__name__, f.name, f.type)


def test_one_vault_at_a_time(tmp_path, vault):
    other = tmp_path / "other"
    shutil.copytree(FIXTURES / "vault_mini", other)
    planner.current_states(vault, TODAY)
    planner.current_states(Vault(other), TODAY)
    assert levels._LAST["replay"][0][0] == Vault(other).root
    assert levels._LAST["problems"][0][0] == Vault(other).root
    assert set(levels._LAST) == {"replay", "problems"}


def test_the_scan_never_enters_browse_private(vault, monkeypatch):
    root = vault.root
    (root / "browse" / "private" / "deep").mkdir(parents=True)
    (root / "browse" / "sites" / "news").mkdir(parents=True)
    _note(root, "browse/private", "secret-day", {"title": "secret"})
    _note(root, "browse/private/deep", "deeper", {"title": "deeper"})
    _note(root, "browse/sites/news", "site-a", {"title": "Site A"})
    _note(root, "browse", "day-1", {"title": "Day 1"})
    listed = []
    real = os.scandir
    monkeypatch.setattr(os, "scandir", lambda p: listed.append(os.path.normcase(p)) or real(p))
    paths = {p.stem for p in vault.note_paths("browse")}
    assert paths == {"site-a", "day-1"}
    idx = Index(vault, Catalog.load(vault))
    scanned = {p.stem for p in idx._scan()}
    assert {"site-a", "day-1"} <= scanned and not {"secret-day", "deeper"} & scanned
    private = os.path.normcase(root / "browse" / "private")
    assert listed and not [p for p in listed if p.startswith(private)]


def test_outside_edits_and_deletes_reach_the_index(vault):
    root = vault.root
    p = _note(root, "resources", "outside", {"title": "Outside"}, "firstword\n")
    idx = Index(vault, Catalog.load(vault))
    assert [h["id"] for h in idx.find("firstword")] == ["outside"]
    p.write_bytes(p.read_bytes().replace(b"firstword", b"otherword"))  # written and closed
    _touch_later(p)
    idx.refresh()
    assert idx.find("firstword") == [] and [h["id"] for h in idx.find("otherword")] == ["outside"]
    p.unlink()
    idx.refresh()
    assert idx.find("otherword") == [] and "outside" not in idx._notes


FINDS = [("two pointers", None), ("", "due"), ("", "weak"), ("", "ready"), ("sum", "problem"),
         ("freshword", None), ("newerword", None)]


def _answers(cx) -> list:
    out = [cx.cortex_find(q, kind).get("hits") for q, kind in FINDS]
    return out + [cx.cortex_read("two-pointers").get("text")]


def test_find_and_read_answer_as_uncached_across_changes(vault):
    root = vault.root
    cx = mcp_server.Cortex(vault)
    cx._ready.wait()

    def same():
        warm = _answers(cx)
        _uncached()
        assert _answers(cx) == warm
        return warm

    first = same()
    _grade(vault, "concept:two-pointers", "wrong", 1)
    assert same() != first
    p = _note(root, "resources", "fresh-page", {"title": "Fresh page"}, "freshword two pointers\n")
    added = same()
    p.write_bytes(p.read_bytes().replace(b"freshword", b"newerword"))
    _touch_later(p)
    assert same() != added
    p.unlink()
    same()
    oa = root / "assessments" / "acme-oa-2026.md"
    oa.write_bytes(oa.read_bytes().replace(b'date: "2026-11-20"', f'date: "{_day(3)}"'.encode()))
    same()


@pytest.mark.slow
@pytest.mark.perf
def test_a_warm_find_stays_quick_on_5k_notes(tmp_path, capsys):
    root = tmp_path / "big"
    _big(root)
    cx = mcp_server.Cortex(Vault(root))
    cx._ready.wait()
    cx.cortex_find("array list")  # the first call folds the log and warms the caches
    times = []
    for _ in range(7):
        t = time.perf_counter()
        out = cx.cortex_find("array list")
        times.append(time.perf_counter() - t)
    assert out["hits"]
    warm = statistics.median(times)
    with capsys.disabled():
        print(f"\nwarm cortex_find on 5k notes: {warm * 1000:.0f} ms (median of 7)")
    assert warm < BUDGET


def test_events_signature_moves_with_every_write(vault):
    log = EventLog(vault)
    assert log.signature() == ()
    _grade(vault, "concept:two-pointers", "wrong", 1)
    one = log.signature()
    assert len(one) == 1
    _grade(vault, "concept:two-pointers", "correct", 0)
    two = log.signature()
    assert two != one and two == log.signature()
    path = one[0][0]
    path.write_bytes(path.read_bytes() + json.dumps({"bad": 1}).encode() + b"\n")
    assert log.signature() != two
