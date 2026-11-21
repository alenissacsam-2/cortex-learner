"""Chat's offline test (cortex.chat_eval): the made-up vault, the scoring rules and the Chat
fixes the test found. Model questions are not run here: the end-to-end test answers them with
a loopback runner only to check what reaches a model."""

import json
import re
import zlib
from datetime import date
from pathlib import Path

import numpy as np
import pytest
from test_chat import Stream

from cortex import chat, chat_eval, chat_facts, chat_study, levels, semantic, taint
from cortex.events import EventLog
from cortex.vault import Vault, VaultError

pytestmark = pytest.mark.usefixtures("home")
TODAY = chat_eval.TODAY


# ---- the spec and the scoring rules -----------------------------------------------------------

def test_the_spec_has_40_questions_and_no_august_2026():
    sp = chat_eval.spec()
    qs = sp["questions"]
    assert [q["id"] for q in qs] == [f"Q{i:02}" for i in range(1, 41)]
    assert {q["mode"] for q in qs} == {"code", "model"}
    assert [q["id"] for q in qs if q["private"]] == ["Q37"]  # the Private switch: Q37 only
    assert "2026-08" not in json.dumps(sp)  # the draft's no-August gap (Q32)
    ids = {i for _, ids in chat_eval.BLOCKERS for i in ids}
    assert ids <= {q["id"] for q in qs} and len(chat_eval.BLOCKERS) == 5


@pytest.mark.parametrize("term,text,want", [
    ("17", "You visited it 17 times.", True),
    ("17", "On 2026-11-17 at 17:05.", False),  # dates and times are not counts
    ("17", "1.17 ms", False),
    ("1.42", "about 1.42 ms", True),
    ("uv", "I use uv now", True),
    ("uv", "uvicorn", False),  # a short term needs a word end
    ("MVCC", "mvcc's write skew", True),
    ("tree", "B-Trees", True),  # longer terms match from a word start
    ("rees", "B-Trees", False),
])
def test_has_matches_whole_numbers_and_word_starts(term, text, want):
    assert chat_eval.has(term, text) is want


def _got(**k):
    return {"answer": "", "cited": [], "cited_titles": [], "served": [], "facts": None,
            "computed": False, "err": None, "bad_cite": None, **k}


def test_score_flags_a_private_leak_to_the_model_but_not_the_question_words():
    q = {"id": "Qx", "mode": "model", "private": False, "question": "What about Kestrel?"}
    asked = chat_eval.score(q, _got(answer="I found nothing about Kestrel.",
                                    facts={"query": "kestrel"}))
    assert asked["status"] == "pass"  # the question's own word in the answer and the query
    served = [{"path": "notes/x.md", "title": "x", "text": "a Teensy 4.1 board"}]
    leak = chat_eval.score(q, _got(answer="Nothing.", served=served))
    assert leak["why"] == ["private leak to the model: 'Teensy'"]


def test_score_records_retrieval_rank_apart_from_the_answer():
    served = [{"path": f"notes/n{i}.md", "title": "", "text": ""} for i in range(5)]
    q = {"id": "Qx", "mode": "model", "private": True, "question": "?", "gold": ["notes/n4"],
         "all_of": ["Raft"]}
    s = chat_eval.score(q, _got(answer="Raft [5]", served=served, cited=["notes/n4.md"]))
    assert (s["status"], s["retrieval"], s["gold_rank"], s["cites_gold"]) == (
        "pass", "buried", 5, True)
    neg = chat_eval.score({**q, "gold": [], "all_of": []}, _got(served=served))
    assert neg["retrieval"] == "negative"


def test_an_empty_report_says_no_data_not_zero():
    out = chat_eval.summary(chat_eval.report(chat_eval.spec(), [], None, "no local model", 0.0))
    assert "recall @1 no data, @3 no data, @5 no data" in out
    assert "0%" not in out and "0/0" not in out
    assert "not run  Private leak with Private off" in out


def test_a_second_run_is_refused_while_one_runs():
    assert chat_eval._RUN.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            chat_eval.run(use_model=False)
    finally:
        chat_eval._RUN.release()


