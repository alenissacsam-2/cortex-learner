"""v6 "one eval history": `cortex eval all` appends paired, numbers-only rows."""

import pytest
from conftest import FakeEmbed
from test_semantic import root  # noqa: F401  (test_semantic's seeded vault)
from typer.testing import CliRunner

from cortex import eval_history, semantic
from cortex.cli import app
from cortex.mcp_server import Cortex
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")


def run(root, sha, monkeypatch):  # noqa: F811
    monkeypatch.setattr(eval_history, "git_sha", lambda: sha)
    out = CliRunner().invoke(app, ["eval", "all", "--vault", str(root), "--k", "3"])
    assert out.exit_code == 0, out.output
    return out.output


def recall_rows(root):  # noqa: F811
    return [r for r in eval_history.read(Vault(root)) if r["metric"] != "brier"]


def test_runs_pair_only_on_the_same_day_and_index_across_commits(root, monkeypatch):  # noqa: F811
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    c = Cortex(Vault(root))
    c.cortex_recall("secret vehicle query")
    c.cortex_read("engines")

    first = run(root, "aaa", monkeypatch)
    assert "no earlier run on this day and index" in first and "(too few)" in first
    rows = recall_rows(root)
    assert {r["variant"] for r in rows} == {"current", "rrf", "no-downrank"}  # no reranker
    cur = next(r for r in rows if r["variant"] == "current" and r["metric"] == "recall@3")
    assert cur["n"] == 1 and cur["value"] == cur["baseline_value"] == 1.0

    run(root, "aaa", monkeypatch)  # same commit: appended, but not a before/after pair
    again = recall_rows(root)
    assert len(again) == 2 * len(rows)
    assert again[:len(rows)] == again[len(rows):]  # nothing changed: identical rows

    after = run(root, "bbb", monkeypatch)
    assert "vs aaa" in after and "+0.000" in after
    assert "no earlier run" not in after

    hist = eval_history.read(Vault(root))
    other_day = [{**r, "date": "2000-01-01", "git_sha": "ccc"} for r in hist]
    other_index = [{**r, "index": "x", "git_sha": "ccc"} for r in hist]
    fresh = [r for r in hist if r["git_sha"] == "bbb"]
    assert eval_history.before(other_day + other_index, fresh) == {}

    raw = (root / eval_history.FILE).read_text("utf-8")
    assert "secret" not in raw and "vehicle" not in raw and "engines" not in raw  # numbers only


def test_bare_eval_runs_all(root, monkeypatch):  # noqa: F811
    """review r4 S2: `cortex eval` alone is `cortex eval all`, history row and all."""
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    Cortex(Vault(root)).cortex_recall("vehicle")
    first = run(root, "aaa", monkeypatch)
    n = len(eval_history.read(Vault(root)))
    out = CliRunner().invoke(app, ["eval", "--vault", str(root), "--k", "3"])
    assert out.exit_code == 0 and out.output == first
    assert len(eval_history.read(Vault(root))) == 2 * n


def test_options_before_a_subcommand_are_refused(root):  # noqa: F811
    out = CliRunner().invoke(app, ["eval", "--vault", str(root), "recall"])
    assert out.exit_code != 0 and "after the subcommand" in out.output


def test_a_page_edit_changes_the_index_key(root, monkeypatch):  # noqa: F811
    from cortex.catalog import Catalog
    from cortex.index import Index

    v = Vault(root)
    key = eval_history.fingerprint(Index(v, Catalog.load(v)), True)
    assert key != eval_history.fingerprint(Index(v, Catalog.load(v)), False)
    (root / "notes/pasta.md").write_text("---\ntitle: Pasta\n---\n# Pasta\n\nchanged\n", "utf-8")
    assert key != eval_history.fingerprint(Index(v, Catalog.load(v)), True)


def test_rows_and_empty_runs():
    learning = {"n": 41, "brier": 0.157, "brier_base": 0.238}
    rows = eval_history.rows({}, 0, 8, learning, date="2026-11-16", sha="a", index="i")
    assert rows == [{"date": "2026-11-16", "git_sha": "a", "index": "i", "metric": "brier",
                     "variant": "all", "value": 0.157, "n": 41, "baseline_value": 0.238}]
    assert "-0.081" in eval_history.text(rows, {})
    assert eval_history.rows({"current": (0.0, 0.0)}, 0, 8, {"n": 0}, date="d", sha="a",
                             index="i") == []
    assert eval_history.text([], {}).startswith("nothing to measure yet")
    assert eval_history.git_sha()  # this checkout's commit, or "unknown"
