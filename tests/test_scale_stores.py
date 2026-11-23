"""The stores that keep big data paths light: the SQLite vector stores (cortex.vecstore), the
site counts in the browsing store and its privacy memo, and background embedding."""

import json
import sys
import threading
import time
from collections import defaultdict
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest
from conftest import FakeEmbed, note
from test_browse_sites import OWNER, ROWS, days_text, imp
from test_private_search import pages as browse_pages

from cortex import semantic, vecstore
from cortex.browse import build, forget, legacy, privacy, sites, sources, store, vectors
from cortex.catalog import Catalog
from cortex.index import Index
from cortex.vault import Vault, VaultError

REAL_LOAD = semantic.load_embedder  # before the autouse fixture wraps it

pytestmark = pytest.mark.usefixtures("home")


def index(root):
    v = Vault(root)
    return Index(v, Catalog.load(v))


def on_disk(path) -> bytes:
    return b"".join(p.read_bytes() for p in path.parent.glob(path.name + "*"))


# ---- cortex.vecstore -----------------------------------------------------------------------


def test_upsert_delete_and_a_change_stamp_seen_by_another_connection(tmp_path):
    p = tmp_path / "v.db"
    a, b = vecstore.connect(p), vecstore.connect(p)
    v = np.eye(4, dtype=np.float32)
    try:
        vecstore.prepare(a, {"dim": 4})
        vecstore.put(a, {"x": ("MANIFESTX", False, np.array([0], np.int32), v[:1])})
        cache = {}
        st, moved = vecstore.sync(b, 4, None, cache, str)
        assert moved and set(cache) == {"x"} and not cache["x"][1]
        # more chunks of the same manifest win; fewer never undo them
        vecstore.put(a, {"x": ("MANIFESTX", True, np.array([0, 9], np.int32), v[:2])})
        vecstore.put(a, {"x": ("MANIFESTX", False, np.array([0], np.int32), v[:1])})
        st2, moved = vecstore.sync(b, 4, st, cache, str)
        assert moved and cache["x"][1] and cache["x"][2].tolist() == [0, 9]
        assert vecstore.sync(b, 4, st2, cache, str) == (st2, False)  # nothing new: no read
        vecstore.put(a, {"y": ("MANIFESTY", True, np.array([0], np.int32), v[2:3])})
        assert vecstore.delete(a, ["x", "gone"]) == 1
        vecstore.sync(b, 4, st2, cache, str)
        assert set(cache) == {"y"}
        ids, page, start, m = vecstore.stack(cache, 4)
        assert ids == ["y"] and m.flags.c_contiguous and np.shares_memory(cache["y"][3], m)
        assert np.allclose(m, v[2:3]) and page.tolist() == [0] and start.tolist() == [0]
    finally:
        a.close()
        b.close()
    assert b"MANIFESTX" not in on_disk(p)  # a deleted row is overwritten, the WAL emptied


# ---- the vault's vectors (cortex.semantic) -------------------------------------------------


def test_the_npz_cache_is_read_in_once_and_kept(root):
    idx = index(root)
    semantic.vectors(idx, FakeEmbed(), 4)
    cache = semantic._read(root)
    semantic._npz_write(Vault(root), cache)  # the cache as it was before the store
    for s in vecstore.SUFFIXES:
        (root / f"{semantic.CACHE}{s}").unlink(missing_ok=True)
    semantic._MEM.clear()
    npz = (root / semantic.NPZ).read_bytes()
    fake = FakeEmbed()
    ids = semantic.vectors(idx, fake, 4)[0]
    assert fake.texts == [] and set(ids) == set(cache)  # nothing embedded again
    for _ in range(2):  # idempotent
        semantic._connect(root, 4).close()
        got = semantic._read(root)
        assert got.keys() == cache.keys()
        for i, e in got.items():
            assert e[0] == cache[i][0] and np.array_equal(e[2], cache[i][2])
            assert np.allclose(e[3], cache[i][3], atol=1e-3)
    assert (root / semantic.NPZ).read_bytes() == npz  # kept as a backup


