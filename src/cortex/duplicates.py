"""`cortex concept duplicates` (v6 agenda 8): candidate pairs of catalog concepts that probably
mean the same thing, each with its evidence and the `cortex concept merge SRC DST` command to
run. Read-only: it never merges, writes or downloads. Private concepts (and everything under a
private domain or area, `planner.hidden`) are left out; concept text is untrusted and printed
through `render.plain` with control characters dropped.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

import numpy as np

from cortex import planner, render
from cortex.catalog import Catalog, slugify
from cortex.index import _edge_targets
from cortex.vault import ID_RE, Vault

LIMIT = 20
NAME_RATIO = 0.85  # same as the add-time check; 0.8-0.85 was all noise when judged
JACCARD = 0.5  # token overlap of the slugified titles and aliases
COSINE = 0.85  # embedding cosine of "title; aliases", only when the model is already present
# score weights: lexical and semantic evidence make a candidate; parent and problems only add
W_NAME, W_JACCARD, W_COSINE, W_PROBLEMS, W_PARENT, W_DOMAIN = 0.35, 0.25, 0.25, 0.1, 0.05, 0.02
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


@dataclass
class Pair:
    src: str
    dst: str
    score: float
    evidence: list[str] = field(default_factory=list)
    titles: tuple[str, str] = ("", "")

    @property
    def command(self) -> str:
        return f"cortex concept merge {self.src} {self.dst}"


def safe(text: str, limit: int = 80) -> str:
    """Untrusted concept text as one plain terminal line (no links, HTML or control chars)."""
    return render.plain(_CTRL.sub("", str(text or "")), limit)


def _matchers(names: dict[str, str]) -> list[difflib.SequenceMatcher]:
    """One matcher per name with it as seq2: difflib caches its index for seq2."""
    out = []
    for n in names:
        m = difflib.SequenceMatcher(None)
        m.set_seq2(n)
        out.append(m)
    return out


def _best_ratio(a: dict[str, str], b: dict[str, str],
                mb: list[difflib.SequenceMatcher]) -> tuple[float, str]:
    """(best name similarity, the name of `a` it came from); a and b map name -> slug, `mb`
    is `_matchers(b)`. Below NAME_RATIO only the fact that it is below matters."""
    same = next((x for x, s in a.items() if s and s in b.values()), None)
    if same is not None:
        return 1.0, same
    best = (0.0, "")
    for x in a:
        for m in mb:
            lo = max(best[0], NAME_RATIO)
            if 2 * min(len(x), len(m.b)) / ((len(x) + len(m.b)) or 1) < lo:
                continue  # the length bound alone rules it out
            m.set_seq1(x)
            if m.quick_ratio() >= lo and (r := m.ratio()) > best[0]:
                best = (r, x)
    return best


def find(vault: Vault, embed=None, limit: int = LIMIT) -> tuple[list[Pair], int, int]:
    """(the top `limit` candidate pairs by score, how many candidates in all, how many
    concepts were compared). `embed` is semantic.load_embedder() or None; never a download."""
    cat = Catalog.load(vault)
    hidden = planner.hidden(vault, cat)
    cons = [e for e in cat.entries.values()
            if e.kind == "concept" and e.id not in hidden and ID_RE.fullmatch(e.id)]
    names = {e.id: {n.casefold(): slugify(n) for n in (e.title, *e.aliases)} for e in cons}
    toks = {e.id: {t for s in names[e.id].values() for t in s.split("-") if t} for e in cons}
    mats = {e.id: _matchers(names[e.id]) for e in cons}
    problems: dict[str, set[str]] = {e.id: set() for e in cons}
    for p in vault.iter_notes("problem"):
        for c in _edge_targets(p.fields.get("concepts") or []):
            if c in problems:
                problems[c].add(p.id)
    sims = None
    if embed is not None and len(cons) > 1:
        v = np.asarray(embed(["; ".join((e.title, *e.aliases)) for e in cons]), np.float32)
        v /= np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
        sims = v @ v.T

    out = []
    # shortcut: every pair is compared (about 3 s for 1,000 concepts); block by token or
    # trigram if the catalog grows past a few thousand.
    for i, a in enumerate(cons):
        for j in range(i + 1, len(cons)):
            b = cons[j]
            ta, tb = toks[a.id], toks[b.id]
            jac = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
            ratio, na = _best_ratio(names[a.id], names[b.id], mats[b.id])
            cos = float(sims[i, j]) if sims is not None else None
            if ratio < NAME_RATIO and jac < JACCARD and (cos is None or cos < COSINE):
                continue
            shared = problems[a.id] & problems[b.id]
            ev = [f"same name after normalising ('{safe(na, 40)}')" if ratio == 1.0 else
                  f"name similarity {ratio:.2f}"] if ratio else []
            ev.append(f"token overlap {jac:.2f}")
            score = W_NAME * ratio + W_JACCARD * jac
            if cos is not None:
                ev.append(f"embedding cosine {cos:.2f}")
                score += W_COSINE * max(cos, 0.0)
            if a.parent and a.parent == b.parent:
                ev.append(f"same area {a.parent}")
                score += W_PARENT
            elif a.domain and a.domain == b.domain:
                ev.append(f"same domain {a.domain}")
                score += W_DOMAIN
            if shared:
                ev.append(f"{len(shared)} shared problem{'s' if len(shared) > 1 else ''}")
                score += W_PROBLEMS * min(len(shared), 2) / 2
            # keep the concept with more linked problems, then more aliases, then the shorter id
            dst, src = sorted((a, b), key=lambda e: (-len(problems[e.id]), -len(e.aliases),
                                                     len(e.id), e.id))
            out.append(Pair(src.id, dst.id, round(score, 3), ev, (src.title, dst.title)))
    out.sort(key=lambda p: (-p.score, p.dst, p.src))
    return out[:max(limit, 0)], len(out), len(cons)


def report(pairs: list[Pair], total: int, compared: int) -> str:
    if not pairs:
        return f"No likely duplicates among {compared} concepts. Nothing was merged."
    lines = [f"{total} candidate pair{'s' if total != 1 else ''} among {compared} concepts"
             f" (showing {len(pairs)}). Read-only: nothing was merged; check each before "
             "running its command (SRC folds into DST)."]
    for k, p in enumerate(pairs, 1):
        lines += ["", f"{k}. {p.src} -> {p.dst}  score {p.score:.2f}",
                  f"   '{safe(p.titles[0])}' / '{safe(p.titles[1])}'",
                  f"   evidence: {'; '.join(p.evidence)}", f"   run: {p.command}"]
    return "\n".join(lines)
