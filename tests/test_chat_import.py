"""The automatic chat import (cortex.chat_import): synthetic Claude Code transcripts and export
conversations in temp CORTEX_CHATS_DIR / CORTEX_CHAT_EXPORT_DIR, vault_mini, a temp home.
Ordinary pages hold the user's turns only, redacted; coding pages a capped summary with no code;
re-runs are idempotent and stat-only; a changed transcript updates its page in place; live,
sidechain, empty, hand-saved, skip-category and excluded chats are left out; the app's job
reports progress and notifies once; the setting turns it off."""

import json
import os
import threading
import time
import types

import pytest
from conftest import events, git, note
from test_app import app as app  # noqa: F401 (fixture)
from test_app import call
from test_app import fakes as fakes  # noqa: F401 (fixture: nothing real runs or opens)
from test_app import vault as vault  # noqa: F401 (fixture)
from typer.testing import CliRunner

from cortex import chat_import, chats, harvest, taint
from cortex.app import desktop, server
from cortex.events import local_day
from cortex.vault import CHAT_ARCHIVE, Vault

pytestmark = pytest.mark.usefixtures("home")

KEY = "sk-" + "a" * 24  # a secret pattern
CWD = "D:\\Dev\\proj"
T0 = "2026-11-10T09:00:00Z"
CHATS = CHAT_ARCHIVE  # where the import writes chat pages (C5)


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    code, exp = tmp_path / "projects", tmp_path / "exports"
    code.mkdir()
    exp.mkdir()
    monkeypatch.setenv("CORTEX_CHATS_DIR", str(code))
    monkeypatch.setenv("CORTEX_CHAT_EXPORT_DIR", str(exp))
    return code, exp


