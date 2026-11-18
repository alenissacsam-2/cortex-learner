from pathlib import Path

import pytest

from cortex.render import MODE_LABEL, brief_text, end_card, esc, home_card, start_card, today_md

SNAP = Path(__file__).parent / "fixtures" / "render"

HOSTILE = [
    "![pwn](http://evil.example/x.png) Dijkstra",
    "see [docs](https://evil.example/a) now",
    "visit http://evil.example/path?q=1 and www.evil.example too",
    "<script>alert(1)</script>Heap",
    "**bold** [x](javascript:alert(1)) |table| `code` # head ~strike~ _u_",
    "[a](b)",
    "<img src=x onerror=alert(1)>",
    "x" * 500,
]


@pytest.mark.parametrize("text", HOSTILE)
def test_esc_hostile(text):
    out = esc(text)
    for bad in ("](", "http", "<", "!["):
        assert bad not in out
    assert "\n" not in out and len(out) <= 120 * 2


def test_esc_keeps_plain_text_and_truncates():
    assert esc("see [docs](https://x.y) now") == "see docs now"
    assert esc("![i](u)Heap") == "Heap"
    assert esc("**a**") == r"\*\*a\*\*"
    assert esc("a  \n b") == "a b"
    long = esc("y" * 500, limit=50)
    assert len(long) == 50 and long.endswith("…")


def _items(n, kind="refresh"):
    return [{"kind": kind, "id": f"c{i}", "title": f"Concept {i}", "state": "needs_refresh",
             "why": "due 2026-11-12"} for i in range(n)]


def _start(**kw):
    args = dict(mode="learn", sprint_day=3, today=[], assessments=[],
                open_mistakes=[], date="2026-11-13")
    return start_card(**{**args, **kw})


def test_start_card_caps_plan_and_more():
    card = _start(today=_items(12), open_mistakes=[(f"m{i}", 2) for i in range(5)],
                  assessments=[{"company": f"Co{i}", "kind": "oa", "outcome": "pending"}
                               for i in range(5)])
    lines = card.splitlines()
    assert len(lines) <= 25
    assert "**Today's plan** (8 of 12)" in card
    assert sum(ln.startswith("- ") for ln in lines) == 8
    assert "+4 more in Today.md" in card
    assert card.count("⚑") == 3
    assert "Co2 oa" in card and "Co3" not in card and "date not set" in card


def test_start_card_lists_the_whole_sweep():
    card = _start(mode="sweep", today=_items(10))
    assert "**Today's plan** (10 of 10)" in card and "Today.md" not in card


def test_mock_card_has_no_plan_line_but_keeps_assessments_and_mistakes():
    """Review r4 D4: a mock has no plan, so no 'nothing planned yet'; mock.md step 1."""
    card = _start(mode="mock", open_mistakes=[("Off by one", 2)],
                  assessments=[{"company": "Acme", "kind": "oa", "outcome": "pending"}])
    assert "Today's plan" not in card
    assert "Coming up" in card and "Open mistakes" in card


@pytest.mark.parametrize("mode,label", list(MODE_LABEL.items()))
def test_mode_labels(mode, label):
    assert _start(mode=mode).splitlines()[0] == f"### Cortex · {label} · Sprint day 3 · 2026-11-13"


def test_start_card_omits_empty_sections():
    card = _start(sprint_day=None, today=_items(2))
    assert card.splitlines()[0] == "### Cortex · Learning session · 2026-11-13"
    for section in ("Taught, not yet checked", "Coming up", "Open mistakes", "more in"):
        assert section not in card
    done = _start(assessments=[{"company": "Acme", "kind": "oa", "outcome": "pass"}])
    assert "Coming up" not in done


def test_start_card_unchecked_from_the_plan_and_track_glyph():
    track = {"kind": "track", "id": None, "title": "Mock interview", "state": None,
             "why": "t: Day 3"}
    bfs = {"kind": "unchecked", "id": "bfs", "title": "BFS", "state": "unverified",
           "why": "Taught today, not yet checked"}
    assert "- ▫ Mock interview · t: Day 3" in _start(today=[track])
    assert "Taught, not yet checked:**" not in _start(today=[track])  # only what the plan shows
    assert "**Taught, not yet checked:** BFS" in _start(today=[bfs, track])


