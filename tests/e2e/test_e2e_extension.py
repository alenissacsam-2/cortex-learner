"""E5 Extension receiver (docs/design/v6/TEST-PLAN.md §4): pairing, a filtered batch, a
session end that imports after the debounce, one receiver per port, and the extension's
rules in step with Python's. The receiver runs in-process on a free port; never 8799."""

import http.client
import json
import shutil
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cortex import browse_serve as bs
from cortex import semantic
from cortex.browse import lists, sources
from cortex.cli import app
from cortex.events import local_day

pytestmark = pytest.mark.usefixtures("home")

LIB = Path(__file__).parents[2] / "extension" / "lib.js"
START = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
DAY = local_day(datetime.fromisoformat(START.replace("Z", "+00:00")))


def rec(url, title="A page", kind="page", excerpt="Some text.", **kw):
    return {"url": url, "title": title, "start": START, "active_ms": 60_000,
            "excerpt": excerpt, "kind": kind, **kw}


def call(port, token, method, path, body=None, raw=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = raw if raw is not None else None if body is None else json.dumps(body).encode()
    try:
        c.request(method, path, body=data, headers={"Content-Type": "application/json",
                                                    "Authorization": f"Bearer {token}"})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")
    finally:
        c.close()


def inbox(vault):
    p = vault / ".cortex" / "browse" / f"inbox-{DAY}.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def pair(vault) -> str:
    r = CliRunner().invoke(app, ["browse", "pair", "--vault", str(vault)])
    assert r.exit_code == 0, r.output
    return r.output.strip()


def test_e5_extension_receiver(root, home, free_port, monkeypatch):
    (home / ".cortex").mkdir(exist_ok=True)
    (home / ".cortex" / "config.yaml").write_text(
        "browse:\n  deny_domains: [Diary.org]\n  private_domains: [clinic.in]\n"
        "  adult: private\n", encoding="utf-8")
    monkeypatch.setattr(sources, "discover", lambda: [])  # no real browser History
    monkeypatch.setattr(semantic, "load_embedder", lambda download=False: None)

    # 1. pair prints a token; a second pair rotates it and the old one gets 401
    old = pair(root)
    srv = bs.make_server(root, free_port)
    srv.processor.clock = clock = Clock()
    threading.Thread(target=lambda: srv.serve_forever(0.05), daemon=True).start()
    try:
        assert call(free_port, old, "GET", "/v1/ping")[0] == 200
        token = pair(root)
        assert token != old and len(token) >= 43
        assert call(free_port, old, "GET", "/v1/ping")[0] == 401
        assert call(free_port, token, "GET", "/v1/ping")[0] == 200

        # 2. a batch of visit, video and search records goes through the filter first
        batch = [
            rec("https://accounts.example.com/login", title="Sign in SECRETLOGIN"),
            rec("https://www.hdfcbank.com/personal", title="Bank SECRETBANK"),
            rec("https://example.com/reset?token=SECRETTOKEN", title="Reset"),
            rec("https://www.pornhub.com/view", title="ADULTMARKER", excerpt="ADULTTEXT"),
            rec("https://docs.python.org/3/library/heapq.html", title="heapq x\ud800"),
            rec("https://www.youtube.com/watch?v=dQw4w9WgXcQ", title="Graphs talk - YouTube",
                kind="video", channel="Some Channel"),
            rec("https://www.google.com/search?q=kruskal+algorithm", kind="search",
                title="kruskal algorithm - Google Search", excerpt="results"),
        ]
        raw = json.dumps(batch).encode()  # the lone surrogate travels as a \\ud800 escape
        code, out = call(free_port, token, "POST", "/v1/batch", raw=raw)
        assert code == 200 and out == {"ok": True, "stored": 4, "dropped": 3, "invalid": 0}
        got = {r["url"]: r for r in inbox(root)}
        assert set(got) == {"https://www.pornhub.com/view",
                            "https://docs.python.org/3/library/heapq.html",
                            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                            "https://www.google.com/search?q=kruskal%20algorithm"}
        adult = got["https://www.pornhub.com/view"]
        assert adult["private"] is True and adult["excerpt"] == ""
        assert got["https://docs.python.org/3/library/heapq.html"]["title"] == "heapq x?"
        assert got["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]["kind"] == "video"
        text = (root / ".cortex" / "browse" / f"inbox-{DAY}.jsonl").read_text(encoding="utf-8")
        assert not any(s in text for s in ("SECRETLOGIN", "SECRETBANK", "SECRETTOKEN",
                                           "ADULTTEXT"))
        deep = b"[" * 60_000 + b"]" * 60_000
        code, out = call(free_port, token, "POST", "/v1/batch", raw=deep)
        assert code == 400 and out["error"] == "json"

        # 3. a session end: one import after the 10 s debounce; the inbox is consumed
        code, out = call(free_port, token, "POST", "/v1/batch", {
            "records": [rec("https://example.com/fourier", title="Fourier series notes")],
            "session_end": True})
        assert code == 200 and out["stored"] == 1
        assert not srv.processor.tick()  # still inside the debounce
        clock.t += bs.DEBOUNCE_S
        assert srv.processor.tick()
        assert not srv.processor.tick()  # one run, not two
        assert inbox(root) == []
        page = root / "browse" / "private" / f"browse-private-{DAY}.md"
        assert "Fourier series notes" in page.read_text(encoding="utf-8")
        st = call(free_port, token, "GET", "/v1/status")[1]
        assert st["queued_records"] == 0 and st["last_processed"] and not st["processing"]

        # 4. a second receiver on the same port is refused; ping hands out the deny list only
        with pytest.raises(OSError):
            bs.make_server(root, free_port).server_close()
        assert call(free_port, token, "GET", "/v1/ping") == (
            200, {"ok": True, "deny_domains": ["diary.org"]})
    finally:
        srv.shutdown()
        srv.server_close()


# Denied by both filters, and kept by both (as page, search or video).
DENY = ["https://accounts.google.com/signin", "https://example.com/login",
        "https://www.hdfcbank.com/personal", "https://netbanking.example.in/home",
        "https://checkout.stripe.com/pay/x", "https://example.com/cb?token=abc",
        "https://example.com/x#access_token=abc", "http://192.168.1.1/admin",
        "https://user:pw@example.com/", "chrome://settings/"]
KEEP = ["https://docs.python.org/3/library/heapq.html",
        "https://www.google.com/search?q=kruskal", "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://en.wikipedia.org/wiki/Minimum_spanning_tree"]


def test_e5_extension_rules_equal_python_rules(tmp_path):
    """5. extension/lib.js, run under node: its RULES equal cortex.browse.lists key for key
    (a new list on either side fails), and its deny check agrees with the receiver's filter."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = tmp_path / "rules.mjs"
    script.write_text(
        f'import {{ RULES, denyReason }} from "{LIB.as_uri()}";\n'
        f"const urls = {json.dumps(DENY + KEEP)};\n"
        "console.log(JSON.stringify({rules: RULES, deny: urls.map((u) => denyReason(u))}));\n",
        encoding="utf-8")
    r = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    js = json.loads(r.stdout)
    want = {"authHosts": lists.AUTH_HOSTS, "authWords": lists.AUTH_WORDS,
            "payHosts": lists.PAY_HOSTS, "payWords": lists.PAY_WORDS,
            "bankHosts": lists.BANK_HOSTS, "webmailHosts": lists.WEBMAIL_HOSTS,
            "secretKeys": lists.SECRET_KEYS, "secretParts": lists.SECRET_PARTS,
            "bankWords": lists.BANK_WORDS, "bankTitles": lists.BANK_TITLES}
    assert set(js["rules"]) == {*want, "bankLabel"}
    for key, value in want.items():
        assert sorted(js["rules"][key]) == sorted(value), key
    assert js["rules"]["bankLabel"] == lists.BANK_LABEL

    settings = bs.load_settings()
    for url, reason in zip(DENY + KEEP, js["deny"], strict=True):
        clean = bs.validate(rec(url))
        kept = clean is not None and bs.filter_record(clean, settings) is not None
        assert (reason is None) == kept == (url in KEEP), (url, reason, kept)
