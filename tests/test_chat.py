"""The local chat: `localllm.Runner.stream` against fake streaming runners (Ollama NDJSON,
OpenAI-compatible SSE), `chat.when`, retrieval with a date range, history trimming, storage
under .cortex/browse/chat/, and `chat.reply` end to end. Loopback fakes only; no model."""

import http.client
import json
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import FakeEmbed, note

from cortex import ask, chat, chat_study, cloudllm, localllm, semantic
from cortex.events import EventLog
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
    ("in the last 3 days", "2026-11-16", "2026-11-18"),  # 3 days, today the third
    ("past week", "2026-11-12", "2026-11-18"),
    ("the last 2 weeks", "2026-11-05", "2026-11-18"),
    ("past 2 to 3 days", "2026-11-16", "2026-11-18"),
    ("last night", "2026-11-17", "2026-11-17"),
    ("what is due tomorrow?", "2026-11-19", "2026-11-19"),
    ("what did I do on Monday?", "2026-11-16", "2026-11-16"),  # TODAY is a Wednesday
    ("last Monday", "2026-11-16", "2026-11-16"),
    ("on Wednesday", "2026-11-18", "2026-11-18"),  # today
    ("last Wednesday", "2026-11-11", "2026-11-11"),  # "last": never today
    ("on friday", "2026-11-13", "2026-11-13"),
    ("this weekend", "2026-11-14", "2026-11-15"),
    ("what did I read over the weekend", "2026-11-14", "2026-11-15"),
    ("Sept 3", "2026-09-03", "2026-09-03"),
    ("on 5 Nov. 2026", "2026-11-05", "2026-11-05"),
    ("my marks 90 on 12 Nov", "2026-11-12", "2026-11-12"),  # a bad first match, then a real one
])
def test_when(text, lo, hi):
    got = chat.when(text, TODAY)
    assert got and (got[0].isoformat(), got[1].isoformat()) == (lo, hi)


@pytest.mark.parametrize("text", ["what did I learn about Dijkstra?", "31 Feb", "",
                                  "and its running time?", "may I ask about heaps",
                                  # words that start like a month are not months
                                  "what did I learn in market research?",
                                  "summarize my 3 decisions", "the novel 2 chapters",
                                  "in decent shape", "2 junior roles", "4 octaves", "8 augments",
                                  "Cyber Monday deals", "the weekend effect in markets"])
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
                          sources=ask.SOURCES, topic="car motor")
    assert set(_paths(found)) == {"browse/private/browse-private-2026-11-15.md",
                                  "browse/private/browse-private-2026-11-14.md",
                                  "notes/kitchen-day.md"}
    assert _paths(found)[0] == "browse/private/browse-private-2026-11-15.md"  # by topic
    assert {p["day"] for p in found} <= {"2026-11-14", "2026-11-15", "2026-11-16"}
    # no range: the undated note and the older day come back
    assert {"notes/engines.md", "browse/private/browse-private-2026-11-10.md"} <= set(
        _paths(ask._passages(vault, "car motor", 8, sources=ask.SOURCES)))


def test_an_undated_note_counts_on_the_day_its_file_changed(vault):
    # cortex_add writes notes without a date; "what did I note on 15 Nov" still finds them
    import os
    from datetime import datetime
    note(vault.root, "notes/gearbox.md", "kind: note\ntitle: Gearbox\n",
         "# Gearbox\n\nCar motor gears.")
    note(vault.root, "concepts/torque.md", "kind: concept\ntitle: Torque\n",
         "# Torque\n\nCar motor torque.")  # concepts stay undated: tools rewrite them
    t = datetime(2026, 11, 15, 12).timestamp()
    for p in ("notes/gearbox.md", "concepts/torque.md", "notes/engines.md"):
        os.utime(vault.root / p, (t, t))
    found = ask._passages(vault, "car motor", 8, between=("2026-11-15", "2026-11-15"),
                          topic="car motor")
    assert "notes/gearbox.md" in _paths(found)
    assert "concepts/torque.md" not in _paths(found)
    assert "notes/engines.md" not in _paths(found)  # no kind: not a note page
    assert next(p for p in found if p["path"] == "notes/gearbox.md")["day"] == "2026-11-15"


def test_no_topic_lists_the_days_newest_first(vault):
    found = ask._passages(vault, "which sites did I visit", 8,
                          between=("2026-11-14", "2026-11-15"), sources=ask.SOURCES, topic="")
    assert _paths(found) == ["browse/private/browse-private-2026-11-15.md",
                             "browse/private/browse-private-2026-11-14.md"]


def test_source_switches(vault):
    off = {"private": False}
    assert not any(p.startswith("browse/private/")
                   for p in _paths(ask._passages(vault, "car motor", 8, sources=off)))
    only_private = ask._passages(vault, "car motor", 8, sources={
        "notes": False, "chats": False, "browsing": False, "private": True})
    assert only_private and all(p["kind"] == "browse" for p in only_private)


SECRET = "notes/secret-garage.md"  # a private note: Chat's local model only, never Claude


def _secret(vault):
    note(vault.root, SECRET, "title: Secret garage\nprivate: true\n",
         "# Secret garage\n\nCar motor zanzibarquux stash.")
    return vault


def test_the_private_switch_covers_private_notes(vault, stream):
    _secret(vault)
    found = ask._passages(vault, "car motor", 8, sources=ask.SOURCES)
    hit = next(p for p in found if p["path"] == SECRET)
    assert hit["kind"] == "note" and "zanzibarquux" in hit["text"]
    assert SECRET not in _paths(ask._passages(vault, "car motor", 8, sources={"private": False}))
    # under MIN_COS
    assert SECRET not in _paths(ask._passages(vault, "pasta recipe", 8, sources=ask.SOURCES))
    only = ask._passages(vault, "car motor", 8, sources={
        "notes": False, "chats": False, "browsing": False, "private": True})
    assert SECRET in _paths(only)
    s = stream("ollama", ["It is in [1]."])
    _turn(vault, None, "car motor", s.runner())
    assert "zanzibarquux" in s.bodies[-1]["messages"][-1]["content"]  # the local model reads it
    off = dict(chat.get_settings(), sources=dict(ask.SOURCES, private=False))
    _turn(vault, None, "car motor", s.runner(), settings=off)
    assert "zanzibarquux" not in json.dumps(s.bodies[-1])


def test_passages_leave_private_pages_out_unless_asked(vault):
    _secret(vault)
    got = _paths(ask._passages(vault, "car motor", 8))  # a caller that does not say
    assert "notes/engines.md" in got and SECRET not in got
    assert not any(p.startswith("browse/private/") for p in got)


def test_private_vectors_are_kept_while_a_file_is_unchanged(vault, monkeypatch):
    _secret(vault)
    reads = []
    page = ask._page
    monkeypatch.setattr(ask, "_page", lambda v, p, i: reads.append(p) or page(v, p, i))
    embed = semantic._embedder
    assert SECRET in _paths(ask._passages(vault, "car motor", 8, sources=ask.SOURCES))
    memo = ask._PRIVATE[vault.root]
    assert vault.root / SECRET in memo
    assert all(e[2].base is None for e in memo.values())  # copies: no batch array kept alive
    reads.clear()
    embed.texts.clear()
    assert SECRET in _paths(ask._passages(vault, "car motor", 8, sources=ask.SOURCES))
    assert reads == [vault.root / SECRET]  # only the hit is read again, for its text
    assert not any("zanzibarquux" in t for t in embed.texts)  # nothing re-embedded
    (vault.root / SECRET).unlink()
    assert SECRET not in _paths(ask._passages(vault, "car motor", 8, sources=ask.SOURCES))
    assert vault.root / SECRET not in ask._PRIVATE[vault.root]  # a deleted page drops out


def test_the_first_private_pass_keeps_to_its_time_cap(vault, monkeypatch):
    _secret(vault)
    monkeypatch.setattr(semantic, "HEAD_BUDGET", 0)  # no time for chunk 0 this question
    assert SECRET not in _paths(ask._passages(vault, "car motor", 8, sources=ask.SOURCES))
    assert ask._PRIVATE[vault.root] == {}
    monkeypatch.setattr(semantic, "HEAD_BUDGET", 1.0)  # a later question embeds it
    assert SECRET in _paths(ask._passages(vault, "car motor", 8, sources=ask.SOURCES))
    long = "## Part\n\nCar motor zanzibarquux.\n\n" * 400  # several chunks
    note(vault.root, "notes/secret-long.md", "title: Secret long\nprivate: true\n", long)
    monkeypatch.setattr(semantic, "RECALL_BUDGET", 0)  # chunk 0 only, the rest later
    ask._passages(vault, "car motor", 8, sources=ask.SOURCES)
    _, starts, _, full = ask._PRIVATE[vault.root][vault.root / "notes/secret-long.md"]
    assert starts == [0] and not full
    monkeypatch.setattr(semantic, "RECALL_BUDGET", 1.0)
    ask._passages(vault, "car motor", 8, sources=ask.SOURCES)
    _, starts, _, full = ask._PRIVATE[vault.root][vault.root / "notes/secret-long.md"]
    assert len(starts) > 1 and full