def test_home_card_shows_the_forecast_beside_the_solid_count():
    up = {"company": "Acme", "kind": "OA", "id": "a", "days": 5, "date": "2026-11-28",
          "solid": 0, "total": 4}
    card = home_card(domains=[], due=[], open_mistakes=0, coming_up={**up, "forecast": 0.643})
    assert ("**Coming up:** Acme OA · in 5 days · 0 of 4 tested topics solid · "
            "forecast recall on 28 Nov: 64%") in card
    hidden = home_card(domains=[], due=[], open_mistakes=0, coming_up={**up, "forecast": None})
    assert "0 of 4 tested topics solid" in hidden and "forecast" not in hidden


def _full_home(**kw):
    """A home card with plenty of everything (UX-7 budget)."""
    args = dict(
        domains=[(f"Domain {i}", 1, 2, 3) for i in range(9)],
        due=["Dijkstra", "Sliding window", "Heaps", *(f"C{i}" for i in range(9))],
        open_mistakes=4,
        projects=[f"Project {i}" for i in range(6)], loops=[f"Loop {i}" for i in range(7)],
        ideas=["Idea one — why", "Idea two"],
        coming_up={"company": "Google", "kind": "OA", "id": "g", "days": 6, "solid": 8,
                   "total": 14, "more": 1},
        solid=["Tries", "Graphs", "BFS", "DFS"], back=["Heaps"], resolved=2,
        unconfirmed=3, revisit=["d-a", "d-b", "d-c"],
        edits=[{"session": "s_01ABC", "day": "2026-11-12", "pages": ["contract", "profile"]},
               {"session": "s_01XYZ", "day": "2026-11-10", "pages": ["profile"]}])
    return home_card(**{**args, **kw})


def test_home_card_budget_and_caps():
    card = _full_home()
    lines = card.splitlines()
    assert len(lines) <= 10, card
    assert "**Projects:** Project 0 · Project 1 · Project 2 +3 more" in card
    assert "**Open loops:** Loop 0 · Loop 1 · Loop 2 +4 more" in card
    assert "Domain 2 ◐ 1 ● 2 ○ 3 +6 more" in card and "Domain 3" not in card
    assert "**Due:** Dijkstra, Sliding window, Heaps +9 · **Open mistakes:** 4" in card
    assert ("**Coming up:** Google OA · in 6 days · 8 of 14 tested topics solid (+1 more)"
            in card)
    assert ("**Lately:** now solid: Tries, Graphs, BFS +1 more · back to solid: Heaps · "
            "2 mistakes resolved") in card
    assert ("**Waiting for you:** 3 about-me lines to confirm · decisions to revisit: d-a, "
            "d-b +1 more · contract and profile edited in another chat on 2026-11-12 · "
            "undo: `cortex undo s_01ABC` (+1 more)") in card
    assert lines[-1].startswith("**What would you like to do?**")


def test_home_card_due_slice_and_empty_lines():
    big = _full_home(due=[f"C{i}" for i in range(40)])
    assert "**Due:** C0, C1, C2 · a slice of 15 is plenty today" in big and "40" not in big
    assert "**Due:** A, B ·" in _full_home(due=["A", "B"])
    bare = home_card(domains=[], due=[], open_mistakes=0)
    assert bare.splitlines() == [
        "### Cortex", "**Your map:** no concepts yet", "**Due:** nothing today · "
        "**Open mistakes:** 0", bare.splitlines()[-1]]
    assert "**Coming up:** Acme · tomorrow" in home_card(
        domains=[], due=[], open_mistakes=0,
        coming_up={"company": "Acme", "kind": None, "days": 1, "solid": 0, "total": 0})


