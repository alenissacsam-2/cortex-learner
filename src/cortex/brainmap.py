"""Brain v3, the neural network look: geometry and page layout (docs/design/brain-v3.md §2;
brain-v2.md §3-§4 and §7 for what still holds).

Python owns the geometry and the layout; `assets/brain3d.js` only draws them. One implicit
brain volume (`inside`): a smooth union of two overlapping hemispheres, a temporal lobe per
side, a tucked cerebellum and a brainstem stub, with a shallow groove on top and gentle folds,
meshed once. Lobes (the dashboard clusters) get 3-D sites by classical MDS of 1 - affinity and
round regions by a centroidal capacity-constrained power diagram over seeded samples of the
volume (volume ~ count^0.65), with an empty gap at region borders. Pages sit in their region
by a PCA + kNN relaxation of their vectors in 3-D, partly transported to fill it, the most
linked pages pulled into a nucleus at its centre; unsorted chats form a thick band under the
brain. Each lobe's hue differs by HUE_GAP from every region it touches.
Fibres are the wikilinks plus each page's 3 nearest by meaning, as quadratic Béziers bundled
toward the regions' centres. The layout persists in `.cortex/brain-layout.json` and grows
incrementally. It only reads the vector cache: it never embeds and never downloads a model.

Builder choices (measured on the real vault, see the report of 17 Nov 2026):
- the mesh is surface nets (dual marching cubes, no case table): one vertex per crossed cell,
  projected onto the surface by two Newton steps, lit by the field's gradient;
- the affinity's shared-parent term is "same lobe kind";
- MDS axes go to front-back, left-right, up-down; the coordinates are whitened and each lobe
  set at its radius rank before the push apart (plain MDS crowded most lobes into the centre
  and made slab-shaped regions); then damped Lloyd moves round the regions while each lobe's
  two most affine lobes are pulled back toward touching it (fixed sites left 14 of 40 real
  regions as slabs; plain Lloyd lost the fixture's families); the cerebellum is its own pool
  when a routine lobe exists;
- the in-lobe relaxation repels by negative sampling (UMAP-style), is fitted to its region by
  mean and covariance, then moved 40 % of the way to its sliced optimal transport onto the
  region (closes the gaps between clumps; 40 % keeps in-lobe preservation near 0.7);
- hubs are a lobe's top 5 % or the vault's top 1 % by degree, pulled toward the centre by
  up to 70 % (log degree), so a journal lobe full of daily hubs gets a nucleus too;
- a lobe's centre is the mean of its region's interior samples, its anchor the interior
  sample nearest that centre, its normal the anchor's direction from the brain's centre;
- a page new since the last layout goes to the similarity-weighted mean of its 3 nearest
  placed pages, so a near-duplicate lands by its twin;
- touching regions are lobes whose samples share a voxel or two face-adjacent ones of a
  32-cube grid; each lobe wants the hue of its site's circular order around the vertical axis
  spread evenly over the wheel; touching lobes get hues 60 degrees apart by colouring the
  touching graph with six hues (DSatur, backtracking), then each moves toward its wanted hue
  as far as its gaps allow; lobes that share a hex colour step 0.03 darker;
- `nodes.weight` (0-1, for the renderer's node size) is 0.6 log link degree + 0.25 log
  similar-fibre degree (each scaled by the vault's maximum) + 0.15 recency of `updated`
  (e-folding RECENT days before the newest page);
- a lobe that appears between relayouts is a guest in its most affine lobe's region;
- a stored page also records whether it had a vector: pages new, moved or embedded since
  the last layout count toward the 20 % relayout threshold;
- the ring takes no tracts; `tracts` stay for the lobe inspector's "Strongest connections".
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
from datetime import date
from functools import lru_cache

import numpy as np

from cortex.semantic import CACHE
from cortex.vault import VaultError

log = logging.getLogger(__name__)

VERSION = 3
REVISION = 2  # of the layout code: a bump relays every vault out once
LAYOUT = ".cortex/brain-layout.json"
LEGEND = ("lobe = a topic from your topic tree, coloured unlike every topic it touches; "
          "position = similar meaning (nearby points are related, distances "
          "are approximate); volume grows slower than the page count; counts are exact in "
          "text; bright fibres = your links; faint fibres = similar meaning.")

# the volume: ellipsoids as (centre, radii x, y above the centre, y below it, z)
HEMI = ((0.24, 0.0, 0.0), (0.44, 0.55, 0.40, 0.86))  # the right one; the left is its mirror
TEMPORAL = ((0.40, -0.25, 0.12), (0.22, 0.18, 0.18, 0.42))  # likewise
CEREBELLUM = ((0.0, -0.38, -0.55), (0.40, 0.17, 0.17, 0.26))
STEM = ((0.0, -0.30, -0.25), (0.0, -0.74, -0.36), 0.085)  # a capsule: two ends, a radius
SMOOTH = 0.08  # polynomial smooth-min width
GROOVE = (0.04, 0.06)  # depth and half-width of the longitudinal groove on top
FOLD = (0.015, 12.0)  # fold amplitude and wave number
BOX = ((-0.78, -0.88, -0.98), (0.78, 0.68, 0.98))  # holds the volume with a margin
GRID = 64  # mesh cells along the longest side of BOX
SAMPLES, MARGIN = 60000, 0.02  # volume samples, each at least MARGIN inside the surface
GAP = 0.02  # pages and region samples keep this far from a region border
AREA_EXP = 0.65
SPREAD = 0.3  # minimum site spacing, a share of the sum of two region radii
LLOYD, LLOYD_STEP = 10, 0.5  # site moves toward their cell's centroid, and how far each goes
TIES, TIE_PULL, TOUCH = 2, 1.0, 0.5  # each lobe's most affine lobes, pulled back toward
# TOUCH x the sum of the two regions' radii
FILL = 0.8  # sites stay inside this share of their pool's extent
FIT = 0.9  # a lobe's layout fills this share of its region's spread
FILL_OT = 0.4  # then goes this share of the way to filling its region evenly
HUBS, VAULT_HUBS = 0.05, 0.01  # hubs: a lobe's top share of pages by degree, or the vault's
HUB_PULL = 0.7  # how far the most linked hub of a lobe is pulled toward its centre
RING_Y, RING_R, RING_W = -0.62, 0.75, 0.07  # the unsorted chats' band: height, radius, thickness
KNN, NEG, EPOCHS = 6, 5, 100  # in-lobe relaxation: attracting neighbours, repelling samples
N_NEAR, N_TRACTS, N_BEACONS, N_SIMILAR = 8, 150, 3, 3
RECENT = 30  # days: a node's weight for recency falls by e for each RECENT days older
PULL, JITTER = 0.35, 0.02  # fibre control points: toward the regions' centres, seeded jitter
HUE_GAP, HUE_MIN = 60, 3  # degrees between touching regions' hues, and (where it fits) any two
RELAYOUT_SHARE = 0.2
ROUTINES = {"journal", "you", "sessions", "placements", "browsing"}  # group lobes in the cerebellum
FRONTAL = {"projects", "people"}  # group lobes pinned to the frontal pole
ARCHIVE = {"chats"}  # group lobes on the basal ring (plus any lobe of kind "archive")
PARTS = ("cerebrum", "cerebellum")
PARAMS_HASH = hashlib.sha256(json.dumps(
    [VERSION, REVISION, HEMI, TEMPORAL, CEREBELLUM, STEM, SMOOTH, GROOVE, FOLD, BOX, SAMPLES,
     MARGIN, GAP, AREA_EXP, SPREAD, LLOYD, LLOYD_STEP, TIES, TIE_PULL, TOUCH, FILL, FIT,
     FILL_OT, HUBS, VAULT_HUBS, HUB_PULL, RING_Y, RING_R, RING_W, KNN, NEG, EPOCHS]
).encode()).hexdigest()[:16]


def _u(key: str, k: int = 4) -> np.ndarray:
    """k uniforms in [0, 1) from a stable hash of `key`."""
    d = hashlib.blake2b(key.encode(), digest_size=4 * k).digest()
    return np.frombuffer(d, "<u4") / 2.0 ** 32


def _unit(x: np.ndarray) -> np.ndarray:
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-12)


# ---- the volume ----------------------------------------------------------------------------

def _ellipsoid(p: np.ndarray, c, r) -> np.ndarray:
    """About the signed distance to an ellipsoid whose lower half has its own height."""
    q = p - np.asarray(c)
    rad = np.empty_like(q)
    rad[:, 0], rad[:, 1], rad[:, 2] = r[0], np.where(q[:, 1] >= 0, r[1], r[2]), r[3]
    k0 = np.linalg.norm(q / rad, axis=1)
    k1 = np.linalg.norm(q / rad ** 2, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(k1 > 1e-9, k0 * (k0 - 1) / k1, -min(r))


def _capsule(p: np.ndarray, a, b, r: float) -> np.ndarray:
    a, ba = np.asarray(a), np.subtract(b, a)
    t = np.clip((p - a) @ ba / (ba @ ba), 0, 1)
    return np.linalg.norm(p - a - t[:, None] * ba, axis=1) - r


@lru_cache
def _waves() -> tuple[np.ndarray, np.ndarray]:
    u = np.array([_u(f"fold:{k}", 3) for k in range(6)])
    z, a = 2 * u[:, 0] - 1, 2 * np.pi * u[:, 1]
    r = np.sqrt(1 - z * z)
    return np.stack([r * np.cos(a), r * np.sin(a), z], 1) * FOLD[1], 2 * np.pi * u[:, 2]


def _parts(p: np.ndarray) -> np.ndarray:
    """Distance to each part: hemispheres (left, right), temporal lobes (left, right),
    cerebellum, brainstem."""
    m = np.array([-1.0, 1.0, 1.0])
    (hc, hr), (tc, tr) = HEMI, TEMPORAL
    return np.stack([_ellipsoid(p, np.multiply(hc, m), hr), _ellipsoid(p, hc, hr),
                     _ellipsoid(p, np.multiply(tc, m), tr), _ellipsoid(p, tc, tr),
                     _ellipsoid(p, *CEREBELLUM), _capsule(p, *STEM)], 1)


def _field(p: np.ndarray, d: np.ndarray | None = None) -> np.ndarray:
    """The brain's implicit function: negative inside, about the distance to the surface."""
    d = _parts(p) if d is None else d
    f = d[:, 0]
    for k in range(1, d.shape[1]):  # polynomial smooth min
        h = np.maximum(SMOOTH - np.abs(f - d[:, k]), 0) / SMOOTH
        f = np.minimum(f, d[:, k]) - h * h * SMOOTH / 4
    depth, width = GROOVE
    f = f + depth * np.exp(-(p[:, 0] / width) ** 2) * np.clip(p[:, 1] / 0.25, 0, 1)
    k, ph = _waves()
    return f + FOLD[0] * np.sin(p @ k.T + ph).sum(1) / math.sqrt(3)


