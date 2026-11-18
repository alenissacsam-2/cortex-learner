"""v5 Phase 2 G5: the circling map (what Cortex has no evidence of)."""

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import run

from cortex import circling
from cortex import session as session_mod
from cortex.catalog import Catalog
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = "2026-11-14"  # the fixture's Acme OA (tests all of dsa) is on 2026-11-20


def page(r, folder, id, front, body=""):
    (r / folder).mkdir(exist_ok=True)
    (r / folder / f"{id}.md").write_text(f'---\nid: "{id}"\n{front}---\n{body}\n',
                                            encoding="utf-8")


def ev(n, kind, concept=None, payload=None, day=TODAY):
    e = {"id": f"e{n:03}", "ts": f"{day}T10:{n % 60:02}:00Z", "day": day, "kind": kind}
    if concept:
        e["object"] = f"concept:{concept}"
    if payload is not None:
        e["payload"] = payload
    return e


@pytest.fixture
def vault(tmp_path):
    r = tmp_path / "vault"
    shutil.copytree(FIXTURES / "vault_mini", r)
    return Vault(r)


def items(vault, events):
    return circling.items(vault, Catalog.load(vault), events, TODAY)


def test_unvisited_counts_the_tests_closure_and_skips_hidden(vault):
    (one,) = items(vault, [])
    assert one["key"] == "unvisited:acme-oa-2026" and one["kind"] == "unvisited"
    assert one["text"].startswith("Cortex has no evidence on 2 of 2 topics for Acme oa (")
    assert "secret-diary" not in one["ids"] and "Private" not in one["text"]
    (one,) = items(vault, [ev(1, "grade", "arrays-basics", {"verdict": "correct"})])
    assert one["ids"] == ["two-pointers"] and "1 of 2" in one["text"]
    seen = [ev(1, "grade", c, {"verdict": "wrong"}) for c in ("arrays-basics", "two-pointers")]
    assert items(vault, seen) == []


def test_taught_loops_decisions_order_cap_and_dismissal(vault):
    page(vault.root, "loops", "write-cv", 'title: "Write the CV"\nloop_type: commitment\n'
         "status: open\ndate: 2026-09-01\n")
    page(vault.root, "loops", "old-idea", 'title: "Idea"\nloop_type: idea\nstatus: open\n'
         "date: 2026-01-01\n")
    page(vault.root, "loops", "fresh", 'title: "Fresh"\nloop_type: commitment\nstatus: open\n'
         "date: 2026-11-01\n")
    page(vault.root, "decisions", "pick-go", 'title: "Pick Go"\nstatus: accepted\n'
         "date: 2026-08-01\nreview_on: 2026-10-01\n", "## Outcome\n")
    page(vault.root, "decisions", "pick-rust", 'title: "Pick Rust"\nstatus: accepted\n'
         "date: 2026-10-20\nreview_on: 2026-11-10\n", "## Outcome\n")  # only 4 days past
    taught = [ev(1, "taught", "two-pointers", day="2026-10-05"),
              ev(2, "taught", "two-pointers", day="2026-10-06")]
    got = items(vault, taught)
    assert [i["key"] for i in got] == ["unvisited:acme-oa-2026", "loop:write-cv",
                                       "decision:pick-go"]  # capped at 3, oldest first
    assert all(i["text"].startswith("Cortex has no evidence") for i in got)
    assert "avoid" not in " ".join(i["text"] for i in got).lower()
    drop = [ev(9, "log", payload={"kind": "circling", "key": k, "verdict": v})
            for k, v in (("unvisited:acme-oa-2026", "dropped"), ("loop:write-cv", "outside"))]
    got = items(vault, taught + drop)
    assert [i["key"] for i in got] == ["decision:pick-go", "taught:two-pointers"]
    assert "Two pointers" in got[1]["text"] and "2 times" in got[1]["text"]
    checked = taught + drop + [ev(5, "ask", "two-pointers", day="2026-10-08")]
    assert [i["key"] for i in items(vault, checked)] == ["decision:pick-go"]


def test_stats_counts_live_verdicts():
    evs = [ev(i, "log", payload={"kind": "circling", "key": f"loop:x{i}", "verdict": v})
           for i, v in enumerate(("loop", "loop", "dropped", "outside", "loop"))]
    evs.append({"id": "v1", "kind": "void", "ref": "e004"})
    assert circling.stats(evs) == {"loop": 2, "dropped": 1, "outside": 1}


def test_reflect_pack_has_circling_wrapped_and_logging(root, monkeypatch):
    now = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(session_mod.SessionManager, "_now", lambda self: now)

    async def body(call):
        out = await call("cortex_reflect")
        (item,) = out["circling"]["items"]  # the fixture's Acme OA has no evidence yet
        assert item["key"] == "unvisited:acme-oa-2026" and "<untrusted_note" in item["text"]
        for bad in ({"key": "nope", "verdict": "loop"}, {"key": "loop:", "verdict": "loop"},
                    {"key": item["key"], "verdict": "avoid"},
                    {"key": item["key"], "verdict": "loop", "reason": "x" * 201}):
            r = await call("cortex_log", {"kind": "circling", "payload": bad})
            assert r["error"] == "invalid_argument", bad
        await call("cortex_log", {"kind": "circling", "payload": {
            "key": item["key"], "verdict": "outside", "reason": "did it at uni"}})
        out = await call("cortex_reflect")
        assert out["circling"] == {"items": [], "stats": {"loop": 0, "dropped": 0,
                                                          "outside": 1}}

    run(root, body)
