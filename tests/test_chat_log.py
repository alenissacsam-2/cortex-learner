"""Every chat logged: keepfold_prime starts a Claude app's live chat log (redacted, server-stamped),
keepfold_log(kind="chat_turn") adds the later turns (grouped by chat id, else this server's
current chat), chat_import turns the log into a chat page (idempotent, updated in place),
Cowork transcripts are imported too, the instructions and skill say so, and `keepfold doctor`
shows per client whether Keepfold is configured, the prime hook and the last call."""

import json
import os
import time
from datetime import datetime
from pathlib import Path

import anyio
import pytest
from conftest import events
from mcp import Client
from mcp.types import Implementation

from keepfold import chat_import, chats, doctor, harvest, mcp_server
from keepfold.mcp_server import build_server
from keepfold.vault import CHAT_ARCHIVE, Vault

pytestmark = pytest.mark.usefixtures("home")

ROOT = Path(__file__).resolve().parents[1]
KEY = "sk-" + "a" * 24  # a secret pattern


def serve(root, body, client="claude-ai", role="owner"):
    """`body(call)` against a fresh server whose client names itself `client`."""
    server = build_server(Vault(root), role)

    async def main():
        async with Client(server, client_info=Implementation(name=client, version="1")) as c:
            async def call(name, args=None):
                r = await c.call_tool(name, args or {})
                assert not r.is_error, r.content
                out = r.structured_content
                assert out.get("next"), out
                return out

            await body(call)

    anyio.run(main)


def live():
    return chats.export_root() / chats.LIVE


def lines(cid):
    return [json.loads(x) for x in (live() / f"{cid}.jsonl").read_text("utf-8").splitlines()]


def turn(cid, user, gist="", **kw):
    return {"kind": "chat_turn", "payload": {"chat": cid, "user": user, "gist": gist, **kw}}


def test_prime_starts_a_redacted_log_and_turns_join_it(root):
    got = {}

    async def body(call):
        p = await call("keepfold_prime", {"prompt": f"How do heaps work? my key is {KEY}"})
        got["chat"] = cid = p["chat"]
        assert "chat_turn" in p["next"] and cid in p["next"]
        r = await call("keepfold_log", turn(cid, "And heapify?", "Heaps are trees."))
        assert (r["recorded"], r["chat"]) == (1, cid)
        await call("keepfold_log", turn(cid, "And heapify?", "Heaps are trees."))  # a retry
        await call("keepfold_log", {"kind": "chat_turn", "payload": {"user": "Sift down?"}})

    serve(root, body)
    cid = got["chat"]
    assert chats.LIVE_ID.fullmatch(cid) and [p.stem for p in live().glob("*.jsonl")] == [cid]
    got_lines = lines(cid)
    assert [(x["type"], x["message"]["content"]) for x in got_lines] == [
        ("user", "How do heaps work? my key is [redacted]"),
        ("assistant", "Heaps are trees."), ("user", "And heapify?"), ("user", "Sift down?")]
    assert all(datetime.fromisoformat(x["timestamp"]).tzinfo for x in got_lines)  # stamped
    assert KEY not in (live() / f"{cid}.jsonl").read_text("utf-8")
    # nothing in the vault: no session, no event
    assert events(root) == []


def test_a_new_prime_starts_a_new_chat_and_an_old_id_still_joins_its_own(root):
    ids = []

    async def body(call):
        ids.append((await call("keepfold_prime", {"prompt": "First chat"}))["chat"])
        ids.append((await call("keepfold_prime", {"prompt": "Second chat"}))["chat"])
        await call("keepfold_log", turn("not-an-id", "no id: the current chat"))
        await call("keepfold_log", turn(ids[0], "back in the first chat"))
        await call("keepfold_log", turn(None, "no id again: the chat touched last"))

    serve(root, body)
    assert ids[0] != ids[1]
    users = {c: [x["message"]["content"] for x in lines(c) if x["type"] == "user"] for c in ids}
    assert users[ids[0]] == ["First chat", "back in the first chat",
                             "no id again: the chat touched last"]
    assert users[ids[1]] == ["Second chat", "no id: the current chat"]

    async def later(call):  # a restarted server: the id from prime still finds the chat
        await call("keepfold_log", turn(ids[1], "after a restart"))

    serve(root, later)
    assert lines(ids[1])[-1]["message"]["content"] == "after a restart"