def test_chat_private_notes_never_reach_mcp_or_the_dashboard(vault):
    """The guard: after Chat searched the private note, the shared index, its vector cache,
    the MCP tools and the dashboard still never hold it."""
    from conftest import run

    from cortex import dashboard

    _secret(vault)
    assert SECRET in _paths(ask._passages(vault, "car motor zanzibarquux", 8, sources=ask.SOURCES))
    marks = ("secret-garage", "Secret garage", "zanzibarquux")
    got = []

    async def body(call):
        q = "car motor garage stash"  # context and prime echo the query: no mark in it
        got.append(await call("cortex_find", {"query": q}))
        got.append(await call("cortex_recall", {"query": q}))
        got.append(await call("cortex_recall", {"query": q, "chats": True}))
        got.append(await call("cortex_read", {"id": "secret-garage"}))
        got.append(await call("cortex_prime", {"prompt": q}))
        got.append(await call("cortex_context", {"topic": q}))

    run(vault.root, body)
    assert got[1]["hits"] or got[0]["hits"]  # the query does find the public car note
    read = got.pop(3)  # its error message echoes the id asked for, nothing else
    assert read["error"] == "unknown_id" and not any(m in json.dumps(read) for m in marks[1:])
    for out in got:
        assert not any(m in json.dumps(out) for m in marks), out
    assert "secret-garage" not in semantic._read(vault.root)  # the shared vector cache
    blob = json.dumps(dashboard.build_data(vault, now=datetime(2026, 11, 18, 12).astimezone()))
    assert not any(m in blob for m in marks)


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


def test_remembers_marks_the_oldest_message_the_model_still_gets(monkeypatch):
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "text": f"m{i}"} for i in range(4)]
    assert chat.remembers([]) == 0 and chat.remembers(msgs) == 0  # all of them: no line
    msgs += [{"role": "user" if i % 2 == 0 else "assistant", "text": f"m{i}"}
             for i in range(4, 10)]
    assert chat.remembers(msgs) == 10 - chat.TURNS
    assert chat.history(msgs)[0]["content"] == msgs[chat.remembers(msgs)]["text"]
    monkeypatch.setattr(chat, "HISTORY_BUDGET", 3)
    assert chat.remembers(msgs) == 10  # not even the last fits: the line goes below them all
    off = [{"role": "user", "text": "q"}, {"role": "assistant", "text": "a", "switches": {
        **ask.SOURCES, "private": True}}, {"role": "user", "text": "q2"},
        {"role": "assistant", "text": "a2", "switches": {**ask.SOURCES, "private": False}}]
    monkeypatch.setattr(chat, "HISTORY_BUDGET", 3000)
    assert chat.remembers(off, {**ask.SOURCES, "private": False}) == 2


def test_the_done_event_says_where_the_memory_starts(vault, stream):
    s = stream("ollama", ["Answer [1]."])
    cid = None
    for q in ("car motor", "car motor torque", "car motor gearbox", "car motor oil"):
        cid, ev = _turn(vault, cid, q, s.runner())
    assert ev[-1]["t"] == "done" and ev[-1]["remembers"] == 8 - chat.TURNS


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
                {"sources": {"web": True}}, {"sources": {"notes": 1}}, {"model": "x"},
                {"passages": float("inf")}, {"temperature": float("nan")}):
        with pytest.raises(ValueError):
            chat.save_settings(bad)
    got = chat.save_settings({"length": "long", "passages": 4, "sources": {"private": False}})
    assert got["length"] == "long" and got["passages"] == 4
    assert got["sources"] == {"notes": True, "chats": True, "browsing": True, "private": False}
    cfg = taint.config()
    assert cfg["vault"] == "x" and cfg["chat"]["other"] == 1
    taint.write_config({"chat": {"temperature": "hot", "passages": 99}})  # bad on disk: defaults
    assert chat.get_settings()["temperature"] == 0.2 and chat.get_settings()["passages"] == 5


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
    steps = [e["s"] for e in ev if e["t"] == "step"]
    assert steps[0] == "Searching your notes and browsing"
    assert steps[1].startswith("Reading ") and steps[1].endswith(" with gemma3:2b")
    assert kinds.index("step") < kinds.index("tok")
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


def test_a_quote_no_passage_holds_is_flagged_and_saved(vault, stream):
    s = stream("ollama", ["Your page says “automobile CAR motor\n basics.” and ",
                          '"the motor runs on pure moonlight".'])
    cid, ev = _turn(vault, None, "car motor", s.runner())
    assert ev[-2]["unquoted"] == ["the motor runs on pure moonlight"]
    assert chat.load(vault, cid)[1]["unquoted"] == ["the motor runs on pure moonlight"]
    ok = stream("ollama", ['It says "Automobile car motor basics".'])
    cid, ev = _turn(vault, None, "car motor", ok.runner())
    assert "unquoted" not in ev[-2] and "unquoted" not in chat.load(vault, cid)[1]


def test_an_answer_citing_nothing_lists_the_pages_searched(vault, stream):
    s = stream("ollama", ["Your forum page says so."])
    cid, ev = _turn(vault, None, "car motor", s.runner())
    src = ev[-2]
    assert src["sources"] == [] and 1 <= len(src["searched"]) <= chat.SEARCHED
    assert all(set(p) == {"path", "title", "kind", "at", "len"} for p in src["searched"])
    assert chat.load(vault, cid)[1]["searched"] == src["searched"]


# ---- the "About the user" block (G1) ---------------------------------------------------------

CORE = "# Core\n\n## Core\n- Call me Sam; my project is a graph planner. ^c-abcde\n"


def _me(vault, page, front, body):
    note(vault.root, f"me/{page}.md", front, body)


def test_the_system_message_knows_who_asks(vault, stream):
    _me(vault, "core", "", CORE)
    _me(vault, "profile", "summary: CS student in Kochi, revising for exams.\n", "# Profile\n")
    s = stream("ollama", ["Your forum page [1] and [2]."])
    _, ev = _turn(vault, None, "car motor", s.runner())
    system, *rest = s.bodies[0]["messages"]
    assert system["content"].startswith(ask.SYSTEM)
    assert "- Call me Sam; my project is a graph planner." in system["content"]
    assert "CS student in Kochi" in system["content"] and "^c-" not in system["content"]
    assert 'id="about the user"' in system["content"] and "never cite it" in system["content"]
    assert len(chat.about(vault, ask.SOURCES, TODAY)) <= chat.ABOUT_MAX
    # unnumbered and outside the passages: citations still count only passages
    assert "Call me Sam" not in rest[-1]["content"]
    assert {s_["n"] for s_ in ev[-2]["sources"]} <= set(range(1, rest[-1]["content"].count(
        '<untrusted_note id="') + 1))


def test_the_block_keeps_chats_privacy_rule(vault):
    _me(vault, "core", "", CORE)
    _me(vault, "profile", "private: true\nsummary: Secret profile line.\n", "# Profile\n")
    on = dict(ask.SOURCES)
    assert "Secret profile" in chat.about(vault, on, TODAY)  # the local model, Private on
    got = chat.about(vault, dict(on, private=False), TODAY)
    assert "Secret profile" not in got and "Call me Sam" in got
    assert chat.about(vault, dict(on, private=False, notes=False), TODAY) == ""
    _me(vault, "core", "private: true\n", CORE)  # a private core is never pinned (prime)
    assert "Call me Sam" not in chat.about(vault, on, TODAY)


def test_the_block_comes_out_of_the_history_budget(vault, stream):
    _me(vault, "core", "", CORE)
    _me(vault, "profile", "summary: " + "Long profile words. " * 60 + "\n", "# Profile\n")
    me = chat.about(vault, ask.SOURCES, TODAY)
    assert 0 < len(me) <= chat.ABOUT_MAX and me.rstrip().endswith("…\n</untrusted_note>")
    s = stream("ollama", ["Answer [1]."])
    cid = None
    for q in ("car motor " + "x" * 1300, "car motor " + "y" * 1300, "car motor torque"):
        cid, _ = _turn(vault, cid, q, s.runner())
    system, *rest = s.bodies[-1]["messages"]
    sent = len(system["content"]) - len(chat.SYSTEM.format(today=f"{TODAY:%A}, {TODAY}"))
    assert sent == len(me)
    assert sent + sum(len(m["content"]) for m in rest[:-1]) <= chat.HISTORY_BUDGET
    assert "x" * 1300 not in "".join(m["content"] for m in rest)  # the oldest turn made room


# ---- trust: coverage, typed pills and the exact passage (v9 B3/B4) -------------------------

def test_default_passages_are_five_short_ones(vault, stream):
    assert chat.DEFAULTS["passages"] == 5 and ask.PASSAGE <= 700
    note(vault.root, "notes/long-engines.md", "title: Long engines\n",
         "# Long engines\n\n" + "Car motor pistons and valves. " * 200)
    found = ask._passages(vault, "car motor", chat.DEFAULTS["passages"], sources=ask.SOURCES)
    assert 1 <= len(found) <= 5 and all(len(p["text"]) <= 700 for p in found)
    s = stream("ollama", ["Pistons [1]."])
    _turn(vault, None, "car motor", s.runner())
    user = s.bodies[0]["messages"][-1]["content"]
    assert 1 <= user.count('<untrusted_note id="') <= 5


def test_coverage_counts_what_was_read_of_what_matched(vault, stream, monkeypatch):
    stats = {}
    found = ask._passages(vault, "car motor", 2, sources=ask.SOURCES, stats=stats)
    every = ask._passages(vault, "car motor", 12, sources=ask.SOURCES)
    assert len(found) == 2 and stats["matched"] == len(every) > 2
    s = stream("ollama", ["Forum [1]."])
    cid, ev = _turn(vault, None, "car motor", s.runner(),
                    settings=dict(chat.get_settings(), passages=4))
    cov = ev[-2]["coverage"]
    n = len(ask._passages(vault, "car motor", 4, sources=chat.get_settings()["sources"]))
    assert cov == {"read": n, "matched": len(every)}
    assert chat.load(vault, cid)[-1]["coverage"] == cov


