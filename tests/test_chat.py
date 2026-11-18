"""The local chat: `localllm.Runner.stream` against fake streaming runners (Ollama NDJSON,
OpenAI-compatible SSE), `chat.when`, retrieval with a date range, history trimming, storage
under .cortex/browse/chat/, and `chat.reply` end to end. Loopback fakes only; no model."""

import http.client
import json
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import FakeEmbed, note

from cortex import ask, chat, cloudllm, localllm, semantic
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")
TODAY = date(2026, 11, 18)


class Stream:
    """A loopback runner that streams `pieces` (and `think` reasoning chunks first) in its
    own format, `delay` seconds apart, and records each request body."""

    def __init__(self, kind="ollama", pieces=("Hello",), think=0, delay=0.0, models=()):
        self.kind, self.pieces, self.think, self.delay = kind, list(pieces), think, delay
        self.bodies, self.sent = [], 0
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                data = json.dumps({"models": [{"name": m} for m in models]} if kind == "ollama"
                                  else {"data": [{"id": m} for m in models]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                fake.bodies.append(json.loads(self.rfile.read(n)))
                self.send_response(200)
                self.end_headers()
                for line in fake.lines():
                    try:
                        self.wfile.write(line)
                        self.wfile.flush()
                    except OSError:
                        return
                    fake.sent += 1
                    time.sleep(fake.delay)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=lambda: self.srv.serve_forever(0.05), daemon=True).start()

    def lines(self):
        if self.kind == "ollama":
            for p in self.pieces:
                yield json.dumps({"message": {"role": "assistant", "content": p},
                                  "done": False}).encode() + b"\n"
            yield b'{"message":{"content":""},"done":true}\n'
            return
        for _ in range(self.think):
            yield b'data: {"choices":[{"delta":{"reasoning_content":"hmm"}}]}\n\n'
        for p in self.pieces:
            yield b"data: " + json.dumps({"choices": [{"delta": {"content": p}}]}).encode() \
                + b"\n\n"
        yield b"data: [DONE]\n\n"

    def runner(self, model="gemma3:2b"):
        return localllm.Runner(self.kind, self.base, model)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def stream():
    made = []

    def make(*a, **k):
        made.append(Stream(*a, **k))
        return made[-1]

    yield make
    for s in made:
        s.close()


MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]


# ---- Runner.stream ---------------------------------------------------------------------------

def test_ollama_streams_ndjson_without_thinking(stream):
    s = stream("ollama", ["Hel", "lo", " there"])
    assert list(s.runner().stream(MSGS, max_tokens=50, temperature=0.3)) == ["Hel", "lo", " there"]
    body = s.bodies[0]
    assert body["stream"] is True and body["think"] is False
    assert body["options"] == {"temperature": 0.3, "num_predict": 50}


def test_openai_qwen_trick_and_reasoning_dropped(stream):
    s = stream("openai", ["The answer", " [1]."], think=3)
    seen = []
    got = list(s.runner("qwen3.5-2b").stream(MSGS, on_thinking=lambda: seen.append(1)))
    assert got == ["The answer", " [1]."] and len(seen) == 3
    msgs = s.bodies[0]["messages"]
    assert msgs[1]["content"] == "hi /no_think"  # the last user message
    assert msgs[-1] == {"role": "assistant", "content": "<think>\n\n</think>\n\n"}
    assert s.bodies[0]["stream"] is True and MSGS[1]["content"] == "hi"  # caller's list intact


def test_openai_without_qwen_has_no_trick(stream):
    s = stream("openai", ["ok"])
    list(s.runner("llama-3b").stream(MSGS))
    assert s.bodies[0]["messages"] == MSGS


@pytest.mark.parametrize("pieces", [
    ["<think>plan</think>Answer"], ["<thi", "nk>pl", "an</th", "ink>Ans", "wer"],
    ["  <THINK>x", "</think>\n\nAnswer"], ["<", "think", ">", "a", "<", "/think", ">", "Answer"]])
def test_think_tags_are_stripped_across_chunks(stream, pieces):
    s = stream("ollama", pieces)
    assert "".join(s.runner().stream(MSGS)) == "Answer"


def test_a_lone_angle_bracket_still_arrives(stream):
    s = stream("ollama", ["a <", "b and <th", "e end"])
    assert "".join(s.runner().stream(MSGS)) == "a <b and <the end"


