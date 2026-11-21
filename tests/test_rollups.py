"""Rollup pages (`cortex.rollups`): a week, month and year page built by code from notes, saved
AI chats, journal lines, study events and browsing; private material only in the private
variant; no rewrite when nothing changed; the CLI; never a model. Made-up data, temp vaults."""

import random
import time

import pytest
from conftest import note
from test_chat import _study_events
from test_chat_facts import _visit
from typer.testing import CliRunner

from cortex import chat, localllm, rollups
from cortex.browse import store
from cortex.catalog import Catalog
from cortex.cli import app
from cortex.index import Index
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")


@pytest.fixture
def vault(root, home):
    (home / ".cortex").mkdir()
    (home / ".cortex" / "config.yaml").write_text(
        "browse:\n  private_domains: [secret.example]\n", encoding="utf-8")
    note(root, "notes/helm.md", "title: Helm charts\ndate: 2026-11-17\n",
         "# Helm charts\n\nDeploying to kubernetes with helm.")
    note(root, "notes/diary.md", "title: Night diary\nprivate: true\ndate: 2026-11-18\n",
         "# Night diary\n\nA private dream.")
    note(root, "resources/chat-kube.md", "title: Kubernetes chat\nkind: resource\n",
         "# Kubernetes chat\n\nTimeline: [[2026-11-03]] · Source: Gemini · Date: 2026-11-03\n\n"
         "We talked about kubernetes operators.")
    note(root, "journal/2026-11-16.md", "title: Journal 2026-11-16\nkind: journal\n"
         "date: 2026-11-16\n", "## Log\n- 09:30 Read about kubernetes operators\n- 12:00 Lunch\n")
    v = Vault(root)
    with store.connect(v) as con:
        _visit(con, "2026-11-16", "https://github.com/k8s", "kubernetes - GitHub",
               "github.com", "github.com", n=2)
        _visit(con, "2026-11-17", "https://github.com/x", "Other repo", "github.com", "github.com")
        _visit(con, "2026-11-17", "https://secret.example/k", "Secret notes",
               "secret.example", "secret.example")
        _visit(con, "2026-11-18", "https://clinic.example/k", "Clinic",
               "clinic.example", "clinic.example", category="health")
    con.close()
    _study_events(v)
    return v


def _read(v, name):
    return (v.root / "rollups" / f"{name}.md").read_text(encoding="utf-8")


def test_week_month_and_year_pages(vault):
    out = rollups.run(vault)
    assert out["written"] > 0 and out["removed"] == 0
    week = _read(vault, "rollup-2026-w47")
    assert week.startswith('---\ntitle: "Week 47 of 2026 (16 Nov to 22 Nov 2026)"\nkind: rollup\n'
                           "period: week\ndate: 2026-11-16\nend: 2026-11-22\n---\n")
    assert "Up: [[rollup-2026-11]]" in week
    assert "- 2026-11-17 [[helm]] Helm charts" in week
    assert "- 2026-11-16 09:30 Read about kubernetes operators" in week
    assert "- github.com: 2 days, 3 visits" in week
    assert "- [[arrays-basics]] Array basics: 2 events on 1 day (1 correct)" in week
    assert "1 wrong" in _read(vault, "rollup-2026-w46")
    month = _read(vault, "rollup-2026-11")
    assert "## Weeks" in month and "- [[rollup-2026-w45]]: 1 saved AI chat" in month
    assert "[[chat-kube]]" in month and "Up: [[rollup-2026]]" in month
    year = _read(vault, "rollup-2026")
    assert "## Months" in year and "[[rollup-2026-11]]" in year and "Up:" not in year
    for name in ("rollup-2026-w47", "rollup-2026-11", "rollup-2026"):
        assert len(_read(vault, name)) <= 2500


def test_a_week_page_has_fixed_sections_and_fits(vault):  # R3-6
    rollups.run(vault)
    week = _read(vault, "rollup-2026-w47")
    heads = [ln[3:] for ln in week.splitlines() if ln.startswith("## ")]
    assert heads == [s for s, _ in rollups.WEEK_SECTIONS]
    assert "## Projects and decisions\n- none\n" in week  # empty, never padded
    # a busy week: every list cut until the page fits, the cut said
    its = [("2026-11-16", "notes", f"n{i}", "A rather long note title number " + str(i), 1,
            "project" if i % 3 else "note", False) for i in range(40)]
    its += [("2026-11-17", "journal", f"09:{i:02d} A journal line about the day {i}", "", 1,
             "", False) for i in range(30)]
    its += [("2026-11-18", "calendar", f"2026-11-18 10:{i:02d} · Meeting {i}", "", 1, "",
             False) for i in range(20)]
    its += [("2026-11-18", "people", "asha", "Asha Rao", 1, "", False)]
    page = rollups.render_week("rollup-2026-w47", "2026-11-16", "2026-11-22", its, False)
    assert 1200 <= len(page) <= rollups.WEEK_MAX
    assert "more" in page and "[[asha]] Asha Rao" in page and "Meeting" in page


