"""Private browse search for local AIs (browse-brain.md, "Private search for local AIs").

Embeds every `browse/**/*.md` page, `browse/private/` included, into its own store
`.cortex/browse/vectors-private.db` (`cortex.vecstore`: a row per page path with its content
hash), apart from the main index's `.cortex/vectors.db` (which never holds a private page).
Only `cortex mcp --private` searches it, and that server refuses to start unless
`browse.private_mcp` is on. The embedder is semantic's (fastembed in ~/.cortex/models, never
downloaded here; tests inject a fake one as `semantic._embedder`). A page is cut into
line-aligned chunks and scores by its best one; pages are re-embedded only when their content
hash changes, and removed pages drop out. An update saves every GROUP chunks, so one stopped by
its budget (the background runs) goes on where it stopped. The older
`.cortex/browse/vectors-private.npz` is read into the store once and kept as a backup.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import sqlite3
import time
from contextlib import closing

import numpy as np

from cortex import semantic, vecstore
from cortex.vault import Vault, VaultError, _split

log = logging.getLogger(__name__)

CACHE = ".cortex/browse/vectors-private.db"
NPZ = ".cortex/browse/vectors-private.npz"  # the cache before the store: read once, a backup
CHUNK_CHARS = 1000
SNIPPET = 240
VERSION = 1  # bump when the chunking changes, so the cache is rebuilt
MAX_K = 20
BATCH = 16  # long chunks: as fast as 64 a call, and a progress line every few seconds
GROUP = 256  # chunks embedded between saves
# vault root -> (the store's stamp as last seen, {path: (hash, full, starts, vecs)}, stack)
_MEM: dict = {}


def _body(text: str) -> tuple[str, str]:
    """(title, body) of a page: the `title` field, else the first `# ` heading."""
    parts = _split(text)
    body = parts[3] if parts else text
    title = ""
    if parts:
        for line in parts[1].splitlines():
            if line.startswith("title:"):
                title = line[6:].strip().strip("'\"")
                break
    if not title:
        title = next((ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")), "")
    return title, body


def _chunks(body: str) -> list[int]:
    """Start offsets of line-aligned chunks of about CHUNK_CHARS (always at least one)."""
    starts, at, size = [0], 0, 0
    for line in body.splitlines(keepends=True):
        if size and size + len(line) > CHUNK_CHARS:
            starts.append(at)
            size = 0
        at += len(line)
        size += len(line)
    return starts


def _files(vault: Vault, say=None) -> dict[str, tuple[str, str]]:
    """{rel path: (sha256, text)} of every browse page."""
    out, top = {}, vault.root / "browse"
    last = time.monotonic()
    for p in sorted(top.rglob("*.md")) if top.is_dir() else []:
        if say and time.monotonic() - last >= semantic.PROGRESS_S:
            say(f"private search: {len(out)} browse pages read")
            last = time.monotonic()
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        rel = p.relative_to(vault.root).as_posix()
        out[rel] = (hashlib.sha256(raw).hexdigest(), raw.decode("utf-8", errors="replace"))
    return out


def signature(vault: Vault) -> str:
    """A cheap fingerprint of the browse pages (path, size and mtime of each; no read, no
    hash), stored with the vectors so a search re-reads the pages only when it changed
    (review C12)."""
    top = vault.root / "browse"
    rows = []
    for p in sorted(top.rglob("*.md")) if top.is_dir() else []:
        try:
            st = p.stat()
        except OSError:
            continue
        rows.append((p.relative_to(vault.root).as_posix(), st.st_size, st.st_mtime_ns))
    return hashlib.sha256(json.dumps(rows).encode("utf-8")).hexdigest()[:20]


def _want(dim: int) -> dict:
    return {"model": semantic.MODEL, "version": VERSION, "dim": dim}


def _ours(con, dim: int | None = None) -> int | None:
    """The store's dim when its rows were made by this model and chunking (and `dim`)."""
    s = vecstore.get_meta(con, "settings") or {}
    ok = [s.get("model"), s.get("version")] == [semantic.MODEL, VERSION]
    return s.get("dim") if ok and dim in (None, s.get("dim")) else None


