"""Private browse search for local AIs (browse-brain.md, "Private search for local AIs").

Embeds every `browse/**/*.md` page, `browse/private/` included, into its own cache
`.cortex/browse/vectors-private.npz`, apart from the main index's `.cortex/vectors-v3.npz`
(which never holds a private page). Only `cortex mcp --private` searches it, and that server
refuses to start unless `browse.private_mcp` is on. The embedder is semantic's (fastembed in
~/.cortex/models, never downloaded here; tests inject a fake one as `semantic._embedder`).
A page is cut into line-aligned chunks and scores by its best one; pages are re-embedded only
when their content hash changes, and removed pages drop out.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import time

import numpy as np

from cortex import semantic
from cortex.vault import Vault, VaultError, _split

log = logging.getLogger(__name__)

CACHE = ".cortex/browse/vectors-private.npz"
CHUNK_CHARS = 1000
SNIPPET = 240
VERSION = 1  # bump when the chunking changes, so the cache is rebuilt
MAX_K = 20
BATCH = 16  # long chunks: as fast as 64 a call, and a progress line every few seconds


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


def _read(vault: Vault, dim: int | None = None) -> dict:
    """{rel path: (hash, starts, unit vectors)}, or {} when missing or built otherwise."""
    try:
        with np.load(vault.root / CACHE, allow_pickle=False) as z:
            meta = json.loads(z["meta"].item())
            if [meta.get("model"), meta.get("version")] != [semantic.MODEL, VERSION] or (
                    dim is not None and meta.get("dim") != dim):
                return {}
            paths, page = [str(p) for p in z["paths"]], z["page"]
            start, vecs = z["start"], z["vecs"].astype(np.float32)
            cut = np.searchsorted(page, np.arange(len(paths) + 1))
            return {p: (meta["hashes"][k], start[cut[k]:cut[k + 1]], vecs[cut[k]:cut[k + 1]])
                    for k, p in enumerate(paths)}
    except (OSError, ValueError, KeyError) as e:
        if not isinstance(e, FileNotFoundError):
            log.warning("rebuilding the private browse vectors: %s", type(e).__name__)
    return {}


def _write(vault: Vault, cache: dict, dim: int) -> None:
    paths = list(cache)
    rows = [cache[p] for p in paths]
    buf = io.BytesIO()
    np.savez(buf, paths=np.array(paths, dtype=str),
             page=np.repeat(np.arange(len(paths), dtype=np.int32), [len(e[1]) for e in rows]),
             start=np.concatenate([e[1] for e in rows] or [np.zeros(0, np.int32)]),
             vecs=np.concatenate([e[2] for e in rows] or [np.zeros((0, dim))]).astype(np.float16),
             meta=np.array(json.dumps({"model": semantic.MODEL, "version": VERSION, "dim": dim,
                                       "hashes": [e[0] for e in rows]})))
    with vault.lock():  # the lock creates .cortex/
        (vault.root / CACHE).parent.mkdir(parents=True, exist_ok=True)
        vault._atomic_write(vault.root / CACHE, buf.getvalue())


def update(vault: Vault, progress=None) -> dict:
    """Bring the private browse vectors up to date. Returns counts only: {pages, embedded,
    removed, chunks}; `progress(str)` gets counts-only lines. Raises VaultError
    `not_set_up` when the embedding model is not downloaded."""
    embed = semantic.load_embedder()
    if embed is None:
        raise VaultError("not_set_up", semantic.NOT_SET_UP)
    say = progress or (lambda s: None)
    say("private search: reading the browse pages")
    dim = semantic._embed(embed, ["cortex"]).shape[1]  # a cache from another embedder is redone
    files = _files(vault, say)
    old = _read(vault, dim)
    keep = {p: e for p, e in old.items() if p in files and files[p][0] == e[0]}
    todo = [p for p in files if p not in keep]
    removed = sum(p not in files for p in old)
    plan = []  # (path, chunk starts, chunk texts): embedded in batches across pages
    for p in todo:
        title, body = _body(files[p][1])
        starts = _chunks(body)
        ends = [*starts[1:], len(body)]
        plan.append((p, starts, [f"{title}\n{body[s:e]}"
                                 for s, e in zip(starts, ends, strict=True)]))
    texts = [t for _, _, ts in plan for t in ts]
    say(f"{len(files)} browse pages; {len(todo)} to embed ({len(texts)} chunks), "
        f"{removed} removed")
    vecs = semantic.embed_all(embed, texts, BATCH, say, "chunks")
    cache, at = dict(keep), 0
    for p, starts, ts in plan:  # every page has at least one chunk
        cache[p] = (files[p][0], np.array(starts, dtype=np.int32), vecs[at:at + len(ts)])
        at += len(ts)
    if todo or removed or not (vault.root / CACHE).is_file():
        _write(vault, cache, dim)
    out = {"pages": len(files), "embedded": len(todo), "removed": removed,
           "chunks": sum(len(e[1]) for e in cache.values())}
    say(f"done: {out['pages']} pages, {out['chunks']} chunks")
    return out


def search(vault: Vault, query: str, k: int = 8) -> list[dict]:
    """Top `k` browse pages for `query`: [{path, title, score, snippet, text}], best first;
    `text` is the best chunk (CHUNK_CHARS). Updates the vectors first. Title, snippet and text
    are untrusted page text: wrap them before a model reads them."""
    if not isinstance(query, str) or not query.strip():
        return []
    update(vault)
    cache = _read(vault)
    if not cache:
        return []
    embed = semantic.load_embedder()
    q = semantic._embed(embed, [semantic.QUERY_PREFIX + query.strip()])[0]
    scored = []
    for p, (_, starts, vecs) in cache.items():
        sims = vecs @ q
        best = int(np.argmax(sims))
        scored.append((float(sims[best]), p, int(starts[best])))
    scored.sort(key=lambda t: -t[0])
    out = []
    for score, p, at in scored[:max(1, min(int(k), MAX_K))]:
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
