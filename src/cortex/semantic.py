"""Semantic recall (SPEC §0 U7): vector cosine + FTS BM25, then graph neighbours.

The embedder is injected as `embed(texts) -> (n, d) array`, so tests use a fake one and never
download the model. The real one is fastembed `BAAI/bge-small-en-v1.5` in ~/.cortex/models,
fetched only by `cortex model download`. A page is embedded as chunks (`_chunks`: its head,
then overlapping windows over the rest of the body) and scores by its best chunk. Vectors are a
disposable per-vault cache, the SQLite store `.cortex/vectors.db` (`cortex.vecstore`: a row per
page with its manifest (path, mtime_ns, size), chunk starts and float16 vectors; a save writes
the changed pages only); only pages whose file changed are re-embedded. A process reads the
store on its first query into one float32 matrix and then only the rows other processes saved
since. The older `.cortex/vectors-v3.npz` is read into it once and kept as a backup. A recall
embeds missing heads for at most HEAD_BUDGET seconds and
other missing chunks for at most RECALL_BUDGET; `cortex model build` builds the rest.
Private pages and raw/ never enter the index, so they are never embedded. Background work (the
browse import) runs inside `background()`: a copy of the model capped at half the cores, at
below-normal priority; a query never waits on the cap.

An optional cross-encoder (`RERANK_MODEL`, same cache dir, same download rule) reorders the top
`RERANK_POOL` hits; it never changes a hit's `score` or `match`. `rerank: false` in
~/.cortex/config.yaml turns it off. `fuse="rrf"` is an eval-only alternative to the weighted sum.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np

from cortex import vecstore
from cortex.events import local_day
from cortex.index import STOPWORDS, Index, _one_line
from cortex.topics import page_date, page_source

log = logging.getLogger(__name__)

MODEL = "BAAI/bge-small-en-v1.5"
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
RERANK_POOL = 30
RRF_K = 60
NOT_SET_UP = "semantic search not set up; run `cortex model download`"
CACHE = ".cortex/vectors.db"  # the vector store (cortex.vecstore)
NPZ = ".cortex/vectors-v3.npz"  # the cache before the store: read into it once, then a backup
BODY_CHARS = 1500
CHUNK_CHARS, CHUNK_OVERLAP = 1000, 200  # ~250-300 tokens: well under bge-small's 512
TEXT_VERSION = 3  # bump when _text or _chunks change, so cached vectors are rebuilt
CHUNKING = [BODY_CHARS, CHUNK_CHARS, CHUNK_OVERLAP]  # in the cache meta: a change rebuilds
RECALL_BUDGET = 1.0  # seconds of extra-chunk embedding per recall; `cortex model build` the rest
HEAD_BUDGET = 1.0  # seconds of missing chunk-0 embedding per recall (a lost cache, an import)
TAIL_BATCH = 8  # chunks per embed call under a budget, so it overshoots by about one second
W_COS, W_KW = 0.7, 0.3
# With the query prefix, bge-small scores unrelated pages ~0.45-0.56 (median ~0.5) and real
# matches >= 0.7 (measured on a real vault): below this a page is not similar at all.
# One constant on purpose: tune it on real queries (RETR-1), never per call.
# A page's best of ~6 chunks sits higher than one head vector, so the floor went from 0.6 to
# 0.62: the quantile where the same share of (page, query) pairs passes as heads did at 0.6
# (3.6%, measured over 30 queries on a 5,214-page vault; labels unused).
MIN_COS = 0.62
MIN_KW_COVER = 0.5  # under MIN_COS, a keyword hit must match this share of the query's terms
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "  # bge, queries only
DECAY = 0.5  # score factor per graph hop
MAX_LIMIT = 20
CHAT_WEIGHT = 0.6  # imported chat transcripts are long and noisy: the user's own pages win ties
# a page whose status says it no longer holds, so an old decision never outranks its successor
STALE_WEIGHT = 0.5
STALE = {("decision", "superseded"), ("decision", "retired"), ("project", "archived"),
         ("loop", "dropped")}
RECENT_BOOST, RECENT_DAYS = 0.12, 180  # a dated journal or chat page: up to +12%, fading
BATCH = 64
PROGRESS_S = 5.0  # a long embedding pass says how far it got
# vault root -> (the store's stamp as last seen, cache, stacked rows or None)
_MEM: dict[Path, tuple] = {}
_embedder = None
_EMBED_LOCK = threading.Lock()
_reranker = None


def models_dir() -> Path:
    """Beside ~/.cortex/config.yaml (the vault pointer, cli._pointer)."""
    return Path.home() / ".cortex" / "models"


def load_embedder(download: bool = False):
    """The model as `embed(texts) -> array`, or None when it is not downloaded. Only
    `download=True` (the `cortex model download` command) may fetch it."""
    global _embedder
    with _EMBED_LOCK:  # the server's warm-up thread and a first tool call may race
        if _embedder is not None:
            return _embedder
        if not download and not models_dir().is_dir():
            return None
        from fastembed import TextEmbedding  # slow import: keep it off CLI and server start

        try:
            model = TextEmbedding(MODEL, cache_dir=str(models_dir()),
                                  local_files_only=not download)
        except Exception:
            if download:
                raise
            return None

        def embed(texts):
            m = (_capped() or model) if getattr(_bg, "on", False) else model
            return np.asarray(list(m.embed(list(texts))), dtype=np.float32)

        _embedder = embed
        return embed


# ---- background embedding: half the cores, below-normal priority -----------------------------

BG_THREADS = max(1, (os.cpu_count() or 2) // 2)  # a background build leaves the rest to the owner
_bg = threading.local()
_bg_model = None


@contextmanager
def background():
    """Embedding in this thread is background work while inside: the real model's calls go to
    a second copy capped at BG_THREADS threads, all at below-normal priority (Windows), so the
    PC stays quick. A query in another thread keeps the full model. Nests."""
    if getattr(_bg, "on", False):
        yield
        return
    _bg.on = True
    old = _priority(_BELOW_NORMAL)
    try:
        yield
    finally:
        _bg.on = False
        if old is not None:
            _priority(old)


def _capped():
    """The capped copy of the model (made on first use), or None when it cannot be loaded."""
    global _bg_model
    with _EMBED_LOCK:
        if _bg_model is None:
            from fastembed import TextEmbedding

            before = _threads()
            try:
                m = TextEmbedding(MODEL, cache_dir=str(models_dir()), local_files_only=True,
                                  threads=BG_THREADS)
                list(m.embed(["cortex"]))  # onnxruntime has made its threads by now
            except Exception as e:
                log.warning("background embedding uses the full model: %s", type(e).__name__)
                _bg_model = False
                return None
            # shortcut: a thread another part of the process made in this second is lowered
            # too; harmless, as background work is all this process does then
            _lower(_threads() - before)
            _bg_model = m
        return _bg_model or None


_BELOW_NORMAL = -1  # THREAD_PRIORITY_BELOW_NORMAL


def _kernel32():
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32")  # its own instance: the argtypes below stay local
    H = wintypes.HANDLE
    k.GetCurrentThread.restype = k.OpenThread.restype = H
    k.CreateToolhelp32Snapshot.restype = H
    k.SetThreadPriority.argtypes = [H, ctypes.c_int]
    k.GetThreadPriority.argtypes = k.CloseHandle.argtypes = [H]
    k.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.Thread32First.argtypes = k.Thread32Next.argtypes = [H, ctypes.c_void_p]
    return k


def _priority(p: int) -> int | None:
    """Set this thread's priority (Windows); the one it had, None elsewhere."""
    if sys.platform != "win32":
        return None
    k = _kernel32()
    h = k.GetCurrentThread()
    old = k.GetThreadPriority(h)
    k.SetThreadPriority(h, p)
    return old


