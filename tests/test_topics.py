"""cortex topics (REVIEW-1005 §5): the vault-backed hub tree, automatic attach with LCA
back-off, place / set_topics, in-place rebuilds, cortex_add filing and the CLI. A synthetic
vault of 12 hubs over 3 levels; a fake embedder gives each leaf hub's pages one direction."""

import json
import re

import numpy as np
import pytest
from conftest import git, note, run
from typer.testing import CliRunner

from cortex import semantic, topics
from cortex.catalog import Catalog
from cortex.cli import app
from cortex.index import Index
from cortex.vault import Vault, VaultError

pytestmark = pytest.mark.usefixtures("home")

HUBS = {  # id: (title, parent)
    "my-topic-map": ("My topic map", None),
    "area-a": ("Area A", "my-topic-map"), "area-b": ("Area B", "my-topic-map"),
    "a-x": ("A X", "area-a"), "a-y": ("A Y", "area-a"),
    "b-z": ("B Z", "area-b"), "small": ("Small", "area-b"),
    "a-x-1": ("A X one", "a-x"), "a-x-2": ("A X two", "a-x"), "a-y-1": ("A Y one", "a-y"),
    "b-z-1": ("B Z one", "b-z"), "b-z-2": ("B Z two", "b-z"),
}
LEAVES = {"a-x-1": ("ax1", 0, 4), "a-x-2": ("ax2", 1, 4), "a-y-1": ("ay1", 2, 4),
          "b-z-1": ("bz1", 3, 4), "b-z-2": ("bz2", 4, 4), "small": ("sm", 5, 2)}
BASE = "d9 d9 d9"  # every leaf shares it, so any two leaves sit at cosine 0.9


class Dims:
    """Fake embedder: each token d<k> adds one along axis k."""

    def __call__(self, texts):
        out = np.zeros((len(texts), 10), dtype=np.float32)
        for i, t in enumerate(texts):
            for k in re.findall(r"\bd(\d)\b", t):
                out[i, int(k)] += 1
        return out


def hub(r, id, title, parent, extra=()):
    rel = [*extra, *([f"[[{parent}]]"] if parent else [])]
    note(r, f"notes/{id}.md", f'title: "{title}"\nkind: note\nnote_type: structure\n'
         f'tags: ["topic"]\nrelated: {json.dumps(rel)}\n',
         f"# {title}\n\nHand paragraph about {title}.\n\n## What I know\n- a fact I wrote\n")


def page(r, id, words, hubs=(), date="2026-03-03", private=False, timeline=True,
         related=()):
    rel = [*related, *(f"[[{h}]]" for h in hubs)]
    body = f"# Page {id}\n\n"
    if timeline:
        body += f"Timeline: [[{date[:8]}01]] · Source: Gemini · Date: {date}\n"
        if hubs:
            body += "Topics: " + " · ".join(f"[[{h}]]" for h in hubs) + "\n"
        body += "\n"
    note(r, f"resources/{id}.md", f'title: "Page {id}"\nkind: resource\nrelated: '
         f'{json.dumps(rel)}\n' + ("private: true\n" if private else ""),
         body + f"**Me:** {words}\n")


def build_tree(r):
    for id, (title, parent) in HUBS.items():
        hub(r, id, title, parent)
    for h, (short, axis, n) in LEAVES.items():
        for k in range(n):
            page(r, f"p-{short}-{k}", f"{BASE} d{axis}", [h], date=f"2026-0{k + 1}-1{axis}")
    page(r, "secret", f"{BASE} d0", ["a-x-1"], private=True)
    note(r, "journal/2026-01-01.md", 'title: "January"\nkind: journal\n',
         "# 2026-01\n\n- a digest line\n")
    return r


@pytest.fixture
def vault(root):
    build_tree(root)
    page(root, "loose-0", f"{BASE} d0")
    page(root, "loose-mix", f"{BASE} d0 d3")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "hubs")
    return root


def index(r):
    v = Vault(r)
    return Index(v, Catalog.load(v))


def prep(r):
    idx = index(r)
    t = topics.tree(idx)
    stack = topics.head_stack(idx, Dims())
    return idx, t, topics.centroids(idx, t, stack)


def vec(words):
    return semantic._embed(Dims(), [words])[0]


def body(r, rel):
    return (r / rel).read_text(encoding="utf-8")


# ---- 1. tree --------------------------------------------------------------------------