def test_cancel_stops_and_closes(stream):
    s = stream("ollama", [f"w{i} " for i in range(40)], delay=0.05)
    cancel = threading.Event()
    got = []
    for piece in s.runner().stream(MSGS, cancel=cancel):
        got.append(piece)
        if len(got) == 2:
            cancel.set()
    assert len(got) == 2
    time.sleep(0.3)
    assert s.sent < 40  # the server saw the connection go


@pytest.mark.parametrize("base", ["http://10.0.0.5:11434", "https://api.example.com",
                                  "http://127.0.0.1.example.com:1234"])
def test_stream_refuses_a_non_loopback_runner(base, monkeypatch):
    monkeypatch.setattr(localllm, "_OPENER", None)  # never reached
    with pytest.raises(ValueError, match="loopback"):
        list(localllm.Runner("openai", base, "m").stream(MSGS))


def test_a_cut_stream_is_an_oserror(monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield b'{"message":{"content":"part"},"done":false}\n'
            raise http.client.IncompleteRead(b"")

    monkeypatch.setattr(localllm._OPENER, "open", lambda req, timeout: Resp())
    it = localllm.Runner("ollama", "http://127.0.0.1:11434", "m").stream(MSGS)
    assert next(it) == "part"
    with pytest.raises(OSError, match="broke off"):
        next(it)


def test_chat_and_probe_survive_http_client_errors(monkeypatch):
    """C8: IncompleteRead/BadStatusLine are not OSError; they must read as no answer."""
    def broken(*a, **k):
        raise http.client.BadStatusLine("x")

    monkeypatch.setattr(localllm, "_request", broken)
    r = localllm.Runner("ollama", "http://127.0.0.1:11434", "m")
    assert r.chat("s", "u") is None
    assert localllm._probe("ollama", "http://127.0.0.1:11434", "/api/tags", None) is None


# ---- choosing a model (C7) and lms only when needed (C15) -------------------------------------

def test_pick_never_takes_an_embedding_model():
    names = ["qwen3-embedding:0.6b", "embeddinggemma:300m", "nomic-embed-text:latest",
             "bge-m3", "qwen3.5:2b", "llama3:8b"]
    assert localllm._pick(names, None) == "qwen3.5:2b"
    assert localllm._pick(["nomic-embed-text", "llama3:8b"], None) == "llama3:8b"
    assert localllm._pick(["nomic-embed-text"], None) is None


def test_ollama_capabilities_decide(stream, monkeypatch):
    real = localllm._request

    def fake(url, body, timeout):
        if url.endswith("/api/show"):
            return {"capabilities": ["embedding"] if body["model"] == "tiny:1b"
                    else ["completion"]}
        return real(url, body, timeout)

    s = stream("ollama", models=["tiny:1b", "chat:3b"])
    monkeypatch.setattr(localllm, "_request", fake)
    monkeypatch.setattr(localllm, "_PROBES", [("ollama", s.base, "/api/tags")])
    assert localllm.find({}).model == "chat:3b"


def test_lms_is_not_asked_when_ollama_answers(stream, monkeypatch):
    s = stream("ollama", models=["qwen3.5:2b"])
    monkeypatch.setattr(localllm, "_PROBES", [("ollama", s.base, "/api/tags")])
    monkeypatch.setattr(localllm, "ensure_lmstudio",
                        lambda *a, **k: pytest.fail("lms asked though Ollama answered"))
    assert localllm.find({"localllm": {"enabled": True}}).model == "qwen3.5:2b"


# ---- when ------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, lo, hi", [
    ("what did I read today", "2026-11-18", "2026-11-18"),
    ("which sites did I visit yesterday?", "2026-11-17", "2026-11-17"),
    ("the day before yesterday", "2026-11-16", "2026-11-16"),
    ("which site about X did I visit 3 days ago?", "2026-11-14", "2026-11-16"),
    ("a few days ago", "2026-11-14", "2026-11-16"),
    ("two weeks ago", "2026-11-01", "2026-11-07"),
    ("a month ago", "2026-10-12", "2026-10-26"),
    ("this week", "2026-11-16", "2026-11-18"),
    ("what did I read last week about exams?", "2026-11-09", "2026-11-15"),
    ("this month", "2026-11-01", "2026-11-18"),
    ("last month", "2026-10-01", "2026-10-31"),
    ("on 12 Nov", "2026-11-12", "2026-11-12"),
    ("Nov 12", "2026-11-12", "2026-11-12"),
    ("on 2026-11-12", "2026-11-12", "2026-11-12"),
    ("12th of November 2025", "2025-11-12", "2025-11-12"),
    ("on 3 Dec", "2025-12-03", "2025-12-03"),  # no year, after today: last year's
    ("in October", "2026-10-01", "2026-10-31"),
    ("in December", "2025-12-01", "2025-12-31"),
    ("since 1 Nov", "2026-11-01", "2026-11-18"),
    ("since 2026-11-10", "2026-11-10", "2026-11-18"),
    ("in the last 3 days", "2026-11-15", "2026-11-18"),
    ("past week", "2026-11-11", "2026-11-18"),
])
def test_when(text, lo, hi):
    got = chat.when(text, TODAY)
    assert got and (got[0].isoformat(), got[1].isoformat()) == (lo, hi)