def _threads() -> set[int]:
    """The ids of this process's threads (Windows; empty elsewhere)."""
    if sys.platform != "win32":
        return set()
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):  # THREADENTRY32
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("tid", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("base", wintypes.LONG), ("delta", wintypes.LONG),
                    ("flags", wintypes.DWORD)]

    k = _kernel32()
    snap = k.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
    if not snap or snap == ctypes.c_void_p(-1).value:
        return set()
    out, e, pid = set(), Entry(), k.GetCurrentProcessId()
    e.size = ctypes.sizeof(Entry)
    try:
        ok = k.Thread32First(snap, ctypes.byref(e))
        while ok:
            if e.pid == pid:
                out.add(e.tid)
            ok = k.Thread32Next(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    return out


def _lower(tids) -> None:
    """Below-normal priority for these threads of this process (Windows)."""
    if not tids:
        return
    k = _kernel32()
    for t in tids:
        if h := k.OpenThread(0x0020, False, t):  # THREAD_SET_INFORMATION
            k.SetThreadPriority(h, _BELOW_NORMAL)
            k.CloseHandle(h)


def rerank_on() -> bool:
    """The `rerank` switch in ~/.cortex/config.yaml (default on)."""
    from cortex.taint import config

    return config().get("rerank") is not False


def load_reranker(download: bool = False, config: bool = True):
    """The cross-encoder as `score(query, texts) -> list[float]`, or None when it is not
    downloaded, fails to load, or is switched off (`config=False` ignores the switch). Only
    `download=True` may fetch it."""
    global _reranker
    if config and not download and not rerank_on():
        return None
    if _reranker is not None:
        return _reranker
    # fastembed's HF cache folder; checked first so an absent model logs no fastembed ERROR
    if not download and not (models_dir() / f"models--{RERANK_MODEL.replace('/', '--')}").is_dir():
        return None
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    try:
        model = TextCrossEncoder(RERANK_MODEL, cache_dir=str(models_dir()),
                                 local_files_only=not download)
    except Exception:
        if download:
            raise
        return None

    def score(query, texts):
        return [float(x) for x in model.rerank(query, list(texts))]

    _reranker = score
    return score


def is_chat(n: dict) -> bool:
    """An imported chat page: a resource whose `Timeline:` line names its `Source:` app."""
    return n["kind"] == "resource" and bool(page_source(n["body"]))


def _today() -> str:
    """The local day recency counts from (tests patch it)."""
    return local_day(datetime.now(UTC))


def _factors(index: Index, id: str, chats: bool, quiet: dict[str, float] | None) -> dict:
    """A page's score factors other than 1 after the floors: `quiet`, its harvest.quiet weight
    (0 hides a folded copy even with `chats`), else `chat`, CHAT_WEIGHT for a chat page (`chats`
    lifts both but 0); `status`, STALE_WEIGHT for a STALE status; `recency`, up to
    1 + RECENT_BOOST for a journal or chat page dated in the last months."""
    q = (quiet or {}).get(id)
    if q == 0:
        return {"quiet": 0.0}
    n = index._notes[id]
    chat = is_chat(n)
    f = {}
    if q is not None:
        if not chats:
            f["quiet"] = q
    elif chat and not chats:
        f["chat"] = CHAT_WEIGHT
    if (n["kind"], str(n["fields"].get("status"))) in STALE:  # str: a YAML list is unhashable
        f["status"] = STALE_WEIGHT
    if (chat or n["kind"] == "journal") and (d := page_date(id, n["fields"], n["body"])):
        try:
            age = max(0, (date.fromisoformat(_today()) - date.fromisoformat(d)).days)
            f["recency"] = 1 + RECENT_BOOST * math.exp(-age / RECENT_DAYS)
        except ValueError:  # a malformed day (2026-13-45) gets no boost
            pass
    return f


def _weight(index: Index, id: str, chats: bool, quiet: dict[str, float] | None) -> float:
    """The product of the page's `_factors`; 0 drops it."""
    return math.prod(_factors(index, id, chats, quiet).values(), start=1.0)


def rerank(index: Index, query: str, hits: list[dict], score, chats: bool = False,
           quiet: dict[str, float] | None = None, out: dict | None = None) -> list[dict]:
    """`hits` reordered by the cross-encoder over each hit's best chunk (`at`; chunk 0 without
    one), its sigmoid times the page's `_weight`; on any failure, as they were. Each hit's
    `score` and `match` are untouched (the Honest Double check reads them)."""
    if len(hits) < 2:
        return hits
    try:
        s = score(query, [_chunk_text(index._notes[h["id"]], h.get("at", 0)) for h in hits])
    except Exception as e:  # a broken model must never break recall
        log.warning("rerank skipped: %s", e)
        return hits
    # logits can be negative, so weight their sigmoid (same order) rather than the logit
    s = [1 / (1 + np.exp(-x)) * _weight(index, h["id"], chats, quiet)
         for x, h in zip(s, hits, strict=True)]
    if out is not None:  # `--explain` shows the weighted rerank score per page
        out.update({h["id"]: float(x) for x, h in zip(s, hits, strict=True)})
    return [h for _, h in sorted(zip(s, hits, strict=True), key=lambda p: -p[0])]


def _unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) or 1.0)