def write(path, lines, age=3600):
    """A transcript last touched `age` seconds ago (not live)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    t = time.time() - age
    os.utime(path, (t, t))
    return path


def u(text, ts=T0, **kw):
    return {"type": "user", "timestamp": ts, "cwd": CWD,
            "message": {"role": "user", "content": text}, **kw}


def a(text="", ts=T0, tools=()):
    content = [{"type": "text", "text": text}] if text else []
    content += [{"type": "tool_use", "id": f"t{i}", "name": n, "input": x}
                for i, (n, x) in enumerate(tools)]
    return {"type": "assistant", "timestamp": ts, "cwd": CWD,
            "message": {"role": "assistant", "content": content}}


def result(text):
    return {"type": "user", "timestamp": T0, "cwd": CWD, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t0", "content": text}]}}


def saved(root):
    return [e["payload"] for e in events(root)
            if isinstance(e.get("payload"), dict) and e["payload"].get("kind") == "chat_saved"]


def page(root, source, cid):
    p = [x for x in saved(root) if (x["source"], x["chat_id"]) == (source, cid)][-1]
    return (root / CHATS / f"{p['pages'][0]}.md").read_text(encoding="utf-8")


def chat(code, name="sess1", extra=()):
    return write(code / "D--Dev-proj" / f"{name}.jsonl", [
        u("How do heaps work?\nsecond line of my question"),
        a("ASSISTANT-ANSWER heaps are trees"),
        u("<command-name>/cortex</command-name>", isMeta=True),
        u("META-LINE", isMeta=True),
        u("SIDECHAIN-USER-TEXT", isSidechain=True),
        u("SUMMARY-OF-EARLIER-TALK", isCompactSummary=True),
        u(f"my key is {KEY}, keep it", ts="2026-11-10T09:05:00Z"),
        a("ASSISTANT-AGAIN"), *extra])


# ---- the pages ---------------------------------------------------------------------------------

def test_an_ordinary_chat_page_holds_my_turns_only_redacted(root_committed, dirs):
    code, exp = dirs
    chat(code)
    write(exp / "claude-ai" / "conv-1.jsonl", [
        {"type": "user", "message": {"content": "Explain tides"}, "timestamp": T0},
        {"type": "assistant", "message": {"content": "EXPORT-ANSWER"}, "timestamp": T0}])
    got = chat_import.run(Vault(root_committed))
    assert got == {"saved": 2}
    body = page(root_committed, "claude-code", "D--Dev-proj/sess1")
    assert body.count("**Me:**") == 2 and "How do heaps work?\nsecond line" in body
    assert "[redacted]" in body and KEY not in body
    for gone in ("ASSISTANT", "SIDECHAIN", "META-LINE", "SUMMARY-OF", "/cortex"):
        assert gone not in body, gone
    day = local_day(chat_import._when(T0))
    assert 'title: "How do heaps work?"' in body
    assert f"Source: Claude Code · Date: {day} · {chat_import.MINE}" in body
    text = body.split("---\n", 2)[2]
    assert harvest.user_turns(text) == ["How do heaps work?\nsecond line of my question",
                                        "my key is [redacted], keep it"]
    other = page(root_committed, "claude-chat", "claude-ai/conv-1")
    assert "Source: Claude · " in other and "**Me:** Explain tides" in other
    assert "EXPORT-ANSWER" not in other
    p = next(x for x in saved(root_committed) if x["source"] == "claude-code")
    assert p["auto"] is True and len(p["hash"]) == 16 and p["title"] == "How do heaps work?"
    assert "chat | 2 chats saved automatically" in (root_committed / "log.md").read_text("utf-8")


def test_a_long_chat_is_capped(root_committed, dirs):
    code, _ = dirs
    write(code / "p" / "long.jsonl", [u(f"turn {i} " + "x" * 3000) for i in range(20)])
    chat_import.run(Vault(root_committed))
    body = page(root_committed, "claude-code", "p/long")
    assert "The first" in body and "of my 20 turns." in body
    assert len(body) < chats.PART_CHARS + 2000 and "…" in body


def test_a_coding_session_is_a_capped_summary_without_code(root_committed, dirs):
    code, _ = dirs
    note(root_committed, "projects/proj.md", 'title: "Proj"\nstatus: active\n', "# Proj")
    tools = [("Write", {"file_path": f"{CWD}\\src\\f{i}.py", "content": "SECRET-CODE = 1"})
             for i in range(35)]
    tools += [("Edit", {"file_path": "C:\\Users\\me\\notes.txt", "old_string": "OLD-CODE",
                        "new_string": "NEW-CODE"}),
              ("Bash", {"command": 'git add -A && git commit -q -m "Add the parser"'}),
              ("Bash", {"command": "git commit -m \"$(cat <<'EOF'\nFix the heap\n\nBody "
                                   "line\nEOF\n)\""}),
              ("Bash", {"command": f"echo {KEY}"})]
    lines = [u(f"request {i}: do thing {i}\nDETAIL-LINE {i}", ts=f"2026-11-1{i % 2}T09:00:00Z")
             for i in range(15)]
    write(code / "D--Dev-proj" / "code.jsonl",
          [*lines, a("ASSISTANT-PLAN", tools=tools), result("TOOL-OUTPUT diff --git")])
    assert chat_import.run(Vault(root_committed)) == {"saved": 1}
    body = page(root_committed, "claude-code", "D--Dev-proj/code")
    for gone in ("SECRET-CODE", "OLD-CODE", "NEW-CODE", "TOOL-OUTPUT", "ASSISTANT", "DETAIL",
                 "notes.txt", "Users", KEY, "**Me:**", "Body line"):
        assert gone not in body, gone
    assert ("Coding session in [[proj|proj]] on 2026-11-10: 15 requests, 35 files changed, "
            "2 commits.") in body
    assert "- request 0: do thing 0" in body and "- …and 3 more" in body  # 12 of 15
    assert "- `src/f0.py`" in body and "- …and 5 more" in body  # 30 of 35
    assert "- Add the parser" in body and "- Fix the heap" in body
    assert 'related: ["[[proj]]"]' in body
    assert 'title: "proj: request 0: do thing 0"' in body


def test_commit_subjects_from_shell_commands():
    assert chat_import.commits("git commit -m 'one' && git -C x commit -am \"two\"") == [
        "one", "two"]
    assert chat_import.commits("git status; echo commit -m x") == []
    assert chat_import.commits("git commit --amend --no-edit") == []


# ---- re-runs and changes -------------------------------------------------------------------------

def test_a_rerun_is_idempotent_and_a_no_change_pass_only_stats(root_committed, dirs,
                                                               monkeypatch):
    code, _ = dirs
    chat(code)
    v = Vault(root_committed)
    chat_import.run(v)
    before = (events(root_committed), sorted((root_committed / CHATS).glob("*.md")))

    def no_read(*a):
        raise AssertionError("read a transcript on a no-change pass")
    with monkeypatch.context() as m:  # never monkeypatch.undo(): it drops the home isolation
        m.setattr(chat_import, "read", no_read)
        assert chat_import.run(v) == {} and chat_import.pending(v) == []
    for f in root_committed.glob(chat_import.STATE + "*"):  # a lost state: read, write nothing
        f.unlink()
    assert chat_import.run(Vault(root_committed)) == {"unchanged": 1}
    assert (events(root_committed),
            sorted((root_committed / CHATS).glob("*.md"))) == before
    for f in root_committed.glob(chat_import.STATE + "*"):
        f.unlink()
    (root_committed / chat_import.STATE).write_bytes(b"not a database " * 100)  # broken cache
    assert chat_import.run(Vault(root_committed)) == {"unchanged": 1}
    assert chat_import.run(Vault(root_committed)) == {}


def test_a_changed_transcript_updates_its_page_in_place(root_committed, dirs):
    code, _ = dirs
    p = chat(code)
    chat_import.run(Vault(root_committed))
    pid = saved(root_committed)[0]["pages"][0]
    md = root_committed / CHATS / f"{pid}.md"
    text = md.read_text(encoding="utf-8")  # topics.place files it under a hub
    md.write_text(text.replace(f"{chat_import.MINE}\n", f"{chat_import.MINE}\nTopics: [[x]]\n"),
                  encoding="utf-8")
    chat(code, extra=[u("A NEW QUESTION", ts="2026-11-10T10:00:00Z")])
    os.utime(p, (time.time() - 600, time.time() - 600))
    assert chat_import.run(Vault(root_committed)) == {"updated": 1}
    body = md.read_text(encoding="utf-8")
    assert "**Me:** A NEW QUESTION" in body and "Topics: [[x]]" in body
    assert [x["pages"] for x in saved(root_committed)] == [[pid], [pid]]
    assert len({x["hash"] for x in saved(root_committed)}) == 2


def _starts(monkeypatch):
    """The byte offset each read starts at."""
    seen, real = [], chat_import._lines

    def spy(f):
        seen.append(f.tell())
        return real(f)
    monkeypatch.setattr(chat_import, "_lines", spy)
    return seen


def _append(p, *lines, age=600):
    with p.open("a", encoding="utf-8") as f:
        f.writelines(json.dumps(x) + "\n" for x in lines)
    os.utime(p, (time.time() - age, time.time() - age))


def test_a_grown_transcript_is_read_from_its_offset_a_rewritten_one_from_the_start(
        root_committed, dirs, monkeypatch):
    code, _ = dirs
    p = chat(code)
    seen = _starts(monkeypatch)
    chat_import.run(Vault(root_committed))
    size = p.stat().st_size
    _append(p, u("THIRD QUESTION"))
    assert chat_import.run(Vault(root_committed)) == {"updated": 1}
    assert seen == [0, size]
    md = next((root_committed / CHATS).glob("chat-*.md"))
    body = md.read_text(encoding="utf-8")
    assert body.count("**Me:**") == 3 and "THIRD QUESTION" in body
    write(p, [u("A different first question"), u("THIRD QUESTION")], age=300)  # same size or not
    assert chat_import.run(Vault(root_committed)) == {"updated": 1}
    assert seen[-1] == 0
    body = md.read_text(encoding="utf-8")
    assert "How do heaps" not in body and body.count("**Me:**") == 2
    assert 'title: "A different first question"' in body


def test_a_huge_line_is_skipped_unparsed_and_a_half_written_one_waits(root_committed, dirs,
                                                                       monkeypatch):
    code, _ = dirs
    monkeypatch.setattr(chat_import, "MAX_LINE", 2000)
    p = code / "p" / "big.jsonl"
    p.parent.mkdir(parents=True)
    whole = json.dumps(u("BEFORE")) + "\n" + json.dumps(u("HUGE " + "y" * 9000)) + "\n" + \
        json.dumps(u("AFTER")) + "\n"
    half = json.dumps(u("LATE"))
    p.write_bytes((whole + half[:20]).encode())
    os.utime(p, (time.time() - 600, time.time() - 600))
    loads = []
    real = json.loads
    monkeypatch.setattr(json, "loads", lambda s, *a, **k: loads.append(len(s)) or real(s))
    chat_import.run(Vault(root_committed))
    assert max(loads) < 2000  # the huge line was never parsed
    body = page(root_committed, "claude-code", "p/big")
    assert "BEFORE" in body and "AFTER" in body and "HUGE" not in body
    seen = _starts(monkeypatch)
    p.write_bytes((whole + half + "\n").encode())
    os.utime(p, (time.time() - 300, time.time() - 300))
    assert chat_import.run(Vault(root_committed)) == {"updated": 1}
    assert seen == [len(whole.encode())]  # the half line is read once it is whole
    assert page(root_committed, "claude-code", "p/big").count("LATE") == 1


def test_newest_first_and_a_bounded_summary(root_committed, dirs):
    code, _ = dirs
    for i, age in enumerate((900, 300, 600)):
        write(code / "p" / f"c{i}.jsonl", [u(f"question {i}")], age=age)
    v = Vault(root_committed)
    assert [p.stem for _, p, _ in chat_import.pending(v)] == ["c1", "c2", "c0"]
    write(code / "p" / "long.jsonl", [u(f"turn {i}: " + "z" * 400) for i in range(3000)] + [
        a(tools=[("Edit", {"file_path": f"{CWD}\\f{i}.py"}) for i in range(1500)])])
    chat_import.run(v)
    st = chat_import.State(v)
    try:
        rec = st.get("claude-code:p/long")
    finally:
        st.close()
    assert len(json.dumps(rec)) < 150_000 and rec["chat"]["n"] == 3000
    assert len(rec["chat"]["files"]) == chat_import.PATHS
    assert len(page(root_committed, "claude-code", "p/long")) < 5000  # a coding summary


def test_a_cancel_stops_between_slices(root_committed, dirs, monkeypatch):
    code, _ = dirs
    for i in range(4):
        write(code / "p" / f"c{i}.jsonl", [u(f"question {i}")])
    monkeypatch.setattr(chat_import, "SLICE_S", 0)
    monkeypatch.setattr(chat_import, "REST_S", 0)
    calls = []
    got = chat_import.run(Vault(root_committed), cancelled=lambda: calls.append(1) or
                          len(calls) > 2)
    assert got["stopped"] == 1 and got.get("saved", 0) < 4
    assert chat_import.run(Vault(root_committed)) == {"saved": 4 - got.get("saved", 0)}


def test_an_owner_edit_or_deletion_is_left_alone(root_committed, dirs):
    code, _ = dirs
    p = chat(code)
    chat_import.run(Vault(root_committed))
    md = root_committed / CHATS / f"{saved(root_committed)[0]['pages'][0]}.md"
    md.write_text(md.read_text(encoding="utf-8") + "My own note.\n", encoding="utf-8")
    chat(code, extra=[u("MORE")])
    os.utime(p, (time.time() - 600, time.time() - 600))
    assert chat_import.run(Vault(root_committed)) == {"edited_by_you": 1}
    assert "MORE" not in md.read_text(encoding="utf-8")
    md.unlink()
    chat(code, extra=[u("EVEN MORE")])
    os.utime(p, (time.time() - 300, time.time() - 300))
    assert chat_import.run(Vault(root_committed)) == {"deleted_by_you": 1}
    assert not md.exists() and len(saved(root_committed)) == 1


# ---- what is skipped ---------------------------------------------------------------------------

def test_a_live_transcript_waits(root_committed, dirs):
    code, _ = dirs
    p = write(code / "p" / "live.jsonl", [u("still typing")], age=5)
    assert chat_import.run(Vault(root_committed)) == {}
    os.utime(p, (time.time() - chats.LIVE_SECONDS, time.time() - chats.LIVE_SECONDS))
    assert chat_import.run(Vault(root_committed)) == {"saved": 1}


def test_sidechains_empty_and_meta_only_transcripts_are_skipped(root_committed, dirs):
    code, _ = dirs
    write(code / "p" / "side.jsonl", [u("SIDE", isSidechain=True), a("x")])
    write(code / "p" / "meta.jsonl", [u("META", isMeta=True), {"type": "system", "content": "s"},
                                      result("only a tool result")])
    write(code / "p" / "empty.jsonl", [])
    write(code / "p" / "sess" / "subagents" / "agent-1.jsonl", [u("SUBAGENT")])
    assert chat_import.run(Vault(root_committed)) == {"empty": 3}
    assert saved(root_committed) == []


def test_a_skip_category_is_never_written_and_a_local_one_is_private(root_committed, dirs):
    code, _ = dirs
    write(code / "p" / "adult.jsonl", [u("where to find porn")])
    write(code / "p" / "health.jsonl", [u("what do these symptoms mean")])
    taint.write_config({"browse": {"skip_categories": ["adult", "money"]}})
    write(code / "p" / "money.jsonl", [u("my credit score dropped")])
    assert chat_import.run(Vault(root_committed)) == {"skipped_category": 2, "saved": 1}
    body = page(root_committed, "claude-code", "p/health")
    assert "private: true" in body
    assert [x["chat_id"] for x in saved(root_committed)] == ["p/health"]


def test_saved_by_hand_excluded_folders_and_earlier_imports_are_left_alone(root_committed,
                                                                         dirs, tmp_path):
    from cortex.events import EventLog
    code, exp = dirs
    chat(code, "hand")
    EventLog(Vault(root_committed)).append(
        session="cli", kind="log", source="external", fields={"payload": {
            "kind": "chat_saved", "source": "claude-code", "chat_id": "D--Dev-proj/hand",
            "title": "t", "pages": []}})
    secret = tmp_path / "work"
    write(code / "w" / "x.jsonl", [{**u("work stuff"), "cwd": str(secret)}])
    taint.write_config({"exclude": [str(secret)]})
    day = local_day(chat_import._when(T0))
    note(root_committed, "resources/gemini-tides.md", 'title: "Tides"\n',
         f"# Tides\n\nTimeline: · Source: Gemini · Date: {day}\n\n**Me:** How do tides work?"
         "\n\n**Gemini:** answer\n")
    write(exp / "gemini" / "2026-11-10-0900.jsonl", [
        {"type": "user", "message": {"content": "How do tides work?"}, "timestamp": T0}])
    assert chat_import.run(Vault(root_committed)) == {
        "saved_by_hand": 1, "excluded_folder": 1, "earlier_import": 1}
    assert [x for x in saved(root_committed) if x.get("auto")] == []  # no event per adoption
    st = chat_import.State(Vault(root_committed))
    try:
        assert st.get("gemini:gemini/2026-11-10-0900") == {"adopted": "gemini-tides"}
    finally:
        st.close()
    assert len(list((root_committed / CHATS).glob("chat-*.md"))) == 0
    p = exp / "gemini" / "2026-11-10-0900.jsonl"
    _append(p, {"type": "user", "message": {"content": "and the moon?"}, "timestamp": T0})
    assert chat_import.run(Vault(root_committed)) == {"earlier_import": 1}  # not read again


# ---- the app, the setting and the CLI ------------------------------------------------------------

def _wait(app):
    end = time.monotonic() + 30
    # the notifier runs just after the job stops running: wait for it too
    while app.jobs.running() or getattr(app, "notified", app.jobs.cur) is not app.jobs.cur:
        assert time.monotonic() < end, "the job did not finish"
        time.sleep(0.02)
    return app.jobs.cur


def _app(root):
    app = server.App(root)
    app.notified = None  # the last job the notifier saw
    sent = []
    notify = desktop.Notifier(types.SimpleNamespace(), sent.append).job_ended

    def on_end(job):
        notify(job)
        app.notified = job

    app.jobs.on_end = on_end
    return app, sent


def test_the_app_job_shows_progress_and_notifies_once(vault, dirs):  # noqa: F811
    code, _ = dirs
    for i in range(8):
        write(code / "p" / f"c{i}.jsonl", [u(f"question number {i}")])
    app, sent = _app(vault)
    assert app.chat_import_auto() is not None
    job = _wait(app)
    assert job.result["counts"] == {"saved": 8} and job.result["quiet"] is False
    assert "8 of 8 chats (100%), about 0 s left" in job.lines and job.lines[-1] == \
        "8 chat pages written"
    assert sent == ["Chat import finished"]
    assert "cortex: app import chats" in git(vault, "log", "-1", "--format=%s")
    assert app.chat_import_auto() is None  # nothing new: no job at all
    p = code / "p" / "c0.jsonl"
    os.utime(p, (time.time() - 200, time.time() - 200))  # touched, same text: a quiet job
    assert app.chat_import_auto() is not None
    assert _wait(app).result["quiet"] is True and sent == ["Chat import finished"]


def test_a_failed_import_notifies_failed_and_the_next_pass_runs(vault, dirs, monkeypatch):  # noqa: F811
    code, _ = dirs
    write(code / "p" / "c.jsonl", [u("a question")])
    app, sent = _app(vault)
    real = chat_import.run
    monkeypatch.setattr(chat_import, "run", lambda *a, **k: 1 / 0)
    app.chat_import_auto()
    assert _wait(app).error == "ZeroDivisionError" and sent == ["Chat import failed"]
    monkeypatch.setattr(chat_import, "run", real)
    app.chat_import_auto()
    assert _wait(app).result["counts"] == {"saved": 1}
    assert sent == ["Chat import failed", "Chat import finished"]


def test_the_server_runs_it_a_minute_after_start_then_every_5_minutes(vault, dirs,  # noqa: F811
                                                                      monkeypatch):
    assert (chat_import.FIRST_S, chat_import.EVERY_S) == (60, 300)
    code, _ = dirs
    write(code / "p" / "c.jsonl", [u("a question")])
    monkeypatch.setattr(chat_import, "FIRST_S", 0.05)
    monkeypatch.setattr(chat_import, "EVERY_S", 0.05)
    srv, app, _ = server.start(vault, idle=False, echo=lambda s: None)
    try:
        end = time.monotonic() + 30
        while not saved(vault):
            assert time.monotonic() < end, "no pass ran"
            time.sleep(0.05)
    finally:
        app.stop.set()
        server.finish(srv, app)


def test_the_setting_turns_it_off(app, dirs):
    code, _ = dirs
    write(code / "p" / "c.jsonl", [u("a question")])
    assert call(app, "GET", "/api/status")[1]["chat_import"] is True
    assert call(app, "POST", "/api/app/chat-import", {"enabled": "no"})[0] == 400
    assert call(app, "POST", "/api/app/chat-import", {"enabled": False})[1] == {"enabled": False}
    assert taint.config()["app"]["chat_import"] is False
    assert call(app, "GET", "/api/status")[1]["chat_import"] is False
    assert app.chat_import_auto() is None and saved(app.root) == []
    call(app, "POST", "/api/app/chat-import", {"enabled": True})
    assert app.chat_import_auto() is not None


def test_the_cli_runs_one_pass(root_committed, dirs):
    from cortex.cli import app
    code, _ = dirs
    chat(code)
    out = CliRunner().invoke(app, ["import-chats", "--auto", "--vault", str(root_committed)])
    assert out.exit_code == 0, out.output
    assert "1 chat page written (1 saved)." in out.output
    assert git(root_committed, "log", "-1", "--format=%s").strip() == "cortex: cli import chats"
    out = CliRunner().invoke(app, ["import-chats", "--auto", "--vault", str(root_committed)])
    assert "0 chat pages written; no new or changed chats." in out.output
    assert CliRunner().invoke(app, ["import-chats"]).exit_code == 1


def test_threads_never_share_a_pass(root_committed, dirs):
    """LOCK: a second pass while one runs is refused as busy."""
    from cortex.vault import VaultError
    v = Vault(root_committed)
    with v.job_lock(chat_import.LOCK, 0, chat_import.BUSY):
        box = []
        t = threading.Thread(target=lambda: box.append(_try(Vault(root_committed))))
        t.start()
        t.join(10)
    assert isinstance(box[0], VaultError) and box[0].code == "busy"


def _try(v):
    try:
        return chat_import.run(v)
    except Exception as e:  # noqa: BLE001
        return e


# ---- a live-logged chat and its claude.ai export: one page -------------------------------------

LIVE_ID = "01JCZ8Q0000000000000000000"
OPEN = "How do B-trees   split when a node is full?"  # spaced unlike the live copy
LIVE_KEY = (chats.LIVE, f"{chats.LIVE}/{LIVE_ID}")


def _line(role, ts, text):
    return {"type": role, "message": {"content": text}, "timestamp": ts}


def _live(exp, extra=()):
    """A Chat-tab log as cortex_prime and chat_turn write it, stamped when the model called
    (seconds after each message); the model forgot the chat_turn for "and deletion?"."""
    return write(exp / chats.LIVE / f"{LIVE_ID}.jsonl", [
        _line("user", "2026-11-10T10:00:04Z", " ".join(OPEN.split())),
        _line("assistant", "2026-11-10T10:10:03Z", "gist of answer"),
        _line("user", "2026-11-10T10:10:03Z", "thanks, that helps"), *extra])


def _export(tmp_path, uid="aaaa-bbbb-1", hour=10):
    """A claude.ai conversations.json, imported with chats.import_export."""
    msgs = [("human", "00:00", OPEN), ("assistant", "00:30", "EXPORT-ANSWER-1"),
            ("human", "05:00", "and deletion?"), ("assistant", "05:30", "EXPORT-ANSWER-2"),
            ("human", "10:00", "thanks, that helps")]
    day = f"2026-11-10T{hour:02}:"
    conv = {"uuid": uid, "name": "t", "updated_at": f"{day}11:00Z", "chat_messages": [
        {"sender": s, "created_at": f"{day}{t}Z", "content": [{"type": "text", "text": x}]}
        for s, t, x in msgs]}
    f = tmp_path / f"{uid}.json"
    f.write_text(json.dumps([conv]), "utf-8")
    assert chats.import_export(f) == ("claude", 1)


def _pages(root):
    return sorted(p.stem for p in (root / CHATS).glob("chat-*.md"))


def _keys(root):
    return {(x["source"], x["chat_id"]): x["pages"] for x in saved(root)}


def _turns(root, pid):
    body = (root / CHATS / f"{pid}.md").read_text("utf-8")
    assert "EXPORT-ANSWER" not in body and "gist" not in body
    return harvest.user_turns(body.split("---\n", 2)[2])


def test_a_live_chat_then_its_export_is_one_page_with_the_full_transcript(root_committed, dirs,
                                                                         tmp_path):
    _, exp = dirs
    _live(exp)
    assert chat_import.run(Vault(root_committed)) == {"saved": 1}
    [pid] = _pages(root_committed)
    _export(tmp_path)
    assert chat_import.run(Vault(root_committed)) == {"updated": 1}
    assert _pages(root_committed) == [pid]  # the same page, now from the export
    assert _turns(root_committed, pid) == [OPEN, "and deletion?", "thanks, that helps"]
    assert _keys(root_committed) == {LIVE_KEY: [pid], ("claude-chat", "claude-ai/aaaa-bbbb-1"):
                                     [pid]}
    n = len(saved(root_committed))
    assert chat_import.run(Vault(root_committed)) == {}
    st = root_committed / chat_import.STATE  # with the state forgotten, still nothing new
    for f in st.parent.glob(st.name + "*"):
        f.unlink()
    assert chat_import.run(Vault(root_committed)) == {"unchanged": 2}
    _export(tmp_path)  # the same export imported again
    assert chat_import.run(Vault(root_committed)) in ({}, {"unchanged": 1})
    assert len(saved(root_committed)) == n and _pages(root_committed) == [pid]


def test_live_turns_after_the_export_are_kept_once(root_committed, dirs, tmp_path):
    _, exp = dirs
    p = _live(exp)
    _export(tmp_path)
    assert chat_import.run(Vault(root_committed)) == {"saved": 1, "unchanged": 1}
    [pid] = _pages(root_committed)
    _append(p, _line("assistant", "2026-11-10T11:00:02Z", "gist"),
            _line("user", "2026-11-10T11:00:02Z", "one more: B+ trees?"),
            _line("assistant", "2026-11-10T11:02:00Z", "gist"),
            _line("user", "2026-11-10T11:02:00Z", "thanks, that helps"))  # said again, later
    assert chat_import.run(Vault(root_committed)) == {"updated": 1}
    assert _pages(root_committed) == [pid]
    assert _turns(root_committed, pid) == [
        OPEN, "and deletion?", "thanks, that helps", "one more: B+ trees?", "thanks, that helps"]


def test_an_export_then_its_live_log_is_one_page(root_committed, dirs, tmp_path):
    _, exp = dirs
    _export(tmp_path)
    assert chat_import.run(Vault(root_committed)) == {"saved": 1}
    [pid] = _pages(root_committed)
    before = (root_committed / CHATS / f"{pid}.md").read_text("utf-8")
    _live(exp)
    assert chat_import.run(Vault(root_committed)) == {"unchanged": 1}
    assert _pages(root_committed) == [pid]
    assert (root_committed / CHATS / f"{pid}.md").read_text("utf-8") == before
    assert _keys(root_committed) == {LIVE_KEY: [pid], ("claude-chat", "claude-ai/aaaa-bbbb-1"):
                                     [pid]}


def test_an_ambiguous_match_keeps_every_page(root_committed, dirs, tmp_path):
    _, exp = dirs
    _live(exp)
    _export(tmp_path, "aaaa-bbbb-1")
    _export(tmp_path, "aaaa-bbbb-2")  # the same opening at the same time: which one?
    assert chat_import.run(Vault(root_committed)) == {"saved": 3}
    assert len(_pages(root_committed)) == 3


def test_a_start_time_too_far_apart_keeps_both_pages(root_committed, dirs, tmp_path):
    _, exp = dirs
    _live(exp)
    _export(tmp_path, hour=11)  # an hour later: another chat that opened the same way
    assert chat_import.run(Vault(root_committed)) == {"saved": 2}
    assert len(_pages(root_committed)) == 2


def test_the_app_lists_chats_made_private_and_why_newest_first_paged(root_committed, dirs,
                                                                    app):
    """G2: Browsing › Chats kept private: title, app, date and category, 20 a page, newest
    first; a shared chat, one the owner made shared again and private browsing never show,
    nor any chat text past the title; a page written before the category was recorded is
    judged again from its body; no MCP tool reads it."""
    import inspect

    from cortex import mcp_server
    from cortex.events import EventLog
    code, _ = dirs
    for d in range(1, 26):
        write(code / "p" / f"h{d}.jsonl", [
            u(f"what do these symptoms mean {d}", ts=f"2026-10-{d:02d}T09:00:00Z"),
            u(f"SECOND-TURN-{d}", ts=f"2026-10-{d:02d}T09:01:00Z")])
    write(code / "p" / "plain.jsonl", [u("How do heaps work?", ts="2026-10-28T09:00:00Z")])
    canary = root_committed / "browse" / "private" / "browse-private-2026-10-02.md"
    canary.parent.mkdir(parents=True, exist_ok=True)
    canary.write_text("---\nprivate: true\n---\nBROWSE-CANARY health\n", encoding="utf-8")
    assert chat_import.run(Vault(root_committed)) == {"saved": 26}
    mine = {x["chat_id"]: x for x in saved(root_committed)}
    assert all(mine[f"p/h{d}"].get("category") == "health" for d in range(1, 26))
    assert "category" not in mine["p/plain"]
    # the owner made one shared again: it leaves the list
    p = root_committed / CHATS / f"{mine['p/h25']['pages'][0]}.md"
    p.write_text(p.read_text(encoding="utf-8").replace("private: true\n", ""), encoding="utf-8")
    status, js, _, _ = call(app, "GET", "/api/browse/private-chats")
    assert status == 200 and js["total"] == 24 and len(js["rows"]) == 20
    dates = [r["date"] for r in js["rows"]]
    assert dates == sorted(dates, reverse=True)
    top = js["rows"][0]
    assert top["app"] == "Claude Code" and top["category"] == "health"
    assert top["date"] == local_day(chat_import._when("2026-10-24T09:00:00Z"))
    assert top["title"].startswith("what do these symptoms mean")
    rest = call(app, "GET", "/api/browse/private-chats?offset=20")[1]
    assert len(rest["rows"]) == 4 and rest["offset"] == 20
    text = json.dumps([js, rest])
    for gone in ("SECOND-TURN", "heaps", "BROWSE-CANARY"):
        assert gone not in text, gone
    assert call(app, "GET", "/api/browse/private-chats?offset=x")[1]["offset"] == 0
    assert call(app, "GET", "/api/browse/private-chats", token=False)[0] == 401
    # a page saved before the category was recorded: judged again from its body
    evs = [dict(e, payload={k: v for k, v in e["payload"].items() if k != "category"})
           if isinstance(e.get("payload"), dict) else e
           for e in EventLog(Vault(root_committed)).iter_events()]
    assert [r["category"] for r in chat_import.made_private(Vault(root_committed), evs)] \
        == ["health"] * 24
    src = inspect.getsource(mcp_server)
    assert "made_private" not in src and "private-chats" not in src
