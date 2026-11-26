"""The numbers in README.md's "How well it works", measured on made-up data only.

Runs the existing benchmarks (scripts/bench_index.py, scripts/bench_scale.py and
scripts/bench_chat_data.py), each as its own process, --runs times, and keeps the median; runs
the offline Chat test with no model (`keepfold.chat_eval.run(use_model=False)`); counts the
tests (`pytest --collect-only`). Everything runs in temp folders under a temp HOME, so the
real ~/.keepfold, vault and browsing are never read; no model is ever loaded or downloaded.
Writes docs/benchmarks/results.json (numbers only), saved after each section, so a stopped run
keeps the sections that finished; readme_charts.py leaves out any section that is missing.

Regenerate every number, chart and README table (on a quiet machine; more than an hour on a
laptop):

    uv run python scripts/readme_numbers.py && uv run python scripts/readme_charts.py

`--quick` uses small sizes and one run (a smoke test, not for the README).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
OUT = REPO / "docs" / "benchmarks" / "results.json"
SECONDS = re.compile(r"^\s*(\S.*?)\s+(\d+\.\d+) s(?:\s|$)")
FIRST_AFTER = re.compile(r"^(\S.*?)\s+(\d+\.\d+) s first\s+(\d+\.\d+) s after")


def temp_home(tmp: Path) -> None:
    """Point HOME (and Windows' profile folders) at `tmp` for this process and its children."""
    h = tmp / "home"
    h.mkdir(parents=True)
    for var in ("HOME", "USERPROFILE"):
        os.environ[var] = str(h)
    for var in ("APPDATA", "LOCALAPPDATA"):
        os.environ[var] = str(h / var)
    for var in ("TEMP", "TMP", "TMPDIR"):  # the benches' own temp folders go in here too
        os.environ[var] = str(tmp)
    tempfile.tempdir = None
    Path.home = classmethod(lambda cls: h)  # type: ignore[method-assign]


def machine() -> dict:
    cpu = platform.processor()
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                cpu = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    return {"cpu": cpu, "logical_cores": os.cpu_count(), "os": platform.platform(),
            "python": platform.python_version()}


def load() -> dict:
    """The machine's load just before a run: CPU percent (Windows) or the 1-minute load
    average, and how many Python processes are running (other test suites)."""
    out: dict = {}
    if sys.platform == "win32":
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "(Get-CimInstance Win32_Processor | Measure-Object "
                                "LoadPercentage -Average).Average"],
                               capture_output=True, text=True, timeout=30)
            out["cpu_percent"] = float(r.stdout.strip())
            t = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                               text=True, timeout=30).stdout
            out["python_processes"] = t.lower().count('"python.exe"')
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    elif hasattr(os, "getloadavg"):
        out["load_avg_1min"] = os.getloadavg()[0]
    return out


def bench(script: str, args: list[str], runs: int, loads: list) -> tuple[list[str], list[str]]:
    """Run a bench script `runs` times; returns ([stdout of each run], the command)."""
    cmd = [sys.executable, str(HERE / script), *args]
    outs = []
    for i in range(runs):
        loads.append({"run": f"{script} {' '.join(args)} #{i + 1}", **load()})
        t = time.perf_counter()
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           cwd=REPO, timeout=3600)
        if r.returncode:
            raise SystemExit(f"{script} {args} failed:\n{r.stdout}\n{r.stderr}")
        print(f"  {script} {' '.join(args)} run {i + 1}: {time.perf_counter() - t:.0f} s",
              flush=True)
        outs.append(r.stdout)
    return outs, ["uv", "run", "python", f"scripts/{script}", *args]


def seconds(outs: list[str], want: dict[str, str]) -> dict:
    """{key: median seconds} of the lines labelled `want[key]`, plus every run's value."""
    got: dict = {k: [] for k in want}
    for out in outs:
        found = {m[1]: float(m[2]) for line in out.splitlines() if (m := SECONDS.match(line))}
        for k, label in want.items():
            if label not in found:
                raise SystemExit(f"no '{label}' line in:\n{out}")
            got[k].append(found[label])
    return {**{k: round(statistics.median(v), 3) for k, v in got.items()}, "runs": got}


def index(sizes: list[int], runs: int, loads: list) -> dict:
    points = []
    for n in sizes:
        outs, cmd = bench("bench_index.py", ["--notes", str(n)], runs, loads)
        p = seconds(outs, {"cold_build_s": "cold index build",
                           "snapshot_build_s": "build from snapshot",
                           "mcp_first_start_s": "MCP initialize, first start",
                           "mcp_next_start_s": "MCP initialize, next start"})
        indexed = re.search(r"(\d+) notes, snapshot ([\d.]+) MB", outs[-1])
        points.append({"notes": n, "command": cmd, "indexed_notes": int(indexed[1]),
                       "snapshot_mb": float(indexed[2]), **p})
    return {"script": "scripts/bench_index.py", "points": points}


def vectors(sizes: list[int], runs: int, loads: list) -> dict:
    points = []
    for n in sizes:
        outs, cmd = bench("bench_scale.py", ["vectors", "--notes", str(n)], runs, loads)
        p = seconds(outs, {"query_s": "query (vector part, warm)",
                           "add_page_s": "add one page",
                           "cold_start_s": "cold start (first query)",
                           "first_build_s": "first build (all chunks)"})
        m = re.search(r"(\d+) pages, (\d+) chunks", outs[-1])
        points.append({"pages": int(m[1]), "chunks": int(m[2]), "notes_arg": n,
                       "command": cmd, **p})
    return {"script": "scripts/bench_scale.py", "points": points}


