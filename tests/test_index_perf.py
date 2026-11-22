"""Spec 1 (docs/design/v5-research/REVIEW-1005.md §4): rowid-keyed FTS deletes, the parse
snapshot and serving before the index is built. Synthetic vaults only."""

import json
import logging
import shutil
import sqlite3
import time
from pathlib import Path

import anyio
import pytest
from mcp import Client

from cortex import index as index_mod
from cortex import mcp_server
from cortex.catalog import Catalog
from cortex.index import SNAPSHOT, Index
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

FIXTURE = Path(__file__).parent / "fixtures" / "vault_mini"


def page(i: int, word: str = "oldword", mention: bool = True) -> str:
    body = f"# Page {i}\n\nText {word} n{i}.\n" + (f"See [[r-{i + 2}]].\n" if mention else "")
    return f'---\ntitle: "Page {i}"\nrelated: ["[[r-{i + 1}]]"]\n---\n{body}'


def make_vault(tmp_path, n: int) -> Path:
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    (root / "resources").mkdir()
    for i in range(n):
        (root / "resources" / f"r-{i}.md").write_text(page(i), encoding="utf-8")
    return root


def build(root: Path) -> Index:
    v = Vault(root)
    return Index(v, Catalog.load(v))


def rows(idx: Index, sql: str) -> list:
    return sorted(idx.db.execute(sql).fetchall())


def count(idx: Index, match: str) -> int:
    return idx.db.execute("SELECT count(*) FROM fts WHERE fts MATCH ?", (match,)).fetchone()[0]


@pytest.fixture
def traced(monkeypatch):
    sql: list[str] = []
    real = sqlite3.connect

    def connect(*a, **k):
        db = real(*a, **k)
        db.set_trace_callback(sql.append)
        return db

    monkeypatch.setattr(index_mod.sqlite3, "connect", connect)
    return sql


@pytest.fixture
def parses(monkeypatch):
    calls: list[str] = []
    real = Index._parse

    def counted(self, path, kind):
        calls.append(path.stem)
        return real(self, path, kind)

    monkeypatch.setattr(Index, "_parse", counted)
    return calls


def edit(root: Path, ids, **kw) -> None:
    for i in ids:
        (root / "resources" / f"r-{i}.md").write_text(page(i, **kw), encoding="utf-8")


# ---- 4.1 rowid-keyed deletes --------------------------------------------------

def test_fresh_build_issues_no_fts_delete_and_refresh_deletes_by_rowid(tmp_path, traced):
    root = make_vault(tmp_path, 200)
    idx = build(root)
    # the quadratic pattern: a delete by the UNINDEXED id before every insert scans all of fts
    assert not [s for s in traced if s.startswith("DELETE FROM fts")]
    edit(root, [1, 2, 3], word="newword")
    traced.clear()
    assert idx.refresh() == 3
    deletes = [s for s in traced if s.startswith("DELETE FROM fts")]
    assert len(deletes) == 3 and all("WHERE rowid" in s for s in deletes)


def test_bulk_edit_leaves_no_stale_rows(tmp_path):
    root = make_vault(tmp_path, 200)
    idx = build(root)
    edited = range(50, 100)
    edit(root, edited, word="newword", mention=False)
    idx.refresh()
    assert count(idx, "oldword") == 150 and count(idx, "newword") == 50
    assert {h["id"] for h in idx.find("newword")} <= {f"r-{i}" for i in edited}
    total = len(list((root / "resources").glob("*.md"))) + 12  # + vault_mini's 12 notes
    assert idx.db.execute("SELECT count(*) FROM fts").fetchone()[0] == total - 1  # 1 private
    assert idx.db.execute("SELECT count(*) FROM nodes").fetchone()[0] == total - 1
    synthetic = idx.db.execute("SELECT type, count(*) FROM edges WHERE src LIKE 'r-%' "
                               "GROUP BY type").fetchall()
    assert dict(synthetic) == {"related": 200, "mentions": 150}


# ---- 4.2 snapshot ---------------------------------------------------------------

def test_snapshot_round_trip_parses_nothing(tmp_path, parses):
    root = make_vault(tmp_path, 60)
    first = build(root)
    assert (root / SNAPSHOT).is_file() and len(parses) >= 60
    parses.clear()
    second = build(root)
    assert parses == []
    assert second._notes == first._notes
    for sql in ("SELECT * FROM nodes", "SELECT * FROM edges",
                "SELECT id, kind, title, aliases, headings, body FROM fts"):
        assert rows(second, sql) == rows(first, sql)
    for q in ("oldword", "two pointers", "page 7", "sum"):
        assert second.find(q) == first.find(q)


