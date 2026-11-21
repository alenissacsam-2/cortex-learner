"""R3-5: the //pref:, //fact: and //forget: tags the owner types in a chat, caught by code in
user turns only (cortex.tags): through cortex_prime, a chat_turn's `user`, the plugin's prime
hook and the chat import; the exposed downgrade; forget with 0, 1 and 2 matches; dedupe."""

import json
import os
import time

import pytest
from conftest import events, run, tiers

from cortex import chat_import, prime_hook, review, tags
from cortex.events import EventLog, local_day
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

PREFS = ("---\nsummary: My preferences\n---\n\n# My preferences\n\n## Coding\n\n"
         "- Uses tabs for indentation\n- Writes Python with type hints\n"
         "- Writes Rust for small CLI tools\n\n## Past\n\n## Not me\n")


def _me(root, page):
    return (root / "me" / f"{page}.md").read_text(encoding="utf-8")


def _changes(root):
    return [e["payload"]["change"] for e in events(root) if e.get("kind") == "change"
            and isinstance(e["payload"].get("change"), dict)]


def test_find_reads_only_tags():
    text = ("hi //pref: Short answers, please \n//FACT:  I live in   Kochi\n"
            "see https://pref:x and a//fact: no\n//forget:\n//pref: Short answers, please")
    assert tags.find(text) == [("pref", "Short answers, please"), ("fact", "I live in Kochi")]
    assert tags.find("") == [] and tags.find("// pref: spaced") == []


def test_pref_and_fact_through_prime_and_chat_turn_are_saved_once(root):
    async def body(call):
        out = await call("cortex_prime", {"prompt": "//pref: Answers without emoji\nhello"})
        assert out["tags"] == ["//pref saved to me/preferences"]
        assert "do not save them again" in out["next"]
        # the same turn logged again, and a fact in a later turn; the gist is never read
        turn = await call("cortex_log", {"kind": "chat_turn", "payload": {
            "user": "//pref: Answers without emoji\n//fact: I study at IIT Madras",
            "gist": "//pref: Assistant text is never a tag"}})
        assert turn["tags"] == ["//fact saved to me/profile"]

    run(root, body)
    prefs, profile = _me(root, "preferences"), _me(root, "profile")
    assert prefs.count("Answers without emoji") == 1 and "## Personal" in prefs
    assert "Assistant text" not in prefs + profile
    assert "- I study at IIT Madras" in profile.split("## Background", 1)[1]
    t = tiers(root)
    assert t["Answers without emoji"] == "confirmed" and t["I study at IIT Madras"] == "confirmed"
    stamped = [c for c in _changes(root) if c.get("tool") == "patch" and c.get("claim")]
    assert {c["confirm"] for c in stamped} == {"tagged"} and len(stamped) == 2


def test_an_exposed_chat_only_proposes(root):
    async def body(call):
        await call("cortex_add", {"kind": "resource", "title": "A web page",
                                  "raw_text": "outside text"})  # the chat read outside text
        out = await call("cortex_log", {"kind": "chat_turn", "payload": {
            "user": "//pref: Dark mode everywhere\n//fact: I own a cat\n"
                    "//forget: Uses tabs for indentation", "gist": "ok"}})
        assert len(out["tags"]) == 3 and "Approve" in out["tags"][0]

    (root / "me/preferences.md").write_text(PREFS, encoding="utf-8")
    run(root, body)
    inbox = (root / "inbox.md").read_text(encoding="utf-8")
    assert "- Personal: Dark mode everywhere (proposed" in inbox
    assert "- Background: I own a cat (proposed" in inbox
    assert "Dark mode" not in _me(root, "preferences") and "cat" not in _me(root, "profile")
    assert "- Uses tabs for indentation" in _me(root, "preferences").split("## Past")[0]
    v = Vault(root)
    (card,) = review.view(v, EventLog(v).iter_events(), local_day_now())["forget"]
    assert [c["text"] for c in card["candidates"]] == ["Uses tabs for indentation"]


def local_day_now():
    from datetime import UTC, datetime
    return local_day(datetime.now(UTC))


def test_forget_with_one_two_and_no_matches(root):
    (root / "me/preferences.md").write_text(PREFS, encoding="utf-8")

    async def body(call):
        one = await call("cortex_prime", {"prompt": "//forget: tabs for indentation"})
        assert one["tags"] == ["//forget retired a line on me/preferences"]
        two = await call("cortex_log", {"kind": "chat_turn", "payload": {
            "user": "//forget: writes python rust", "gist": "x"}})  # both Writes lines
        assert "nothing changed" in two["tags"][0]
        none = await call("cortex_log", {"kind": "chat_turn", "payload": {
            "user": "//forget: my bicycle", "gist": "x"}})
        assert "no line" in none["tags"][0]

    run(root, body)
    prefs = _me(root, "preferences")
    past = prefs.split("## Past", 1)[1]
    assert "Uses tabs for indentation" in past.split("## Not me")[0]
    assert "Writes Python" in prefs.split("## Past")[0] and "Writes Rust" in prefs.split(
        "## Past")[0]
    v = Vault(root)
    cards = review.view(v, EventLog(v).iter_events(), local_day_now())
    by = {c["text"]: c for c in cards["forget"]}
    assert set(by) == {"writes python rust", "my bicycle"} and cards["count"] >= 2
    assert {c["text"] for c in by["writes python rust"]["candidates"]} == {
        "Writes Python with type hints", "Writes Rust for small CLI tools"}
    assert by["my bicycle"]["candidates"] == []
    # retiring a candidate closes its card; Dismiss (a `dismiss` change) closes the other
    from cortex.mcp_server import Cortex
    core = Cortex(v)
    core.app_clicks = True
    rid = next(r["id"] for r in tags.candidates(v, "Writes Rust", local_day_now())
               if r["text"].startswith("Writes Rust"))
    core._call("cortex_patch", core.cortex_patch, {"id": "me/preferences", "op": "retire",
                                                   "target": rid, "user_confirmed": True})
    core._call("cortex_patch", core._dismiss, {"page": "forget", "section": "Forget",
                                               "text": by["my bicycle"]["key"],
                                               "waiting": False})
    core.sessions.close(summary="test")
    assert tags.forget_cards(v, EventLog(v).iter_events(), local_day_now()) == []


