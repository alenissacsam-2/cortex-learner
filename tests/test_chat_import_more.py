"""R8: the automatic chat import (keepfold.chat_import) also reads OpenAI Codex CLI, Google Gemini
CLI and GitHub Copilot CLI sessions from a temp home. Fixtures are written by hand from each
tool's published format (openai/codex rollout lines, gemini-cli chatRecordingService records,
the Copilot session events of microsoft/copilot-sdk), never copied from a real session."""

import json
import os
import time

import pytest
from test_app import app as app  # noqa: F401 (fixture)
from test_app import fakes as fakes  # noqa: F401 (fixture: nothing real runs or opens)
from test_app import vault as vault  # noqa: F401 (fixture)
from test_chat_import import KEY, T0, page, saved
from test_chat_import import dirs as dirs  # noqa: F401 (fixture)

from keepfold import chat_import, taint
from keepfold.events import local_day
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home", "dirs")

CWD = "D:\\Dev\\proj"
T1 = "2026-11-10T09:05:00Z"
CODEX_ID = "rollout-2026-11-10T09-00-00-0199a213-81c0-7800-8aa1-bbab2a035a53"


def write(path, lines, age=3600, mode="w"):
    """JSON lines (a str line as it is), the file last touched `age` seconds ago (not live)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as f:
        f.write("".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines))
    t = time.time() - age
    os.utime(path, (t, t))
    return path


def home():
    from pathlib import Path
    return Path.home()


# ---- Codex ---------------------------------------------------------------------------------------

def cx(kind, payload, ts=T0):
    return {"timestamp": ts, "type": kind, "payload": payload}


def said(text, ts=T0, role="user"):
    kind = "input_text" if role == "user" else "output_text"
    return cx("response_item", {"type": "message", "role": role,
                                "content": [{"type": kind, "text": text}]}, ts)


def meta(source="cli", cwd=CWD):
    return cx("session_meta", {"id": "0199a213", "session_id": "0199a213", "timestamp": T0,
                               "cwd": cwd, "originator": "codex_cli_rs", "cli_version": "0.1.0",
                               "source": source})


def codex(name=CODEX_ID, lines=(), **kw):
    return write(home() / ".codex" / "sessions" / "2026" / "11" / "10" / f"{name}.jsonl",
                 lines, **kw)


def test_a_codex_session_is_a_coding_summary_without_code(root_committed):
    codex(lines=[
        meta(),
        said("<environment_context>\n  <cwd>D:\\Dev\\proj</cwd>\n</environment_context>"),
        said("# AGENTS.md instructions for D:\\Dev\\proj\n\n<INSTRUCTIONS>\nENV-RULES\n"
             "</INSTRUCTIONS>"),
        said("Fix the heap bug\nit pops the wrong end"),
        said("ASSISTANT-TEXT", role="assistant"),
        cx("response_item", {"type": "reasoning", "summary": [], "encrypted_content": "x"}),
        cx("response_item", {"type": "custom_tool_call", "call_id": "c1", "name": "apply_patch",
                             "input": "*** Begin Patch\n*** Update File: src/heap.py\n@@\n"
                                      "-SECRET-CODE\n+NEW-CODE\n*** End Patch\n"}),
        cx("response_item", {"type": "function_call", "call_id": "c2", "name": "shell_command",
                             "arguments": json.dumps({"command": 'git commit -m "Fix heap order"'
                                                      })}),
        cx("response_item", {"type": "function_call_output", "call_id": "c2",
                             "output": "TOOL-OUTPUT"}),
        cx("event_msg", {"type": "user_message", "message": "Fix the heap bug"}),
        cx("brand_new_record", {"anything": 1}),  # a type the reader does not know: skipped
        {"timestamp": T1},  # no payload
        said("Now add a test", ts=T1)])
    assert chat_import.run(Vault(root_committed)) == {"saved": 1}
    body = page(root_committed, "codex", f"10/{CODEX_ID}")
    assert 'title: "proj: Fix the heap bug"' in body
    assert f"Source: Codex · Date: {local_day(chat_import._when(T0))}" in body
    assert "\nWho: me and Codex\n" in body and "2 requests, 1 file changed, 1 commit" in body
    assert "- Fix the heap bug\n- Now add a test" in body
    assert "`src/heap.py`" in body and "- Fix heap order" in body
    for gone in ("SECRET-CODE", "NEW-CODE", "ASSISTANT", "ENV-RULES", "environment_context",
                 "TOOL-OUTPUT", "AGENTS.md"):
        assert gone not in body, gone


def test_a_codex_chat_grows_redacted_and_its_sub_agents_are_skipped(root_committed,
                                                                    monkeypatch):
    p = codex(lines=[meta(), said(f"Explain tides <private>MY-SECRET-PLAN</private> key {KEY}")])
    codex("rollout-2026-11-10T09-00-00-sub", [
        meta({"subagent": {"thread_spawn": {"parent_thread_id": "0199a213", "depth": 1}}}),
        said("SUBAGENT-PROMPT")])
    codex("rollout-2026-11-10T09-00-00-broken", ["not json", "{broken"])
    v = Vault(root_committed)
    assert chat_import.run(v) == {"saved": 1, "empty": 2}
    body = page(root_committed, "codex", f"10/{CODEX_ID}")
    assert "**Me:** Explain tides [private] key [redacted]" in body
    assert "MY-SECRET-PLAN" not in body and KEY not in body and "Where:" not in body
    assert "SUBAGENT" not in "".join(x["title"] for x in saved(root_committed))
    offsets = []
    real = chat_import.read
    monkeypatch.setattr(chat_import, "read", lambda path, prev=None, *a: offsets.append(
        (prev or {}).get("offset")) or real(path, prev, *a))
    write(p, [said("And the moon?", ts=T1)], mode="a")
    assert chat_import.run(v) == {"updated": 1}
    assert offsets and offsets[0] > 0  # read on from where it stopped
    body = page(root_committed, "codex", f"10/{CODEX_ID}")
    assert body.count("**Me:**") == 2 and body.count("**Me:** Explain tides") == 1
    assert "**Me:** And the moon?" in body


def test_a_codex_session_in_an_excluded_folder_and_a_skip_category(root_committed, tmp_path):
    secret = tmp_path / "work"
    codex(lines=[meta(cwd=str(secret)), said("work stuff")])
    codex("rollout-2026-11-10T09-00-00-adult", [meta(), said("where to find porn")])
    codex("rollout-2026-11-10T09-00-00-health", [meta(), said("what do these symptoms mean")])
    taint.write_config({"exclude": [str(secret)], "browse": {"skip_categories": ["adult"]}})
    v = Vault(root_committed)
    assert chat_import.run(v) == {"excluded_folder": 1, "skipped_category": 1, "saved": 1}
    from keepfold.events import EventLog
    rows = chat_import.made_private(v, EventLog(v).iter_events())
    assert [(r["app"], r["category"]) for r in rows] == [("Codex", "health")]


# ---- Gemini CLI ----------------------------------------------------------------------------------

def gem(lines, name="session-2026-11-10T09-00-abcd1234.jsonl", root=True, **kw):
    d = home() / ".gemini" / "tmp" / "proj"
    if root:
        d.mkdir(parents=True, exist_ok=True)
        (d / ".project_root").write_text(CWD, encoding="utf-8")
    return write(d / "chats" / name, lines, **kw)


def g(i, kind, content, ts=T0, **kw):
    return {"id": i, "timestamp": ts, "type": kind, "content": content, **kw}


GEM_META = {"sessionId": "abcd1234-0000", "projectHash": "h", "startTime": T0,
            "lastUpdated": T0, "kind": "main"}


def test_a_gemini_cli_session_replays_rewinds_and_patches(root_committed):
    p = gem([GEM_META,
             g("m1", "user", [{"text": "How do tides work?\n--- notes.md ---\nFILE-CONTENT"}],
               displayContent=[{"text": "How do tides work?"}]),
             g("m2", "gemini", [{"text": "GEMINI-ANSWER"}], toolCalls=[], model="gemini"),
             g("m3", "user", "REWOUND-TURN", ts="2026-11-10T09:02:00Z"),
             {"$rewindTo": "m3"},
             g("m4", "user", "And the moon?", ts=T1),
             {"$patch": {"id": "m4", "content": [{"text": f"What about the moon? {KEY}"}]}},
             g("m5", "info", "INFO-LINE"),
             {"$set": {"summary": "tides"}},
             {"$brandNew": {"x": 1}},  # a record the reader does not know: skipped
             "not json"])
    # a legacy .json with a .jsonl of the same name is the same chat: read once
    write(p.with_suffix(".json"), [json.dumps({**GEM_META, "messages": [
        g("m1", "user", "LEGACY-COPY")]})])
    v = Vault(root_committed)
    assert chat_import.run(v) == {"saved": 1}
    body = page(root_committed, "gemini-cli", "chats/session-2026-11-10T09-00-abcd1234")
    assert 'title: "How do tides work?"' in body and "Source: Gemini CLI · " in body
    assert "**Me:** How do tides work?\n" in body and "What about the moon? [redacted]" in body
    for gone in ("FILE-CONTENT", "GEMINI-ANSWER", "REWOUND", "And the moon?", "INFO-LINE",
                 "LEGACY-COPY", KEY):
        assert gone not in body, gone
    write(p, [g("m6", "user", "One more question", ts="2026-11-10T09:10:00Z")], mode="a")
    assert chat_import.run(v) == {"updated": 1}
    body = page(root_committed, "gemini-cli", "chats/session-2026-11-10T09-00-abcd1234")
    assert body.count("**Me:**") == 3 and "**Me:** One more question" in body


def test_a_gemini_cli_coding_session_and_a_legacy_json_file(root_committed):
    gem([GEM_META, g("m1", "user", "Write my notes file"),
         g("m2", "gemini", "", toolCalls=[
             {"id": "t1", "name": "write_file", "status": "success", "timestamp": T0,
              "args": {"file_path": "D:\\Dev\\proj\\notes.md", "content": "FILE-BODY"},
              "result": [{"text": "TOOL-RESULT"}]},
             {"id": "t2", "name": "run_shell_command", "status": "success", "timestamp": T0,
              "args": {"command": "git commit -m 'Add notes'"}}])])
    gem([json.dumps({**GEM_META, "sessionId": "old", "messages": [
        g("a", "user", "An old question"), g("b", "gemini", "OLD-ANSWER")]})],
        name="session-2026-11-01T09-00-old00000.json", root=False)
    gem(["{not whole json"], name="session-2026-11-02T09-00-bad00000.json", root=False)
    v = Vault(root_committed)
    assert chat_import.run(v) == {"saved": 2, "empty": 1}
    body = page(root_committed, "gemini-cli", "chats/session-2026-11-10T09-00-abcd1234")
    assert 'title: "proj: Write my notes file"' in body and "`notes.md`" in body
    assert "- Add notes" in body
    old = page(root_committed, "gemini-cli", "chats/session-2026-11-01T09-00-old00000")
    assert "**Me:** An old question" in old and "OLD-ANSWER" not in old
    for gone in ("FILE-BODY", "TOOL-RESULT"):
        assert gone not in body, gone


def test_only_gemini_cli_chat_files_are_read(root_committed):
    d = home() / ".gemini" / "tmp" / "proj"
    gem([GEM_META, g("m1", "user", "A real chat")])
    write(d / "chats" / "abcd1234-0000" / "sub-agent.jsonl", [g("s", "user", "SUBAGENT")])
    write(d / "logs.json", ["[]"])
    write(d / "shell_history", ["SHELL-HISTORY"])
    write(d / "chats" / "session-2026-11-10T09-00-sub00000.jsonl",
          [{**GEM_META, "kind": "subagent"}, g("s", "user", "SUBAGENT-TOO")])
    assert chat_import.run(Vault(root_committed)) == {"saved": 1, "empty": 1}
    assert [x["title"] for x in saved(root_committed)] == ["A real chat"]


# ---- Copilot CLI ---------------------------------------------------------------------------------

def ev(kind, data, ts=T0, **kw):
    return {"id": f"e-{kind}-{ts}", "parentId": None, "timestamp": ts, "type": kind,
            "data": data, **kw}


def copilot(lines, sid="1b2c3d4e-0000", **kw):
    return write(home() / ".copilot" / "session-state" / sid / "events.jsonl", lines, **kw)


def test_a_copilot_cli_session_is_a_coding_summary(root_committed):
    copilot([
        ev("session.start", {"sessionId": "1b2c3d4e-0000", "version": 1, "startTime": T0,
                             "producer": "copilot-agent", "context": {"cwd": CWD}}),
        ev("user.message", {"content": "Add a test for heaps",
                            "transformedContent": "TRANSFORMED-PROMPT"}),
        ev("assistant.message", {"messageId": "m", "content": "ASSISTANT-TEXT", "toolRequests": [
            {"toolCallId": "t1", "name": "edit", "arguments": {"path": "x"}}]}),
        ev("tool.execution_complete", {"toolCallId": "t1", "success": True,
                                       "fileEdits": [{"kind": "edit",
                                                      "path": "D:\\Dev\\proj\\tests\\t.py"}],
                                       "result": {"content": "TOOL-OUTPUT"}}),
        ev("tool.execution_start", {"toolCallId": "t2", "toolName": "bash",
                                    "arguments": {"command": 'git commit -m "Add heap test"'},
                                    "shellToolInfo": {"hasWriteFileRedirection": False,
                                                      "possiblePaths": []}}),
        ev("user.message", {"content": "SUBAGENT-PROMPT"}, agentId="sub1"),
        ev("user.message", {"content": "CHECKPOINT-REPLAY"}, checkpoint=True),
        ev("user.message", {"content": "AGENT-KICKOFF", "agentAuthored": True}),
        ev("session.brand_new_event", {"x": 1}),  # a type the reader does not know: skipped
        ev("user.message", {"content": "Run it again <private>MY-NOTE</private>"}, ts=T1)])
    copilot(["not json", "{broken"], sid="broken-0000")
    v = Vault(root_committed)
    assert chat_import.run(v) == {"saved": 1, "empty": 1}
    body = page(root_committed, "copilot-cli", "1b2c3d4e-0000/events")
    assert 'title: "proj: Add a test for heaps"' in body and "Source: Copilot CLI · " in body
    assert "2 requests, 1 file changed, 1 commit" in body and "`tests/t.py`" in body
    assert "- Add heap test" in body and "- Run it again [private]" in body
    for gone in ("TRANSFORMED", "ASSISTANT", "TOOL-OUTPUT", "SUBAGENT", "CHECKPOINT",
                 "AGENT-KICKOFF", "MY-NOTE"):
        assert gone not in body, gone


def test_the_switch_off_imports_none_of_them(app):  # noqa: F811
    codex(lines=[meta(), said("a codex question")])
    gem([GEM_META, g("m1", "user", "a gemini question")])
    copilot([ev("user.message", {"content": "a copilot question"})])
    taint.write_config({"app": {"chat_import": False}})
    assert app.chat_import_auto() is None and saved(app.root) == []
    taint.write_config({"app": {"chat_import": True}})
    assert {s for s, _, _ in chat_import.pending(Vault(app.root))} \
        == {"codex", "gemini-cli", "copilot-cli"}