def test_snapshot_changed_deleted_new_and_private(tmp_path, parses):
    root = make_vault(tmp_path, 60)
    area = root / "areas" / "dsa.arrays.md"  # a private area hides its concepts
    area.write_text(area.read_text(encoding="utf-8").replace(
        'domain: "dsa"', 'domain: "dsa"\nprivate: true'), encoding="utf-8")
    (root / "resources" / "secret.md").write_text(
        '---\ntitle: "Secret"\nprivate: true\n---\n# Secret\n\nhiddenword\n', encoding="utf-8")
    build(root)
    snap = json.loads((root / SNAPSHOT).read_text(encoding="utf-8"))["files"]
    assert snap["resources/secret.md"][4:] == [{"private": True, "flagged": False}]
    assert snap["areas/dsa.arrays.md"][4:] == [{"private": True, "flagged": True}]
    assert "concepts/two-pointers.md" not in snap  # private by inheritance: reparsed
    assert "hiddenword" not in (root / SNAPSHOT).read_text(encoding="utf-8")

    edit(root, [5], word="newword")
    (root / "resources" / "r-6.md").unlink()
    (root / "resources" / "fresh.md").write_text("# Fresh\n\nfreshword\n", encoding="utf-8")
    parses.clear()
    idx = build(root)
    # inherited-private pages are parsed (twice: before and after planner.hidden); nothing else.
    # dsa.off-by-one is a mistake about those concepts, so it is hidden too (OQ-22)
    assert set(parses) == {"arrays-basics", "fresh", "r-5", "two-pointers", "dsa.off-by-one"}
    assert [h["id"] for h in idx.find("newword")] == ["r-5"]
    assert [h["id"] for h in idx.find("freshword")] == ["fresh"]
    assert "r-6" not in idx._notes and not idx.find("n6")
    assert idx.find("hiddenword") == [] and "secret" in idx._private
    assert {"dsa.arrays", "two-pointers", "arrays-basics", "dsa.off-by-one"} <= idx._private
    assert "two-pointers" not in [h["id"] for h in idx.find("two pointers")]
    assert "dsa.arrays" in idx._flagged

    # lifting the area's flag brings its concepts back in full
    area.write_text(area.read_text(encoding="utf-8").replace("private: true\n", ""),
                    encoding="utf-8")
    idx.refresh()
    assert [h["id"] for h in idx.find("two pointers")][0] == "two-pointers"
    assert "secret" in idx._private


@pytest.mark.parametrize("text", ["{not json", json.dumps({"schema": 0, "files": {}}),
                                  json.dumps([1, 2])])
def test_bad_snapshot_is_ignored_with_a_warning(tmp_path, caplog, text):
    root = make_vault(tmp_path, 60)
    good = build(root)
    (root / SNAPSHOT).write_text(text, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="cortex.index"):
        idx = build(root)
    assert [r for r in caplog.records if "snapshot ignored" in r.getMessage()]
    assert idx._notes == good._notes
    assert rows(idx, "SELECT * FROM edges") == rows(good, "SELECT * FROM edges")
    assert json.loads((root / SNAPSHOT).read_text(encoding="utf-8"))["schema"] == 1  # rebuilt


# ---- 4.3 serve before the index is built ------------------------------------------

def slow_index(monkeypatch, seconds: float) -> None:
    real = Index.__init__

    def init(self, *a, **k):
        time.sleep(seconds)
        real(self, *a, **k)

    monkeypatch.setattr(Index, "__init__", init)


def call_find(server):
    async def main():
        async with Client(server) as client:
            r = await client.call_tool("cortex_find", {"query": "two pointers"})
            return r.structured_content

    return anyio.run(main)


@pytest.mark.perf  # a 0.5 s budget: under -n auto the machine is busy
def test_server_answers_before_index_is_built(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    mcp_server.build_server(Vault(root))  # first start: catalog sync writes its stamp
    slow_index(monkeypatch, 2)
    t = time.perf_counter()
    server = mcp_server.build_server(Vault(root))
    assert time.perf_counter() - t < 0.5
    out = call_find(server)
    assert out["hits"][0]["id"] == "two-pointers", out


def test_tool_times_out_while_indexing(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    slow_index(monkeypatch, 2)
    monkeypatch.setattr(mcp_server, "READY_WAIT", 0.1)
    out = call_find(mcp_server.build_server(Vault(root)))
    assert out["error"] == "vault_locked" and "still indexing" in out["message"]
    assert out["next"]
