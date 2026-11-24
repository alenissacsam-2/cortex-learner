"""FW: cortex_find's `where`, frontmatter filters (cortex.where), with and without a query,
through find's own privacy (private pages, a profile's withheld pages). Made-up vaults only."""

import statistics
import time
from types import SimpleNamespace

import pytest
from conftest import note

from cortex import mcp_server, where, xray
from cortex.mcp_server import Cortex
from cortex.vault import Vault, VaultError

pytestmark = pytest.mark.usefixtures("home")


# ---- where.parse / where.match ----------------------------------------------------------------

def ok(text, fields):
    return where.match(where.parse(text), fields)


def test_parse_clauses_quotes_and_casefolded_keys():
    assert where.parse('Status=open  title~"binary search" due<=2026-12-01') == [
        ("status", "=", "open"), ("title", "~", "binary search"), ("due", "<=", "2026-12-01")]
    assert where.parse("a!=b c<1 d>2 e>=3 f-g_h=x") == [
        ("a", "!=", "b"), ("c", "<", "1"), ("d", ">", "2"), ("e", ">=", "3"), ("f-g_h", "=", "x")]


@pytest.mark.parametrize("text, why", [
    ("", "blank"), ("   ", "blank"), (None, "blank"),
    ("status", "no operator"), ("status:open", "no operator"),
    ("rating==4", "unknown operator"), ("rating=>4", "unknown operator"),
    ("a<>b", "unknown operator"), ("=open", "unknown operator"),
    ('title~"binary search', "unbalanced quote"), ('title~bin"ary', "unbalanced quote"),
    ("a=1 b=2 c=3 d=4 e=5 f=6 g=7", "at most 6"),
    ("status=", "has no value"), ("title~", "has no value"), ('title~""', "has no value"),
    ('title~"  "', "has no value"), ("a=1 status!=", "has no value"),
])
def test_every_error_is_invalid_argument_with_an_example(text, why):
    with pytest.raises(VaultError) as e:
        where.parse(text)
    assert e.value.code == "invalid_argument"
    assert why in str(e.value) and "for example where=" in str(e.value)


def test_each_operator_on_strings():
    f = {"status": " Open ", "title": "Binary Search Tricks"}
    assert ok("status=open", f) and not ok("status=done", f)
    assert ok("status!=done", f) and not ok("status!=OPEN", f)
    assert ok("title~search", f) and ok('title~"binary search"', f) and not ok("title~heap", f)
    assert not ok("title<z", f) and not ok("title>a", f)  # no string ordering
    assert not ok("status<=open", f) and not ok("status>=open", f)


def test_casefolded_keys_and_and_of_clauses():
    f = {"Status": "open", "Rating": 4}
    assert ok("STATUS=open rating>=4", f)
    assert not ok("status=open rating>4", f)  # every clause must pass


def test_list_fields():
    f = {"tags": ["DSA", "graphs"]}
    assert ok("tags=dsa", f) and ok("tags~raph", f) and not ok("tags=heap", f)
    assert ok("tags!=heap", f) and not ok("tags!=graphs", f)  # none equals
    assert ok("tags!=x", {"tags": []}) and not ok("tags=x", {"tags": []})


def test_link_values_on_either_side():
    f = {"related": ["[[concept.dijkstra|Dijkstra]]", "[[bfs]]"], "parent": "dsa.graphs"}
    assert ok("related=[[concept.dijkstra]]", f) and ok("related=concept.dijkstra", f)
    assert ok("related=BFS", f) and ok("parent=[[dsa.graphs|Graphs]]", f)
    assert not ok("related=dijkstra", f)


def test_days_and_timestamps():
    f = {"due": "2026-12-01", "at": "2026-11-01T09:30:00"}
    assert ok("due<=2026-12-01", f) and ok("due>2026-11-30", f) and not ok("due<2026-12-01", f)
    assert ok("at>=2026-11-01", f) and ok("at<2026-11-02", f) and not ok("at>2026-11-01", f)
    assert ok("at<=2026-11-01T23:00", f)  # either side by its first 10 characters
    assert not ok("due<soon", f) and not ok("due<5", f)  # a day and a non-day: fails


def test_equal_compares_days_and_numbers_as_such():
    f = {"rating": 4.0, "at": "2026-11-20T10:00:00", "n": "07"}
    assert ok("rating=4", f) and not ok("rating!=4", f) and ok("rating!=4.5", f)
    assert ok("at=2026-11-20", f) and not ok("at!=2026-11-20", f) and not ok("at=2026-11-21", f)
    assert ok("n=7", f)  # both numbers
    assert ok("at!=soon", f) and not ok("at=2026-11-20T10", {"at": "x"})  # else as strings


