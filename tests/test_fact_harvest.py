"""Facts the owner states in chats become about-me guesses (cortex.fact_harvest), on vault_mini
with synthetic chat pages and a fake runner returning canned JSON. Nothing real runs or loads."""

from datetime import timedelta

import pytest
from conftest import events, git
from test_app import app as app  # noqa: F401 (fixture)
from test_app import fakes as fakes  # noqa: F401 (fixture: nothing real runs or opens)
from test_app import vault as vault  # noqa: F401 (fixture)
from test_safety_net import AT, NOW, Runner, chat
from typer.testing import CliRunner

from cortex import claims, cli
from cortex import fact_harvest as fh
from cortex import safety_net as sn
from cortex.events import EventLog
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

PUNE = {"page": "profile", "fact": "Lives in Pune", "quote": "I moved to Pune last month"}


def facts(*items):
    return Runner({"facts": list(items)})


def go(root, runner):
    return fh.run(Vault(root), runner, now=NOW)


def live(root, page):
    v = Vault(root)
    return {r["text"]: r for r in claims.tiers(v, EventLog(v).iter_events())
            if r["page"] == page and r["status"] == "live"}


def change_of(root, text):
    cid = live(root, "profile")[text]["id"]
    return next(e["payload"]["change"] for e in events(root) if e["kind"] == "change"
                and e["payload"].get("change", {}).get("claim") == cid)


def test_a_verbatim_first_person_fact_is_saved_as_a_guess(root):
    chat(root, "c1", "I moved to Pune last month. Any good cafes near the station?")
    r = facts(PUNE)
    out = go(root, r)
    assert out["chats"] == 1 and out["saved"] == 1 and out["session"]
    assert len(r.calls) == 1 and "<untrusted_note" in r.calls[0]
    row = live(root, "profile")["Lives in Pune"]
    assert row["tier"] == "agent" and row["section"] == "Background"
    c = change_of(root, "Lives in Pune")
    assert c["producer"] == "imports" and c["chat"] == "c1"
    assert {e["mode"] for e in events(root) if e["session"] == out["session"]
            and "mode" in e} == {"tidy"}


def test_unsaid_third_person_and_question_statements_are_dropped(root):
    chat(root, "c1", "He works at Google as a manager.",
         "Do I need a visa to work in Germany?", "should I learn rust before go")
    out = go(root, facts(
        {"page": "profile", "fact": "Lives in Mumbai", "quote": "I live in Mumbai"},  # made up
        {"page": "profile", "fact": "Works at Google", "quote": "He works at Google"},
        {"page": "goals", "fact": "Wants to work in Germany",
         "quote": "I need a visa to work in Germany"}))
    assert out == {"chats": 1, "facts": 0}
    assert fh.check({"page": "goals", "fact": "Learn Rust", "quote": "should I learn rust"},
                    ["should I learn rust before go"]) is None  # a question with no "?"
    assert fh.check({"page": "profile", "fact": "Lives in Pune", "quote": "I moved to Pune"},
                    ["Honestly, I moved to Pune last month"]) == ("profile", "Lives in Pune")
    assert set(live(root, "profile")) == {"Test learner, synthetic."}  # the fixture's
    assert live(root, "goals") == {}


def test_core_setup_voice_targets_and_malformed_json_are_dropped(root):
    chat(root, "c1", "I use Neovim every day.")
    out = go(root, facts(*({"page": p, "fact": "Uses Neovim", "quote": "I use Neovim"}
                           for p in ("core", "setup", "voice"))))
    assert out == {"chats": 1, "facts": 0}
    chat(root, "c2", "I use Neovim every day, honestly.")
    for bad in ("sure! {not json", '{"facts": "Uses Neovim"}', '{"facts": [["profile"]]}'):
        (root / ".cortex" / "fact-harvest.json").unlink()
        assert go(root, Runner(bad)).get("facts", 0) == 0
    text = "".join(p.read_text("utf-8") for p in (root / "me").glob("*.md"))
    assert "Neovim" not in text