@pytest.mark.parametrize("text", ["what did I learn about Dijkstra?", "31 Feb", "",
                                  "and its running time?", "may I ask about heaps"])
def test_when_none(text):
    assert chat.when(text, TODAY) is None


def test_topic_drops_the_date_and_generic_words():
    for text, want in (("Which sites did I visit yesterday?", ""),
                       ("which website about binary search did I visit 3 days ago?",
                        "binary search")):
        got = chat._when(text, TODAY)
        assert chat.topic(text, got[2]) == want


# ---- retrieval with a date range --------------------------------------------------------------

@pytest.fixture
def vault(root, monkeypatch):
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    day = "kind: browse\nprivate: true\ndate: {}\n"
    note(root, "browse/private/browse-private-2026-11-15.md", day.format("2026-11-15"),
         "# Browsing 2026-11-15 (private)\n\n## Topics\n- Car motor repair forum (cars.example)\n")
    note(root, "browse/private/browse-private-2026-11-14.md", day.format("2026-11-14"),
         "# Browsing 2026-11-14 (private)\n\n## Topics\n- Pasta recipe blog (food.example)\n")
    note(root, "browse/private/browse-private-2026-11-10.md", day.format("2026-11-10"),
         "# Browsing 2026-11-10 (private)\n\n## Topics\n- Car motor auction\n")
    note(root, "notes/engines.md", "title: Engines\n", "# Engines\n\nAutomobile car motor basics.")
    note(root, "notes/kitchen-day.md", "title: Kitchen day\ndate: 2026-11-16\n",
         "# Kitchen day\n\nCooking pasta at home.")
    return Vault(root)


def _paths(found):
    return [p["path"] for p in found]


def test_a_range_keeps_only_pages_dated_inside_it(vault):
    found = ask._passages(vault, "car motor 3 days ago", 8, between=("2026-11-14", "2026-11-16"),
                          topic="car motor")
    assert set(_paths(found)) == {"browse/private/browse-private-2026-11-15.md",
                                  "browse/private/browse-private-2026-11-14.md",
                                  "notes/kitchen-day.md"}
    assert _paths(found)[0] == "browse/private/browse-private-2026-11-15.md"  # by topic
    assert {p["day"] for p in found} <= {"2026-11-14", "2026-11-15", "2026-11-16"}
    # no range: the undated note and the older day come back
    assert {"notes/engines.md", "browse/private/browse-private-2026-11-10.md"} <= set(
        _paths(ask._passages(vault, "car motor", 8)))


def test_no_topic_lists_the_days_newest_first(vault):
    found = ask._passages(vault, "which sites did I visit", 8,
                          between=("2026-11-14", "2026-11-15"), topic="")
    assert _paths(found) == ["browse/private/browse-private-2026-11-15.md",
                             "browse/private/browse-private-2026-11-14.md"]


def test_source_switches(vault):
    off = {"private": False}
    assert not any(p.startswith("browse/private/")
                   for p in _paths(ask._passages(vault, "car motor", 8, sources=off)))
    only_private = ask._passages(vault, "car motor", 8,
                                 sources={"notes": False, "chats": False, "browsing": False})
    assert only_private and all(p["kind"] == "browse" for p in only_private)


# ---- history and storage ---------------------------------------------------------------------

