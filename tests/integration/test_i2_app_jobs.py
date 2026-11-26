"""I2 app API <-> jobs <-> vault (docs/design/v6/TEST-PLAN.md §3): the real server on a free
loopback port, a urllib client with the token header, synthetic exports and a temp home. Auth,
GET changes nothing, the upload lifecycle (upload, preview counts only, run, upload deleted),
one job at a time, cancel and Quit leave no upload, and no response carries a title or URL."""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from integration.builders import EXPORTS, chatgpt_zip
from keepfold import chats, localllm
from keepfold.app import actions, server, targets

pytestmark = pytest.mark.usefixtures("home")
# what the synthetic exports hold that the API must never echo: titles, URLs, a secret, text
LEAKS = ("Graph revision", "Explain Dijkstra", "sk-chatgpttesttesttesttest99", "Dijkstra in 10",
         "youtube.com", "wikipedia.org", "Shortest path", "dijkstra algorithm", "Graph Channel")


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    """No real runner, `claude` CLI, opener or process."""
    def no_runner(*a, **k):
        raise OSError("no runner in tests")

    def no_proc(*a, **k):
        raise AssertionError("a test must not start a process")

    monkeypatch.setattr(localllm, "_request", no_runner)
    monkeypatch.setattr(localllm, "_lms_exe", lambda cfg: None)
    monkeypatch.setattr(targets, "_which", lambda c: None)
    monkeypatch.setattr(actions, "_popen", no_proc)
    monkeypatch.setattr(server, "_open", lambda url: None)
    monkeypatch.setattr(server, "PORT", 0)  # never the owner's app port


@pytest.fixture
def vault(root_committed):
    (root_committed / ".keepfold").mkdir(exist_ok=True)
    return root_committed


@pytest.fixture
def app(vault):
    srv, a = server.make_server(vault)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    yield a
    srv.shutdown()
    srv.server_close()


