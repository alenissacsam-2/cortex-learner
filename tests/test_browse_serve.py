"""browse_serve: the receiver, now the background import (no port since 0.3.2, which removed
the capture extension). Tmp vaults only."""
import ast
import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortex import browse_serve as bs
from cortex.events import local_day

START = "2026-11-10T12:00:00Z"
DAY = local_day(datetime(2026, 11, 10, 12, tzinfo=UTC))


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    return v


def test_serve_runs_until_stopped_and_opens_no_port(vault):
    """The receiver is the background import only: no server, socket or token."""
    tree = ast.parse(Path(bs.__file__).read_text(encoding="utf-8"))
    mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not mods & {"socket", "http.server", "hmac", "secrets"}
    assert not any(hasattr(bs, x) for x in ("make_server", "pair", "PORT"))
    stop = threading.Event()
    t = threading.Thread(target=bs.serve, args=(vault,), kwargs={"stop": stop})
    t.start()
    stop.set()
    t.join(5)
    assert not t.is_alive()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _aged(monkeypatch, run, root):
    """A Processor whose import is due (old last import, one source imported before), with
    `run` as the import, on a hand-moved clock."""
    monkeypatch.setattr(bs, "auto_import_due", lambda v, now=None: True)
    monkeypatch.setattr(bs, "auto_sources", lambda v: ["comet/Default"])
    monkeypatch.setattr(bs, "run_import", run)
    clock, lines = Clock(), []
    p = bs.Processor(bs.Vault(root), clock=clock, say=lines.append)
    clock.t += bs.AUTO_FIRST_S
    return p, clock, lines


def test_one_run_at_a_time(monkeypatch, tmp_path):
    runs, started, release = [], threading.Event(), threading.Event()
    p, clock, _ = _aged(monkeypatch, lambda v, **kw: (runs.append(kw["srcs"]), started.set(),
                                                      release.wait(5)), tmp_path)
    t = threading.Thread(target=p.tick)
    t.start()
    assert started.wait(5)
    clock.t += bs.AUTO_EVERY_S  # the next check comes while the import runs
    assert not p.tick() and len(runs) == 1 and p.processing is True
    release.set()
    t.join(5)
    assert p.tick() and runs == [["comet/Default"]] * 2  # then it gets its own run


def test_a_failed_run_logs_no_text(monkeypatch, caplog, tmp_path):
    def boom(v, **kw):
        raise ValueError("https://secret.example/page Secret Title")

    p, _, lines = _aged(monkeypatch, boom, tmp_path)
    with caplog.at_level(logging.DEBUG, logger="cortex"):
        assert p.tick()
    assert "ValueError" in caplog.text and "secret" not in caplog.text.lower()
    assert p.processing is False and lines[-1] == "failed: ValueError"


def test_processing_prints_counts_only_progress(monkeypatch, tmp_path):
    def fake(v, progress=None, srcs=None):
        progress("comet/Default", 12, 5, None)
        return {"days_from": "2026-11-01", "private_written": 3, "kept": 9, "private": 2,
                "dropped": 1, "model": True}

    p, _, lines = _aged(monkeypatch, fake, tmp_path)
    assert p.tick()
    text = "\n".join(lines)
    assert "last import over 6 hours ago" in text
    assert "comet/Default: 12 visits read, 5 new" in text
    assert "3 private day pages written" in text and "local model used" in text
    assert p.last == lines[-1]


def test_run_import_is_the_whole_import_with_no_since(vault, home, monkeypatch):
    """Every browser found, no `since` (it would skip History days before it), no runner
    unless config enables the local LLM, and the embedder never downloaded."""
    from cortex import semantic
    from cortex.browse import build, sources

    got = {}
    monkeypatch.setattr(sources, "discover", lambda: ["a source"])
    monkeypatch.setattr(semantic, "load_embedder", lambda download=False: got.update(
        download=download))
    monkeypatch.setattr(build, "run", lambda v, srcs, settings, **kw: got.update(
        root=v.root, srcs=srcs, **kw) or {})
    bs.run_import(bs.Vault(vault))
    assert got["srcs"] == ["a source"] and got["root"] == vault and got["download"] is False
    assert got["runner"] is None and got["budget"] is None and "since" not in got


def test_run_import_still_imports_the_inbox_the_extension_left(vault, home, monkeypatch):
    """The extension is gone, but the visits it saved and no import read yet still import
    (with no browser chosen), and the file moves to the archive."""
    from cortex import semantic
    from cortex.browse import build, sources

    monkeypatch.setattr(sources, "discover", lambda *a, **k: [])
    monkeypatch.setattr(semantic, "load_embedder", lambda download=False: None)
    d = vault / ".cortex" / "browse"
    d.mkdir(parents=True)
    (d / f"inbox-{DAY}.jsonl").write_text(json.dumps({
        "url": "https://example.com/a", "title": "Fourier series notes", "start": START,
        "active_ms": 60_000, "excerpt": "Some text.", "kind": "page"}) + "\n", encoding="utf-8")
    bs.run_import(bs.Vault(vault), srcs=[])
    page = vault / "browse" / "private" / f"browse-private-{DAY}.md"
    assert "Fourier series notes" in page.read_text(encoding="utf-8")
    assert not (d / f"inbox-{DAY}.jsonl").exists()
    assert (vault / build.ARCHIVE / f"inbox-{DAY}.jsonl").is_file()