def test_another_process_save_is_seen_through_the_stamp(root, monkeypatch):
    idx = index(root)
    monkeypatch.setattr(semantic, "_MEM", {})  # process A
    semantic.vectors(idx, FakeEmbed(), 4)
    a = semantic._MEM
    monkeypatch.setattr(semantic, "_MEM", {})  # process B adds a page
    note(root, "notes/zebra.md", "title: Zebra\n", "# Zebra\n\nA motor car.")
    idx_b = index(root)
    semantic.vectors(idx_b, FakeEmbed(), 4)
    monkeypatch.setattr(semantic, "_MEM", a)  # back in A, whose index sees the page too
    fake = FakeEmbed()
    ids = semantic.vectors(index(root), fake, 4)[0]
    assert "zebra" in ids and fake.texts == []  # read from the store, not embedded again


def test_private_browse_vectors_never_reach_the_public_store(root, monkeypatch):
    browse_pages(root)
    fake = FakeEmbed()
    monkeypatch.setattr(semantic, "_embedder", fake)
    vectors.update(Vault(root))
    semantic.vectors(index(root), fake, 4)
    with closing(vecstore.connect(root / semantic.CACHE, create=False)) as con:
        public = vecstore.manifests(con)
    assert public and not any("browse/private" in m or i.startswith("browse-private")
                              for i, m in public.items())
    private = set(vectors._read(Vault(root)))
    assert "browse/private/browse-private-2026-11-09.md" in private
    assert not private & {json.loads(m)[0] for m in public.values()} - {
        "browse/browse-2026-11-09.md"}
    assert vectors.CACHE != semantic.CACHE


def test_the_private_npz_is_migrated_once_and_renamed(root, monkeypatch):
    """F3: only `legacy.migrate` reads the .npz; it is then renamed, never deleted, and a
    forget still prunes the renamed backup."""
    browse_pages(root)
    fake = FakeEmbed()
    monkeypatch.setattr(semantic, "_embedder", fake)
    v = Vault(root)
    vectors.update(v)
    old, sig = vectors._read(v), vectors._stored_signature(v)
    vectors._npz_write(v, old, 4, sig)
    npz = (root / vectors.NPZ).read_bytes()
    for s in vecstore.SUFFIXES:
        (root / f"{vectors.CACHE}{s}").unlink(missing_ok=True)
    vectors._MEM.clear()
    assert legacy.pending(v) == [vectors.NPZ]
    assert vectors._read(v) == {}  # the store never reads the .npz itself
    assert legacy.migrate(v) == {vectors.NPZ: "migrated"}
    kept = root / (vectors.NPZ + legacy.DONE)
    assert not (root / vectors.NPZ).exists() and kept.read_bytes() == npz
    assert legacy.pending(v) == [] and legacy.migrate(v) == {}  # once
    fake.texts.clear()
    assert vectors.update(v)["embedded"] == 0
    assert vectors._read(v).keys() == old.keys() and vectors._stored_signature(v) == sig
    gone = sorted(old)[0]
    (root / gone).unlink()
    vectors.prune(v)
    assert gone not in vectors._npz(v, rel=vectors.NPZ + legacy.DONE)[0]


def test_the_app_start_migrates_the_old_browse_files(root):
    """F3: the app's start reads a legacy file in and renames it, before any import."""
    from cortex.app import server
    (root / ".cortex").mkdir(exist_ok=True)
    p = root / sites.DAYS
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(DAYS), encoding="utf-8")
    srv, app, _ = server.start(root, idle=False, echo=lambda s: None)
    try:
        assert not p.exists() and (root / (sites.DAYS + legacy.DONE)).is_file()
        assert sites.load_days(Vault(root))[0] == DAYS
    finally:
        app.stop.set()
        server.finish(srv, app)


