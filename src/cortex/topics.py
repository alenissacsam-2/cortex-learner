"""The topic tree behind `cortex topics` (docs/design/v5-research/REVIEW-1005.md §5).

A hub is a `kind: note` page tagged `topic`; its parent is the first hub in its `related`, and
the root is `my-topic-map`. A resource's or note's topics are the hub ids in its `related`,
mirrored by its body `Topics:` line (frontmatter wins). `attach` files a page under the hub
nearest its chunk-0 vector, backing off to the lowest common ancestor of the hubs within
`margin` of the best. `build` rewrites only the generated hub parts (the `Part of` line under
the H1, `## Sub-topics` / `## Subtopics` / `## Areas` and `## Conversations` / `## Other
conversations`) and the month pages' `## Links`; everything else on a hub belongs to the user or
the assistant. Private pages are never members, never listed and never edited. Every write goes
through `cortex.vault`.

Split proposals (REVIEW-1005 §6): `propose_split` clusters a big hub's direct pages (spherical
k-means, k by mean cosine silhouette) and labels each cluster by c-TF-IDF over member titles; the
assistant names the clusters and `apply_split` creates the child hubs and moves the pages.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from cortex.catalog import slugify
from cortex.index import STOPWORDS, Index, _edge_targets, _target
from cortex.relink import TOPIC_MAP as ROOT
from cortex.vault import (
    HEADING_RE,
    Vault,
    VaultError,
    _dump_fm,
    _load_fm,
    _newline,
    _section,
    _split,
    _to_yaml,
    invalid,
    is_private,
    scan_secrets,
)

log = logging.getLogger(__name__)

MARGIN = 0.02  # REVIEW-1005 §1.7: 72% attached at 0.93 precision
FLOOR = 0.80  # the top-1 cosine p50 is 0.87, min 0.68
TOUCH_FLOOR = 0.71  # prime's bare prompt vector to a hub centroid; see arena/touch-final.md
MIN_MEMBERS = 3
MAX_HUBS = 4
SPLIT_N = 25  # spec 3: a hub with more direct pages is a split candidate
MIN_CHILD = 4  # a smaller cluster's members stay on the parent
MAX_K = 8
MAX_SHARE = 0.45  # a bigger cluster gets one k+1 retry
SIL_MIN = 0.06  # p25 of `cortex topics eval --splits`: 0.058 over 68 hand splits, 2026-11-15
MAX_DEPTH = 4  # no proposals for hubs this deep
RESTARTS, ITERS = 10, 30
LABEL_STOP = STOPWORDS | frozenset("about into using vs via not new".split())
PAGE_KINDS = ("resource", "note")
CONV = ("Conversations", "Other conversations")
WATCHED = "Watched & searched"
TOP_RE = re.compile(r"^Topics: [^\r\n]*", re.M)
TL_RE = re.compile(r"^Timeline: [^\r\n]*", re.M)
DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
MONTH_RE = re.compile(r"^\d{4}-\d{2}-01$")


# ---- the tree ---------------------------------------------------------------------------

@dataclass
class Tree:
    parent: dict[str, str | None]  # every hub; None at the top
    kids: dict[str, list[str]]
    title: dict[str, str]

    def path(self, id: str) -> list[str]:
        """Top hub .. id."""
        out: list[str] = []
        x: str | None = id
        while x is not None and x not in out:
            out.append(x)
            x = self.parent.get(x)
        return out[::-1]

    def depth(self, id: str) -> int:
        return len(self.path(id)) - 1

    def lca(self, ids) -> str | None:
        out = None
        for level in zip(*(self.path(i) for i in ids), strict=False):
            if len(set(level)) != 1:
                break
            out = level[0]
        return out


def is_hub(n: dict) -> bool:
    tags = n["fields"].get("tags")
    return n["kind"] == "note" and isinstance(tags, list) and "topic" in tags


def tree(index: Index) -> Tree:
    hubs = {i: n for i, n in index._notes.items() if is_hub(n)}
    parent: dict[str, str | None] = {}
    for i, n in hubs.items():
        rel = n["fields"].get("related") or []
        parent[i] = None if i == ROOT else next(
            (t for t in _edge_targets(rel) if t in hubs and t != i), None)
    for i in sorted(hubs):  # break cycles at the first repeat
        seen, x = [i], parent[i]
        while x is not None:
            if x in seen:
                parent[seen[-1]] = None
                break
            seen.append(x)
            x = parent[x]
    kids: dict[str, list[str]] = defaultdict(list)
    for i in sorted(hubs):
        if parent[i] is not None:
            kids[parent[i]].append(i)
    return Tree(parent, dict(kids), {i: n["title"] for i, n in hubs.items()})


def page_hubs(n: dict, hubs) -> list[str]:
    return list(dict.fromkeys(t for t in _edge_targets(n["fields"].get("related") or [])
                              if t in hubs))


def _pages(index: Index):
    """Non-private resources and notes that are not hubs (the index holds no private page)."""
    return ((i, n) for i, n in sorted(index._notes.items())
            if n["kind"] in PAGE_KINDS and not is_hub(n))


def members(index: Index, t: Tree) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for i, n in _pages(index):
        for h in page_hubs(n, t.parent):
            out[h].add(i)
    return dict(out)


def unfiled(index: Index, t: Tree) -> list[str]:
    return [i for i, n in _pages(index) if not page_hubs(n, t.parent)]


# ---- attach -------------------------------------------------------------------------------

def heads(stack) -> dict[str, np.ndarray]:
    """Page id -> its chunk-0 unit vector (the first row per page in `semantic.vectors`)."""
    ids, page, _, vecs = stack
    first = np.searchsorted(page, np.arange(len(ids)))
    return {i: vecs[r] for i, r in zip(ids, first.tolist(), strict=True)}


def centroids(index: Index, t: Tree, stack):
    """(hub ids, member chunk-0 sums (h x d), member counts) for hubs with MIN_MEMBERS."""
    hv = heads(stack)
    ids, sums, counts = [], [], []
    for h, ps in sorted(members(index, t).items()):
        vs = [hv[p] for p in sorted(ps) if p in hv]
        if len(vs) >= MIN_MEMBERS:
            ids.append(h)
            sums.append(np.sum(vs, axis=0))
            counts.append(len(vs))
    dim = stack[3].shape[1]
    return ids, np.asarray(sums, np.float32).reshape(len(ids), dim), np.asarray(counts)


@dataclass
class Attach:
    hub: str | None  # None: not attached
    score: float  # the best hub's cosine
    near: list[tuple[str, float]]  # hubs within margin of the best
    top: list[tuple[str, float]]  # the best 3 hubs, for suggestions


def attach(vec, t: Tree, cents, exclude=(), margin: float = MARGIN,
           floor: float = FLOOR) -> Attach:
    """The hub for unit vector `vec`: the LCA of every hub within `margin` of the best cosine
    to a member centroid; none under `floor` or at the root. A hub in `exclude` (the page's
    own) loses the page's vector first (leave-one-out)."""
    ids, sums, counts = cents
    if not ids:
        return Attach(None, 0.0, [], [])
    if exclude:
        sums, counts = sums.copy(), counts.copy()
        for k, h in enumerate(ids):
            if h in exclude:
                sums[k] -= vec
                counts[k] -= 1
    norms = np.linalg.norm(sums, axis=1)
    ok = (counts >= MIN_MEMBERS) & (norms > 1e-9)
    cos = np.where(ok, sums @ vec / np.where(ok, norms, 1.0), -np.inf)
    order = [k for k in np.argsort(-cos, kind="stable").tolist() if ok[k]]
    if not order:
        return Attach(None, 0.0, [], [])
    best = float(cos[order[0]])
    near = [(ids[k], float(cos[k])) for k in order if cos[k] >= best - margin - 1e-9]
    top = [(ids[k], float(cos[k])) for k in order[:3]]
    hub = t.lca([h for h, _ in near]) if best >= floor else None
    return Attach(None if hub in (None, ROOT) else hub, best, near, top)


