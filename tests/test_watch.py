"""Watched folders (R18): adding and removing one (CLI, app), the walk's rules (no links out, no
hidden or system folders, never the vault), a pass that reads only new and changed files and at
most PER_PASS of them, a folder that is gone, and the cost of an unchanged 2,000-file folder.
Every folder is made up in tmp_path."""

import os
import shutil
import sys
import time
from pathlib import Path

import pytest
from test_app import _serve, call, fakes  # noqa: F401 (fakes: autouse, nothing real runs)
from test_more_sources import _repo
from typer.testing import CliRunner

from keepfold import importers, sources, watch
from keepfold.cli import app as cli
from keepfold.vault import Vault, VaultError

runner = CliRunner()


def _notes(d: Path, n: int, start: int = 0) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for i in range(start, start + n):
        (d / f"note{i}.md").write_bytes(f"# Note {i}\n\nWhat I learned on day {i}.\n".encode())
    return d


def _resources(v: Vault, prefix: str) -> list[str]:
    return sorted(p.stem for p in (v.root / "resources").glob(f"{prefix}-*.md"))


def _hide(p: Path) -> None:
    import ctypes
    assert ctypes.windll.kernel32.SetFileAttributesW(str(p), 0x2)


def _link(target: Path, link: Path) -> None:
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))  # needs no admin rights, unlike a symlink
    else:
        os.symlink(target, link)


def test_the_walk_never_leaves_the_folder_nor_reads_hidden_or_the_vault(tmp_path):
    d, out = tmp_path / "Papers", tmp_path / "Outside"
    for rel in ("a.md", "sub/b.pdf", "sub/c.png", ".hidden/d.md", "node_modules/e.md",
                "attr/f.md", "MyVault/g.md", ".h.md"):
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(b"# x\n\ntext\n")
    _notes(out, 1)
    _link(out, d / "linked")
    if sys.platform == "win32":
        _hide(d / "attr")
        (d / "sub" / "hidden.md").write_bytes(b"x")
        _hide(d / "sub" / "hidden.md")
    else:
        shutil.rmtree(d / "attr")
    got, whole = watch.walk(d, watch._norm(d / "MyVault"), watch.SUFFIXES["docs"])
    assert sorted(got) == ["a.md", "sub/b.pdf"] and whole
    sig, entry = got["sub/b.pdf"]
    assert sig == [(d / "sub/b.pdf").stat().st_mtime_ns, 10] and entry[0] == "sub/b.pdf"
    assert entry[3]() == b"# x\n\ntext\n"
    assert sorted(watch.walk(d, watch._norm(tmp_path / "v"), watch.SUFFIXES["notes"])[0]) == [
        "MyVault/g.md", "a.md"]


def test_add_refuses_the_vault_and_what_is_not_a_folder(root_plain, tmp_path):
    v = Vault(root_plain)
    for bad, why in ((root_plain, "your vault"), (root_plain / "me", "your vault"),
                     ("Papers", "full path"), (tmp_path / "none", "is not a folder")):
        with pytest.raises(VaultError, match=why):
            watch.add(v, bad)
    with pytest.raises(VaultError, match="source must be"):
        watch.add(v, tmp_path, "photos")
    with pytest.raises(VaultError):
        watch.add(v, tmp_path, "docs", "everyone")
    assert watch.watched(v) == []


def test_add_list_replace_and_remove(root_plain, tmp_path):
    v, d = Vault(root_plain), _notes(tmp_path / "Notes", 2)
    told = []
    w = watch.add(v, f'"{d}"', "docs", "private", said=told.append)
    assert w == {"path": str(d.resolve()), "source": "docs", "tier": "local"}
    assert sources.remembered(v)["docs"] is sources.Audience.LOCAL  # a given tier is kept
    listing = watch.listing(v)
    assert listing["rows"] == [{**w, "missing": False}]
    assert listing["tiers"]["docs"] == "local" and listing["tiers"]["notes"] == "cloud"
    watch.run(v)
    assert str(d.resolve()) in watch._json(v, watch.FILES, {})
    w2 = watch.add(v, d, "notes")  # again, as notes: replaced, and its files read afresh
    assert w2["tier"] == "cloud" and [x["source"] for x in watch.watched(v)] == ["notes"]
    assert watch._json(v, watch.FILES, {}) == {}
    assert watch.remove(v, str(d)) and watch.watched(v) == []
    assert not watch.remove(v, str(d))