def test_pills_are_typed_and_titles_escaped(vault, stream):
    note(vault.root, "notes/linky.md", "title: '![x](http://evil.example/a.png) [Car](http://"
         "evil.example) <b>motor</b>'\n", "# Linky\n\nCar motor link page.")
    s = stream("ollama", ["[1] [2] [3] [4] [5]"])
    _, ev = _turn(vault, None, "car motor", s.runner())
    src = ev[-2]["sources"]
    assert src and all(p["kind"] in ("note", "chat", "browse") for p in src)
    assert {"note", "browse"} <= {p["kind"] for p in src}
    blob = json.dumps(src)
    assert "evil.example" not in blob and "<b>" not in blob and "](" not in blob


def test_a_citation_opens_its_exact_passage(vault):
    body = ("# Engines\n\nAutomobile car motor basics.\n\n## Gearbox\n\n"
            + "Shifting gears needs a clutch. " * 60 + "\n\n## Pistons\n\nPistons go up.\n")
    note(vault.root, "notes/engines.md", "title: Engines\n", body)
    found = ask._passages(vault, "car motor", 8, sources=ask.SOURCES)
    p = next(x for x in found if x["path"] == "notes/engines.md")
    got = chat.passage(vault, p["path"], p["at"], p["len"], ask.SOURCES)
    assert got["kind"] == "note" and got["title"] == "Engines"
    assert len(got["text"]) <= 700 and got["text"] in p["text"]
    assert got["text"].startswith("Automobile car motor basics.")
    assert p["text"].rstrip().endswith(got["text"][-40:])  # both ends, whatever the line ends
    at = vault.read_path(vault.root / "notes/engines.md").body.index("Shifting")
    later = chat.passage(vault, "notes/engines.md", at, 100, ask.SOURCES)
    assert later["heading"] == "Gearbox" and later["text"].startswith("Shifting gears")
    browse = next(x for x in found if x["kind"] == "browse")
    got = chat.passage(vault, browse["path"], browse["at"], browse["len"], ask.SOURCES)
    assert got["kind"] == "browse" and got["text"]
    assert got["text"] in browse["text"].replace("\r\n", "\n")  # the view drops line ends
    for bad in ("../outside.md", "/etc/x.md", "notes/engines.txt", "notes\\engines.md"):
        with pytest.raises(ValueError):
            chat.passage(vault, bad, 0, 100, ask.SOURCES)


def test_a_deep_passage_opens_at_its_own_copy_of_a_repeated_line(vault, monkeypatch):
    """Audit 21 Nov C6: a line that repeats is placed in the chunk, not at its first copy."""
    refrain = "Refrain: the same line repeats here."
    head = f"# Deep\n\n{refrain}\n"
    start = semantic.BODY_CHARS - semantic.CHUNK_OVERLAP  # the second chunk's start
    filler = "".join(f"filler line {i:03} with some words.\n" for i in range(60))
    filler = filler[:start - len(head) - 1] + "\n"
    body = head + filler + refrain + "\nThe deep part goes on about gearboxes.\n" * 3
    note(vault.root, "notes/deep.md", "title: Deep\n", body)
    monkeypatch.setattr(semantic, "search", lambda idx, q, k, **kw: (
        [{"id": "deep", "at": start, "title": "Deep"}], None))
    p = next(x for x in ask._passages(vault, "gearboxes", 4, sources=ask.SOURCES)
             if x["path"] == "notes/deep.md")
    page = vault.read_path(vault.root / "notes/deep.md").body
    assert page.find(refrain) < start and p["at"] == page.rindex(refrain)


def test_the_passage_view_keeps_to_the_switches(vault):
    _secret(vault)
    on = dict(ask.SOURCES)
    assert "zanzibarquux" in chat.passage(vault, SECRET, 0, 700, on)["text"]
    with pytest.raises(PermissionError):
        chat.passage(vault, SECRET, 0, 700, dict(on, private=False))
    priv = "browse/private/browse-private-2026-11-15.md"
    assert chat.passage(vault, priv, 0, 700, on)["text"]
    with pytest.raises(PermissionError):
        chat.passage(vault, priv, 0, 700, dict(on, private=False))
    with pytest.raises(PermissionError):
        chat.passage(vault, "notes/engines.md", 0, 700, dict(on, notes=False))


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


def test_my_relation_adds_a_public_persons_name_to_the_search(vault, stream, monkeypatch):
    note(vault.root, "people/asha-rao.md", "title: Asha Rao\nkind: person\nrelation: sister\n"
         "private: false\n", "# Asha Rao\n\nPlays the violin.")
    note(vault.root, "people/hidden-sis.md", "title: Hidden Sis\nkind: person\nrelation: sister\n",
         "# Hidden Sis\n\nPrivate by default.")  # a people page is private unless it says not
    note(vault.root, "people/ravi.md", "title: Ravi\nkind: person\nrelation: study-partner\n"
         "private: false\n", "# Ravi\n\nDSA.")
    assert chat.relatives(vault, "What does my sister play?") == ["Asha Rao"]
    assert chat.relatives(vault, "my study partner and my sisters") == ["Asha Rao", "Ravi"]
    assert chat.relatives(vault, "my brother") == [] and chat.relatives(vault, "a sister") == []
    asked = []
    real = ask._passages
    monkeypatch.setattr(ask, "_passages", lambda v, q, k, **kw: asked.append(q) or real(v, q, k,
                                                                                      **kw))
    s = stream("ollama", ["The violin [1]."])
    cid, ev = _turn(vault, None, "What does my sister play?", s.runner())
    assert asked == ["What does my sister play?\nAsha Rao"]
    assert ev[-2]["coverage"]["also"] == ["Asha Rao"]
    assert chat.load(vault, cid)[-1]["coverage"]["also"] == ["Asha Rao"]
    _, ev = _turn(vault, None, "car motor", s.runner())
    assert "also" not in ev[-2]["coverage"]


def test_history_leaves_out_answers_made_with_a_switch_now_off(vault, stream):
    s = stream("ollama", ["Answer [1]."])
    cid, _ = _turn(vault, None, "car motor", s.runner())  # every switch on
    assert chat.load(vault, cid)[1]["switches"] == ask.SOURCES
    _turn(vault, cid, "car motor torque", s.runner())
    no_private = {**chat.get_settings(), "sources": {**ask.SOURCES, "private": False}}
    _turn(vault, cid, "car motor gearbox", s.runner(), settings=no_private)
    assert [m["role"] for m in s.bodies[2]["messages"]] == ["system", "user"]  # both left out
    _turn(vault, cid, "car motor engines", s.runner(), settings=no_private)
    msgs = s.bodies[3]["messages"]  # the answer made with Private off is kept
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[1]["content"] == "car motor gearbox"
    _turn(vault, cid, "car motor again", s.runner())  # turning a switch on hides nothing
    assert [m["content"] for m in s.bodies[4]["messages"][1:-1:2]] == [
        "car motor torque", "car motor gearbox", "car motor engines"]


def test_history_treats_an_old_answer_as_made_with_every_switch_on():
    old = [{"role": "user", "text": "q1"}, {"role": "user", "text": "q1 again"},
           {"role": "assistant", "text": "private site"},
           {"role": "user", "text": "q2"},
           {"role": "assistant", "text": "a2", "switches": {**ask.SOURCES, "private": False}}]
    assert [m["content"] for m in chat.history(old, ask.SOURCES)] == ["q1\n\nq1 again",
                                                                      "private site", "q2", "a2"]
    off = {**ask.SOURCES, "private": False}
    assert [m["content"] for m in chat.history(old, off)] == ["q2", "a2"]
    assert chat.history(old, {**ask.SOURCES, "notes": False}) == []


def test_a_new_subject_is_searched_alone(vault, stream, monkeypatch):
    s = stream("ollama", ["Answer [1]."])
    cid, _ = _turn(vault, None, "car motor", s.runner())
    asked = []
    real = ask._passages
    monkeypatch.setattr(ask, "_passages", lambda v, q, k, **kw: asked.append(q) or real(v, q, k,
                                                                                      **kw))
    _turn(vault, cid, "what is Dijkstra's algorithm?", s.runner())
    _turn(vault, cid, "what did I browse yesterday?", s.runner())
    assert asked == ["what is Dijkstra's algorithm?",
                     "what did I browse yesterday?"]


def test_a_follow_up_keeps_the_last_questions_dates(vault, stream, monkeypatch):
    s = stream("ollama", ["Answer [1]."])
    asked = []
    real = ask._passages

    def passages(v, q, k, **kw):
        asked.append((q, kw["between"], kw["topic"]))
        return real(v, q, k, **kw)
    monkeypatch.setattr(ask, "_passages", passages)
    cid, _ = _turn(vault, None, "what did I read about car motors yesterday?", s.runner())
    _turn(vault, cid, "which of those was hardest?", s.runner())
    _turn(vault, cid, "give me an example", s.runner())
    day = ("2026-11-17", "2026-11-17")
    assert asked[0][1:] == (day, "car motors")
    assert asked[1] == ("which of those was hardest?\nwhat did I read about car motors yesterday?",
                        day, "car motors")
    assert asked[2] == ("give me an example\nwhich of those was hardest?", None, "")


