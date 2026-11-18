"""NEXT.md item 1: the cross-encoder rerank, the recall usage log and `cortex eval recall`."""

import json

import pytest
from conftest import FakeEmbed
from test_semantic import ids, idx, root  # noqa: F401  (test_semantic's seeded vault)
from typer.testing import CliRunner

from cortex import recall_log, semantic
from cortex.cli import app
from cortex.mcp_server import Cortex
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")
REAL_LOAD = semantic.load_reranker  # conftest swaps it per test; this is the original


def pasta_first(query, texts):
    return [t.count("Pasta") for t in texts]


def test_rerank_reorders_and_keeps_score_and_match(idx):  # noqa: F811
    pool = {h["id"]: h for h in semantic.recall(idx, "vehicle", FakeEmbed(), 20)}
    base = semantic.recall(idx, "vehicle", FakeEmbed())
    rr = semantic.recall(idx, "vehicle", FakeEmbed(), rerank_with=pasta_first)
    assert ids(base)[0] == "engines" and ids(rr)[:2] == ["pasta", "engines"]
    assert all(h == pool[h["id"]] for h in rr)  # score, match and via as recall gave them
    assert len(semantic.recall(idx, "vehicle", FakeEmbed(), 2, rerank_with=pasta_first)) == 2


def test_no_reranker_or_a_broken_one_keeps_the_current_order(idx, home, monkeypatch):  # noqa: F811
    base = semantic.recall(idx, "vehicle", FakeEmbed())
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    assert semantic.search(idx, "vehicle")[0] == base  # none injected: current ranking

    def broken(query, texts):
        raise RuntimeError("onnx")

    assert semantic.recall(idx, "vehicle", FakeEmbed(), rerank_with=broken) == base
    assert REAL_LOAD() is None and not (home / ".cortex/models").exists()  # never downloads

    monkeypatch.setattr(semantic, "_reranker", pasta_first)
    assert ids(semantic.search(idx, "vehicle")[0])[0] == "pasta"
    (home / ".cortex").mkdir()
    (home / ".cortex/config.yaml").write_text("vault: x\nrerank: false\n", "utf-8")
    assert REAL_LOAD() is None and REAL_LOAD(config=False) is pasta_first
    assert semantic.search(idx, "vehicle")[0] == base  # switched off


def logged(root):  # noqa: F811
    return [json.loads(x) for x in (root / recall_log.FILE).read_text("utf-8").splitlines()]


def test_recall_and_context_are_logged_and_reads_link_to_them(root, monkeypatch):  # noqa: F811
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    c = Cortex(Vault(root))
    got = ids(c.cortex_recall("vehicle")["hits"])
    c.cortex_context(topic="automobile")
    c.cortex_read("pasta")  # returned by the recall only
    c.cortex_read("engines")  # returned by both: the latest recall
    c.cortex_recall("I like cars.", check=True)  # a check is not logged
    rows = logged(root)
    assert [r.get("tool") for r in rows] == ["cortex_recall", "cortex_context", None, None]
    assert rows[0]["query"] == "vehicle" and rows[0]["ids"] == got and rows[0]["ts"]
    assert "pasta" in got and "pasta" not in rows[1]["ids"]
    assert rows[2] == {"ts": rows[2]["ts"], "opened": rows[0]["rid"], "id": "pasta"}
    assert rows[3]["opened"] == rows[1]["rid"] and rows[3]["id"] == "engines"
    assert not list(root.joinpath("events").rglob("*.jsonl"))  # never in the event log

    c._recalls = [(t - recall_log.WINDOW_S - 1, rid, i) for t, rid, i in c._recalls]
    c.cortex_read("gearbox")  # too late: no row
    assert len(logged(root)) == 4

    monkeypatch.setattr(recall_log, "CAP", 3)
    c.cortex_recall("motor")
    assert [r.get("query") for r in logged(root)] == [None, None, "motor"]


def test_eval_set_and_metrics(idx):  # noqa: F811
    rows = [{"rid": "a", "query": "vehicle", "ids": ["engines"]},
            {"rid": "b", "query": "nothing opened", "ids": ["pasta"]},
            {"rid": "c", "query": "opened elsewhere", "ids": ["pasta"]},
            {"opened": "a", "id": "engines"}, {"opened": "a", "id": "gearbox"},
            {"opened": "c", "id": "engines"}]
    assert recall_log.eval_set(rows) == [("vehicle", {"engines", "gearbox"})]
    m = recall_log.evaluate(idx, [("vehicle", {"engines"})], FakeEmbed(), pasta_first, k=3)
    assert m["current"] == (1.0, 1.0) and m["rerank"] == (1.0, 0.5)
    assert set(m) == set(recall_log.VARIANTS) and m["rrf"][1] == 1.0
    assert recall_log.evaluate(idx, [], None, None)["rerank"] is None


def test_eval_cli_prints_each_variant(root, monkeypatch):  # noqa: F811
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    c = Cortex(Vault(root))
    c.cortex_recall("vehicle")
    c.cortex_read("engines")
    run = CliRunner().invoke
    out = run(app, ["eval", "recall", "--vault", str(root), "--k", "3"])
    assert out.exit_code == 0, out.output
    assert "queries: 1 (too few to trust" in out.output
    assert "current" in out.output and "skipped: reranker not downloaded" in out.output
    monkeypatch.setattr(semantic, "_reranker", pasta_first)
    out = run(app, ["eval", "recall", "--vault", str(root)])
    rows = {ln.split()[0]: ln.split()[1:] for ln in out.output.splitlines()[2:]}
    assert rows["rerank"] == ["1.000", "0.500"] and rows["current"] == ["1.000", "1.000"]
    assert rows["no-downrank"] == ["1.000", "1.000"]  # no chat pages: same as current