def test_a_pass_reads_new_and_changed_files_only(root_plain, tmp_path, monkeypatch):
    v, d = Vault(root_plain), _notes(tmp_path / "Docs", 3)
    watch.add(v, d, "docs", "normal")
    runs = []
    real = sources.run
    monkeypatch.setattr(sources, "run", lambda *a, **k: runs.append(a[1]) or real(*a, **k))
    r = watch.run(v)
    assert r == [{"path": str(d.resolve()), "read": 3, "left": 0,
                  "line": "Documents: read 3, imported 3, private 0, duplicate 0, skipped 0"}]
    assert _resources(v, "docs") == ["docs-note0", "docs-note1", "docs-note2"]
    last = watch.watched(v)[0]["last"]
    assert last["line"] == r[0]["line"] and last["at"][:4].isdigit()
    assert watch.run(v)[0] == {"path": str(d.resolve()), "read": 0, "left": 0}
    assert len(runs) == 1  # unchanged: a stat each, no import
    (d / "note1.md").write_bytes(b"# Note 1\n\nWhat I learned on day 1, and more since.\n")
    os.utime(d / "note1.md", ns=(1, 2_000_000_000_000_000_000))
    _notes(d, 1, start=3)
    (d / "note0.md").unlink()
    r = watch.run(v)
    # a watched document is keyed by its file: a changed one rewrites its page
    assert r[0]["read"] == 2 and "imported 1, updated 1" in r[0]["line"] and len(runs) == 2
    assert _resources(v, "docs") == ["docs-note0", "docs-note1", "docs-note2", "docs-note3"]
    assert "and more since" in (v.root / "resources" / "docs-note1.md").read_text("utf-8")
    manifest = watch._json(v, watch.FILES, {})[str(d.resolve())]
    assert sorted(manifest) == ["note1.md", "note2.md", "note3.md"]
    assert watch.watched(v)[0]["last"]["line"] == r[0]["line"]


def test_an_edited_watched_document_rewrites_its_one_page(root_plain, tmp_path):
    from test_more_sources import _docx
    v, d = Vault(root_plain), tmp_path / "Docs"
    d.mkdir()
    (d / "plan.docx").write_bytes(_docx(["Week one: linear algebra"], "Spring Plan"))
    watch.add(v, d, "docs", "normal")
    assert "imported 1" in watch.run(v)[0]["line"]
    (d / "plan.docx").write_bytes(_docx(["Week one: probability instead"], "Spring Plan"))
    os.utime(d / "plan.docx", ns=(1, 2_000_000_000_000_000_000))
    assert "imported 0, updated 1" in watch.run(v)[0]["line"]
    assert _resources(v, "docs") == ["docs-spring-plan"]
    page = (v.root / "resources" / "docs-spring-plan.md").read_text("utf-8")
    assert "probability instead" in page and "linear algebra" not in page


def test_a_watched_document_is_keyed_by_its_path_in_the_folder(tmp_path):
    d = _notes(tmp_path / "Docs" / "sub", 1).parent.resolve()
    entries, _ = watch.walk(d, watch._norm(tmp_path / "vault"), watch.SUFFIXES["docs"])
    items = list(watch._source("docs", d, [e for _, e in entries.values()]).items())
    assert [it.key for it in items] == [f"docs {watch._norm(d)} sub/note0.md"]