@pytest.mark.parametrize("text, want", [
    ("and its running time?", True), ("why?", True), ("explain that again", True),
    ("what about heaps", True), ("But how is it proved?", True), ("tell me more", True),
    ("how do I avoid the second one?", True), ("is the latter faster?", True),
    ("give me an example", True), ("explain it again please", True),
    ("can a list be one?", True), ("which one is faster for lookups?", True),
    ("explain dijkstra", False), ("what is one-hot encoding?", False),
    ("one thing I learned about graphs", False),
    ("what did I learn this month?", False),
    ("what are the sorting algorithms that I know?", False),
    ("is that one faster?", True), ("what did that note say last week?", True),
    ("what is Dijkstra's algorithm?", False), ("summarize my notes on heaps", False),
    ("which sites did I visit yesterday?", False),
])
def test_follow_up(text, want):
    assert chat.follow_up(text, chat._when(text, TODAY)) is want


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


def test_a_chat_from_history_off_stays_in_memory_when_history_comes_back_on(vault, stream):
    s = stream("ollama", ["ok [1]"])
    off = {**chat.get_settings(), "history": False}
    cid, _ = _turn(vault, None, "car motor secret-off", s.runner(), off)
    _turn(vault, cid, "and the forum?", s.runner(), {**off, "history": True})
    _turn(vault, cid, "and a third?", s.runner(), {**off, "history": True})
    assert not (vault.root / chat.DIR).exists() or not any((vault.root / chat.DIR).iterdir())
    assert [m["text"] for m in chat.load(vault, cid) if m["role"] == "user"] == [
        "car motor secret-off", "and the forum?", "and a third?"]  # nothing lost either
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
    _, ev = _turn(vault, None, "car motor", dead)  # asked twice, then the pages searched
    assert ev[-1]["t"] == "done" and ev[-2]["searched"] and not ev[-2]["sources"]
    assert chat.SAID_NOTHING.format(model="m") in [e.get("s") for e in ev if e["t"] == "tok"]
    monkeypatch.setattr(semantic, "_embedder", None)
    _, ev = _turn(vault, None, "car motor", dead)
    assert ev[0]["code"] == "not_set_up"


class Silent(Stream):
    """Streams `first` to the first request and `then` to every later one."""

    def __init__(self, first, then):
        super().__init__("openai", first)
        self.then = then

    def lines(self):
        if len(self.bodies) > 1:
            self.pieces = self.then
        yield from super().lines()


def test_a_silent_model_is_asked_once_more(vault):
    s = Silent(["  ", "<|im_end|>"], ["Pistons [1].<|im_end|>"])
    try:
        cid, ev = _turn(vault, None, "car motor", s.runner())
    finally:
        s.close()
    assert len(s.bodies) == 2
    assert "gemma3:2b said nothing; asking once more" in [e.get("s") for e in ev
                                                          if e["t"] == "step"]
    assert ev[[e["t"] for e in ev].index("text")]["s"] == ""  # the empty first try is cleared
    assert [e["s"] for e in ev if e["t"] == "text"][-1] == "Pistons [1]."
    saved = chat.load(vault, cid)[-1]
    assert saved["text"] == "Pistons [1]." and "unanswered" not in saved


def test_still_nothing_shows_the_pages_searched_and_stays_out_of_history(vault, stream):
    s = Silent(["</think>"], ["<|eot_id|>"])
    try:
        cid, ev = _turn(vault, None, "car motor", s.runner())
    finally:
        s.close()
    assert len(s.bodies) == 2 and not [e for e in ev if e["t"] == "err"]
    assert ev[-2]["sources"] == [] and 1 <= len(ev[-2]["searched"]) <= chat.SEARCHED
    saved = chat.load(vault, cid)
    assert saved[-1]["text"] == chat.SAID_NOTHING.format(model="gemma3:2b")
    assert saved[-1]["unanswered"] is True
    ok = stream("ollama", ["Engines [1]."])
    _turn(vault, cid, "car motor torque", ok.runner())
    msgs = ok.bodies[0]["messages"]  # Cortex's note never reaches the model as its own words
    assert [m["role"] for m in msgs] == ["system", "user", "user"]  # as after a failed turn
    assert "gave no answer" not in json.dumps(msgs)


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


def _site_days(vault, days, llm=None):


    from cortex.browse import legacy, sites
    for rel, data in ((sites.DAYS, days), (sites.CACHE, {"llm": llm or {}, "hashes": {}})):
        p = vault.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")
    legacy.migrate(vault)  # the counts into the browsing store, where they are read


ALL_ON = {"notes": True, "chats": True, "browsing": True, "private": True}


def test_a_site_question_lists_every_site_once_with_its_counts(vault):
    _site_days(vault, {
        "2026-11-17": {"miruro.tv||1": {"n": 4}, "github.com||0": {"n": 9}},
        "2026-11-16": {"miruro.tv||1": {"n": 2}},
        "2026-10-01": {"old.example||0": {"n": 50}},
    }, llm={"miruro.tv": {"h": {"desc": "An **anime** streaming [site](x)"}}})
    got = (date(2026, 11, 16), date(2026, 11, 17), "")
    out = chat.site_list(vault, "list all the unique sites I visited", got, ALL_ON, TODAY)
    assert out.startswith("**2 sites** from 16 Nov to 17 Nov")
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert lines == ["- **github.com** · programming · 9 visits on 1 day",
                     "- **miruro.tv** · video · 6 visits on 2 days: An anime streaming site(x)"]
    assert "old.example" not in out
    assert "Don't save (adult" in out          # the default skips adult: never stored, said so
    no_private = dict(ALL_ON, private=False)
    assert "miruro" not in chat.site_list(vault, "which sites did I visit", got, no_private)
    assert chat.site_list(vault, "what did I read about sites of battles?", got, ALL_ON) is None
    assert chat.site_list(vault, "what websites did I go to?", got, ALL_ON) is not None
    assert chat.site_list(vault, "what did I learn yesterday?", got, ALL_ON) is None


def test_a_named_browser_counts_only_its_visits(vault):
    _site_days(vault, {
        # ids are source keys, as sources.read stamps them; one profile per browser or several
        "2026-11-17": {"miruro.tv||0": {"n": 5, "w": {"comet/Default": 2, "comet/Work": 1,
                                                        "chrome/Default": 2}},
                       "github.com||0": {"n": 4, "w": {"chrome/Default": 4}},
                       "x.example||0": {"n": 2, "w": {"comet": 9}}},  # a forget lowered n
        "2026-11-16": {"old.example||0": {"n": 7}},  # imported before browsers were kept
    })
    got = (date(2026, 11, 16), date(2026, 11, 17), "")
    out = chat.site_list(vault, "which sites did I visit in Comet?", got, ALL_ON, TODAY)
    assert out.startswith("**2 sites** in Comet from 16 Nov to 17 Nov")
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert lines == ["- **miruro.tv** · video · 3 visits on 1 day",
                     "- **x.example** · 2 visits on 1 day"]
    assert "7 visits in this range were imported before" in out


def test_a_site_question_before_the_stored_days_says_so(vault):
    _site_days(vault, {"2026-11-17": {"github.com||0": {"n": 1}}})
    got = chat._when("which sites did I visit in the past 4 to 5 months?", date(2026, 11, 19))
    assert got[0] == date(2026, 6, 23)  # 150 days, today the last
    out = chat.site_list(vault, "which sites did I visit in the past 4 to 5 months?", got, ALL_ON,
                         date(2026, 11, 19))
    assert "**1 site** from 23 Jun to 19 Nov" in out and "from 2026-11-17 on" in out


def test_reply_answers_a_site_question_without_the_model(vault, stream):
    _site_days(vault, {"2026-11-17": {"github.com||0": {"n": 3}}})
    s = stream("ollama", ["never used"])
    _, events = _turn(vault, None, "which sites did I visit yesterday?", s.runner())
    steps = [e["s"] for e in events if e["t"] == "step"]
    assert steps == ["Counting the sites you visited from 17 Nov"]
    assert "**github.com** · programming · 3 visits on 1 day" in "".join(e["s"] for e in events
                                                         if e["t"] == "tok")


@pytest.mark.parametrize("bad", [{"n": "abc"}, {"n": [1]}, {"n": 2, "w": {"comet": "x"}},
                                 {"n": 2, "p": [1, 2]}, {"n": 2, "p": 5}, "junk"])
def test_a_malformed_entry_is_skipped_not_the_answer(vault, home, bad):
    (home / ".cortex").mkdir(exist_ok=True)
    (home / ".cortex" / "config.yaml").write_text(  # a listed subdomain: page URLs are read
        "browse:\n  private_domains: [docs.bad.example]\n", encoding="utf-8")
    _site_days(vault, {"2026-11-17": {"github.com||0": {"n": 3, "w": {"comet": 3}},
                                      "bad.example||0": bad, "nopipe": {"n": 1}, "x||": {}}},
               llm={"github.com": {"v": "a string"}, "bad.example": "a string"})
    got = (date(2026, 11, 17), date(2026, 11, 17), "")
    for q in ("which sites did I visit?", "which sites did I visit in Comet?"):
        assert "- **github.com** · programming · 3 visits" in chat.site_list(vault, q, got, ALL_ON)
    day = date(2026, 11, 17)
    assert "**github.com**" in chat.digest(vault, day, day, ALL_ON, TODAY)


def test_reply_sends_the_step_before_counting(vault, monkeypatch):
    _site_days(vault, {"2026-11-17": {"github.com||0": {"n": 3}}})
    seen, real = [], chat.site_list

    def site_list(*a):
        seen.extend(e["t"] for e in events)
        return real(*a)
    monkeypatch.setattr(chat, "site_list", site_list)
    events = []
    chat.reply(vault, None, "which sites did I visit yesterday?", None, events.append,
               threading.Event(), chat.get_settings(), today=TODAY)
    assert seen == ["start", "step"] and events[-1]["t"] == "done"
    stop = threading.Event()
    monkeypatch.setattr(chat, "site_list", lambda *a: stop.set() or real(*a))
    events = []
    chat.reply(vault, None, "which sites did I visit yesterday?", None, events.append, stop,
               chat.get_settings(), today=TODAY)
    assert "tok" not in [e["t"] for e in events] and events[-1]["stopped"] is True