def test_history_keeps_the_last_turns_within_budget(monkeypatch):
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "text": f"m{i} " + "x" * 400}
            for i in range(12)]
    got = chat.history(msgs)
    assert len(got) == chat.TURNS and got[0]["role"] == "user"
    assert got[-1]["content"].startswith("m11")
    monkeypatch.setattr(chat, "HISTORY_BUDGET", 1000)
    got = chat.history(msgs)
    assert sum(len(m["content"]) for m in got) <= 1000 and got[-1]["content"].startswith("m11")
    assert got[0]["role"] == "user"  # an assistant message never leads
    two = chat.history([{"role": "user", "text": "a"}, {"role": "user", "text": "b"}])
    assert two == [{"role": "user", "content": "a\n\nb"}]


def test_ids_are_strict_ulids(vault):
    for bad in ("../x", "..\\..\\config", "01ARZ3NDEKTSV4RRFFQ69G5FA", "01arz3ndektsv4rrffq69g5fav",
                "01ARZ3NDEKTSV4RRFFQ69G5FAV/../x", None, 5, "01ARZ3NDEKTSV4RRFFQ69G5FAU"):
        with pytest.raises(ValueError):
            chat.load(vault, bad)
    assert chat.load(vault, chat.new()) == []


def test_storage_list_delete(vault):
    a, b = chat.new(), chat.new()
    chat._save(vault, a, [
        {"role": "user", "text": "x" * 80 + " tail", "ts": "2026-11-17T10:00:00Z"},
        {"role": "assistant", "text": "y", "ts": "2026-11-17T10:00:05Z"}], True)
    chat._save(vault, b, [{"role": "user", "text": "second", "ts": "2026-11-18T09:00:00Z"}], True)
    assert (vault.root / ".cortex" / "browse" / "chat" / f"{a}.jsonl").is_file()
    rows = chat.list(vault)
    assert [r["id"] for r in rows] == [b, a]
    assert rows[1] == {"id": a, "title": "x" * 60, "updated": "2026-11-17T10:00:05Z", "count": 2}
    assert chat.delete(vault, a) and not chat.delete(vault, a)
    assert chat.delete_all(vault) == 1 and chat.list(vault) == []


def test_settings_validate_and_keep_other_keys(home):
    from cortex import taint
    taint.write_config({"vault": "x", "chat": {"other": 1}})
    assert chat.get_settings() == chat.DEFAULTS
    for bad in ({"length": "huge"}, {"temperature": 2}, {"temperature": True},
                {"passages": 3}, {"passages": 13}, {"passages": 5.5}, {"history": "no"},
                {"sources": {"web": True}}, {"sources": {"notes": 1}}, {"model": "x"}):
        with pytest.raises(ValueError):
            chat.save_settings(bad)
    got = chat.save_settings({"length": "long", "passages": 4, "sources": {"private": False}})
    assert got["length"] == "long" and got["passages"] == 4
    assert got["sources"] == {"notes": True, "chats": True, "browsing": True, "private": False}
    cfg = taint.config()
    assert cfg["vault"] == "x" and cfg["chat"]["other"] == 1
    taint.write_config({"chat": {"temperature": "hot", "passages": 99}})  # bad on disk: defaults
    assert chat.get_settings()["temperature"] == 0.2 and chat.get_settings()["passages"] == 8


# ---- a turn ------------------------------------------------------------------------------------

def _turn(vault, cid, text, runner, settings=None, cancel=None, on=None):
    events = []

    def emit(ev):
        events.append(ev)
        if on:
            on(ev)
    cid = chat.reply(vault, cid, text, runner, emit, cancel or threading.Event(),
                     settings or chat.get_settings(), today=TODAY)
    return cid, events


def test_reply_streams_cites_and_saves(vault, stream):
    s = stream("ollama", ["Your forum page", " says so [1]."])
    cid, ev = _turn(vault, None, "car motor", s.runner())
    kinds = [e["t"] for e in ev]
    assert kinds[0] == "start" and kinds[-2:] == ["src", "done"] and "tok" in kinds
    assert "".join(e["s"] for e in ev if e["t"] == "tok") == "Your forum page says so [1]."
    src = ev[-2]["sources"]
    assert [s_["n"] for s_ in src] == [1] and src[0]["kind"] in ("browse", "note")
    system, *rest = s.bodies[0]["messages"]
    assert system["content"].startswith(ask.SYSTEM) and "2026-11-18" in system["content"]
    assert rest[-1]["content"].count('<untrusted_note id="') >= 1
    saved = chat.load(vault, cid)
    assert [m["role"] for m in saved] == ["user", "assistant"]
    assert saved[1]["sources"] == src and saved[1]["model"] == "gemma3:2b"
    assert ev[-1]["id"] == cid and ev[-1]["stopped"] is False
    assert ev[-2]["searched"] == [] and "searched" not in saved[1]


