"""R26: cortex_find(kind="timeline"), the pages from the days around one page, in date order,
through find's own filters (private, browse-private, a client profile's withheld kinds)."""

import re
import time
from pathlib import Path

import pytest
from conftest import note

from cortex.mcp_server import CORE_ONLY_NEXT, Cortex
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

LINE = re.compile(r"^([▸-]) (\d{4}-\d{2}-\d{2}) · (\w+) · \[\[([^\]]+)\]\] · (.*)$", re.M)


def page(root, folder, id, day, title, extra=""):
    note(root, f"{folder}/{id}.md", f"title: {title}\ndate: {day}\n{extra}", f"# {title}\n\nx")


@pytest.fixture
def vroot(root):
    page(root, "decisions", "move-to-sqlite", "2026-03-10", "Move to SQLite")
    page(root, "notes", "wal-notes", "2026-03-08", "WAL notes")
    page(root, "resources", "fts5-paper", "2026-03-12", "FTS5 paper")
    page(root, "projects", "cortex-db", "2026-03-13", "Cortex DB")
    page(root, "notes", "too-early", "2026-03-06", "Too early")
    page(root, "notes", "too-late", "2026-03-14", "Too late")
    page(root, "problems", "lc-x", "2026-03-10", "A problem: not a timeline kind")
    note(root, "journal/2026-03-09.md", "title: 2026-03-09\n", "- 10:00 read about WAL")
    page(root, "notes", "secret-plan", "2026-03-11", "Secret plan", "private: true\n")
    note(root, "browse/private/2026-03-10.md", "title: Hidden browsing\ndate: 2026-03-10\n",
         "# Hidden browsing\n\nzzz")
    return root


def timeline(root, client=None, **kw):
    c = Cortex(Vault(root))
    if client:
        c.over_mcp, c.sessions.client = True, client
    return c.cortex_find(kind="timeline", **kw)


def lines(out):
    return LINE.findall(out["text"])


def test_date_order_window_and_the_anchor_mark(vroot):
    out = timeline(vroot, query="move-to-sqlite")
    got = lines(out)
    assert [(d, i) for _, d, _, i, _ in got] == [
        ("2026-03-08", "wal-notes"), ("2026-03-09", "2026-03-09"),
        ("2026-03-10", "move-to-sqlite"), ("2026-03-12", "fts5-paper"),
        ("2026-03-13", "cortex-db")]
    assert [m for m, *_ in got if m == "▸"] == ["▸"] and got[2][0] == "▸"
    assert got[2][2] == "decision" and got[1][2] == "journal"
    assert out["text"].startswith('<untrusted_note id="find">')
    assert out["hits"][2] == {"id": "move-to-sqlite", "kind": "decision", "day": "2026-03-10",
                              "anchor": True}
    assert "2026-03-07 to 2026-03-13" in out["next"]
    real = {p.stem for p in vroot.rglob("*.md")}
    assert {h["id"] for h in out["hits"]} <= real  # ids are real page ids


def test_words_find_the_anchor_and_since_until_override(vroot):
    out = timeline(vroot, query="FTS5 paper", since="2026-03-12", until="2026-03-14")
    assert [i for _, _, _, i, _ in lines(out)] == ["fts5-paper", "cortex-db", "too-late"]
    assert lines(out)[0][0] == "▸"
    wide = timeline(vroot, query="move-to-sqlite", since="2026-03-01")
    ids = [i for _, _, _, i, _ in lines(wide)]
    assert "too-early" in ids and "too-late" not in ids  # until stays anchor + 3


def test_private_and_browse_private_never_appear(vroot):
    out = timeline(vroot, query="move-to-sqlite", since="2026-03-01", until="2026-03-31")
    text = str(out)
    for word in ("secret-plan", "Secret plan", "Hidden browsing", "zzz"):
        assert word not in text
    assert "searched" not in out and "pages" not in out  # no count either


def test_the_cap_keeps_the_nearest_and_limit_caps_it(root):
    for k in range(20):
        page(root, "notes", f"n-{k:02d}", f"2026-05-{k + 1:02d}", f"Note {k}")
    out = timeline(root, query="n-10", since="2026-05-01", until="2026-05-31")
    ids = [i for _, _, _, i, _ in lines(out)]
    assert len(ids) == 12 and "n-10" in ids and ids == sorted(ids)
    assert ids == [f"n-{k:02d}" for k in range(4, 16)]  # nearest 11 around the anchor, ties older
    assert len(lines(timeline(root, query="n-10", limit=3))) == 3
    assert len(lines(timeline(root, query="n-10", limit=50))) <= 12