@pytest.mark.parametrize("client, role", [("claude-code", "owner"), ("mcp", "owner"),
                                          ("claude-ai", "reader")])
def test_no_log_where_transcripts_cover_it_or_not_the_owner(root, client, role):
    async def body(call):
        p = await call("keepfold_prime", {"prompt": "Hello"})
        assert "chat" not in p and "chat_turn" not in p["next"]
        r = await call("keepfold_log", turn("x", "more"))
        assert r["recorded"] == 0

    serve(root, body, client, role)
    assert not live().exists()


def test_a_chat_turn_needs_the_user_text(root):
    async def body(call):
        r = await call("keepfold_log", {"kind": "chat_turn", "payload": {"gist": "x"}})
        assert r["error"] == "invalid_argument" and "payload.user" in r["message"]

    serve(root, body)


def test_the_live_log_becomes_a_chat_page_once_and_grows_in_place(root_committed):
    root, got = root_committed, {}

    async def body(call):
        primed = await call("keepfold_prime", {"prompt": "Plan my DP revision"})
        got["chat"] = cid = primed["chat"]
        await call("keepfold_log", turn(cid, f"I prefer short drills, key {KEY}", "A 3-week plan."))

    serve(root, body)
    vault, later = Vault(root), time.time() + 600  # past chats.LIVE_SECONDS
    assert chat_import.run(vault, now=later) == {"saved": 1}
    p = next(e["payload"] for e in events(root) if e.get("payload", {}).get("kind") == "chat_saved")
    assert (p["source"], p["chat_id"], p["auto"]) == (chats.LIVE, f"{chats.LIVE}/{got['chat']}",
                                                      True)
    body_text = (root / CHAT_ARCHIVE / f"{p['pages'][0]}.md").read_text("utf-8")
    assert "Source: Claude · " in body_text and chat_import.MINE in body_text
    assert "A 3-week plan." not in body_text and KEY not in body_text  # my turns only, redacted
    assert harvest.user_turns(body_text.split("---\n", 2)[2]) == [
        "Plan my DP revision", "I prefer short drills, key [redacted]"]
    assert chat_import.run(vault, now=later) == {}  # idempotent: nothing pending

    async def more(call):
        await call("keepfold_log", turn(got["chat"], "And knapsack?", "Did the plan."))

    serve(root, more)
    assert chat_import.run(vault, now=time.time() + 600) == {"updated": 1}
    assert "**Me:** And knapsack?" in (root / CHAT_ARCHIVE / f"{p['pages'][0]}.md").read_text(
        "utf-8")
    listed = chats.list_chats(set(), limit=5)
    assert [(c["id"], c["source"]) for c in listed] == [(p["chat_id"], chats.LIVE)]


def test_cowork_transcripts_are_imported(root_committed):
    base = Path(os.environ["LOCALAPPDATA"]) / "Claude-Data" / "claude_x" / \
        "local-agent-mode-sessions" / "acct" / "org" / "local_1" / ".claude" / "projects" / "s"
    base.mkdir(parents=True)
    t = base / "abc.jsonl"
    t.write_text(json.dumps({"type": "user", "timestamp": "2026-11-10T09:00:00Z",
                             "message": {"content": "Make me a study timetable"}}) + "\n",
                 encoding="utf-8")
    (base.parents[2] / "audit.jsonl").write_text("{}\n", encoding="utf-8")  # not a transcript
    assert list(chats.cowork_files()) == [t]
    assert chat_import.run(Vault(root_committed), now=time.time() + 600) == {"saved": 1}