def test_tree_parents_root_cycles_and_lca(vault):
    hub(vault, "a-y", "A Y", "area-a", extra=["[[p-ax1-0]]"])  # a non-hub related id first
    hub(vault, "c-1", "C one", "c-2")
    hub(vault, "c-2", "C two", "c-1")
    t = topics.tree(index(vault))
    assert t.parent["my-topic-map"] is None and t.parent["area-a"] == "my-topic-map"
    assert t.parent["a-y"] == "area-a" and t.parent["a-x-2"] == "a-x"
    assert sorted(t.kids["a-x"]) == ["a-x-1", "a-x-2"]
    assert (t.parent["c-1"], t.parent["c-2"]) == ("c-2", None)  # broken at the first repeat
    assert t.depth("a-x-1") == 3 and t.path("a-x-1") == ["my-topic-map", "area-a", "a-x",
                                                        "a-x-1"]
    assert t.lca(["a-x-1", "a-x-2"]) == "a-x" and t.lca(["a-x-1", "a-y-1"]) == "area-a"
    assert t.lca(["a-x-1", "b-z-1"]) == "my-topic-map" and t.lca(["a-x-1", "c-1"]) is None
    assert "secret" not in topics.members(index(vault), t)["a-x-1"]


# ---- 2. attach ------------------------------------------------------------------------

def test_attach_leaf_lca_none_floor_and_min_members(vault):
    _, t, cents = prep(vault)
    assert "small" not in cents[0]  # 2 members < MIN_MEMBERS
    a = topics.attach(vec(f"{BASE} d0"), t, cents)
    assert a.hub == "a-x-1" and a.score == pytest.approx(1.0)
    a = topics.attach(vec(f"{BASE} d0 d1"), t, cents)  # two close siblings
    assert a.hub == "a-x" and {h for h, _ in a.near} == {"a-x-1", "a-x-2"}
    assert topics.attach(vec(f"{BASE} d0 d3"), t, cents).hub is None  # LCA is the root
    a = topics.attach(vec("d8"), t, cents)
    assert a.hub is None and a.score < topics.FLOOR
    a = topics.attach(vec(f"{BASE} d5"), t, cents)  # only the small hub fits
    assert a.hub is None and "small" not in {h for h, _ in a.top}


def test_attach_leave_one_out_drops_the_pages_own_vector(vault):
    page(vault, "odd", f"{BASE} d1", ["a-x-1"])
    _, t, cents = prep(vault)
    v = vec(f"{BASE} d1")
    own = dict(topics.attach(v, t, cents, margin=0.2).near)
    loo = dict(topics.attach(v, t, cents, exclude={"a-x-1"}, margin=0.2).near)
    # the centroids come from the stored vectors, float16 (read back after a save)
    assert loo["a-x-1"] == pytest.approx(0.9, abs=1e-3) and own["a-x-1"] > 0.93


def test_evaluate_leave_one_out(vault):
    idx, t, _ = prep(vault)
    rows = topics.evaluate(idx, t, topics.head_stack(idx, Dims()), (0.02,))
    (r,) = rows
    assert r["pages"] == 22  # 5 leaves x 4 + small's 2; private and unfiled pages left out
    assert r["precision"] == 1.0 and r["exact"] == 1.0
    assert r["attached"] == pytest.approx(20 / 22) and r["depth"] == {3: 20}


# ---- 3. place -------------------------------------------------------------------------

def test_place_sets_related_topics_line_and_hub_lines(vault):
    v = Vault(vault)
    page(vault, "loose", f"{BASE} d0", related=["[[two-pointers]]"])
    assert topics.place(v, "loose", ["a-x-1"])
    text = body(vault, "resources/loose.md")
    assert 'related: ["[[two-pointers]]", "[[a-x-1]]"]' in text
    assert "Date: 2026-03-03\nTopics: [[a-x-1]]\n\n**Me:**" in text
    assert "## Conversations\n- 2026-03-03 · Gemini · [[loose|Page loose]]\n" in \
        body(vault, "notes/a-x-1.md")
    before = {p: body(vault, p) for p in ("resources/loose.md", "notes/a-x-1.md")}
    assert not topics.place(v, "loose", ["a-x-1"])  # idempotent
    assert before == {p: body(vault, p) for p in before}
    assert topics.place(v, "loose", ["b-z-1"])  # a move leaves the old hub's list
    assert "[[loose|" not in body(vault, "notes/a-x-1.md")
    assert "[[loose|" in body(vault, "notes/b-z-1.md")
    assert 'related: ["[[two-pointers]]", "[[b-z-1]]"]' in body(vault, "resources/loose.md")
    assert topics.place(v, "loose", [])
    text = body(vault, "resources/loose.md")
    assert "Topics:" not in text and 'related: ["[[two-pointers]]"]' in text