def test_the_same_name_in_two_watched_folders_is_two_pages(root_plain, tmp_path):
    v, a, b = Vault(root_plain), tmp_path / "A", tmp_path / "B"
    for d, text in ((a, "Text of the first folder."), (b, "Text of the second folder.")):
        d.mkdir()
        (d / "notes.md").write_bytes(f"# Notes\n\n{text}\n".encode())
        watch.add(v, d, "docs", "normal")
    assert [r["read"] for r in watch.run(v)] == [1, 1]
    pages = {n: (v.root / "resources" / f"{n}.md").read_text("utf-8")
             for n in _resources(v, "docs")}
    assert len(pages) == 2 and sum("first folder" in t for t in pages.values()) == 1
    (a / "notes.md").write_bytes(b"# Notes\n\nThe first folder, edited.\n")
    os.utime(a / "notes.md", ns=(1, 2_000_000_000_000_000_000))
    r = watch.run(v)
    assert "imported 0, updated 1" in r[0]["line"] and r[1]["read"] == 0
    after = {n: (v.root / "resources" / f"{n}.md").read_text("utf-8")
             for n in _resources(v, "docs")}
    assert sorted(after) == sorted(pages)
    assert [n for n in pages if pages[n] != after[n]] == [
        n for n in pages if "first folder" in pages[n]]
    assert any("The first folder, edited." in t for t in after.values())


def test_at_most_per_pass_files_are_read_and_the_rest_next_time(root_plain, tmp_path,
                                                                monkeypatch):
    v, d = Vault(root_plain), _notes(tmp_path / "Notes", 5)
    watch.add(v, d, "notes", "normal")
    monkeypatch.setattr(watch, "PER_PASS", 2)
    first = watch.run(v)[0]
    assert (first["read"], first["left"]) == (2, 3)
    assert first["line"].endswith("; 3 more on the next pass")
    assert [watch.run(v)[0]["read"] for _ in range(3)] == [2, 1, 0]
    assert len(_resources(v, "notes")) == 5
    (d / "note0.md").write_bytes(b"# Note 0\n\nWhat I learned on day 0, and more since.\n")
    os.utime(d / "note0.md", ns=(1, 2_000_000_000_000_000_000))
    r = watch.run(v)[0]
    assert r["read"] == 1 and "imported 0, updated 1" in r["line"]
    assert len(_resources(v, "notes")) == 5


def test_a_file_over_the_limit_is_skipped_once(root_plain, tmp_path, monkeypatch):
    v, d = Vault(root_plain), _notes(tmp_path / "Notes", 1)
    (d / "big.md").write_bytes(b"# Big\n\n" + b"word " * 40)
    monkeypatch.setattr(importers, "MAX_BYTES", 100)
    watch.add(v, d, "notes", "normal")
    r = watch.run(v)[0]
    assert r["read"] == 2 and "imported 1" in r["line"] and "skipped 1" in r["line"]
    assert watch.run(v)[0]["read"] == 0  # recorded, not read again until it changes


def test_a_folder_that_is_gone_is_reported_and_kept(root_plain, tmp_path, monkeypatch):
    v, d = Vault(root_plain), _notes(tmp_path / "Docs", 1)
    other = _notes(tmp_path / "Other", 1)
    watch.add(v, d, "docs", "normal")
    watch.add(v, other, "docs", "skip")
    shutil.rmtree(d)
    r = watch.run(v)
    assert r[0] == {"path": str(d.resolve()), "read": 0, "left": 0, "missing": True,
                    "line": watch.GONE}
    assert r[1] == {"path": str(other.resolve()), "read": 0, "left": 0}  # skip reads nothing
    rows = watch.listing(v)["rows"]
    assert [x["missing"] for x in rows] == [True, False] and rows[0]["last"]["line"] == watch.GONE
    _notes(d, 1)
    assert watch.run(v)[0]["read"] == 1
    monkeypatch.setattr(watch, "walk", lambda *a: (_ for _ in ()).throw(OSError("disk gone")))
    assert watch.run(v)[0]["line"] == "Failed: disk gone"


