"""UX-B: "#" hands Chat a vault page (GET /api/chat/pages, the `pages` of a chat turn) and
Review cards carry their related pages. Made-up vaults only; no model (a fake loopback one)."""

import copy
import os
import statistics
import time

import pytest
from test_app import _serve, call, chat_stream, embed, fakes, llm  # noqa: F401  (fixtures)
from test_home import _big, _note

from cortex import ask, chat
from cortex.app import server
from cortex.events import EventLog
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")
OFF = {k: False for k in ("notes", "chats", "browsing", "private")}


@pytest.fixture
def vault(root_committed):
    (root_committed / ".cortex").mkdir(exist_ok=True)
    return root_committed


@pytest.fixture
def app(vault):
    srv, a = _serve(vault)
    yield a
    srv.shutdown()
    srv.server_close()


def pages(app, q: str) -> list[dict]:
    code, js, _, _ = call(app, "GET", "/api/chat/pages?q=" + q)
    assert code == 200, js
    return js["pages"]


def test_the_list_matches_titles_under_chats_switches(app, vault):
    _note(vault, "notes", "heap-sort", {"title": "Heap sort"}, "Sift down.\n")
    _note(vault, "notes", "min-heap", {"title": "Min heap basics"}, "Smallest on top.\n")
    _note(vault, "notes", "heap-diary", {"title": "Heap diary", "private": True}, "x\n")
    _note(vault, "notes", "stack", {"title": "Stacks"}, "Mentions heap in the body only.\n")
    later = time.time() + 60
    os.utime(vault / "notes" / "min-heap.md", (later, later))
    assert call(app, "GET", "/api/chat/pages?q=heap", token=False)[0] == 401
    got = pages(app, "heap")
    assert {p["id"] for p in got} == {"heap-sort", "min-heap"}  # never the private one
    assert {p["kind"] for p in got} == {"note"} and "Heap sort" in {p["title"] for p in got}
    assert [p["id"] for p in pages(app, "SORT%20heap")] == ["heap-sort"]  # any order, any case
    assert pages(app, "he%20ba")[0]["id"] == "min-heap"  # word prefixes
    newest = pages(app, "")
    assert newest[0]["id"] == "min-heap" and len(newest) <= chat.PICK
    assert not {"heap-diary", "secret-diary"} & {p["id"] for p in newest}
    # a page whose switch is off in Chat is never offered
    assert call(app, "POST", "/api/chat/settings", {"sources": {"notes": False}})[0] == 200
    assert pages(app, "heap") == [] and pages(app, "") == []


def test_picked_pages_lead_the_prompt_and_a_refused_one_is_never_read(app, embed, llm,  # noqa: F811
                                                                     vault, monkeypatch):
    _note(vault, "notes", "alpha", {"title": "Alpha plan"}, "CANARY-ALPHA-41 is the plan.\n")
    _note(vault, "notes", "secret-x", {"title": "Secret", "private": True},
          "CANARY-SECRET-73 stays home.\n")
    _note(vault, "notes", "gamma", {"title": "Gamma"}, "CANARY-GAMMA-58 here.\n")
    read = []
    real = ask._page
    monkeypatch.setattr(ask, "_page", lambda v, p, i: read.append(i) or real(v, p, i))
    assert call(app, "POST", "/api/chat/settings", {"sources": {"private": False}})[0] == 200
    ev = chat_stream(app, {"text": "what is the plan?",
                           "pages": ["alpha", "secret-x", "nope", 5, "alpha"]})[2]
    assert ev[-1]["t"] == "done", ev
    sent = str(llm.bodies[-1]["messages"][-1]["content"])
    assert sent.count("CANARY-ALPHA-41") == 1 and "Alpha plan" in sent
    assert sent.index("CANARY-ALPHA-41") < sent.index("Question:")
    assert '<untrusted_note id="1">' in sent  # wrapped as a file's passage is
    assert "secret-x" not in read and "CANARY-SECRET-73" not in str(llm.bodies)
    src = next(e for e in ev if e["t"] == "src")["sources"]
    assert src[0]["n"] == 1 and src[0]["path"] == "notes/alpha.md"  # the sources show it
    # its switch turned off: the picked page is dropped unread
    read.clear()
    assert call(app, "POST", "/api/chat/settings", {"sources": OFF})[0] == 200
    chat_stream(app, {"text": "and gamma?", "pages": ["gamma"]})
    assert "gamma" not in read and "CANARY-GAMMA-58" not in str(llm.bodies)


def test_review_cards_carry_their_related_pages(app, vault):
    links = "".join(f"See [[{i}]].\n" for i in ("beta", "gamma", "delta", "epsilon",
                                                 "secret-x"))
    _note(vault, "notes", "alpha", {"title": "Alpha"}, links)
    for i in ("beta", "gamma", "delta", "epsilon", "zeta"):
        _note(vault, "notes", i, {"title": i.title()}, "x\n")
    _note(vault, "notes", "secret-x", {"title": "Secret", "private": True}, "See [[alpha]].\n")
    view = {"pending": [{"key": "k1", "page": "alpha"}], "stale": [{"id": "c1", "page": "zeta"}],
            "conflicts": [{"key": "c", "left": {"page": "alpha"}, "right": {"page": "beta"}}],
            "today": [{"type": "pending", "key": "k1"}]}
    cold = app._related(Vault(vault), copy.deepcopy(view), [])  # Chat's index not built yet
    assert "related" not in cold["pending"][0]  # Review never builds it (~3 s at 5k)
    chat.pick_list(Vault(vault), "", chat.get_settings()["sources"])  # Chat's first use builds it
    got = app._related(Vault(vault), copy.deepcopy(view), [])
    rel = got["pending"][0]["related"]
    assert len(rel) == app.RELATED
    assert {r["path"] for r in rel} < {f"notes/{i}.md" for i in ("beta", "gamma", "delta",
                                                                  "epsilon")}
    assert {r["title"] for r in rel} < {"Beta", "Gamma", "Delta", "Epsilon"}
    assert got["stale"][0]["related"] == []  # no line when there are none
    assert "related" not in got["conflicts"][0] and "related" not in got["today"][0]
    code, js, _, _ = call(app, "GET", "/api/review")  # the view itself still builds
    assert code == 200 and "pending" in js


@pytest.mark.slow
@pytest.mark.perf
def test_the_list_and_related_stay_quick_on_5k_pages(tmp_path, capsys):
    root = tmp_path / "big"
    _big(root)
    v, a = Vault(root), server.App(root)
    sources = chat.get_settings()["sources"]
    assert len(chat.pick_list(v, "note", sources)) == chat.PICK  # builds the kept index once

    def ms(fn) -> float:
        times = []
        for _ in range(11):
            t = time.perf_counter()
            fn()
            times.append(time.perf_counter() - t)
        return statistics.median(times) * 1000
    words, newest = ms(lambda: chat.pick_list(v, "note 12", sources)), \
        ms(lambda: chat.pick_list(v, "", sources))
    cards = {"pending": [{"key": f"k{i}", "page": f"note-{i}"} for i in range(20)]}
    evs = list(EventLog(v).iter_events())
    related = ms(lambda: a._related(v, copy.deepcopy(cards), evs))
    with capsys.disabled():
        print(f"\n5k pages: '#' list {words:.1f} ms (words), {newest:.1f} ms (newest); related "
              f"for 20 page cards {related:.1f} ms (medians of 11)")
    assert words < 50 and newest < 50 and related < 30
