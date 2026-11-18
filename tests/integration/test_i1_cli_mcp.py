"""I1 CLI <-> vault <-> index <-> MCP (docs/design/v6/TEST-PLAN.md §3): what the CLI writes the
MCP server finds, a grade shows the same level on both sides, a void undoes it on both, and
an event key written again after `cortex undo` lands (board verdict #17)."""

import re

import pytest
from conftest import FakeEmbed, events, git, run
from typer.testing import CliRunner

from cortex import semantic
from cortex.cli import app
from cortex.events import EventLog
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")


def cli(*args, vault=None) -> str:
    r = CliRunner().invoke(app, [*map(str, args), *(("--vault", str(vault)) if vault else ())])
    assert r.exit_code == 0, r.output
    return r.output


def added(out: str) -> str:
    return re.search(r"added \w+ (\S+)", out).group(1)


def dry_state(vault, cid: str) -> str:
    """The level `cortex replay --dry-run` gives `cid`: its diff line, else its stamp."""
    out = cli("replay", "--dry-run", vault=vault)
    if m := re.search(rf"^{re.escape(cid)}  \S+ → (\S+)", out, re.M):
        return m.group(1)
    return Vault(vault).read("concept", cid).fields["cortex_status"]


def test_i1_cli_and_mcp_share_one_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    v = tmp_path / "vault"
    cli("init", v)
    dom = added(cli("concept", "add", "Graphs", "--kind", "domain", vault=v))
    area = added(cli("concept", "add", "Spanning trees", "--kind", "area", "--parent", dom,
                     vault=v))
    kid = added(cli("concept", "add", "Kruskal algorithm", "--parent", area, vault=v))
    cli("log", "taught", kid, vault=v)
    taught = dry_state(v, kid)
    got = {}

    async def body(call):
        # a concept added by the CLI is found by cortex_find and cortex_recall
        (hit,) = [h for h in (await call("cortex_find", {"query": "Kruskal"}))["hits"]
                  if h["id"] == kid]
        assert hit["state"] == taught
        assert kid in {h["id"] for h in (await call("cortex_recall",
                                                    {"query": "Kruskal algorithm"}))["hits"]}

        # a grade over MCP: replay --dry-run shows the level the MCP status shows
        g = await call("cortex_grade", {"concept": kid, "question": "Why sort edges first?",
                                        "item_type": "recall", "learner_answer": "greedy",
                                        "verdict": "correct", "key": "k-17"})
        assert "error" not in g, g
        state = (await call("cortex_find", {"query": "Kruskal"}))["hits"][0]["state"]
        assert state != taught and dry_state(v, kid) == state

        # a CLI void removes the grade on both sides
        gid = next(e["id"] for e in events(v) if e["kind"] == "grade")
        cli("void", gid, "--reason", "test", vault=v)
        assert (await call("cortex_find", {"query": "Kruskal"}))["hits"][0]["state"] == taught
        assert dry_state(v, kid) == taught

        # #17: another process sees the key; after undo the same key writes again
        assert EventLog(Vault(v)).has_key("k-17")
        await call("cortex_end", {"summary": "i1"})
        got["session"] = next(e["session"] for e in events(v) if e.get("key") == "k-17")
        assert "reverted" in cli("undo", got["session"], vault=v)
        assert not EventLog(Vault(v)).has_key("k-17")
        # the same server: its reply cache must not swallow the key the undo removed
        await call("cortex_grade", {"concept": kid, "question": "Why sort edges first?",
                                    "item_type": "recall", "learner_answer": "greedy",
                                    "verdict": "correct", "key": "k-17"})
        await call("cortex_end", {"summary": "i1 again"})

    run(v, body)
    again = [e for e in events(v) if e.get("key") == "k-17"]
    assert len(again) == 1 and again[0]["session"] != got["session"]
    assert git(v, "status", "--porcelain") == ""