def test_a_store_migrated_by_an_earlier_version_only_renames_the_old_files(root):
    """The site counts read in already (`days_migrated`): the file is renamed, not read
    again; verdicts.json goes into the store under the store's own labels."""
    v = Vault(root)
    p = root / sites.DAYS
    p.parent.mkdir(parents=True, exist_ok=True)
    with closing(store.connect(v)) as con:
        store.migrate_days(con, {}, [])
        store.put_verdicts(con, {"h1": "health"})
    p.write_text(json.dumps(DAYS), encoding="utf-8")
    (root / sites.VERDICTS).write_text(json.dumps({"h1": "ordinary", "h2": "money"}),
                                       encoding="utf-8")
    assert legacy.migrate(v) == {sites.DAYS: "migrated", sites.VERDICTS: "migrated"}
    assert sites.load_days(v)[0] == {}
    assert sites.load_verdicts(v) == {"h1": "health", "h2": "money"}
    assert (root / (sites.VERDICTS + legacy.DONE)).is_file()


# ---- background embedding --------------------------------------------------------------------


def test_background_embedding_uses_a_capped_model_at_low_priority(home, monkeypatch):
    import fastembed

    arenas = []

    class Model:
        def __init__(self, name, cache_dir=None, local_files_only=True, threads=None,
                     enable_cpu_mem_arena=True):
            self.threads = threads
            arenas.append(enable_cpu_mem_arena)

        def embed(self, texts):
            return [np.full(4, self.threads or 0, np.float32) for _ in texts]

    monkeypatch.setattr(fastembed, "TextEmbedding", Model)
    semantic.models_dir().mkdir(parents=True)
    embed = REAL_LOAD()
    assert embed(["x"])[0][0] == 0  # the full model
    got = {}
    k = semantic._kernel32() if sys.platform == "win32" else None
    before = k.GetThreadPriority(k.GetCurrentThread()) if k else None
    with semantic.background():
        got["bg"] = embed(["x"])[0][0]
        if k:
            got["prio"] = k.GetThreadPriority(k.GetCurrentThread())
        t = threading.Thread(target=lambda: got.update(other=embed(["x"])[0][0]))
        t.start()
        t.join()
    assert got["bg"] == semantic.BG_THREADS  # capped at about half the cores
    assert got["other"] == 0  # a query in another thread is never slowed
    assert embed(["x"])[0][0] == 0
    if k:
        assert got["prio"] == -1 and k.GetThreadPriority(k.GetCurrentThread()) == before
    # neither copy keeps onnxruntime's CPU arena: it would hold the largest batch's peak
    assert arenas == [False, False]


class Counted:
    """A fake fastembed model that counts its loads; `gate`, when set, holds every embed."""

    loads = 0
    gate: threading.Event | None = None
    inside = threading.Event()

    def __init__(self, threads=None):
        type(self).loads += 1

    def embed(self, texts):
        self.inside.set()
        if self.gate is not None:
            self.gate.wait(10)
        return [np.ones(4, np.float32) for _ in texts]


@pytest.fixture
def counted(monkeypatch):
    monkeypatch.setattr(Counted, "loads", 0)
    monkeypatch.setattr(Counted, "gate", None)
    monkeypatch.setattr(semantic, "_text_embedding",
                        lambda download=False, threads=None: Counted(threads))
    semantic.models_dir().mkdir(parents=True)
    return REAL_LOAD()


def test_idle_models_are_dropped_and_load_again_on_the_next_call(counted):
    embed = counted
    with semantic.background():
        embed(["x"])  # the capped copy
        assert semantic._capped.model is not None
    assert semantic._capped.model is None  # M3: dropped when the background work ended
    embed(["x"])
    assert Counted.loads == 2
    soon = time.monotonic() + semantic.IDLE_UNLOAD - 30
    assert semantic.drop_idle(soon) == []  # used within IDLE_UNLOAD: kept
    later = time.monotonic() + semantic.IDLE_UNLOAD + 1
    assert semantic.drop_idle(later) == ["embedder"]
    assert semantic._main.model is None
    assert REAL_LOAD() is embed  # still set up: callers keep the same function
    assert embed(["x", "y"]).shape == (2, 4) and Counted.loads == 3  # loaded again
    assert semantic.drop_idle() == []  # just used
    with semantic.background():  # a long background run: its copy goes when idle too
        embed(["x"])
        later = time.monotonic() + semantic.IDLE_UNLOAD + 1
        assert semantic.drop_idle(later) == ["embedder", "background copy"]
    assert Counted.loads == 4


