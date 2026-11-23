"""R7: Codex CLI and Gemini CLI get Cortex's prime on each prompt from their own hooks
(`cortex prime-hook --client codex|gemini`), and `cortex taint` after another server's tool.
The stdin samples follow each app's documented hook input (made-up content):
Codex: learn.chatgpt.com/docs/hooks and codex-rs/hooks/schema/generated/*.schema.json;
Gemini: github.com/google-gemini/gemini-cli docs/hooks/reference.md."""

import json
import re
import sys

import pytest

from cortex import delta, prime_hook, taint, xray
from cortex.app import targets
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

ENTRY = {"command": "C:\\tools\\cortex.exe", "args": ["mcp", "--vault", "C:\\notes"]}
DBMS = "".join(f"- DBMS rule {i}: tables first, then the SQL query, one worked example.\n"
               for i in range(14))  # past the 800-char preferences cap: Email waits
CODEX_SID = "019a7c3e-55b1-7d42-9e0a-2f6c1b8d4e21"
GEMINI_SID = "5f0c2a9e-3b7d-4c1e-8a6f-0d2e4b9c7a13"


@pytest.fixture
def vault(root, monkeypatch):
    (root / "me/preferences.md").write_text(
        "# Preferences\n\n## Learning\n" + DBMS
        + "\n## Communication\n- Email: formal greeting, at most five sentences.\n",
        encoding="utf-8")
    (root / ".cortex").mkdir(exist_ok=True)
    (root / ".cortex/profiles.json").write_text(json.dumps({"profiles": [
        {"match": "codex", "scopes": ["all"]}, {"match": "gemini", "scopes": ["all"]}],
        "unknown": ["core"]}), encoding="utf-8")
    monkeypatch.setenv("CORTEX_VAULT", str(root))
    return root


def codex(prompt, cwd, event="UserPromptSubmit", **kw):
    return json.dumps({"session_id": CODEX_SID, "transcript_path": None, "cwd": str(cwd),
                       "hook_event_name": event, "model": "gpt-5-codex",
                       "permission_mode": "default", "turn_id": "turn-1", "prompt": prompt,
                       **kw})


def gemini(prompt, cwd, event="BeforeAgent", **kw):
    return json.dumps({"session_id": GEMINI_SID, "transcript_path": str(cwd / "t.json"),
                       "cwd": str(cwd), "hook_event_name": event,
                       "timestamp": "2026-11-23T10:00:00.000Z", "prompt": prompt, **kw})


def context(out, event):
    got = json.loads(out)["hookSpecificOutput"]
    assert got["hookEventName"] == event
    return got["additionalContext"]


@pytest.mark.parametrize("client,make,event,mcp_name", [
    ("codex", codex, "UserPromptSubmit", "codex-mcp-client"),
    ("gemini", gemini, "BeforeAgent", "gemini-cli-mcp-client")])
def test_first_prompt_primes_then_only_deltas(vault, tmp_path, client, make, event, mcp_name):
    first = context(prime_hook.run(make("Revise DBMS normalisation", tmp_path), client), event)
    assert first.startswith(prime_hook.HEAD) and first.endswith(prime_hook.NEXT)
    assert "DBMS rule 13" in first and "Email" not in first
    second = context(prime_hook.run(make("Write an email to my professor", tmp_path), client),
                     event)
    assert second.startswith(delta.HEAD) and "Email: formal greeting" in second
    assert "DBMS" not in second and prime_hook.HEAD not in second
    assert prime_hook.run(make("Another email for the dean", tmp_path), client) == ""
    sid = CODEX_SID if client == "codex" else GEMINI_SID
    assert json.loads((vault / prime_hook.SERVED).read_text("utf-8"))[sid]["n"] == 3
    rows = [r for r in xray.rows(Vault(vault)) if r["tool"] == "prime-hook"]
    assert [r["client"] for r in rows] == [mcp_name, mcp_name]


def test_tags_and_session_start(vault, tmp_path):
    out = context(prime_hook.run(gemini("hi //pref: answers in tables", tmp_path), "gemini"),
                  "BeforeAgent")
    assert "Cortex saved the user's //pref" in out and "answers in tables" in out
    assert "answers in tables" in (vault / "me/preferences.md").read_text("utf-8")
    assert prime_hook.run(gemini("", tmp_path, "SessionStart", source="startup"), "gemini") == ""
    again = context(prime_hook.run(codex("", tmp_path, "SessionStart", source="compact"),
                                   "codex"), "SessionStart")
    assert again.startswith(prime_hook.AGAIN.format(source="compacted"))
    assert prime_hook.run(codex("x", tmp_path), "cursor") == ""  # no such client: nothing