def test_home_card_names_tests_that_need_a_date():
    wait = "**Waiting for you:** "
    assert wait + "1 test needs a date (or a result): [[g-oa]]" in home_card(
        domains=[], due=[], open_mistakes=0, undated=["g-oa"])
    card = _full_home(unconfirmed=0, revisit=[], edits=[], undated=["a", "b", "c", "d"])
    assert wait + "4 tests need a date (or a result): [[a]], [[b]], [[c]]\n" in card
    assert len(card.splitlines()) <= 10  # inside Waiting for you: the UX-7 budget holds
    assert "need a date" not in home_card(domains=[], due=[], open_mistakes=0)


@pytest.mark.parametrize("text", HOSTILE)
def test_home_card_escapes_every_item(text):
    card = _full_home(due=[text], projects=[text], loops=[text], ideas=[text], solid=[text],
                      back=[text], domains=[(text, 1, 1, 1)], revisit=[text], undated=[text],
                      coming_up={"company": text, "kind": "oa", "days": 3, "solid": 0,
                                 "total": 1})
    for bad in ("](", "http", "<", "!["):
        assert bad not in card


def _end(**kw):
    args = dict(counts={"graded": 4, "logged": 1}, moved=[], unchecked=[], new_mistakes=[],
                next_items=[], plan_cleared=False, duration_min=30)
    return end_card(**{**args, **kw})


def test_end_card_nothing_moved_and_omissions():
    card = _end()
    assert card.splitlines() == ["### Session closed · 30 min · 4 answers · 1 logged",
                                 "**Moved:** nothing changed this time"]


def test_end_card_done_only_when_cleared():
    assert "Done for today ✓" not in _end()
    assert _end(plan_cleared=True).splitlines()[-1] == "Done for today ✓"


def test_end_card_names_files_held_back():
    """review r4 L5: a file the secret scan kept out of the commit gets an escaped line."""
    card = _end(not_committed=[r"inbox.md: \bghp_[A-Za-z0-9]{20,}",
                               "notes/<b>[x](http://e.co)</b>.md: api[_-]?key"])
    lines = card.splitlines()
    assert "Not committed: inbox.md (looks like a key)" in lines
    bad = next(ln for ln in lines if ln.startswith("Not committed: notes/"))
    assert bad.endswith("(looks like a key)") and not any(
        s in bad for s in ("<", "](", "http", "ghp_", "api"))


def test_end_card_moved_cap():
    moved = [{"title": f"C{i}", "before": "learning", "after": "solid"} for i in range(8)]
    card = _end(moved=moved)
    assert card.count("→") == 6 and "+2 more" in card
    assert "C0 ◐ → ● Solid" in card


def test_end_card_changed_lines():
    rule = {"tool": "patch", "id": "teaching-contract", "kind": "me",
            "op": "append_to_section", "target": "Rules"}
    changes = [rule, rule,
               {"tool": "patch", "id": "two-sum", "kind": "problem", "op": "set_field",
                "target": "status"},
               {"tool": "add", "id": "dsa.graphs.dijkstra", "kind": "concept", "op": "create",
                "target": None},
               {"tool": "add", "id": "x_<b>", "kind": "concept", "op": "adopt", "target": None}]
    lines = _end(changes=changes).splitlines()
    i = lines.index("**Changed:**")
    assert lines[i + 1:] == ["- patched me/teaching-contract § Rules (append) ×2",
                             "- set problems/two-sum status",
                             "- added concept dsa.graphs.dijkstra",
                             "- adopted concept x\\_"]  # untrusted id escaped, tag stripped
    assert "Changed" not in _end()


def test_end_card_changed_cap():
    changes = [{"tool": "add", "id": f"c{i}", "kind": "concept", "op": "create"}
               for i in range(18)]
    lines = _end(changes=changes).splitlines()
    assert lines[-1] == "+3 more" and len(lines) - lines.index("**Changed:**") == 17


def test_brief_text_cap():
    brief = "\n".join(f"line {i} " + "x" * 50 for i in range(500))
    out = brief_text(brief, budget_tokens=100)
    assert len(out) <= 400 and out.endswith("\n[brief truncated]")
    assert out.splitlines()[-2] in brief.splitlines()
    assert brief_text("short", 100) == "short"