def test_watched_git_projects_are_read_again_after_a_commit(root_plain, tmp_path):
    from test_more_sources import _git
    v, projects = Vault(root_plain), tmp_path / "projects"
    repo = _repo(projects, "planner")
    watch.add(v, projects, "repos", "normal")
    assert watch.run(v)[0]["read"] == 1 and _resources(v, "repo") == ["repo-planner"]
    assert watch.run(v)[0]["read"] == 0
    _git(repo, "commit", "-q", "--allow-empty", "-m", "Add monthly view")
    r = watch.run(v)[0]
    assert r["read"] == 1 and "updated 1" in r["line"]
    assert "Add monthly view" in (v.root / "resources" / "repo-planner.md").read_text("utf-8")


def test_cli_watch_add_list_remove(root_plain, tmp_path):
    d = _notes(tmp_path / "Papers", 1)
    out = runner.invoke(cli, ["watch", "list", "--vault", str(root_plain)]).output
    assert "No folder is watched" in out
    out = runner.invoke(cli, ["watch", "add", str(d), "--tier", "private", "--vault",
                              str(root_plain)]).output
    assert f"Watching {d.resolve()} as docs, tier private" in out
    out = runner.invoke(cli, ["watch", "list", "--vault", str(root_plain)]).output
    assert f"{d.resolve()}  docs  private" in out and "not read yet" in out
    watch.run(Vault(root_plain))
    out = runner.invoke(cli, ["watch", "list", "--vault", str(root_plain)]).output
    assert ": Documents: read 1, imported 1, private 1" in out
    out = runner.invoke(cli, ["watch", "add", str(root_plain), "--vault", str(root_plain)])
    assert "your vault" in out.output and out.exit_code != 0
    out = runner.invoke(cli, ["watch", "remove", str(d), "--vault", str(root_plain)]).output
    assert "Stopped watching" in out
    out = runner.invoke(cli, ["watch", "remove", str(d), "--vault", str(root_plain)])
    assert "is not watched" in out.output and out.exit_code != 0


@pytest.fixture
def server_app(root_committed, home):
    (root_committed / ".keepfold").mkdir(exist_ok=True)
    srv, a = _serve(root_committed)
    yield a
    srv.shutdown()
    srv.server_close()


def test_app_adds_lists_and_removes_a_watched_folder(server_app, tmp_path):
    d = _notes(tmp_path / "Papers", 1)
    assert call(server_app, "GET", "/api/setup")[1]["watched"]["rows"] == []
    st, js, _, _ = call(server_app, "POST", "/api/import/watch/add",
                        {"path": str(d), "source": "docs", "tier": "local"})
    assert st == 200 and js["watched"]["rows"] == [
        {"path": str(d.resolve()), "source": "docs", "tier": "local", "missing": False}]
    assert call(server_app, "GET", "/api/setup")[1]["watched"]["tiers"]["docs"] == "local"
    for bad in ({"path": str(d), "source": "photos"}, {"path": "", "source": "docs"},
                {"path": str(d), "source": "docs", "tier": "everyone"},
                {"path": str(server_app.root), "source": "docs"}):
        assert call(server_app, "POST", "/api/import/watch/add", bad)[0] == 400
    assert server_app.watch_auto()[0]["read"] == 1  # the periodic pass's call
    st, js, _, _ = call(server_app, "POST", "/api/import/watch/remove", {"path": str(d)})
    assert st == 200 and js["watched"]["rows"] == []
    assert call(server_app, "POST", "/api/import/watch/remove", {"path": str(d)})[0] == 400


@pytest.mark.perf
def test_an_unchanged_2000_file_folder_costs_a_stat_each(root_plain, tmp_path, monkeypatch):
    """The pass over a watched folder nothing changed in: a listing and a manifest compare,
    measured at about 20 ms for 2,000 files in 20 folders (50 ms cold)."""
    v, d = Vault(root_plain), tmp_path / "Big"
    for i in range(20):
        _notes(d / f"part{i}", 100, start=i * 100)
    watch.add(v, d, "docs", "normal")
    walked, _ = watch.walk(d.resolve(), watch._norm(root_plain), watch.SUFFIXES["docs"])
    assert len(walked) == 2000
    watch._write(v, watch.FILES, {str(d.resolve()): {k: s for k, (s, _) in walked.items()}})
    monkeypatch.setattr(sources, "run", lambda *a, **k: pytest.fail("an unchanged file read"))
    took = []
    for _ in range(5):
        t = time.perf_counter()
        r = watch.run(v)
        took.append(time.perf_counter() - t)
    assert r[0]["read"] == 0
    assert min(took) < 0.5, took