def test_a_looping_answer_is_cut_to_its_lines_once():
    line = "- YouTube (Private): watched videos on many topics"
    text = "\n".join(["Intro", line, "- Miruro.tv: anime", line, line])
    assert chat._looping(text) and not chat._looping("\n".join(["Intro", line, line]))
    assert chat._once(text) == "\n".join(["Intro", line, "- Miruro.tv: anime"])


# ---- study questions from the learner model --------------------------------------------------

@pytest.mark.parametrize("text, want", [
    ("What should I study today?", "plan"), ("what's my plan", "plan"),
    ("What's next?", "plan"), ("what should i work on next", "plan"),
    ("Today's plan", "plan"), ("show me my study plan for today", "plan"),
    ("What's due?", "due"), ("what should I review today?", "due"),
    ("Is anything due for review?", "due"), ("what do I need to revise", "due"),
    ("What are my weak topics?", "weak"), ("Where am I weak?", "weak"),
    ("What mistakes do I keep making?", "weak"), ("what are my recurring mistakes", "weak"),
    ("What did I study yesterday?", "range"), ("what have I studied this week?", "range"),
    ("What did I study on 12 Nov?", "range"), ("which concepts did I practise last week", "range"),
    ("How am I doing?", "progress"), ("how's my progress so far", "progress"),
    ("what is my progress", "progress"),
    ("What is Dijkstra's algorithm?", None), ("What did I read about car motors yesterday?", None),
    ("what is due this week?", "due"), ("What's due tomorrow?", "due"),
    ("what should I review tomorrow", "due"), ("is anything due this week", "due"),
    ("What did I study about graphs yesterday?", None),
    ("What did I learn yesterday?", "digest"),  # studied and browsed: the digest has both
    ("what should I study for heaps", None), ("what's the plan for the Rust rewrite?", None),
    ("What is due process?", None), ("what are weak references in Java?", None),
    ("How am I doing on two pointers?", None), ("what did I study", None),
    ("which sites did I visit yesterday?", None), ("what mistakes do students make in DP?", None),
    ("plan a trip to Goa", None), ("how is progress measured in agile?", None),
])
def test_study_questions_caught_and_not(text, want):
    assert chat_study.kind(text, chat._when(text, TODAY), TODAY) == want


@pytest.mark.parametrize("text", ["What's next?", "what next", "so what next?"])
def test_what_next_is_the_plan_only_when_it_opens_the_chat(text):
    assert chat_study.kind(text, None, TODAY) == "plan"
    assert chat_study.kind(text, None, TODAY, first=False) is None


def _study_events(vault):
    log = EventLog(vault)

    def at(day):
        return datetime(2026, 11, day, 12).astimezone()  # local noon: that local day anywhere

    log.append(session="cli", kind="grade", source="tutor", now=at(15),
               fields={"object": "concept:two-pointers", "verdict": "wrong", "confidence": 4,
                       "mistakes": ["dsa.off-by-one"]})
    log.append(session="cli", kind="taught", source="tutor", now=at(17),
               fields={"object": "concept:arrays-basics"})
    log.append(session="cli", kind="grade", source="tutor", now=at(17),
               fields={"object": "concept:arrays-basics", "verdict": "correct"})
    log.append(session="cli", kind="ask", source="tutor", now=at(17),
               fields={"object": "concept:two-pointers", "question": "Why sort first?"})
    log.append(session="cli", kind="grade", source="tutor", now=at(17),  # a private concept
               fields={"object": "concept:secret-diary", "verdict": "wrong", "confidence": 4})


def test_study_answers_come_from_the_learner_model(vault):
    _study_events(vault)

    def ask_(q):
        return chat.study_answer(vault, q, chat._when(q, TODAY), TODAY)

    plan = ask_("What should I study today?")
    assert plan.startswith("**Today's plan**")
    assert "Drill set A" in plan and "Day 2" not in plan  # the plan's next steps, no days
    weak = ask_("Where am I weak?")
    assert "**Two pointers** · a confident miss on 2026-11-15, 1 open mistake" in weak
    assert "⚑ DSA: off-by-one at the loop end · seen 1×, last on 2026-11-15" in weak
    day = ask_("What did I study yesterday?")
    assert day.startswith("**2 concepts** studied on 17 Nov")
    assert "**Array basics** · taught; 1 answer graded (1 correct)" in day
    assert "**Two pointers** · asked 1 question" in day
    progress = ask_("How am I doing?")
    due = ask_("what's due?")
    assert "**2 concepts** in Cortex" in progress
    for out in (plan, weak, day, progress, due):
        assert "Private concept" not in out and "secret" not in out  # D1: hidden as planned
        assert "predicted" not in out and "interval" not in out
    assert ask_("What is Dijkstra's algorithm?") is None
    assert ask_("What did I study on 3 Nov?") == "Cortex has no study recorded on 3 Nov."


def test_reply_answers_a_study_question_without_the_model(vault, stream):
    _study_events(vault)
    s = stream("ollama", ["never used"])
    cid, events = _turn(vault, None, "What did I study yesterday?", s.runner())
    assert [e["s"] for e in events if e["t"] == "step"] == ["Reading what you studied from 17 Nov"]
    text = "".join(e["s"] for e in events if e["t"] == "tok")
    assert text.startswith("**3 concepts** studied on 17 Nov") and s.bodies == []
    assert "**Private concept**" in text  # the private switch (on by default): the local side
    saved = chat.load(vault, cid)
    assert saved[-1]["role"] == "assistant" and saved[-1]["text"] == text
    assert saved[-1]["sources"] == [] and "searched" not in saved[-1]
    off = dict(chat.get_settings(), sources=dict(ALL_ON, private=False))
    _, events = _turn(vault, None, "What did I study yesterday?", s.runner(), settings=off)
    text = "".join(e["s"] for e in events if e["t"] == "tok")
    assert text.startswith("**2 concepts** studied on 17 Nov") and "Private" not in text


def test_a_data_answer_needs_no_model(vault, monkeypatch):
    _site_days(vault, {"2026-11-17": {"github.com||0": {"n": 3}}})
    monkeypatch.setattr(semantic, "load_embedder", lambda *a, **k: None)
    _, events = _turn(vault, None, "which sites did I visit yesterday?", None)
    assert not [e for e in events if e["t"] == "err"]
    assert "github.com" in "".join(e["s"] for e in events if e["t"] == "tok")
    assert [e["model"] for e in events if e["t"] in ("start", "done")] == ["Cortex", "Cortex"]
    _, events = _turn(vault, None, "what is Dijkstra's algorithm?", None)
    assert [e["code"] for e in events if e["t"] == "err"] == ["not_set_up"]


# ---- the digest: a day or week from Cortex's own data ----------------------------------------

@pytest.mark.parametrize("text, want", [
    ("What did I do yesterday?", "digest"), ("summarize my day", "digest"),
    ("Summarize my week", "digest"), ("What happened this week?", "digest"),
    ("recap last week", "digest"), ("Give me a summary of yesterday", "digest"),
    ("What have I been up to?", "digest"), ("what have I been up to this week?", "digest"),
    ("so what did I do over the past week", "digest"), ("what have I done today", "digest"),
    ("What did I study yesterday?", "range"),
    ("What did I read about engines yesterday?", None), ("summarize this article", None),
    ("what did I do on reddit yesterday?", None), ("what happened", None),
    ("summarize", None), ("what did I do", None), ("recap the French revolution", None),
    ("what happened in the news yesterday?", None),
    ("what did I learn yesterday?", "digest"), ("what have I learnt this week?", "digest"),
    ("What did I learn last night?", "digest"), ("what did I do on Monday?", "digest"),
    ("recap last weekend", "digest"), ("what happened on 12 Dec 2025?", "digest"),
    ("what did I learn about Dijkstra?", None), ("what did I learn", None),
    # history, not the learner's days: the model answers
    ("what happened on 11 September 2001?", None), ("what happened in November 1918?", None),
    ("recap of 1 May 1886", None), ("what happened on 12 Nov 2025?", None),
])
def test_digest_questions_caught_and_not(text, want):
    assert chat_study.kind(text, chat._when(text, TODAY), TODAY) == want


def test_digest_days_default_to_today_or_this_week():
    def days(q):
        return chat_study.digest_days(q, chat._when(q, TODAY), TODAY)
    assert days("summarize my day") == (TODAY, TODAY, "day")
    assert days("what have I been up to?") == days("summarize my week") == (date(2026, 11, 16),
                                                                            TODAY, "week")
    assert days("recap last week") == (date(2026, 11, 9), date(2026, 11, 15), "week")
    assert days("summarize the last 3 days") == (date(2026, 11, 16), TODAY, "days")
    assert days("what did I do 2 days ago?")[2] == "days"
    assert days("what did I do two weeks ago?")[2] == "days"
    assert days("what did I do yesterday?")[2] == "day"
    assert days("what is Dijkstra's algorithm?") is None
    q = "what did I do yesterday?"  # chat.digest answers it, under the source switches
    assert chat.study_answer(None, q, chat._when(q, TODAY), TODAY) is None