def test_today_md():
    items = _items(1, "unchecked") + [
        {"kind": "track", "id": None, "title": "[x](http://e) Read <b>notes</b>", "state": None,
         "why": "sprint: Day 3"}]
    md = today_md(today=items, sprint_day=3, as_of="2026-11-13 18:00",
                  assessments=[{"company": "Acme", "kind": "oa", "date": "2026-11-20",
                                "outcome": "pending"}])
    lines = md.splitlines()
    assert lines[0] == "<!-- generated by cortex; safe to delete -->"
    assert "# Today · 2026-11-13 18:00" in lines
    assert "- ↻ Needs refresh · Concept 0 · due 2026-11-12" in lines
    assert "- ▫ · x Read notes · sprint: Day 3" in lines
    assert "## Taught but not checked" in lines and "- Acme oa · 2026-11-20" in lines
    assert lines[-1] == "Refresh with /cortex or `cortex brief`"



def test_confident_miss_item_renders():
    item = {"kind": "confident_miss", "id": "bfs", "title": "BFS", "state": "learning",
            "why": "confident miss on 2026-11-12"}
    assert "- ◐ BFS · confident miss on 2026-11-12" in _start(today=[item]).splitlines()
    md = today_md(today=[item], sprint_day=None, assessments=[], as_of="2026-11-13")
    assert "- ◐ Learning · BFS · confident miss on 2026-11-12" in md.splitlines()

# realistic fake data for the snapshots
START_ARGS = dict(
    mode="learn", sprint_day=6, date="2026-11-13",
    today=[
        {"kind": "unchecked", "id": "bfs", "title": "Breadth-first search", "state": "learning",
         "why": "Taught, not yet checked"},
        {"kind": "track", "id": "two-pointers", "title": "Two pointers", "state": "unverified",
         "why": "placement-sprint-2026: Day 6"},
        {"kind": "track", "id": None, "title": "Solve 2 LeetCode mediums", "state": None,
         "why": "placement-sprint-2026: Day 6"},
        {"kind": "asked", "id": "sql-joins", "title": "SQL joins", "state": "learning",
         "why": "1 asked, not graded"},
        {"kind": "refresh", "id": "binary-search", "title": "Binary search",
         "state": "needs_refresh", "why": "due 2026-11-12"},
    ],
    assessments=[{"company": "Acme", "kind": "oa", "date": "2026-11-20", "outcome": "pending"},
                 {"company": "Globex", "kind": "interview", "outcome": "pending"}],
    open_mistakes=[("Off-by-one in loop bounds", 2)],
)
END_ARGS = dict(
    counts={"graded": 6, "logged": 2}, duration_min=42, plan_cleared=True,
    moved=[{"title": "Two pointers", "before": "unverified", "after": "learning"},
           {"title": "Binary search", "before": "needs_refresh", "after": "solid"}],
    unchecked=["Dijkstra's algorithm"], new_mistakes=["Forgot visited set in BFS"],
    next_items=[{"title": "Dijkstra's algorithm"}, {"title": "SQL joins"},
                {"title": "Sliding window"}, {"title": "Heaps"}],
)


@pytest.mark.parametrize("name,fn,args", [("start_card.md", start_card, START_ARGS),
                                          ("end_card.md", end_card, END_ARGS)])
def test_snapshots(name, fn, args):
    assert fn(**args) == (SNAP / name).read_text(encoding="utf-8")


def test_end_card_new_notes_tally():
    def add(kind, nid, note_type=None, op="create"):
        return {"tool": "add", "id": nid, "kind": kind, "op": op, "target": None,
                **({"note_type": note_type} if kind == "note" else {})}

    changes = [add("note", "a", "permanent"), add("note", "b", "permanent"),
               add("note", "inbox:c", "fleeting"), add("note", "d"), add("concept", "e")]
    assert "**New notes this session:** 4 (1 fleeting, 2 permanent, 1 untyped)" in \
        _end(changes=changes)
    assert "New notes" not in _end(changes=[add("concept", "e")])
