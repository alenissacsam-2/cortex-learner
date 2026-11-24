"""Chat's Evidence Rail backend (polish P3): `chat.certainty` for each branch, the coverage
`horizon` (months, switches, the per-version cache, speed on 5k pages), the no-data answer for a
past range with nothing in it (no search model, no runner) and its equivalence with the dated
search, and the `conflicts` block for a recorded update_of pair. Made-up data; loopback fakes
only, no model."""
# ruff: noqa: F811  (test_chat's `vault` and `stream` fixtures, taken as arguments)

import json
import os
import threading
import time
from datetime import date, datetime

import pytest
from conftest import note
from test_chat import ALL_ON, TODAY, _turn, stream, vault  # noqa: F401  (fixtures)

from cortex import ask, chat, semantic
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")
OFF = {k: False for k in ALL_ON}


# ---- certainty -------------------------------------------------------------------------------

@pytest.mark.parametrize("kw, want", [
    ({"stopped": True, "data": True}, None),
    ({"stopped": True, "cited": 2, "read": 3}, None),
    ({"empty": True}, "none"),  # NO_ANSWER: nothing found
    ({"data": True, "empty": True}, "none"),  # an empty data or facts answer, a switch off
    ({"data": True}, "exact"),  # counted by code with a result
    ({"data": True, "unquoted": True}, "exact"),  # the count stands; the model only worded it
    ({"cited": 1, "read": 1, "quoted": True}, "exact"),
    ({"cited": 2, "read": 5, "quoted": True}, "exact"),
    ({"cited": 1, "read": 3, "quoted": True, "unquoted": True}, "thin"),
    ({"cited": 1, "read": 2}, "grounded"),
    ({"cited": 1, "read": 1}, "thin"),  # one passage read
    ({"cited": 0, "read": 5}, "thin"),  # only `searched`
    ({"cited": 0, "read": 5, "quoted": True}, "thin"),
    ({"read": 4, "unanswered": True}, "thin"),  # SAID_NOTHING
])
def test_certainty_rule(kw, want):
    assert chat.certainty(**kw) == want


# ---- horizon ---------------------------------------------------------------------------------

@pytest.fixture
def wide(vault):
    """`vault` (Nov 2026 only) plus pages dated every way the dated search dates them."""
    r = vault.root
    note(r, "notes/old.md", "kind: note\ntitle: Old\ndate: 2024-01-05\n", "# Old\n\nCar motor.")
    note(r, "notes/undated.md", "kind: note\ntitle: Undated\n", "# Undated\n\nCar motor oil.")
    t = datetime(2025, 3, 15, 12).timestamp()  # an undated note counts on its file's day
    os.utime(r / "notes/undated.md", (t, t))
    note(r, "resources/chat-june.md", "kind: resource\ntitle: June chat\n",
         "# June chat\n\nTimeline: [[2025-06-02]] · Source: Gemini · Date: 2025-06-02\n\n"
         "We talked about pasta.")
    note(r, "notes/secret.md", "kind: note\ntitle: Secret\nprivate: true\ndate: 2024-07-03\n",
         "# Secret\n\nCar motor secret.")
    note(r, "browse/browse-2025-02-03.md", "kind: browse\ndate: 2025-02-03\n",
         "# Browsing 2025-02-03\n\n## Topics\n- Car motor shop\n")
    note(r, "browse/private/browse-private-2025-09-09.md",
         "kind: browse\nprivate: true\ndate: 2025-09-09\n",
         "# Browsing 2025-09-09 (private)\n\n## Topics\n- Pasta recipe\n")
    return vault


def test_horizon_months_follow_the_switches(wide):
    h = chat.horizon(wide, ALL_ON)
    assert {"2024-01", "2024-07", "2025-02", "2025-03", "2025-06", "2025-09", "2026-11"} \
        <= set(h["months"])
    assert (h["first"], h["last"]) == ("2024-01", h["months"][-1]) and h["months"] == sorted(
        h["months"])
    no_private = set(chat.horizon(wide, dict(ALL_ON, private=False))["months"])
    assert "2024-07" not in no_private and "2025-09" not in no_private  # note and browsing
    assert "2024-01" in no_private
    assert "2025-06" not in chat.horizon(wide, dict(ALL_ON, chats=False))["months"]
    # an indexed browse page: a browse hit of the search, a note of its dated listing
    assert "2025-02" in chat.horizon(wide, dict(ALL_ON, browsing=False))["months"]
    assert "2025-02" not in chat.horizon(wide, dict(ALL_ON, browsing=False, notes=False))[
        "months"]
    assert chat.horizon(wide, OFF) is None