def _filled(body: str) -> str:
    """The body without headings of empty sections: template headings ("## How I write to
    them" on a fresh people page) otherwise match queries they say nothing about."""
    lines, out = body.splitlines(), []
    for i, line in enumerate(lines):
        if h := _HEAD.match(line):
            nxt = next((x for x in lines[i + 1:] if x.strip()), "")
            sub = _HEAD.match(nxt)
            if not nxt or (sub and len(sub[1]) <= len(h[1])):  # nothing under it
                continue
        out.append(line)
    return "\n".join(out)


_HEAD = re.compile(r"^(#{1,6})\s")


def _text(n: dict) -> str:
    summary = n["fields"].get("summary") or ""
    return f"{n['title']}\n{summary}\n{_filled(n['body'].strip())[:BODY_CHARS]}"


def _chunks(n: dict) -> list[tuple[int, str]]:
    """(start in the filled body, text) per chunk. Chunk 0 is `_text` (start 0). The rest are
    title-prefixed windows of about CHUNK_CHARS over the body from BODY_CHARS - CHUNK_OVERLAP
    on, each overlapping the one before by CHUNK_OVERLAP and cut at the last turn (`### `,
    `**Me:**`), else paragraph, boundary in the window's final CHUNK_OVERLAP characters."""
    body, title = _filled(n["body"].strip()), n["title"]
    out = [(0, _text(n))]
    s = BODY_CHARS - CHUNK_OVERLAP
    while len(body) > BODY_CHARS:
        end = s + CHUNK_CHARS
        if end >= len(body) - CHUNK_OVERLAP:  # the rest, rather than a short last window
            out.append((s, f"{title}\n{body[s:]}"))
            break
        seg = body[end - CHUNK_OVERLAP:end]
        turns = [m.start() for m in _TURN.finditer(seg)]
        cut = turns[-1] if turns else seg.rfind("\n\n")
        if cut > 0:
            end += cut - CHUNK_OVERLAP
        out.append((s, f"{title}\n{body[s:end]}"))
        s = end - CHUNK_OVERLAP
    return out


