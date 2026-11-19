"""browse_serve: the loopback receiver. Tmp vaults and random loopback ports only."""
import http.client
import json
import logging
import re
import shutil
import socket
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortex import browse_serve as bs
from cortex.browse import lists
from cortex.events import local_day

EXT = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
START = "2026-11-10T12:00:00Z"
DAY = local_day(datetime(2026, 11, 10, 12, tzinfo=UTC))


def rec(url="https://example.com/a", **kw):
    return {"url": url, "title": "A page", "start": START, "active_ms": 60_000,
            "excerpt": "Some text.", "kind": "page", **kw}


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    return v


@pytest.fixture
def srv(vault):
    token = bs.pair(vault)
    s = bs.make_server(vault, 0)
    threading.Thread(target=lambda: s.serve_forever(0.05), daemon=True).start()
    s.token = token
    yield s
    s.shutdown()
    s.server_close()


def call(s, method, path, body=None, *, token=None, headers=None, raw=None):
    c = http.client.HTTPConnection("127.0.0.1", s.server_address[1], timeout=5)
    h = {"Content-Type": "application/json"}
    if token is not False:
        h["Authorization"] = f"Bearer {token or s.token}"
    h.update(headers or {})
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    out = r.status, dict(r.getheaders()), r.read()
    c.close()
    return out


def inbox(vault, day=DAY):
    p = Path(vault) / ".cortex" / "browse" / f"inbox-{day}.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def test_pair_creates_and_rotates(vault):
    a = bs.pair(vault)
    path = vault / ".cortex" / "browse" / "token"
    assert path.read_text() == a and len(a) >= 43
    b = bs.pair(vault)
    assert b != a and path.read_text() == b


def test_binds_loopback_only(vault):
    for host in ("0.0.0.0", "192.168.1.5", "localhost", "::"):
        with pytest.raises(ValueError, match="127.0.0.1 only"):
            bs.make_server(vault, 0, host=host)
    s = bs.make_server(vault, 0)
    assert s.server_address[0] == "127.0.0.1"
    s.server_close()
    with pytest.raises(ValueError, match="pair"):  # no token yet
        bs.serve(vault, 0, stop=threading.Event())


def test_serve_runs_until_stopped(vault):
    token = bs.pair(vault)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    stop = threading.Event()
    t = threading.Thread(target=bs.serve, args=(vault, port), kwargs={"stop": stop})
    t.start()
    for _ in range(50):
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            c.request("GET", "/v1/ping", headers={"Authorization": f"Bearer {token}"})
            r = c.getresponse()
            break
        except ConnectionRefusedError:
            stop.wait(0.05)
    assert r.status == 200 and json.loads(r.read()) == {
        "ok": True, "deny_domains": []}
    c.close()
    stop.set()
    t.join(5)
    assert not t.is_alive()


def test_auth(srv, vault):
    assert call(srv, "GET", "/v1/ping")[0] == 200
    assert call(srv, "GET", "/v1/ping", token=False)[0] == 401
    assert call(srv, "GET", "/v1/ping", token="nope")[0] == 401
    assert call(srv, "GET", "/v1/ping", token=False,
                headers={"Authorization": f"Basic {srv.token}"})[0] == 401
    assert call(srv, "POST", "/v1/visit", rec(), token="nope")[0] == 401
    old = srv.token
    srv.token = bs.pair(vault)  # rotation takes effect at once
    assert call(srv, "GET", "/v1/ping", token=old)[0] == 401
    assert call(srv, "GET", "/v1/ping")[0] == 200
    assert inbox(vault) == []


def test_origin_and_host_rules(srv, vault):
    for bad in ("https://evil.example", "null", "http://127.0.0.1:1234", EXT + ".evil.com"):
        code, h, _ = call(srv, "POST", "/v1/visit", rec(), headers={"Origin": bad})
        assert code == 403 and "Access-Control-Allow-Origin" not in h
    code, h, _ = call(srv, "POST", "/v1/visit", rec(), headers={"Origin": EXT})
    assert code == 200 and h["Access-Control-Allow-Origin"] == EXT
    code, h, _ = call(srv, "GET", "/v1/ping")
    assert code == 200 and "Access-Control-Allow-Origin" not in h  # no CORS for *
    moz = "moz-extension://0b5e7f2a-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
    assert call(srv, "GET", "/v1/ping", headers={"Origin": moz})[0] == 200
    assert call(srv, "OPTIONS", "/v1/batch", token=False, headers={"Origin": EXT})[0] == 204
    assert call(srv, "OPTIONS", "/v1/batch", token=False,
                headers={"Origin": "https://evil.example"})[0] == 403
    # DNS rebinding: a page on evil.example resolved to 127.0.0.1 sends its own Host
    assert call(srv, "GET", "/v1/ping", headers={"Host": "evil.example:8799"})[0] == 403
    assert len(inbox(vault)) == 1