def _last_run(vault, when: datetime) -> None:
    from cortex.browse import build
    p = vault / build.STATE  # what a finished import leaves
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"last_run": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "sources": {
        "comet/Default": {"cursor": 1}}}), encoding="utf-8")


def test_receiver_imports_by_itself_when_the_last_import_is_old(vault, home, monkeypatch):
    """browse.auto_import_hours: due by the last import's age, first checked a minute after
    start then every 10 minutes; never before a first import, never with 0, never while the
    browse-wide lock is held (a forget or deepen). Only the profiles imported before."""
    from cortex.browse import build, sites, sources
    runs, lines = [], []
    monkeypatch.setattr(sources, "discover", lambda *a, **k: [
        sources.Source("chrome", "Default", vault / "a"),
        sources.Source("comet", "Default", vault / "b")])
    monkeypatch.setattr(bs, "run_import", lambda v, progress=None, srcs=None: runs.append(
        [s.key for s in srcs]) or {})
    monkeypatch.setattr(bs, "run_deepen", lambda v, say=None: None)  # no local model
    now = datetime(2026, 11, 19, 12, tzinfo=UTC)
    t = [0.0]
    p = bs.Processor(bs.Vault(vault), clock=lambda: t[0], say=lines.append, now=lambda: now)
    t[0] = bs.AUTO_FIRST_S - 1
    _last_run(vault, now - timedelta(hours=7))
    assert not p.tick() and not runs  # not before the first check
    (vault / build.STATE).unlink()
    t[0] = bs.AUTO_FIRST_S
    assert not p.tick() and not runs  # never imported: the first import is the user's
    _last_run(vault, now - timedelta(hours=7))
    t[0] += 1
    assert not p.tick()  # the next check is 10 minutes later
    t[0] += bs.AUTO_EVERY_S
    with sites.busy(bs.Vault(vault)):  # a forget or deepen holds the lock: not now
        assert not p.tick() and not runs
    t[0] += bs.AUTO_EVERY_S
    last = json.loads((vault / build.STATE).read_text(encoding="utf-8"))
    (vault / build.STATE).write_text(json.dumps({**last, "sources": {}}), encoding="utf-8")
    assert not p.tick() and not runs  # no profile imported before: none is read unasked
    _last_run(vault, now - timedelta(hours=7))
    t[0] += bs.AUTO_EVERY_S
    C = ["comet/Default"]  # chrome is found but was never ticked: it stays unread
    assert p.tick() and runs == [C]
    assert any("last import over 6 hours ago" in x for x in lines)
    _last_run(vault, now - timedelta(hours=5))  # fresh enough
    t[0] += bs.AUTO_EVERY_S
    assert not p.tick() and runs == [C]
    (home / ".cortex").mkdir(exist_ok=True)
    cfg = home / ".cortex" / "config.yaml"
    cfg.write_text("browse:\n  auto_import_hours: 4\n", encoding="utf-8")
    t[0] += bs.AUTO_EVERY_S
    assert p.tick() and runs == [C, C]
    cfg.write_text("browse:\n  auto_import_hours: 0\n", encoding="utf-8")
    t[0] += bs.AUTO_EVERY_S
    assert not p.tick() and runs == [C, C]


def test_auto_import_hours_falls_back_on_bad_values():
    assert bs.auto_import_hours({}) == 6
    assert bs.auto_import_hours({"browse": {"auto_import_hours": 12}}) == 12
    assert bs.auto_import_hours({"browse": {"auto_import_hours": 0}}) == 0
    for bad in (-1, 169, "6", True, float("nan")):
        assert bs.auto_import_hours({"browse": {"auto_import_hours": bad}}) == 6


def test_a_failing_check_never_ends_the_receivers_loop(tmp_path, monkeypatch):
    import threading

    from cortex import browse_serve
    from cortex.vault import Vault
    (tmp_path / ".cortex").mkdir()
    proc = browse_serve.Processor(Vault(tmp_path))
    calls = []

    def tick():
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("a profile folder")
        stop.set()
        return False
    stop = threading.Event()
    monkeypatch.setattr(proc, "tick", tick)
    proc.loop(stop, every=0.01)
    assert len(calls) == 2  # the error was logged and the loop went on


def test_auto_sources_is_empty_when_the_browsers_cannot_be_listed(tmp_path, monkeypatch):
    from cortex import browse_serve
    from cortex.browse import sources
    from cortex.vault import Vault
    (tmp_path / ".cortex").mkdir()

    def denied(*a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(sources, "chosen", denied)
    assert browse_serve.auto_sources(Vault(tmp_path)) == []