# a turn starts at its `### <time>` line when it has one, else at `**Me:**`
_TURN = re.compile(r"\n(?:### [^\n]*\n)?\*\*Me:\*\*|\n### ")


def _chunk_text(n: dict, at: int) -> str:
    """The text of the chunk starting at `at`, as embedded; chunk 0 when there is none."""
    return next((t for s, t in _chunks(n) if s == at), _text(n)) if at else _text(n)


def _pages(index: Index) -> dict[str, tuple]:
    return {id: (index._rel(p), *index._stats[p])  # not relative_to: most of a warm query
            for p, id in index._ids.items() if id in index._notes}


# The cache: page id -> (manifest (path, mtime_ns, size), full (every chunk built), chunk
# starts int32 (k,), unit vectors (k, dim)); chunk 0 is always the first row. In memory the
# vectors are views of one float32 matrix (`vecstore.stack`); on disk, the rows of CACHE.


def _want(dim: int) -> dict:
    return {"model": MODEL, "text": TEXT_VERSION, "chunk": CHUNKING, "dim": dim}


def _npz_read(root: Path) -> dict:
    """The .npz cache from before the store, or {} when it is missing, unreadable or built
    with other settings."""
    try:
        with np.load(root / NPZ, allow_pickle=False) as z:
            meta = json.loads(z["meta"].item())
            if [meta.get("model"), meta.get("text"), meta.get("chunk")] == [
                    MODEL, TEXT_VERSION, CHUNKING]:
                ids, full, page = [str(i) for i in z["ids"]], z["full"], z["page"]
                start, vecs = z["start"], z["vecs"].astype(np.float32)
                cut = np.searchsorted(page, np.arange(len(ids) + 1))
                return {i: (tuple(meta["pages"][i]), bool(full[k]), start[cut[k]:cut[k + 1]],
                            vecs[cut[k]:cut[k + 1]]) for k, i in enumerate(ids)}
    except (OSError, ValueError, KeyError) as e:
        if not isinstance(e, FileNotFoundError):
            log.warning("the old vector cache is unreadable: %s", e)
    return {}


def _npz_write(vault, cache: dict) -> None:
    """Rewrite the .npz backup with `cache` only (a forget takes rows out of it too)."""
    ids = list(cache)
    rows = [cache[i] for i in ids]
    dim = rows[0][3].shape[1] if rows else 1
    buf = io.BytesIO()
    np.savez(buf, ids=np.array(ids, dtype=str), full=np.array([e[1] for e in rows], dtype=bool),
             page=np.repeat(np.arange(len(ids), dtype=np.int32), [len(e[2]) for e in rows]),
             start=np.concatenate([e[2] for e in rows] or [np.zeros(0, np.int32)]),
             vecs=np.concatenate([e[3] for e in rows] or [np.zeros((0, dim))]).astype(np.float16),
             meta=np.array(json.dumps({"model": MODEL, "text": TEXT_VERSION, "chunk": CHUNKING,
                                       "dim": dim, "pages": {i: cache[i][0] for i in ids}})))
    with vault.lock():
        vault._atomic_write(vault.root / NPZ, buf.getvalue())


def _connect(root: Path, dim: int, write: bool = True):
    """The store, its settings checked (other ones empty it) and the .npz read into it once
    (the .npz is kept as a backup, never deleted). With `write` False (a dry run) nothing is
    made or changed: None when there is no store or it holds other settings."""
    p = root / CACHE
    if not write:
        if not p.is_file():
            return None
        con = vecstore.connect(p, create=False)
        if vecstore.get_meta(con, "settings") != _want(dim):
            con.close()
            return None
        return con
    con = vecstore.connect(p)
    try:
        vecstore.prepare(con, _want(dim))
        if not vecstore.get_meta(con, "migrated"):  # idempotent: a rerun upserts the same rows
            vecstore.put(con, {i: (json.dumps(list(e[0])), e[1], e[2], e[3])
                               for i, e in _npz_read(root).items() if e[3].shape[1] == dim})
            with vecstore.tx(con):
                vecstore.set_meta(con, "migrated", True)
    except BaseException:
        con.close()
        raise
    return con


def _sync(con, dim: int, st0, cache: dict) -> tuple[tuple, bool]:
    return vecstore.sync(con, dim, st0, cache, lambda m: tuple(json.loads(m)))


def _save(con, cache: dict, changed: set, gone) -> None:
    """Upsert the `changed` pages and delete the `gone` ones; a disposable cache, so a failure
    is a warning (a later call embeds what is missing)."""
    try:
        vecstore.delete(con, [i for i in gone if i not in cache])  # a changed page: upserted
        vecstore.put(con, {i: (json.dumps(list(cache[i][0])), *cache[i][1:])
                           for i in changed if i in cache})
    except sqlite3.Error as e:
        log.warning("vector cache not saved: %s", e)


