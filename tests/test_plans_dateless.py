"""v8 task 5: study plans without dates, several subjects, the most recently studied first."""

import json
import re
from datetime import UTC, date, datetime, timedelta

from conftest import run
from typer.testing import CliRunner

from cortex import dashboard, events, planner
from cortex.catalog import Catalog
from cortex.cli import app
from cortex.events import EventLog
from cortex.vault import Vault

DAY_WORDS = re.compile(r"\bDay \d|Sprint day|\bbehind\b|overdue|\b\d+ of \d+ days\b", re.I)
ISO = re.compile(r"\d{4}-\d{2}-\d{2}")


def grade(v, concept, when, verdict="correct"):
    EventLog(v).append(session="cli", kind="grade", source="tutor", now=when,
                       fields={"object": f"concept:{concept}", "verdict": verdict})


def touch(v, ids, when):
    EventLog(v).append(session="cli", kind="log", source="tutor", now=when,
                       fields={"payload": {"kind": "touch", "ids": ids}})


def two_subjects(root, now):
    """vault_mini's DSA sprint (an old dated track) plus a DBMS plan; the owner studied DSA
    three days ago and switched to DBMS an hour ago. Acme's test is 6 days out."""
    exam = (now.date() + timedelta(days=6)).isoformat()
    path = root / "assessments/acme-oa-2026.md"
    path.write_text(path.read_text(encoding="utf-8").replace("2026-11-20", exam),
                    encoding="utf-8")
    v = Vault(root)
    c = Catalog.load(v)
    c.add("domain", "DBMS")
    c.add("area", "Queries", parent="dbms")
    c.add("concept", "SQL joins", parent="dbms.queries")
    c.add("concept", "Indexes", parent="dbms.queries")
    (root / "tracks/dbms-plan.md").write_text(
        '---\nid: "dbms-plan"\ntitle: "DBMS plan"\nsubject: "dbms"\n---\n# DBMS plan\n\n'
        "- [x] [[sql-joins]] Read the joins chapter\n- [ ] [[sql-joins]] Join drills\n"
        "- [ ] [[indexes]] Index basics\n", encoding="utf-8")
    grade(v, "two-pointers", now - timedelta(days=3), "wrong")
    grade(v, "sql-joins", now - timedelta(hours=1))
    return v, exam


def plan_lines(text):
    return [ln for ln in text.splitlines() if "DBMS plan" in ln or "Test sprint" in ln]


def test_switching_subjects_puts_the_latest_first_with_no_dates_or_pressure(root, home):
    now = datetime.now(UTC)
    v, exam = two_subjects(root, now)
    catalog = Catalog.load(v)
    states, _ = planner.current_states(v, events.local_day(now), catalog)

    groups = planner.next_up(v, catalog, states, now=now)
    assert [g["subject"] for g in groups][:2] == ["dbms", "dsa"]
    assert groups[0]["title"] == "DBMS"
    assert groups[0]["last_day"] == events.local_day(now - timedelta(hours=1))
    assert [i["title"] for i in groups[0]["items"] if i["kind"] == "track"] == [
        "Join drills", "Index basics"]
    dsa = [i["title"] for i in groups[1]["items"] if i["kind"] == "track"]
    assert dsa[0] == "Two Sum II" and "two-pointers" in {i["id"] for i in groups[1]["items"]}

    items = planner.today(v, catalog, states, now=now)
    track = [i for i in items if i["kind"] == "track"]
    assert [i["why"] for i in track] == ["DBMS plan"] * 2 + ["Test sprint"] * 3

    out = CliRunner().invoke(app, ["today", "--vault", str(root)])
    assert out.exit_code == 0, out.output
    md = (root / "Today.md").read_text(encoding="utf-8")
    seen = {"cli": out.output, "today_md": md}

    async def body(call):
        seen["card"] = (await call("cortex_start", {"mode": "learn"}))["display"]
        seen["short"] = (await call("cortex_start", {"mode": "short"}))["display"]

    run(root, body)
    data = dashboard.build_data(v, now=now)
    seen["dashboard"] = json.dumps({k: data[k] for k in ("plans", "today")}, indent=1)
    for name, text in seen.items():
        assert not DAY_WORDS.search(text), name
        for line in plan_lines(text):  # a plan step never carries a date
            assert not ISO.search(line), (name, line)
    assert md.index("Join drills") < md.index("Two Sum II")  # DBMS leads after the switch
    assert seen["card"].index("Join drills") < seen["card"].index("Two Sum II")
    assert f"Acme oa · {exam}" in md and f"Acme oa · {exam}" in seen["card"]  # exams keep dates
    assert data["assessments"][0]["days_left"] > 0  # the countdown stays
    assert {p["id"]: (p["done"], p["left"]) for p in data["plans"]} == {
        "dbms-plan": (1, 2), "sprint-test": (2, 3)}


def test_last_active_is_a_pure_fold_and_a_touch_counts(root):
    now = datetime(2026, 11, 19, 12, tzinfo=UTC)
    v, _ = two_subjects(root, now)
    catalog = Catalog.load(v)
    evs = list(EventLog(v).iter_events())
    last = planner.last_active(v, catalog, evs)
    assert last["dbms"]["ts"] > last["dsa"]["ts"]
    assert last == planner.last_active(v, catalog, list(reversed(evs)))  # order-free
    touch(v, ["lc-1"], now)  # a touch on a DSA problem page brings DSA back to the front
    later = planner.last_active(v, catalog, list(EventLog(v).iter_events()))
    assert later["dsa"]["ts"] > later["dbms"]["ts"]
    assert planner._by_recency(["math", "dbms", "dsa", "art"], later) == [
        "dsa", "dbms", "math", "art"]  # never studied: last, in the given order


def test_a_plan_without_a_subject_field_takes_its_links_domain_or_its_own_id(root):
    v = Vault(root)
    (root / "tracks/reading.md").write_text(
        '---\nid: "reading"\ntitle: "Reading list"\n---\n- [ ] Read SICP ch. 1\n',
        encoding="utf-8")
    by_id = {p["id"]: p for p in planner.plans(v, Catalog.load(v))}
    assert by_id["sprint-test"]["subject"] == "dsa"
    assert by_id["reading"]["subject"] == "reading"
    assert by_id["reading"]["steps"] == [{"id": None, "title": "Read SICP ch. 1"}]
    now = datetime.combine(date(2026, 11, 15), datetime.min.time(), UTC)
    groups = planner.next_up(v, Catalog.load(v), {}, now=now, events=[])
    assert {g["subject"]: g["title"] for g in groups}["reading"] == "Reading list"
