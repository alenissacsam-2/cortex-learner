"""Chat's data answers on a heavy synthetic vault: site lists, digests, study answers and a
whole `chat.reply`, each timed (best of --repeat runs after one warm-up).

Builds, in a temp folder only, `.keepfold/browse/site-days.json` with --days days of --keys
site keys each (page maps of --urls URLs, browser counts, hours, a few searches; read into
the browsing store's site_days table as an import does), a sites
cache of --sites entries, --notes notes and --events events. Every domain (s123.example),
title and term is made up. A temp HOME holds the config, so the real ~/.keepfold is never read.

    uv run python scripts/bench_chat_data.py [--days 90] [--keys 2000] [--urls 20]
        [--profile "site_list all"] [--keep | --vault <a folder kept by --keep>]
"""

from __future__ import annotations

import argparse
import cProfile
import importlib.util
import json
import os
import pstats
import random
import shutil
import tempfile
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
TODAY = date(2026, 11, 18)
ALL_ON = {"notes": True, "chats": True, "browsing": True, "private": True}
BROWSERS = ("chrome", "edge", "firefox")
WORDS = ("intro guide notes graph tree heap stack queue sort search cache memory thread "
         "lock index query model vector learn topic review lecture video forum").split()


def _bench_index():
    spec = importlib.util.spec_from_file_location("bench_index", HERE / "bench_index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _words(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n))


def site_days(rng: random.Random, days: int, keys: int, urls: int, sites: int) -> dict:
    """{day: {site key: counts}} in `sites.record`'s format; each site has a pool of pages it
    revisits, so a URL recurs across days as in real history."""
    names = [f"s{i}.example" for i in range(sites)]
    pool = {s: [f"https://{'docs.' if j % 5 == 0 else 'www.'}{s}/p/{j}" for j in range(urls * 2)]
            for s in names}
    titles = {u: _words(rng, 5).title() for ps in pool.values() for u in ps}
    out: dict = {}
    for d in range(days):
        day = (TODAY - timedelta(days=d)).isoformat()
        base = int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp())
        dk: dict = {}
        for s in rng.sample(names, min(keys, sites)):
            key = f"{s}||{int(rng.random() < 0.05)}"
            n = rng.randint(1, 60)
            a = base + rng.randint(0, 40000)
            w = {b: c for b in BROWSERS if (c := rng.randint(0, n))}
            p = {u: [titles[u], rng.randint(1, 5), rng.randint(0, 600)]
                 for u in rng.sample(pool[s], urls)}
            e = {"n": n, "s": rng.randint(0, 3600), "a": a, "b": a + rng.randint(0, 40000),
                 "w": w, "hr": {str(rng.randint(0, 23)): n}, "p": p}
            if rng.random() < 0.1:
                e["q"] = {_words(rng, 3): 1}
            if rng.random() < 0.1:
                e["co"] = {f"{rng.choice(names)}||0": 1 for _ in range(3)}
            dk[key] = e
        out[day] = dk
    return out


def llm_cache(rng: random.Random, sites: int) -> dict:
    cats = ("programming", "learning", "news", "video", "reference")
    return {f"s{i}.example": {"category": rng.choice(cats),
                              "v": {"desc": f"A made-up site about {_words(rng, 3)}"}}
            for i in range(sites)}


def catalog(concepts: list[str]) -> str:
    lines = ["schema: 1", "domains:", '  - id: "bench"', '    title: "Bench"', "    areas:"]
    for a in range(8):
        lines += [f'      - id: "bench.a{a}"', f'        title: "Area {a}"', "        concepts:"]
        for c in concepts[a::8]:
            lines += [f'          - id: "{c}"', f'            title: "Concept {c}"',
                      "            aliases: []", "            requires: []"]
    return "\n".join(lines) + "\n"


def events(rng: random.Random, root: Path, n: int, concepts: list[str], mistakes: list[str]):
    from ulid import ULID

    by_file: dict[Path, list[str]] = {}
    start = datetime.combine(TODAY, datetime.min.time(), UTC) - timedelta(days=89)
    for _ in range(n):
        t = start + timedelta(seconds=rng.randint(0, 90 * 86400))
        kind = rng.choices(("grade", "taught", "ask"), (7, 2, 1))[0]
        ev = {"id": str(ULID.from_datetime(t)), "ts": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "day": t.date().isoformat(), "session": "cli", "kind": kind, "source": "tutor",
              "object": f"concept:{rng.choice(concepts)}"}
        ev["key"] = ev["id"]
        if kind == "grade":
            ev["verdict"] = rng.choice(("correct", "partial", "wrong"))
            ev["confidence"] = rng.randint(1, 4)
            if ev["verdict"] == "wrong" and mistakes and rng.random() < 0.3:
                ev["mistakes"] = [rng.choice(mistakes)]
        elif kind == "ask":
            ev["question"] = _words(rng, 6) + "?"
        ev["schema"] = 1
        path = root / "events" / ev["day"][:4] / ev["day"][5:7] / "cli.jsonl"
        by_file.setdefault(path, []).append(json.dumps(ev, separators=(",", ":")))
    for path, lines in by_file.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build(root: Path, *, days: int, keys: int, urls: int, sites: int, notes: int,
          n_events: int, seed: int = 0) -> None:
    """A synthetic vault at `root` (made-up data only)."""
    rng = random.Random(seed)
    _bench_index().generate(root, notes, seed)
    concepts = sorted(p.stem for p in (root / "concepts").glob("*.md"))
    mistakes = sorted(p.stem for p in (root / "mistakes").glob("*.md"))
    (root / "catalog.yaml").write_text(catalog(concepts), encoding="utf-8")
    events(rng, root, n_events, concepts, mistakes)
    browse = root / ".keepfold" / "browse"
    browse.mkdir(parents=True)
    (browse / "site-days.json").write_text(
        json.dumps(site_days(rng, days, keys, urls, sites), separators=(",", ":")),
        encoding="utf-8")
    (browse / "sites.json").write_text(json.dumps({"llm": llm_cache(rng, sites), "hashes": {}}),
                                       encoding="utf-8")
    from keepfold.browse import legacy
    from keepfold.vault import Vault

    legacy.migrate(Vault(root))  # the counts in the browsing store, as an import does


