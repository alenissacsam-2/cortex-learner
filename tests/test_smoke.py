import importlib

import anyio
import pytest
from conftest import git
from mcp import Client
from typer.testing import CliRunner

from keepfold import mcp_server
from keepfold.cli import app
from keepfold.mcp_server import build_server
from keepfold.vault import Vault

MODULES = [
    "vault", "catalog", "events", "levels", "planner", "session", "index",
    "render", "doctor", "backup", "cli", "mcp_server",
]


@pytest.mark.parametrize("name", MODULES)
def test_import(name):
    importlib.import_module(f"keepfold.{name}")


@pytest.fixture
def root(root_committed, monkeypatch):
    monkeypatch.setattr(mcp_server, "CODE_MTIME", float("inf"))  # no restart_needed noise
    return root_committed


def test_version():
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("keepfold ")


# ---- end-to-end study path (v6 CONSENSUS item 2) ----------------------------------
# Shape and invariants only, not wording: every behaviour change must keep this green.


def test_study_path_end_to_end(root):
    server = build_server(Vault(root))
    got = {}

    async def main():
        async with Client(server) as client:
            async def call(name, args=None):
                r = await client.call_tool(name, args or {})
                assert not r.is_error, r.content
                out = r.structured_content
                assert out.get("next") and "error" not in out, out
                return out

            home = await call("keepfold_start", {"mode": "home"})
            assert home["mode"] == "home" and home["display"].startswith("### Keepfold")
            await call("keepfold_end")
            short = await call("keepfold_start", {"mode": "short"})
            assert short["mode"] == "short" and 0 < len(short["today"]) <= 5
            await call("keepfold_end")
            learn = await call("keepfold_start", {"mode": "learn", "model": "test-model"})
            assert learn["mode"] == "learn" and learn["today"] and learn["display"]
            ask = await call("keepfold_ask", {"concept": "two-pointers", "item_type": "recall",
                                            "question": "Which option?", "answer_key": "B"})
            graded = await call("keepfold_grade", {"item_id": ask["item_id"],
                                                 "learner_answer": "B", "verdict": "correct"})
            assert graded["state_after"] and graded["display_line"]
            logged = await call("keepfold_log", {"kind": "assessment_result", "payload": {
                "company": "Globex", "assessment_kind": "oa", "date": "2026-11-25",
                "outcome": "pending"}})
            assert logged["recorded"] == 1
            end = await call("keepfold_end", {"summary": "Smoke test session."})
            assert end["display"] and end["counts"]["graded"] == 1 and end["commit"]
            got.update(session=learn["session"], commit=end["commit"])

    anyio.run(main)
    assert git(root, "rev-parse", "HEAD").strip() == got["commit"]
    assert got["session"] in git(root, "log", "-1", "--format=%s", got["commit"])
    assert git(root, "status", "--porcelain") == ""
