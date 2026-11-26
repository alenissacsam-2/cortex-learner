"""An editable install updated under a running server: every tool refuses with
restart_needed and records nothing (old in-memory modules + new lazy imports crash)."""
import time

import pytest
from conftest import events, run

from keepfold import mcp_server

pytestmark = pytest.mark.usefixtures("home")
JOURNAL = {"kind": "journal", "payload": {"text": "late night"}}


def test_changed_code_refuses_writes_and_end(root, monkeypatch):
    async def body(call):
        assert "error" not in await call("keepfold_log", JOURNAL)  # opens an ambient session
        before = events(root)
        monkeypatch.setattr(mcp_server, "CODE_MTIME", 0.0)  # as if loaded before an update
        monkeypatch.setattr(mcp_server, "_scan", [float("-inf"), 0.0])
        for name, args in (("keepfold_log", JOURNAL), ("keepfold_end", {}),
                           ("keepfold_context", {}), ("keepfold_start", {})):
            out = await call(name, args)
            assert out["error"] == "restart_needed", name
            assert "restart Claude" in out["message"] and "nothing was recorded" in out["next"]
        assert events(root) == before

    run(root, body)


def test_unchanged_code_runs_tools(root):
    async def body(call):
        before = len(events(root))
        assert "error" not in await call("keepfold_log", JOURNAL)
        assert "error" not in await call("keepfold_end", {})
        assert len(events(root)) > before

    run(root, body)


def test_import_error_after_a_change_is_restart_needed(root, monkeypatch):
    def broken(vault):
        raise ImportError("cannot import name 'PRIVATE_BROWSE_DIR' from 'keepfold.vault'")

    async def body(call):
        monkeypatch.setattr(mcp_server.Catalog, "load", broken)
        monkeypatch.setattr(mcp_server, "CODE_MTIME", 10.0)
        # the cached scan still says unchanged; the error path rescans and sees the update
        monkeypatch.setattr(mcp_server, "_scan", [time.monotonic(), 0.0])
        monkeypatch.setattr(mcp_server, "_code_mtime", lambda: 100.0)
        assert (await call("keepfold_log", JOURNAL))["error"] == "restart_needed"

    run(root, body)


def test_mtime_scan_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_server, "_code_mtime", lambda: calls.append(1) or 0.0)
    monkeypatch.setattr(mcp_server, "_scan", [float("-inf"), 0.0])
    for _ in range(5):
        assert mcp_server._code_changed() is False
    assert len(calls) == 1
    mcp_server._scan[0] -= mcp_server.CODE_SCAN_EVERY  # the window passed
    mcp_server._code_changed()
    assert len(calls) == 2
