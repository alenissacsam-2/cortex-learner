"""The browsing store (keepfold.browse.store) and the local model's title checks in batches
(keepfold.browse.titles), on synthetic History files and temp vaults only: an import reads only
what is new and saving twice changes nothing; a History file unchanged since the last run is
not even copied; titles go 25 to a call and each call's labels are saved at once; background
checks wait for an idle PC and an already loaded model and never load one; the site pages move
from Markdown into the store (zipped first, once); the read API never hands a hidden site to
Claude; a forget reaches the store; a backup holds a snapshot of it."""

import json
import math
import shutil
import sqlite3
import zipfile
from contextlib import closing
from dataclasses import replace
from datetime import UTC

import pytest
from e2e.fake_ollama import FakeOllama
from test_browse import LINK, Embed, chrome_raw, chromium, hubbed, m
from test_browse_sites import OWNER, ROWS
from test_browse_v7 import every_byte

from keepfold import backup, browse, cli, localllm
from keepfold.app.server import Job
from keepfold.browse import build, forget, sites, sources, store, titles
from keepfold.events import local_day
from keepfold.vault import Vault

HISTORY = ("Google", "Chrome", "User Data", "Default", "History")
HIDDEN_WORDS = ("pornhub", "adulttitle", "adultterm", "adultvid")


@pytest.fixture
def vault(root):
    return hubbed(root)


@pytest.fixture
def hist(tmp_path):
    local = tmp_path / "local"
    chromium(local.joinpath(*HISTORY), ROWS)
    return local, tmp_path / "roaming"


def imp(r, hist, **kw):
    kw.setdefault("embed", Embed())
    return build.run(Vault(r), sources.discover(*hist), OWNER, tz=UTC, now=m(0, 2), **kw)


def rows(r) -> int:
    with closing(store.connect(Vault(r))) as con:
        return con.execute("SELECT COUNT(*) FROM visits").fetchone()[0]


def browse_files(r) -> dict:
    return {p.relative_to(r).as_posix(): p.read_bytes() for p in (r / "browse").rglob("*")
            if p.is_file()}


def add_visit(hist, t, url, title):
    with closing(sqlite3.connect(hist[0].joinpath(*HISTORY))) as con, con:
        uid = con.execute("INSERT INTO urls(url, title) VALUES (?, ?)", (url, title)).lastrowid
        con.execute("INSERT INTO visits(url, visit_time, visit_duration, transition) "
                    "VALUES (?, ?, 0, ?)", (uid, chrome_raw(t), LINK))


def local_runner(stop_after: int | None = None):
    """A real `localllm.Runner` (the import only asks one of its own type) with its model calls
    stubbed: every text "ordinary"; KeyboardInterrupt on call `stop_after` + 1."""
    r = localllm.Runner("ollama", "http://127.0.0.1:9", "fake")
    r.batches = []

    def classify_many(texts, labels, *, timeout=60, budget=None):
        if stop_after is not None and len(r.batches) == stop_after:
            raise KeyboardInterrupt
        r.batches.append(list(texts))
        return dict.fromkeys(range(len(texts)), "ordinary")

    r.classify_many = classify_many
    r.classify = lambda text, labels, **k: (r.batches.append([text]), ("ordinary", 0.9))[1]
    r.summarise = lambda *a, **k: None
    return r


# ---- only what is new ---------------------------------------------------------------------

def test_saving_twice_changes_nothing_and_a_run_reads_only_what_is_new(vault, hist):
    first = imp(vault, hist)
    n = rows(vault)
    assert first["visits_new"] == n > 0
    pages = browse_files(vault)
    again = imp(vault, hist)
    assert not again.get("visits_new") and rows(vault) == n
    assert browse_files(vault) == pages  # no day was dirty: no page made again
    add_visit(hist, m(80), "https://lichess.org/practice", "Chess practice")
    rep = imp(vault, hist)
    assert rep["visits_new"] == 1 and rows(vault) == n + 1


def test_an_unchanged_history_file_is_not_even_copied(vault, hist, monkeypatch):
    imp(vault, hist)
    copied = []
    real = sources._copy
    monkeypatch.setattr(sources, "_copy", lambda p: (copied.append(p), real(p))[1])
    rep = imp(vault, hist)
    assert copied == [] and rep["sources"] == 1 and not rep.get("sources_failed")
    add_visit(hist, m(80), "https://lichess.org/practice", "Chess practice")
    assert imp(vault, hist)["visits_new"] == 1 and len(copied) == 1


