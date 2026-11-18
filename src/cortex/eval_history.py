"""`cortex eval all`: one eval report and its paired history (v6 CONSENSUS "one eval history").

`.cortex/eval-history.jsonl` (tool-private, gitignored): one row per metric per run,
`{date, git_sha, index, metric, variant, value, n, baseline_value}`. Numbers only, never a
query or page text. The recall eval replays old queries on today's index, which grows, so
absolute numbers from different days are not comparable (panel X4). A run therefore pairs
only with rows of the same local `date` and `index` (a hash of the indexed files' size and
mtime, and whether vectors were used): `baseline_value` is `current` for each recall variant
(the constant base-rate Brier for learning), and the printer shows each metric's delta against
the latest earlier run on that day and index from another `git_sha`, the before/after pair.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cortex import learning_eval, recall_log
from cortex.session import _git
from cortex.vault import Vault

FILE = ".cortex/eval-history.jsonl"


def fingerprint(index, embed: bool) -> str:
    """Same key = same indexed pages (path, mtime, size) and the same recall mode."""
    stats = sorted((index._rel(p), st) for p, st in index._stats.items())
    return hashlib.sha1(repr((stats, embed)).encode()).hexdigest()[:12]


def git_sha() -> str:
    """The code's commit (`-dirty` with uncommitted changes), or "unknown" outside a repo."""
    try:
        out = _git(Path(__file__).parent, "describe", "--always", "--dirty", "--exclude=*")
    except OSError:
        return "unknown"
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "unknown"


def rows(recall: dict, n_queries: int, k: int, learning: dict, *, date: str, sha: str,
         index: str) -> list[dict]:
    """History rows for one run: recall@k and MRR per variant against `current`, and the
    learning Brier against its base rate. A signal with n = 0 has no rows."""
    base = {"date": date, "git_sha": sha, "index": index}
    out = []
    cur = recall.get("current")
    for variant, m in recall.items() if n_queries and cur else ():
        if m is None:  # rerank variant without the reranker
            continue
        for metric, value, b in ((f"recall@{k}", m[0], cur[0]), (f"mrr@{k}", m[1], cur[1])):
            out.append({**base, "metric": metric, "variant": variant, "value": round(value, 4),
                        "n": n_queries, "baseline_value": round(b, 4)})
    if learning.get("n"):
        out.append({**base, "metric": "brier", "variant": "all", "value": learning["brier"],
                    "n": learning["n"], "baseline_value": learning["brier_base"]})
    return out


def read(vault: Vault) -> list[dict]:
    path = vault.root / FILE
    out = []
    for line in path.read_text("utf-8").splitlines() if path.is_file() else []:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def append(vault: Vault, new: list[dict]) -> None:
    path = vault.root / FILE
    with vault.lock():  # the lock creates .cortex/
        old = path.read_text("utf-8").splitlines() if path.is_file() else []
        lines = [json.dumps(r, separators=(",", ":")) for r in new]
        vault.write_text(path,"\n".join([*old, *lines]) + "\n")


def before(history: list[dict], run: list[dict]) -> dict[tuple[str, str], dict]:
    """(metric, variant) -> the latest earlier row on the run's day and index, with the same
    n, from another git_sha. Rows from other days or another index never pair."""
    if not run:
        return {}
    key = (run[0]["date"], run[0]["index"])
    n = {(r["metric"], r["variant"]): r["n"] for r in run}
    return {(r["metric"], r["variant"]): r for r in history
            if (r.get("date"), r.get("index")) == key and r.get("git_sha") != run[0]["git_sha"]
            and n.get((r.get("metric"), r.get("variant"))) == r.get("n")}


def text(run: list[dict], prior: dict[tuple[str, str], dict]) -> str:
    if not run:
        return "nothing to measure yet: no opened recall queries and no graded items"
    r0 = run[0]
    shas = sorted({r["git_sha"] for r in prior.values()})
    out = [f"eval all {r0['date']} at {r0['git_sha']}, index {r0['index']}",
           f"{'metric':<10} {'variant':<12} {'value':>7} {'n':>5} {'vs base':>8} "
           f"{'vs ' + (shas[-1] if len(shas) == 1 else 'before'):>14}"]
    for r in run:
        floor = learning_eval.MIN_PAIRS if r["metric"] == "brier" else recall_log.MIN_TRUST
        p = prior.get((r["metric"], r["variant"]))
        paired = f"{r['value'] - p['value']:+.3f}" if p else "-"
        out.append(f"{r['metric']:<10} {r['variant']:<12} {r['value']:>7.3f} {r['n']:>5} "
                   f"{r['value'] - r['baseline_value']:>+8.3f} {paired:>14}"
                   + ("  (too few)" if r["n"] < floor else ""))
    if not prior:
        out.append("no earlier run on this day and index from another commit: "
                   "run this before and after a change, same day, to get a paired delta")
    return "\n".join(out)
