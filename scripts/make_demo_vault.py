"""Fill a throwaway vault with made-up study history for screenshots and demos.

Adds the packaged sample (Sam, a made-up learner) and then more made-up evidence through the
normal vault and event APIs: extra concepts and notes, two mistakes, graded answers with a
"how sure" level of 1-4, two upcoming assessments and a few lessons taught. Every event is
keyed `demo:<n>`, so running it again adds nothing. Dates are relative to today; everything
else is fixed. It refuses the configured vault and any vault with events of its own (not demo
or sample ones) unless --force. For imported chats, run `cortex import` on the
synthetic exports in tests/fixtures/exports afterwards.

    uv run cortex init <vault>
    uv run python scripts/make_demo_vault.py <vault>
"""

from __future__ import annotations

import argparse
import os
import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from cortex import sample, taint
from cortex.catalog import sync as catalog_sync
from cortex.events import EventLog
from cortex.vault import Vault

CONCEPTS = {  # id: (title, area, requires, note line)
    "variables": ("Variables", "programming.basics", [], "A name points at a value."),
    "conditionals": ("Conditionals", "programming.basics", ["variables"],
                     "`elif` is checked only when every branch above it was false."),
    "strings": ("Strings", "programming.basics", ["variables"],
                "Strings are immutable; slicing makes a new one."),
    "lists": ("Lists", "programming.basics", ["loops"],
              "`append` changes the list in place and returns None."),
    "dictionaries": ("Dictionaries", "programming.basics", ["lists"],
                     "Keys must be hashable; lookups are O(1) on average."),
    "binary-search": ("Binary search", "programming.algorithms", ["lists", "big-o-notation"],
                      "Halve a sorted range until the target is found or the range is empty."),
    "sorting": ("Sorting", "programming.algorithms", ["lists", "big-o-notation"],
                "`sorted()` returns a new list; `.sort()` sorts in place."),
}
MISTAKES = {  # id: (title, concept, pattern, fix)
    "programming.mutating-list-while-looping": (
        "Changing a list while looping over it", "lists",
        "Removes items from the list the `for` loop is walking, so some are skipped.",
        "Loop over a copy (`for x in items[:]`) or build a new list."),
    "programming.binary-search-midpoint": (
        "Binary search bounds that never close", "binary-search",
        "Sets `lo = mid` instead of `mid + 1`, so the loop can run forever.",
        "Check the two-element case by hand before running it."),
}
NOTES = {  # id: (title, summary, related, body)
    "a-loop-is-a-promise": (
        "A loop is a promise about its last step", "Say what the last iteration does before "
        "writing the loop.", ["loops", "programming.off-by-one-loop"],
        "Most of my loop bugs are at the end. Saying the last value out loud catches them."),
    "binary-search-invariant": (
        "Binary search keeps one invariant", "The target, if present, is always inside "
        "lo..hi.", ["binary-search", "programming.binary-search-midpoint"],
        "Every update of `lo` or `hi` must keep the target inside the range and shrink it."),
    "lists-vs-dictionaries": (
        "Lists or dictionaries?", "Position matters: a list. Lookup by name: a dictionary.",
        ["lists", "dictionaries"],
        "A dictionary trades order guarantees I rarely need for fast lookup by key."),
    "sorting-is-often-the-first-step": (
        "Sorting is often the first step", "Many problems get easier once the input is "
        "sorted.", ["sorting", "binary-search", "big-o-notation"],
        "Sorting costs O(n log n) once and then binary search answers each query in O(log n)."),
    "strings-are-sequences": (
        "Strings are sequences too", "Anything that works on a list read-only works on a "
        "string.", ["strings", "lists", "loops"],
        "Indexing, slicing and `for` loops all work the same way on strings."),
}
ASSESSMENTS = {  # id: (company, kind, days ahead, tests)
    "practice-oa": ("Practice OA", "oa", 5, {"concept:loops": 1, "concept:lists": 1,
                                             "concept:binary-search": 1,
                                             "concept:strings": 1}),
    "intro-midterm": ("Intro Programming", "test", 16, {
        "concept:variables": 1, "concept:conditionals": 1, "concept:loops": 2,
        "concept:functions": 2, "concept:recursion": 1, "concept:lists": 1,
        "concept:dictionaries": 1}),
}
# how likely a concept's answers are right (scaled by the stated confidence below)
SKILL = {"loops": 0.95, "functions": 0.9, "strings": 0.8, "lists": 0.7, "recursion": 0.6,
         "binary-search": 0.55, "dictionaries": 0.75}
RIGHT_AT = {1: 0.35, 2: 0.55, 3: 0.75, 4: 0.92}
MISTAKE_OF = {"loops": "programming.off-by-one-loop",
              "lists": "programming.mutating-list-while-looping",
              "binary-search": "programming.binary-search-midpoint"}
ANSWERS_PER_BAND = 14
DAYS = 34  # history spread over this many days


def _fm(fields: dict) -> str:
    def val(v):
        if isinstance(v, list):
            return "[" + ", ".join(f'"{x}"' for x in v) + "]"
        return f'"{v}"'
    return "---\n" + "".join(f"{k}: {val(v)}\n" for k, v in fields.items()) + "---\n"