def _read(root: Path) -> dict:
    """Every stored row: {id: (manifest, full, starts, float32 vectors)}; {} without a store."""
    if not (root / CACHE).is_file():
        return {}
    with closing(vecstore.connect(root / CACHE, create=False)) as con:
        dim = (vecstore.get_meta(con, "settings") or {}).get("dim")
        return {i: (tuple(json.loads(m)), full, s, v.astype(np.float32))
                for i, m, full, s, v in vecstore.rows(con, dim)} if dim else {}


def stamp(root: Path) -> tuple | None:
    """The store's change stamp, None when there is none or it is unreadable (the brain
    map's cache key)."""
    if not (root / CACHE).is_file():
        return None
    try:
        with closing(vecstore.connect(root / CACHE, create=False)) as con:
            return vecstore.stamp(con)
    except sqlite3.Error:
        return None


def heads(root: Path) -> tuple[list[str], np.ndarray]:
    """(ids, float32 chunk-0 vectors) of every stored page, read now (the brain map's input:
    one row per page, not the whole matrix). FileNotFoundError without a store."""
    if not (root / CACHE).is_file():
        raise FileNotFoundError(root / CACHE)
    with closing(vecstore.connect(root / CACHE, create=False)) as con:
        dim = (vecstore.get_meta(con, "settings") or {}).get("dim")
        return vecstore.heads(con, dim) if dim else ([], np.zeros((0, 0), np.float32))


def prune(vault) -> int:
    """Delete the rows of pages that went or changed, with no model needed (a forget or a
    settings change must not leave a page's vectors behind; a changed page is embedded again
    later); the .npz backup loses them too. Rows deleted from the store."""
    from cortex.catalog import Catalog

    root = vault.root
    if not ((root / CACHE).is_file() or (root / NPZ).is_file()):
        return 0
    pages = _pages(Index(vault, Catalog.load(vault)))
    n = 0
    if (root / CACHE).is_file():
        with closing(vecstore.connect(root / CACHE)) as con:
            n = vecstore.delete(con, [i for i, m in vecstore.manifests(con).items()
                                      if pages.get(i) != tuple(json.loads(m))])
    old = _npz_read(root)
    keep = {i: e for i, e in old.items() if pages.get(i) == e[0]}
    if len(keep) < len(old):
        _npz_write(vault, keep)
    return n


def _embed(embed, texts: list[str]) -> np.ndarray:
    v = np.asarray(embed(texts), dtype=np.float32)
    return v / np.where((n := np.linalg.norm(v, axis=1, keepdims=True)) > 0, n, 1.0)


def embed_all(embed, texts: list[str], batch: int = BATCH, say=None,
              what: str = "texts") -> np.ndarray:
    """`_embed` of many texts in batches, shortest first so a batch pads little (the vectors
    are the same; 1.4x faster on titles), returned in input order. `say` gets a counts-only
    line every PROGRESS_S seconds."""
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    parts, last = [], time.monotonic()
    for s in range(0, len(order), batch):
        parts.append(_embed(embed, [texts[i] for i in order[s:s + batch]]))
        if say and time.monotonic() - last >= PROGRESS_S:
            say(f"embedded {s + len(parts[-1])} of {len(texts)} {what}")
            last = time.monotonic()
    if not parts:
        return np.zeros((0, 1), np.float32)
    got = np.concatenate(parts)
    out = np.empty_like(got)
    out[order] = got
    return out


def vectors(index: Index, embed, dim: int, budget: float | None = None,
            save_every: float | None = None, progress=None, heads: float | None = None,
            only: set[str] | None = None, save: bool = True):
    """Bring the vault's cache up to date and return (page ids, row -> page index, row chunk
    start, unit row vectors), rows grouped by page.

    Chunk 0 of new and changed pages gets at most `heads` seconds per call (None: all of
    them); a page without it is left out of the rows until a later call or `cortex model
    build` embeds it. Gone pages are dropped. The other chunks get at most `budget` seconds
    per call (None: no limit, for `cortex model build`); until all of a page's chunks are
    built it scores by those it has.
    With `save_every` (seconds) the build saves as it goes, so an interrupted one resumes;
    `progress(done, total)` counts the chunks built in this call. With `only` (page ids) the
    missing chunk 0 of no other page is embedded in this call (the browse import needs the hub
    members only; recall and `cortex model build` embed the rest). With `save=False` (a dry
    run) nothing is written and the in-memory cache is left as it was.

    The store is read lazily: the whole of it on a process's first call, then only the rows
    other processes saved since (its change stamp); a save writes the changed pages only."""
    root = index.vault.root
    pages = _pages(index)
    st, cache, stack = _MEM.get(root, (None, {}, None))
    cache = dict(cache)
    con = None
    try:
        con = _connect(root, dim, save)
        if con is None:  # a dry run with no usable store: nothing cached
            st, cache, stack = None, {}, None
        else:
            st, moved = _sync(con, dim, st, cache)
            stack = None if moved else stack
    except (sqlite3.Error, OSError) as e:  # a disposable cache: embed what this call can
        log.warning("vector cache not read: %s", e)
        con = None
    try:
        return _vectors(index, embed, dim, pages, st, cache, stack, con, budget, save_every,
                        progress, heads, only, save)
    finally:
        if con is not None:
            con.close()