def head_stack(index: Index, embed):
    """`semantic.vectors` with every page's chunk 0 and no extra chunks."""
    from cortex import semantic

    dim = len(semantic._embed(embed, ["topic"])[0])
    return semantic.vectors(index, embed, dim, budget=0.0)


def attach_text(index: Index, embed, text: str) -> Attach:
    """The hub `text` belongs under (also Part 1's hook for linking a topic touch)."""
    from cortex import semantic

    vec = semantic._embed(embed, [text])[0]
    t = tree(index)
    stack = semantic.vectors(index, embed, len(vec), budget=0.0)
    return attach(vec, t, centroids(index, t, stack))


_PRIMED: tuple = (None, None, None)  # prompt_hub's (vector stack, tree, centroids)


def prompt_hub(index: Index, embed, prompt: str, hits: list[dict],
               save: bool = True) -> str | None:
    """The hub a chat prompt is about (cortex_prime's `touch`), or None. Two views must agree:
    `attach` of the prompt's bare vector to the member centroids (floor TOUCH_FLOOR, the same
    margin and LCA back-off) and recall, where a hit that is not a mere graph neighbour must be
    that hub, under it, or filed at or under it. No embedder or no hits, no hub. With `save`
    False (the app's dry run) the vector cache is read, never written."""
    from cortex import semantic

    global _PRIMED
    if embed is None or not hits:
        return None
    # bare, like the page vectors: the bge query prefix compresses the lead of the best hub
    q = semantic._embed(embed, [prompt])[0]
    # the recall that made `hits` just brought the vectors up to date; vectors() again costs
    # ~0.5 s on 5k pages (its manifest walk), and tree + centroids another ~60 ms
    stack = semantic._MEM.get(index.vault.root, (None, None, None))[2]
    if stack is None or stack[3].shape[1] != len(q):
        stack = semantic.vectors(index, embed, len(q), budget=0.0, save=save)
    if _PRIMED[0] is not stack:  # any page change re-embeds that page, so a new stack
        t = tree(index)
        _PRIMED = (stack, t, centroids(index, t, stack))
    _, t, cents = _PRIMED
    hub = attach(q, t, cents, floor=TOUCH_FLOOR).hub
    for h in hits if hub else ():
        n = index._notes.get(h["id"])
        if n is None or h.get("match") == "linked":
            continue
        if any(hub in t.path(x) for x in ([h["id"]] if is_hub(n) else page_hubs(n, t.parent))):
            return hub
    return None


def attach_unfiled(index: Index, embed, margin: float = MARGIN, floor: float = FLOOR,
                   limit: int | None = None) -> tuple[int, list[tuple[str, Attach]]]:
    """(pages tried, [(page, Attach)]) over the unfiled pages; `limit` tries a seeded sample."""
    t = tree(index)
    stack = head_stack(index, embed)
    cents, hv = centroids(index, t, stack), heads(stack)
    scope = [p for p in unfiled(index, t) if p in hv]
    if limit is not None:
        random.Random(0).shuffle(scope)
        scope = scope[:max(0, limit)]
    return len(scope), [(p, attach(hv[p], t, cents, (), margin, floor)) for p in scope]


