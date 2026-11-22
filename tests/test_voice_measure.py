"""V1 (22 Nov): the measured writing style: counted numbers over the voice samples, plain
lines from them, proposed for me/voice where they wait for Approve; no model, no network."""

import datetime as dt
import inspect

import pytest
from conftest import run
from typer.testing import CliRunner

from cortex import voice
from cortex.app import server
from cortex.cli import ME_PAGES, app
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")
NOW = dt.datetime(2026, 11, 20, tzinfo=dt.UTC)
FRIEND = [f"hey! i'm free at {i} \U0001f642" for i in range(10)]
PROF = [f"Dear Professor Lee,\nCould we meet on day {i} to discuss the draft chapter of my "
        "thesis?\nI would like your advice on the method section before I submit it to the "
        "committee next month.\nRegards, Sam" for i in range(10)]


def _samples(v, friend=FRIEND, prof=PROF):
    rows = ([{"audience": "friend", "act": "other", "channel": "chat", "text": t} for t in friend]
            + [{"audience": "professor", "act": "request", "channel": "email", "text": t}
               for t in prof]
            + [{"audience": "friend", "act": "request", "channel": "ai", "text": f"fix bug {i}"}
               for i in range(10)]  # typed to an AI: never the person's voice
            + [{"audience": "boss", "act": "update", "channel": "email", "text": "Done."}] * 7)
    voice.add(v, voice.SAMPLES, rows, NOW)


def test_measure_numbers_floor_and_ai_excluded(tmp_path):
    v = Vault(tmp_path)
    _samples(v)
    m = voice.measure(v)
    assert set(m) == {"all", "friend", "professor"}  # boss: 7 samples, under the floor
    assert m["all"]["n"] == 27 and m["friend"]["n"] == 10  # the ai rows are not counted
    assert m["friend"] == {"n": 10, "sentence_words": 2.5, "questions": 0.0,
                           "contractions": 20.0, "exclamations": 50.0, "opens": "hey",
                           "closes": None, "lowercase": 1.0, "emoji": 1.0, "message_words": 5}
    p = m["professor"]
    assert (p["opens"], p["closes"], p["lowercase"], p["emoji"]) == ("Dear", "Regards, Sam", 0, 0)
    assert p["questions"] == 0.25 and p["contractions"] == 0 and p["exclamations"] == 0
    assert voice.measure(v) == m  # deterministic


def test_style_lines(tmp_path):
    v = Vault(tmp_path)
    _samples(v)
    m = voice.measure(v)
    assert voice.style_lines(m, "friend") == [
        "Writes short sentences (median 2.5 words) and asks few questions.",
        'Opens with "hey".',
        "Uses exclamation marks often; often writes all lowercase; uses contractions "
        "(I'm, don't).",
        "Keeps messages short (median 5 words); uses emoji often (1 per message)."]
    prof = voice.style_lines(m, "professor")
    assert 'Opens with "Dear" and signs off "Regards, Sam".' in prof
    assert "Rarely uses exclamation marks; avoids contractions." in prof
    assert len(prof) <= 4 and not any("sentences" in x for x in prof)  # 13 words: the middle
    assert voice.style_lines(m, "boss") == []


def _inbox(root):
    p = root / "inbox.md"
    return [ln for ln in p.read_text("utf-8").splitlines() if "Measured: " in ln] \
        if p.is_file() else []


def test_propose_waits_rerun_replaces_and_approved_line_stays(root):
    (root / "me/voice.md").write_text(ME_PAGES["me/voice.md"], encoding="utf-8")
    page = (root / "me/voice.md").read_text("utf-8")
    v = Vault(root)
    _samples(v)
    n = voice.propose(v)
    first = _inbox(root)
    assert n["proposed"] == len(first) >= 6
    assert (root / "me/voice.md").read_text("utf-8") == page  # nothing written until Approve
    assert any(ln.startswith("- friend: Measured: Writes short sentences") for ln in first)
    assert any(ln.startswith("- Everyone: Measured: ") for ln in first)
    assert not any(ln.startswith("- boss:") for ln in first)

    assert voice.propose(v) == {"kept": len(first)}  # a re-run adds no duplicates
    assert _inbox(root) == first

    a = server.App(root)
    line = "Measured: Writes short sentences (median 2.5 words) and asks few questions."
    p = next(x for x in a.review()["pending"] if x["text"] == line)
    a.review_approve({"key": p["key"]})
    assert line in (root / "me/voice.md").read_text("utf-8")

    m = voice.measure(v)
    m["friend"] = {**m["friend"], "sentence_words": 30, "emoji": 0.5}
    n = voice.propose(v, m)
    now = _inbox(root)
    # the approved line is not withdrawn; "short messages" is already said for Everyone
    assert n["withdrawn"] == 1 and n["proposed"] == 1
    assert not any("median 2.5 words" in ln or "5 words); uses emoji" in ln for ln in now)
    assert any("Writes long sentences (median 30 words)" in ln for ln in now)
    assert line in (root / "me/voice.md").read_text("utf-8")  # approved: never removed

    async def body(call):
        out = await call("cortex_as_me", {"task": "Ask a friend over", "audience": "friend"})
        assert any(line in c["text"] for c in out["voice"]["card"])

    run(root, body)


def test_cli_measure(root):
    _samples(Vault(root))
    r = CliRunner().invoke(app, ["voice", "measure", "--vault", str(root)])
    assert r.exit_code == 0, r.output
    assert "friend: n 10" in r.output and "Opens with" in r.output
    assert not (root / "inbox.md").is_file() or "Measured:" not in (
        root / "inbox.md").read_text("utf-8")
    r = CliRunner().invoke(app, ["voice", "measure", "--propose", "--vault", str(root)])
    assert r.exit_code == 0 and "proposed" in r.output and _inbox(root)


def test_no_network():
    src = inspect.getsource(voice)
    assert not any(w in src for w in ("urllib", "socket", "http", "requests"))
