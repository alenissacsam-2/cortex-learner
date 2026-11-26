"""R29a: `keepfold mcp` (stdio) exits when its client goes away (stdin EOF), within seconds, even
while the warm-up loads a model or a tool call waits for the start-up index build. Each test
starts its own server process on a temp copy of vault_mini, with the isolated home conftest
sets in the environment."""

import json
import os
import shutil
import subprocess
import sys
import time

import pytest
from conftest import FIXTURES

EXIT_WITHIN = 10  # seconds
SLOW_WARM_UP = ("import threading, time\nfrom keepfold import semantic\n"
                "semantic.load_embedder = lambda download=False: time.sleep(120) "
                "if threading.current_thread().name == 'keepfold-embedder' else None\n")
SLOW_INDEX = ("import time\nfrom keepfold import mcp_server\n"
              "build = mcp_server.Keepfold._build_index\n"
              "mcp_server.Keepfold._build_index = lambda self: (time.sleep(120), build(self))\n")


def _start(tmp_path, patch=""):
    vault = tmp_path / "vault"
    shutil.copytree(FIXTURES / "vault_mini", vault)
    boot = patch + "from keepfold.taint import main\nmain()\n"
    return subprocess.Popen([sys.executable, "-c", boot, "mcp", "--vault", str(vault)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=tmp_path, env=dict(os.environ))


def _send(p, msg):
    p.stdin.write((json.dumps({"jsonrpc": "2.0", **msg}) + "\n").encode())
    p.stdin.flush()


def _handshake(p):
    _send(p, {"id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "eof-test", "version": "0"}}})
    assert json.loads(p.stdout.readline())["id"] == 1
    _send(p, {"method": "notifications/initialized"})  # starts the embedder warm-up


def _call(p, id=2):
    _send(p, {"id": id, "method": "tools/call",
              "params": {"name": "keepfold_find", "arguments": {"query": "pointers"}}})


def _closes_cleanly(p):
    t = time.monotonic()
    p.stdin.close()
    try:
        rc = p.wait(EXIT_WITHIN)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        pytest.fail(f"keepfold mcp still ran {EXIT_WITHIN} s after stdin closed")
    err = p.stderr.read().decode("utf-8", "replace")
    assert rc == 0, err
    return time.monotonic() - t


def test_exits_on_stdin_eof_after_initialize(tmp_path):
    p = _start(tmp_path)
    _handshake(p)
    _closes_cleanly(p)


def test_exits_while_the_warm_up_still_loads_the_model(tmp_path):
    p = _start(tmp_path, SLOW_WARM_UP)
    _handshake(p)
    _call(p)
    assert json.loads(p.stdout.readline())["id"] == 2
    _closes_cleanly(p)


def test_exits_while_a_call_waits_for_the_start_up_index(tmp_path):
    """The call waits for `_ready` (mcp_server._until_ready); in an AnyIO worker thread, not a
    daemon, that wait kept the process up to READY_WAIT (120 s) after EOF."""
    p = _start(tmp_path, SLOW_INDEX)
    _handshake(p)
    _call(p)
    time.sleep(0.5)  # the call is in flight, waiting for the index
    _closes_cleanly(p)