def test_a_model_in_use_is_never_dropped(counted):
    embed = counted
    embed(["warm"])
    Counted.inside.clear()
    Counted.gate = threading.Event()
    t = threading.Thread(target=lambda: embed(["slow"]))
    t.start()
    assert Counted.inside.wait(10)
    try:
        assert semantic.drop_idle(time.monotonic() + 10 * semantic.IDLE_UNLOAD) == []
    finally:
        Counted.gate.set()
        t.join(10)
    assert semantic.drop_idle(time.monotonic() + semantic.IDLE_UNLOAD + 1) == ["embedder"]
    assert Counted.loads == 1


def _model_files():
    (semantic.models_dir() / "models--Qdrant--bge-small-en-v1.5-onnx-Q").mkdir(parents=True)


def test_background_use_never_restarts_the_idle_clock(counted, monkeypatch):
    """M3: only a person's use keeps the main model; the safety net's embedder and the full
    model doing background work (no capped copy) leave its idle clock where it was."""
    embed = counted
    embed(["a person's recall"])
    used = semantic._main.used
    quiet = semantic.loaded_embedder()
    assert quiet is not None and quiet(["the safety net"]).shape == (1, 4)
    monkeypatch.setattr(semantic, "_capped", semantic._Held(lambda: False))  # copy failed
    with semantic.background():
        assert embed(["the browse import"]).shape == (1, 4)
    assert semantic._main.used == used and Counted.loads == 1
    assert semantic.drop_idle(used + semantic.IDLE_UNLOAD + 1) == ["embedder"]
    assert semantic.loaded_embedder() is None  # dropped: the net never loads it again
    with semantic.background():  # background work that had to load it: gone when the
        embed(["the browse import"])  # person's clock runs out, not IDLE_UNLOAD after this
    assert Counted.loads == 2
    assert semantic.drop_idle(used + semantic.IDLE_UNLOAD + 1) == ["embedder"]
    embed(["the next recall"])  # a person's use loads it and restarts the clock
    assert Counted.loads == 3 and semantic.drop_idle(time.monotonic() + 1) == []


def test_background_import_setup_loads_no_model(home, root, monkeypatch):
    """M3: inside background(), load_embedder loads nothing (the browse import asks for it
    every run); the first embed loads only the capped copy, which goes when the work ends,
    and `_embedder` (prime and review's "already loaded") stays unset."""
    from cortex import browse_serve

    monkeypatch.setattr(semantic, "load_embedder", REAL_LOAD)
    monkeypatch.setattr(Counted, "loads", 0)
    monkeypatch.setattr(semantic, "_text_embedding",
                        lambda download=False, threads=None: Counted(threads))
    semantic.models_dir().mkdir(parents=True)
    with semantic.background():  # a models folder without the search model: not set up
        assert semantic.load_embedder() is None
    _model_files()
    got = {}

    def run(v, srcs, settings, embed=None, **kw):
        got["loads"] = Counted.loads
        got["shape"] = embed(["a visit's title"]).shape
        got["loaded"] = (semantic._main.model, semantic._capped.model is not None)
        return {}

    monkeypatch.setattr(build, "run", run)
    monkeypatch.setattr(sites, "update_vectors", lambda *a, **k: None)
    trims = []
    monkeypatch.setattr(semantic, "_trim", lambda: trims.append(1))
    browse_serve.run_import(Vault(root), srcs=[])
    assert got == {"loads": 0, "shape": (1, 4), "loaded": (None, True)}
    assert Counted.loads == 1 and semantic._capped.model is None  # dropped as the run ended
    assert trims == [1]  # no model left: the working set emptied too
    assert semantic._main.model is None and semantic._embedder is None
    from cortex import safety_net
    assert safety_net.background_tools()[1] is None  # nothing in memory: the net uses none


# ---- site counts in the browsing store ----------------------------------------------------


DAYS = {"2026-11-09": {"lichess.org|gaming|0": {
    "n": 2, "s": 60, "w": {"chrome": 2}, "p": {"https://lichess.org/a": ["Chess", 2, 60]}}}}