def test_instructions_and_skill_say_every_chat_is_logged():
    text = mcp_server.INSTRUCTIONS
    assert "Every chat is logged in Keepfold; say so if asked" in text
    assert "keepfold_log(kind='chat_turn'" in text and len(text) < 1600
    skill = (ROOT / "skills" / "keepfold" / "SKILL.md").read_text("utf-8")
    assert "Every chat is logged in Keepfold" in skill and 'kind="chat_turn"' in skill
    assert "every chat is logged" in skill.split("\n---\n", 1)[0]  # the description, too
    plugin = (ROOT / "plugin" / "skills" / "keepfold" / "SKILL.md").read_text("utf-8")
    assert skill.split("\n---\n", 1)[1] == plugin.split("\n---\n", 1)[1]  # rebuilt


# ---- keepfold doctor ------------------------------------------------------------------------

def rows(root, home, **kw):
    return {name: (status, detail, fix) for status, name, detail, fix in
            doctor.check_clients(root, home / "settings.json", **kw)}


def test_doctor_shows_each_client_and_its_last_call(root, home):
    desktop = home / "claude_desktop_config.json"
    assert rows(root, home, desktop_configs=[]) == {
        "Claude Desktop (Chat)": ("PASS", "not installed", ""),
        "Claude Code": ("PASS", "not installed", "")}
    desktop.write_text("{}", encoding="utf-8")
    got = rows(root, home, desktop_configs=[desktop])["Claude Desktop (Chat)"]
    assert got[0] == "WARN" and "not logged" in got[1] and "Connect" in got[2]
    entry = {"command": "keepfold", "args": ["mcp", "--vault", "v"]}
    desktop.write_text(json.dumps({"mcpServers": {"keepfold": entry}}), encoding="utf-8")
    got = rows(root, home, desktop_configs=[desktop])["Claude Desktop (Chat)"]
    assert got[0] == "WARN" and "no call recorded yet" in got[1] and "tray" in got[2]

    async def body(call):  # any call marks the client
        await call("keepfold_find", {"query": "x"})

    serve(root, body)
    serve(root, body, client="claude-code")
    assert set(doctor.last_calls(root)) == {"claude-ai", "claude-code"}
    got = rows(root, home, desktop_configs=[desktop])
    assert got["Claude Desktop (Chat)"][0] == "PASS" and "last call 20" in \
        got["Claude Desktop (Chat)"][1]
    (home / ".claude").mkdir()  # Claude Code installed, Keepfold only from the desktop config
    code = rows(root, home, desktop_configs=[desktop])["Claude Code"]
    assert code[0] == "WARN" and "desktop config only" in code[1]
    assert "prime hook missing" in code[1] and "plugin" in code[2]
    (home / "settings.json").write_text(json.dumps({"enabledPlugins": {"keepfold@keepfold": True}}),
                                        encoding="utf-8")
    got = rows(root, home, desktop_configs=[desktop])["Claude Code"]
    assert got[0] == "PASS" and "from plugin; prime hook installed; last call" in got[1]


def test_the_client_mark_is_written_at_most_once_a_minute(root, monkeypatch):
    monkeypatch.setattr(mcp_server, "SEEN_EVERY", 3600.0)

    async def body(call):
        await call("keepfold_find", {"query": "x"})
        mark = root / mcp_server.SEEN / "claude-ai.txt"
        os.utime(mark, (1, 1))
        await call("keepfold_find", {"query": "y"})
        assert mark.stat().st_mtime == 1  # not rewritten inside SEEN_EVERY

    serve(root, body)


def test_doctor_runs_the_client_rows(root, home):
    from typer.testing import CliRunner

    from keepfold.cli import app

    r = CliRunner().invoke(app, ["doctor", "--vault", str(root)])
    assert "Claude Desktop (Chat): not installed" in r.output
    assert "Claude Code: not installed" in r.output
