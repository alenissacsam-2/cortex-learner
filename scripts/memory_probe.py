"""Where a long-lived Cortex process's memory goes, on a synthetic vault only (a temp folder
and a temp HOME; the real vault and ~/.cortex/config.yaml are never read).

A child process makes the vault: --notes pages (most short, some long: the long ones fill a
batch of 64 heads with ~400 tokens each, as a real vault's do) and a browse import of
--visits synthetic Chromium visits with a fake embedder (it writes the browse store and the
browse/ pages). This process then takes the steps a long-lived MCP server or the app takes
and prints its working set and private bytes (Windows, GetProcessMemoryInfo) after each:

  import cortex, open the MCP core, load the embedder, build the semantic index (as
  `cortex model build`), load the reranker, recall, the background copy embedding the private
  browse vectors, a browse search, the app's home (dashboard.build_data and its caches),
  then idle: the models dropped as after IDLE_UNLOAD (when semantic has `drop_idle`), the C
  heap trimmed, and the first recall after it (the reload).

The models must already be in ~/.cortex/models (only `cortex model download` fetches them;
this never does); without them the model steps are skipped and say so.

    uv run python scripts/memory_probe.py [--notes 1500] [--visits 30000] [--trace]
    uv run python scripts/memory_probe.py --app 60 [--hidden] [--notes 300] [--visits 3000]
    uv run python scripts/memory_probe.py --launch 12 [--hidden] [--notes 300] [--visits 3000]
    uv run python scripts/memory_probe.py --launch 25 --busy --scale 0.2 [--notes 300]
    uv run python scripts/memory_probe.py --mcp 15 --scale 0.2 [--notes 300]

--trace adds tracemalloc's Python heap per step and its top allocation sites at the end
(slower; its own bookkeeping counts in the process numbers). --app runs the desktop app on the
vault instead (its window shows on the screen) and prints its WebView2 processes' CPU and memory
per window state (`app_probe`). --launch starts the app as the installer does and, idle, prints
its memory and loaded search models each minute (`launch_probe`); with --busy a person searches
once at the start and new visits and chat pages keep coming, so the live browse import and the
safety net run on their schedules. --mcp starts one `cortex mcp` connection, sends a prime and a
recall over stdio and prints its memory and loaded models each idle minute (`mcp_probe`).
--scale shortens the schedules and the minute alike. The probe caps numpy's BLAS threads as
Cortex's entry points do (`taint.cap_blas`); set OPENBLAS_NUM_THREADS to measure another count.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import UTC, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS = HERE.parent / "tests"
MODELS = Path.home() / ".cortex" / "models"  # read before HOME moves to the temp folder
MB = 2 ** 20
WORDS = ("graph tree heap stack queue sort cache memory thread index model vector learning "
         "network chess cooking physics history python recursion pointer array binary search "
         "dynamic programming interview project deadline meeting idea decision paper").split()


# ---- process memory (Windows) -------------------------------------------------------------

class _Counters(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS_EX
    _fields_ = [("cb", ctypes.c_uint32), ("faults", ctypes.c_uint32)] + [
        (n, ctypes.c_size_t) for n in ("peak_ws", "ws", "qpp", "qp", "qpnp", "qnp",
                                       "pagefile", "peak_pagefile", "private")]


def memory() -> tuple[float, float, float]:
    """(working set, private bytes, peak working set) of this process in MB; zeros elsewhere."""
    if sys.platform != "win32":
        return 0.0, 0.0, 0.0
    k = ctypes.WinDLL("kernel32")
    k.GetCurrentProcess.restype = ctypes.c_void_p
    k.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
    c = _Counters()
    c.cb = ctypes.sizeof(c)
    k.K32GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return c.ws / MB, c.private / MB, c.peak_ws / MB


def heap_trim() -> None:
    """The C runtime's _heapmin (Windows): free heap pages go back to the OS."""
    if sys.platform == "win32":
        ctypes.CDLL("ucrtbase")._heapmin()