def test_prime_hook_saves_tags_on_every_prompt_once(root, monkeypatch):
    monkeypatch.setenv("CORTEX_VAULT", str(root))

    def hook(prompt):
        out = prime_hook.run(json.dumps({"session_id": "chat-1", "prompt": prompt,
                                         "hook_event_name": "UserPromptSubmit"}))
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""

    assert "//pref" not in hook("hello there")  # primes the chat
    ctx = hook("//pref: Replies in British English")  # a later prompt: only the note
    assert ctx.startswith("Cortex saved the user's") and "me/preferences" in ctx
    assert hook("//pref: Replies in British English") == ""  # seen today already
    assert _me(root, "preferences").count("Replies in British English") == 1
    assert tiers(root)["Replies in British English"] == "confirmed"

    async def body(call):  # the same turn through the server afterwards: nothing new
        out = await call("cortex_prime", {"prompt": "//pref: Replies in British English",
                                          "related_only": True})
        assert "tags" not in out

    run(root, body)
    assert _me(root, "preferences").count("Replies in British English") == 1


def _transcript(code, lines):
    p = code / "D--Dev-proj" / "s1.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    t = time.time() - 3600  # not a live chat
    os.utime(p, (t, t))
    return p


def test_import_reads_user_turns_only_and_once(root, tmp_path, monkeypatch):
    code, exp = tmp_path / "projects", tmp_path / "exports"
    code.mkdir()
    exp.mkdir()
    monkeypatch.setenv("CORTEX_CHATS_DIR", str(code))
    monkeypatch.setenv("CORTEX_CHAT_EXPORT_DIR", str(exp))
    ts = "2026-11-10T09:00:00Z"

    def turn(kind, text):
        content = text if kind == "user" else [{"type": "text", "text": text}]
        return {"type": kind, "timestamp": ts, "cwd": "D:\\x",
                "message": {"role": kind, "content": content}}

    tool = {"type": "user", "timestamp": ts, "cwd": "D:\\x", "message": {"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t0",
                         "content": "//pref: from a tool output"}]}}
    (root / "me/preferences.md").write_text(PREFS, encoding="utf-8")
    lines = [turn("user", "Plan my week\n//fact: I play the violin\n"
                          "//forget: tabs for indentation"),
             turn("assistant", "//pref: an assistant line"), tool]
    p = _transcript(code, lines)
    chat_import.run(Vault(root))
    profile, prefs = _me(root, "profile"), (root / "me/preferences.md")
    assert profile.count("I play the violin") == 1
    assert not prefs.exists() or "assistant line" not in prefs.read_text(encoding="utf-8")
    assert "tool output" not in json.dumps(events(root))
    # an imported forget may predate the line it matches: a Review card, never a retire
    assert "## Coding\n\n- Uses tabs for indentation" in _me(root, "preferences")
    (card,) = review.view(Vault(root), EventLog(Vault(root)).iter_events(),
                          local_day_now())["forget"]
    assert card["text"] == "tabs for indentation"
    p.write_text(p.read_text(encoding="utf-8") + json.dumps(turn("user", "thanks")) + "\n",
                 encoding="utf-8")  # the chat grows: a re-import
    os.utime(p, (time.time() - 3500,) * 2)
    chat_import.run(Vault(root))
    assert _me(root, "profile").count("I play the violin") == 1
    day = local_day(chat_import._when(ts))
    assert EventLog(Vault(root)).has_key(tags.key("fact", "I play the violin", day))


def _import_one(root, tmp_path, monkeypatch, text):
    code, exp = tmp_path / "projects", tmp_path / "exports"
    code.mkdir()
    exp.mkdir()
    monkeypatch.setenv("CORTEX_CHATS_DIR", str(code))
    monkeypatch.setenv("CORTEX_CHAT_EXPORT_DIR", str(exp))
    _transcript(code, [{"type": "user", "timestamp": "2026-11-10T09:00:00Z", "cwd": "D:\\x",
                        "message": {"role": "user", "content": text}}])
    return chat_import.run(Vault(root))


def test_a_private_chats_tags_never_reach_the_shared_me_pages(root, tmp_path, monkeypatch):
    """Audit 21 Nov I2: a health chat is imported as a private page; its //fact stays in it."""
    out = _import_one(root, tmp_path, monkeypatch,
                      "what do these symptoms mean\n//fact: I have epilepsy")
    assert out == {"saved": 1}
    profile = root / "me" / "profile.md"
    assert not profile.exists() or "epilepsy" not in profile.read_text(encoding="utf-8")