def test_console_entry_takes_the_client(vault, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cortex", "prime-hook", "--client", "gemini"])
    monkeypatch.setattr(taint, "read_stdin", lambda: gemini("Revise DBMS please", tmp_path))
    taint.main()
    assert context(capsys.readouterr().out, "BeforeAgent").startswith(prime_hook.HEAD)


def test_taint_maps_each_apps_tool_names(vault, tmp_path):
    marker = vault / taint.MARKER
    for app, tool in (("gemini", "web_fetch"), ("gemini", "google_web_search"),
                      ("gemini", "mcp_fetch_get"), ("codex", "mcp__fetch__get")):
        marker.unlink(missing_ok=True)
        assert taint.run(json.dumps({"session_id": "s", "hook_event_name": "AfterTool",
                                     "tool_name": tool})), (app, tool)
        assert marker.is_file()
    marker.unlink()
    for tool in ("mcp_cortex_cortex_prime", "mcp__cortex__cortex_patch"):
        assert not taint.run(json.dumps({"session_id": "s", "tool_name": tool}))
    assert not marker.exists()
    m = re.compile(targets.Gemini.TAINT)  # Gemini tests it as a JS RegExp: same syntax here
    assert all(m.search(t) for t in ("web_fetch", "google_web_search", "mcp_fetch_get"))
    assert not any(m.search(t) for t in ("mcp_cortex_cortex_prime", "read_file",
                                         "run_shell_command"))


def test_hook_command_quotes_for_each_shell(monkeypatch):
    monkeypatch.setattr(targets.sys, "platform", "win32")
    exe = {"command": "C:\\Program Files\\O'Neil\\cortex.exe"}
    assert targets.hook_command(exe, "taint --client gemini", powershell=True) == \
        "& 'C:\\Program Files\\O''Neil\\cortex.exe' taint --client gemini"
    assert targets.hook_command(exe, "prime-hook --client codex") == \
        '"C:\\Program Files\\O\'Neil\\cortex.exe" prime-hook --client codex'
    monkeypatch.setattr(targets.sys, "platform", "linux")
    assert targets.hook_command({"command": "/opt/my tools/cortex"}, "taint --client codex") \
        == "'/opt/my tools/cortex' taint --client codex"


def _baks(p):
    return sorted(p.parent.glob(p.name + ".cortex-*.bak"))


def test_gemini_add_then_remove_keeps_the_owners_hooks(home):
    p = home / ".gemini" / "settings.json"
    p.parent.mkdir()
    mine = {"type": "command", "command": "node lint.js", "name": "lint"}
    old = {"theme": "dark", "hooks": {"BeforeAgent": [{"hooks": [mine]}]}}
    p.write_text(json.dumps(old, indent=4), encoding="utf-8")
    t = targets.by_id()["gemini-cli"]
    assert {"file": str(p), "change": "add", "detail": "the Cortex hooks"} in t.plan(True, ENTRY)
    t.apply(ENTRY)
    hooks = json.loads(p.read_text("utf-8"))["hooks"]
    assert hooks["BeforeAgent"][0] == {"hooks": [mine]} and len(hooks["BeforeAgent"]) == 2
    ours = hooks["BeforeAgent"][1]["hooks"][0]
    assert ours["command"].endswith(" prime-hook --client gemini") and ours["timeout"] == 30000
    assert hooks["AfterTool"][0]["matcher"] == targets.Gemini.TAINT
    assert hooks["SessionStart"][0]["hooks"][0]["command"] == ours["command"]
    assert t.plan(True, ENTRY) == []  # nothing left to do
    moved = {**ENTRY, "command": "D:\\new\\cortex.exe"}
    assert {"file": str(p), "change": "update", "detail": "the Cortex hooks"} in t.plan(True,
                                                                                         moved)
    t.apply(moved)
    hooks = json.loads(p.read_text("utf-8"))["hooks"]
    assert len(hooks["BeforeAgent"]) == 2 and "new" in hooks["BeforeAgent"][1]["hooks"][0][
        "command"]
    assert {"file": str(p), "change": "remove", "detail": "the Cortex hooks"} in t.plan(False)
    t.remove()
    data = json.loads(p.read_text("utf-8"))
    assert data == {**old, "mcpServers": {}} and _baks(p)


def test_codex_add_then_remove_leaves_hooks_json_as_it_was(home):
    d = home / ".codex"
    d.mkdir()
    p = d / "hooks.json"
    t = targets.by_id()["codex"]
    rows = t.plan(True, ENTRY)
    assert {"file": str(p), "change": "add", "detail": "the Cortex hooks"} in rows
    t.apply(ENTRY)
    hooks = json.loads(p.read_text("utf-8"))["hooks"]
    assert set(hooks) == {"UserPromptSubmit", "SessionStart", "PostToolUse"}
    assert hooks["SessionStart"][0]["matcher"] == "compact|resume|clear"
    assert hooks["PostToolUse"][0]["matcher"] == "^mcp__"
    assert hooks["UserPromptSubmit"][0]["hooks"][0]["timeout"] == 30
    t.remove()
    assert not p.exists() and _baks(p)  # only Cortex's hooks were in it: gone, backed up

    old = {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "python3 log.py", "timeout": 5}]}]}}
    p.write_text(json.dumps(old), encoding="utf-8")
    t.apply(ENTRY)
    assert len(json.loads(p.read_text("utf-8"))["hooks"]["PostToolUse"]) == 2
    t.remove()
    assert json.loads(p.read_text("utf-8")) == old


@pytest.mark.parametrize("bad", ["{not json", "[1]", '{"hooks": []}',
                                 '{"hooks": {"UserPromptSubmit": {}}}'])
def test_an_unparseable_hooks_file_is_left_alone(home, bad):
    d = home / ".codex"
    d.mkdir()
    p, toml = d / "hooks.json", d / "config.toml"
    p.write_text(bad, encoding="utf-8")
    toml.write_text('model = "o3"\n', encoding="utf-8")
    t = targets.by_id()["codex"]
    rows = [r for r in t.plan(True, ENTRY) if r["file"] == str(p)]
    assert rows[0]["change"] == "error" and "untouched" in rows[0]["detail"]
    with pytest.raises(ValueError, match="untouched"):
        t.apply(ENTRY)
    assert p.read_text("utf-8") == bad and toml.read_text("utf-8") == 'model = "o3"\n'
    assert not _baks(p) and not _baks(toml)