def inside(p) -> np.ndarray:
    """Whether each point (N x 3) is inside the brain volume."""
    return _field(np.atleast_2d(np.asarray(p, float))) < 0


def _grad(p: np.ndarray, h: float = 1e-4) -> np.ndarray:
    return np.stack([_field(p + e) - _field(p - e) for e in np.eye(3) * h], 1) / (2 * h)


@lru_cache(maxsize=1)
def _samples() -> tuple[np.ndarray, np.ndarray, float]:
    """SAMPLES seeded points at least MARGIN inside the volume (none in the brainstem), which
    of them are cerebellum, and the volume each stands for."""
    rng = np.random.default_rng(3)
    lo, hi = np.array(BOX[0]), np.array(BOX[1])
    pts, cb, drawn = [], [], 0
    while sum(map(len, pts)) < SAMPLES:
        p = lo + (hi - lo) * rng.random((1 << 16, 3))
        d = _parts(p)
        part = np.argmin(d, 1)
        ok = (_field(p, d) < -MARGIN) & (part != 5)
        pts.append(p[ok])
        cb.append(part[ok] == 4)
        drawn += len(p)
    kept = sum(map(len, pts))
    return (np.concatenate(pts)[:SAMPLES], np.concatenate(cb)[:SAMPLES],
            float(np.prod(hi - lo)) * kept / drawn / SAMPLES)


def _pool(cb: np.ndarray, part: str, split: bool) -> np.ndarray:
    """Which of the points flagged cerebellum-or-not belong to `part`'s partition: the
    cerebellum is its own pool only when a lobe lives there."""
    return cb == (part == "cerebellum") if split else np.full(len(cb), part == "cerebrum")