def evaluate(index: Index, t: Tree, stack, margins=(0.01, 0.02, 0.03),
             floor: float = FLOOR) -> list[dict]:
    """Leave-one-out over pages with hubs (REVIEW-1005 §1.7): per margin the share attached,
    and of those the share on one of the page's hubs or an ancestor (precision) or exactly
    on one (exact), plus attachments by depth."""
    hv, cents = heads(stack), centroids(index, t, stack)
    pages = [(i, set(hs)) for i, n in _pages(index)
             if i in hv and (hs := page_hubs(n, t.parent))]
    rows = []
    for m in margins:
        att = ok = exact = 0
        depth: Counter = Counter()
        for i, hs in pages:
            a = attach(hv[i], t, cents, hs, m, floor)
            if a.hub is None:
                continue
            att += 1
            depth[t.depth(a.hub)] += 1
            exact += a.hub in hs
            ok += any(a.hub in t.path(h) for h in hs)
        rows.append({"margin": m, "pages": len(pages),
                     "attached": att / len(pages) if pages else 0.0,
                     "precision": ok / att if att else 0.0,
                     "exact": exact / att if att else 0.0, "depth": dict(sorted(depth.items()))})
    return rows


def status(index: Index, t: Tree) -> dict:
    mem = members(index, t)
    return {"hubs": len(t.parent),
            "by_depth": dict(sorted(Counter(t.depth(h) for h in t.parent).items())),
            "over_split": sum(len(ps) > SPLIT_N for ps in mem.values()),
            "pages": sum(1 for _ in _pages(index)), "unfiled": len(unfiled(index, t))}


# ---- page facts ---------------------------------------------------------------------------

def alias(s: str, n: int = 80) -> str:
    """Link text must not contain [ ] |."""
    return " ".join(str(s).translate(str.maketrans("[]|", "()/")).split())[:n]


@lru_cache(maxsize=1 << 15)  # the app's home asks for the same unchanged bodies on every build
def _timeline(body: str) -> str:
    m = TL_RE.search(body)
    return m.group(0) if m else ""


def page_date(id: str, fields, body: str) -> str:
    """`date`, else `Date:` on the Timeline line, else a day in the id; '' without one."""
    d = fields.get("date")
    if d and DAY_RE.match(str(d)):
        return str(d)[:10]
    m = re.search(r"Date: (\d{4}-\d{2}-\d{2})", _timeline(body)) or DAY_RE.search(id)
    return m.group(1) if m and m.groups() else m.group(0) if m else ""


def page_source(body: str) -> str:
    """`Source:` on an imported chat page's Timeline line ('' without one)."""
    m = re.search(r"Source: ([^·\r\n]+?)\s*(?:·|$)", _timeline(body))
    return m.group(1) if m else ""


def conv_line(id: str, title: str, date: str, source: str) -> str:
    return "- " + " · ".join(x for x in (date, source, f"[[{id}|{alias(title)}]]") if x)


def _links(line: str, id: str) -> bool:
    return re.search(r"\[\[" + re.escape(id) + r"[|#^\]]", line) is not None


# ---- body editing -------------------------------------------------------------------------

def _parts(text: str) -> tuple[str, str]:
    p = _split(text)
    return ("".join(p[:3]), p[3]) if p else ("", text)


def _find(lines: list[str], names) -> tuple[int, int] | None:
    for name in names:
        try:
            return _section(lines, name)
        except VaultError:
            continue
    return None


def _blank_tail(lines: list[str]) -> list[str]:
    n = 0
    while n < len(lines) and not lines[-1 - n].strip():
        n += 1
    return lines[len(lines) - n:]


def _set_section(body: str, heading: str, rows: list[str], nl: str, alts=(),
                 before=()) -> str:
    """`## heading` (found under it or an alternative name, then renamed) holding `rows`, its
    trailing blank lines kept; no rows removes the section. A missing one goes before the first
    `before` heading present, else at the end."""
    lines = body.splitlines(keepends=True)
    sec = _find(lines, (heading, *alts))
    block = [f"## {heading}{nl}", *(r + nl for r in rows)] if rows else []
    if sec:
        s, e = sec
        tail = _blank_tail(lines[s:e])
        if block:
            block += tail or ([nl] if e < len(lines) else [])
        if block and lines[e - 1:e] and not lines[e - 1].endswith("\n") and e == len(lines):
            block[-1] = block[-1].rstrip("\r\n")  # the file had no final newline
        lines[s - 1:e] = block
    elif block:
        at = next((i for i, ln in enumerate(lines) if (m := HEADING_RE.match(ln.rstrip("\r\n")))
                   and m.group(2) in before), len(lines))
        if at < len(lines):
            block.append(nl)
        if at and lines[at - 1].strip():
            if not lines[at - 1].endswith("\n"):
                lines[at - 1] += nl
            block.insert(0, nl)
        lines[at:at] = block
    return "".join(lines)


def _set_partof(body: str, line: str, nl: str) -> str:
    """`line` as the `Part of …` line under the H1 (replaced, else inserted)."""
    lines = body.splitlines(keepends=True)
    h = next((i for i, ln in enumerate(lines) if ln.startswith("# ")), None)
    if h is None:
        return body
    j = h + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j < len(lines) and lines[j].startswith("Part of "):
        lines[j] = line + (lines[j][len(lines[j].rstrip("\r\n")):] or nl)
        return "".join(lines)
    if not lines[h].endswith("\n"):
        lines[h] += nl
    new = [nl, line + nl]
    if h + 1 < len(lines) and lines[h + 1].strip():
        new.append(nl)
    lines[h + 1:h + 1] = new
    return "".join(lines)


def _owned(body: str, headings) -> list[str]:
    """The body without the generated parts, blank lines dropped: what `build` must keep."""
    lines = body.splitlines(keepends=True)
    for name in headings:
        if sec := _find(lines, (name,)):
            del lines[sec[0] - 1:sec[1]]
    h = next((i for i, ln in enumerate(lines) if ln.startswith("# ")), None)
    if h is not None:
        part = next((j for j in range(h + 1, len(lines)) if lines[j].strip()), None)
        if part is not None and lines[part].startswith("Part of "):
            del lines[part]
    return [ln.rstrip() for ln in lines if ln.strip()]