def _digest_vault(vault):
    _study_events(vault)
    day = {f"site{i:02}.example||0": {"n": 20 - i} for i in range(12)}
    day["miruro.tv||1"] = {"n": 30}  # a private category: only with the private switch
    _site_days(vault, {"2026-11-17": day, "2026-11-10": {"old.example||0": {"n": 99}}})
    root = vault.root
    note(root, "notes/rust-ownership.md", "title: Rust **ownership**\ndate: 2026-11-17\n",
         "# Rust ownership\n\nBorrowing rules.")
    note(root, "notes/diary-entry.md", "title: Diary entry\nprivate: true\ndate: 2026-11-17\n",
         "# Diary\n\nSecret things.")
    note(root, "resources/chat-heaps.md", "title: Heaps chat\nkind: resource\n",
         "# Heaps chat\n\nTimeline: [[2026-11-01]] · Source: Gemini · Date: 2026-11-17\n")
    note(root, "journal/2026-11-17.md", "title: Journal 2026-11-17\nkind: journal\n"
         "date: 2026-11-17\n", "## Log\n- 09:30 Started **graphs** [revision](x)\n- 21:00 Tired\n")
    return vault


def test_a_digest_lists_each_section_under_the_switches(vault):
    _digest_vault(vault)
    d = date(2026, 11, 17)
    out = chat.digest(vault, d, d, ALL_ON, TODAY)
    assert out.startswith("**Your day** on 17 Nov:")
    assert "**3 concepts** studied on 17 Nov" in out and "**Array basics**" in out
    assert "**Private concept**" in out  # the private switch: the local side names it
    assert "**13 sites** visited (204 visits), most first:" in out
    assert "- **miruro.tv** · video · 30 visits" in out
    assert "- and 3 more sites" in out and "site10.example" not in out  # capped at 10
    assert "old.example" not in out
    assert "**1 note** written or changed:\n\n- Rust ownership" in out  # markdown stripped
    assert "**1 private note** written or changed:\n\n- Diary entry" in out  # its own section
    assert "**1 saved AI chat**:\n\n- Heaps chat" in out
    assert "**Journal** (2 lines):\n\n- 09:30 Started graphs revision\n- 21:00 Tired" in out
    assert out.endswith("Not included: site categories set to Don't save (adult), never stored.")
    shut = chat.digest(vault, d, d, dict(ALL_ON, private=False), TODAY)
    assert "Diary entry" not in shut and "miruro" not in shut and "Rust ownership" in shut
    assert "**2 concepts** studied on 17 Nov" in shut and "Private concept" not in shut  # D1
    assert "private notes, subjects and browsing (switched off in Chat settings)" in shut
    only_private = chat.digest(vault, d, d, dict(ALL_ON, notes=False, chats=False), TODAY)
    assert "**1 private note** written or changed:\n\n- Diary entry" in only_private
    assert "**1 note**" not in only_private and "Rust" not in only_private
    assert "studied" not in only_private and "Journal" not in only_private
    assert only_private.endswith("Not included: notes, studying and journal, saved AI chats "
                                 "(switched off in Chat settings); site categories set to "
                                 "Don't save (adult), never stored.")
    bare = chat.digest(vault, d, d, dict(ALL_ON, notes=False, chats=False, browsing=False,
                                         private=False), TODAY)
    assert "sites" not in bare and "Rust" not in bare and "Journal" not in bare
    assert "Heaps" not in bare and "studied" not in bare  # concepts are notes: switched off
    empty = chat.digest(vault, date(2026, 11, 3), date(2026, 11, 3), ALL_ON, TODAY)
    assert empty.startswith("Cortex has nothing recorded on 3 Nov. Not included:")


def test_reply_answers_a_digest_without_a_runner_or_embedder(vault, monkeypatch):
    _digest_vault(vault)
    monkeypatch.setattr(semantic, "load_embedder", lambda *a, **k: None)
    _, events = _turn(vault, None, "What did I do yesterday?", None)
    assert not [e for e in events if e["t"] == "err"]
    assert [e["s"] for e in events if e["t"] == "step"] == ["Putting together your day on 17 Nov"]
    assert [e["model"] for e in events if e["t"] in ("start", "done")] == ["Cortex", "Cortex"]
    text = "".join(e["s"] for e in events if e["t"] == "tok")
    assert text.startswith("**Your day** on 17 Nov:") and "Rust ownership" in text
    _, events = _turn(vault, None, "summarize my week", None)
    assert [e["s"] for e in events if e["t"] == "step"] == [
        "Putting together your week from 16 Nov to 18 Nov"]


def test_cortexs_own_answer_gets_no_quote_check(vault, monkeypatch):
    """Audit 21 Nov C7: a quoted title in the digest is not flagged "Quote not found"."""
    note(vault.root, "notes/ddia.md",
         'title: \'Notes on "Designing Data-Intensive Apps"\'\ndate: 2026-11-17\n',
         "# DDIA\n\nReplication.")
    monkeypatch.setattr(semantic, "load_embedder", lambda *a, **k: None)
    cid, events = _turn(vault, None, "What did I do yesterday?", None)
    text = "".join(e["s"] for e in events if e["t"] == "tok")
    assert '"Designing Data-Intensive Apps"' in text
    assert "unquoted" not in events[-2] and "unquoted" not in chat.load(vault, cid)[1]


@pytest.mark.parametrize("q,caught", [
    ("which sites did I visit yesterday?", True), ("list all the unique sites I visited", True),
    ("what websites did I go to?", True), ("sites I've been on this week", True),
    ("my most visited sites", True), ("What is the domain of every rational function?", False),
    ("List the binding sites used by hemoglobin", False), ("what is a website?", False),
    ("which sites did I visit in Comet last week?", True), ("which sites did I use on Edge?", True),
    ("how many sites did I visit in the past 4 to 5 months?", True),
    ("what's a good site I can use to practise SQL?", False),
    ("can you suggest websites I could use for revision?", False),
    ("which sites should I use?", False), ("which sites do I use for DSA?", False),
    ("which sites did I visit to learn edge detection?", False)])
def test_only_a_first_person_browsing_question_gets_the_site_list(vault, q, caught):
    _site_days(vault, {"2026-11-17": {"github.com||0": {"n": 1}}})
    assert (chat.site_list(vault, q, None, ALL_ON) is not None) is caught


def test_a_visible_site_never_shows_the_private_side_description(vault):
    from pathlib import Path
    cfg = Path.home() / ".cortex" / "config.yaml"  # the test's own home
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("browse:\n  visible_to_claude: true\n", encoding="utf-8")
    _site_days(vault, {"2026-11-17": {"x.example||0": {"n": 2}}},
               llm={"x.example": {"h": {"desc": "Made from private titles"}}})
    out = chat.site_list(vault, "which sites did I visit?", None, ALL_ON)
    assert "x.example" in out and "private titles" not in out


def test_study_answers_follow_the_notes_switch(vault):
    off = dict(chat.get_settings(), sources=dict(ALL_ON, notes=False))
    _, events = _turn(vault, None, "where am I weak?", None, settings=off)
    text = "".join(e["s"] for e in events if e["t"] == "tok")
    assert text == chat.NOTES_OFF


def test_the_runner_is_only_looked_up_when_the_model_is_needed(vault):
    asked = []

    def find():
        asked.append(1)
        raise ValueError("only a loopback runner")
    _site_days(vault, {"2026-11-17": {"github.com||0": {"n": 1}}})
    _, events = _turn(vault, None, "which sites did I visit yesterday?", find)
    assert not asked and [e["model"] for e in events if e["t"] == "start"] == ["Cortex"]
    _, events = _turn(vault, None, "what is Dijkstra's algorithm?", find)
    assert asked == [1]
    assert [(e["code"], e["message"]) for e in events if e["t"] == "err"] == [
        ("no_local_model", "only a loopback runner")]


def test_a_site_the_model_filed_under_a_dont_save_category_is_never_listed(vault):
    _site_days(vault, {"2026-11-17": {"plain.example||0": {"n": 3}, "github.com||0": {"n": 1}}},
               llm={"plain.example": {"category": "adult"}})
    out = chat.site_list(vault, "which sites did I visit?", None, ALL_ON)
    assert "github.com" in out and "plain.example" not in out
    d = date(2026, 11, 17)
    assert "plain.example" not in chat.digest(vault, d, d, ALL_ON, TODAY)

def test_a_browser_is_a_filter_only_after_in_or_on(vault):
    _site_days(vault, {"2026-11-17": {"github.com||0": {"n": 4, "w": {"chrome": 4}}}})
    assert chat.site_list(vault, "which sites did I visit on Edge?", None, ALL_ON).startswith(
        "No sites are stored in Edge")
    assert chat.site_list(vault, "which sites did I use in Google Chrome?", None,
                          ALL_ON).startswith("**1 site** in Chrome")


def test_today_starts_at_four_in_the_morning(vault, monkeypatch):
    assert chat._today(datetime(2026, 11, 20, 1, 30).astimezone()) == date(2026, 11, 19)
    assert chat._today(datetime(2026, 11, 20, 4, 30).astimezone()) == date(2026, 11, 20)
    _study_events(vault)
    monkeypatch.setattr(chat, "_today", lambda now=None: date(2026, 11, 17))  # 01:30 on the 18th
    events = []
    chat.reply(vault, None, "What did I study today?", None, events.append, threading.Event(),
               chat.get_settings())
    assert [e["s"] for e in events if e["t"] == "step"] == ["Reading what you studied from 17 Nov"]
    # 3: the private switch is on by default, so the private concept counts too
    assert "**3 concepts** studied on 17 Nov" in "".join(e["s"] for e in events if e["t"] == "tok")