def test_horizon_is_kept_per_vault_version(wide, monkeypatch):
    first = chat.horizon(wide, ALL_ON)
    real, calls = chat._page_days, []
    monkeypatch.setattr(chat, "_page_days", lambda *a: calls.append(1) or real(*a))
    assert chat.horizon(wide, ALL_ON) == first and calls == []  # reused
    note(wide.root, "notes/newer.md", "kind: note\ntitle: Newer\ndate: 2023-05-01\n", "# N\n")
    assert chat.horizon(wide, ALL_ON)["first"] == "2023-05" and calls == [1]
    assert chat.horizon(wide, dict(ALL_ON, chats=False)) and calls == [1, 1]  # other switches
    # a private browse day is not in home.version: its name still stamps the cache
    note(wide.root, "browse/private/browse-private-2022-02-02.md", "kind: browse\n", "# B\n")
    assert chat.horizon(wide, ALL_ON)["first"] == "2022-02"


@pytest.mark.perf
def test_horizon_is_quick_on_5k_pages(tmp_path):
    root = tmp_path / "big"
    (root / "notes").mkdir(parents=True)
    for i in range(5000):
        (root / "notes" / f"n{i}.md").write_bytes(
            f"---\nkind: note\ntitle: Note {i}\ndate: {2020 + i % 7}-{1 + i % 12:02d}-11\n---\n"
            f"# Note {i}\n\nWords {i}.\n".encode())
    v = Vault(root)
    assert len(chat.horizon(v, ALL_ON)["months"]) == 84  # the first builds the index
    t = time.perf_counter()
    h = chat.horizon(v, ALL_ON)
    took = time.perf_counter() - t
    assert (h["first"], h["last"]) == ("2020-01", "2026-12") and took <= 0.5, took


# ---- the no-data answer ----------------------------------------------------------------------

class Refuse:
    """An embedder and a runner factory that fail the test when called."""

    def __call__(self, *a, **k):
        raise AssertionError("no model may be called for a range with nothing in it")


@pytest.mark.parametrize("embedder", ["refuse", "none"])
def test_a_range_with_nothing_is_answered_without_a_model(vault, monkeypatch, embedder):
    monkeypatch.setattr(semantic, "_embedder", Refuse() if embedder == "refuse" else None)
    cid, ev = _turn(vault, None, "car motor notes in March 2025", Refuse())
    kinds = [e["t"] for e in ev]
    assert kinds == ["start", "step", "tok", "src", "done"], ev
    assert ev[0]["model"] == "Cortex" == ev[-1]["model"]
    assert ev[1]["s"] == "Searching your notes and browsing from 1 Mar 2025 to 31 Mar 2025"
    said = ("Cortex has nothing from 1 Mar 2025 to 31 Mar 2025. "
            "Your data covers Nov 2026 – Nov 2026.")  # all of `vault` is from Nov 2026
    assert ev[2]["s"] == said
    src = ev[3]
    assert src["certainty"] == "none" and src["sources"] == [] == src["searched"]
    assert src["horizon"] == {"first": "2026-11", "last": "2026-11", "months": ["2026-11"]}
    saved = chat.load(vault, cid)[-1]
    assert saved["text"] == said and saved["model"] == "Cortex"
    assert saved["certainty"] == "none" and saved["horizon"] == {"first": "2026-11",
                                                                 "last": "2026-11"}


def test_a_gap_inside_the_data_says_only_what_is_missing(wide, monkeypatch):
    monkeypatch.setattr(semantic, "_embedder", Refuse())
    _, ev = _turn(wide, None, "car motor notes in October 2025", Refuse())
    assert ev[2]["s"] == "Cortex has nothing from 1 Oct 2025 to 31 Oct 2025."
    _, ev = _turn(wide, None, "what car motor notes from 1 Oct 2023?", Refuse())
    assert ev[2]["s"] == "Cortex has nothing on 1 Oct 2023. Your data covers Jan 2024 – Nov 2026."