def _topics_line(body: str, line: str | None, nl: str) -> str:
    m = TOP_RE.search(body)
    if m:
        if line is not None:
            return body[:m.start()] + line + body[m.end():]
        end = m.end() + (len(nl) if body.startswith(nl, m.end()) else 0)
        return body[:m.start()] + body[end:]
    if line is None:
        return body
    if t := TL_RE.search(body):
        return body[:t.end()] + nl + line + body[t.end():]
    lines = body.splitlines(keepends=True)
    if lines and lines[0].startswith("# "):
        if not lines[0].endswith("\n"):
            lines[0] += nl
        new = [nl, line + nl]
        if len(lines) > 1 and lines[1].strip():
            new.append(nl)
        lines[1:1] = new
        return "".join(lines)
    return line + nl + (nl if body.strip() and body[:1] not in "\r\n" else "") + body


# ---- place --------------------------------------------------------------------------------

def _hub_page(vault: Vault, id: str):
    """The public hub note `id`, else None."""
    try:
        n = vault.read("note", id)
    except VaultError:
        return None
    tags = n.fields.get("tags")
    return n if isinstance(tags, list) and "topic" in tags and not is_private(n) else None


def page_kind(vault: Vault, id: str) -> str:
    for k in PAGE_KINDS:
        if vault.path_for(k, id).is_file():
            return k
    raise VaultError("unknown_id", f"No resource or note '{id}'; check the id with find.")


def _page_change(old: list[str], hubs: list[str]):
    line = "Topics: " + " · ".join(f"[[{h}]]" for h in hubs) if hubs else None

    def change(text: str) -> str:
        parts = _split(text)
        if parts is None:
            raise VaultError("invalid_argument", "The page has no frontmatter.")
        head, fm, close, body = parts
        nl = _newline(text)
        if old != hubs:
            data = _load_fm(fm)
            cur = data.get("related")
            items = list(cur) if isinstance(cur, list) else [cur] if cur else []
            keep = [str(x) for x in items if _target(x) not in old]
            data["related"] = _to_yaml(keep + [f"[[{h}]]" for h in hubs])
            fm = _dump_fm(data, nl)
        return head + fm + close + _topics_line(body, line, nl)

    return change


def _hub_add(id: str, line: str):
    def change(text: str) -> str:
        prefix, body = _parts(text)
        nl = _newline(text)
        lines = body.splitlines(keepends=True)
        sec = _find(lines, CONV[::-1])
        if sec is None:
            kids = _find(lines, ("Sub-topics", "Subtopics"))
            return prefix + _set_section(body, CONV[1] if kids else CONV[0], [line], nl,
                                         before=(WATCHED,))
        s, e = sec
        if any(_links(ln, id) for ln in lines[s:e]):
            return text
        at = next((j + 1 for j in range(e - 1, s - 1, -1) if lines[j].strip()), s)
        if at and not lines[at - 1].endswith("\n"):
            lines[at - 1] += nl
        lines.insert(at, line + nl)
        if at + 1 < len(lines) and HEADING_RE.match(lines[at + 1].rstrip("\r\n")):
            lines.insert(at + 1, nl)
        return prefix + "".join(lines)

    return change


def _hub_drop(id: str):
    def change(text: str) -> str:
        prefix, body = _parts(text)
        lines = body.splitlines(keepends=True)
        sec = _find(lines, CONV[::-1])
        if sec is None:
            return text
        s, e = sec
        return prefix + "".join(ln for k, ln in enumerate(lines)
                                if not (s <= k < e and _links(ln, id)))

    return change


def place(vault: Vault, page_id: str, hubs: list[str], kind: str | None = None) -> bool:
    """Set a resource's or note's topics to `hubs` (0 to MAX_HUBS hub ids): the hub ids in
    `related` (other ids kept) and the body `Topics:` line (under `Timeline:`, else the H1);
    the page's line is added to each new hub's conversation section and dropped from hubs it
    left. Returns whether anything changed. Private pages and hubs are refused."""
    hubs = list(dict.fromkeys(str(h).strip() for h in hubs if str(h).strip()))
    if len(hubs) > MAX_HUBS:
        raise VaultError("invalid_argument", f"A page takes at most {MAX_HUBS} topics.")
    with vault.lock():
        kind = kind or page_kind(vault, page_id)
        if kind not in PAGE_KINDS:
            raise VaultError("invalid_argument", "Only resources and notes take topics.")
        note = vault.read(kind, page_id)
        if is_private(note):
            raise VaultError("invalid_argument", "A private page is never filed under a topic: "
                             "hub pages list their pages.")
        tags = note.fields.get("tags")
        if isinstance(tags, list) and "topic" in tags:
            raise VaultError("invalid_argument", f"'{page_id}' is a topic hub; its parent is "
                             "the first hub in its related list.")
        bad = [h for h in hubs if _hub_page(vault, h) is None]
        if bad:
            raise VaultError("invalid_argument", f"Not topic hubs: {', '.join(bad)}; use hub "
                             "ids (notes tagged topic).")
        old = [t for t in dict.fromkeys(_edge_targets(note.fields.get("related") or []))
               if _hub_page(vault, t) is not None]
        changed = vault._mutate(note.path, _page_change(old, hubs))
        line = conv_line(page_id, str(note.fields.get("title") or page_id),
                         page_date(page_id, note.fields, note.body), page_source(note.body))
        for h in hubs:
            changed |= vault._mutate(vault.path_for("note", h), _hub_add(page_id, line))
        for h in old:
            if h not in hubs:
                changed |= vault._mutate(vault.path_for("note", h), _hub_drop(page_id))
    return changed


# ---- build --------------------------------------------------------------------------------

def _watched(body: str) -> list[str]:
    lines = body.splitlines()
    sec = _find(lines, (WATCHED,))
    return [ln[2:].strip() for ln in lines[sec[0]:sec[1]] if ln.startswith("- ")] if sec else []