def _vectors(index: Index, embed, dim: int, pages: dict, st, cache: dict, stack, con, budget,
             save_every, progress, heads, only, save: bool):
    root = index.vault.root
    save = save and con is not None
    gone = [i for i, e in cache.items() if pages.get(i) != e[0]]  # went, changed or private now
    for i in gone:
        del cache[i]
    dirty, changed = bool(gone), set()
    todo = [i for i in pages if i not in cache and (only is None or i in only)]
    t0, step = time.monotonic(), (BATCH if heads is None else TAIL_BATCH)
    for s in range(0, len(todo), step):
        if heads is not None and time.monotonic() - t0 >= heads:
            break
        batch = todo[s:s + step]
        vecs = _embed(embed, [_text(index._notes[i]) for i in batch])
        for i, v in zip(batch, vecs, strict=True):
            cache[i] = (pages[i], False, np.zeros(1, np.int32), v[None])
        changed.update(batch)
        dirty = True
    pending = [i for i in pages if i in cache and not cache[i][1]]
    work = {}
    if progress:  # counted up front only for the CLI build
        work = {i: _chunks(index._notes[i]) for i in pending}
        total = sum(len(c) for c in work.values()) - sum(len(cache[i][2]) for i in pending)
    t0 = last = time.monotonic()
    done, step = 0, (BATCH if budget is None else TAIL_BATCH)
    for i in pending:
        meta, _, have, vecs = cache[i]
        built = set(have.tolist())
        rest = [c for c in work.pop(i, None) or _chunks(index._notes[i]) if c[0] not in built]
        if not rest:  # short page: chunk 0 is all of it
            cache[i], dirty = (meta, True, have, vecs), True
            changed.add(i)
        while rest and (budget is None or time.monotonic() - t0 < budget):
            part, rest = rest[:step], rest[step:]
            have = np.concatenate([have, np.array([s for s, _ in part], np.int32)])
            vecs = np.concatenate([vecs, _embed(embed, [t for _, t in part])])
            cache[i], dirty, done = (meta, not rest, have, vecs), True, done + len(part)
            changed.add(i)
            if progress:
                progress(done, total)
            if save and save_every is not None and time.monotonic() - last >= save_every:
                _save(con, cache, changed, gone)
                changed, gone = set(), []
                last = time.monotonic()
        if budget is not None and time.monotonic() - t0 >= budget:
            break
    if save and (changed or gone):
        _save(con, cache, changed, gone)
    if save and dirty:  # read back what this call and any other process saved since
        try:
            st = _sync(con, dim, st, cache)[0]
        except sqlite3.Error as e:
            log.warning("vector cache not read back: %s", e)
        for i in [i for i, e in cache.items() if pages.get(i) != e[0]]:
            del cache[i]  # another process's newer page: in the store, not in this index yet
    if dirty or stack is None:
        stack = vecstore.stack(cache, dim)
    if save:
        _MEM[root] = (st, cache, stack)
    return stack


def best_chunks(page: np.ndarray, start: np.ndarray, sims: np.ndarray,
                n: int) -> tuple[np.ndarray, np.ndarray]:
    """Per page (rows grouped by page; every page has chunk 0): the best chunk's cosine and
    its start offset."""
    if not n:
        return np.zeros(0, np.float32), np.zeros(0, np.int32)
    best = np.maximum.reduceat(sims, np.searchsorted(page, np.arange(n)))
    rows = np.flatnonzero(sims == best[page])
    _, first = np.unique(page[rows], return_index=True)
    return best, start[rows[first]]


def _terms(query: str) -> list[str]:
    """Distinct non-stopword FTS terms; short words match exactly: a prefix like "i"* would hit
    most of the vault."""
    return list(dict.fromkeys(f'"{t.lower()}"' + ("*" if len(t) > 3 else "")
                              for t in re.findall(r"\w+", query or "")
                              if t.lower() not in STOPWORDS))


def _cover(index: Index, query: str) -> dict[str, float]:
    """Page id -> share of the query's terms it matches (pages matching none are absent)."""
    terms = _terms(query)
    n: dict[str, int] = {}
    for t in terms:
        for (i,) in index.db.execute("SELECT id FROM fts WHERE fts MATCH ?", (t,)):
            n[i] = n.get(i, 0) + 1
    return {i: c / len(terms) for i, c in n.items()}