def test_place_after_h1_private_and_bad_ids(vault):
    v = Vault(vault)
    note(vault, "notes/plain.md", 'title: "Plain"\nkind: note\n', "# Plain\n\nSome text.")
    assert topics.place(v, "plain", ["a-x-1", "a-y-1"])
    assert "# Plain\n\nTopics: [[a-x-1]] · [[a-y-1]]\n\nSome text." in \
        body(vault, "notes/plain.md")
    secret = body(vault, "resources/secret.md")
    with pytest.raises(VaultError, match="private"):
        topics.place(v, "secret", ["a-x-2"])
    assert body(vault, "resources/secret.md") == secret
    assert "[[secret" not in body(vault, "notes/a-x-2.md")
    for bad in (["p-ax1-0"], ["nope"], ["a-x-1", "a-x-2", "a-y-1", "b-z-1", "b-z-2"]):
        with pytest.raises(VaultError) as e:
            topics.place(v, "plain", bad)
        assert e.value.code == "invalid_argument"
    with pytest.raises(VaultError, match="hub"):
        topics.place(v, "a-x-1", ["a-x"])


# ---- 4. build -------------------------------------------------------------------------

def test_build_regenerates_generated_parts_only(vault):
    v = Vault(vault)
    note(vault, "notes/a-x.md", 'title: "A X"\nkind: note\ntags: ["topic"]\n'
         'related: ["[[area-a]]"]\n', "# A X\n\nPart of [[area-a|Area A]] · stale\n\n"
         "Hand paragraph about A X.\n\n## Conversations\n- stale line\n\n## What I know\n"
         "- a fact I wrote\n\n## Watched & searched\n- [2025-12] YouTube: a video\n")
    files = sorted(p for p in vault.rglob("*.md") if ".git" not in p.parts)
    mtimes = {p: p.stat().st_mtime_ns for p in files}
    dry = topics.build(v, index(vault), dry_run=True)
    assert {p: p.stat().st_mtime_ns for p in files} == mtimes  # a dry run writes nothing
    assert not (vault / ".cortex" / "lock").exists()
    done = topics.build(v, index(vault))
    assert done == dry and done["hubs"] == 12 and done["months"] == 1 and not done["unsafe"]
    ax = body(vault, "notes/a-x.md")
    assert ax.startswith('---\ntitle: "A X"\n')
    assert "# A X\n\nPart of [[area-a|Area A]] · first explored 2025-12 · last 2026-04-11 · " \
           "Gemini 8, YouTube/Google 1\n\nHand paragraph about A X.\n\n" in ax
    assert "## What I know\n- a fact I wrote\n\n## Watched & searched\n- [2025-12] YouTube: " \
           "a video\n" in ax  # hand text and its place kept byte for byte
    assert "stale" not in ax and "## Conversations" not in ax
    assert "## Sub-topics\n- [[a-x-1|A X one]]: 4 conversations, 2026-01 to 2026-04\n" \
           "- [[a-x-2|A X two]]: 4 conversations, 2026-01 to 2026-04\n" in ax
    leaf = body(vault, "notes/a-x-1.md")
    assert "Part of [[a-x|A X]] · first explored 2026-01-10 · last 2026-04-10 · Gemini 4\n" \
        in leaf
    assert "Hand paragraph about A X one.\n\n## What I know\n- a fact I wrote\n\n" \
           "## Conversations\n- 2026-01-10 · Gemini · [[p-ax1-0|Page p-ax1-0]]\n" in leaf
    assert "secret" not in leaf
    area = body(vault, "notes/area-a.md")
    assert "# Area A\n\nPart of [[my-topic-map]].\n\nHand paragraph" in area
    assert "## Subtopics\n- [[a-x|A X]]: 9 items, 2025-12 to 2026-04 (2 sub-topics)\n" \
           "- [[a-y|A Y]]: 4 items, 2026-01 to 2026-04 (1 sub-topics)\n" in area
    root_map = body(vault, "notes/my-topic-map.md")
    assert "## Areas\n- [[area-a|Area A]]: 2 subtopics, 13 items\n" \
           "- [[area-b|Area B]]: 2 subtopics, 10 items\n" in root_map
    month = body(vault, "journal/2026-01-01.md")
    assert "- a digest line\n\n## Links\nTopics this month:\n" in month
    assert "- [[a-x|A X]] (2)\n" in month and "- [[small|Small]] (1)\n" in month
    again = topics.build(v, index(vault))
    assert again["hubs"] == again["months"] == 0 and again["unchanged"] == 13  # idempotent