def build(vault: Vault, index: Index, dry_run: bool = False, only=None) -> Counter:
    """Regenerate the generated hub parts and the month pages' `## Links` in place. A page whose
    hand-owned text would change is skipped and counted as `unsafe`. Returns counts: hubs and
    months changed, unchanged, unsafe. `only` limits it to those hubs and skips the months."""
    t = tree(index)
    mem = members(index, t)
    notes = index._notes
    info = {p: (page_date(p, notes[p]["fields"], notes[p]["body"]), page_source(notes[p]["body"]),
                notes[p]["title"]) for ps in mem.values() for p in ps}
    acts = {h: _watched(notes[h]["body"]) for h in t.parent}
    kids = t.kids
    memo: dict[str, frozenset] = {}

    def sub(h: str) -> frozenset:
        if h not in memo:
            memo[h] = frozenset(mem.get(h, ())).union(*(sub(k) for k in kids.get(h, [])))
        return memo[h]

    def span(h: str) -> tuple[str, str, int]:
        a = [x for k in [h, *kids.get(h, [])] for x in acts[k]]
        ds = sorted([info[p][0] for p in sub(h) if info[p][0]]
                    + [x[1:8] for x in a if x.startswith("[")]) or ["?"]
        return ds[0], ds[-1], len(sub(h)) + len(a)

    def convs(h: str) -> list[str]:
        return [conv_line(p, info[p][2], info[p][0], info[p][1])
                for p in sorted(mem.get(h, ()), key=lambda p: (info[p][0], p))]

    def subs_of(h: str) -> list[str]:
        return sorted((s for s in kids.get(h, []) if sub(s) or acts[s]), key=lambda s: -span(s)[2])

    plans: dict[str, tuple] = {}  # hub -> (Part of line or None, [(heading, rows, alts)])
    for h in t.parent:
        p = t.parent[h]
        if h == ROOT:
            rows = []
            for d in kids.get(h, []):
                if ss := subs_of(d):
                    rows.append(f"- [[{d}|{alias(t.title[d])}]]: {len(ss)} subtopics, "
                                f"{sum(span(s)[2] for s in ss)} items")
            plans[h] = (None, [("Areas", rows, ())])
            continue
        if p == ROOT:
            ss = subs_of(h)
            rows = []
            for s in ss:
                a, b, c = span(s)
                nk = len([k for k in kids.get(s, []) if sub(k)])
                rows.append(f"- [[{s}|{alias(t.title[s])}]]: {c} items, {a[:7]} to {b[:7]}"
                            + (f" ({nk} sub-topics)" if nk else ""))
            partof = f"Part of [[{ROOT}]]."
            plans[h] = (partof, [("Subtopics", rows, ()),
                                 (CONV[1] if ss else CONV[0], convs(h), CONV)])
            continue
        first, last, count = span(h)
        ks = sorted((k for k in kids.get(h, []) if sub(k)), key=lambda k: -span(k)[2])
        by_src = Counter(info[x][1] or "other" for x in sub(h))
        if acts[h]:
            by_src["YouTube/Google"] = len(acts[h])
        partof = None
        if p is not None:
            partof = f"Part of [[{p}|{alias(t.title[p])}]]" + (
                f" · first explored {first} · last {last} · "
                + ", ".join(f"{c} {n}" for c, n in by_src.most_common()) if count else "")
        rows = [f"- [[{k}|{alias(t.title[k])}]]: {span(k)[2]} conversations, "
                f"{span(k)[0][:7]} to {span(k)[1][:7]}" for k in ks]
        plans[h] = (partof, [("Sub-topics", rows, ()),
                             (CONV[1] if ks else CONV[0], convs(h), CONV)])

    def hub_change(plan):
        partof, secs = plan
        heads_ = [x for name, _, alts in secs for x in (name, *alts)]

        def change(text: str) -> str:
            prefix, body = _parts(text)
            nl = _newline(text)
            new = _set_partof(body, partof, nl) if partof else body
            for name, rows, alts in secs:
                new = _set_section(new, name, rows, nl, tuple(a for a in alts if a != name),
                                   before=(*CONV, WATCHED) if name not in CONV else (WATCHED,))
            if _owned(new, heads_) != _owned(body, heads_):
                raise _Unsafe
            return prefix + new

        return change

    done: Counter = Counter()
    jobs = [(vault.path_for("note", h), hub_change(plans[h]), "hubs") for h in sorted(plans)
            if only is None or h in only]
    if only is None:
        jobs += _month_jobs(vault, index, t, mem, info)
    with _maybe_lock(vault, dry_run):
        for path, change, what in jobs:
            try:
                text = path.read_bytes().decode("utf-8")
                if change(text) == text:
                    done["unchanged"] += 1
                    continue
                if not dry_run:
                    vault._mutate(path, change)
            except _Unsafe:
                done["unsafe"] += 1
                continue
            done[what] += 1
    return done


class _Unsafe(Exception):
    """A rebuild would change text that `build` does not own."""


def _maybe_lock(vault: Vault, dry_run: bool):
    from contextlib import nullcontext

    return nullcontext() if dry_run else vault.lock()  # a dry run writes nothing, not even a lock