def test_a_follow_up_borrows_the_range_with_nothing(vault, stream, monkeypatch):
    s = stream("ollama", ["Engines [1]."])
    cid, ev = _turn(vault, None, "car motor", s.runner())
    assert ev[0]["model"] == "gemma3:2b"
    monkeypatch.setattr(semantic, "_embedder", Refuse())
    _, ev = _turn(vault, cid, "car motor in March 2025", Refuse())
    assert ev[0]["model"] == "Cortex"
    _, ev = _turn(vault, cid, "and which of those was best?", Refuse())  # borrows March 2025
    assert ev[0]["model"] == "Cortex" and ev[2]["s"].startswith(
        "Cortex has nothing from 1 Mar 2025 to 31 Mar 2025.")


@pytest.mark.parametrize("q", ["car motor notes in March 2025", "car motor in June 2025",
                               "car motor in February 2025", "car motor in January 2024"])
def test_a_page_dated_in_the_range_goes_to_the_search(wide, stream, q):
    # dated by its file's day, a Timeline line, a browse page's name, a `date`
    s = stream("ollama", ["It says so [1]."])
    _, ev = _turn(wide, None, q, s.runner(), settings=dict(chat.get_settings(), sources=ALL_ON))
    assert ev[0]["model"] == "gemma3:2b" and len(s.bodies) == 1


def test_the_no_data_answer_never_hides_a_page_the_search_finds(wide):
    """For every month and some days, under several switch sets: when the dated search returns
    a page, there is no no-data answer."""
    months = [date(y, m, 1) for y in range(2023, 2027) for m in range(1, 13)]
    ranges = [(a, date(a.year + (a.month == 12), a.month % 12 + 1, 1)
               - (date(2001, 1, 2) - date(2001, 1, 1))) for a in months]
    ranges += [(d, d) for d in (date(2025, 3, 15), date(2025, 3, 16), date(2026, 11, 16),
                                date(2025, 9, 9), date(2025, 2, 3), date(2024, 7, 3))]
    shortcut = 0
    for sources in (ALL_ON, dict(ALL_ON, private=False), dict(ALL_ON, notes=False),
                    dict(ALL_ON, chats=False, browsing=False)):
        days = chat._horizon_days(wide, sources)
        for lo, hi in ranges:
            hi = min(hi, TODAY)
            if lo > hi:
                continue
            found = ask._passages(wide, "car motor pasta", 8, sources=sources, topic="",
                                  between=(lo.isoformat(), hi.isoformat()))
            nothing = chat._nothing(lo, hi, days, TODAY)
            assert not (found and nothing), (lo, hi, sources, [p["path"] for p in found])
            shortcut += nothing is not None
    assert shortcut > 100  # most of these months hold nothing


# ---- conflicts and certainty end to end ------------------------------------------------------

def _change(root, claim, page, old_page, old_claim, day="2026-11-10", n=1):
    p = root / "events" / "2026" / "11" / "cli.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "id": f"01JZZZZZZZZZZZZZZZZZZZZZ{n:02d}", "ts": f"{day}T10:00:00Z", "day": day,
            "session": "cli", "kind": "change", "source": "agent", "schema": 1,
            "payload": {"change": {"tool": "patch", "kind": "me", "id": page, "claim": claim,
                                   "update_of": {"page": old_page, "claim": old_claim}}}}) + "\n")


def _passage(path, title, day=""):
    return {"path": path, "title": title, "text": f"{title} text", "kind": "note", "day": day,
            "at": 0, "len": 10}


