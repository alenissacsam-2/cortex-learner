"""Spec 1 benchmark (docs/design/v5-research/REVIEW-1005.md §4.5) on a synthetic vault.

Generates N notes with the real vault's kind and size mix (§1.1) in a temp directory, then
times a cold index build, a build from the snapshot, and the MCP `initialize` round trip of
`keepfold mcp` over stdio. Never touches a real vault.

    uv run python scripts/bench_index.py [--notes 5000] [--keep]
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from keepfold import vault as vault_mod
from keepfold.catalog import Catalog
from keepfold.index import SNAPSHOT, Index
from keepfold.vault import Vault

# §1.1 counts of .md files per folder (5,198 in all)
MIX = {"resources": 3682, "notes": 477, "concepts": 397, "problems": 281, "mistakes": 60,
       "areas": 81, "domains": 19, "journal": 60, "sessions": 63, "projects": 27,
       "decisions": 17, "loops": 13, "people": 12, "me": 9}
WORDS = ("array list python pointer graph tree heap stack queue sort search cache memory "
         "thread lock index query model vector learn note topic chat recall review").split()


def body_len(rng: random.Random, folder: str) -> int:
    if folder == "resources":  # chat pages: p50 2.6k, p90 10k, max 60k characters
        return min(60_000, int(rng.lognormvariate(7.86, 1.05)))
    return rng.randint(200, 1500)


def text(rng: random.Random, n: int) -> str:
    out, size = [], 0
    while size < n:
        line = " ".join(rng.choice(WORDS) for _ in range(12))
        out.append(f"## {line[:30]}\n" if rng.random() < 0.05 else line + "\n")
        size += len(out[-1])
    return "".join(out)


def generate(root: Path, notes: int, seed: int = 0) -> None:
    rng = random.Random(seed)
    total = sum(MIX.values())
    ids = []
    for folder, n in MIX.items():
        (root / folder).mkdir(parents=True)
        ids += [(folder, f"{folder[:3]}-{i}") for i in range(max(1, n * notes // total))]
    for folder, id in ids:
        links = ", ".join(f'"[[{rng.choice(ids)[1]}]]"' for _ in range(rng.randint(0, 3)))
        fm = (f'---\ntitle: "{id.replace("-", " ")}"\ntags: ["t{rng.randint(0, 40)}"]\n'
              f"related: [{links}]\nsummary: \"{text(rng, 80).strip()[:100]}\"\n---\n")
        (root / folder / f"{id}.md").write_text(
            f"{fm}# {id}\n\n{text(rng, body_len(rng, folder))}", encoding="utf-8")


def timed(label: str, fn):
    t = time.perf_counter()
    out = fn()
    print(f"{label:<28}{time.perf_counter() - t:8.2f} s")
    return out


def initialize_round_trip(root: Path) -> float:
    exe = shutil.which("keepfold", path=str(Path(sys.executable).parent))
    if exe is None:
        raise SystemExit("keepfold executable not found next to this Python; run via uv run")
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                      "clientInfo": {"name": "bench", "version": "0"}}}
    t = time.perf_counter()
    p = subprocess.Popen([exe, "mcp", "--vault", str(root)], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        p.stdin.write((json.dumps(msg) + "\n").encode())
        p.stdin.flush()
        line = p.stdout.readline()
        dt = time.perf_counter() - t
        if b'"result"' not in line:
            raise SystemExit(f"no initialize result: {line[:200]!r}")
        return dt
    finally:
        p.kill()
        p.wait()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notes", type=int, default=5000)
    ap.add_argument("--keep", action="store_true", help="keep the temp vault")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="keepfold-bench-"))
    root = tmp / "vault"
    try:
        timed(f"generate {args.notes} notes", lambda: generate(root, args.notes))
        # §1.4 measured with a warm OS file cache; the first open of a new file also pays the
        # virus scanner (~13 ms a file on Windows), which would swamp the build itself
        timed("first read (warm cache)", lambda: [p.read_bytes() for p in root.rglob("*.md")])
        v = Vault(root)
        cat = Catalog.load(v)
        idx = timed("cold index build", lambda: Index(v, cat))
        print(f"{'':<28}{len(idx._notes)} notes, snapshot "
              f"{(root / SNAPSHOT).stat().st_size / 1e6:.1f} MB")
        vault_mod._PARSED.clear()  # a new process: no in-memory parse cache
        timed("build from snapshot", lambda: Index(Vault(root), cat))
        # the first start also runs the catalog sync of a vault it has never seen
        print(f"{'MCP initialize, first start':<28}{initialize_round_trip(root):8.2f} s")
        print(f"{'MCP initialize, next start':<28}{initialize_round_trip(root):8.2f} s")
    finally:
        if args.keep:
            print(f"vault kept at {root}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