def test_body_cap_type_and_batch_cap(srv, vault):
    # a declared body over the cap is refused before it is read
    code, _, _ = call(srv, "POST", "/v1/batch", raw=b"",
                      headers={"Content-Length": str(bs.MAX_BODY + 1)})
    assert code == 413
    assert call(srv, "POST", "/v1/visit", rec(), headers={"Content-Type": "text/plain"})[0] == 415
    assert call(srv, "POST", "/v1/visit", raw=b"{not json")[0] == 400
    assert call(srv, "POST", "/v1/batch", [rec()] * (bs.MAX_BATCH + 1))[0] == 400
    assert call(srv, "POST", "/v1/batch", rec())[0] == 400  # an object, not a list
    assert call(srv, "POST", "/v1/nope", rec())[0] == 404
    assert inbox(vault) == []


def test_validation(srv, vault):
    for bad in (rec(kind="tab"), rec(active_ms=-1), rec(active_ms=True), rec(active_ms=1.5),
                rec(start="yesterday"), rec(start="2999-01-01T00:00:00Z"), rec(url=""),
                rec(url="x" * 5000), rec(title=7), {"url": "https://example.com/"}, []):
        assert call(srv, "POST", "/v1/visit", bad)[0] == 400, bad
    epoch = int(datetime(2026, 11, 10, 12, tzinfo=UTC).timestamp() * 1000)
    code, _, body = call(srv, "POST", "/v1/batch", [
        rec(active_ms=10**9, excerpt="x" * 5000, extra="ignored"),
        rec(start=epoch), rec(kind="nope")])
    assert code == 200 and json.loads(body) == {"ok": True, "stored": 2, "dropped": 0,
                                                "invalid": 1}
    a, b = inbox(vault)
    assert a["active_ms"] == bs.MAX_ACTIVE_MS and len(a["excerpt"]) == bs.MAX_EXCERPT
    assert "extra" not in a and a["start"] == b["start"] == "2026-11-10T12:00:00.000Z"