def test_a_recorded_pair_among_the_cited_pages_is_a_conflict(vault, stream, monkeypatch):
    _change(vault.root, "c-new", "preferences", "profile", "c-old")
    got = [_passage("me/profile.md", "Profile [x](http://evil.example)"),
           _passage("notes/engines.md", "Engines", "2026-11-01"),
           _passage("me/preferences.md", "Preferences")]
    monkeypatch.setattr(ask, "_passages", lambda *a, **k: [dict(p) for p in got])
    s = stream("ollama", ["Now dark [3], before light [1]."])
    cid, ev = _turn(vault, None, "what do I prefer?", s.runner())
    want = [{"old": {"path": "me/profile.md", "title": "Profile x", "day": ""},
             "new": {"path": "me/preferences.md", "title": "Preferences", "day": ""}}]
    assert ev[-2]["conflicts"] == want == chat.load(vault, cid)[-1]["conflicts"]
    assert "evil.example" not in json.dumps(want)
    s = stream("ollama", ["Only the newer one [3]."])
    _, ev = _turn(vault, None, "what do I prefer?", s.runner())
    assert "conflicts" not in ev[-2]  # one side cited: no pair among the cited pages
    s = stream("ollama", ["No citation at all."])
    _, ev = _turn(vault, None, "what do I prefer?", s.runner())
    assert ev[-2]["conflicts"] == want  # none cited: the pages read


def test_the_newer_side_is_the_later_day(vault):
    _change(vault.root, "c-new", "preferences", "profile", "c-old")
    later = [_passage("me/profile.md", "Profile", "2026-11-12"),
             _passage("me/preferences.md", "Preferences", "2026-11-01")]
    (old, new), = chat._conflicts(vault, later)
    assert (old["path"], new["path"]) == ("me/preferences.md", "me/profile.md")
    assert chat._conflicts(vault, later[:1]) == []


def test_certainty_of_each_answer(vault, stream, monkeypatch):
    two = [dict(_passage("notes/engines.md", "Engines"), text="Automobile car motor basics."),
           _passage("notes/kitchen-day.md", "Kitchen")]
    real = ask._passages
    monkeypatch.setattr(ask, "_passages", lambda *a, **k: [dict(p) for p in two])
    s = stream("ollama", ['It says "Automobile car motor basics" [1].'])
    cid, ev = _turn(vault, None, "car motor", s.runner())
    assert ev[-2]["certainty"] == "exact" == chat.load(vault, cid)[-1]["certainty"]
    assert ev[-2]["horizon"]["last"] == "2026-11"
    s = stream("ollama", ["Engines run [1]."])
    assert _turn(vault, None, "car motor", s.runner())[1][-2]["certainty"] == "grounded"
    monkeypatch.setattr(ask, "_passages", lambda *a, **k: [dict(two[0])])
    s = stream("ollama", ["Engines run [1]."])
    assert _turn(vault, None, "car motor", s.runner())[1][-2]["certainty"] == "thin"
    monkeypatch.setattr(ask, "_passages", real)
    s = stream("ollama", ["Engines run."])
    assert _turn(vault, None, "car motor", s.runner())[1][-2]["certainty"] == "thin"
    s = stream("ollama", ['It says "the motor runs on pure moonlight" [1].'])
    assert _turn(vault, None, "car motor", s.runner())[1][-2]["certainty"] == "thin"
    off = dict(chat.get_settings(), sources=OFF)
    _, ev = _turn(vault, None, "car motor", s.runner(), settings=off)  # NO_ANSWER
    assert ev[2]["s"] == chat.NO_ANSWER and ev[-2]["certainty"] == "none"
    assert "horizon" not in ev[-2]  # it could read nothing
    _, ev = _turn(vault, None, "what did I do on 16 Nov?", s.runner())  # the digest
    assert ev[0]["model"] == "Cortex" and ev[-2]["certainty"] == "exact"
    assert "horizon" not in ev[-2]  # data answers count their own data
    _, ev = _turn(vault, None, "what did I do on 2 Nov?", s.runner())
    assert ev[-2]["certainty"] == "none"  # an empty digest


def test_a_stopped_answer_has_no_certainty(vault, stream):
    s = stream("ollama", [f"w{i} " for i in range(30)], delay=0.03)
    cancel = threading.Event()
    cid, ev = _turn(vault, None, "car motor", s.runner(), cancel=cancel,
                    on=lambda e: e["t"] == "tok" and cancel.set())
    assert ev[-1]["stopped"] is True
    assert "certainty" not in ev[-2] and "certainty" not in chat.load(vault, cid)[-1]