class Report:
    def __init__(self, trace: bool):
        self.trace, self.rows, self.last = trace, [], memory()
        print(f"{'step':<44}{'ws MB':>8}{'+ws':>7}{'priv MB':>9}{'+priv':>7}{'peak':>7}"
              f"{'py MB' if trace else '':>7}{'s':>7}")

    def step(self, label: str, seconds: float | None = None) -> None:
        import tracemalloc

        ws, priv, peak = memory()
        py = tracemalloc.get_traced_memory()[0] / MB if self.trace else None
        self.rows.append((label, ws, priv, peak, py, seconds))
        print(f"{label:<44}{ws:8.0f}{ws - self.last[0]:+7.0f}{priv:9.0f}"
              f"{priv - self.last[1]:+7.0f}{peak:7.0f}"
              f"{'' if py is None else f'{py:7.0f}':>7}"
              f"{'' if seconds is None else f'{seconds:7.1f}':>7}", flush=True)
        self.last = (ws, priv, peak)


def timed(fn):
    t = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t


# ---- the synthetic vault (made in a child process, so its imports stay out of the numbers) ---

def _text(rng, n_chars: int) -> str:
    out, size = [], 0
    while size < n_chars:
        line = " ".join(rng.choice(WORDS) for _ in range(rng.randint(6, 16))).capitalize() + ".\n"
        out.append(line + ("\n" if rng.random() < 0.3 else ""))
        size += len(line)
    return "".join(out)