@lru_cache(maxsize=1)
def _mesh() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Surface nets over a grid of GRID cells along BOX's longest side: one vertex per cell
    the surface crosses (the mean of its edge crossings, then projected onto the surface), one
    quad per crossed grid edge, counter-clockwise seen from outside; gradient normals."""
    lo, hi = np.array(BOX[0]), np.array(BOX[1])
    h = float((hi - lo).max()) / GRID
    n = np.ceil((hi - lo) / h).astype(int) + 1
    g = np.stack(np.meshgrid(*(lo[a] + h * np.arange(n[a]) for a in range(3)), indexing="ij"), -1)
    f = _field(g.reshape(-1, 3)).reshape(*n)
    ins = f < 0
    cs = tuple(int(x) for x in n - 1)
    acc, cnt, quads = np.zeros((*cs, 3)), np.zeros(cs), []
    for a in range(3):
        b, c = (a + 1) % 3, (a + 2) % 3
        s0, s1 = [slice(None)] * 3, [slice(None)] * 3
        s0[a], s1[a] = slice(0, -1), slice(1, None)
        f0, f1 = f[tuple(s0)], f[tuple(s1)]
        x = ins[tuple(s0)] != ins[tuple(s1)]
        t = np.where(x, f0 / np.where(x, f0 - f1, 1), 0)
        pt = (g[tuple(s0)] + (t * h)[..., None] * np.eye(3)[a]) * x[..., None]
        for db in (0, 1):  # an edge along a belongs to the 4 cells around it
            for dc in (0, 1):
                s = [slice(None)] * 3
                s[b], s[c] = slice(db, db + cs[b]), slice(dc, dc + cs[c])
                acc += pt[tuple(s)]
                cnt += x[tuple(s)]
        e = np.argwhere(x)
        e = e[(e[:, b] >= 1) & (e[:, b] < cs[b]) & (e[:, c] >= 1) & (e[:, c] < cs[c])]
        corners = []
        for ob, oc in ((0, 0), (1, 0), (1, 1), (0, 1)):  # counter-clockwise about +a
            k = e.copy()
            k[:, b] += ob - 1
            k[:, c] += oc - 1
            corners.append(np.ravel_multi_index(tuple(k.T), cs))
        q = np.stack(corners, 1)
        quads.append(np.where(ins[tuple(e.T)][:, None], q, q[:, ::-1]))  # inside below: faces +a
    act = cnt.ravel() > 0
    vid = np.cumsum(act) - 1
    v = acc.reshape(-1, 3)[act] / cnt.ravel()[act, None]
    for _ in range(2):  # onto the surface: Newton steps of at most half a cell
        gr = _grad(v)
        step = (_field(v) / np.maximum((gr * gr).sum(1), 1e-9))[:, None] * gr
        v = v - step * np.minimum(1, 0.5 * h / np.maximum(np.linalg.norm(step, axis=1),
                                                          1e-12))[:, None]
    q = vid[np.concatenate(quads)]
    return v, _unit(_grad(v)), np.concatenate([q[:, [0, 1, 2]], q[:, [0, 2, 3]]])


# ---- partition -----------------------------------------------------------------------------

def _assign(p: np.ndarray, s: np.ndarray, w: np.ndarray):
    """Power-diagram cell of each point and its distance to the nearest border (inf with one
    site); cell -1 with no site."""
    n, k = len(p), len(s)
    if k < 2:
        return np.full(n, 0 if k else -1), np.full(n, np.inf)
    cost = ((p[:, None, :] - s[None]) ** 2).sum(2) - w
    r = np.arange(n)
    a = np.argmin(cost, axis=1)
    c1 = cost[r, a]
    cost[r, a] = np.inf
    a2 = np.argmin(cost, axis=1)
    return a, (cost[r, a2] - c1) / (2 * np.linalg.norm(s[a] - s[a2], axis=1) + 1e-9)


def _capacity(p: np.ndarray, s: np.ndarray, target: np.ndarray, r2: np.ndarray,
              wt: np.ndarray | None = None, rounds: int = 60) -> np.ndarray:
    """Power weights until each site's cell holds about its target count of samples."""
    wt, target = np.zeros(len(s)) if wt is None else wt, np.maximum(target, 1)
    d = ((p[:, None, :] - s[None]) ** 2).sum(2)
    for _ in range(rounds):
        got = np.bincount(np.argmin(d - wt, axis=1), minlength=len(s))
        wt = wt + 0.5 * r2 * np.clip((target - got) / target, -1, 1)
    return wt