def _keyword(index: Index, query: str) -> dict[str, float]:
    """BM25 over the FTS index, any non-stopword term (OR), scaled so the best hit is 1."""
    terms = _terms(query)
    if not terms:
        return {}
    rows = index.db.execute("SELECT id, bm25(fts, 0, 0, 10, 8, 3, 1) FROM fts WHERE fts MATCH ?",
                            (" OR ".join(terms),)).fetchall()
    s = {i: -b for i, b in rows}
    top = max(s.values(), default=0.0)
    return {i: v / top for i, v in s.items()} if top > 0 else {}


def _neighbours(index: Index, id: str):
    return index.db.execute("SELECT dst, type FROM edges WHERE src = ?1 UNION "
                            "SELECT src, type FROM edges WHERE dst = ?1", (id,)).fetchall()


def recall(index: Index, query: str, embed=None, limit: int = 8, hops: int = 1,
           rerank_with=None, fuse: str = "weighted", chats: bool = False,
           quiet: dict[str, float] | None = None, explain: dict | None = None) -> list[dict]:
    """Hybrid hits (0.7 cosine + 0.3 BM25; keyword only when `embed` is None; a cosine under
    MIN_COS counts as 0, and such a page is a hit only through a keyword match of at least
    MIN_KW_COVER of the query's terms), plus graph
    neighbours of the seeds within `hops` (score x0.5 per hop, `via` names the edge). Up to a
    quarter of the `limit` slots go to linked pages. Returns
    [{id, kind, title, summary, score, match: similar|keyword|linked, via?, at?}], best first;
    `at` is the start of the page's best chunk in its filled body (absent for chunk 0).
    Imported chat pages (`is_chat`) score x CHAT_WEIGHT after the floors, unless `chats`;
    `quiet` ({id: weight}, harvest.quiet) overrides that weight, and weight 0 drops the page,
    from the graph neighbours too, even with `chats`. A STALE page scores x STALE_WEIGHT and a
    recent journal or chat page up to x(1 + RECENT_BOOST) (`_factors`).
    With `rerank_with` (a `load_reranker` scorer) the top RERANK_POOL are reordered by it.
    `explain` (a dict) is filled with the score parts for `explained`."""
    limit = max(1, min(MAX_LIMIT, int(limit)))
    hops = max(0, min(2, int(hops)))
    if rerank_with is not None:
        pool = _rank(index, query, embed, max(limit, RERANK_POOL), hops, fuse, chats, quiet,
                     explain)
        out = None if explain is None else explain.setdefault("rerank", {})
        return rerank(index, query, pool, rerank_with, chats, quiet, out)[:limit]
    return _rank(index, query, embed, limit, hops, fuse, chats, quiet, explain)