def test_build_never_rewrites_hand_text(vault, monkeypatch):
    v = Vault(vault)
    before = body(vault, "notes/a-x-1.md")
    monkeypatch.setattr(topics, "_set_partof", lambda b, line, nl: b.replace("Hand", "Bot"))
    done = topics.build(v, index(vault))
    assert done["unsafe"] >= 1 and body(vault, "notes/a-x-1.md") == before


# ---- 5. cortex_add --------------------------------------------------------------------

def test_cortex_add_files_or_suggests(vault, monkeypatch):
    monkeypatch.setattr(semantic, "_embedder", Dims())

    async def go(call):
        sure = await call("cortex_add", {"kind": "resource", "title": "Loose zero",
                                         "fields": {"summary": f"{BASE} d0"}})
        assert sure["topic"]["id"] == "a-x-1" and "<untrusted_note" in sure["topic"]["title"]
        assert "Filed under [[a-x-1]]" in sure["next"] and "topic_suggestions" not in sure
        unsure = await call("cortex_add", {"kind": "note", "title": "Mixed bag",
                                           "fields": {"summary": f"{BASE} d0 d3"}})
        assert "topic" not in unsure and len(unsure["topic_suggestions"]) == 3
        assert all("<untrusted_note" in s["title"] for s in unsure["topic_suggestions"])
        assert "set_topics" in unsure["next"]
        named = await call("cortex_add", {"kind": "resource", "title": "Named hub",
                                          "fields": {"summary": f"{BASE} d0",
                                                     "related": ["a-x-2"]}})
        assert "topic" not in named and "topic_suggestions" not in named
        end = await call("cortex_end", {})
        assert "set topics on resources/loose-zero" in end["display"]

    run(vault, go)
    text = body(vault, "resources/loose-zero.md")
    assert '"[[a-x-1]]"' in text and "# Loose zero\n\nTopics: [[a-x-1]]\n\n## Notes" in text
    assert "[[loose-zero|Loose zero]]" in body(vault, "notes/a-x-1.md")
    assert "Topics:" not in body(vault, "notes/mixed-bag.md")
    assert 'related: ["[[a-x-2]]"]' in body(vault, "resources/named-hub.md")


def test_cortex_add_without_embedder(vault):
    async def go(call):
        out = await call("cortex_add", {"kind": "resource", "title": "No model",
                                        "fields": {"summary": f"{BASE} d0"}})
        assert "topic" not in out and "topic_suggestions" not in out and "error" not in out

    run(vault, go)
    assert "Topics:" not in body(vault, "resources/no-model.md")


# ---- 6. set_topics --------------------------------------------------------------------

def test_set_topics(vault):
    def patch(text, id="loose-0"):
        return {"id": id, "op": "set_topics", "target": "", "text": text}

    async def go(call):
        for bad in ("nope", "p-ax1-0", "a-x-1,a-x-2,a-y-1,b-z-1,b-z-2"):
            assert (await call("cortex_patch", patch(bad)))["error"] == "invalid_argument", bad
        assert (await call("cortex_patch", patch("a-x-1", "two-pointers")))["error"] \
            == "invalid_argument"
        ok = await call("cortex_patch", patch("a-x-1, b-z-2"))
        assert ok["changed"] is True
        assert 'related: ["[[a-x-1]]", "[[b-z-2]]"]' in body(vault, "resources/loose-0.md")
        cleared = await call("cortex_patch", patch(""))
        assert cleared["changed"] is True
        end = await call("cortex_end", {})
        assert "set topics on resources/loose-0" in end["display"]

    run(vault, go)
    text = body(vault, "resources/loose-0.md")
    assert "related: []" in text and "Topics:" not in text
    assert "[[loose-0|" not in body(vault, "notes/b-z-2.md")


# ---- 7. CLI ---------------------------------------------------------------------------