def _month_jobs(vault: Vault, index: Index, t: Tree, mem: dict, info: dict) -> list:
    """`## Links` on each month page: topics that month rolled up to depth 2, most pages first,
    and projects its digest lines name (ported from the import's build_hubs)."""
    def top(h: str) -> str:
        while t.depth(h) > 2:
            h = t.parent[h]
        return h

    by_page: dict[str, set[str]] = defaultdict(set)
    for h, ps in mem.items():
        for p in ps:
            by_page[p].add(top(h))
    month_topics: dict[str, Counter] = defaultdict(Counter)
    for p, tops in sorted(by_page.items()):
        if info[p][0]:
            month_topics[info[p][0][:7]].update(sorted(tops))
    projects = []
    for pid, n in sorted(index._notes.items()):
        if n["kind"] == "project":
            words = [w for w in re.split(r"[-_]", pid) if len(w) > 3]
            if words:
                projects.append((pid, re.compile("|".join(map(re.escape, words)), re.I)))
    jobs = []
    for mid, n in sorted(index._notes.items()):
        if n["kind"] != "journal" or not MONTH_RE.match(mid):
            continue
        links = [f"- [[{h}|{t.title[h]}]] ({c})" for h, c in month_topics[mid[:7]].most_common(15)]

        def change(text: str, links=links) -> str:
            base = re.split(r"\n## Links\n", text)[0].rstrip() + "\n"
            digest = "\n".join(ln for ln in base.splitlines() if " digest " in ln)
            proj = [f"- [[{pid}]]" for pid, rx in projects
                    if digest and len({x.lower() for x in rx.findall(digest)}) >= 2]
            if not (links or proj):
                return text
            return base + "\n## Links\n" + (
                "Topics this month:\n" + "\n".join(links) + "\n" if links else "") + (
                "Projects mentioned:\n" + "\n".join(proj) + "\n" if proj else "")

        jobs.append((vault.path_for("journal", mid), change, "months"))
    return jobs


# ---- split proposals (REVIEW-1005 §6) ------------------------------------------------------

def _kmeans(X: np.ndarray, k: int, seed: int) -> np.ndarray:
    """Spherical k-means with k-means++ starts: labels of the best of RESTARTS seeded runs by
    total cosine to the centroids. A cluster that empties keeps its old centroid."""
    rng = np.random.default_rng([seed, k])
    best, best_score = None, -np.inf
    for _ in range(RESTARTS):
        C = [X[rng.integers(len(X))]]
        for _ in range(k - 1):
            w = np.clip(1.0 - np.max(X @ np.asarray(C).T, axis=1), 0.0, None).astype(float) ** 2
            C.append(X[rng.choice(len(X), p=w / w.sum())] if w.sum() > 1e-12
                     else X[rng.integers(len(X))])
        C = np.asarray(C)
        for _ in range(ITERS):
            lab = np.argmax(X @ C.T, axis=1)
            S = np.zeros_like(C)
            np.add.at(S, lab, X)
            norm = np.linalg.norm(S, axis=1)
            new = np.where((norm > 1e-9)[:, None], S / np.maximum(norm, 1e-9)[:, None], C)
            if np.allclose(new, C):
                break
            C = new
        sims = X @ C.T
        lab = np.argmax(sims, axis=1)
        score = float(sims[np.arange(len(X)), lab].sum())
        if score > best_score + 1e-9:
            best, best_score = lab, score
    return best


def _silhouette(X: np.ndarray, lab: np.ndarray) -> np.ndarray:
    """Per-point cosine silhouette; 0 for a point alone in its cluster or with one cluster."""
    ks, own = np.unique(lab, return_inverse=True)
    n = len(X)
    if len(ks) < 2:
        return np.zeros(n)
    D = np.clip(1.0 - X @ X.T, 0.0, None)
    np.fill_diagonal(D, 0.0)
    sums = np.stack([D[:, own == j].sum(axis=1) for j in range(len(ks))], axis=1)
    sizes = np.bincount(own)
    alone = sizes[own] == 1
    a = sums[np.arange(n), own] / np.maximum(sizes[own] - 1, 1)
    mean = sums / sizes
    mean[np.arange(n), own] = np.inf
    b = mean.min(axis=1)
    s = (b - a) / np.maximum(np.maximum(a, b), 1e-12)
    s[alone] = 0.0
    return s