# ---- bounded memory: chunks of visits, runs of days ---------------------------------------

def test_a_history_is_read_a_chunk_at_a_time_and_the_chunks_change_nothing(hist):
    """However long the history, a source is held a chunk at a time; a visit's time on the
    page is still measured to the next visit across a chunk's edge."""
    src = sources.discover(*hist)[0]
    whole, top_t, top_id = sources.read_since(src)
    with sources.Since(src) as rd:
        chunks = list(rd.chunks(3))
    assert rd.total == len(ROWS) and {len(c) for c in chunks[:-1]} == {3}
    assert [v for c in chunks for v in c] == whole and len(whole) == len(ROWS)
    assert (rd.top_time, rd.top_id) == (top_t, top_id) and top_id == len(ROWS)


def test_small_chunks_and_runs_of_days_make_the_same_store_and_pages(vault, hist, tmp_path,
                                                                      monkeypatch):
    for day in (1, 2, 3):
        add_visit(hist, m(10, day), "https://lichess.org/practice", "Chess practice")
        add_visit(hist, m(20, day), "https://www.chess.com/lessons/2", "Chess lesson two")
    other = tmp_path / "other"
    shutil.copytree(vault, other)
    imp(vault, hist)
    asked: list[int] = []
    real = store.items
    monkeypatch.setattr(store, "CHUNK", 2)
    monkeypatch.setattr(build, "RENDER_VISITS", 2)
    monkeypatch.setattr(store, "items", lambda con, days, s: (asked.append(len(days)),
                                                             real(con, days, s))[1])
    imp(other, hist)
    assert set(asked) == {1}  # one day's visits at a time
    assert browse_files(other) == browse_files(vault)
    q = "SELECT source, native, ts, day, url, title, secs FROM visits ORDER BY native"
    with closing(store.connect(Vault(vault))) as a, closing(store.connect(Vault(other))) as b:
        assert a.execute(q).fetchall() == b.execute(q).fetchall()
        # a first import into an empty store makes the indexes after its rows
        assert {n for (n,) in b.execute("SELECT name FROM sqlite_master WHERE type = 'index' "
                                        "AND tbl_name = 'visits' AND sql IS NOT NULL")} \
            == {"visits_day", "visits_tkey"}


def test_a_stop_mid_read_keeps_each_saved_chunk_and_the_next_run_goes_on_from_it(
        vault, hist, tmp_path, monkeypatch):
    whole = tmp_path / "whole"
    shutil.copytree(vault, whole)
    imp(whole, hist)
    monkeypatch.setattr(store, "CHUNK", 2)
    saved: list[int] = []

    def stop(n, done, total):
        if n == 2 and done < total:
            saved.append(done)
            if len(saved) == 2:
                raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        imp(vault, hist, step=stop)
    assert 0 < rows(vault) < rows(whole)
    opened: list = []
    real = sources.Since.__enter__
    monkeypatch.setattr(sources.Since, "__enter__",
                        lambda self: (opened.append(self), real(self))[1])
    imp(vault, hist)
    assert opened[0].total < len(ROWS)  # from the saved chunks' cursor on, not the whole file
    assert rows(vault) == rows(whole) and browse_files(vault) == browse_files(whole)


def test_a_stopped_refilter_goes_on_from_the_days_left_not_from_the_oldest(
        vault, hist, tmp_path, monkeypatch):
    """A settings change makes the next import filter everything again and redo every day; a
    stop in the middle keeps the days already made, so the next run does only the rest."""
    for day in (1, 2, 3):
        add_visit(hist, m(10, day), "https://lichess.org/practice", "Chess practice")
    changed = replace(OWNER, deny=("medium.com",))

    def run(r, settings=changed, **kw):
        return build.run(Vault(r), sources.discover(*hist), settings, embed=Embed(), tz=UTC,
                         now=m(0, 5), **kw)
    run(vault, OWNER)
    whole = tmp_path / "whole"
    shutil.copytree(vault, whole)
    run(whole)
    monkeypatch.setattr(build, "RENDER_VISITS", 1)  # one day per run of days

    def stop(n, done, total):
        if n == 4 and done == 2:  # the newest day saved, the next one written, not saved
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        run(vault, step=stop)
    totals: dict = {}
    run(vault, step=lambda n, done, total: totals.setdefault(n, total))
    assert totals[4] == 3  # not all 4 days again
    assert browse_files(vault) == browse_files(whole)


