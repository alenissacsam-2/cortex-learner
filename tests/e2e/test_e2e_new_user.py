"""E1 New user (docs/design/v6/TEST-PLAN.md §4): init, doctor, the app's setup and a ChatGPT
import, a Chrome history import, ask, the dashboard, the MCP tools over stdio, then Quit. Every
process is a real `keepfold` subprocess on a fresh home; nothing private leaks to stdout or MCP."""

import http.client
import json
import re
import subprocess
import time
from datetime import UTC, datetime, timedelta

import anyio
import pytest
from integration.builders import chatgpt_zip, fake_profile_root
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from ruamel.yaml import YAML

from e2e.conftest import CLI
from keepfold import dashboard
from keepfold.vault import Vault

pytestmark = pytest.mark.slow

SECRETS = ("sk-chatgpttesttesttesttest99", "SECRETMARKER", "ADULTMARKER", "pornhub.com",
           "viewkey")
QUESTION = "what is a minimum spanning tree?"


def _history(now: datetime) -> list[tuple]:
    t = (now - timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
    return [
        (t, "https://en.wikipedia.org/wiki/Minimum_spanning_tree",
         "Minimum spanning tree - Wikipedia"),
        (t + timedelta(minutes=5), "https://www.google.com/search?q=kruskal+algorithm",
         "kruskal algorithm - Google Search", "kruskal algorithm"),
        (t + timedelta(minutes=10), "https://www.pornhub.com/view_video.php?viewkey=e2e",
         "ADULTMARKER video"),
        (t + timedelta(minutes=15), "https://example.com/cb?token=SECRETMARKER",
         "Callback SECRETMARKER"),
        (t + timedelta(minutes=20), "https://example.org/graphs/prim", "Prim algorithm notes"),
    ]


class Api:
    def __init__(self, port: int, token: str):
        self.port, self.token = port, token

    def call(self, method: str, path: str, body=None, raw: bytes | None = None,
             headers: dict | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"X-Keepfold-Token": self.token, **(headers or {})}
        data = raw
        if body is not None:
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
        try:
            conn.request(method, path, body=data, headers=h)
            r = conn.getresponse()
            return r.status, json.loads(r.read() or b"{}")
        finally:
            conn.close()

    def job(self, method: str, path: str, body: dict, timeout: float = 60) -> dict:
        """Start a job and wait until it ends; its view (result or error)."""
        code, started = self.call(method, path, body)
        assert code == 200, (path, started)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, j = self.call("GET", "/api/job")
            if j["seq"] == started["seq"] and not j["job"]["running"]:
                return j["job"]
            time.sleep(0.2)
        raise AssertionError(f"{path} did not finish in {timeout} s")


def _wait_for_url(log, proc: subprocess.Popen, timeout: float = 60) -> tuple[int, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # T1: from 20:00 to 04:00 the start link is /manage?t=…#tonight
        if m := re.search(r"http://127\.0\.0\.1:(\d+)/(?:manage)?\?t=([\w-]+)",
                          log.read_text(encoding="utf-8", errors="replace")):
            return int(m.group(1)), m.group(2)
        assert proc.poll() is None, log.read_text(encoding="utf-8", errors="replace")
        time.sleep(0.2)
    raise AssertionError("the app printed no URL")


def _set_config(home, **keys) -> None:
    p = home / ".keepfold" / "config.yaml"
    yaml = YAML(typ="safe")
    data = yaml.load(p.read_text(encoding="utf-8")) or {}
    data.update(keys)
    with p.open("w", encoding="utf-8") as f:
        yaml.dump(data, f)


async def _mcp(env, vault) -> dict:
    """list_tools, then keepfold_find, keepfold_recall and keepfold_read over stdio."""
    params = StdioServerParameters(command=CLI[0], args=[*CLI[1:], "mcp", "--vault", str(vault)],
                                   env=env, cwd=str(vault.parent))
    out = {}
    async with Client(params) as client:
        out["tools"] = {t.name for t in (await client.list_tools()).tools}
        for name, args in (("keepfold_find", {"query": "spanning tree"}),
                           ("keepfold_find", {"query": "video callback token key"}),
                           ("keepfold_recall", {"query": "minimum spanning tree"}),
                           ("keepfold_recall", {"query": "adult video callback token Dijkstra"})):
            r = await client.call_tool(name, args)
            assert not r.is_error, r.content
            out.setdefault(name, []).append(r)
        hits = out["keepfold_find"][0].structured_content["hits"]
        assert hits, out["keepfold_find"][0].structured_content
        r = await client.call_tool("keepfold_read", {"id": hits[0]["id"]})
        assert not r.is_error, r.content
        out["keepfold_read"] = [r]
    return out


def _text(r) -> str:
    return json.dumps(r.structured_content) + "".join(getattr(c, "text", "") for c in r.content)


def test_e1_new_user(tmp_path, env, run_cli, fake_ollama):
    home, vault = tmp_path / "home", tmp_path / "vault"
    # 1. init: folders, a git repo, and the pointer at the vault
    r = run_cli("init", vault)
    assert r.returncode == 0, r.stderr
    assert (vault / ".git").is_dir() and (vault / ".keepfold").is_dir() and (vault / "me").is_dir()
    cfg = YAML(typ="safe").load((home / ".keepfold" / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["vault"] == str(vault.resolve())

    # 2. doctor: no model and no `keepfold` on PATH are warnings, never a failure
    r = run_cli("doctor")
    assert r.returncode == 0, r.stdout + r.stderr
    assert re.search(r"^WARN\s+Search model:", r.stdout, re.M), r.stdout
    assert re.search(r"^WARN\s+`keepfold` on PATH:", r.stdout, re.M), r.stdout
    assert not re.search(r"^FAIL", r.stdout, re.M), r.stdout

    # 3. the app in the background; its URL carries the token
    _set_config(home, localllm={"url": fake_ollama.url})
    log = tmp_path / "app.log"
    with log.open("w", encoding="utf-8") as f:
        app = subprocess.Popen([*CLI, "app", "--no-open"], env=env, cwd=tmp_path, stdout=f,
                               stderr=subprocess.STDOUT)
    try:
        port, token = _wait_for_url(log, app)
        api = Api(port, token)
        assert Api(port, "wrong").call("GET", "/api/status")[0] == 401

        # 4. status, the vault and runner steps, then a ChatGPT import: upload, preview, run
        code, st = api.call("GET", "/api/status")
        assert code == 200 and st["first_run"] is False and st["vault"] == str(vault.resolve())
        job = api.job("POST", "/api/setup/vault", {"path": str(vault)})
        assert job["error"] is None and job["result"]["ok"], job
        _, setup = api.call("GET", "/api/setup")
        assert setup["vault"]["done"] and setup["runner"]["ollama"]["serving"], setup["runner"]
        job = api.job("POST", "/api/setup/test", {"model": "2b", "runner": "ollama"})
        assert job["error"] is None and job["result"]["ok"], job
        _, setup = api.call("GET", "/api/setup")
        assert setup["test"] == {"done": True, "model": "qwen3.5:2b", "runner": "ollama"}

        data = chatgpt_zip(tmp_path / "exports").read_bytes()
        code, up = api.call("POST", "/api/import/upload", raw=data, headers={
            "Content-Type": "application/octet-stream", "X-Keepfold-Name": "export.zip"})
        assert code == 200 and up["size"] == len(data), up
        assert len(list((home / ".keepfold" / "tmp").glob("upload-*"))) == 1
        job = api.job("POST", "/api/import/preview", {"source": "chatgpt",
                                                      "upload": up["upload"]})
        assert job["error"] is None and job["result"]["kind"] == "import-preview", job
        found = job["result"]["found"]
        assert found >= 1 and job["result"]["already"] == 0
        job = api.job("POST", "/api/import/run", {"source": "chatgpt", "upload": up["upload"]})
        assert job["error"] is None and job["result"]["counts"]["conversations"] == found, job
        assert len(list((home / ".keepfold" / "claude-chat" / "chatgpt").glob("*.jsonl"))) == found

        # 5. a Chrome history import from a synthetic profile: counts only on stdout
        fake_profile_root(home / "AppData" / "Local", {"chrome": _history(datetime.now(UTC))})
        r = run_cli("browse", "import", "--browser", "chrome")
        assert r.returncode == 0, r.stdout + r.stderr
        assert re.search(r"visits from \S+: kept \d+, private \d+, dropped", r.stdout), r.stdout
        out = r.stdout + r.stderr
        for row in _history(datetime.now(UTC)):
            assert row[2] not in out and row[1] not in out, row
        assert not any(s in out for s in SECRETS), out
        assert list((vault / "browse" / "private").rglob("*.md"))

        # 6. ask, with the local model: the answer cites a path; the passages went as data
        (vault / "notes" / "minimum-spanning-tree.md").write_text(  # as written in Obsidian
            "# Minimum spanning tree\n\nA minimum spanning tree joins every vertex of a "
            "weighted graph at the least total edge weight; Kruskal and Prim build one.\n",
            encoding="utf-8")
        r = run_cli("ask", QUESTION)
        assert r.returncode == 0, r.stdout + r.stderr
        assert re.search(r"^\[1\] \S+\.md$", r.stdout, re.M), r.stdout
        assert "answered by ollama qwen3.5:2b" in r.stdout
        asked = [b for b in fake_ollama.chats() if QUESTION in b["messages"][-1]["content"]]
        assert asked and "<untrusted_note" in asked[-1]["messages"][-1]["content"]

        # 7. the dashboard: index.md on demand, no page in the vault; a snapshot keeps its CSP
        r = run_cli("dashboard", "--no-open")
        assert r.returncode == 0, r.stdout + r.stderr
        assert (vault / "index.md").is_file()
        assert not (vault / ".keepfold" / "dashboard.html").exists()
        html = dashboard.snapshot(Vault(vault), now=datetime.now(UTC))
        assert "default-src 'none'" in html

        # 8. the MCP tools over stdio: nothing private, no secret
        got = anyio.run(_mcp, env, vault)
        assert {"keepfold_find", "keepfold_recall", "keepfold_read"} <= got["tools"]
        assert "spanning" in _text(got["keepfold_find"][0]).lower()
        for name in ("keepfold_find", "keepfold_recall", "keepfold_read"):
            for res in got[name]:
                text = _text(res)
                assert not any(s in text for s in SECRETS), (name, text)

        # 9. Quit: the process ends within 5 s and no upload is left behind
        assert api.call("POST", "/api/quit", {})[0] == 200
        assert app.wait(timeout=5) == 0
        assert not list((home / ".keepfold" / "tmp").glob("upload-*"))
    finally:
        if app.poll() is None:
            app.kill()
            app.wait()