def _lloyd(q: np.ndarray, x: np.ndarray, share: np.ndarray, r: np.ndarray,
           aff: np.ndarray) -> np.ndarray:
    """A centroidal capacity-constrained power diagram, on every k-th sample: LLOYD rounds of
    capacity weights, then each site LLOYD_STEP of the way to its cell's centroid (round cells
    instead of the slabs that fixed, crowded sites cut), and each lobe and its TIES most affine
    lobes pulled TIE_PULL of the way back to touching (r_i + r_j apart) when the moves part them
    (plain Lloyd spread the fixture's families of related lobes apart)."""
    sub = q[::max(1, len(q) // 12000)]
    k = len(x)
    a = aff - 2 * np.eye(k)
    near = np.argsort(-a, axis=1, kind="stable")[:, :min(TIES, k - 1)]
    i, j = np.repeat(np.arange(k), near.shape[1]), near.ravel()
    wt = None
    for _ in range(LLOYD):
        wt = _capacity(sub, x, share * len(sub), r ** 2, wt, 20)
        cell = np.argmin(((sub[:, None, :] - x[None]) ** 2).sum(2) - wt, axis=1)
        cnt = np.bincount(cell, minlength=k)
        cen = np.stack([np.bincount(cell, sub[:, c], k) for c in range(3)], 1)
        x = np.where(cnt[:, None] > 0, x + LLOYD_STEP * (cen / np.maximum(cnt, 1)[:, None] - x), x)
        d = x[j] - x[i]
        dist = np.linalg.norm(d, axis=1)
        far = np.maximum(dist - TOUCH * (r[i] + r[j]), 0)
        mv = (TIE_PULL / 2 * far / np.maximum(dist, 1e-9))[:, None] * d
        dx = np.zeros_like(x)
        np.add.at(dx, i, mv)
        np.add.at(dx, j, -mv)
        x = x + dx / np.maximum(np.bincount(np.concatenate([i, j]), minlength=k), 1)[:, None]
    return x


def _mds(dist: np.ndarray, dim: int = 3) -> np.ndarray:
    k = len(dist)
    out = np.zeros((k, dim))
    if k < 2:
        return out
    j = np.eye(k) - 1 / k
    val, vec = np.linalg.eigh(-0.5 * j @ dist ** 2 @ j)
    top = np.argsort(val)[::-1][:dim]
    out[:, :len(top)] = vec[:, top] * np.sqrt(np.maximum(val[top], 0))
    return out


def _spread(x: np.ndarray, r: np.ndarray, pinned: list[int], c: np.ndarray,
            ext: np.ndarray) -> np.ndarray:
    """Push sites apart toward SPREAD x the sum of their region radii, inside FILL of the
    pool's extent `ext` around its centre `c`."""
    free = np.ones(len(x), bool)
    free[pinned] = False
    need = SPREAD * (r[:, None] + r[None])
    np.fill_diagonal(need, 0)
    for _ in range(60):
        d = x[:, None] - x[None]
        dist = np.linalg.norm(d, axis=2)
        np.fill_diagonal(dist, 1)
        push = np.clip(need - dist, 0, None) / dist
        x = x + 0.25 * free[:, None] * (push[..., None] * d).sum(1)
        e = (x - c) / ext
        x = c + ext * e * np.minimum(1, FILL / np.maximum(np.linalg.norm(e, axis=1),
                                                         1e-9))[:, None]
    return x


def _sites(clusters: list[dict], base: list, ring: np.ndarray, aff: np.ndarray) -> dict:
    """A full relayout of the lobes: {id: {body, site, w}} ({body: "ring"} on the ring)."""
    ids, size = [c["id"] for c in clusters], [c["count"] for c in clusters]
    out = {ids[k]: {"body": "ring"} for k in range(len(ids)) if ring[k]}
    rest = [k for k in range(len(ids)) if not ring[k]]
    groups = {"cerebrum": [k for k in rest if base[k] not in ROUTINES],
              "cerebellum": [k for k in rest if base[k] in ROUTINES]}
    p, cb, vol = _samples()
    for part, ks in groups.items():
        if not ks:
            continue
        q = p[_pool(cb, part, bool(groups["cerebellum"]))]
        ks = sorted(ks, key=lambda k: (-size[k], ids[k]))
        area = np.array([size[k] for k in ks], float) ** AREA_EXP
        share = area / area.sum()
        dist = 1 - aff[np.ix_(ks, ks)]
        np.fill_diagonal(dist, 0)
        x = _mds(dist)[:, [1, 2, 0]]  # the widest spread front to back, then across, then up
        x = x * np.where(x[0] < 0, -1, 1)  # the biggest lobe toward the front, top and right
        # whitened, then each lobe at its radius rank (uniform in the ball): MDS's few far
        # lobes would otherwise crowd the rest into the centre, and its main axis into slabs
        u = x - x.mean(0)
        u = u / np.maximum(u.std(0), 1e-9)
        rank = np.argsort(np.argsort(np.linalg.norm(u, axis=1), kind="stable"), kind="stable")
        x = _unit(u) * np.cbrt((rank + 0.5) / len(x))[:, None]
        x = x + (np.array([_u(f"site:{ids[k]}", 3) for k in ks]) - 0.5) * 1e-3  # exact ties
        pinned = [j for j, k in enumerate(ks) if base[k] in FRONTAL]
        for t, j in enumerate(pinned):
            x[j] = (0.35 * (t - (len(pinned) - 1) / 2), 0.1, 0.85)
        c, ext = q.mean(0), math.sqrt(5) * q.std(0)  # a uniform ellipsoid's semi-axes
        r = (3 * share * len(q) * vol / (4 * np.pi)) ** (1 / 3)
        x = _lloyd(q, _spread(c + x * ext * FILL, r, pinned, c, ext), share, r,
                   aff[np.ix_(ks, ks)])
        wt = _capacity(q, x, share * len(q), r ** 2)
        for j, k in enumerate(ks):
            out[ids[k]] = {"body": part, "site": np.round(x[j], 5).tolist(),
                           "w": round(float(wt[j]), 6)}
    return out


def _regions(lobes: dict, lid: list[str]):
    """Each pool's (samples, its sited lobe ids, sites, weights, cell of each sample, border
    distance) from the stored sites, and every sample's lobe index (-1 for none)."""
    p, cb, _ = _samples()
    split = any(e["body"] == "cerebellum" for e in lobes.values())
    li = {c: k for k, c in enumerate(lid)}
    geo, lab = {}, np.full(len(p), -1)
    for part in PARTS:
        m = _pool(cb, part, split)
        own = sorted(i for i, e in lobes.items() if e["body"] == part and "site" in e)
        s = np.array([lobes[i]["site"] for i in own], float).reshape(-1, 3)
        wt = np.array([lobes[i]["w"] for i in own], float)
        cell, gap = _assign(p[m], s, wt)
        geo[part] = (p[m], own, s, wt, cell, gap)
        if own:
            lab[m] = np.array([li.get(i, -1) for i in own])[cell]
    return geo, lab


def _adjacent(lab: np.ndarray, nl: int) -> np.ndarray:
    """Lobe pairs whose regions touch: their samples share a voxel, or two face-adjacent
    voxels, of a 32-cube grid over BOX."""
    p = _samples()[0]
    lo, hi = np.array(BOX[0]), np.array(BOX[1])
    g = np.minimum(((p - lo) / (hi - lo) * 32).astype(int), 31)
    ok = lab >= 0
    m = np.zeros((32, 32, 32, nl), np.float32)
    m[g[ok, 0], g[ok, 1], g[ok, 2], lab[ok]] = 1
    a = m.reshape(-1, nl).T @ m.reshape(-1, nl)
    for ax in range(3):
        x = np.moveaxis(m, ax, 0)
        t = x[:-1].reshape(-1, nl).T @ x[1:].reshape(-1, nl)
        a += t + t.T
    np.fill_diagonal(a, 0)
    return a >= 2


# ---- colour --------------------------------------------------------------------------------

def _hex(light: float, chroma: float, hue: float) -> str:
    """OKLCH to an sRGB hex colour; chroma is reduced until it fits the gamut."""
    a, b = chroma * math.cos(math.radians(hue)), chroma * math.sin(math.radians(hue))
    for _ in range(60):
        lms = (np.array([[1, 0.3963377774, 0.2158037573], [1, -0.1055613458, -0.0638541728],
                         [1, -0.0894841775, -1.2914855480]]) @ (light, a, b)) ** 3
        rgb = np.array([[4.0767416621, -3.3077115913, 0.2309699292],
                        [-1.2684380046, 2.6097574011, -0.3413193965],
                        [-0.0041960863, -0.7034186147, 1.7076147010]]) @ lms
        if rgb.min() >= 0 and rgb.max() <= 1:
            break
        a, b = 0.95 * a, 0.95 * b
    rgb = np.clip(rgb, 0, 1)
    s = np.where(rgb <= 0.0031308, 12.92 * rgb, 1.055 * rgb ** (1 / 2.4) - 0.055)
    return "#" + "".join(f"{round(float(x) * 255):02x}" for x in s)


def _hd(a, b) -> np.ndarray:
    d = np.abs(np.asarray(a, float) - b) % 360
    return np.minimum(d, 360 - d)


def _hues(want: np.ndarray, adj: np.ndarray, order: list[int]) -> dict[int, int]:
    """Whole-degree hues, HUE_GAP apart where lobes touch. First a colouring of the touching
    graph with 360 / HUE_GAP evenly spaced hues (the first lobe of `order` at its `want`): DSatur
    with backtracking (most distinct hues among touching lobes first, then most touching
    lobes, then `order`; the hues nearest a lobe's `want` tried first). Should that fail within
    its budget, a greedy pass in the same order takes the hue that falls least short, and
    lobes still short pick again. Then every lobe moves toward `want` as far as its gaps
    allow, HUE_MIN from all other lobes where it can. (Greedy in size order, as v3 did, left
    35 touching pairs under 60 degrees on the real vault; this leaves none.)"""
    cand, m = np.arange(360), 360 // HUE_GAP
    slot = (round(want[order[0]]) + HUE_GAP * np.arange(m)) % 360 if order else cand
    rank = {k: t for t, k in enumerate(order)}
    deg = {k: int(adj[k, order].sum()) for k in order}
    hue: dict[int, int] = {}
    steps = [0]

    def nxt(left):
        return max(left, key=lambda k: (len({hue[j] for j in hue if adj[k, j]}), deg[k],
                                        -rank[k]))

    def search(left):  # shortcut: recursion depth = lobe count, fine below ~900 lobes
        if not left:
            return True
        k = nxt(left)
        used = {hue[j] for j in hue if adj[k, j]}
        for h in sorted((int(h) for h in slot if h not in used),
                        key=lambda h: (_hd(h, want[k]), h)):
            steps[0] += 1
            if steps[0] > 20000:
                return False
            hue[k] = h
            if search([j for j in left if j != k]):
                return True
            del hue[k]
        return False

    def pick(k, apart=False):
        short, every = np.zeros(360), np.full(360, np.inf)
        for j, h in hue.items():
            if j != k:
                d = _hd(cand, h)
                every = np.minimum(every, d)
                short += np.maximum(HUE_GAP - d, 0) * adj[k, j]
        off = _hd(cand, want[k])
        ok = short == 0
        if ok.any():
            if apart and (ok & (every >= HUE_MIN)).any():
                ok &= every >= HUE_MIN
            return int(cand[ok][np.argmin(off[ok])])
        return int(cand[np.lexsort((off, short))[0]])

    if not search(list(order)):
        hue.clear()
        left = list(order)
        while left:
            k = nxt(left)
            left.remove(k)
            hue[k] = pick(k)
        for _ in range(20):
            bad = [k for k in order if any(adj[k, j] and _hd(hue[k], h) < HUE_GAP
                                           for j, h in hue.items())]
            for k in bad:
                hue[k] = pick(k)
    for _ in range(2):
        for k in order:
            hue[k] = pick(k, True)
    return hue


# ---- vectors, affinity, in-lobe layout -----------------------------------------------------

def _vectors(root, ids: list[str]) -> tuple[np.ndarray, np.ndarray, bool]:
    """Unit page vectors (each page's head chunk, else the mean of its chunks) from the
    semantic cache, a has-vector mask, and whether the cache was readable."""
    n = len(ids)
    try:
        with np.load(root / CACHE, allow_pickle=False) as z:
            vid, page, start, vecs = z["ids"], z["page"], z["start"], z["vecs"]
        if not len(page) == len(start) == len(vecs) or (len(page) and page.max() >= len(vid)):
            raise ValueError("inconsistent arrays")
        row = {str(i): k for k, i in enumerate(vid)}
        head = np.full(len(vid), -1)
        z0 = np.flatnonzero(start == 0)[::-1]
        head[page[z0]] = z0  # reversed, so the first head row of a page wins
        g = np.array([row.get(i, -1) for i in ids], dtype=np.int64)
        out = np.zeros((n, vecs.shape[1]), np.float32)
        h = np.where(g >= 0, head[g], -1)
        out[h >= 0] = vecs[h[h >= 0]]
        for k in np.flatnonzero((g >= 0) & (h < 0)):  # no head chunk: the mean of its chunks
            out[k] = vecs[page == g[k]].astype(np.float32).mean(0)
    except Exception as e:  # a disposable cache: lay the brain out by links instead
        if not isinstance(e, FileNotFoundError):
            log.warning("brain: vector cache unreadable, laying out by links: %s", e)
        return np.zeros((n, 0), np.float32), np.zeros(n, bool), False
    norm = np.linalg.norm(out, axis=1)
    has = np.isfinite(norm) & (norm > 0)
    out[has] /= norm[has, None]
    out[~has] = 0
    return out, has, True


def _knn(x: np.ndarray, k: int, block: int = 1024) -> tuple[np.ndarray, np.ndarray]:
    """Each row's k nearest other rows by dot product, best first (ties by index)."""
    n = len(x)
    k = max(0, min(k, n - 1))
    idx, sim = np.zeros((n, k), np.int64), np.zeros((n, k), np.float32)
    if not k:
        return idx, sim
    for s in range(0, n, block):
        m = x[s:s + block] @ x.T
        r = np.arange(len(m))
        m[r, s + r] = -np.inf
        part = np.argpartition(-m, k - 1, axis=1)[:, :k]
        val = np.take_along_axis(m, part, 1)
        o = np.lexsort((part, -val), axis=1)
        idx[s:s + block], sim[s:s + block] = (np.take_along_axis(part, o, 1),
                                              np.take_along_axis(val, o, 1))
    return idx, sim


def _affinity(cent, hasc, links: np.ndarray, pl: np.ndarray, clusters: list[dict]) -> np.ndarray:
    """A = 0.65 cos(centroids) + 0.25 cross-links / max + 0.10 shared parent (same lobe kind)."""
    n = len(clusters)
    cos = cent @ cent.T if cent.shape[1] else np.zeros((n, n))
    known = np.outer(hasc, hasc)
    off = known & ~np.eye(n, dtype=bool)
    cos = np.where(known, cos, cos[off].mean() if off.any() else 0.0)
    x = np.zeros((n, n))
    if len(links):
        la, lb = pl[links[:, 0]], pl[links[:, 1]]
        m = la != lb
        np.add.at(x, (la[m], lb[m]), 1)
        x = x + x.T
    x = x / x.max() if x.max(initial=0) else x
    fam = [{"topic": -1, "domain": -2, "group": -3}.get(c["kind"], k)
           for k, c in enumerate(clusters)]  # unfiled and archive share no parent
    return 0.65 * cos + 0.25 * x + 0.10 * np.equal.outer(fam, fam)


def _embed(v: np.ndarray, key: str) -> np.ndarray:
    """A lobe's pages in 3-D: PCA of their vectors, then EPOCHS rounds of relaxation in which
    each page is pulled toward its KNN nearest by cosine (weighted by it) and pushed from NEG
    seeded random pages (UMAP-style)."""
    n = len(v)
    if n < 4:
        return np.eye(3)[:n]
    x = v - v.mean(0)
    if n < x.shape[1]:  # PCA scores from the smaller Gram matrix: eigh of 384 x 384 is slow
        val, vec = np.linalg.eigh(x @ x.T)
        y = vec[:, -3:] * np.sqrt(np.maximum(val[-3:], 0))
    else:
        y = x @ np.linalg.eigh(x.T @ x)[1][:, -3:]
    y = (y - y.mean(0)) / (y.std() or 1) * 3
    nb, sim = _knn(v, KNN)
    i, j = np.repeat(np.arange(n), nb.shape[1]), nb.ravel()
    w = np.maximum(sim.ravel(), 0)
    w = w / (w.max() or 1)
    ii = np.repeat(np.arange(n), NEG)
    rng = np.random.default_rng(int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8)
                                               .digest(), "little"))
    for e in range(EPOCHS):
        d = y[i] - y[j]
        g = np.clip((-2 * w / (1 + (d * d).sum(1)))[:, None] * d, -4, 4)
        jj = rng.integers(0, n, len(ii))
        dn = y[ii] - y[jj]
        d2 = (dn * dn).sum(1)
        gn = np.clip((2 / ((0.001 + d2) * (1 + d2)))[:, None] * dn, -4, 4) * (ii != jj)[:, None]
        dy = np.stack([np.bincount(i, g[:, c], n) - np.bincount(j, g[:, c], n)
                       + np.bincount(ii, gn[:, c], n) for c in range(3)], 1)
        y = y + (1 - e / EPOCHS) * dy / nb.shape[1]
    return y