def legacy(r, tmp_path, *, stop_after: int | None = None, old_build: bool = False):
    """A vault imported before the store (pages, site-days.json, cursors in a state with no
    sites_stamp) with two Chrome profiles, then the app's import with only Default ticked
    (stopped after `stop_after` saved chunks, then run again; `old_build`: the stopped run
    left no "legacy" meta, as a build before it did). Day 0's private page before and
    after."""
    from keepfold import browse_serve, semantic
    from keepfold.app import actions

    ud = tmp_path / "local" / "Google" / "Chrome" / "User Data"
    chromium(ud / "Default" / "History", ROWS)
    chromium(ud / "Profile 1" / "History",
             [(m(80), "https://www.chess.com/lessons/9", "Chess lesson nine", 300)])
    found = sources.discover(tmp_path / "local", tmp_path / "roaming")
    v = Vault(r)
    build.run(v, found, OWNER, embed=Embed())
    sites._save(v, sites.DAYS, sites.load_days(v)[0])  # the counts as they were kept then
    for p in (r / ".keepfold" / "browse").glob("store.db*"):
        p.unlink()
    state = build.load_state(v)
    del state["sites_stamp"]
    (r / build.STATE).write_text(json.dumps(state), encoding="utf-8")
    page = r / "browse" / "private" / f"browse-private-{local_day(m(0), None)}.md"
    before = page.read_bytes()
    add_visit((tmp_path / "local",), m(10, 1), "https://lichess.org/practice", "Chess practice")
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(semantic, "load_embedder", lambda *a, **k: Embed())
        mp.setattr(browse_serve, "idle_runner", lambda *a, **k: (None, None))
        mp.setattr(browse, "load_settings", lambda *a, **k: OWNER)
        mp.setattr(sources, "discover", lambda *a, **k: list(found))
        if stop_after is not None:
            mp.setattr(store, "CHUNK", 2)
            job = Job("import", "browse", True)
            real = job.progress

            def progress(step, steps, done, total, label):
                if label == build.PHASES[1] and done >= 2 * stop_after:
                    job.cancelled.set()
                real(step, steps, done, total, label)
            job.progress = progress
            assert actions.history_import(job, r, {"chrome/Default"})["cancelled"]
            assert 0 < rows(r) < len(ROWS)
            if old_build:
                with closing(store.connect(v)) as con, con:
                    con.execute("DELETE FROM meta WHERE k = 'legacy'")
        assert actions.history_import(Job("import", "browse", True), r,
                                      {"chrome/Default"})["ok"]
    finally:
        mp.undo()
    return before, page.read_bytes()


def test_the_app_s_first_import_into_the_store_keeps_the_days_made_before_it(vault, tmp_path):
    """The app moves the site pages into the store before the import, which then reads every
    visit once; the days up to the old cursor keep their pages (an unticked profile's visits
    too) and only the new day is made."""
    before, after = legacy(vault, tmp_path)
    assert b"lesson nine" in before.lower()
    assert after == before


def test_a_stopped_first_import_into_the_store_still_keeps_the_days_made_before_it(
        vault, tmp_path):
    before, after = legacy(vault, tmp_path, stop_after=1)
    assert after == before


def test_a_first_import_an_older_build_stopped_keeps_the_days_it_had_not_read_yet(
        vault, tmp_path):
    before, after = legacy(vault, tmp_path, stop_after=1, old_build=True)
    assert after == before