def test_find_model_never_takes_a_bigger_model(monkeypatch):
    from cortex import localllm

    big = localllm.Runner("lmstudio", "http://127.0.0.1:1234", "qwen3.5-9b")
    monkeypatch.setattr(localllm, "status", lambda: {"runner": big})
    monkeypatch.setattr(localllm, "loaded", lambda r: True)
    r, why = chat_eval.find_model()
    assert r is None and "not a 2B model" in why
    small = localllm.Runner("lmstudio", "http://127.0.0.1:1234", "qwen3.5-2b")
    monkeypatch.setattr(localllm, "status", lambda: {"runner": small})
    assert chat_eval.find_model() == (small, "")
    monkeypatch.setattr(localllm, "loaded", lambda r: False)  # reachable, nothing loaded
    assert chat_eval.find_model() == (None, "no local model loaded")


def test_pinned_config_is_per_run_and_read_only():
    pin = {"browse": {"private_domains": ["synthdiy.example"]}}
    with taint.pinned(pin):
        assert taint.config()["browse"]["private_domains"] == ["synthdiy.example"]
        taint.config()["browse"]["private_domains"].append("x")  # a copy each time
        assert taint.config()["browse"]["private_domains"] == ["synthdiy.example"]
        with pytest.raises(VaultError, match="pinned"):
            taint.write_config({})
    assert "synthdiy.example" not in json.dumps(taint.config())


# ---- the Chat fixes the test found ------------------------------------------------------------

def test_week_of_a_day_is_that_monday_to_sunday():  # Q09
    lo, hi, _ = chat._when("What did I do the week of 10 Nov 2026?", TODAY)
    assert (lo, hi) == (date(2026, 11, 9), date(2026, 11, 15))


def test_top_n_sites_is_a_site_list():  # Q04: a number is no topic
    assert chat.asks_sites("What were my top 3 most visited websites?", TODAY)
    assert not chat.asks_sites("Which sites about binary search did I visit?", TODAY)


def test_this_year_does_not_point_back():  # Q33
    assert not chat.follow_up("How many times did I visit cp-algorithms.com this year?")
    assert chat.follow_up("and that year?")


def test_last_review_names_the_concept_not_the_verb():  # Q07
    text = "When did I last review B-Trees?"
    r = chat_facts.route(text, chat._when(text, TODAY), TODAY)
    assert (r["kind"], r["term"]) == ("last", "b trees")


@pytest.mark.parametrize("text", [
    "Which topics am I weakest in or need to refresh?",
    "What open mistakes do I still need to fix?",
])
def test_weak_spot_wordings(text):  # Q28, Q30
    assert chat_study.kind(text) == "weak"


@pytest.mark.parametrize("text", ["Which topics are solid?", "Which concepts am I learning?",
                                  "What topics are due for a refresh?"])
def test_state_questions(text):  # Q29
    assert chat_study.kind(text) == "state"


# ---- the made-up vault, end to end ------------------------------------------------------------

_KEEP = {".md", ".jsonl", ".json"}


def _hash_embed(texts):
    """A bag of words in 63 hashed buckets plus a shared axis that keeps every cosine above
    semantic.MIN_COS: unlike FakeEmbed's 8 words it ranks this vault's pages, so the search
    serves passages to the loopback model."""
    out = np.zeros((len(texts), 64), dtype=np.float32)
    for k, t in enumerate(texts):
        for w in re.findall(r"\w+", t.lower()):
            out[k, 1 + zlib.crc32(w.encode()) % 63] += 1
        out[k, 0] = 2 * max(float(np.linalg.norm(out[k])), 1.0)
    return out