def _msqrt(c: np.ndarray, p: float) -> np.ndarray:
    val, vec = np.linalg.eigh(c)
    return vec @ np.diag(np.maximum(val, 1e-12) ** p) @ vec.T


def _fit(y: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Map a 3-D page layout into a region's samples `q` by matching FIT of their spread
    (mean and covariance): affine, so neighbours stay neighbours; settle() snaps the few
    pages left outside. (Filling the region by radius rank was measured on the real vault:
    0.65 of the 5 nearest kept, against 0.68 for this.)"""
    mq = q.mean(0)
    if len(y) < 4 or len(q) < 4:
        return mq + (y - y.mean(0)) * 1e-2
    yc = y - y.mean(0)
    return mq + FIT * yc @ _msqrt(np.cov(yc.T), -0.5) @ _msqrt(np.cov((q - mq).T), 0.5)


def _fill(y: np.ndarray, q: np.ndarray, key: str) -> np.ndarray:
    """Move a fitted layout FILL_OT of the way to its sliced optimal transport onto the
    region's samples `q` (seeded rotations; each axis matched by rank to q's quantiles): the
    gaps between the relaxation's clumps close and the lobe fills its round region, while
    neighbours mostly stay neighbours."""
    n = len(y)
    if n < 4 or len(q) < 4:
        return y
    rng = np.random.default_rng(int.from_bytes(hashlib.blake2b(f"fill:{key}".encode(),
                                                               digest_size=8).digest(), "little"))
    at = ((np.arange(n) + 0.5) / n * len(q)).astype(int)
    z = y.copy()
    for _ in range(40):
        rot = np.linalg.qr(rng.normal(size=(3, 3)))[0]
        pz, pq = z @ rot, q @ rot
        o = np.argsort(pz, axis=0, kind="stable")
        dz = np.empty_like(pz)
        np.put_along_axis(dz, o, np.sort(pq, axis=0)[at] - np.take_along_axis(pz, o, 0), 0)
        z = z + dz @ rot.T
    return y + FILL_OT * (z - y)


# ---- persistence ---------------------------------------------------------------------------

def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _read_layout(root) -> tuple[str, dict | None]:
    """The stored layout's text, and the layout when it is valid for these parameters."""
    try:
        text = (root / LAYOUT).read_text(encoding="utf-8")
        st = json.loads(text)
    except (OSError, ValueError):
        return "", None
    ok = (isinstance(st, dict) and st.get("version") == VERSION
          and st.get("params_hash") == PARAMS_HASH
          and isinstance(st.get("pages"), dict) and isinstance(st.get("lobes"), dict)
          and all(isinstance(v, list) and len(v) == 5 and all(map(_num, v[:3]))
                  and isinstance(v[3], str) and v[4] in (0, 1) for v in st["pages"].values())
          and all(isinstance(e, dict) for e in st["lobes"].values()))
    sited = ok and {i: e["body"] for i, e in st["lobes"].items() if e.get("body") in PARTS
                    and isinstance(e.get("site"), list) and len(e["site"]) == 3
                    and all(map(_num, e["site"])) and _num(e.get("w"))}
    ok = ok and all(e.get("body") == "ring" or i in sited or (
        isinstance(e.get("host"), str) and sited.get(e["host"]) == e.get("body"))
        for i, e in st["lobes"].items())
    return text, st if ok else None