def test_an_undated_note_changed_before_four_counts_on_the_day_before(vault):
    import os
    note(vault.root, "notes/late.md", "kind: note\ntitle: Late\n", "# Late\n\nCar motor gears.")
    t = datetime(2026, 11, 16, 1, 30).timestamp()
    os.utime(vault.root / "notes/late.md", (t, t))
    found = ask._passages(vault, "car motor", 8, between=("2026-11-15", "2026-11-15"),
                          topic="car motor")
    assert next(p for p in found if p["path"] == "notes/late.md")["day"] == "2026-11-15"


def test_a_date_in_another_year_shows_its_year():
    assert chat._span(date(2001, 9, 11), date(2001, 9, 11), TODAY) == "11 Sep 2001"
    assert chat._span(date(2025, 12, 28), date(2026, 1, 3), TODAY) == "28 Dec 2025 to 3 Jan"
    assert chat._span(date(2026, 11, 10), date(2026, 11, 16), TODAY) == "10 Nov to 16 Nov"
    q = "what did I study on 3 Dec?"  # last year's 3 Dec
    assert chat_study.kind(q, chat._when(q, TODAY), TODAY) == "range"


def test_due_for_a_range_counts_reviews_due_by_its_last_day(vault):
    _study_events(vault)

    def ask_(q):
        return chat.study_answer(vault, q, chat._when(q, TODAY), TODAY)
    assert ask_("what's due?").startswith("**1 concept due for review** today:")
    assert ask_("what is due tomorrow?").startswith("**2 concepts due for review** by 19 Nov:")
    assert ask_("what is due this week?").startswith("**2 concepts due for review** by 22 Nov:")


def test_due_on_a_day_looks_forward(vault):
    _study_events(vault)  # TODAY is Wednesday 18 Nov 2026

    def by(q, today=TODAY):
        got = chat._when(q, today)
        assert chat_study.kind(q, got, today) == "due", q
        return chat_study._due_by(q, chat_study._ahead(q, got, today))
    assert by("what is due on Friday?") == date(2026, 11, 20)  # not last Friday
    assert by("what is due this weekend?") == date(2026, 11, 22)
    assert by("what is due this weekend?", date(2026, 11, 22)) == date(2026, 11, 22)  # Sunday
    assert by("what is due on 25 Nov?") == date(2026, 11, 25)  # not 2025
    assert by("what is due in December?") == date(2026, 12, 31)
    assert by("what is due on 17 Nov 2026?") == date(2026, 11, 17)  # a stated year stays
    # moved a year on, a month keeps its real days and 29 Feb finds the next 29 Feb
    assert by("what is due in February?", date(2027, 11, 18)) == date(2028, 2, 29)
    assert by("what is due in February?", date(2028, 11, 18)) == date(2029, 2, 28)
    assert by("what is due on 28 Feb?", date(2027, 11, 18)) == date(2028, 2, 28)
    assert by("what is due on 29 Feb?", date(2028, 3, 5)) == date(2032, 2, 29)
    got = chat._when("last Friday", TODAY)
    assert chat_study._ahead("last Friday", got, TODAY) == got
    assert chat.study_answer(vault, "what is due on Friday?",
                             chat._when("what is due on Friday?", TODAY), TODAY).startswith(
        "**2 concepts due for review** by 20 Nov:")


def test_a_site_question_after_a_dated_one_shows_its_own_dates(vault):
    cid, _ = _turn(vault, None, "what did I study yesterday?", None)
    _, events = _turn(vault, cid, "which sites did I visit?", None)
    assert [e["s"] for e in events if e["t"] == "step"] == ["Counting the sites you visited"]


def test_a_week_digest_on_a_monday_is_your_week_so_far(vault, monkeypatch):
    monday = date(2026, 11, 16)
    _site_days(vault, {"2026-11-16": {"github.com||0": {"n": 1}},
                       "2026-11-10": {"github.com||0": {"n": 1}}})
    assert chat.digest(vault, monday, monday, ALL_ON, monday, "week").startswith(
        "**Your week so far** on 16 Nov:")  # the fixture's Kitchen day note and the site
    assert chat.digest(vault, date(2026, 11, 9), date(2026, 11, 15), ALL_ON, monday,
                       "week").startswith("**Your week** from 9 Nov to 15 Nov:")
    monkeypatch.setattr(semantic, "load_embedder", lambda *a, **k: None)

    def steps(q, today=TODAY):
        events = []
        chat.reply(vault, None, q, None, events.append, threading.Event(), chat.get_settings(),
                   today=today)
        return [e["s"] for e in events if e["t"] == "step"]
    assert steps("summarize this week", monday) == ["Putting together your week on 16 Nov"]
    assert steps("summarize the last 3 days") == [
        "Putting together your days from 16 Nov to 18 Nov"]
    assert steps("what did I do 2 days ago?") == [
        "Putting together your days from 15 Nov to 17 Nov"]
    assert steps("what did I do the day before yesterday?") == [
        "Putting together your day on 16 Nov"]


def test_what_next_mid_chat_goes_to_the_model(vault, stream):
    s = stream("ollama", ["Then the inner loop [1]."])
    cid, events = _turn(vault, None, "What's next?", s.runner())
    assert [e["model"] for e in events if e["t"] == "start"] == ["Cortex"]
    _, events = _turn(vault, cid, "what next?", s.runner())
    assert [e["model"] for e in events if e["t"] == "start"] == [s.runner().model]


def test_a_future_date_does_not_narrow_the_search(vault, stream, monkeypatch):
    s = stream("ollama", ["Answer [1]."])
    asked = []
    real = ask._passages

    def passages(v, q, k, **kw):
        asked.append(kw["between"])
        return real(v, q, k, **kw)
    monkeypatch.setattr(ask, "_passages", passages)
    _turn(vault, None, "explain car motors for tomorrow's exam", s.runner())
    _turn(vault, None, "what did I read about car motors yesterday?", s.runner())
    assert asked == [None, ("2026-11-17", "2026-11-17")]


# ---- the router, follow-ups on the assistant's list, two-part questions (R4) -------------------

@pytest.mark.parametrize("text, leans, want", [
    ("which sites did I visit yesterday?", False, "Counting the sites you visited from 17 Nov"),
    ("what did I do yesterday?", False, "Putting together your day on 17 Nov"),
    ("what is due?", False, "Reading your due reviews"),
    ("how many times did I visit github?", False, 'Counting "github"'),
    ("how many times did I visit github?", True, None),  # a follow-up: no typed answer
    ("what is Dijkstra's algorithm?", False, None),
])
def test_the_router_picks_one_data_answer(vault, text, leans, want):
    r = chat._route(vault, text, chat._when(text, TODAY), TODAY, True, chat.get_settings(),
                    leans)
    assert (r.step if r else None) == want
    assert r is None or (r.data is None) is (r.fq is not None)


ASSESSMENTS = "1. Apex Systems OA (2026-11-24)\n2. Nimbus Tech Interview (2026-12-03)"


def test_list_items_are_plain_text_with_their_trailing_parentheses():
    assert chat.list_items("Here:\n" + ASSESSMENTS + "\n\nThat is all.") == [
        "Apex Systems OA (2026-11-24)", "Nimbus Tech Interview (2026-12-03)"]
    assert chat.list_items("- **VeloxKV** [1]\n  - a nested line\n* `TinySQL` (14 commits)\n"
                           "• [notes-cli](https://x.example)\n2) Last") == [
        "VeloxKV", "TinySQL (14 commits)", "notes-cli", "Last"]


@pytest.mark.parametrize("text, items, want", [
    ("And what does the second one test?", 2, "Nimbus Tech Interview (2026-12-03)"),
    ("what does the 2nd cover?", 2, "Nimbus Tech Interview (2026-12-03)"),
    ("tell me about number 1", 2, "Apex Systems OA (2026-11-24)"),
    ("and #2?", 2, "Nimbus Tech Interview (2026-12-03)"),
    ("what about the last one?", 2, "Nimbus Tech Interview (2026-12-03)"),
    ("and the first?", 2, "Apex Systems OA (2026-11-24)"),
    ("and the third one?", 2, None),  # the list has no third
    ("what does it test?", 1, "Apex Systems OA (2026-11-24)"),  # a pronoun, one item
    ("what does it test?", 2, None),  # a pronoun, two items: no guess
    ("when was the last time I studied heaps?", 2, None),
    ("what is Dijkstra's algorithm?", 2, None),
])
def test_a_follow_up_picks_an_item_of_the_last_list(text, items, want):
    lst = chat.list_items(ASSESSMENTS)[:items]
    assert chat.picked(text, chat._when(text, TODAY), lst, "List my assessments") == want


def test_the_other_one_is_the_item_the_last_question_did_not_name():
    lst = chat.list_items(ASSESSMENTS)
    assert chat.picked("and the other one?", None, lst, "what does Apex Systems OA test?") == \
        lst[1]
    assert chat.picked("and the other one?", None, lst, "list them") is None


def _prior(vault, turns):
    cid = chat.new()
    chat._save(vault, cid, [{"role": r, "text": t, "ts": chat._now(),
                             **({"sources": [], "switches": dict(ask.SOURCES)}
                                if r == "assistant" else {})} for r, t in turns], True)
    return cid


def _asked(monkeypatch):
    asked = []
    real = ask._passages
    monkeypatch.setattr(ask, "_passages", lambda v, q, k, **kw: asked.append(q) or real(v, q, k,
                                                                                      **kw))
    return asked