def test_an_answer_citing_nothing_lists_the_pages_searched(vault, stream):
    s = stream("ollama", ["Your forum page says so."])
    cid, ev = _turn(vault, None, "car motor", s.runner())
    src = ev[-2]
    assert src["sources"] == [] and 1 <= len(src["searched"]) <= chat.SEARCHED
    assert all(set(p) == {"path", "title", "kind"} for p in src["searched"])
    assert chat.load(vault, cid)[1]["searched"] == src["searched"]


def test_a_follow_up_carries_history_and_the_previous_question(vault, stream, monkeypatch):
    s = stream("ollama", ["Answer [1]."])
    cid, _ = _turn(vault, None, "car motor", s.runner())
    asked = []
    real = ask._passages
    monkeypatch.setattr(ask, "_passages", lambda v, q, k, **kw: asked.append(q) or real(v, q, k,
                                                                                      **kw))
    _turn(vault, cid, "and its running time?", s.runner())
    assert asked == ["and its running time?\ncar motor"]
    msgs = s.bodies[1]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[1]["content"] == "car motor" and "untrusted_note" not in msgs[2]["content"]
    assert len(chat.load(vault, cid)) == 4


def test_cancel_keeps_the_partial_answer(vault, stream):
    s = stream("ollama", [f"w{i} " for i in range(30)], delay=0.03)
    cancel = threading.Event()
    cid, ev = _turn(vault, None, "car motor", s.runner(), cancel=cancel,
                    on=lambda e: e["t"] == "tok" and cancel.set())
    last = chat.load(vault, cid)[-1]
    assert last["stopped"] is True and last["text"].startswith("w0") and ev[-1]["stopped"]


def test_history_off_writes_nothing(vault, stream):
    s = stream("ollama", ["ok [1]"])
    cid, _ = _turn(vault, None, "car motor", s.runner(), {**chat.get_settings(), "history": False})
    assert not (vault.root / chat.DIR).exists() or not any((vault.root / chat.DIR).iterdir())
    assert len(chat.load(vault, cid)) == 2  # this session still remembers it
    chat.delete_all(vault)


def test_off_topic_without_passages_says_i_dont_know(vault, stream, monkeypatch):
    monkeypatch.setattr(ask, "_passages", lambda *a, **k: [])
    s = stream("ollama", ["never asked"])
    _, ev = _turn(vault, None, "the moon", s.runner())
    assert [e["s"] for e in ev if e["t"] == "tok"] == [chat.NO_ANSWER] and not s.bodies


def test_errors_are_events(vault, stream, monkeypatch):
    _, ev = _turn(vault, None, "car motor", None)
    assert ev == [{"t": "err", "code": "no_local_model", "message": ask.NO_MODEL,
                   "id": ev[0]["id"]}]
    dead = localllm.Runner("ollama", "http://127.0.0.1:9", "m")  # nothing listens
    _, ev = _turn(vault, None, "car motor", dead)
    assert ev[-1]["t"] == "err" and ev[-1]["code"] == "no_answer"
    monkeypatch.setattr(semantic, "_embedder", None)
    _, ev = _turn(vault, None, "car motor", dead)
    assert ev[0]["code"] == "not_set_up"


def test_a_cloud_runner_never_hears_private_passages(vault, monkeypatch):
    cloud = cloudllm.CloudRunner("openai", "https://api.example.com/v1", "gpt-x")
    monkeypatch.setattr(cloudllm.CloudRunner, "stream",
                        lambda *a, **k: pytest.fail("the cloud runner was asked"), raising=False)
    _, ev = _turn(vault, None, "car motor", cloud)
    assert ev[0]["code"] == "no_local_model"
    far = localllm.Runner("ollama", "http://10.1.2.3:11434", "m")
    assert _turn(vault, None, "car motor", far)[1][0]["code"] == "no_local_model"


def test_bad_text_is_refused(vault, stream):
    for bad in ("", "   ", "x" * (chat.MAX_TEXT + 1), None):
        with pytest.raises(ValueError):
            _turn(vault, None, bad, stream().runner())