def test_cli_status_attach_build_and_eval(vault, monkeypatch):
    cli = CliRunner()

    def call(*args):
        r = cli.invoke(app, ["topics", *args, "--vault", str(vault)])
        assert r.exit_code == 0, r.output
        return r.output

    out = call("status")
    assert "hubs: 12 (depth 0: 1, depth 1: 2, depth 2: 4, depth 3: 5)" in out
    assert "unfiled: 2" in out
    assert "semantic search not set up" in cli.invoke(
        app, ["topics", "attach", "--vault", str(vault)]).output
    monkeypatch.setattr(semantic, "_embedder", Dims())
    before = body(vault, "resources/loose-0.md")
    out = call("attach", "--dry-run")
    assert "attached 1 of 2 (area 0, subtopic 0, leaf 1); left unfiled 1" in out
    assert "loose-0 -> a-x-1 (1.000): Page loose-0 -> A X one" in out
    assert "nothing written" in out and body(vault, "resources/loose-0.md") == before
    assert "pages with hubs: 22" in call("eval", "--margins", "0.02")
    assert "would change: hubs 12" in call("build", "--dry-run")
    call("attach")
    assert '"[[a-x-1]]"' in body(vault, "resources/loose-0.md")
    assert "Topics:" not in body(vault, "resources/loose-mix.md")


# ---- spec 3: split proposals (REVIEW-1005 §6) -----------------------------------------------

GROUPS = (("graph-bfs-dfs", 6), ("the-sql-join-index", 8), ("tree-heap-trie", 7))


def big_hub(r, id="big", parent="area-b", n=10, groups=GROUPS):
    """A hub with one group of n identical-direction pages per (words, axis); returns the ids."""
    hub(r, id, f"Big {id}", parent)
    out = []
    for words, axis in groups:
        ids = [f"{id}-{words}-{k}" for k in range(n)]
        for p in ids:
            page(r, p, f"d{axis} d{axis} d{axis}", [id])
        out.append(ids)
    return out


def proposal(r, hub="big"):
    idx = index(r)
    return topics.propose_split(idx, topics.tree(idx), topics.head_stack(idx, Dims()), hub)


def test_split_clusters_outliers_and_share_retry(vault, monkeypatch):
    groups = big_hub(vault)
    page(vault, "big-secret", "d6 d6 d6", ["big"], private=True)
    p = proposal(vault)
    assert p["k"] == 3 and p["stay"] == [] and "note" not in p
    assert sorted(sorted(c["members"]) for c in p["clusters"]) == sorted(groups)
    assert "big-secret" not in json.dumps(p)  # private pages never appear
    for k in range(2):  # a 2-page outlier group stays on the parent
        page(vault, f"big-odd-{k}", "d5 d5 d5", ["big"])
    p = proposal(vault)
    assert len(p["clusters"]) == 3 and p["stay"] == ["big-odd-0", "big-odd-1"]
    calls = []
    real = topics._kmeans
    monkeypatch.setattr(topics, "_kmeans", lambda X, k, s: calls.append(k) or real(X, k, s))
    big_hub(vault, "lop", n=18, groups=[("one-two", 6)])
    for words, axis in (("red-blue", 7), ("cat-dog", 8), ("sun-moon", 5)):
        for k in range(4):
            page(vault, f"lop-{words}-{k}", f"d{axis} d{axis} d{axis}", ["lop"])
    p = proposal(vault, "lop")  # 18 of 30 pages (60%) in one cluster: one k+1 retry
    assert p["k"] == 4 and "k+1" in p["note"] and calls.count(5) == 2


def test_split_hash_gate_depth_and_labels(vault, monkeypatch):
    big_hub(vault)
    p = proposal(vault)
    assert proposal(vault)["hash"] == p["hash"] and len(p["hash"]) == 16
    terms = {tuple(c["terms"]) for c in p["clusters"]}
    assert terms == {("bfs", "dfs", "graph"), ("index", "join", "sql"), ("heap", "tree", "trie")}
    assert all(len(c["sample_titles"]) == 5 for c in p["clusters"])
    page(vault, "big-graph-bfs-dfs-new", "d6 d6 d6", ["big"])
    assert proposal(vault)["hash"] != p["hash"]  # a new member changes the hash
    monkeypatch.setattr(topics, "SIL_MIN", 1.01)
    assert proposal(vault) is None
    monkeypatch.undo()
    hub(vault, "big", "Big big", "a-x-1")  # now at depth 4
    assert proposal(vault) is None


def split_children(p, **extra):
    names = {"bfs": "Graph search", "index": "SQL joins", "heap": "Tree structures"}
    return [{"title": names[c["terms"][0]], "desc": "Pages about " + " ".join(c["terms"])
             + " and the problems the user worked through on them.", **extra}
            for c in p["clusters"]]


def split(call, args):
    return call("cortex_reflect", {"action": "split", **args})