def test_filter_runs_before_storing(srv, vault, caplog):
    caplog.set_level(logging.DEBUG)
    batch = [
        rec("https://accounts.example.com/login?next=/x", title="Sign in SECRETLOGIN"),
        rec("https://example.com/reset?token=abc123", title="Reset SECRETTOKEN"),
        rec("http://192.168.1.1/admin"),
        rec("chrome://settings/"),
        rec("https://www.hdfcbank.com/personal"),
        rec("https://docs.python.org/3/library/http.html?utm_source=x#frag"),
        rec("https://www.google.com/search?q=fsrs+algorithm&sca_esv=1&ei=2",
            title="fsrs algorithm - Google Search", kind="search", excerpt="results text"),
        rec("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10", title="A talk - YouTube",
            kind="video", channel="  Some   Channel "),
        rec("https://www.webmd.com/a/b", title="Health page"),
        rec("https://www.pornhub.com/view", title="x"),
        rec("https://mail.google.com/mail/u/0/#inbox/abc", title="Subject line - Gmail"),
    ]
    code, _, body = call(srv, "POST", "/v1/batch", batch)
    assert json.loads(body) == {"ok": True, "stored": 5, "dropped": 6, "invalid": 0}
    got = {r["url"]: r for r in inbox(vault)}
    assert set(got) == {
        "https://docs.python.org/3/library/http.html",
        "https://www.google.com/search?q=fsrs%20algorithm",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.webmd.com/a/b",
        "https://mail.google.com/",
    }
    s = got["https://www.google.com/search?q=fsrs%20algorithm"]
    assert (s["kind"], s["title"], s["excerpt"]) == ("search", "fsrs algorithm", "")
    v = got["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]
    assert (v["kind"], v["title"], v["channel"]) == ("video", "A talk", "Some Channel")
    assert got["https://www.webmd.com/a/b"]["private"] is True
    assert got["https://www.webmd.com/a/b"]["excerpt"] == ""  # second review L2
    m = got["https://mail.google.com/"]
    assert (m["title"], m["excerpt"]) == ("mail.google.com", "")
    assert not any("private" in r for u, r in got.items() if "webmd" not in u)
    text = (vault / ".cortex" / "browse" / f"inbox-{DAY}.jsonl").read_text(encoding="utf-8")
    assert "SECRET" not in text and "abc123" not in text and "Subject" not in text
    logged = caplog.text
    assert "http" not in logged and "fsrs" not in logged and "A talk" not in logged


def test_user_deny_list_and_adult_private(srv, vault, home):
    (home / ".cortex").mkdir(exist_ok=True)
    (home / ".cortex" / "config.yaml").write_text(
        "browse:\n  deny_domains: [example.org]\n  adult: private\n", encoding="utf-8")
    code, _, body = call(srv, "POST", "/v1/batch", [
        rec("https://sub.example.org/x"), rec("https://www.pornhub.com/view")])
    assert json.loads(body)["stored"] == 1
    (r,) = inbox(vault)
    assert r["private"] is True and "pornhub" in r["url"]


def test_ping_hands_the_extension_the_deny_list_only(srv, home):
    """M4: the extension applies the config's deny_domains too; second review L3: the private
    list stays with the receiver."""
    (home / ".cortex").mkdir(exist_ok=True)
    (home / ".cortex" / "config.yaml").write_text(
        "browse:\n  deny_domains: [Diary.org]\n  private_domains: [clinic.in]\n",
        encoding="utf-8")
    code, _, body = call(srv, "GET", "/v1/ping")
    assert code == 200 and json.loads(body) == {
        "ok": True, "deny_domains": ["diary.org"]}
    assert call(srv, "GET", "/v1/ping", token="wrong")[0] == 401


def test_channel_is_judged_and_bank_titles_drop(srv, vault):
    """L3: a video's channel goes through the category and secret checks; M2: bank titles."""
    vid = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    batch = [rec(vid, title="A vlog", kind="video", channel="Herpes Support Group"),
             rec(vid, title="A vlog", kind="video", channel="api_key=abcdefghijklmnop"),
             rec("https://www.sbicard.com/en/personal/account-summary.page",
                 title="Account Summary | SBI Card")]
    code, _, body = call(srv, "POST", "/v1/batch", batch)
    assert json.loads(body)["stored"] == 1
    (r,) = inbox(vault)
    assert r["private"] is True and r["channel"] == "Herpes Support Group"


def test_deep_json_is_a_400_and_the_socket_times_out(srv):
    """L5: a nesting bomb is refused, not a crashed handler thread; reads time out."""
    deep = b"[" * 100_000 + b"]" * 100_000
    code, _, body = call(srv, "POST", "/v1/batch", raw=deep[:200_000])
    assert code in (400, 413)
    code, _, body = call(srv, "POST", "/v1/batch", raw=b"[" * 60_000 + b"]" * 60_000)
    assert code == 400 and json.loads(body)["error"] == "json"
    assert srv.RequestHandlerClass.timeout == 10


def test_jsonl_day_and_torn_line(srv, vault):
    late = "2026-11-12T23:30:00+00:00"
    call(srv, "POST", "/v1/visit", rec())
    call(srv, "POST", "/v1/visit", rec(start=late))
    day2 = local_day(datetime.fromisoformat(late))
    assert len(inbox(vault)) == 1 and len(inbox(vault, day2)) == 1
    p = vault / ".cortex" / "browse" / f"inbox-{DAY}.jsonl"
    with open(p, "ab") as f:
        f.write(b'{"torn":')
    call(srv, "POST", "/v1/visit", rec(title="after"))
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines[1] == '{"torn":' and json.loads(lines[2])["title"] == "after"


def test_two_clients_at_once(srv, vault):
    def client(n):
        for i in range(10):
            batch = [rec(f"https://example.com/{n}/{i}/{j}") for j in range(10)]
            assert call(srv, "POST", "/v1/batch", batch)[0] == 200

    ts = [threading.Thread(target=client, args=(n,)) for n in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    urls = [r["url"] for r in inbox(vault)]
    assert len(urls) == len(set(urls)) == 200


def test_extension_rules_mirror_python():
    """extension/lib.js RULES must hold the same lists as cortex.browse.lists."""
    src = (Path(__file__).parents[1] / "extension" / "lib.js").read_text(encoding="utf-8")
    rules = json.loads(re.search(r"/\*RULES\*/(.*?)/\*END\*/", src, re.S).group(1))
    want = {"authHosts": lists.AUTH_HOSTS, "authWords": lists.AUTH_WORDS,
            "payHosts": lists.PAY_HOSTS, "payWords": lists.PAY_WORDS,
            "bankHosts": lists.BANK_HOSTS, "webmailHosts": lists.WEBMAIL_HOSTS,
            "secretKeys": lists.SECRET_KEYS, "secretParts": set(lists.SECRET_PARTS),
            "bankWords": lists.BANK_WORDS, "bankTitles": set(lists.BANK_TITLES)}
    for key, value in want.items():
        assert set(rules[key]) == set(value), key
    assert rules["bankLabel"] == lists.BANK_LABEL


def test_extension_manifest():
    ext = Path(__file__).parents[1] / "extension"
    m = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
    assert m["manifest_version"] == 3 and m["incognito"] == "not_allowed"
    assert set(m["permissions"]) == {"tabs", "storage", "idle", "scripting", "alarms"}
    assert "content_security_policy" not in m  # the MV3 default
    for f in ext.glob("*.js"):  # no remote code, no URL other than the loopback receiver
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"https?://(?!127\.0\.0\.1)[a-z0-9]", text), f.name
        assert "eval(" not in text and "new Function" not in text, f.name
    assert not re.search(r"https?://", (ext / "popup.html").read_text(encoding="utf-8"))


# ---- second privacy review (17 Nov) --------------------------------------------------------

def test_r2_l2_a_private_record_is_stored_without_its_text(srv, vault, home):
    (home / ".cortex").mkdir(exist_ok=True)
    (home / ".cortex" / "config.yaml").write_text(
        "browse:\n  private_domains: [clinic.in]\n", encoding="utf-8")
    call(srv, "POST", "/v1/batch", [rec("https://clinic.in/a", excerpt="CLINICTEXT"),
                                    rec("https://e.com/a", title="HIV symptoms",
                                        excerpt="HIVTEXT"), rec(excerpt="kept text")])
    got = {r["url"]: r for r in inbox(vault)}
    assert got["https://clinic.in/a"]["excerpt"] == got["https://e.com/a"]["excerpt"] == ""
    assert got["https://example.com/a"]["excerpt"] == "kept text"


def test_r2_m3_a_lone_surrogate_is_replaced_and_a_batch_is_all_or_nothing(srv, vault):
    """p6: `document.title = "\\ud800"` reached the receiver, crashed `store` after one day's
    file was written, and the extension resent the batch forever (duplicates)."""
    good = json.dumps(rec(title="ok"))
    bad = json.dumps(rec(title="x\ud800", start="2026-11-12T10:00:00Z"))
    for _ in range(3):
        code, _, body = call(srv, "POST", "/v1/batch", raw=f"[{good},{bad}]".encode())
        assert code == 200 and json.loads(body)["stored"] == 2
    day2 = local_day(datetime(2026, 11, 12, 10, tzinfo=UTC))
    assert len(inbox(vault)) == 3 and [r["title"] for r in inbox(vault, day2)] == ["x?"] * 3
    assert bs.validate(rec(url="https://e.com/\ud800")) is None
    with pytest.raises(UnicodeEncodeError):  # a bad line anywhere: nothing is appended
        bs.store(bs.Vault(vault), [{**rec(), "start": "2026-11-13T10:00:00.000Z"},
                                   {**rec(), "title": "\ud800"}])
    assert inbox(vault, local_day(datetime(2026, 11, 13, 10, tzinfo=UTC))) == []
    assert len(inbox(vault)) == 3


# ---- processing at session end ---------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def runs(srv, monkeypatch):
    """The import replaced by a recorder; the processor on a hand-moved clock."""
    got = []
    monkeypatch.setattr(bs, "run_import", lambda v, **kw: got.append(v) or {})
    srv.processor.clock = Clock()
    return got


def test_session_end_runs_the_import_once_after_the_debounce(srv, vault, runs):
    p, clock = srv.processor, srv.processor.clock
    code, _, body = call(srv, "POST", "/v1/batch", {"records": [rec()], "session_end": True})
    assert code == 200 and json.loads(body)["stored"] == 1 and len(inbox(vault)) == 1
    assert not p.tick() and runs == []  # debounced
    clock.t += bs.DEBOUNCE_S - 1
    assert call(srv, "POST", "/v1/batch", {"records": [], "session_end": True})[0] == 200
    clock.t += bs.DEBOUNCE_S - 1
    assert not p.tick()  # the second end pushed the run back: still one run
    clock.t += 1
    assert p.tick() and len(runs) == 1 and runs[0].root == vault
    clock.t += 3 * bs.IDLE_PROCESS_S
    assert not p.tick() and len(runs) == 1  # nothing new, no second run
    for body in ([rec()], {"records": [rec()]}, {"records": [], "session_end": "yes"}):
        assert call(srv, "POST", "/v1/batch", body)[0] == 200  # no session end
    clock.t += bs.DEBOUNCE_S
    assert not p.tick() and len(runs) == 1
    assert call(srv, "POST", "/v1/batch", {"records": rec(), "session_end": True})[0] == 400


def test_records_left_20_min_without_a_batch_are_processed(srv, runs):
    p, clock = srv.processor, srv.processor.clock
    call(srv, "POST", "/v1/batch", [])  # nothing stored: no wait starts
    clock.t += 2 * bs.IDLE_PROCESS_S
    assert not p.tick()
    call(srv, "POST", "/v1/batch", [rec()])
    clock.t += bs.IDLE_PROCESS_S - 1
    call(srv, "POST", "/v1/batch", [])  # any batch restarts the wait
    clock.t += bs.IDLE_PROCESS_S - 1
    assert not p.tick() and runs == []
    clock.t += 1
    assert p.tick() and len(runs) == 1
    clock.t += 2 * bs.IDLE_PROCESS_S
    assert not p.tick() and len(runs) == 1


def test_one_run_at_a_time(srv, runs, monkeypatch):
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(bs, "run_import", lambda v, **kw: (runs.append(v), started.set(),
                                                     release.wait(5)))
    p, clock = srv.processor, srv.processor.clock
    p.batch(1, True)
    clock.t += bs.DEBOUNCE_S
    t = threading.Thread(target=p.tick)
    t.start()
    assert started.wait(5)
    p.batch(1, True)  # a session ends while the import runs
    clock.t += bs.DEBOUNCE_S
    assert not p.tick() and len(runs) == 1
    assert json.loads(call(srv, "GET", "/v1/status")[2])["processing"] is True
    release.set()
    t.join(5)
    assert p.tick() and len(runs) == 2  # then it gets its own run


def test_status_needs_the_token_and_counts(srv, vault):
    assert call(srv, "GET", "/v1/status", token=False)[0] == 401
    assert call(srv, "GET", "/v1/status", token="nope")[0] == 401
    assert call(srv, "GET", "/v1/status", headers={"Origin": "https://e.com"})[0] == 403
    call(srv, "POST", "/v1/batch", [rec(), rec("https://example.com/b")])
    code, _, body = call(srv, "GET", "/v1/status")
    assert code == 200 and json.loads(body) == {"ok": True, "queued_records": 2,
                                                "last_processed": None, "processing": False,
                                                "last_summary": ""}
    (vault / ".cortex" / "browse" / "state.json").write_text(
        '{"last_run": "2026-11-17T09:00:00Z"}', encoding="utf-8")
    assert json.loads(call(srv, "GET", "/v1/status")[2])["last_processed"] == \
        "2026-11-17T09:00:00Z"


def test_a_failed_run_logs_no_text(srv, monkeypatch, caplog):
    def boom(v, **kw):
        raise ValueError("https://secret.example/page Secret Title")

    monkeypatch.setattr(bs, "run_import", boom)
    p = srv.processor
    p.clock = Clock()
    p.batch(1, True)
    p.clock.t += bs.DEBOUNCE_S
    with caplog.at_level(logging.DEBUG, logger="cortex"):
        assert p.tick()
    assert "ValueError" in caplog.text and "secret" not in caplog.text.lower()
    assert p.processing is False


def test_run_import_is_the_whole_import_with_no_since(vault, home, monkeypatch):
    """Every browser found, no `since` (it would skip History days before it), no runner
    unless config enables the local LLM, and the embedder never downloaded."""
    from cortex import semantic
    from cortex.browse import build, sources

    got = {}
    monkeypatch.setattr(sources, "discover", lambda: ["a source"])
    monkeypatch.setattr(semantic, "load_embedder", lambda download=False: got.update(
        download=download))
    monkeypatch.setattr(build, "run", lambda v, srcs, settings, **kw: got.update(
        root=v.root, srcs=srcs, **kw) or {})
    bs.run_import(bs.Vault(vault))
    assert got["srcs"] == ["a source"] and got["root"] == vault and got["download"] is False
    assert got["runner"] is None and got["budget"] is None and "since" not in got


def test_session_end_writes_the_private_page_and_empties_the_inbox(srv, vault, home,
                                                                    monkeypatch):
    """End to end with the real import (no browser History, no embedder): the receiver's
    records become the private day page and the inbox file goes."""
    from cortex import semantic
    from cortex.browse import sources

    monkeypatch.setattr(sources, "discover", lambda: [])
    monkeypatch.setattr(semantic, "load_embedder", lambda download=False: None)
    p = srv.processor
    p.clock = Clock()
    call(srv, "POST", "/v1/batch", {"records": [rec(title="Fourier series notes")],
                                    "session_end": True})
    p.clock.t += bs.DEBOUNCE_S
    assert p.tick()
    page = vault / "browse" / "private" / f"browse-private-{DAY}.md"
    assert "Fourier series notes" in page.read_text(encoding="utf-8")
    assert inbox(vault) == []
    st = json.loads(call(srv, "GET", "/v1/status")[2])
    assert st["queued_records"] == 0 and st["last_processed"] and st["processing"] is False


def _two_found(vault, monkeypatch, read):
    from cortex.browse import sources
    monkeypatch.setattr(sources, "discover", lambda *a, **k: [
        sources.Source("chrome", "Default", vault / "a"),
        sources.Source("comet", "Default", vault / "b")])
    class Empty(sources.Since):  # records which source was opened; reads nothing
        def __enter__(self):
            read.append(self.src.key)
            return self

        def __exit__(self, *exc):
            pass

        def chunks(self, n=1000):
            return iter(())
    monkeypatch.setattr(sources, "Since", Empty)


def test_session_end_import_reads_only_the_sources_imported_before(srv, vault, home,
                                                                    monkeypatch):
    """No import the user did not click reads a browser or profile they never imported."""
    from cortex import semantic
    from cortex.browse import build
    read = []
    _two_found(vault, monkeypatch, read)
    monkeypatch.setattr(semantic, "load_embedder", lambda download=False: None)
    (vault / build.STATE).parent.mkdir(parents=True, exist_ok=True)
    (vault / build.STATE).write_text(json.dumps({"sources": {"comet/Default": {"cursor": 1}}}),
                                     encoding="utf-8")
    p = srv.processor
    p.clock = Clock()
    call(srv, "POST", "/v1/batch", {"records": [], "session_end": True})
    p.clock.t += bs.DEBOUNCE_S
    assert p.tick() and read == ["comet/Default"]


def test_session_end_with_none_imported_before_reads_no_history_but_the_inbox(
        srv, vault, home, monkeypatch):
    from cortex import semantic
    read = []
    _two_found(vault, monkeypatch, read)
    monkeypatch.setattr(semantic, "load_embedder", lambda download=False: None)
    p = srv.processor
    p.clock = Clock()
    call(srv, "POST", "/v1/batch", {"records": [rec(title="Fourier series notes")],
                                    "session_end": True})
    p.clock.t += bs.DEBOUNCE_S
    assert p.tick() and read == []
    page = vault / "browse" / "private" / f"browse-private-{DAY}.md"
    assert "Fourier series notes" in page.read_text(encoding="utf-8")
    assert inbox(vault) == []


def test_extension_js_parses_and_session_logic(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    ext = Path(__file__).parents[1] / "extension"
    for f in ext.glob("*.js"):
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, (f.name, r.stderr)
    lib = (ext / "lib.js").as_uri()
    script = tmp_path / "session.mjs"
    script.write_text(f"""
import assert from "node:assert/strict";
import {{ awaySince, sessionEndDue, batchBody, SESSION_IDLE_MS }} from "{lib}";
const now = 10_000_000;
assert.equal(SESSION_IDLE_MS, 15 * 60 * 1000);
assert.equal(awaySince(0, "idle", now), now - 60000);  // idle is reported after 60 s
assert.equal(awaySince(0, "locked", now), now);
assert.equal(awaySince(now - 60000, "locked", now + 5), now - 60000);  // idle, then locked
assert.equal(awaySince(now - 60000, "active", now), 0);
assert.equal(sessionEndDue(now - SESSION_IDLE_MS, true, now), true);
assert.equal(sessionEndDue(now - SESSION_IDLE_MS + 1, true, now), false);
assert.equal(sessionEndDue(now - SESSION_IDLE_MS, false, now), false);  // nothing waits
assert.equal(sessionEndDue(0, true, now), false);  // not away
assert.deepEqual(batchBody([1], false), [1]);
assert.deepEqual(batchBody([], true), {{ records: [], session_end: true }});
console.log("ok");
""", encoding="utf-8")
    r = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


def test_processing_prints_counts_only_progress(tmp_path, monkeypatch):
    lines = []
    p = bs.Processor(bs.Vault(tmp_path), clock=lambda: 100.0, say=lines.append)

    def fake(v, progress=None, srcs=None):
        progress("comet/Default", 12, 5, None)
        return {"days_from": "2026-11-01", "private_written": 3, "kept": 9, "private": 2,
                "dropped": 1, "model": True}

    monkeypatch.setattr(bs, "run_import", fake)
    p.batch(0, True)
    p.clock = lambda: 200.0
    assert p.tick()
    text = "\n".join(lines)
    assert "session end received" in text and "comet/Default: 12 visits read, 5 new" in text
    assert "3 private day pages written" in text and "local model used" in text
    assert p.status()["last_summary"] == lines[-1]


def test_second_server_on_same_port_refused(vault):
    # P5: Windows would otherwise let a second receiver share the port silently
    s = bs.make_server(vault, 0)
    try:
        with pytest.raises(OSError):
            bs.make_server(vault, s.server_address[1]).server_close()
    finally:
        s.server_close()


# ---- identity proof: the token goes only to a listener that holds it ----------------------


@pytest.fixture(params=["404", "wrong proof", "old receiver"])
def squatter(request):
    """Another program on the receiver's port (or an older Cortex receiver, whose /v1/hello
    asks for the token): it records every request it gets."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    got: list[str] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            n = int(self.headers.get("Content-Length") or 0)
            got.append(f"{self.requestline}\n{self.headers}\n"
                       + self.rfile.read(n).decode("utf-8", "replace"))
            body = json.dumps({"wrong proof": {"proof": "0" * 64},
                               "old receiver": {"ok": False, "error": "token"}}.get(
                                   request.param, {"ok": False})).encode()
            self.send_response({"404": 404, "old receiver": 401}.get(request.param, 200))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

    s = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=lambda: s.serve_forever(0.05), daemon=True).start()
    s.got, s.param = got, request.param
    yield s
    s.shutdown()
    s.server_close()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_hello_proves_the_token_without_asking_for_it(srv, vault):
    import hmac
    nonce = "ab" * 32
    code, _, body = call(srv, "GET", f"/v1/hello?n={nonce}", token=False)
    want = hmac.new(srv.token.encode(), nonce.encode(), "sha256").hexdigest()
    assert code == 200 and json.loads(body) == {"proof": want}
    assert srv.token.encode() not in body
    assert call(srv, "GET", "/v1/hello?n=short", token=False)[0] == 401  # not a nonce
    assert call(srv, "GET", f"/v1/hello?n={nonce}", token=False,
                headers={"Host": "evil.example"})[0] == 403
    assert call(srv, "GET", f"/v1/hello?n={nonce}", token=False,
                headers={"Origin": "https://e.com"})[0] == 403
    (vault / ".cortex" / "browse" / "token").unlink()
    assert call(srv, "GET", f"/v1/hello?n={nonce}", token=False)[0] == 503


def test_app_probe_never_sends_the_token_to_another_program(squatter, vault, monkeypatch):
    from cortex.app import status
    token = bs.pair(vault)
    monkeypatch.setattr(status, "RECEIVER_PORT", squatter.server_address[1])
    old = squatter.param == "old receiver"  # told apart: "restart it", not "another program"
    assert status.receiver_probe(vault) == ("old" if old else "busy", {})
    assert squatter.got and not any(token in g for g in squatter.got)
    assert not any("authorization" in g.lower() for g in squatter.got)


def test_app_probe_trusts_the_real_receiver(srv, vault, monkeypatch):
    from cortex.app import status
    call(srv, "POST", "/v1/batch", [rec()])
    monkeypatch.setattr(status, "RECEIVER_PORT", srv.server_address[1])
    state, live = status.receiver_probe(vault)
    assert state == "running" and live["queued_records"] == 1
    monkeypatch.setattr(status, "RECEIVER_PORT", _closed_port())
    assert status.receiver_probe(vault) == ("stopped", {})


def test_extension_proves_the_receiver_before_sending_the_token(srv, squatter, tmp_path):
    """extension/lib.js proveReceiver under node (WebCrypto HMAC, the same as Python's):
    true for the real receiver, false for another program (which never sees the token),
    null when nothing listens."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    lib = (Path(__file__).parents[1] / "extension" / "lib.js").as_uri()
    ports = [srv.server_address[1], squatter.server_address[1], _closed_port()]
    script = tmp_path / "prove.mjs"
    script.write_text(f"""
import {{ proveReceiver }} from "{lib}";
const token = {json.dumps(srv.token)};
const out = [];
for (const p of {json.dumps(ports)}) out.push(await proveReceiver(p, token));
console.log(JSON.stringify(out));
""", encoding="utf-8")
    r = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [True, "old" if squatter.param == "old receiver" else False,
                                    None]
    assert squatter.got and not any(srv.token in g for g in squatter.got)


def test_extension_proves_the_receiver_at_every_flush(tmp_path):
    """extension/background.js under node with a stub chrome and fetch: a list sync and a
    flush each ask /v1/hello first, never a cached proof, so a program that took the port
    after a good send gets neither the token nor the visits; the backoff still follows."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    bg = (Path(__file__).parents[1] / "extension" / "background.js").as_uri()
    script = tmp_path / "flush.mjs"
    script.write_text("""
import { createHmac } from "node:crypto";
const TOKEN = "k".repeat(43), listeners = {}, calls = [];
const ev = (name) => ({ addListener: (fn) => { listeners[name] = fn; } });
const area = (m) => ({
  get: async (k) => typeof k === "string" ? (k in m ? { [k]: m[k] } : {})
    : Object.fromEntries(Object.entries(k).map(([x, d]) => [x, x in m ? m[x] : d])),
  set: async (o) => { Object.assign(m, o); }, remove: async (k) => { delete m[k]; } });
const local = { token: TOKEN, queue: [] }, session = {};
globalThis.chrome = {
  storage: { local: area(local), session: area(session), onChanged: ev("changed") },
  tabs: { onActivated: ev("a"), onUpdated: ev("u"), onRemoved: ev("r") },
  windows: { onFocusChanged: ev("f"), onRemoved: ev("w"), WINDOW_ID_NONE: -1 },
  idle: { setDetectionInterval() {}, onStateChanged: ev("i") },
  runtime: { onStartup: ev("s"), onMessage: ev("msg") },
  alarms: { get: async () => ({}), create() {}, onAlarm: ev("alarm") } };
let squat = false;
const answer = (b) => ({ ok: true, status: 200, json: async () => b });
globalThis.fetch = async (url, o = {}) => {
  const u = new URL(url), auth = (o.headers || {}).Authorization;
  calls.push(u.pathname + (auth ? " +token" : ""));
  if (squat) return answer({ proof: "not the receiver" });  // answers anything, 200
  if (u.pathname === "/v1/hello")
    return answer({ proof: createHmac("sha256", TOKEN).update(u.searchParams.get("n")).digest("hex") });
  return answer({ deny_domains: [] });
};
await import(process.argv[2]);
const round = async () => {
  local.queue.push({ url: "https://a.example/", title: "A", start: Date.now(), active_ms: 1 });
  calls.length = 0;
  listeners.msg("flush");
  await new Promise((r) => setTimeout(r, 300));
  return { calls: [...calls], status: local.status, left: local.queue.length, backoff: local.backoff };
};
const out = [await round()];
squat = true;  // the receiver stopped; another program took the port
out.push(await round());
squat = false;
out.push(await round());
console.log(JSON.stringify(out));
""", encoding="utf-8")  # noqa: E501  (JavaScript)
    r = subprocess.run([node, str(script), bg], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    good = ["/v1/hello", "/v1/ping +token", "/v1/hello", "/v1/batch +token"]
    assert json.loads(r.stdout) == [
        {"calls": good, "status": "ok", "left": 0, "backoff": 0},
        {"calls": ["/v1/hello", "/v1/hello"], "status": "down", "left": 1, "backoff": 1},
        {"calls": good, "status": "ok", "left": 0, "backoff": 0}]

@pytest.mark.parametrize("which", ["browse", "app"])
def test_connections_over_the_cap_are_closed_at_once(vault, home, monkeypatch, which):
    """A local program cannot exhaust threads with idle connections (both servers)."""
    from cortex.app import server as app_server
    monkeypatch.setattr(bs, "MAX_CONNECTIONS", 4)
    s = bs.make_server(vault, 0) if which == "browse" else app_server.make_server(None)[0]
    threading.Thread(target=lambda: s.serve_forever(0.05), daemon=True).start()
    port = s.server_address[1]
    try:
        idle = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(4)]
        extra = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(3)]
        for x in extra:  # closed by the server without a byte sent
            try:
                assert x.recv(1) == b""
            except (ConnectionResetError, ConnectionAbortedError):
                pass
        idle[0].settimeout(0.3)
        with pytest.raises(TimeoutError):  # a connection under the cap stays open
            idle[0].recv(1)
        for x in idle + extra:
            x.close()
        status = None
        for _ in range(50):  # the handlers see EOF and give their slots back
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                c.request("GET", "/v1/ping" if which == "browse" else "/manage")
                status = c.getresponse().status
                break
            except (ConnectionResetError, ConnectionAbortedError, http.client.HTTPException):
                threading.Event().wait(0.1)
            finally:
                c.close()
        assert status in (200, 401)
    finally:
        s.shutdown()
        s.server_close()
def _last_run(vault, when: datetime) -> None:
    from cortex.browse import build
    p = vault / build.STATE  # what a finished import leaves
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"last_run": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "sources": {
        "comet/Default": {"cursor": 1}}}), encoding="utf-8")


def test_receiver_imports_by_itself_when_the_last_import_is_old(vault, home, monkeypatch):
    """browse.auto_import_hours: due by the last import's age, first checked a minute after
    start then every 10 minutes; never before a first import, never with 0, never while the
    browse-wide lock is held (a forget or deepen). Only the profiles imported before."""
    from cortex.browse import build, sites, sources
    runs, lines = [], []
    monkeypatch.setattr(sources, "discover", lambda *a, **k: [
        sources.Source("chrome", "Default", vault / "a"),
        sources.Source("comet", "Default", vault / "b")])
    monkeypatch.setattr(bs, "run_import", lambda v, progress=None, srcs=None: runs.append(
        [s.key for s in srcs]) or {})
    monkeypatch.setattr(bs, "run_deepen", lambda v, say=None: None)  # no local model
    now = datetime(2026, 11, 19, 12, tzinfo=UTC)
    t = [0.0]
    p = bs.Processor(bs.Vault(vault), clock=lambda: t[0], say=lines.append, now=lambda: now)
    t[0] = bs.AUTO_FIRST_S - 1
    _last_run(vault, now - timedelta(hours=7))
    assert not p.tick() and not runs  # not before the first check
    (vault / build.STATE).unlink()
    t[0] = bs.AUTO_FIRST_S
    assert not p.tick() and not runs  # never imported: the first import is the user's
    _last_run(vault, now - timedelta(hours=7))
    t[0] += 1
    assert not p.tick()  # the next check is 10 minutes later
    t[0] += bs.AUTO_EVERY_S
    with sites.busy(bs.Vault(vault)):  # a forget or deepen holds the lock: not now
        assert not p.tick() and not runs
    t[0] += bs.AUTO_EVERY_S
    last = json.loads((vault / build.STATE).read_text(encoding="utf-8"))
    (vault / build.STATE).write_text(json.dumps({**last, "sources": {}}), encoding="utf-8")
    assert not p.tick() and not runs  # no profile imported before: none is read unasked
    _last_run(vault, now - timedelta(hours=7))
    t[0] += bs.AUTO_EVERY_S
    C = ["comet/Default"]  # chrome is found but was never ticked: it stays unread
    assert p.tick() and runs == [C]
    assert any("last import over 6 hours ago" in x for x in lines)
    _last_run(vault, now - timedelta(hours=5))  # fresh enough
    t[0] += bs.AUTO_EVERY_S
    assert not p.tick() and runs == [C]
    (home / ".cortex").mkdir(exist_ok=True)
    cfg = home / ".cortex" / "config.yaml"
    cfg.write_text("browse:\n  auto_import_hours: 4\n", encoding="utf-8")
    t[0] += bs.AUTO_EVERY_S
    assert p.tick() and runs == [C, C]
    cfg.write_text("browse:\n  auto_import_hours: 0\n", encoding="utf-8")
    t[0] += bs.AUTO_EVERY_S
    assert not p.tick() and runs == [C, C]


def test_auto_import_hours_falls_back_on_bad_values():
    assert bs.auto_import_hours({}) == 6
    assert bs.auto_import_hours({"browse": {"auto_import_hours": 12}}) == 12
    assert bs.auto_import_hours({"browse": {"auto_import_hours": 0}}) == 0
    for bad in (-1, 169, "6", True, float("nan")):
        assert bs.auto_import_hours({"browse": {"auto_import_hours": bad}}) == 6


def test_a_failing_check_never_ends_the_receivers_loop(tmp_path, monkeypatch):
    import threading

    from cortex import browse_serve
    from cortex.vault import Vault
    (tmp_path / ".cortex").mkdir()
    proc = browse_serve.Processor(Vault(tmp_path))
    calls = []

    def tick():
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("a profile folder")
        stop.set()
        return False
    stop = threading.Event()
    monkeypatch.setattr(proc, "tick", tick)
    proc.loop(stop, every=0.01)
    assert len(calls) == 2  # the error was logged and the loop went on


def test_auto_sources_is_empty_when_the_browsers_cannot_be_listed(tmp_path, monkeypatch):
    from cortex import browse_serve
    from cortex.browse import sources
    from cortex.vault import Vault
    (tmp_path / ".cortex").mkdir()

    def denied(*a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(sources, "chosen", denied)
    assert browse_serve.auto_sources(Vault(tmp_path)) == []