def _files(root: Path) -> dict[str, str]:
    """Every page and record the build wrote, minus what is stamped at write time."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and p.suffix in _KEEP and not rel.startswith((".cortex/", "events/")):
            out[rel] = p.read_text(encoding="utf-8")
    return out


@pytest.mark.slow  # two builds of about 6 s each (Windows file locks on every page)
def test_the_made_up_vault_end_to_end(tmp_path, monkeypatch):
    sp = chat_eval.spec()
    r = chat_eval.run(use_model=False, keep=tmp_path / "vault")
    rows = {q["id"]: q for q in r["questions"]}

    # every question Cortex answers with no model passes; the model ones are skipped
    code = [q["id"] for q in sp["questions"] if q["mode"] == "code"]
    assert {i: rows[i]["why"] for i in code if rows[i]["status"] != "pass"} == {}
    assert (r["ran"], r["skipped"]) == (len(code), 40 - len(code))
    assert {b["name"]: b["state"] for b in r["blockers"]} == {
        "Private leak with Private off": "ok", "Stale preference served as current": "not run",
        "Made-up history for an empty month": "ok",
        "Computed count or study state wrong": "ok",
        "Deep passage missed (1.42 ms past character 700)": "not run"}
    assert r["retrieval"]["facts"].endswith("(100%)")  # every computed piece held its facts

    # the model questions through a loopback runner: nothing private reaches it with Private
    # off, and Q37 (Private on) gets the private note
    v = Vault((tmp_path / "vault").resolve())
    monkeypatch.setattr(semantic, "_embedder", _hash_embed)
    s = Stream("openai", ["I could not find that in your notes."])
    model_qs = [q for q in sp["questions"] if q["mode"] == "model"]
    try:
        from cortex.catalog import Catalog
        from cortex.index import Index

        semantic.vectors(Index(v, Catalog.load(v)), _hash_embed, 64)
        with taint.pinned(sp["meta"]["config"]), chat_eval._watching():
            got = {q["id"]: chat_eval._ask(v, q, s.runner("qwen3.5-2b")) for q in model_qs}
        bodies = list(s.bodies)
    finally:
        chat_eval.forget(v.root)
        s.close()
    scored = {q["id"]: chat_eval.score(q, got[q["id"]]) for q in model_qs}
    assert {i: w for i, sc in scored.items() for w in sc["why"] if "leak" in w} == {}
    assert any("Teensy 4.1" in p["text"] for p in got["Q37"]["served"])
    assert got["Q36"]["served"]  # the private-off question was searched, not refused
    q37 = next(q["question"] for q in model_qs if q["id"] == "Q37")
    asked = {q["question"] for q in sp["questions"]}
    assert any(q37 in m["content"] for b in bodies for m in b["messages"])
    for b in bodies:
        text = "\n".join(m["content"] for m in b["messages"])
        if q37 in text:
            assert "Teensy 4.1" in text
            continue
        for q in asked:
            text = text.replace(q, " ")  # the person's own words may name the topic
        assert [t for t in chat_eval.CANARIES if chat_eval.has(t, text)] == [], text[:400]

    # the fold agrees with the spec's expectations as of TODAY
    states, mistakes = levels.fold(list(EventLog(v).iter_events()), TODAY.isoformat(),
                                   levels.problem_concepts(v))
    want = sp["expect"]
    assert {c: states[c].state for c in want["states"]} == want["states"]
    assert {c: st.confident_miss for c, st in states.items() if st.confident_miss} == \
        want["confident_miss"]
    assert sorted(m for m, st in mistakes.items() if st.status == "open") == \
        want["open_mistakes"]
    assert sorted(m for m, st in mistakes.items() if st.status != "open") == \
        want["resolved_mistakes"]

    # no August 2026 data anywhere in the vault (Q32's empty month), events included; only the
    # span of a week that runs into it (week 31 ends 2 Aug, week 36 starts 31 Aug) names it
    every = {p.relative_to(v.root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
             for p in v.root.rglob("*") if p.is_file() and p.suffix in _KEEP
             and not p.relative_to(v.root).as_posix().startswith(".cortex/browse/chat/")}
    span = re.compile(r"(?:date|end): 2026-08-\d\d|Up: \[\[rollup-2026-08\]\]")
    aug = [(k, m[0]) for k, t in every.items()
           for m in re.finditer(r"(?m)^.*(?:2026-08|August 2026).*$", t)
           if not (k.startswith("rollups/rollup-2026-w") and span.fullmatch(m[0]))]
    assert aug == [], "\n".join(map(str, aug))
    assert not (v.root / "rollups" / "rollup-2026-08.md").exists()

    # the same spec builds the same vault
    with taint.pinned(sp["meta"]["config"]):
        again = chat_eval.build(tmp_path / "again", sp)
    chat_eval.forget(again.root)
    first, second = _files(v.root), _files(again.root)
    assert first.keys() == second.keys() and len(first) > 100
    assert [k for k in first if first[k] != second[k]] == []