def _page(vault: Vault, rel: str, text: str) -> None:
    p = vault.root / rel
    if not p.exists():  # never overwrite: a re-run leaves edited pages alone
        vault.write_text(p, text)


def pages(vault: Vault, today: date) -> None:
    for cid, (title, area, req, line) in CONCEPTS.items():
        mistakes = [f"[[{m}]]" for m, v in MISTAKES.items() if v[1] == cid]
        _page(vault, f"concepts/{cid}.md", _fm({
            "id": cid, "title": title, "parent": f"[[{area}]]", "domain": "programming",
            "requires": [f"[[{r}]]" for r in req], "mistakes": mistakes})
            + f"# {title}\n\n## Notes\n- {line}\n")
    for mid, (title, cid, pattern, fix) in MISTAKES.items():
        _page(vault, f"mistakes/{mid}.md", _fm({
            "id": mid, "title": title, "concepts": [f"[[{cid}]]"]})
            + f"# {title}\n\n## Pattern\n{pattern}\n\n## Fix\n{fix}\n")
    for nid, (title, summary, related, body) in NOTES.items():
        links = "".join(f"- [[{r}]]\n" for r in related)
        _page(vault, f"notes/{nid}.md", _fm({
            "id": nid, "title": title, "kind": "note", "note_type": "permanent",
            "summary": summary, "tags": ["python"], "related": [f"[[{r}]]" for r in related]})
            + f"# {title}\n\n{body}\n\n## Links\n{links}")
    for aid, (company, kind, ahead, tests) in ASSESSMENTS.items():
        _page(vault, f"assessments/{aid}.md", _fm({
            "id": aid, "company": company, "kind": kind,
            "date": (today + timedelta(days=ahead)).isoformat(), "outcome": "pending"})
            .removesuffix("---\n")
            + "tests: {" + ", ".join(f'"{k}": {w}' for k, w in tests.items()) + "}\n"
            + f"---\n# {company} ({kind})\n\n## Notes\n- A made-up assessment for the demo.\n")
    catalog_sync(vault)


def answers(rng: random.Random) -> list[tuple[int, str, int, bool]]:
    """(days ago, concept, confidence, right) for ANSWERS_PER_BAND answers per confidence."""
    concepts = sorted(SKILL)
    out = []
    for conf in (1, 2, 3, 4):
        for _ in range(ANSWERS_PER_BAND):
            c = rng.choice(concepts)
            out.append((rng.randint(2, DAYS), c, conf, rng.random() < RIGHT_AT[conf] * SKILL[c]))
    return sorted(out, key=lambda a: -a[0])


def events(vault: Vault, now: datetime) -> int:
    log = EventLog(vault)
    n = 0

    def add(kind, days, fields, source="tutor"):
        nonlocal n
        n += 1
        key = f"demo:{n}"
        if not log.has_key(key):
            log.append(session="cli", kind=kind, source=source, key=key, fields=fields,
                       now=now - timedelta(days=days, hours=n % 5))

    for days, c, conf, right in answers(random.Random(7)):
        fields = {"object": f"concept:{c}", "verdict": "correct" if right else "wrong",
                  "hints": 0, "confidence": conf,
                  "item_type": "application" if days % 3 == 0 else "recall"}
        if not right and c in MISTAKE_OF:
            fields["mistakes"] = [MISTAKE_OF[c]]
        add("grade", days, fields)
    for c in ("variables", "conditionals"):  # only spaced passes: these reach Solid
        for days in (30, 19, 8):
            add("grade", days, {"object": f"concept:{c}", "verdict": "correct", "hints": 0,
                                "item_type": "application"})
    # yesterday: a confident miss, which tops Today until it is right on a later day
    add("grade", 1, {"object": "concept:binary-search", "verdict": "wrong", "hints": 0,
                     "confidence": 4, "item_type": "application",
                     "mistakes": ["programming.binary-search-midpoint"]})
    add("taught", 1, {"object": "concept:sorting"})
    add("taught", 3, {"object": "concept:dictionaries"})
    return n


def refusal(root: Path) -> str | None:
    """Why `root` looks like a real vault (made-up events are permanent history), or None."""
    named = (taint.config().get("vault"), os.environ.get("CORTEX_VAULT"))
    if any(v and Path(v).expanduser().resolve() == root for v in named):
        return f"{root} is your configured Cortex vault"
    real = sum(1 for e in EventLog(Vault(root)).iter_events()
               if not str(e.get("key") or "").startswith(("demo:", sample.KEY)))
    return f"{root} already has {real} events of its own" if real else None


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("vault", type=Path, help="an existing throwaway vault (cortex init)")
    ap.add_argument("--force", action="store_true",
                    help="write even into the configured vault or one with events of its own")
    a = ap.parse_args(argv)
    if not a.force and (why := refusal(a.vault.resolve())):
        ap.error(f"{why}; refusing to add made-up history. Use a fresh `cortex init` folder, "
                 "or pass --force.")
    vault = Vault(a.vault.resolve())
    now = datetime.now(UTC)
    sample.seed(vault, now)
    pages(vault, now.astimezone().date())
    print(f"demo vault ready: {events(vault, now)} demo events in {vault.root}")


if __name__ == "__main__":
    main()