def test_numbers_and_bools():
    f = {"rating": 4, "score": "10", "pinned": True, "ratio": 0.5}
    assert ok("rating>=4", f) and ok("rating<4.5", f) and not ok("rating>4", f)
    assert ok("score>9", f)  # as numbers: as strings "10" < "9"
    assert ok("ratio<1", f) and ok("rating=4", f) and ok("rating!=5", f)
    assert ok("pinned=true", f) and ok("pinned!=false", f) and not ok("pinned=false", f)
    assert not ok("pinned>0", f) and not ok("rating>four", f)


def test_a_missing_field_fails_all_but_not_equal():
    for text in ("x=1", "x~1", "x<1", "x<=1", "x>1", "x>=1"):
        assert not ok(text, {"y": 1}), text
        assert not ok(text, {"x": None}), text
    assert ok("x!=1", {"y": 1}) and ok("x!=1", {"x": None})


# ---- cortex_find ------------------------------------------------------------------------------

def page(root, folder, id, front, body="x"):
    note(root, f"{folder}/{id}.md", front, f"# {id}\n\n{body}")


@pytest.fixture
def vroot(root):
    page(root, "notes", "n-open", 'title: Open graph note\ndate: 2026-03-10\nstatus: open\n'
         'tags: [dsa, graphs]\nrating: 4\nrelated: ["[[two-pointers]]"]\n', "shortest paths")
    page(root, "notes", "n-done", 'title: Done note\ndate: 2026-03-12\nstatus: done\n'
         'tags: [placement]\nrating: 2\n', "shortest paths")
    page(root, "decisions", "d-open", "title: Open decision\ndate: 2026-03-11\nstatus: open\n"
         "tags: [placement]\n", "pick a stack")
    page(root, "projects", "p-undated", "title: Undated project\nstatus: open\n", "build it")
    page(root, "projects", "a-undated", "title: Another undated\nstatus: open\n", "build it")
    page(root, "notes", "secret-plan", "title: Secret plan\ndate: 2026-03-13\nstatus: open\n"
         "private: true\n", "shortest paths")
    note(root, "journal/2026-03-09.md", "title: 2026-03-09\nstatus: open\n", "- 10:00 x")
    return root


def find(root, client=None, **kw):
    c = Cortex(Vault(root))
    if client:
        c.over_mcp, c.sessions.client = True, client
    return c.cortex_find(**kw)


def ids(out):
    return [h["id"] for h in out["hits"]]


def test_no_query_lists_newest_first_undated_last_then_by_id(vroot):
    out = find(vroot, where="status=open")
    assert ids(out) == ["d-open", "n-open", "2026-03-09", "a-undated", "p-undated"]
    assert out["text"].startswith('<untrusted_note id="find">')
    assert ids(find(vroot, where="status=open", limit=2)) == ["d-open", "n-open"]
    assert ids(find(vroot, where="status=open", kind="project")) == ["a-undated", "p-undated"]
    assert ids(find(vroot, where="tags=placement")) == ["n-done", "d-open"]
    assert ids(find(vroot, where="related=[[two-pointers]] rating>=4")) == ["n-open"]
    assert find(vroot)["hits"] == []  # no where, no query: nothing, as before


def test_with_a_query_where_applies_before_the_limit(vroot):
    assert set(ids(find(vroot, query="shortest paths"))) == {"n-open", "n-done"}
    assert ids(find(vroot, query="shortest paths", where="status=done", limit=1)) == ["n-done"]
    assert ids(find(vroot, query="shortest paths", where="status=open")) == ["n-open"]


def test_with_since_and_until(vroot):
    assert ids(find(vroot, where="status=open", since="2026-03-10", until="2026-03-10")) == [
        "n-open"]
    assert ids(find(vroot, where="status=open", since="2026-03-11")) == ["d-open"]


def test_private_and_withheld_pages_are_never_listed(vroot):
    owner = find(vroot, where="status=open")
    assert "secret-plan" not in ids(owner) and "Secret plan" not in owner["text"]
    assert "2026-03-09" in ids(owner)
    coding = find(vroot, "cursor-vscode", where="status=open")  # no personal scope: no journal
    assert "2026-03-09" not in ids(coding) and "n-open" in ids(coding)
    assert "secret-plan" not in ids(find(vroot, query="shortest paths", where="status=open"))
    empty = find(vroot, where="title~secret")
    assert empty["hits"] == [] and "secret" not in str(empty).casefold().replace(
        "title~secret", "")