class Client:
    """urllib with the token header; every response body is kept for the leak check."""

    def __init__(self, app):
        self.app, self.seen = app, []

    def call(self, method, path, body=None, *, raw=None, token=True, headers=None):
        h = dict(headers or {})
        if token:
            h["X-Keepfold-Token"] = self.app.token if token is True else token
        data = raw
        if body is not None:
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
        req = urllib.request.Request(f"http://127.0.0.1:{self.app.port}{path}", data=data,
                                     headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                code, text = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, text = e.code, e.read()
        self.seen.append(text)
        return code, json.loads(text) if text.startswith(b"{") else None

    def upload(self, data: bytes, name: str):
        return self.call("POST", "/api/import/upload", raw=data, headers={
            "Content-Type": "application/octet-stream", "X-Keepfold-Name": name})

    def wait(self, timeout=30) -> dict:
        ev = threading.Event()
        for _ in range(int(timeout / 0.05)):
            _, j = self.call("GET", "/api/job")
            if j["job"] and not j["job"]["running"]:
                return j["job"]
            ev.wait(0.05)
        raise AssertionError("job did not finish")


def _uploads(home: Path) -> list[Path]:
    return list((home / ".keepfold" / "tmp").glob("upload-*"))


def _tree(*dirs: Path) -> dict:
    return {str(p): (p.read_bytes() if p.is_file() else None)
            for d in dirs for p in sorted(d.rglob("*"))}


def test_auth_and_get_changes_nothing(app, vault, home):
    c = Client(app)
    assert c.call("GET", "/api/status", token=False)[0] == 401
    assert c.call("GET", "/api/status", token="wrong")[0] == 401
    assert c.call("GET", "/api/status", headers={"Host": "evil.example"})[0] == 403
    assert c.call("GET", "/api/status", headers={"Origin": "http://evil.example"})[0] == 403
    assert c.call("POST", "/api/quit", {}, headers={"Origin": "http://evil.example"})[0] == 403
    assert not app.stop.is_set()
    before = _tree(vault, home)
    for path in app.get:  # a site's details or a passage with none named: not found
        assert c.call("GET", path)[0] == (
            404 if path in ("/api/home/site", "/api/chat/passage") else 200), path
    for path in set(app.post) - set(app.get):  # a GET on a change route is refused, not run
        assert c.call("GET", path)[0] == 405, path
    after = _tree(vault, home)
    # /api/home lays the brain out once and keeps it there: a view cache, as the dashboard's
    after.pop(str(vault / ".keepfold" / "brain-layout.json"), None)
    assert after == before


def test_upload_preview_run_lifecycle(app, home, tmp_path):
    c = Client(app)
    data = chatgpt_zip(tmp_path / "exports").read_bytes()
    code, up = c.upload(data, "export.zip")
    assert code == 200 and up["size"] == len(data)
    assert [p.name for p in _uploads(home)] == [f"upload-{up['upload']}-export.zip"]

    assert c.call("POST", "/api/import/preview", {"source": "chatgpt",
                                                   "upload": up["upload"]})[0] == 200
    job = c.wait()
    r = job["result"]
    assert job["error"] is None and r["kind"] == "import-preview" and r["found"] >= 1
    assert set(r) <= {"ok", "kind", "found", "already", "detected", "lines", "note"}
    out = chats.export_root() / "chatgpt"
    assert not out.exists() and len(_uploads(home)) == 1  # a dry run keeps the upload

    c.call("POST", "/api/import/run", {"source": "chatgpt", "upload": up["upload"]})
    job = c.wait()
    assert job["error"] is None and job["result"]["counts"]["conversations"] == r["found"]
    pages = list(out.glob("*.jsonl"))
    assert len(pages) == r["found"]
    assert all("sk-chatgpttesttesttesttest99" not in p.read_text(encoding="utf-8")
               for p in pages)
    assert not _uploads(home) and up["upload"] not in app.uploads
    # the upload is gone, so a second run is refused, not re-imported
    assert c.call("POST", "/api/import/run", {"source": "chatgpt",
                                               "upload": up["upload"]})[0] == 400

    # a Google activity preview: counts only, nothing of the activity itself
    c.call("POST", "/api/import/preview", {"source": "google-activity",
                                            "path": str(EXPORTS / "takeout_MyActivity.json")})
    job = c.wait()
    assert job["error"] is None and job["result"]["found"] >= 1, job
    c.call("GET", "/api/setup")
    c.call("GET", "/api/status")
    c.call("GET", "/api/browse")
    body = b"".join(c.seen).decode("utf-8")
    for leak in LEAKS:
        assert leak not in body, leak


def test_second_job_refused_and_cancel_leaves_no_upload(app, home, monkeypatch):
    c = Client(app)
    started, release = threading.Event(), threading.Event()

    def slow_run(job, source, path, root=None):
        started.set()
        while not job.cancelled.is_set() and not release.is_set():
            job.cancelled.wait(0.02)
        return {"ok": False, "kind": "import", "cancelled": job.cancelled.is_set(), "lines": []}

    monkeypatch.setattr(actions, "import_run", slow_run)
    _, up = c.upload(b"[]", "MyActivity.json")
    assert len(_uploads(home)) == 1
    assert c.call("POST", "/api/import/run", {"source": "google-activity",
                                               "upload": up["upload"]})[0] == 200
    assert started.wait(10)
    code, busy = c.call("POST", "/api/vault/doctor", {})
    assert code == 409 and "still running" in busy["error"]
    _, other = c.upload(b"[]", "conversations.json")
    assert c.call("POST", "/api/import/preview", {"source": "chatgpt",
                                                   "upload": other["upload"]})[0] == 409
    assert c.call("POST", "/api/job/cancel", {})[0] == 200
    job = c.wait()
    assert job["result"]["cancelled"] is True and job["cancellable"] is True
    assert [p.name for p in _uploads(home)] == [f"upload-{other['upload']}-conversations.json"]
    release.set()


def test_quit_deletes_uploads_never_imported(vault, home, monkeypatch):
    from test_app import daytime
    daytime(monkeypatch)  # by night the link ends in #tonight
    lines, done = [], threading.Event()

    def serve():
        try:
            server.run(vault, open_browser=False, echo=lines.append)
        finally:
            done.set()

    threading.Thread(target=serve, daemon=True).start()
    for _ in range(200):
        if lines:
            break
        done.wait(0.05)
    url = lines[0].split(": ", 1)[1]
    port, token = int(url.split(":")[2].split("/")[0]), url.rsplit("=", 1)[1]
    c = Client(type("A", (), {"port": port, "token": token})())
    assert c.upload(b"[]", "conversations.json")[0] == 200
    assert len(_uploads(home)) == 1
    assert c.call("POST", "/api/quit", {})[0] == 200
    assert done.wait(15)
    assert not _uploads(home)