def _day(s) -> int:
    """A date string's day number; 0 when it has none."""
    try:
        return date.fromisoformat(str(s)[:10]).toordinal()
    except ValueError:
        return 0


def _b64(a, dtype: str) -> str:
    return base64.b64encode(np.ascontiguousarray(a, dtype=dtype).tobytes()).decode("ascii")


def _r(v, nd=4) -> list[float]:
    return [round(float(x), nd) for x in v]


# ---- the blob ------------------------------------------------------------------------------

def build(vault, pages: list[dict], clusters: list[dict], relayout: bool = False) -> dict:
    """The dashboard's `brain` key (brain-v3.md §2, brain-v2.md §4) for build_data's pages and
    lobes; reads and, when it changed, rewrites `.cortex/brain-layout.json`."""
    root, n, nl = vault.root, len(pages), len(clusters)
    ids = [p["id"] for p in pages]
    at = {i: k for k, i in enumerate(ids)}
    lid = [c["id"] for c in clusters]
    li = {c: k for k, c in enumerate(lid)}
    pl = np.array([li[p["cluster"]] for p in pages], dtype=np.int64)
    vec, has, vec_ok = _vectors(root, ids)
    links = np.array(sorted({(min(a, b), max(a, b)) for a, p in enumerate(pages)
                             for b in (at[x] for x in p["links"] if x in at) if a != b}),
                     dtype=np.int64).reshape(-1, 2)
    deg = np.bincount(links.ravel(), minlength=n)
    nbrs: list[list[int]] = [[] for _ in range(n)]
    for a, b in links:
        nbrs[a].append(int(b))
        nbrs[b].append(int(a))
    base = [c["id"].removesuffix("-group") if c["kind"] == "group" else None for c in clusters]
    ring = np.array([c["kind"] == "archive" or b in ARCHIVE for c, b in zip(clusters, base,
                                                                            strict=True)], bool)
    cent = np.zeros((nl, vec.shape[1]), np.float32)
    np.add.at(cent, pl[has], vec[has])
    hasc = np.linalg.norm(cent, axis=1) > 0
    cent = _unit(cent) * hasc[:, None]
    aff = _affinity(cent, hasc, links, pl, clusters)

    old_text, st = _read_layout(root)
    changed = n if st is None else sum(
        1 for k, i in enumerate(ids) if (s := st["pages"].get(i)) is None
        or s[3] != lid[pl[k]] or (has[k] and not s[4]))
    full = relayout or st is None or changed > RELAYOUT_SHARE * n
    if full:
        st = {"version": VERSION, "params_hash": PARAMS_HASH, "pages": {},
              "lobes": _sites(clusters, base, ring, aff)}
    else:
        st["pages"] = {i: s for i in ids if (s := st["pages"].get(i)) and s[3] == lid[pl[at[i]]]}
    lobes = st["lobes"]
    for k in range(nl):  # a lobe new since the last layout: a guest in its most affine region
        if lid[k] in lobes:
            continue
        owners = [h for h in range(nl) if "site" in lobes.get(lid[h], {})]
        sited = sorted(i for i, e in lobes.items() if "site" in e)
        if ring[k] or not sited:
            lobes[lid[k]] = {"body": "ring"}
        else:
            host = lid[max(owners, key=lambda h: (aff[k, h], -h))] if owners else sited[0]
            lobes[lid[k]] = {"body": lobes[host]["body"], "host": host}
    ring_k = ring  # the lobe's kind; where it sits is the stored body
    ring = np.array([lobes[i]["body"] == "ring" for i in lid], bool)

    geo, lab = _regions(lobes, lid)
    vol = _samples()[2]

    def region(k):
        """(pool, cell index, the pool's sites and weights, interior samples) of lobe k."""
        e = lobes[lid[k]]
        q, own, s, wt, cell, gap = geo[e["body"]]
        j = own.index(e.get("host", lid[k]))
        m = cell == j
        inner = m & (gap >= GAP)
        q = q[inner if inner.any() else m]
        return e["body"], j, s, wt, q if len(q) else s[j:j + 1]

    regions = {k: region(k) for k in range(nl) if not ring[k]}
    centre, anchor, normal, area = (np.zeros((nl, 3)), np.zeros((nl, 3)), np.zeros((nl, 3)),
                                    np.zeros(nl))
    for k in range(nl):
        if ring[k]:
            centre[k], anchor[k], normal[k] = (0, RING_Y, 0), (0, RING_Y, RING_R), (0, 0, 1)
            continue
        part, j, _, _, q = regions[k]
        centre[k] = q.mean(0)
        anchor[k] = q[np.argmin(((q - centre[k]) ** 2).sum(1))]
        normal[k] = _unit(anchor[k])
        if "host" not in lobes[lid[k]]:
            area[k] = (geo[part][4] == j).sum() / SAMPLES

    # place every page that has no stored position
    pos = np.full((n, 3), np.nan)
    for i, s in st["pages"].items():
        pos[at[i]] = s[:3]

    def jitter(rows, tag, size):
        return (np.array([_u(f"{tag}:{ids[r]}", 3) for r in rows]).reshape(-1, 3) - 0.5) * size

    def settle(k, pts, rows):
        """Into lobe k's region: points outside it, near a border or near the surface snap
        to its nearest interior sample."""
        _, j, s, wt, q = regions[k]
        pts = np.array(pts, float)
        cell, gap = _assign(pts, s, wt)
        bad = np.flatnonzero((cell != j) | (gap < GAP / 2) | (_field(pts) > -MARGIN / 2))
        if len(bad):
            near = np.argmin((q * q).sum(1)[None] - 2 * pts[bad] @ q.T, axis=1)
            pts[bad] = q[near] + jitter([rows[i] for i in bad], "snap", 0.6 * GAP)
        pos[rows] = np.round(pts, 5)

    def spacing(k):
        return (max(area[k] * SAMPLES * vol, 1e-6) / max(clusters[k]["count"], 1)) ** (1 / 3)

    cand = ([k for k in range(nl) if not ring[k] and hasc[k]
             and clusters[k]["kind"] in ("topic", "domain")]
            or [k for k in range(nl) if not ring[k] and hasc[k]])

    def beacons(r):
        if not has[r] or not cand:
            return []
        sims = cent[cand] @ vec[r]
        return [cand[t] for t in sorted(range(len(cand)), key=lambda t: (-sims[t], t))[:N_BEACONS]]

    big = max(float(np.quantile(deg, 1 - VAULT_HUBS)), 1) if n else 1  # the vault's top hubs
    later = []
    for k in range(nl):
        rows = [r for r in np.flatnonzero(pl == k) if np.isnan(pos[r, 0])]
        if not rows:
            continue
        if ring[k]:
            for r in rows:
                u = _u(f"ring:{ids[r]}", 4)
                bl = beacons(r)
                if bl:
                    wgt = np.exp(20 * (cent[bl] @ vec[r] - 1))
                    a = math.atan2(float(wgt @ anchor[bl, 0]), float(wgt @ anchor[bl, 2]))
                else:
                    a = 2 * math.pi * u[3]
                a += (u[0] - 0.5) * 0.3
                rho, phi = RING_W * math.sqrt(u[1]), 2 * math.pi * u[2]  # in the band's section
                rr, y = RING_R + rho * math.cos(phi), RING_Y + rho * math.sin(phi)
                while _field(np.array([[rr * math.sin(a), y, rr * math.cos(a)]]))[0] < MARGIN:
                    y -= MARGIN  # clear of the cerebellum
                pos[r] = np.round((rr * math.sin(a), y, rr * math.cos(a)), 5)
            continue
        emb = [r for r in rows if has[r]]
        later += [r for r in rows if not has[r]]
        own_placed = [r for r in np.flatnonzero((pl == k) & has) if not np.isnan(pos[r, 0])]
        if emb and "host" not in lobes[lid[k]] and not own_placed:
            q = regions[k][4]  # a fresh region: the lobe's map, filling it
            y = _fill(_fit(_embed(vec[emb], lid[k]), q), q, lid[k])
            rank = sorted(range(len(emb)), key=lambda t: (-deg[emb[t]], ids[emb[t]]))
            hubs = [t for i, t in enumerate(rank) if deg[emb[t]] > 0
                    and (i < math.ceil(HUBS * len(emb)) or deg[emb[t]] >= big)]
            if hubs:  # the nucleus: the more links, the nearer the centre
                d = np.log1p(deg[[emb[t] for t in hubs]])
                y[hubs] += (HUB_PULL * d / d.max())[:, None] * (centre[k] - y[hubs])
            settle(k, y, emb)
            continue
        host = li.get(lobes[lid[k]].get("host"), k)
        for r in emb:  # next to its 3 nearest placed neighbours by vector, inside its lobe
            pool = [x for x in np.flatnonzero((pl == k) & has) if not np.isnan(pos[x, 0])] or [
                x for x in np.flatnonzero((pl == host) & has) if not np.isnan(pos[x, 0])]
            if not pool:
                later.append(r)
                continue
            sims = vec[pool] @ vec[r]
            best = sorted(range(len(pool)), key=lambda t: (-sims[t], t))[:3]
            wgt = np.exp(50 * (sims[best] - sims[best[0]]))  # the closest in meaning weighs most
            pt = wgt @ pos[[pool[t] for t in best]] / wgt.sum()
            settle(k, pt[None] + jitter([r], "new", 0.3 * spacing(k)), [r])
    for r in sorted(later, key=lambda r: (-deg[r], ids[r])):  # by links, else a seeded spot
        k = pl[r]
        placed = [x for x in nbrs[r] if not np.isnan(pos[x, 0])]
        if placed:
            pt = pos[placed].mean(0) + jitter([r], "link", 0.5 * spacing(k))[0]
        else:
            q = regions[k][4]
            pt = q[int(_u(f"seed:{ids[r]}", 1)[0] * len(q))] + jitter([r], "seed", 0.5 * GAP)[0]
        settle(k, pt[None], [r])

    for k, i in enumerate(ids):
        if i not in st["pages"]:
            st["pages"][i] = [*(float(x) for x in pos[k]), lid[pl[k]], int(has[k])]
    text = json.dumps(st, sort_keys=True, separators=(",", ":"))
    if text != old_text:
        try:
            vault.write_text(root / LAYOUT, text)
        except (VaultError, OSError) as e:  # a view cache: the next build lays it out again
            log.warning("brain layout not saved: %s", e)

    # depth inside the surface as placed; 0 on the ring
    dep = np.zeros(n)
    cortex = ~ring[pl] if n else np.zeros(0, bool)
    if cortex.any():
        dep[cortex] = np.maximum(-_field(pos[cortex]), 0)

    # one mesh; each vertex takes the lobe of its region in its pool
    vp, vn, tri = _mesh()
    split = any(e["body"] == "cerebellum" for e in lobes.values())
    vcb = np.argmin(_parts(vp), 1) == 4
    vl = np.full(len(vp), 65535)
    for part in PARTS:
        _, own, s, wt, _, _ = geo[part]
        m = _pool(vcb, part, split)
        if own and m.any():
            vl[m] = np.array([li.get(i, 65535) for i in own])[_assign(vp[m], s, wt)[0]]
    mesh = [{"name": "brain", "pos": _b64(vp, "<f4"), "nrm": _b64(vn, "<f4"),
             "idx": _b64(tri, "<u4"), "lobe": _b64(vl, "<u2")}]

    # colours: hue from the site's direction around the vertical axis, apart where regions touch
    adj = _adjacent(lab, nl) if nl else np.zeros((0, 0), bool)
    for k in range(nl):  # a guest touches its host and the host's neighbours
        h = li.get(lobes[lid[k]].get("host"))
        if h is not None:
            adj[k] |= adj[h]
            adj[:, k] |= adj[:, h]
            adj[k, h] = adj[h, k] = True
            adj[k, k] = False
    kind = [c["kind"] for c in clusters]
    tinted = [k for k in range(nl) if not ring[k] and kind[k] in ("topic", "domain", "group")]
    angle = {}
    for k in tinted:
        e = lobes[lid[k]]
        site, c = np.array(lobes[e.get("host", lid[k])]["site"]), geo[e["body"]][0].mean(0)
        angle[k] = (math.degrees(math.atan2(site[2] - c[2], site[0] - c[0]))
                    + (1e-3 if "host" in e else 0)) % 360
    # spread over the whole wheel in the same circular order, the biggest lobe at its angle
    order = sorted(tinted, key=lambda k: (angle[k], k))
    want = np.zeros(nl)
    for t, k in enumerate(order):
        want[k] = (angle[tinted[0]] + 360 * (t - order.index(tinted[0])) / len(order)) % 360
    hue = _hues(want, adj, tinted)
    color: list[str] = []
    for k in range(nl):
        light, chroma = ((0.74, 0.07 if kind[k] == "group" else 0.19) if k in hue
                         else (0.55 if ring[k] else 0.70, 0))
        c = _hex(light, chroma, hue.get(k, 0))
        while c in color:  # a hue shared by lobes that do not touch: a shade darker
            light -= 0.03
            c = _hex(light, chroma, hue.get(k, 0))
        color.append(c)

    wide = n >= 65535
    none = 0xFFFFFFFF if wide else 65535
    near = np.full((n, N_NEAR), none, np.int64)
    emb = np.flatnonzero(has)
    nb, _ = _knn(vec[emb], N_NEAR)
    near[emb, :nb.shape[1]] = emb[nb]

    # fibres: every link, then each embedded page's nearest by meaning that is not a link
    seen = {(int(a), int(b)) for a, b in links}
    sa, sb = [], []
    for r in emb:
        for t in near[r, :N_SIMILAR]:
            key = (min(int(r), int(t)), max(int(r), int(t)))
            if t != none and key not in seen:
                seen.add(key)
                sa.append(int(r))
                sb.append(int(t))
    fa = np.concatenate([links[:, 0], sa]).astype(np.int64)
    fb = np.concatenate([links[:, 1], sb]).astype(np.int64)
    home = np.where(ring[pl][:, None], pos, centre[pl]) if n else pos  # ring ends: no pull
    mid = (pos[fa] + pos[fb]) / 2
    seed = np.array([_u(f"fibre:{i}", 6) for i in ids]).reshape(-1, 6)  # a pair's: both ends'
    ctrl = (mid + PULL * ((home[fa] + home[fb]) / 2 - mid)
            + ((seed[fa, :3] + seed[fb, 3:]) % 1 - 0.5) * 2 * JITTER)
    fibres = {"a": _b64(fa, "<u4"), "b": _b64(fb, "<u4"), "c": _b64(ctrl, "<f4"),
              "k": _b64([0] * len(links) + [1] * len(sa), "u1")}

    # node weight for the renderer's size: links, similar-meaning fibres, recency of `updated`
    sdeg = np.bincount(np.array(sa + sb, dtype=np.int64), minlength=n)
    day = np.array([_day(p.get("updated")) for p in pages], float)
    recent = np.exp((day - day.max()) / RECENT) if n else day
    weight = (0.6 * np.log1p(deg) / max(math.log1p(deg.max(initial=0)), 1e-9)
              + 0.25 * np.log1p(sdeg) / max(math.log1p(sdeg.max(initial=0)), 1e-9)
              + 0.15 * recent)

    cross: dict = {}
    for a, b in links:
        la, lb = sorted((int(pl[a]), int(pl[b])))
        if la != lb and not ring[la] and not ring[lb]:
            cross[la, lb] = cross.get((la, lb), 0) + 1
    tracts = []
    for (a, b), cnt in sorted(cross.items(), key=lambda t: (-t[1], t[0]))[:N_TRACTS]:
        if cnt < 2:
            break
        p1, p2 = (anchor[a] + (anchor[b] - anchor[a]) * t for t in (1 / 3, 2 / 3))
        tracts.append({"a": a, "b": b, "n": cnt, "p": [
            _r(anchor[a]), _r(0.55 * p1), _r(0.55 * p2), _r(anchor[b])]})  # 45 % toward the centre

    return {
        "version": VERSION,
        "legend": LEGEND,
        "nodes": {"ids": ids, "xyz": _b64(pos, "<f4"), "lobe": _b64(pl, "<u2"),
                  "depth": _b64(dep, "<f4"), "embedded": _b64(has, "u1"),
                  "weight": _b64(np.clip(weight, 0, 1), "<f4")},
        "lobes": [{"id": c["id"], "title": c["title"], "kind": c["kind"], "count": c["count"],
                   "anchor": _r(anchor[k]), "normal": _r(normal[k]), "centre": _r(centre[k]),
                   "color": color[k], "archive": bool(ring_k[k]),
                   "area": round(float(area[k]), 4)} for k, c in enumerate(clusters)],
        "mesh": mesh,
        "fibres": fibres,
        "tracts": tracts,
        "near": {"ids": _b64(near, "<u4" if wide else "<u2")},
        "beacons": {str(r): bl for r in range(n) if ring[pl[r]] and (bl := beacons(r))},
        "stats": {"embedded": int(has.sum()), "unembedded": int(n - has.sum()),
                  "relayout": bool(full), "vectors": vec_ok},
    }


def preservation(xyz: np.ndarray, vec: np.ndarray, has: np.ndarray, lobe: np.ndarray,
                 skip=(), sample: int = 50, k3: int = 5, kv: int = 20) -> float:
    """§7's in-lobe check: for `sample` pages (seeded), the share of their k3 nearest same-lobe
    points in 3D that are among their kv nearest same-lobe pages by vector."""
    rows = [r for r in range(len(xyz)) if has[r] and lobe[r] not in skip
            and (has & (lobe == lobe[r])).sum() > kv]
    rows = sorted(rows, key=lambda r: _u(f"sample:{r}", 1)[0])[:sample]
    got = []
    for r in rows:
        m = np.flatnonzero(has & (lobe == lobe[r]))
        m = m[m != r]
        a = m[np.argsort(np.linalg.norm(xyz[m] - xyz[r], axis=1), kind="stable")[:k3]]
        b = m[np.argsort(-(vec[m] @ vec[r]), kind="stable")[:kv]]
        got.append(len(set(a) & set(b)) / k3)
    return float(np.mean(got)) if got else 1.0