def test_reflect_and_cortex_split(vault, monkeypatch):
    groups = big_hub(vault)
    monkeypatch.setattr(semantic, "_embedder", Dims())
    out = {}

    async def go(call):
        r = await call("cortex_reflect")
        (s,) = r["splits"]
        assert s["hub"] == "big" and "action=\'split\'" in r["next"]
        assert all(c["titles"].startswith('<untrusted_note id="split">') for c in s["clusters"])
        assert "<untrusted_note" in s["title"] and [c["n"] for c in s["clusters"]] == [10] * 3
        prop = json.loads((vault / ".cortex/proposals/split-big.json").read_text("utf-8"))
        assert prop["hash"] == s["hash"]
        kids = split_children(prop)
        bad = await split(call, {"hub": "big", "hash": "0" * 16, "children": kids})
        assert bad["error"] == "invalid_argument" and "cortex_reflect" in bad["message"]
        bad = await split(call, {"hub": "big", "hash": s["hash"], "children": kids[:2]})
        assert bad["error"] == "invalid_argument"
        outside = [{**k, "members": ["p-ax1-0"]} for k in kids]
        bad = await split(call, {"hub": "big", "hash": s["hash"], "children": outside})
        assert bad["error"] == "invalid_argument" and "p-ax1-0" in bad["message"]
        args = {"hub": "big", "hash": s["hash"], "children": kids, "key": "split-1"}
        out["ok"] = await split(call, args)
        assert (await split(call, args)) == out["ok"]  # a repeated key writes nothing
        out["end"] = (await call("cortex_end", {}))["display"]

    run(vault, go)
    ok = out["ok"]
    assert ok["stayed"] == 0 and sorted(c["n"] for c in ok["children"]) == [10, 10, 10]
    assert {c["id"] for c in ok["children"]} == {"big-graph-search", "big-sql-joins",
                                                 "big-tree-structures"}
    child = body(vault, "notes/big-graph-search.md")
    assert 'tags: ["topic"]' in child and 'related: ["[[big]]"]' in child
    assert 'note_type: "structure"' in child and "names:" not in child
    assert "# Graph search\n\nPart of [[big|Big big]]" in child
    assert "Pages about bfs dfs graph" in child
    assert "[[big-graph-bfs-dfs-0|Page big-graph-bfs-dfs-0]]" in child
    member = body(vault, f"resources/{groups[0][0]}.md")
    assert '"[[big-graph-search]]"' in member and '"[[big]]"' not in member
    assert "Topics: [[big-graph-search]]" in member
    parent = body(vault, "notes/big.md")
    assert "## Sub-topics\n" in parent and "[[big-sql-joins|SQL joins]]: 10 conversations" in parent
    assert "## Conversations" not in parent  # no direct pages are left on the parent
    idx = index(vault)
    assert not topics.members(idx, topics.tree(idx)).get("big")
    assert "split notes/big into sub-topics (30 pages moved)" in out["end"]
    assert "added note big-graph-search" in out["end"]


def test_reflect_caps_splits_at_two(vault, monkeypatch):
    for h in ("big1", "big2", "big3"):
        big_hub(vault, h, n=4)
    monkeypatch.setattr(topics, "SPLIT_N", 10)
    monkeypatch.setattr(semantic, "_embedder", Dims())

    async def go(call):
        r = await call("cortex_reflect")
        assert [s["hub"] for s in r["splits"]] == ["big1", "big2"]

    run(vault, go)


def test_cli_split_list_propose_apply_and_eval(vault, monkeypatch):
    big_hub(vault)
    cli = CliRunner()

    def call(*args):
        r = cli.invoke(app, ["topics", *args, "--vault", str(vault)])
        assert r.exit_code == 0, r.output
        return r.output

    assert "big (30 pages)" in call("split")
    monkeypatch.setattr(semantic, "_embedder", Dims())
    out = call("split", "big", "--propose")
    assert "k=3" in out and "bfs, dfs, graph" in out
    assert (vault / ".cortex/proposals/split-big.json").is_file()
    assert not (vault / "notes/big-bfs-dfs-graph.md").exists()  # --propose writes no page
    out = call("split", "big", "--apply", "--auto-names")
    assert "created 3 provisional sub-topics; 0 pages stay" in out
    child = body(vault, "notes/big-bfs-dfs-graph.md")
    assert 'names: "provisional"' in child and "# Bfs Dfs Graph" in child
    out = call("eval", "--splits")
    assert "split hubs: 1; compared with an automatic split: 1" in out
    assert "ratio 1.00" in out