def test_site_days_json_is_migrated_once_and_renamed(root):
    from cortex import doctor
    v = Vault(root)
    p = root / sites.DAYS
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(DAYS), encoding="utf-8")
    text = p.read_text(encoding="utf-8")
    assert sites.load_days(v)[0] == {} and sites.lean_days(v) == {}  # no reader of the file
    assert doctor.check_browse_legacy(root)[0] == "WARN"
    assert legacy.migrate(v) == {sites.DAYS: "migrated"}
    assert doctor.check_browse_legacy(root)[0] == "PASS"
    gen = sites.days_gen(v)
    assert sites.load_days(v)[0] == DAYS and gen
    assert not p.exists() and (root / (sites.DAYS + legacy.DONE)).read_text(
        encoding="utf-8") == text  # renamed, never deleted
    p.write_text(json.dumps({**DAYS, "2026-11-10": DAYS["2026-11-09"]}), encoding="utf-8")
    with pytest.raises(VaultError):  # a second file, but the backup name holds other bytes
        v.move(p, root / (sites.DAYS + legacy.DONE))
    assert legacy.migrate(v) == {sites.DAYS: "failed"} and sites.days_gen(v) == gen  # once
    assert sites.stored_days(v) == {"2026-11-09"} and p.is_file()  # left for doctor
    assert sites.lean_days(v)["2026-11-09"]["lichess.org|gaming|0"] == {
        "n": 2, "w": {"chrome": 2}, "h": ["lichess.org"]}
    sites._put_days(v, {"2026-11-09": {}})
    assert sites.load_days(v)[0] == {} and sites.days_gen(v) > gen
    assert sites.lean_days(v) == {}


@pytest.fixture
def bvault(root):
    from test_browse import hubbed
    return hubbed(root)


@pytest.fixture
def hist(tmp_path):
    from test_browse import chromium
    local = tmp_path / "local"
    chromium(local / "Google" / "Chrome" / "User Data" / "Default" / "History", ROWS)
    return local, tmp_path / "roaming"


def test_forget_takes_a_site_out_of_the_counts_their_backup_and_the_file(bvault, hist):
    imp(bvault, hist)
    v = Vault(bvault)
    kept = sites.DAYS + legacy.DONE
    sites._save(v, kept, sites.load_days(v)[0])  # the backup legacy.migrate left
    assert "lichess" in days_text(bvault)
    with closing(store.connect(v)) as con:
        assert con.execute("SELECT COUNT(*) FROM judged WHERE kept LIKE '%lichess%'").fetchone()[0]
    c = forget.purge(v, forget.Rule(hosts=("lichess.org",)), settings=OWNER)
    assert c["site_keys"] and c["site_days_backup"]
    assert "lichess" not in days_text(bvault)
    assert "lichess" not in (bvault / kept).read_text(encoding="utf-8")
    assert "chess.com" in days_text(bvault)  # only that site went
    assert b"lichess" not in on_disk(bvault / store.PATH)  # memo and counts included


# ---- the privacy memo ------------------------------------------------------------------------


def visit(url, title, n, channel=""):
    return sources.Visit(start=datetime(2026, 11, 9, 10, n, tzinfo=UTC), url=url, title=title,
                         secs=30, source="t", native=f"n{n}", channel=channel)


URLS = [r[1:3] for r in ROWS] + [("https://mail.google.com/mail/u/0", "Inbox (3) - me"),
                                 ("https://www.youtube.com/watch?v=abcdefghijk", "x")]
LISTS = [privacy.Settings(), privacy.Settings(deny=("lichess.org",)),
         privacy.Settings(allow=("lichess.org", "google.com")),
         privacy.Settings(private=("youtube.com", "mail.google.com"))]


@pytest.mark.parametrize("url, title", URLS)
def test_the_lists_on_a_neutral_verdict_are_the_verdict(url, title):
    neutral = privacy.judge(url, title, settings=store._NEUTRAL)
    for s in LISTS:
        assert store._settle(neutral, s) == privacy.judge(url, title, settings=s)