def _cal(d: Path, name: str, *events: tuple[str, str, str]) -> Path:
    """An .ics of (uid, YYYYMMDD, summary) all-day events, its mtime moved on each write."""
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(("BEGIN:VCALENDAR\n" + "".join(
        f"BEGIN:VEVENT\nUID:{u}\nDTSTART;VALUE=DATE:{day}\nSUMMARY:{s}\n"
        "ATTENDEE:mailto:friend@example.com\nDESCRIPTION:secret agenda\nEND:VEVENT\n"
        for u, day, s in events) + "END:VCALENDAR\n").encode())
    t = getattr(_cal, "t", 1_900_000_000_000_000_000) + 1_000_000_000
    _cal.t = t
    os.utime(f, ns=(t, t))
    return f


def test_a_watched_calendar_adds_new_events_and_never_rereads_an_unchanged_file(
        root_plain, tmp_path, monkeypatch):
    v, d = Vault(root_plain), tmp_path / "Calendars"
    _cal(d, "uni.ics", ("a", "20261210", "DSA exam"))
    _cal(d, "notes.txt", ("x", "20261210", "not a calendar"))
    with pytest.raises(VaultError, match="is not a folder"):
        watch.add(v, d / "notes.txt", "calendar")  # only an .ics file stands for its folder
    w = watch.add(v, d / "uni.ics", "calendar", "normal")  # one file: its folder is watched
    assert w["path"] == str(d.resolve()) and w["source"] == "calendar"
    runs = []
    real = sources.run
    monkeypatch.setattr(sources, "run", lambda *a, **k: runs.append(a[1]) or real(*a, **k))
    r = watch.run(v)[0]
    assert r["read"] == 1 and r["line"].startswith("Calendar: read 1, imported 1")
    page = (v.root / "resources" / "calendar-2026-12.md").read_text("utf-8")
    assert "- 2026-12-10 all day · DSA exam" in page
    assert "friend@" not in page and "secret agenda" not in page  # never attendees or notes
    assert watch.run(v)[0]["read"] == 0 and len(runs) == 1  # unchanged: a stat, never a read
    _cal(d, "uni.ics", ("a", "20261210", "DSA exam"), ("b", "20261215", "OS deadline"))
    r = watch.run(v)[0]
    assert r["read"] == 1 and "imported 1" in r["line"] and "duplicate 1" in r["line"]
    page = (v.root / "resources" / "calendar-2026-12.md").read_text("utf-8")
    assert page.count("DSA exam") == 1 and "- 2026-12-15 all day · OS deadline" in page
    assert watch.watched(v)[0]["last"]["line"] == r["line"]


def test_a_moved_calendar_event_keeps_its_old_line(root_plain, tmp_path):
    """The shortcut in importers.calendar_source: lines carry no UID, so an event moved to
    another day is a new line and its old one stays (a UID -> line store would fix it)."""
    v, d = Vault(root_plain), tmp_path / "Cal"
    _cal(d, "uni.ics", ("a", "20261210", "DSA exam"))
    watch.add(v, d, "calendar", "normal")
    watch.run(v)
    _cal(d, "uni.ics", ("a", "20261212", "DSA exam"))
    assert "imported 1" in watch.run(v)[0]["line"]
    page = (v.root / "resources" / "calendar-2026-12.md").read_text("utf-8")
    assert "- 2026-12-10 all day · DSA exam" in page and "- 2026-12-12 all day · DSA exam" in page


def test_cli_watches_a_calendar(root_plain, tmp_path):
    d = tmp_path / "Cal"
    _cal(d, "uni.ics", ("a", "20261210", "DSA exam"))
    out = runner.invoke(cli, ["watch", "add", str(d), "--source", "calendar", "--vault",
                              str(root_plain)]).output
    assert f"Watching {d.resolve()} as calendar" in out