def _rank(index: Index, query: str, embed, limit: int, hops: int, fuse: str,
          chats: bool = False, quiet: dict[str, float] | None = None,
          explain: dict | None = None) -> list[dict]:
    kw = raw_kw = _keyword(index, query)
    cos: dict[str, float] = {}
    raw_cos: dict[str, float] = {}
    cover: dict[str, float] = {}
    at: dict[str, int] = {}  # page -> start of its best chunk, for the rerank
    use_cos = embed is not None and bool((query or "").strip())
    if use_cos:
        q = _unit(np.asarray(embed([QUERY_PREFIX + query])[0], dtype=np.float32))
        ids, page, start, m = vectors(index, embed, len(q), RECALL_BUDGET, heads=HEAD_BUDGET)
        best, where = best_chunks(page, start, m @ q, len(ids))
        at = dict(zip(ids, where.tolist(), strict=True))
        # under the floor a page is only a hit through a keyword match of most query terms,
        # so one shared common word ("best") is no hit, while an exact id or title still is
        raw_cos = dict(zip(ids, best.tolist(), strict=True))
        cos = {i: c for i, c in raw_cos.items() if c >= MIN_COS}
        cover = _cover(index, query) if kw else {}
        kw = {i: v for i, v in kw.items() if i in cos or cover.get(i, 0.0) >= MIN_KW_COVER}
    w_cos, w_kw = (W_COS, W_KW) if use_cos else (0.0, 1.0)
    score = {i: w_cos * cos.get(i, 0.0) + w_kw * kw.get(i, 0.0)
             for i in set(cos) | set(kw) if i in index._notes}
    if fuse == "rrf":  # reciprocal rank fusion of the two lists (eval only)
        score = dict.fromkeys(score, 0.0)
        for d in (cos, kw):
            for r, i in enumerate(sorted(d, key=lambda i: (-d[i], i))):
                if i in score:
                    score[i] += 1 / (RRF_K + r + 1)
    # after the floors, so a chat page that is the only match still shows
    score = {i: sc * w for i, sc in score.items() if (w := _weight(index, i, chats, quiet))}
    if explain is not None:
        explain.update(cos=raw_cos, kw=raw_kw, cover=cover, at=at, score=score, chats=chats,
                       quiet=quiet, w=(w_cos, w_kw))

    def match(i):
        return "keyword" if w_kw * kw.get(i, 0.0) > w_cos * cos.get(i, 0.0) else "similar"

    ranked = sorted((i for i in score if score[i] > 0), key=lambda i: (-score[i], i))[:limit]
    n_linked = min(max(1, limit // 4), limit - 1) if hops else 0
    kept, spare = ranked[:limit - n_linked], ranked[limit - n_linked:]
    hits = {i: (score[i], match(i), None) for i in kept}
    linked: dict[str, tuple[float, str]] = {}
    for seed in kept:
        seen, frontier = {seed}, [seed]
        for h in range(1, hops + 1):
            nxt = []
            for f in frontier:
                for n, typ in _neighbours(index, f):
                    if n in seen or n not in index._notes or (quiet or {}).get(n) == 0:
                        continue
                    seen.add(n)
                    nxt.append(n)
                    sc = score[seed] * DECAY ** h
                    if n not in hits and sc > linked.get(n, (0.0, ""))[0]:
                        linked[n] = (sc, f"{f} ({typ})")
            frontier = nxt
    for i, (sc, via) in sorted(linked.items(), key=lambda kv: (-kv[1][0], kv[0]))[:n_linked]:
        hits[i] = (score[i], match(i), None) if score.get(i, 0.0) > sc else (sc, "linked", via)
    for i in spare:
        if len(hits) >= limit:
            break
        hits.setdefault(i, (score[i], match(i), None))
    out = []
    for i, (sc, how, via) in sorted(hits.items(), key=lambda kv: (-kv[1][0], kv[0])):
        n = index._notes[i]
        summary = n["fields"].get("summary")
        out.append({"id": i, "kind": n["kind"], "title": n["title"],
                    "summary": _one_line(str(summary)) if summary else "",
                    "score": round(sc, 3), "match": how, **({"via": via} if via else {}),
                    **({"at": at[i]} if at.get(i) else {})})
    return out


def search(index: Index, query: str, limit: int = 8, hops: int = 1,
           rerank: bool = True, chats: bool = False,
           quiet: dict[str, float] | None = None,
           explain: dict | None = None) -> tuple[list[dict], str]:
    """`recall` with the downloaded model (and the reranker when present, on and `rerank`);
    without the embedder, keyword + graph and the NOT_SET_UP notice (never a download)."""
    embed = load_embedder()
    ce = load_reranker() if rerank else None
    return recall(index, query, embed, limit, hops, ce, chats=chats, quiet=quiet,
                  explain=explain), \
        "" if embed else NOT_SET_UP


def explained(index: Index, explain: dict, hits: list[dict],
              misses: int = 0) -> tuple[list[dict], list[dict]]:
    """The score parts of each hit, and of the `misses` best pages that missed the cut, from a
    `recall(explain=...)` dict: {id, cos (raw best-chunk cosine, None without the model), kw
    (BM25 scaled to the best hit), cover (share of query terms), weight, factors (`_factors`,
    whose product is the weight), score, rerank?, at,
    chunk (its text without the title line), via?, reason (misses only)}. Read-only."""
    raw_cos, kw, cover = explain["cos"], explain["kw"], explain["cover"]
    score, (w_cos, w_kw), rr = explain["score"], explain["w"], explain.get("rerank", {})

    def weight(i: str) -> float:
        return _weight(index, i, explain["chats"], explain["quiet"]) or 0.0

    def parts(i: str, h: dict | None = None) -> dict:
        at = explain["at"].get(i, 0)
        chunk = _chunk_text(index._notes[i], at).split("\n", 1)[-1] if raw_cos else ""
        f = _factors(index, i, explain["chats"], explain["quiet"])
        return {"id": i, "cos": raw_cos.get(i), "kw": kw.get(i, 0.0), "cover": cover.get(i),
                "weight": weight(i), "factors": {k: round(v, 3) for k, v in f.items()},
                "score": h["score"] if h else round(score.get(i, 0.0), 3),
                **({"rerank": round(rr[i], 3)} if i in rr else {}), "at": at,
                "chunk": _one_line(chunk)[:80], **({"via": h["via"]} if h and "via" in h else {})}

    def reason(p: dict) -> str:
        if not p["weight"]:
            return "hidden (harvest quiet weight 0)"
        if p["id"] in score:
            return "below limit" + (" after rerank" if rr else "")
        why = [] if p["cos"] is None else [f"cosine {p['cos']:.3f} < MIN_COS {MIN_COS}"]
        if p["kw"]:
            why.append(f"keyword cover {p['cover'] or 0.0:.2f} < MIN_KW_COVER {MIN_KW_COVER}")
        return ", ".join(why) or "no signal"

    def would(i: str) -> float:  # the fused score without the floors: the order of the misses
        return (w_cos * raw_cos.get(i, 0.0) + w_kw * kw.get(i, 0.0)) * weight(i)

    shown = {h["id"] for h in hits}
    cand = sorted((i for i in set(raw_cos) | set(kw) if i in index._notes and i not in shown),
                  key=lambda i: (-would(i), i))[:misses]
    return ([parts(h["id"], h) for h in hits],
            [{**(p := parts(i)), "reason": reason(p)} for i in cand])