def _npz(vault: Vault, dim: int | None = None) -> tuple[dict, str | None, int | None]:
    """The .npz cache from before the store: ({path: (hash, starts, vecs)}, its signature,
    its dim), or ({}, None, None) when missing, unreadable or built otherwise."""
    try:
        with np.load(vault.root / NPZ, allow_pickle=False) as z:
            meta = json.loads(z["meta"].item())
            if [meta.get("model"), meta.get("version")] != [semantic.MODEL, VERSION] or (
                    dim is not None and meta.get("dim") != dim):
                return {}, None, None
            paths, page = [str(p) for p in z["paths"]], z["page"]
            start, vecs = z["start"], z["vecs"]
            cut = np.searchsorted(page, np.arange(len(paths) + 1))
            return ({p: (meta["hashes"][k], start[cut[k]:cut[k + 1]], vecs[cut[k]:cut[k + 1]])
                     for k, p in enumerate(paths)}, meta.get("sig"), meta.get("dim"))
    except (OSError, ValueError, KeyError) as e:
        if not isinstance(e, FileNotFoundError):
            log.warning("the old private browse vectors are unreadable: %s", type(e).__name__)
    return {}, None, None


def _npz_write(vault: Vault, cache: dict, dim: int, sig: str | None = None) -> None:
    """Rewrite the .npz backup with `cache` ({path: (hash, starts, vecs)}) only."""
    paths = list(cache)
    rows = [cache[p] for p in paths]
    buf = io.BytesIO()
    np.savez(buf, paths=np.array(paths, dtype=str),
             page=np.repeat(np.arange(len(paths), dtype=np.int32), [len(e[1]) for e in rows]),
             start=np.concatenate([e[1] for e in rows] or [np.zeros(0, np.int32)]),
             vecs=np.concatenate([e[2] for e in rows] or [np.zeros((0, dim))]).astype(np.float16),
             meta=np.array(json.dumps({"model": semantic.MODEL, "version": VERSION, "dim": dim,
                                       "hashes": [e[0] for e in rows], "sig": sig})))
    with vault.lock():
        vault._atomic_write(vault.root / NPZ, buf.getvalue())


def _connect(vault: Vault, dim: int):
    """The store for `dim` (other settings empty it and its signature) with the .npz read into
    it once (the .npz is kept as a backup, never deleted)."""
    con = vecstore.connect(vault.root / CACHE)
    try:
        if vecstore.prepare(con, _want(dim)):
            with vecstore.tx(con):
                vecstore.set_meta(con, "sig", None)
        if not vecstore.get_meta(con, "migrated"):  # idempotent: a rerun upserts the same rows
            old, sig, _ = _npz(vault, dim)
            vecstore.put(con, {p: (h, True, s, v) for p, (h, s, v) in old.items()})
            with vecstore.tx(con):
                if old:
                    vecstore.set_meta(con, "sig", sig)
                vecstore.set_meta(con, "migrated", True)
    except BaseException:
        con.close()
        raise
    return con


def _load(vault: Vault, dim: int | None = None) -> tuple[dict, tuple | None]:
    """({path: (hash, full, starts, vecs)}, stacked rows) as stored; this process keeps them
    and reads only what other processes saved since. ({}, None) when there is no store or it
    was built otherwise."""
    root, p = vault.root, vault.root / CACHE
    if not p.is_file():
        _MEM.pop(root, None)
        return {}, None
    st, cache, stack = _MEM.get(root, (None, {}, None))
    cache = dict(cache)
    try:
        with closing(vecstore.connect(p, create=False)) as con:
            if (d := _ours(con, dim)) is None:
                return {}, None
            st, moved = vecstore.sync(con, d, st, cache, str)
    except sqlite3.DatabaseError as e:
        log.warning("private browse vectors not read: %s", type(e).__name__)
        return {}, None
    if moved or stack is None:
        stack = vecstore.stack(cache, d)
    _MEM[root] = (st, cache, stack)
    return cache, stack


def _stored_signature(vault: Vault) -> str | None:
    if not (vault.root / CACHE).is_file():
        return None
    try:
        with closing(vecstore.connect(vault.root / CACHE, create=False)) as con:
            return vecstore.get_meta(con, "sig") if _ours(con) else None
    except sqlite3.DatabaseError:
        return None


def _read(vault: Vault, dim: int | None = None) -> dict:
    """{rel path: (hash, starts, unit vectors)}, or {} when missing or built otherwise."""
    return {p: (e[0], e[2], e[3]) for p, e in _load(vault, dim)[0].items()}


