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

--trace adds tracemalloc's Python heap per step and its top allocation sites at the end
(slower; its own bookkeeping counts in the process numbers).
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


def make(tmp: Path, notes: int, visits: int) -> None:
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
    local = tmp / "local"
    heavy_history(local / "Google" / "Chrome" / "User Data" / "Default" / "History",
                  n=visits, n_sites=max(500, visits // 12), days=365)
    v = Vault(root)
    settings = Settings(visible_to_claude=True, hidden_from_claude=("adult",))
    store.migrate(v, settings=settings)
    build.run(v, sources.discover(local, tmp / "roaming"), settings, embed=Embed(per_text=0),
              runner=None, tz=UTC, now=NOW + timedelta(minutes=1), summaries=False,
              keep_local=False, ask=False)
    for p in (root / ".cortex").glob("vectors*"):  # the fake embedder's: the probe builds anew
        p.unlink()
    print(f"made {notes} notes, {sum(1 for _ in (root / 'browse').rglob('*.md'))} browse pages",
          flush=True)


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notes", type=int, default=1500)
    ap.add_argument("--visits", type=int, default=30_000)
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep the temp folder")
    ap.add_argument("--make", help=argparse.SUPPRESS)  # the child: make the vault there
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # each line as it comes, into a file too
    if args.make:
        _home(Path(args.make))
        make(Path(args.make), args.notes, args.visits)
        return
    tmp = Path(tempfile.mkdtemp(prefix="cortex-memory-probe-"))
    try:
        env = _home(tmp)
        subprocess.run([sys.executable, __file__, "--make", str(tmp), "--notes",
                        str(args.notes), "--visits", str(args.visits)],
                       check=True, env={**os.environ, **env})
        print(f"{os.cpu_count()} logical cores; models in {MODELS.name}: "
              f"{'yes' if MODELS.is_dir() else 'no'}")
        probe(tmp, args.trace)
    finally:
        if args.keep:
            print(f"kept at {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