def test_the_first_read_into_the_store_keeps_a_partly_expired_oldest_day(vault, hist):
    """The browser expired the first hours of the oldest day since the old import: that day
    is partial in the store, so no later run makes its page again from what is left."""
    add_visit(hist, m(10, 1), "https://lichess.org/practice", "Chess practice")
    v = Vault(vault)
    imp(vault, hist)
    sites._save(v, sites.DAYS, sites.load_days(v)[0])
    for p in (vault / ".keepfold" / "browse").glob("store.db*"):
        p.unlink()
    with closing(sqlite3.connect(hist[0].joinpath(*HISTORY))) as con, con:
        con.execute("DELETE FROM visits WHERE id <= 3")
    # day 0 is being expired by now
    build.run(v, sources.discover(*hist), OWNER, embed=Embed(), tz=UTC, now=m(0, 100))
    with closing(store.connect(v)) as con:
        assert store.partial(con) == {"2026-11-09"}
        assert store.get_meta(con, "legacy") == {"cursors": {}, "archive": False}


# ---- titles in batches --------------------------------------------------------------------

class Batches:
    """A stub runner: "ordinary" for every line, except the texts in `drop` and, when `empty`,
    the first call's whole reply (no usable JSON)."""

    def __init__(self, drop=(), empty=False):
        self.calls, self.drop, self.empty = [], set(drop), empty

    def classify_many(self, texts, labels, *, budget=None):
        self.calls.append(len(texts))
        if self.empty and len(self.calls) == 1:
            return {}
        return {i: "ordinary" for i, t in enumerate(texts) if t not in self.drop}

    def classify(self, text, labels, *, budget=None):
        self.calls.append(1)
        return None if text in self.drop else ("ordinary", 0.9)


TEXTS = {f"h{i}": f"title {i}" for i in range(60)}


def test_titles_go_25_to_a_call():
    r = Batches()
    got, c = titles.check(TEXTS, r, ["ordinary"])
    assert r.calls == [25, 25, 10] and c["calls"] == math.ceil(60 / titles.BATCH)
    assert len(got) == c["asked"] == c["answered"] == 60


def test_a_short_reply_is_asked_again_in_halves_not_the_whole_batch():
    r = Batches(drop={"title 3"})
    got, c = titles.check(TEXTS, r, ["ordinary"])
    assert r.calls == [25, 1, 25, 10] and "h3" not in got and len(got) == 59
    r = Batches(empty=True)
    got, _ = titles.check(TEXTS, r, ["ordinary"])
    assert r.calls == [25, 13, 12, 25, 10] and len(got) == 60


def test_each_call_is_saved_so_a_stop_keeps_it():
    saved: dict = {}
    gate = iter([True, False])  # the owner comes back after the first call
    got, c = titles.check(TEXTS, Batches(), ["ordinary"], save=saved.update,
                          gate=lambda: next(gate))
    assert len(saved) == 25 and saved == got and c["paused"] and c["left"] == 35


def test_an_import_stopped_mid_check_keeps_its_labels_and_never_asks_them_again(
        vault, hist, monkeypatch):
    monkeypatch.setattr(titles, "BATCH", 2)
    stopped = local_runner(stop_after=1)
    with pytest.raises(KeyboardInterrupt):
        imp(vault, hist, runner=stopped)
    assert len(stopped.batches) == 1 and len(store.verdicts(Vault(vault))) == 2
    again = local_runner()
    imp(vault, hist, runner=again)
    asked = [t for b in again.batches for t in b]
    assert asked and not set(asked) & set(stopped.batches[0])


def test_background_checks_wait_for_an_idle_pc_and_a_loaded_model_and_never_load_one(
        monkeypatch):
    monkeypatch.setattr(localllm, "_PROBES", [])  # never the real 127.0.0.1:11434
    monkeypatch.setattr(localllm, "_lms_exe", lambda cfg: None)
    monkeypatch.setattr(titles, "full_screen", lambda: False)
    with FakeOllama() as f:
        cli._write_config(localllm={"enabled": True, "url": f.url, "model": "qwen3.5:2b"})
        monkeypatch.setattr(titles, "idle_seconds", lambda: 5.0)
        assert titles.may_run() == (None, "waiting until the PC is idle")
        assert f.requests == []  # not even asked while the owner is at the PC
        monkeypatch.setattr(titles, "idle_seconds", lambda: 600.0)
        assert titles.may_run() == (None, "the local model is not loaded")
        f.loaded = ["qwen3.5:2b"]
        r, why = titles.may_run()
        assert r is not None and r.model == "qwen3.5:2b" and why is None
        # only GETs: the model list and what is in memory; no chat, no load, no pull
        assert {q["path"] for q in f.requests} <= {"/api/tags", "/api/ps"}
        assert all(q["body"] is None for q in f.requests)