def calls(vault) -> dict:
    """{name: zero-argument call}: every data answer Chat gives."""
    from keepfold import chat, chat_study
    from keepfold.browse import load_settings, sites

    def when(q):
        return chat._when(q, TODAY)

    def sides():
        days, _ = sites.load_days(vault)
        side, _ = sites._sides(load_settings(), sites.load_cache(vault)["llm"], days)
        return sum(side(d, k) is not None for d, ks in days.items() for k in ks)

    def reply():
        out: list = []
        chat.reply(vault, None, "which sites did I visit last week?", None, out.append, None,
                   dict(chat.DEFAULTS, sources=ALL_ON, history=False), today=TODAY)
        assert any(e["t"] == "done" for e in out), out
    week = (TODAY - timedelta(days=6), TODAY)
    return {
        "load_days": lambda: sites.load_days(vault),
        "lean_days": lambda: sites.lean_days(vault),
        "sides (build + every key)": sides,
        "site_list all days": lambda: chat.site_list(
            vault, "which sites did I visit?", None, ALL_ON),
        "site_list one day": lambda: chat.site_list(
            vault, "which sites did I visit yesterday?", when("yesterday"), ALL_ON),
        "site_list one browser": lambda: chat.site_list(
            vault, "which sites did I visit in Chrome?", None, ALL_ON),
        "digest a day": lambda: chat.digest(vault, TODAY, TODAY, ALL_ON, TODAY),
        "digest a week": lambda: chat.digest(vault, *week, ALL_ON, TODAY),
        **{f"study {k}": (lambda k=k: chat_study.answer(vault, k, (*week, None), TODAY))
           for k in ("plan", "due", "weak", "range", "progress")},
        "reply (site question)": reply,
    }


def timed(fn, repeat: int) -> tuple[float, float]:
    """(the first call, the best of `repeat` more): the first fills the parse caches, as the
    first answer after an import does in the running app."""
    out = []
    for _ in range(repeat + 1):
        t = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t)
    return out[0], min(out[1:])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--keys", type=int, default=2000)
    ap.add_argument("--urls", type=int, default=20)
    ap.add_argument("--sites", type=int, default=5000)
    ap.add_argument("--notes", type=int, default=10000)
    ap.add_argument("--events", type=int, default=20000)
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--profile", help="cProfile this call by name and print the top 25")
    ap.add_argument("--keep", action="store_true", help="keep the temp folder")
    ap.add_argument("--vault", type=Path, help="reuse a temp folder kept by --keep")
    args = ap.parse_args()
    tmp = args.vault or Path(tempfile.mkdtemp(prefix="keepfold-bench-chat-"))
    home, root = tmp / "home", tmp / "vault"
    for var in ("HOME", "USERPROFILE"):
        os.environ[var] = str(home)
    try:
        t = time.perf_counter()
        if not args.vault:
            (home / ".keepfold").mkdir(parents=True)
            # a few private domains, one a subdomain: sites._sides checks page URLs for it
            (home / ".keepfold" / "config.yaml").write_text(
                "browse:\n  private_domains: [s1.example, docs.s2.example, s3.example]\n",
                encoding="utf-8")
            build(root, days=args.days, keys=args.keys, urls=args.urls, sites=args.sites,
                  notes=args.notes, n_events=args.events)
        # the import's one-time migration renamed it after reading it into the store
        size = (root / ".keepfold/browse/site-days.json.migrated").stat().st_size / 1e6
        print(f"built in {time.perf_counter() - t:.1f} s: site-days.json {size:.0f} MB")
        from keepfold.vault import Vault
        vault = Vault(root)
        for name, fn in calls(vault).items():
            if args.profile and name.startswith(args.profile):
                fn()  # profile a warm call
                pr = cProfile.Profile()
                pr.runcall(fn)
                pstats.Stats(pr).sort_stats("cumulative").print_stats(25)
            elif not args.profile:
                first, best = timed(fn, args.repeat)
                print(f"{name:<28}{first:8.2f} s first{best:8.2f} s after")
    finally:
        if args.keep or args.vault:
            print(f"kept at {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