def _cluster(X: np.ndarray, seed: int):
    """(k, labels, per-point silhouette, mean silhouette, note) for the k in 3..min(MAX_K,
    n // MIN_CHILD) with the best mean silhouette, else None. A cluster over MAX_SHARE gets one
    k+1 retry, kept when it fixes that and still clears SIL_MIN."""
    n = len(X)
    best = None
    for k in range(3, min(MAX_K, n // MIN_CHILD) + 1):
        lab = _kmeans(X, k, seed)
        s = _silhouette(X, lab)
        if best is None or s.mean() > best[3] + 1e-9:
            best = (k, lab, s, float(s.mean()))
    if best is None:
        return None
    k, lab, s, sil = best
    note = ""
    if np.bincount(lab).max() > MAX_SHARE * n:
        lab2 = _kmeans(X, k + 1, seed) if k + 1 <= min(MAX_K, n) else None
        s2 = _silhouette(X, lab2) if lab2 is not None else None
        if lab2 is not None and np.bincount(lab2).max() <= MAX_SHARE * n and s2.mean() >= SIL_MIN:
            k, lab, s, sil = k + 1, lab2, s2, float(s2.mean())
        else:
            note = (f"one cluster holds {np.bincount(lab).max() / n:.0%} of the pages; k+1 did not "
                    "split it")
    return k, lab, s, sil, note


def _terms(titles: list[list[str]]) -> list[list[str]]:
    """c-TF-IDF: per cluster the top 3 title words by tf x log(1 + mean cluster words / the
    word's count over all clusters); stopwords and digits never count."""
    tf = [Counter(w for t in ts for w in re.findall(r"[a-z]+", t.lower())
                  if len(w) > 1 and w not in LABEL_STOP) for ts in titles]
    total: Counter = Counter()
    for c in tf:
        total.update(c)
    avg = sum(total.values()) / max(len(tf), 1)
    return [[w for w, _ in sorted(c.items(), key=lambda x: (
        -x[1] * math.log(1 + avg / total[x[0]]), x[0]))[:3]] for c in tf]


def _proposal(index: Index, hub: str, ids: list[str], hv: dict, seed: int = 0,
              gate: bool = True) -> dict | None:
    """The split proposal over pages `ids` (sorted): clusters of at least MIN_CHILD pages with a
    non-negative silhouette, the rest in `stay`. None under SIL_MIN or with fewer than 2
    clusters, unless `gate` is off (the eval)."""
    if len(ids) < 3 * MIN_CHILD:
        return None
    X = np.stack([hv[p] for p in ids])
    c = _cluster(X, seed)
    if c is None or (gate and c[3] < SIL_MIN):
        return None
    k, lab, s, sil, note = c
    stay = [i for i in range(len(ids)) if s[i] < 0]
    groups = []
    for j in range(k):
        g = [i for i in range(len(ids)) if lab[i] == j and s[i] >= 0]
        if len(g) >= MIN_CHILD:
            groups.append(g)
        else:
            stay += g
    if gate and len(groups) < 2:
        return None
    groups.sort(key=lambda g: (-len(g), g[0]))
    title = {p: str(index._notes[p]["title"]) for p in ids}
    terms = _terms([[title[ids[i]] for i in g] for g in groups])
    clusters = []
    for g, words in zip(groups, terms, strict=True):
        cen = X[g].sum(axis=0)
        near = sorted(g, key=lambda i: (-float(X[i] @ cen), ids[i]))[:5]
        clusters.append({"members": [ids[i] for i in g], "terms": words,
                         "sample_titles": [title[ids[i]] for i in near]})
    digest = hashlib.sha256(json.dumps([hub, ids, k, seed]).encode()).hexdigest()[:16]
    return {"hub": hub, "hash": digest, "k": k, "silhouette": round(sil, 4),
            "clusters": clusters, "stay": sorted(ids[i] for i in stay),
            **({"note": note} if note else {})}


def propose_split(index: Index, t: Tree, stack, hub: str, seed: int = 0) -> dict | None:
    """{hub, hash, k, silhouette, clusters[{members, terms, sample_titles}], stay[], note?} for
    the hub's direct non-private pages, or None (no such hub, depth >= MAX_DEPTH, too few pages,
    silhouette under SIL_MIN). The hash covers the hub, the sorted member ids, k and the seed."""
    if hub not in t.parent or t.depth(hub) >= MAX_DEPTH:
        return None
    hv = heads(stack)
    ids = sorted(p for p in members(index, t).get(hub, ()) if p in hv)
    return _proposal(index, hub, ids, hv, seed)


def split_candidates(index: Index, t: Tree) -> list[tuple[str, int]]:
    """(hub, direct pages): hubs over SPLIT_N above MAX_DEPTH, biggest first, then parents of
    children still marked `names: provisional`."""
    mem = members(index, t)
    over = sorted((h for h, ps in mem.items() if len(ps) > SPLIT_N and t.depth(h) < MAX_DEPTH),
                  key=lambda h: (-len(mem[h]), h))
    prov = sorted({p for h, p in t.parent.items() if p is not None and p not in over
                   and index._notes[h]["fields"].get("names") == "provisional"})
    return [(h, len(mem.get(h, ()))) for h in over + prov]


def proposal_path(vault: Vault, hub: str):
    return vault.root / ".cortex" / "proposals" / f"split-{hub}.json"


def write_proposal(vault: Vault, prop: dict) -> None:
    """`.cortex/proposals/split-<hub>.json` (git-ignored); a failure only logs."""
    path = proposal_path(vault, prop["hub"])
    try:
        vault.write_text(path, json.dumps(prop, indent=1, ensure_ascii=False) + "\n")
    except (VaultError, OSError) as e:
        log.warning("split proposal not saved: %s", e)


def auto_children(prop: dict) -> list[dict]:
    """Offline names for `--apply --auto-names`: the c-TF-IDF terms."""
    return [{"title": " ".join(c["terms"]).title() or f"Group {k + 1}",
             "desc": "Provisional name from the page titles: " + (", ".join(c["terms"]) or "none")
             + "."} for k, c in enumerate(prop["clusters"])]


def apply_split(vault: Vault, index: Index, embed, hub: str, hash: str, children,
                provisional: bool = False) -> dict:
    """Create the named child hubs of `hub` and move their pages there, after recomputing the
    proposal and checking its hash. Everything is validated before the first write. Returns
    {children[{id, title, n}], stayed, moved}."""
    index.refresh()
    t = tree(index)
    if hub not in t.parent:
        raise invalid(f"'{hub}' is not a topic hub.")
    prop = propose_split(index, t, head_stack(index, embed), hub)
    if prop is None or prop["hash"] != hash:
        raise invalid("The split proposal no longer matches: pages changed; run cortex_reflect "
                      "again.")
    if not isinstance(children, list) or not children or len(children) > MAX_K:
        raise invalid(f"children must be a list of 1 to {MAX_K} {{title, desc, members?}}.")
    if not all(isinstance(c, dict) for c in children):
        raise invalid("Each child is an object {title, desc, members?}.")
    own = all(c.get("members") is not None for c in children)
    if not own and len(children) != len(prop["clusters"]):
        raise invalid(f"Give {len(prop['clusters'])} children, one per cluster in order, or a "
                      "members list on every child.")
    direct = set(members(index, t).get(hub, ()))
    plan, uses = [], Counter()
    for k, c in enumerate(children):
        title, desc = (" ".join(str(c.get(f) or "").split()) for f in ("title", "desc"))
        if not slugify(title) or not desc:
            raise invalid("Each child needs a title (with a letter or digit) and a desc.")
        ms = c.get("members")
        if ms is None:
            ms = prop["clusters"][k]["members"]
        if not isinstance(ms, list) or not ms:
            raise invalid("members must be a non-empty list of page ids.")
        ms = list(dict.fromkeys(str(m) for m in ms))
        if bad := [m for m in ms if m not in direct]:
            raise invalid(f"Not direct pages of {hub}: {', '.join(bad[:5])}.")
        scan_secrets(f"{title}\n{desc}")
        uses.update(ms)
        plan.append((title, desc, ms))
    if over := sorted(m for m, n in uses.items() if n > 2):
        raise invalid(f"A page goes in at most 2 children: {', '.join(over[:5])}.")
    ids: list[str] = []
    for title, _, _ in plan:
        base = f"{hub}-{slugify(title)}"[:70].rstrip("-.")
        cid, n = base, 2
        while cid in ids or any((vault.root / d / f"{cid}.md").exists()
                                for k in vault.KIND_DIRS for d in vault.dirs(k)):
            cid = f"{base[:66].rstrip('-.')}-{n}"
            n += 1
        ids.append(cid)
    moves = {}
    for m in sorted(uses):
        hs = [h for h in page_hubs(index._notes[m], t.parent) if h != hub]
        hs += [cid for cid, (_, _, ms) in zip(ids, plan, strict=True) if m in ms]
        if len(hs) > MAX_HUBS:
            raise invalid(f"'{m}' would have more than {MAX_HUBS} topics.")
        moves[m] = (hs, index._notes[m]["kind"])
    with vault.lock():
        for cid, (title, desc, _) in zip(ids, plan, strict=True):
            fields = {"title": title, "kind": "note", "note_type": "structure", "tags": ["topic"],
                      "related": [f"[[{hub}]]"], "summary": desc}
            if provisional:
                fields["names"] = "provisional"
            vault.create_note("note", cid, fields, f"# {title}\n\n{desc}\n")
        for m, (hs, kind) in moves.items():
            place(vault, m, hs, kind)
        index.refresh()
        build(vault, index, only={hub, *ids})
    return {"children": [{"id": cid, "title": title, "n": len(ms)}
                         for cid, (title, _, ms) in zip(ids, plan, strict=True)],
            "stayed": len(direct - set(uses)), "moved": len(uses)}


def _browse(sizes: list[int], left: int, n: int) -> float:
    """Share of the flat list's scan saved: a page costs the child list plus its child's list
    (or the parent's leftover list), against n."""
    nc = len(sizes)
    cost = (sum(s * (nc + s) for s in sizes) + left * (nc + left)) / n
    return 1.0 - cost / n


def _cohesion(X: np.ndarray, groups: list[list[int]]) -> float:
    """Mean pairwise cosine within each group, weighted by group size."""
    num = den = 0.0
    for g in groups:
        m = len(g)
        if m >= 2:
            s = X[g].sum(axis=0)
            num += m * (float(s @ s) - m) / (m * (m - 1))
            den += m
    return num / den if den else 0.0


def _ari(a, b) -> float:
    """Adjusted Rand index of two labelings."""
    _, ai = np.unique(a, return_inverse=True)
    _, bi = np.unique(b, return_inverse=True)
    M = np.zeros((ai.max() + 1, bi.max() + 1))
    np.add.at(M, (ai, bi), 1)

    def c2(x):
        return float((x * (x - 1) / 2).sum())

    s, sa, sb = c2(M), c2(M.sum(axis=1)), c2(M.sum(axis=0))
    exp = sa * sb / c2(np.array([len(ai)]))
    top = (sa + sb) / 2
    return (s - exp) / (top - exp) if abs(top - exp) > 1e-12 else 1.0


def evaluate_splits(index: Index, t: Tree, stack, seed: int = 0) -> dict:
    """REVIEW-1005 §6.4 on the hand-split hubs (children holding pages, at least 10 pages in
    all): the manual split's silhouette (its p25 calibrates SIL_MIN), browse-cost reduction
    manual vs automatic, cohesion, and ARI at the chosen k. Read-only."""
    hv, mem = heads(stack), members(index, t)
    rows = []
    for h in sorted(t.parent):
        label: dict[str, int] = {}
        for j, kid in enumerate(t.kids.get(h, [])):
            for p in sorted(mem.get(kid, ())):
                if p in hv:
                    label.setdefault(p, j)
        if not label:
            continue
        for p in mem.get(h, ()):
            if p in hv:
                label.setdefault(p, -1)  # the parent's leftover list
        ids = sorted(label)
        if len(ids) < 10:
            continue
        X = np.stack([hv[p] for p in ids])
        man = np.array([label[p] for p in ids])
        inside = man >= 0
        msil = (float(_silhouette(X[inside], man[inside]).mean())
                if len(set(man[inside].tolist())) > 1 else 0.0)
        mgroups = [np.flatnonzero(man == j).tolist() for j in sorted(set(man[inside].tolist()))]
        row = {"hub": h, "n": len(ids), "manual_sil": msil,
               "manual_browse": _browse([len(g) for g in mgroups], int((~inside).sum()), len(ids)),
               "manual_cohesion": _cohesion(X, mgroups)}
        prop = _proposal(index, h, ids, hv, seed, gate=False)
        if prop is not None:
            pos = {p: i for i, p in enumerate(ids)}
            agroups = [[pos[p] for p in c["members"]] for c in prop["clusters"]]
            auto = np.full(len(ids), -1)
            for j, g in enumerate(agroups):
                auto[g] = j
            row.update(auto_sil=prop["silhouette"], k=prop["k"],
                       auto_browse=_browse([len(g) for g in agroups], len(prop["stay"]), len(ids)),
                       auto_cohesion=_cohesion(X, agroups), ari=_ari(man, auto))
        rows.append(row)
    done = [r for r in rows if "auto_browse" in r]

    def med(key, rs):
        return float(np.median([r[key] for r in rs])) if rs else 0.0

    man_b, auto_b = med("manual_browse", done), med("auto_browse", done)
    return {"hubs": len(rows), "compared": len(done),
            "manual_sil_p25": float(np.percentile([r["manual_sil"] for r in rows], 25))
            if rows else 0.0,
            "manual_sil_median": med("manual_sil", rows),
            "manual_browse": man_b, "auto_browse": auto_b,
            "browse_ratio": auto_b / man_b if man_b > 0 else 0.0,
            "manual_cohesion": med("manual_cohesion", done),
            "auto_cohesion": med("auto_cohesion", done), "ari": med("ari", done), "rows": rows}