def test_a_profile_without_personal_gets_no_journal(vroot):
    coding = [i for _, _, _, i, _ in lines(timeline(vroot, "cursor-vscode",
                                                    query="move-to-sqlite"))]
    assert "2026-03-09" not in coding and "move-to-sqlite" in coding
    owner = [i for _, _, _, i, _ in lines(timeline(vroot, "claude-code",
                                                   query="move-to-sqlite"))]
    assert "2026-03-09" in owner


def test_an_unknown_app_and_an_unknown_anchor_get_nothing(vroot):
    out = timeline(vroot, "random-agent-xyz", query="move-to-sqlite")
    assert out["hits"] == [] and out["text"] == "" and out["next"].startswith(CORE_ONLY_NEXT)
    out = timeline(vroot, query="qwertyuiop zxcvbnm")
    assert out["hits"] == [] and out["text"] == "" and "page id" in out["next"]
    assert timeline(vroot, query="")["hits"] == []


def test_an_id_the_client_may_not_see_answers_as_no_anchor(vroot):
    """A hidden page's id never centres the window, and the answer is word for word the one
    for no match, so it does not tell the page exists."""
    note(vroot, "notes/diary-steps.md", "title: Diary steps\ndate: 2026-03-10\n"
         "tags: [playbook]\nscope: personal\n", "# Diary steps\n\nx")
    none = timeline(vroot, query="qwertyuiop zxcvbnm")
    assert timeline(vroot, query="secret-plan") == none  # private
    assert timeline(vroot, "cursor-vscode", query="diary-steps") == none  # out of its scope
    assert timeline(vroot, "cursor-vscode", query="2026-03-09") == none  # journal: personal
    assert timeline(vroot, "claude-code", query="diary-steps")["hits"]  # in scope: served
    unknown = timeline(vroot, "random-agent-xyz", query="move-to-sqlite")
    assert unknown == {**none, "next": f"{CORE_ONLY_NEXT} {none['next']}"}


def test_one_bound_alone_never_errors(vroot):
    late = timeline(vroot, query="move-to-sqlite", since="2026-03-20")  # after anchor + 3
    assert late["hits"] == [] and "2026-03-20 to 2026-03-26" in late["next"]
    early = timeline(vroot, query="move-to-sqlite", until="2026-03-01")  # before anchor - 3
    assert early["hits"] == [] and "2026-02-23 to 2026-03-01" in early["next"]
    page(vroot, "notes", "spring", "2026-03-24", "Spring note")
    assert [i for _, _, _, i, _ in lines(timeline(vroot, query="move-to-sqlite",
                                                  since="2026-03-20"))] == ["spring"]


def test_an_undated_anchor_asks_for_since_and_until(vroot):
    out = timeline(vroot, query="two-pointers")  # a concept with no date
    assert out["hits"] == [] and "since and until" in out["next"]
    out = timeline(vroot, query="two-pointers", since="2026-03-09", until="2026-03-10")
    assert {i for _, _, _, i, _ in lines(out)} == {"2026-03-09", "move-to-sqlite"}


@pytest.mark.slow
@pytest.mark.perf
def test_timeline_is_quick_on_5k_notes(tmp_path):
    from test_home import _big

    root = tmp_path / "big"
    _big(root)
    c = Cortex(Vault(root))
    day = sorted(p.stem for p in Path(root / "journal").glob("*.md"))[30]
    c.cortex_find(kind="timeline", query=day)  # the first builds the index

    def best(**kw):  # the best of 3: a refresh, then the call
        times = []
        for _ in range(3):
            t = time.perf_counter()
            out = c.cortex_find(**kw)
            times.append(time.perf_counter() - t)
        return min(times), out

    find_s, _ = best(query="graph tree")
    took, out = best(kind="timeline", query=day)
    assert out["hits"] and len(out["hits"]) <= 12
    print(f"\n5k notes: find {find_s:.2f} s, timeline {took:.2f} s")
    # both pay the same index refresh (~0.85 s here); the timeline's own pass is ~0.2 s
    assert took < 1.5 * find_s + 0.1, (took, find_s)
