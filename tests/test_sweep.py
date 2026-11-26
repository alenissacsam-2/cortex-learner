"""ideas-r2 #3 (`sweep` mode) and #4 (the batch confidence nudge)."""

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import run

from keepfold import planner
from keepfold.catalog import Catalog
from keepfold.events import EventLog
from keepfold.levels import ConceptState
from keepfold.vault import Vault

NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    shutil.copytree(Path(__file__).parent / "fixtures" / "vault_mini", root)
    return Vault(root)


def grade(n, concept, source="tutor", day="2026-11-14"):
    return {"id": f"e{n:03}", "ts": f"{day}T12:00:{n % 60:02}Z", "day": day, "kind": "grade",
            "object": f"concept:{concept}", "verdict": "correct", "source": source}


def concepts(v, domain, n):
    c = Catalog.load(v)
    if domain not in c.entries:
        c.add("domain", domain)
        c.add("area", "Basics", parent=domain)
    area = next(e.id for e in Catalog.load(v).entries.values()
                if e.kind == "area" and e.domain == domain)
    return [Catalog.load(v).add("concept", f"{domain} {i}", parent=area, force=True)
            for i in range(n)]


def sweep(v, events, states=None):
    states = states if states is not None else {}
    return planner.today(v, Catalog.load(v), states, now=NOW, mode="sweep", events=events)


def test_imported_only_first_then_most_overdue_capped(v):
    a = concepts(v, "algebra", 3)
    evs = [grade(1, a[0], "imported"),
           grade(2, a[1], day=planner.UNKNOWN_DATE),  # the placeholder day counts as imported
           grade(3, a[2], "imported"), grade(4, a[2]),  # a live check too: not imported-only
           grade(5, "arrays-basics", "imported")]
    late = concepts(v, "logic", 12)
    evs += [grade(10 + i, c) for i, c in enumerate(late)]
    states = {c: ConceptState(c, "needs_refresh", next_due=f"2026-11-{i + 1:02}")
              for i, c in enumerate(reversed(late))}
    states[a[2]] = ConceptState(a[2], "learning", next_due="2026-12-01")  # graded, not due
    items = sweep(v, evs, states)
    assert len(items) == planner.SWEEP_CAP and {i["kind"] for i in items} == {"sweep"}
    ids = [i["id"] for i in items]
    assert {a[0], a[1], "arrays-basics"} <= set(ids) and a[2] not in ids
    assert sum(i["why"] == "Only imported evidence" for i in items) == 3
    # the 7 fallbacks are the most overdue (earliest next_due) of the 12 due ones
    assert {i for i in ids if i in late} == set(list(reversed(late))[:7])


def test_placement_weight_orders_and_private_is_never_picked(v):
    evs = [grade(1, "arrays-basics", "imported"), grade(2, "two-pointers", "imported"),
           grade(3, "secret-diary", "imported")]
    ids = [i["id"] for i in sweep(v, evs)]
    # the fixture's Acme OA weighs two-pointers 1.0 (domain 0.6 + concept 0.4), arrays 0.6
    assert ids == ["two-pointers", "arrays-basics"]


def test_domains_interleave_where_possible(v):
    a, b = concepts(v, "algebra", 4), concepts(v, "logic", 2)
    evs = [grade(i, c, "imported") for i, c in enumerate([*a, *b, "arrays-basics"])]
    items = sweep(v, evs)
    cat = Catalog.load(v)
    doms = [cat.entries[i["id"]].domain for i in items]
    assert len(doms) == 7 and all(x != y for x, y in zip(doms, doms[1:], strict=False)), doms


def test_sweep_over_mcp_and_the_confidence_nudge(root):
    nudge = "how sure (1-4)"
    EventLog(Vault(root)).append(  # as the removed importer stamped an undated row
        session="import", kind="grade", source="imported",
        now=datetime(2026, 10, 1, 12).astimezone(),
        fields={"object": "concept:arrays-basics", "question": "imported", "verdict": "correct"})

    async def body(call):
        sure = await call("keepfold_grade", {"concept": "two pointers", "items": [
            {"question": "q", "answer": "a", "correct": True, "confidence": 3}]})
        out = await call("keepfold_start", {"mode": "sweep"})
        assert out["mode"] == "sweep" and "Sweep" in out["display"]
        assert [i["id"] for i in out["today"]][:1] == ["arrays-basics"]
        assert "secret-diary" not in str(out["today"])
        items = [{"question": "q", "answer": "a", "correct": True}]
        first = await call("keepfold_grade", {"concept": "two pointers", "items": items})
        again = await call("keepfold_grade", {"concept": "two pointers", "items": items})
        assert nudge not in sure["next"]
        assert nudge in first["next"] and nudge not in again["next"]

    run(root, body)