# ---- steps and progress -------------------------------------------------------------------

def test_the_phases_come_in_order_and_a_job_s_share_never_goes_back(vault, hist):
    seen: list = []
    imp(vault, hist, runner=local_runner(), step=lambda n, done, total: seen.append(n))
    assert seen == sorted(seen) and set(seen) == {1, 2, 3, 4, 5}
    j = Job("Import past history", "browse", True)
    j.progress(2, 5, 9, 10, build.PHASES[1])
    j.progress(1, 5, 0, 10, build.PHASES[0])  # a late line from an earlier step
    p = j.view()["progress"]
    assert (p["step"], p["steps"], p["label"], p["overall"]) == (1, 5, build.PHASES[0], 38)


# ---- the site pages in the store ----------------------------------------------------------

def test_migrate_zips_the_site_pages_moves_them_into_the_store_once(vault, hist):
    v = Vault(vault)
    imp(vault, hist)  # a vault from before the store: the site Markdown pages
    pages = {p.relative_to(vault).as_posix(): p.read_bytes() for p in store._site_page_files(v)}
    assert pages and not store.migrated(v)
    dry = store.migrate(v, settings=OWNER, dry_run=True)
    assert dry["files"] == len(pages) and dry["missing"] == 0 and dry["zip"] is None
    assert all((vault / p).is_file() for p in pages) and not store.migrated(v)
    rep = store.migrate(v, settings=OWNER, today="2026-11-20")
    assert rep["missing"] == 0 and rep["removed"] == len(pages) and store.migrated(v)
    assert not store._site_page_files(v)
    with zipfile.ZipFile(vault / rep["zip"]) as z:
        assert {n: z.read(n) for n in z.namelist()} == pages  # every page, byte for byte
    assert {"lichess.org", "pornhub.com"} <= {r["site"] for r in
                                               store.site_rows(v, include_private=True)}
    again = store.migrate(v, settings=OWNER)
    assert again["already"] and again["zip"] is None and again["removed"] == 0
    add_visit(hist, m(80), "https://lichess.org/practice", "Chess practice")
    imp(vault, hist)
    assert not store._site_page_files(v)  # from now on the site pages live in the store only


def test_the_read_api_never_hands_a_hidden_site_to_claude(vault, hist):
    v = Vault(vault)
    store.migrate(v, settings=OWNER)  # a new vault: no page to move, only marks it done
    imp(vault, hist)
    assert not store._site_page_files(v)
    shown = store.site_rows(v)
    assert shown and all(not r["hidden"] and r["audience"] == "cloud" for r in shown)
    blob = json.dumps(shown).lower()
    assert not [w for w in HIDDEN_WORDS if w in blob]
    mine = store.site_rows(v, include_private=True)
    assert any(r["site"] == "pornhub.com" and r["hidden"] and r["audience"] == "local"
               for r in mine)
    assert store.site_rows(v, since="2026-11-10") == []  # every visit was on the 9th


def test_a_forget_takes_the_rows_out_of_the_store(vault, hist):
    v = Vault(vault)
    store.migrate(v, settings=OWNER)
    imp(vault, hist)
    assert b"healthmarker" in every_byte(vault)
    forget.purge(v, forget.Rule(hosts=("webmd.com",)), settings=OWNER)
    assert b"healthmarker" not in every_byte(vault)  # the store's file and its WAL too
    assert "webmd.com" not in {r["site"] for r in store.site_rows(v, include_private=True)}


def test_a_backup_holds_a_whole_snapshot_of_the_store(vault, hist, tmp_path):
    imp(vault, hist)
    with zipfile.ZipFile(backup.backup(vault, tmp_path / "out")) as z:
        names = z.namelist()
        data = z.read(store.PATH)
    assert not [n for n in names if n.endswith(("store.db-wal", "store.db-shm"))]
    (tmp_path / "copy.db").write_bytes(data)
    with closing(sqlite3.connect(tmp_path / "copy.db")) as con:
        assert con.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == rows(vault)