def test_private_and_old_chats_are_never_read(root):
    chat(root, "c-private", "I moved to Pune last month", private=True)
    old = (AT - timedelta(days=30)).astimezone()
    chat(root, "c-old", "I moved to Pune last month", when=old.strftime("%Y-%m-%d %H:%M"),
         day=old.date().isoformat())
    r = facts(PUNE)
    assert go(root, r) == {"skipped_private": 1, "skipped_old": 1}
    assert r.calls == []


def test_no_runner_or_a_paused_producer_does_nothing_and_keeps_the_chat_pending(
        root, monkeypatch):
    chat(root, "c1", "I moved to Pune last month")
    assert go(root, None) == {}
    assert fh.pending(Vault(root)) and not (root / ".cortex" / "fact-harvest.json").exists()
    monkeypatch.setattr(fh, "paused", lambda v: True)
    r = facts(PUNE)
    assert go(root, r) == {} and r.calls == []
    monkeypatch.undo()
    assert go(root, r)["saved"] == 1
    assert not fh.pending(Vault(root))
    assert go(root, r) == {} and len(r.calls) == 1  # a second pass with nothing new


def test_a_duplicate_of_an_existing_line_is_not_saved_twice(root):
    p = root / "me" / "profile.md"
    p.write_text(p.read_text("utf-8") + "\n## Background\n- Lives in Pune\n", encoding="utf-8")
    git(root, "commit", "-qam", "a line")
    chat(root, "c1", "I moved to Pune last month")
    chat(root, "c2", "I'm on the payments team now")
    chat(root, "c3", "I moved to Pune last month, finally")
    out = go(root, facts(PUNE, {"page": "profile", "fact": "Works on the payments team",
                                "quote": "I'm on the payments team"}))
    assert out["saved"] == 1 and out["not_saved"] == 2  # Pune from c1 and c3
    text = p.read_text("utf-8")
    assert text.count("Lives in Pune") == 1 and text.count("Works on the payments team") == 1


def test_undo_takes_a_pass_back(root):
    chat(root, "c1", "I moved to Pune last month")
    sid = go(root, facts(PUNE))["session"]
    assert "Lives in Pune" in live(root, "profile")
    r = CliRunner().invoke(cli.app, ["undo", sid, "--vault", str(root)])
    assert r.exit_code == 0, r.output
    assert "Lives in Pune" not in live(root, "profile")


def test_cli(root, monkeypatch):
    chat(root, "c1", "I moved to Pune last month")
    monkeypatch.setattr(fh, "cli_runner", lambda: None)
    r = CliRunner().invoke(cli.app, ["fact-harvest", "--vault", str(root)])
    assert r.exit_code == 0 and "No local model" in r.output
    monkeypatch.setattr(fh, "cli_runner", lambda: facts(PUNE))
    r = CliRunner().invoke(cli.app, ["fact-harvest", "--vault", str(root)])
    assert r.exit_code == 0, r.output
    assert "1 saved" in r.output and "cortex undo s_" in r.output


def test_the_app_pass_waits_for_jobs_and_a_model_and_commits(app, monkeypatch):  # noqa: F811
    chat(app.root, "c1", "I moved to Pune last month")
    r = facts(PUNE)
    monkeypatch.setattr(sn, "background_tools", lambda: (r, None))
    monkeypatch.setattr(app.jobs, "running", lambda: True)
    assert fh.auto(app) is None and r.calls == []
    monkeypatch.setattr(app.jobs, "running", lambda: False)
    monkeypatch.setattr(sn, "background_tools", lambda: (None, None))  # not idle, or no model
    assert fh.auto(app) == {} and fh.pending(Vault(app.root))
    monkeypatch.setattr(sn, "background_tools", lambda: (r, None))
    assert fh.auto(app)["saved"] == 1
    assert "tidy" in git(app.root, "log", "-1", "--format=%s")
    assert fh.auto(app) is None  # nothing new: no model probe