def update(vault: Vault, progress=None, budget: float | None = None, gate=None) -> dict:
    """Bring the private browse vectors up to date. Returns counts only: {pages, embedded,
    removed, chunks}, and `left` (pages still to embed) when `budget` (seconds) ran out or
    `gate()` turned False; what was embedded is saved, so the next run goes on.
    `progress(str)` gets counts-only lines. Raises VaultError `not_set_up` when the embedding
    model is not downloaded."""
    embed = semantic.load_embedder()
    if embed is None:
        raise VaultError("not_set_up", semantic.NOT_SET_UP)
    say = progress or (lambda s: None)
    say("private search: reading the browse pages")
    dim = semantic._embed(embed, ["cortex"]).shape[1]  # a cache from another embedder is redone
    sig = signature(vault)  # before reading: a page changed meanwhile is read again next time
    files = _files(vault, say)
    with closing(_connect(vault, dim)) as con:
        old = vecstore.manifests(con)
        gone = [p for p in old if p not in files]
        todo = [p for p in files if old.get(p) != files[p][0]]
        vecstore.delete(con, gone)
        plan = []  # (path, chunk starts, chunk texts): embedded in groups across pages
        for p in todo:
            title, body = _body(files[p][1])
            starts = _chunks(body)
            ends = [*starts[1:], len(body)]
            plan.append((p, starts, [f"{title}\n{body[s:e]}"
                                     for s, e in zip(starts, ends, strict=True)]))
        total = sum(len(ts) for _, _, ts in plan)
        say(f"{len(files)} browse pages; {len(todo)} to embed ({total} chunks), "
            f"{len(gone)} removed")
        t0 = last = time.monotonic()
        done = pages = 0
        while pages < len(plan):
            if pages and ((budget is not None and time.monotonic() - t0 >= budget)
                          or (gate is not None and not gate())):
                break
            group, n = [], 0
            while pages + len(group) < len(plan) and (not group or n < GROUP):
                group.append(plan[pages + len(group)])
                n += len(group[-1][2])
            vecs = semantic.embed_all(embed, [t for _, _, ts in group for t in ts], BATCH)
            at, items = 0, {}
            for p, starts, ts in group:  # every page has at least one chunk
                items[p] = (files[p][0], True, np.array(starts, np.int32), vecs[at:at + len(ts)])
                at += len(ts)
            vecstore.put(con, items)
            pages, done = pages + len(group), done + n
            if time.monotonic() - last >= semantic.PROGRESS_S:
                say(f"embedded {done} of {total} chunks")
                last = time.monotonic()
        left = len(plan) - pages
        if not left and vecstore.get_meta(con, "sig") != sig:
            with vecstore.tx(con):
                vecstore.set_meta(con, "sig", sig)
        chunks = con.execute("SELECT COALESCE(SUM(n), 0) FROM vecs").fetchone()[0]
    out = {"pages": len(files), "embedded": pages, "removed": len(gone), "chunks": chunks,
           **({"left": left} if left else {})}
    say(f"done: {out['pages']} pages, {out['chunks']} chunks"
        + (f", {left} pages left for the next run" if left else ""))
    return out


def prune(vault: Vault) -> int:
    """Drop the rows of pages that went or changed, with no model needed (a forget or a
    settings change must not leave a page's vectors and path behind); the .npz backup loses
    them too. Rows dropped from the store."""
    root, n, files = vault.root, 0, None
    if (root / CACHE).is_file():
        with closing(vecstore.connect(root / CACHE)) as con:
            if have := vecstore.manifests(con):
                files = _files(vault)
                n = vecstore.delete(con, [p for p, h in have.items()
                                          if p not in files or files[p][0] != h])
    old, sig, dim = _npz(vault)
    if old:
        files = _files(vault) if files is None else files
        keep = {p: e for p, e in old.items() if p in files and files[p][0] == e[0]}
        if len(keep) < len(old):
            _npz_write(vault, keep, dim, sig)
    return n


def search(vault: Vault, query: str, k: int = 8) -> list[dict]:
    """Top `k` browse pages for `query`: [{path, title, score, snippet, text}], best first;
    `text` is the best chunk (CHUNK_CHARS). Updates the vectors first when the pages'
    signature changed. Title, snippet and text are untrusted page text: wrap them before a
    model reads them."""
    if not isinstance(query, str) or not query.strip():
        return []
    if _stored_signature(vault) != signature(vault):
        update(vault)
    cache, stack = _load(vault)
    if not cache:
        return []
    embed = semantic.load_embedder()
    if embed is None:
        raise VaultError("not_set_up", semantic.NOT_SET_UP)
    q = semantic._embed(embed, [semantic.QUERY_PREFIX + query.strip()])[0]
    ids, page, start, m = stack
    best, where = semantic.best_chunks(page, start, m @ q, len(ids))
    out = []
    for j in np.argsort(-best, kind="stable")[:max(1, min(int(k), MAX_K))]:
        score, p, at = float(best[j]), ids[j], int(where[j])
        if score <= 0:
            break
        try:
            title, body = _body((vault.root / p).read_bytes().decode("utf-8", errors="replace"))
        except OSError:
            continue
        snippet = " ".join(body[at:at + SNIPPET * 2].split())[:SNIPPET]
        out.append({"path": p, "title": title or p.rsplit("/", 1)[-1][:-3],
                    "score": round(score, 3), "snippet": snippet,
                    "text": body[at:at + CHUNK_CHARS].strip()})
    return out