def test_people_and_calendar_on_the_week(vault, root):  # R3-6
    note(root, "people/asha.md", "title: Asha Rao\nkind: person\nprivate: false\n", "# Asha Rao\n")
    note(root, "people/ravi.md", "title: Ravi Iyer\nkind: person\n", "# Ravi Iyer\n")  # private
    note(root, "notes/sync.md", "title: Sync\ndate: 2026-11-19\n",
         "# Sync\n\nMet Asha Rao and Ravi Iyer.")
    note(root, "resources/calendar-2026-11.md",
         "title: Calendar 2026-11\nkind: resource\ndate: 2026-11-01\n",
         "# Calendar 2026-11\n\nSource: Calendar · Date: 2026-11-01\n\n## Events\n"
         "- 2026-11-20 10:00 · Exam · Hall 2\n- 2026-11-03 09:00 · Lab\n")
    chat._INDEX.pop(vault.root, None)
    rollups.run(vault)
    week = _read(vault, "rollup-2026-w47")
    assert "- [[asha]] Asha Rao" in week and "- 2026-11-20 10:00 · Exam · Hall 2" in week
    assert "Lab" not in week and "[[calendar-2026-11]]" not in week  # its events, not the page
    assert "ravi" not in "".join(p.read_text(encoding="utf-8")  # a private person: on no page
                                 for p in (vault.root / "rollups").glob("*.md"))
    assert "Lab" in _read(vault, "rollup-2026-w45")


def test_private_material_only_in_the_private_variant(vault):
    rollups.run(vault)
    public = "".join(p.read_text(encoding="utf-8")
                     for p in (vault.root / "rollups").glob("*.md")
                     if not p.stem.endswith("-private"))
    for leak in ("diary", "Night", "secret", "clinic", "secret-diary", "Secret"):
        assert leak not in public
    private = _read(vault, "rollup-2026-w47-private")
    assert "private: true" in private
    assert "[[diary]]" in private
    assert "secret" + ".example" not in private and "clinic" not in private  # never on a page
    assert "secret-diary" in private
    assert "helm" not in private and "github.com" not in private  # the public items stay apart
    # the shared index keeps the private page out and the public one in
    idx = Index(vault, Catalog.load(vault))
    assert "rollup-2026-w47" in idx._notes and "rollup-2026-w47-private" not in idx._notes


def test_study_also_on_a_private_subject_is_private(vault):
    """Audit 21 Nov C9: an event whose `also` names a private concept goes on the private page."""
    from datetime import datetime

    from cortex.events import EventLog

    EventLog(vault).append(session="cli", kind="grade", source="tutor",
                           now=datetime(2026, 11, 12, 12).astimezone(),
                           fields={"object": "concept:arrays-basics", "verdict": "correct",
                                   "also": ["concept:secret-diary"]})
    rollups.run(vault)
    assert "arrays-basics" not in _read(vault, "rollup-2026-w46")
    assert "arrays-basics" in _read(vault, "rollup-2026-w46-private")


def test_no_rewrite_when_nothing_changed(vault):
    rollups.run(vault)
    page = vault.root / "rollups" / "rollup-2026-11.md"
    before = page.stat().st_mtime_ns
    assert rollups.run(vault) is None  # the stamp: nothing read
    (vault.root / ".cortex" / "rollups.json").unlink()
    again = rollups.run(vault)  # a full pass, every text the same
    assert again["written"] == 0 and again["kept"] > 0
    assert page.stat().st_mtime_ns == before
    note(vault.root, "notes/new.md", "title: New one\ndate: 2026-11-19\n", "# New one\n\nText.")
    third = rollups.run(vault)
    assert third["written"] == 3  # its week, month and year; nothing else
    assert "[[new]]" in _read(vault, "rollup-2026-w47")


def test_stale_pages_removed_and_since(vault):
    rollups.run(vault)
    (vault.root / "resources" / "chat-kube.md").unlink()
    out = rollups.run(vault, "2026-11")
    assert out["removed"] == 1 and not (vault.root / "rollups" / "rollup-2026-w45.md").exists()
    with pytest.raises(Exception, match="month"):
        rollups.run(vault, "2026-13")


def test_rollups_are_not_counted_as_notes(vault):
    rollups.run(vault)
    pages = chat._pages_in(vault, "2026-01-01", "2026-12-31",
                           {"notes": True, "chats": True, "private": True})
    assert not [p for p in pages if p[2].startswith("rollup-")]


def test_cli(vault):
    r = CliRunner().invoke(app, ["rollups", "--vault", str(vault.root)])
    assert r.exit_code == 0, r.output
    assert "written" in r.output
    r = CliRunner().invoke(app, ["rollups", "--vault", str(vault.root)])
    assert r.output.strip() == "rollups: nothing changed"


def test_never_a_model(vault, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("rollups must never touch a local model")

    for name in ("find_loaded", "Runner"):
        if hasattr(localllm, name):
            monkeypatch.setattr(localllm, name, refuse)
    assert rollups.run(vault)["written"] > 0


@pytest.mark.slow
def test_speed_on_5k_pages(root, home):
    rnd = random.Random(7)
    for i in range(5000):
        d = f"2025-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}"
        note(root, f"notes/n{i}.md", f"title: Note {i}\ndate: {d}\n", f"# Note {i}\n\nBody {i}.")
    v = Vault(root)
    t = time.perf_counter()
    first = rollups.run(v)  # nearly all of it: the first parse of 5k notes (the index)
    took = time.perf_counter() - t
    t = time.perf_counter()
    assert rollups.run(v) is None
    again = time.perf_counter() - t
    note(root, "notes/n0.md", "title: Note 0 changed\ndate: 2025-03-03\n", "# Note 0\n\nNew.")
    t = time.perf_counter()
    one = rollups.run(v)  # the index refreshed, not rebuilt
    changed = time.perf_counter() - t
    print(f"5k pages: {first['written']} pages in {took:.2f} s, unchanged pass {again:.3f} s, "
          f"one note changed {changed:.2f} s ({one['written']} written)")
    assert one["written"] <= 6 and took < 60 and again < 1 and changed < 10