def make(tmp: Path, notes: int, visits: int, models: Path | None = None) -> None:
    import random

    sys.path.insert(0, str(TESTS))
    from test_browse_perf import NOW, Embed, heavy_history

    from cortex.browse import Settings, build, sources, store
    from cortex.vault import Vault

    root = tmp / "vault"
    shutil.copytree(TESTS / "fixtures" / "vault_mini", root)
    rng = random.Random(1)
    for d in ("resources", "notes"):
        (root / d).mkdir(exist_ok=True)
    for hub in ("chess", "cooking", "graphs"):
        (root / "notes" / f"{hub}.md").write_text(
            f'---\ntitle: "{hub.title()} hub"\nkind: note\nnote_type: structure\n'
            f'tags: ["topic"]\nrelated: []\n---\n# {hub.title()}\n', encoding="utf-8")
    for i in range(notes):  # 4 in 5 short (one chunk), 1 in 5 long (a ~400-token head, ~8 more)
        size = rng.randint(200, 1400) if i % 5 else rng.randint(6000, 9000)
        (root / "resources" / f"probe-{i}.md").write_text(
            f'---\ntitle: "probe page {i}"\nkind: resource\nsummary: "{rng.choice(WORDS)} '
            f'notes {i}"\n---\n# probe page {i}\n\n{_text(rng, size)}', encoding="utf-8")
    local = Path(os.environ["LOCALAPPDATA"])  # the temp HOME's: the app's live import finds it
    heavy_history(_history(), n=visits, n_sites=max(500, visits // 12), days=365)
    v = Vault(root)
    settings = Settings(visible_to_claude=True, hidden_from_claude=("adult",))
    store.migrate(v, settings=settings)
    build.run(v, sources.discover(local, Path(os.environ["APPDATA"])), settings,
              embed=Embed(per_text=0),
              runner=None, tz=UTC, now=NOW + timedelta(minutes=1), summaries=False,
              keep_local=False, ask=False)
    for p in (root / ".cortex").glob("vectors*"):  # the fake embedder's: the probe builds anew
        p.unlink()
    if models is not None and models.is_dir():  # --launch: the index a real install has
        from cortex import semantic
        from cortex.mcp_server import Cortex
        semantic.models_dir = lambda: models
        core = Cortex(v)
        core._ready.wait()
        embed = semantic.load_embedder()
        semantic.vectors(core.index, embed, len(semantic._embed(embed, ["cortex"])[0]))
    print(f"made {notes} notes, {sum(1 for _ in (root / 'browse').rglob('*.md'))} browse pages",
          flush=True)


def _history() -> Path:
    """The made-up Chrome History, under the temp HOME's LOCALAPPDATA."""
    return (Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data" / "Default"
            / "History")


def _home(tmp: Path) -> dict:
    """A temp HOME, so the real ~/.cortex is never read or written."""
    h = tmp / "home"
    h.mkdir(parents=True, exist_ok=True)
    env = {"HOME": str(h), "USERPROFILE": str(h), "APPDATA": str(h / "APPDATA"),
           "LOCALAPPDATA": str(h / "LOCALAPPDATA")}
    os.environ.update(env)
    Path.home = classmethod(lambda cls: h)  # type: ignore[method-assign]
    return env


# ---- the probe -------------------------------------------------------------------------------

def probe(tmp: Path, trace: bool) -> None:
    import tracemalloc

    if trace:
        tracemalloc.start(8)
    r = Report(trace)
    r.step("python started")
    _, s = timed(lambda: __import__("cortex.mcp_server"))
    import cortex.home  # noqa: F401
    from cortex import semantic
    from cortex.mcp_server import Cortex
    from cortex.vault import Vault
    r.step("import cortex (mcp_server, semantic, home)", s)
    semantic.models_dir = lambda: MODELS  # the real models, read only; nothing else of ~/.cortex

    v = Vault(tmp / "vault")

    def core_open():
        c = Cortex(v)
        c._ready.wait()
        return c
    core, s = timed(core_open)
    r.step("MCP core open (catalog + index)", s)

    embed, s = timed(semantic.load_embedder)
    if embed is None:
        print(f"!! no embedding model in {MODELS}: the model steps are skipped")
    else:
        r.step("embedder loaded", s)
        dim = len(semantic._embed(embed, ["cortex"])[0])
        stack, s = timed(lambda: semantic.vectors(core.index, embed, dim))
        r.step(f"semantic index built ({len(stack[3])} chunks)", s)
    ce, s = timed(semantic.load_reranker)
    if ce is None:
        print("!! no reranker model: the rerank step is skipped")
    else:
        r.step("reranker loaded", s)
    queries = [f"what do I know about {a} and {b}" for a, b in zip(WORDS, WORDS[5:], strict=False)]
    _, s = timed(lambda: core.cortex_recall(queries[0]))
    r.step("first recall", s)
    lat = []
    for q in queries[1:13]:
        lat.append(timed(lambda q=q: core.cortex_recall(q))[1])
    r.step(f"12 recalls (median {statistics.median(lat) * 1000:.0f} ms)", sum(lat))
    q_lat = []
    if embed is not None:
        for q in queries[:30]:
            q_lat.append(timed(lambda q=q: embed([semantic.QUERY_PREFIX + q]))[1])
        print(f"   query embedding median {statistics.median(q_lat) * 1000:.1f} ms, "
              f"p90 {sorted(q_lat)[int(len(q_lat) * 0.9)] * 1000:.1f} ms")

    from cortex.browse import vectors as browse_vectors
    if embed is not None:
        def bg():
            with semantic.background():
                return browse_vectors.update(v)
        got, s = timed(bg)
        r.step(f"background copy: browse vectors ({got['chunks']} chunks)", s)
        _, s = timed(lambda: browse_vectors.search(v, queries[3]))
        r.step("private browse search", s)

    from cortex.home import HomeCache
    home = HomeCache(v)
    _, s = timed(lambda: home.get(False))
    r.step("app home (build_data, feed, caches)", s)
    _, s = timed(lambda: home.get(True))
    r.step("app home, private", s)

    gc.collect()
    r.step("idle (gc.collect)")
    drop = getattr(semantic, "drop_idle", None)
    if drop is not None:
        dropped, s = timed(lambda: drop(time.monotonic() + 10 ** 6))
        r.step(f"models dropped: {', '.join(dropped) or 'none'}", s)
        gc.collect()
        r.step("gc.collect")
    heap_trim()
    r.step("C heap trimmed (_heapmin)")
    if embed is not None:
        _, s = timed(lambda: embed([semantic.QUERY_PREFIX + queries[20]]))
        r.step("first query embedding after idle (reload)", s)
        _, s = timed(lambda: core.cortex_recall(queries[21]))
        r.step("recall after idle (reranker reload)", s)
        lat = [timed(lambda q=q: embed([semantic.QUERY_PREFIX + q]))[1] for q in queries[:30]]
        print(f"   query embedding after reload: median {statistics.median(lat) * 1000:.1f} ms")
    if trace:
        snap = tracemalloc.take_snapshot()
        print("\ntop Python allocations (tracemalloc, by line):")
        for st in snap.statistics("lineno")[:15]:
            f = st.traceback[0]
            print(f"  {st.size / MB:7.1f} MB  {st.count:8d}  {Path(f.filename).name}:{f.lineno}")


# ---- the desktop app's WebView2 processes (--app) ---------------------------------------------

class _Entry(ctypes.Structure):  # PROCESSENTRY32W
    _fields_ = [("size", ctypes.c_uint32), ("usage", ctypes.c_uint32), ("pid", ctypes.c_uint32),
                ("heap", ctypes.c_size_t), ("module", ctypes.c_uint32),
                ("threads", ctypes.c_uint32), ("ppid", ctypes.c_uint32), ("prio", ctypes.c_long),
                ("flags", ctypes.c_uint32), ("exe", ctypes.c_wchar * 260)]


def descendants(root: int) -> list[int]:
    """Every process started under `root`: the WebView2 browser and its renderer, GPU and
    utility processes (a Toolhelp snapshot; nothing outside root's tree is opened)."""
    k = ctypes.WinDLL("kernel32")
    k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    snap, e, kids = k.CreateToolhelp32Snapshot(2, 0), _Entry(), {}  # TH32CS_SNAPPROCESS
    e.size = ctypes.sizeof(e)
    ok = k.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        kids.setdefault(e.ppid, []).append(e.pid)
        ok = k.Process32NextW(snap, ctypes.byref(e))
    k.CloseHandle(snap)
    out, todo = [], [root]
    while todo:
        for pid in kids.get(todo.pop(), []):
            if pid != root and pid not in out:
                out.append(pid)
                todo.append(pid)
    return out


def usage(pid: int) -> tuple[float, float, float] | None:
    """(CPU seconds so far, working set MB, private MB) of one process; None when it is gone."""
    k = ctypes.WinDLL("kernel32")
    k.OpenProcess.restype = ctypes.c_void_p
    k.GetProcessTimes.argtypes = [ctypes.c_void_p] * 5
    k.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k.OpenProcess(0x1010, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ
    if not h:
        return None
    try:
        t = [ctypes.c_uint64() for _ in range(4)]  # FILETIMEs: created, exited, kernel, user
        if not k.GetProcessTimes(h, *map(ctypes.byref, t)):
            return None
        c = _Counters()
        c.cb = ctypes.sizeof(c)
        k.K32GetProcessMemoryInfo(h, ctypes.byref(c), c.cb)
        return (t[2].value + t[3].value) / 1e7, c.ws / MB, c.private / MB
    finally:
        k.CloseHandle(h)


def _fake_app() -> tuple[list, int]:
    """The desktop app made safe to run beside the owner's: no Run key or login item written, a
    fake tray, Home as the first page; ([the App once started], a free port, also server.PORT)."""
    import socket
    import types
    from datetime import datetime

    from cortex import localllm
    from cortex.app import actions, desktop, server
    (Path.home() / ".cortex").mkdir(exist_ok=True)
    desktop.first_run = lambda echo=print: None  # never the real Run key
    actions.migrate_login = lambda root: None
    desktop._tray = lambda d: types.SimpleNamespace(
        notify=lambda *a: None, update_menu=lambda: None, stop=lambda: None)
    url = server.start_url
    server.start_url = lambda app, now=None: url(app, datetime(2026, 11, 20, 12))  # Home
    apps, real = [], server.start

    def start(*a, **k):
        out = real(*a, **k)
        apps.append(out[1])
        return out
    server.start = start
    server.UPDATE_FIRST_S = 10 ** 6  # no release check on GitHub
    localllm._lms_exe = lambda cfg: None  # LM Studio's CLI is never run (Status's probes)
    with socket.socket() as s:  # a free port: never the owner's app's
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server.PORT = port
    return apps, port


def _desktop(apps: list):
    while not apps or getattr(apps[0], "desktop", None) is None or apps[0].desktop.win is None:
        time.sleep(0.2)
    return apps[0].desktop


def app_probe(tmp: Path, seconds: int, hidden: bool) -> None:
    """The desktop app (desktop.run, a fake tray, a free port) on the made-up vault's Home: for
    `seconds` each with the window shown, hidden to the tray (its X), minimised and shown again,
    the CPU seconds and memory of its WebView2 processes and the page's document.visibilityState.
    `hidden`: started as Start with Windows starts it, first measured before it is ever shown."""
    import threading

    from cortex.app import desktop
    apps, port = _fake_app()
    me = os.getpid()
    print(f"{'window':<28}{'CPU s':>7}{'CPU %':>7}{'ws MB':>7}{'priv MB':>8}{'procs':>6}  page")

    def visibility(win) -> str:
        got = []
        t = threading.Thread(target=lambda: got.append(win.evaluate_js(
            "document.visibilityState")), daemon=True)
        t.start()
        t.join(5)  # a sleeping page answers late
        return got[0] if got else "(asleep)"

    def phase(label: str, d) -> None:
        time.sleep(5)
        seen = visibility(d.win)
        a = {p: u for p in descendants(me) if (u := usage(p))}
        t = time.monotonic()
        time.sleep(seconds)
        b = {p: u for p in descendants(me) if (u := usage(p))}
        dt = time.monotonic() - t
        cpu = sum(u[0] - (a[p][0] if p in a else 0) for p, u in b.items())
        print(f"{label:<28}{cpu:7.1f}{100 * cpu / dt:7.1f}{sum(u[1] for u in b.values()):7.0f}"
              f"{sum(u[2] for u in b.values()):8.0f}{len(b):6d}  {seen}", flush=True)

    def drive():
        d = _desktop(apps)
        try:
            d.win.events.loaded.wait(120)
            time.sleep(10)  # the page settles
            if hidden:
                phase("started hidden", d)
                d.show()
                d.win.events.loaded.wait(60)
            phase("shown", d)
            d.closing()  # the window's X: hidden to the tray
            phase("hidden to the tray", d)
            phase("hidden, later", d)
            t = time.monotonic()
            d.show()
            print(f"   shown again in {time.monotonic() - t:.2f} s")
            phase("shown again", d)
            d.win.minimize()
            phase("minimised", d)
            d.show()
            phase("restored", d)
        finally:
            d.quit()

    threading.Thread(target=drive, daemon=True, name="probe-drive").start()
    desktop.run(tmp / "vault", hidden=hidden, port=port, echo=lambda s: None)
    time.sleep(3)
    print(f"WebView2 processes left after Quit: {len(descendants(me))}")


def _scale(f: float) -> None:
    """Every schedule M3 measures times `f` (the models' idle unload and its check, the live
    browse import's, the chat passes' with the safety net), so N minutes take N * f; the model
    loads and the work itself stay real."""
    from cortex import browse_serve, chat_import, semantic
    semantic.IDLE_UNLOAD *= f
    semantic.IDLE_CHECK *= f
    browse_serve.AUTO_FIRST_S *= f
    browse_serve.AUTO_EVERY_S *= f
    chat_import.FIRST_S *= f
    chat_import.EVERY_S *= f


def _feed(root: Path, minute: float, stop) -> None:
    """A PC in use, made up: 40 new visits in the History every minute, so each live import has
    some, and a new chat page every 5 minutes, so each safety-net pass has one waiting."""
    from datetime import datetime

    sys.path.insert(0, str(TESTS))
    from test_browse_perf import heavy_history

    from cortex.vault import Vault
    v, n = Vault(root), 0
    while not stop.wait(minute):
        n += 1
        heavy_history(_history(), n=40, n_sites=40, days=1, start_id=10 ** 7 + 40 * n,
                      end=datetime.now(UTC))
        if n % 5 == 1:
            when = datetime.now().strftime("%Y-%m-%d %H:%M")
            a, b = WORDS[n % len(WORDS)], WORDS[n * 7 % len(WORDS)]
            v.write_text(root / "resources" / f"probe-chat-{n}.md", (
                f'---\ntitle: "Chat {n}"\nkind: resource\nresource_type: other\n---\n'
                f"# Chat {n}\n\nTimeline: · Source: Claude · Date: {when[:10]} · My turns only"
                f"\n\n### {when}\n**Me:** explain {a} and {b} with an example\n\n"))


def launch_probe(tmp: Path, minutes: int, hidden: bool, busy: bool = False,
                 scale: float = 1.0) -> None:
    """The app as the installer starts it (`Cortex.exe -m cortex.app.launch [--hidden]`:
    launch.main, made safe by _fake_app) on the made-up vault, the real models readable and no
    job started; its window stays as it starts. Each minute: this (the app's) process's memory,
    its WebView2 processes', which search models are loaded and how long unused, and what used
    them. At the end, what dropping the models gives back. `busy` (M3): a person's search at
    the start, then the PC in use as `_feed` makes it, so the live import and the safety net
    run on their schedules; `scale` shortens every schedule and the minute (`_scale`)."""
    import threading
    import traceback
    from contextlib import contextmanager

    from cortex import semantic
    from cortex.app import launch
    os.environ["CORTEX_VAULT"] = str(tmp / "vault")  # the vault, as a config would name it
    semantic.models_dir = lambda: MODELS
    _scale(scale)
    minute = 60 * scale
    stop = threading.Event()
    if busy:
        threading.Thread(target=_feed, args=(tmp / "vault", minute, stop), daemon=True,
                         name="probe-feed").start()
    apps, _ = _fake_app()
    held = {"embedder": semantic._main, "background copy": semantic._capped,
            "reranker": semantic._ranker}
    names, uses, real_use = {id(h): n for n, h in held.items()}, [], semantic._Held.use

    @contextmanager
    def use(self, *a, **k):  # who keeps a model warm: the thread and the Cortex frames
        where = [f"{Path(f.filename).stem}.{f.name}" for f in traceback.extract_stack()[:-2]
                 if "cortex" in f.filename and Path(f.filename).stem != "semantic"]
        uses.append(f"{names.get(id(self), '?')}: {threading.current_thread().name} "
                    f"({' < '.join(where[:-4:-1])})")
        with real_use(self, *a, **k) as m:
            yield m
    semantic._Held.use = use
    me = os.getpid()
    print(f"{'min':>4}{'ws MB':>8}{'priv MB':>9}{'WebView2 ws':>13}{'priv':>7}  jobs, "
          "models loaded (seconds unused)")

    def drive():
        d = _desktop(apps)
        t0 = time.monotonic()
        try:
            if busy and (embed := semantic.load_embedder()) is not None:
                embed([semantic.QUERY_PREFIX + "what do I know about graphs"])
                print("   0: a person's search (the embedder loaded)")
            for minute in range(1, minutes + 1):
                time.sleep(max(0.0, t0 + 60 * scale * minute - time.monotonic()))
                ws, priv, _ = memory()
                kids = [u for p in descendants(me) if (u := usage(p))]
                now = time.monotonic()
                loaded = ", ".join(f"{n} ({(now - h.used) / scale:.0f})"
                                   for n, h in held.items() if h.model) or "none"
                print(f"{minute:4d}{ws:8.0f}{priv:9.0f}{sum(k[1] for k in kids):13.0f}"
                      f"{sum(k[2] for k in kids):7.0f}  {'job' if d.app.jobs.running() else '-'}"
                      f", {loaded}")
                for u in sorted(set(uses)):
                    print(f"        used: {u}")
                uses.clear()
            stop.set()
            dropped = semantic.drop_idle(time.monotonic() + 10 ** 6)
            gc.collect()
            ws, priv, _ = memory()
            print(f"   dropped {', '.join(dropped) or 'nothing'}: ws {ws:.0f} MB, private "
                  f"{priv:.0f} MB; OPENBLAS_NUM_THREADS={os.environ.get('OPENBLAS_NUM_THREADS')}")
        finally:
            d.quit()

    threading.Thread(target=drive, daemon=True, name="probe-drive").start()
    sys.argv = [sys.argv[0], *(["--hidden"] if hidden else [])]
    launch.main()


def serve_child(tmp: Path, models: Path, scale: float) -> None:
    """`cortex mcp --vault` as an AI app starts it (taint.main), on the made-up vault with the
    real models readable. On stderr every minute: `@@` and its memory and loaded models.
    cortex.semantic is imported first, to point it at the models and scale its idle unload:
    numpy then loads before the handshake instead of in the warm-up a moment after."""
    import threading

    from cortex import semantic, taint
    semantic.models_dir = lambda: models
    semantic.IDLE_UNLOAD *= scale
    semantic.IDLE_CHECK *= scale

    def watch():
        t0 = time.monotonic()
        held = {"embedder": semantic._main, "reranker": semantic._ranker}
        for minute in range(1, 10 ** 6):
            time.sleep(max(0.0, t0 + 60 * scale * minute - time.monotonic()))
            ws, priv, _ = memory()
            now = time.monotonic()
            loaded = ", ".join(f"{n} ({(now - h.used) / scale:.0f})" for n, h in held.items()
                               if h.model) or "none"
            sys.stderr.write(f"@@{minute:4d}{ws:8.0f}{priv:9.0f}  {loaded}\n")
            sys.stderr.flush()
    threading.Thread(target=watch, daemon=True, name="probe-watch").start()
    sys.argv = ["cortex", "mcp", "--vault", str(tmp / "vault")]
    taint.main()


def mcp_probe(tmp: Path, minutes: int, scale: float) -> None:
    """One `cortex mcp` connection (`serve_child`), driven over stdio as an AI app does:
    initialize, one cortex_prime, one cortex_recall, then idle `minutes` (each 60 * `scale` s,
    the idle unload scaled alike); then a prime and a recall again. Prints the calls' seconds
    and the server's memory and loaded models each minute."""
    import json
    import threading

    p = subprocess.Popen([sys.executable, __file__, "--serve", str(tmp), "--models", str(MODELS),
                          "--scale", str(scale)], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    print(f"{'min':>4}{'ws MB':>8}{'priv MB':>9}  models loaded (seconds unused)")

    def rows():
        for line in p.stderr:
            s = line.decode("utf-8", "replace").rstrip()
            if s.startswith("@@"):
                print(s[2:], flush=True)
    threading.Thread(target=rows, daemon=True).start()
    n = [0]

    def call(method: str, params: dict | None = None) -> dict | None:
        msg = {"jsonrpc": "2.0", "method": method, **({} if params is None else
                                                      {"params": params})}
        if not method.startswith("notifications/"):
            n[0] += 1
            msg["id"] = n[0]
        p.stdin.write((json.dumps(msg) + "\n").encode())
        p.stdin.flush()
        while "id" in msg:
            line = p.stdout.readline()
            if not line:
                raise RuntimeError("the server ended")
            r = json.loads(line)
            if r.get("id") == n[0] and "method" not in r:
                if "error" in r or r.get("result", {}).get("isError"):
                    print(f"!! {method}: {str(r)[:300]}")
                return r
        return None

    def timed_call(label: str, method: str, params: dict | None = None) -> None:
        t = time.perf_counter()
        call(method, params)
        print(f"   {label}: {time.perf_counter() - t:.2f} s", flush=True)

    def tools(when: str) -> None:
        timed_call(f"cortex_prime{when}", "tools/call", {"name": "cortex_prime", "arguments": {
            "prompt": "what do I know about graphs and binary search"}})
        timed_call(f"cortex_recall{when}", "tools/call", {"name": "cortex_recall", "arguments": {
            "query": "dynamic programming interview notes"}})

    t0 = time.monotonic()
    timed_call("initialize", "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "memory-probe",
                                                           "version": "0"}})
    call("notifications/initialized")
    tools(" (first)")
    time.sleep(max(0.0, t0 + 60 * scale * minutes - time.monotonic()) + 1)
    tools(" after idle")
    time.sleep(60 * scale + 1)  # one more row: the models loaded again
    p.stdin.close()
    p.wait(60)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notes", type=int, default=1500)
    ap.add_argument("--visits", type=int, default=30_000)
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep the temp folder")
    ap.add_argument("--app", type=int, default=0, metavar="SECONDS",
                    help="the desktop app's WebView2 processes instead, SECONDS per state")
    ap.add_argument("--launch", type=int, default=0, metavar="MINUTES",
                    help="the app as the installer starts it, idle, MINUTES long")
    ap.add_argument("--hidden", action="store_true",
                    help="--app, --launch: started hidden, as at login")
    ap.add_argument("--busy", action="store_true",
                    help="--launch: a person's search at the start, then new visits and chat "
                         "pages, so the live import and the safety net run (M3)")
    ap.add_argument("--mcp", type=int, default=0, metavar="MINUTES",
                    help="one `cortex mcp` connection: a prime and a recall, then idle MINUTES")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="--launch, --mcp: every schedule and the minute times this (0.2: a "
                         "minute takes 12 s)")
    ap.add_argument("--make", help=argparse.SUPPRESS)  # the child: make the vault there
    ap.add_argument("--models", help=argparse.SUPPRESS)  # the child: and index it with these
    ap.add_argument("--serve", help=argparse.SUPPRESS)  # the child: `cortex mcp` on that vault
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # each line as it comes, into a file too
    from cortex import taint
    taint.cap_blas()  # as every Cortex entry point does, before numpy loads
    if args.make:
        _home(Path(args.make))
        make(Path(args.make), args.notes, args.visits, args.models and Path(args.models))
        return
    if args.serve:
        _home(Path(args.serve))
        serve_child(Path(args.serve), Path(args.models), args.scale)
        return
    tmp = Path(tempfile.mkdtemp(prefix="cortex-memory-probe-"))
    try:
        env = _home(tmp)
        subprocess.run([sys.executable, __file__, "--make", str(tmp), "--notes",
                        str(args.notes), "--visits", str(args.visits),
                        *(["--models", str(MODELS)] if args.launch or args.mcp else [])],
                       check=True, env={**os.environ, **env})
        print(f"{os.cpu_count()} logical cores; models in {MODELS.name}: "
              f"{'yes' if MODELS.is_dir() else 'no'}")
        if args.mcp:
            mcp_probe(tmp, args.mcp, args.scale)
        elif args.launch:
            launch_probe(tmp, args.launch, args.hidden, args.busy, args.scale)
        elif args.app:
            app_probe(tmp, args.app, args.hidden)
        else:
            probe(tmp, args.trace)
    finally:
        if args.keep:
            print(f"kept at {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