def browse(visits: int, runs: int, loads: list) -> dict:
    outs, cmd = bench("bench_scale.py", ["browse", "--visits", str(visits)], runs, loads)
    return {"script": "scripts/bench_scale.py", "visits": visits, "command": cmd,
            **seconds(outs, {"first_import_s": f"first import ({visits} visits)",
                             "nothing_new_s": "nothing new",
                             "live_read_50_s": "live read, 50 new visits"})}


def chat_data(args: list[str], runs: int, loads: list) -> dict:
    outs, cmd = bench("bench_chat_data.py", args, runs, loads)
    calls: dict = {}
    for out in outs:
        for line in out.splitlines():
            if m := FIRST_AFTER.match(line):
                calls.setdefault(m[1], {"first": [], "after": []})
                calls[m[1]]["first"].append(float(m[2]))
                calls[m[1]]["after"].append(float(m[3]))
    if not calls:
        raise SystemExit(f"no timings in:\n{outs[-1]}")
    return {"script": "scripts/bench_chat_data.py", "command": cmd,
            "calls": {k: {"first_s": statistics.median(v["first"]),
                          "after_s": statistics.median(v["after"]), "runs": v}
                      for k, v in calls.items()}}


def chat_test() -> dict:
    """The offline Chat test with no model: model questions come back blocked, never failed."""
    from keepfold import chat_eval

    r = chat_eval.run(use_model=False)
    return {"source": "keepfold.chat_eval.run(use_model=False) (`keepfold chat eval --no-model`)",
            "result": r["result"], "model_note": r["model_note"], "total": r["total"],
            "ran": r["ran"], "passed": r["passed"], "failed": r["failed"],
            "needs_model": r["blocked"],
            "by_query_kind": {k: {"pass": v["pass"], "fail": v["fail"],
                                  "needs_model": v["blocked"]}
                              for k, v in r["queries"].items()},
            "by_kind": {k: {"pass": v["pass"], "fail": v["fail"], "needs_model": v["blocked"]}
                        for k, v in r["kinds"].items()},
            "retrieval_cases": r["retrieval"]["cases"],
            "facts_right": r["retrieval"]["facts"],
            "blockers": [{"name": b["name"], "state": b["state"]} for b in r["blockers"]],
            "latency_median_s": r["latency"]["median"], "latency_max_s": r["latency"]["max"]}


def test_count() -> dict:
    r = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                        "-p", "no:cacheprovider"], capture_output=True, text=True,
                       encoding="utf-8", cwd=REPO, timeout=600)
    m = re.search(r"(\d+) tests? collected", r.stdout)
    if not m:
        raise SystemExit(f"pytest --collect-only gave no count:\n{r.stdout[-2000:]}")
    return {"collected": int(m[1]), "command": "uv run pytest --collect-only -q"}


def git_sha() -> str:
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                       text=True, cwd=REPO)
    return r.stdout.strip() or "unknown"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--index-sizes", default="1000,2500,5000,10000")
    ap.add_argument("--vector-sizes", default="1000,2500,5000,10000")
    # shortcut: 50,000 visits, not bench_scale's 200,000, so three runs finish in reasonable
    # time on a laptop; raise it on a quiet machine
    ap.add_argument("--visits", type=int, default=50_000)
    ap.add_argument("--chat-data", default="", help="extra bench_chat_data.py arguments")
    ap.add_argument("--quick", action="store_true", help="small sizes, one run: a smoke test")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    if args.quick:
        args.runs, args.index_sizes, args.vector_sizes, args.visits = 1, "200,400", "100,200", 2000
        args.chat_data = args.chat_data or ("--days 5 --keys 50 --sites 100 --notes 300 "
                                            "--events 500 --repeat 1")
    tmp = Path(tempfile.mkdtemp(prefix="keepfold-readme-"))
    temp_home(tmp)
    loads: list = []
    result = {"generated": datetime.now().astimezone().isoformat(timespec="seconds"),
              "git_sha": git_sha(), "machine": machine(), "runs": args.runs,
              "statistic": f"median of {args.runs} runs", "quick": args.quick}
    args.out.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        """Write what has finished so far, so a stopped run keeps its finished sections."""
        cpu = [x["cpu_percent"] for x in loads if "cpu_percent" in x]
        result["finished"] = datetime.now().astimezone().isoformat(timespec="seconds")
        result["load"] = {"cpu_percent_median": statistics.median(cpu) if cpu else None,
                          "cpu_percent_max": max(cpu) if cpu else None,
                          "before_each_run": loads}
        args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")

    steps = [
        ("index", lambda: index([int(s) for s in args.index_sizes.split(",")], args.runs, loads)),
        ("vectors", lambda: vectors([int(s) for s in args.vector_sizes.split(",")], args.runs,
                                    loads)),
        ("browse", lambda: browse(args.visits, args.runs, loads)),
        ("chat_data", lambda: chat_data(args.chat_data.split(), args.runs, loads)),
        ("chat_test", chat_test),
        ("tests", test_count),
    ]
    try:
        for key, run in steps:
            print(key, flush=True)
            result[key] = run()
            save()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