def test_an_empty_listing_says_what_it_searched(vroot):
    out = find(vroot, where="status=open", kind="note", since="2026-03-11")
    assert out["hits"] == []
    s = out["searched"]
    assert s["pages"] == 0 and s["kinds"] == ["note"] and s["by"] == "where filter"
    assert "where" in out["next"]
    out = find(vroot, where="status=gone tags!=x")
    assert out["hits"] == [] and out["searched"]["pages"] == 0
    s = find(vroot, query="qwertyzz", where="tags=placement")["searched"]
    assert s["pages"] == 2 and s["by"] == "keyword"  # the pages past the filter, as for since


def test_loops(vroot):
    page(vroot, "loops", "l-soon", "title: Soon\nloop_type: commitment\nstatus: open\n"
         "due: 2026-11-28\n")
    page(vroot, "loops", "l-later", "title: Later\nloop_type: commitment\nstatus: open\n"
         "due: 2026-12-20\n")
    page(vroot, "loops", "l-idea", "title: Idea\nloop_type: idea\nstatus: open\n")
    assert ids(find(vroot, kind="loop", where="due<=2026-12-01")) == ["l-soon"]
    assert ids(find(vroot, kind="loop", where="due!=x")) == ["l-soon", "l-later", "l-idea"]
    assert ids(find(vroot, kind="loop", where="loop_type=idea")) == ["l-idea"]
    assert ids(find(vroot, kind="loop", query="later", where="status=open")) == ["l-later"]


def test_due_concepts(vroot, monkeypatch):
    S = SimpleNamespace
    states = {c: S(state="needs_refresh", next_due="2026-01-01", open_mistakes=[])
              for c in ("arrays-basics", "two-pointers")}
    monkeypatch.setattr(mcp_server.planner, "current_states", lambda *a, **k: (states, None))
    assert set(ids(find(vroot, kind="due"))) == {"arrays-basics", "two-pointers"}
    assert ids(find(vroot, kind="due", where="aliases~pointer")) == ["two-pointers"]
    assert ids(find(vroot, kind="due", where="mistakes=[[dsa.off-by-one]]")) == [
        "two-pointers"]
    assert ids(find(vroot, kind="due", where="mistakes!=[[dsa.off-by-one]]")) == [
        "arrays-basics"]


@pytest.mark.parametrize("kind", ["resurface", "timeline"])
def test_resurface_and_timeline_refuse_where(vroot, kind):
    with pytest.raises(VaultError) as e:
        find(vroot, kind=kind, where="status=open")
    assert e.value.code == "invalid_argument" and kind in str(e.value)


def test_a_bad_where_raises_before_any_search(vroot):
    for bad in ("", "status", "a==b"):
        with pytest.raises(VaultError) as e:
            find(vroot, query="shortest", where=bad)
        assert e.value.code == "invalid_argument"


def test_a_listing_is_a_read_for_the_broad_read_flag(vroot):
    """The X-ray row keeps the listed ids under `found`, the key xray.wide counts (its window
    maths: test_xray_trace.test_the_window_maths)."""
    v = Vault(vroot)
    out = find(vroot, "cursor-vscode", where="status=open")
    row = xray.rows(v)[-1]
    assert row["tool"] == "cortex_find" and row["client"] == "cursor-vscode"
    assert row["served"] == {"found": ids(out)} and ids(out)
    assert "status" not in str(row)  # ids only, never the filter text


@pytest.mark.slow
@pytest.mark.perf
def test_a_where_listing_stays_quick_on_5k_notes(tmp_path, capsys):
    from test_home import _big

    root = tmp_path / "big"
    _big(root)
    cx = mcp_server.Cortex(Vault(root))
    cx._ready.wait()
    cx.cortex_find(where="tags=t7")  # the first call folds the log and warms the caches

    def median(**kw):
        times = []
        for _ in range(7):
            t = time.perf_counter()
            out = cx.cortex_find(**kw)
            times.append(time.perf_counter() - t)
        return statistics.median(times), out

    plain, _ = median(query="array list")
    warm, out = median(where="tags=t7")
    assert out["hits"]
    with capsys.disabled():
        print(f"\nwarm on 5k notes: where-only listing {warm * 1000:.0f} ms, plain find "
              f"{plain * 1000:.0f} ms (medians of 7)")
    # 150 ms on a quiet PC; both pay the same index refresh (most of it), so a busy PC is
    # judged by the listing's own cost over a plain find
    assert warm < 0.15 or warm < plain + 0.03, (warm, plain)