def test_the_privacy_memo_judges_a_record_once_and_keeps_only_stored_ones(root, monkeypatch):
    calls = []
    real = privacy.judge
    monkeypatch.setattr(privacy, "judge", lambda url, *a, **k: calls.append(url) or real(
        url, *a, **k))
    s = privacy.Settings(adult="skip", visible_to_claude=True)
    chess = "https://lichess.org/a"
    vs = [visit(chess, "Chess openings", 0), visit("https://www.pornhub.com/x", "ADULTTITLE", 1),
          visit("https://netbanking.example.com/x", "Net banking", 2)]
    with closing(store.connect(Vault(root))) as con:
        assert store.ingest(con, vs, s, UTC)["dropped"] == 2 and len(calls) == 3

        def memo():
            return [json.loads(k)["url"] for (k,) in con.execute("SELECT kept FROM judged")]
        assert memo() == [chess]  # a record not stored leaves no trace
        calls.clear()
        c = store.ingest(con, [replace(x, native=x.native + "b") for x in vs], s, UTC)
        assert c["new"] == 1 and c["dropped"] == 2 and chess not in calls  # a later run
        c = store.ingest(con, [visit(chess, "Chess openings", 3)], replace(s, deny=(
            "lichess.org",)), UTC)
        assert c["dropped"] == 1 and memo() == []  # the lists apply on top; it goes
        store.ingest(con, [visit(chess, "Chess openings", 4)], s, UTC)
        assert memo() == [chess]
        monkeypatch.setattr(build, "FILTER_VERSION", build.FILTER_VERSION + 1)
        calls.clear()
        store.ingest(con, [visit(chess, "Chess openings", 5)], s, UTC)
        assert calls == [chess]  # a new filter version: judged again
    gone = defaultdict(set)
    assert store.forget_rows(Vault(root), forget.Rule(hosts=("lichess.org",)), gone,
                             str)["store_memo"] == 1
    with closing(store.connect(Vault(root))) as con:
        assert con.execute("SELECT COUNT(*) FROM judged").fetchone()[0] == 0


# ---- scale -----------------------------------------------------------------------------------


@pytest.mark.slow
def test_a_big_vector_store_adds_a_page_without_rewriting_the_rest(tmp_path):
    """100k chunks of 384 dims: adding one page writes that page only, and a cold start
    reads the whole store in a few seconds at most."""
    p, dim, rng = tmp_path / "v.db", 384, np.random.default_rng(0)
    with closing(vecstore.connect(p)) as con:
        vecstore.prepare(con, {"dim": dim})
        for b in range(10):
            vecstore.put(con, {f"p{b}-{i}": (f"m{i}", True, np.arange(10, dtype=np.int32),
                                             rng.standard_normal((10, dim)).astype(np.float32))
                               for i in range(1000)})
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size = p.stat().st_size
        t = time.perf_counter()
        vecstore.put(con, {"new": ("m", True, np.arange(10, dtype=np.int32),
                                   rng.standard_normal((10, dim)).astype(np.float32))})
        add = time.perf_counter() - t
        wrote = sum(f.stat().st_size for f in tmp_path.glob("v.db-wal"))
    t = time.perf_counter()
    with closing(vecstore.connect(p, create=False)) as con:
        cache = {}
        vecstore.sync(con, dim, None, cache, str)
    ids, _, _, m = vecstore.stack(cache, dim)
    cold = time.perf_counter() - t
    assert len(ids) == 10001 and m.shape == (100010, dim)
    assert add < 0.5 and wrote < size / 20, (add, wrote, size)
    assert cold < 10, cold


def test_the_background_net_uses_the_embedder_only_while_it_is_in_memory(monkeypatch):
    from cortex import safety_net
    from cortex.browse import titles
    monkeypatch.setattr(titles, "may_run", lambda: (None, "no model"))
    monkeypatch.setattr(semantic, "_embedder", semantic._embed_real)
    monkeypatch.setattr(semantic._main, "model", None)  # dropped after IDLE_UNLOAD
    assert safety_net.background_tools() == (None, None)
    monkeypatch.setattr(semantic._main, "model", object())
    assert safety_net.background_tools() == (None, semantic._embed_quiet)  # M3
    fake = lambda texts: texts  # noqa: E731  (a test's injected embedder is always in memory)
    monkeypatch.setattr(semantic, "_embedder", fake)
    monkeypatch.setattr(semantic._main, "model", None)
    assert safety_net.background_tools() == (None, fake)