@pytest.mark.parametrize("prior, text, want", [
    ([("user", "List my two upcoming assessments in date order."), ("assistant", ASSESSMENTS)],
     "And what does the second one test?", "Nimbus Tech Interview"),  # Q38
    ([("user", "List my active Rust projects."),
      ("assistant", "1. VeloxKV (42 commits)\n2. TinySQL (14 commits)")],
     "What does my git repo summary say about the second one?", "TinySQL"),  # Q39
    ([("user", "my projects"), ("assistant", "- VeloxKV\n- TinySQL\n- notes-cli")],
     "and #2?", "TinySQL"),
    ([("user", "my projects"), ("assistant", "- VeloxKV\n- TinySQL\n- notes-cli")],
     "what about the last one?", "notes-cli"),
    ([("user", "my rust project"), ("assistant", "- VeloxKV (42 commits)")],
     "what does it store?", "VeloxKV"),
])
def test_the_search_holds_the_picked_item(vault, stream, monkeypatch, prior, text, want):
    s = stream("ollama", ["Answer [1]."])
    asked = _asked(monkeypatch)
    _turn(vault, _prior(vault, prior), text, s.runner())
    assert len(asked) == 1 and want in asked[0] and prior[0][1] not in asked[0]
    assert asked[0].startswith(text)


def test_a_new_subject_is_not_mixed_with_the_list(vault, stream, monkeypatch):
    s = stream("ollama", ["Answer [1]."])
    asked = _asked(monkeypatch)
    cid = _prior(vault, [("user", "List my assessments."), ("assistant", ASSESSMENTS)])
    _turn(vault, cid, "what is Dijkstra's algorithm?", s.runner())
    assert asked == ["what is Dijkstra's algorithm?"]


def test_a_list_made_with_a_switch_now_off_is_not_picked_from(vault, stream, monkeypatch):
    s = stream("ollama", ["Answer [1]."])
    asked = _asked(monkeypatch)
    cid = _prior(vault, [("user", "List my assessments."), ("assistant", ASSESSMENTS)])
    off = {**chat.get_settings(), "sources": {**ask.SOURCES, "private": False}}
    _turn(vault, cid, "And what does the second one test?", s.runner(), settings=off)
    assert "Nimbus" not in asked[0]


Q37 = ("With Private ON, what microcontroller does Project Kestrel use and how many times did "
       "I visit synthdiy.example?")


@pytest.mark.parametrize("text, want", [
    (Q37, ["With Private ON, what microcontroller does Project Kestrel use",
           "how many times did I visit synthdiy.example?"]),
    ("Who is Arjun Kulkarni and when do we sync?", ["Who is Arjun Kulkarni",
                                                     "when do we sync?"]),
    ("What storage engine did I choose for VeloxKV, and did I reverse it later?",
     ["What storage engine did I choose for VeloxKV", "did I reverse it later?"]),
    ("What are the pros and cons of B-trees?", []),
    ("compare heaps and stacks", []),
    ("Compare my notes on rust and what go does", []),
    ("what is the difference between heaps and stacks?", []),
    ("rust vs go and which is faster?", []),
    ("Where and when did I meet Priya?", []),
    ("What do my PDFs and book highlights say about MVCC and write skew?", []),
    ("which sites did I visit yesterday; what is due?", ["which sites did I visit yesterday",
                                                         "what is due?"]),
])
def test_halves(text, want):
    assert chat.halves(text, lambda h: False) == want


def test_a_second_half_that_routes_is_a_question():
    assert chat.halves("what is a heap and list my projects", lambda h: h.startswith(
        "list")) == ["what is a heap", "list my projects"]
    assert chat.halves("what is a heap and list my projects", lambda h: False) == []
    assert chat.halves("tell me about heaps and list my projects", lambda h: True) == []


def test_two_routed_halves_are_both_answered_by_code(vault):
    events = []
    chat.reply(vault, None, "how many times did I visit github and how many times did I "
               "visit gitlab?", None, events.append, threading.Event(), chat.get_settings(),
               today=TODAY)
    assert [e["s"] for e in events if e["t"] == "step"] == ['Counting "github"',
                                                            'Counting "gitlab"']
    answer = "".join(e["s"] for e in events if e["t"] == "tok")
    assert answer == ('I found nothing about "github" in your archive.\n\n'
                      'I found nothing about "gitlab" in your archive.')
    assert events[0]["model"] == "Cortex" and events[-1]["t"] == "done"


def test_a_routed_half_goes_in_front_of_the_other_halfs_passages(vault, stream, monkeypatch):
    s = stream("ollama", ["A motor [1]; no visits."])
    asked = _asked(monkeypatch)
    text = "what is a car motor and how many times did I visit github?"
    cid, events = _turn(vault, None, text, s.runner())
    assert asked == ["what is a car motor"]  # Q37's shape: the other half searched alone
    kinds = [e["t"] for e in events]
    assert kinds.index("facts") < kinds.index("tok")
    user = s.bodies[0]["messages"][-1]["content"]
    assert user.startswith('<untrusted_note id="facts">\nFacts computed by Cortex:\nI found '
                           'nothing about "github"')
    assert user.endswith(f"Question: {text}" + chat.CITE)
    assert chat.load(vault, cid)[-1]["facts"]["query"] == "github"


def test_without_a_model_the_routed_half_is_shown_and_the_rest_says_why(vault, monkeypatch):
    events = []
    chat.reply(vault, None, "what is a car motor and how many times did I visit github?",
               None, events.append, threading.Event(), chat.get_settings(), today=TODAY)
    assert events[0]["model"] == "Cortex" and "err" not in [e["t"] for e in events]
    assert "".join(e["s"] for e in events if e["t"] == "tok") == (
        f'I found nothing about "github" in your archive.\n\n{ask.NO_MODEL}')
    monkeypatch.setattr(semantic, "_embedder", None)
    monkeypatch.setattr(semantic, "load_embedder", lambda: None)
    events = []
    chat.reply(vault, None, "what is a car motor and how many times did I visit github?",
               None, events.append, threading.Event(), chat.get_settings(), today=TODAY)
    assert "".join(e["s"] for e in events if e["t"] == "tok").endswith(semantic.NOT_SET_UP)


def test_unrouted_halves_take_half_the_passages_each(vault, stream, monkeypatch):
    s = stream("ollama", ["Answer [1]."])
    asked = []
    real = ask._passages

    def passages(v, q, k, **kw):
        asked.append((q, k))
        return real(v, q, k, **kw)
    monkeypatch.setattr(ask, "_passages", passages)
    _, events = _turn(vault, None, "what is a car motor and what pasta did I cook?",
                      s.runner())
    assert asked == [("what is a car motor", 3), ("what pasta did I cook?", 2)]
    src = next(e for e in events if e["t"] == "src")
    paths = [p["path"] for p in src["searched"] + src["sources"]]
    assert len(paths) == len(set(paths))  # merged without repeats
    asked.clear()
    cid, _ = _turn(vault, None, "who wrote the engines note and what did he say about it?",
                   s.runner())  # a new chat: "he" and "it" point at the first half
    assert asked[1][0] == "what did he say about it?\nwho wrote the engines note"
    asked.clear()
    _turn(vault, cid, "who wrote the kitchen note and what did he say about it?", s.runner())
    assert len(asked) == 1  # mid-chat they may point at the last turn: no split


# ---- passages: a me/ page's ## Past, and each passage's header (Q17-Q19) -----------------------

def _prefs(vault):
    note(vault.root, "me/preferences.md", "title: My preferences\nprivate: false\n",
         "# My preferences\n\n## Coding\n- Always use uv for car motor scripts ^c-new\n\n"
         "## Past\n- Use Pipenv for car motor scripts (until 2026-09-12 → ^c-new) ^c-old\n\n"
         "## Not me\n- Car motor racing\n")


@pytest.mark.parametrize("q", ["Which car motor tool do I prefer to use right now?",  # Q18
                               "How do I like car motor scripts?",  # Q17
                               "What are my coding rules for car motor scripts?"])  # Q19
def test_the_past_of_a_me_page_never_reaches_the_model(vault, stream, q):
    _prefs(vault)
    s = stream("ollama", ["uv [1]."])
    _turn(vault, None, q, s.runner())
    user = s.bodies[0]["messages"][-1]["content"]
    assert "Always use uv" in user and "Pipenv" not in user and "## Past" not in user
    assert "[undated | current]\nMy preferences\nFrom: your note" in user
    assert "Car motor racing" in user  # the next section stays


@pytest.mark.parametrize("q", ["Which car motor tool did I use before?",
                               "what did I prefer for car motor scripts?",
                               "what did I use to run car motor scripts with?"])
def test_a_question_about_the_past_keeps_it(vault, stream, q):
    _prefs(vault)
    s = stream("ollama", ["Pipenv [1]."])
    _turn(vault, None, q, s.runner())
    user = s.bodies[0]["messages"][-1]["content"]
    assert "Use Pipenv for car motor scripts" in user
    assert "[undated | past]\nMy preferences" in user


def test_every_passage_starts_with_its_day_and_status():
    p = {"path": "decisions/dec-a.md", "title": "A", "kind": "note", "day": "2026-07-05",
         "text": "x", "stale": True}
    assert ask.prompt([p], "q").startswith('<untrusted_note id="1">\n[2026-07-05 | replaced]\nA\n')
    assert ask.header({"path": "notes/b.md", "day": ""}) == "[undated]"
    assert ask.header({"path": "notes/b.md", "day": "2026-11-01"}) == "[2026-11-01]"


def test_a_superseded_decision_is_marked_stale(vault):
    note(vault.root, "decisions/dec-old.md", "title: Old car motor choice\nkind: decision\n"
         "status: superseded\ndate: 2026-07-05\n", "# Old car motor choice\n\nCar motor A.")
    found = ask._passages(vault, "car motor choice", 8, sources=ask.SOURCES)
    old = next(p for p in found if p["path"] == "decisions/dec-old.md")
    assert old["stale"] and ask.header(old) == "[2026-07-05 | replaced]"
