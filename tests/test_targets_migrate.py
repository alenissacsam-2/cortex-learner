"""R4: migrate_old_entries points what Keepfold wrote into other apps under its old name Cortex
(0.6.1 and older) at the new names. Temp home only, under a folder whose name has a space and
a non-ASCII letter: HOME, USERPROFILE, APPDATA and LOCALAPPDATA all point there, and `claude`,
the registry and the installed Keepfold are fakes. Nothing reads the real configs."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest

from keepfold import clients, doctor, taint
from keepfold.app import targets

OLD_EXE = "C:\\Users\\José Smith\\AppData\\Local\\Programs\\Cortex\\bin\\cortex.exe"
NEW_EXE = "C:\\Users\\José Smith\\AppData\\Local\\Programs\\Keepfold\\bin\\keepfold.exe"
VAULT = "D:\\Notes\\Mi Cortex vault é"
OLD = {"command": OLD_EXE, "args": ["mcp", "--vault", VAULT]}
NEW_BASE = {"command": NEW_EXE, "args": ["mcp", "--vault", VAULT]}
OLD_BLOCK = ("<!-- cortex:start (added by the Cortex app; it removes this block) -->\n"
             "## Cortex\nCall `cortex_prime` first.\n<!-- cortex:end -->\n")
JSON_IDS = ["claude-desktop", "claude-code", "gemini-cli", "antigravity", "cursor", "vscode",
            "windsurf", "lmstudio", "zed", "cline", "copilot-cli", "junie"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "José Smith"
    h.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    for var in ("LOCALAPPDATA", "APPDATA"):
        monkeypatch.setenv(var, str(h / var))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(clients, "mcp_command", lambda: NEW_EXE)
    monkeypatch.setattr(taint, "_vault_root", lambda: None)
    monkeypatch.setattr(targets, "_which", lambda c: None)  # never the real `claude`
    monkeypatch.setattr(targets, "_run", lambda *a, **k: pytest.fail("ran a command"))
    return h


def _t(tid):
    t = targets.by_id()[tid]
    return t.json if tid == "claude-code" else t


def _path(tid) -> Path:
    if tid == "claude-desktop":
        d = (Path.home() / "Library" / "Application Support" / "Claude" if sys.platform == "darwin"
             else Path.home() / "APPDATA" / "Claude" if sys.platform == "win32"
             else Path.home() / ".config" / "Claude")
        d.mkdir(parents=True)
    return _t(tid).paths()[0]


def _put(p: Path, data) -> bytes:
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(data, indent=2) if not isinstance(data, str) else data).encode()
    p.write_bytes(raw)
    return raw


def _rows(app=None, **kw):
    rows = targets.migrate_old_entries(**kw)
    return [r for r in rows if app is None or r["app"] == app]


def _baks(p: Path) -> list[Path]:
    return sorted(p.parent.glob(p.name + ".keepfold-*.bak"))


def _all_nothing():
    rows = targets.migrate_old_entries()
    assert {r["action"] for r in rows} == {"nothing"}, [r for r in rows if r["action"] != "nothing"]
    assert len(rows) == len(targets.targets())


# ---- MCP entries in JSON configs ------------------------------------------------------------


@pytest.mark.parametrize("tid", JSON_IDS)
def test_old_json_entry_moves_with_a_backup_and_once(home, tid):
    t, p = _t(tid), _path(tid)
    old = {**OLD, "args": [*OLD["args"], "--private"]} if tid == "lmstudio" else dict(OLD)
    if tid == "cline":
        old["disabled"] = False  # a key the app adds itself stays
    _put(p, {"theme": "dark", t.key: {"other": {"command": "x"}, "cortex": old, "last": {}}})
    label = targets.by_id()[tid].label
    (row,) = _rows(label)
    assert row["action"] == "moved" and row["file"] == str(p)
    assert Path(row["backup"]) in _baks(p) and json.loads(Path(row["backup"]).read_text(
        encoding="utf-8"))[t.key]["cortex"] == old
    data = json.loads(p.read_text(encoding="utf-8"))
    want = t.entry({"command": NEW_EXE, "args": old["args"]})
    if tid == "cline":
        want = {"disabled": False, **want}
    assert data["theme"] == "dark" and data[t.key]["keepfold"] == want
    assert list(data[t.key]) == ["other", "keepfold", "last"]  # same place, other keys kept
    assert "cortex" not in json.dumps(data).lower().replace("mi cortex vault", "")
    _all_nothing()  # a second run does nothing
    assert len(_baks(p)) == 1


def test_antigravity_old_folder_too(home):
    t = targets.by_id()["antigravity"]
    for p in t.all_paths():
        _put(p, {"mcpServers": {"cortex": OLD}})
    rows = _rows("Antigravity")
    assert [r["action"] for r in rows] == ["moved", "moved"]
    for p in t.all_paths():
        assert json.loads(p.read_text(encoding="utf-8"))["mcpServers"] == {
            "keepfold": NEW_BASE}


@pytest.mark.parametrize("tid", ["claude-desktop", "cursor", "vscode", "copilot-cli"])
def test_old_and_new_json_entries_become_one(home, tid):
    t, p = _t(tid), _path(tid)
    mine = t.entry({"command": "D:\\elsewhere\\keepfold.exe", "args": ["mcp", "--vault", "X"]})
    _put(p, {t.key: {"keepfold": mine, "cortex": OLD}})
    (row,) = [r for r in _rows() if r["file"] == str(p)]
    assert row["action"] == "moved" and "there already" in row["note"]
    assert json.loads(p.read_text(encoding="utf-8"))[t.key] == {"keepfold": mine}


@pytest.mark.parametrize("entry", [
    {"command": "uv", "args": ["run", "cortex", "mcp"]},
    {"command": "C:\\tools\\other-server.exe", "args": ["mcp"]},
    {**OLD, "env": {"CORTEX_VAULT": VAULT}},
    {"command": OLD_EXE, "args": ["mcp", "--vault", VAULT, "--debug"]},
    "cortex",
])
@pytest.mark.parametrize("tid", ["claude-desktop", "windsurf", "vscode"])
def test_hand_written_json_entry_is_left(home, tid, entry):
    t, p = _t(tid), _path(tid)
    raw = _put(p, {t.key: {"cortex": entry}})
    (row,) = [r for r in _rows() if r["file"] == str(p)]
    assert row["action"] == "left" and "by hand" in row["note"] and not row["backup"]
    assert p.read_bytes() == raw and not _baks(p)
    assert doctor.check_old_connections()[0] == "WARN"


@pytest.mark.parametrize("tid", ["claude-desktop", "zed", "junie"])
def test_malformed_json_is_never_rewritten(home, tid):
    p = _path(tid)
    raw = _put(p, '{\n  // a comment\n  "mcpServers": {"cortex": {"command": "cortex"}},\n}')
    (row,) = [r for r in _rows() if r["file"] == str(p)]
    assert row["action"] == "error" and "untouched" in row["note"]
    assert p.read_bytes() == raw and not _baks(p)


def test_malformed_json_without_cortex_is_none_of_ours(home):
    p = _path("zed")
    raw = _put(p, '{\n  // Zed settings have comments\n  "theme": "One Dark",\n}')
    assert {r["action"] for r in _rows("Zed")} == {"nothing"}
    assert p.read_bytes() == raw


def test_dry_run_writes_nothing(home):
    p = _path("windsurf")
    raw = _put(p, {"mcpServers": {"cortex": OLD}})
    (row,) = _rows("Windsurf", dry_run=True)
    assert row["action"] == "moved" and not row["backup"]
    assert p.read_bytes() == raw and not _baks(p)


# ---- Codex: TOML, hooks.json, AGENTS.md -----------------------------------------------------

CODEX_OLD = ('# my codex config\nmodel = "o4"\n\n[mcp_servers.cortex]\n'
             f'command = {json.dumps(OLD_EXE, ensure_ascii=False)}\n'
             f'args = {json.dumps(OLD["args"], ensure_ascii=False)}\n\n'
             '[mcp_servers.other]\ncommand = "x"\n')


def test_codex_toml_moves(home):
    t = targets.by_id()["codex"]
    _put(t.path(), CODEX_OLD)
    rows = _rows("Codex CLI")
    assert [(r["action"], r["file"]) for r in rows] == [("moved", str(t.path()))]
    assert _baks(t.path()) and Path(rows[0]["backup"]).read_text(encoding="utf-8") == CODEX_OLD
    text = t.path().read_text(encoding="utf-8")
    assert text.startswith('# my codex config\nmodel = "o4"\n')
    data = tomllib.loads(text)
    assert data["model"] == "o4" and data["mcp_servers"] == {"other": {"command": "x"},
                                                            "keepfold": NEW_BASE}
    _all_nothing()


def test_codex_toml_old_and_new_become_one(home):
    t = targets.by_id()["codex"]
    mine = {"command": "D:\\k\\keepfold.exe", "args": ["mcp"]}
    _put(t.path(), targets.codex_text(CODEX_OLD, mine))
    (row,) = _rows("Codex CLI")
    assert row["action"] == "moved"
    assert tomllib.loads(t.path().read_text(encoding="utf-8"))["mcp_servers"] == {
        "other": {"command": "x"}, "keepfold": mine}


@pytest.mark.parametrize("text", [
    '[mcp_servers.cortex]\ncommand = "uvx"\nargs = ["cortex", "mcp"]\n',  # hand-written: left
    '[mcp_servers.cortex\ncommand = "cortex"\n',  # malformed: error
])
def test_codex_toml_hand_written_or_malformed_is_untouched(home, text):
    t = targets.by_id()["codex"]
    raw = _put(t.path(), text)
    (row,) = _rows("Codex CLI")
    assert row["action"] == ("left" if "uvx" in text else "error")
    assert t.path().read_bytes() == raw and not _baks(t.path())


def _old_hooks(t, gemini: bool) -> dict:
    """The hook groups 0.6.1 wrote: today's shape, with the old executable (and, in Gemini,
    the old hook names and matcher)."""
    g = t.hook_groups({"command": OLD_EXE})
    if gemini:
        for lst in g.values():
            for grp in lst:
                for h in grp["hooks"]:
                    h["name"] = h["name"].replace("keepfold", "cortex")
                if "matcher" in grp:
                    grp["matcher"] = grp["matcher"].replace("keepfold", "cortex")
    return g


@pytest.mark.parametrize("tid", ["codex", "gemini-cli"])
def test_old_hooks_move_and_no_hook_runs_cortex(home, tid):
    t = targets.by_id()[tid]
    mine = {"hooks": [{"type": "command", "command": "echo hi"}]}
    old = _old_hooks(t, tid == "gemini-cli")
    assert all(not targets._OURS.search(h["command"])  # connect alone does not see them
               for lst in old.values() for g in lst for h in g["hooks"])
    hooks = {ev: [mine, *lst] if ev == "SessionStart" else lst for ev, lst in old.items()}
    _put(t.hook_path(), {"hooks": hooks, "theme": "x"})
    rows = [r for r in _rows(t.label) if r["file"] == str(t.hook_path())]
    assert [r["action"] for r in rows] == ["moved"] and _baks(t.hook_path())
    data = json.loads(t.hook_path().read_text(encoding="utf-8"))
    want = t.hook_groups(NEW_BASE)
    assert data["theme"] == "x" and set(data["hooks"]) == set(want)
    for ev, lst in want.items():
        assert [g for g in data["hooks"][ev] if g is not mine and g != mine] == lst
    assert mine in data["hooks"]["SessionStart"]
    text = json.dumps(data)
    assert "cortex" not in text.replace("Mi Cortex vault", "").lower()  # nothing fires on it
    _all_nothing()


def test_hand_written_hook_is_left(home):
    t = targets.by_id()["codex"]
    hand = {"hooks": [{"type": "command", "command": "cortex prime-hook"}]}
    old = _old_hooks(t, False)
    old["UserPromptSubmit"].append(hand)
    _put(t.hook_path(), {"hooks": old})
    rows = [r for r in _rows("Codex CLI") if r["file"] == str(t.hook_path())]
    assert sorted(r["action"] for r in rows) == ["left", "moved"]
    data = json.loads(t.hook_path().read_text(encoding="utf-8"))
    assert hand in data["hooks"]["UserPromptSubmit"]
    rows = [r for r in _rows("Codex CLI") if r["action"] != "nothing"]
    assert [r["action"] for r in rows] == ["left"]  # only the owner's hook stays reported


def test_codex_hooks_malformed_is_untouched(home):
    t = targets.by_id()["codex"]
    raw = _put(t.hook_path(), '{"hooks": {"UserPromptSubmit": [ "cortex prime-hook" ')
    (row,) = [r for r in _rows("Codex CLI") if r["file"] == str(t.hook_path())]
    assert row["action"] == "error" and t.hook_path().read_bytes() == raw


@pytest.mark.parametrize("crlf", [False, True])
def test_rules_block_swapped_in_place_byte_for_byte(home, crlf):
    t = targets.by_id()["codex"]
    before = "\ufeff# My agents\r\n\r\nBe brief.  \r\n\r\n"
    after = "\r\n## Mine\r\nTabs, not spaces.\t\r\n"
    if not crlf:
        before, after = before.replace("\r\n", "\n"), after.replace("\r\n", "\n")
    block = OLD_BLOCK.replace("\n", "\r\n") if crlf else OLD_BLOCK
    _put(t.rule_path(), before + block + after)
    (row,) = [r for r in _rows("Codex CLI") if r["file"] == str(t.rule_path())]
    assert row["action"] == "moved" and _baks(t.rule_path())
    raw = t.rule_path().read_bytes().decode("utf-8")
    new = f"{targets.START}\n{targets.rule_block(NEW_BASE, t.CLIENT)}{targets.END}\n"
    assert raw == before + (new.replace("\n", "\r\n") if crlf else new) + after
    _all_nothing()


def test_rules_old_and_new_block_become_one(home):
    t = targets.by_id()["codex"]
    new = f"{targets.START}\nmine\n{targets.END}\n"
    _put(t.rule_path(), "top\n" + OLD_BLOCK + "middle\n" + new + "end\n")
    (row,) = [r for r in _rows("Codex CLI") if r["file"] == str(t.rule_path())]
    assert row["action"] == "moved"
    assert t.rule_path().read_text(encoding="utf-8") == "top\nmiddle\n" + new + "end\n"


def test_rules_unpaired_old_markers_are_untouched(home):
    t = targets.by_id()["codex"]
    raw = _put(t.rule_path(), "mine\n" + OLD_BLOCK.replace("<!-- cortex:end -->\n", ""))
    (row,) = [r for r in _rows("Codex CLI") if r["file"] == str(t.rule_path())]
    assert row["action"] == "error" and "pair up" in row["note"]
    assert t.rule_path().read_bytes() == raw and not _baks(t.rule_path())


def test_gemini_md_block(home):
    t = targets.by_id()["gemini-cli"]
    _put(t.md(), "My notes.\n\n" + OLD_BLOCK)
    (row,) = [r for r in _rows("Gemini CLI") if r["file"] == str(t.md())]
    assert row["action"] == "moved"
    assert t.md().read_text(encoding="utf-8") == (
        f"My notes.\n\n{targets.START}\n{t.BLOCK}{targets.END}\n")
    _all_nothing()


@pytest.mark.parametrize("tid", ["cursor", "vscode"])
def test_rules_file_named_after_cortex_moves(home, tid):
    t = targets.by_id()[tid]
    old_path, head = targets.OLD_RULES[tid][0](), targets.OLD_RULES[tid][1]
    _put(old_path, head + "\n" + OLD_BLOCK)
    (row,) = [r for r in _rows(t.label) if r["file"] == str(old_path)]
    assert row["action"] == "moved" and Path(row["backup"]).is_file()
    assert not old_path.exists()
    assert t.rule_path().read_text(encoding="utf-8") == (
        f"{t.RULE_HEAD}\n{targets.START}\n{targets.rule_block(NEW_BASE, t.CLIENT)}"
        f"{targets.END}\n")
    _all_nothing()


def test_rules_file_named_after_cortex_keeps_the_owners_text(home):
    t = targets.by_id()["cursor"]
    old_path, head = targets.OLD_RULES["cursor"][0](), targets.OLD_RULES["cursor"][1]
    _put(old_path, head + "Always answer in Spanish, José.\n\n" + OLD_BLOCK + "Bye.\n")
    (row,) = [r for r in _rows("Cursor") if r["file"] == str(old_path)]
    assert row["action"] == "moved" and "your own text" in row["note"]
    assert old_path.read_text(encoding="utf-8") == (
        head + "Always answer in Spanish, José.\n\nBye.\n")
    assert targets.START in t.rule_path().read_text(encoding="utf-8")
    _all_nothing()


# ---- Claude Code's plugin -------------------------------------------------------------------


def _settings(plugins: dict) -> Path:
    p = Path.home() / ".claude" / "settings.json"
    _put(p, {"enabledPlugins": plugins, "theme": "dark"})
    return p


def test_plugin_without_claude_is_only_reported(home):
    p = _settings({"cortex@cortex": True, "other@x": True})
    raw = p.read_bytes()
    (row,) = [r for r in _rows("Claude Code") if r["file"] == str(p)]
    assert row["action"] == "left" and "keepfold@keepfold" in row["note"]
    assert p.read_bytes() == raw and not _baks(p)
    assert doctor.check_old_connections()[0] == "WARN"


def test_plugin_moves_through_claude(home, monkeypatch):
    p, ran = _settings({"cortex@cortex": True}), []

    def run(args, timeout=300):  # the `claude` CLI, as far as settings.json goes
        ran.append(args[1:])
        data = json.loads(p.read_text(encoding="utf-8"))
        if args[1:3] == ["plugin", "install"]:
            data["enabledPlugins"][args[3]] = True
        if args[1:3] == ["plugin", "uninstall"]:
            del data["enabledPlugins"][args[3]]
        p.write_text(json.dumps(data), encoding="utf-8")
        return True, "ok"
    monkeypatch.setattr(targets, "_which", lambda c: {"claude": "C:\\bin\\claude.exe",
                                                      "git": "C:\\git\\git.exe"}.get(c))
    monkeypatch.setattr(targets, "_run", run)
    monkeypatch.setattr(targets.ClaudeCode, "_source", lambda self: "owner/keepfold")
    assert [r["action"] for r in _rows("Claude Code", dry_run=True)] == ["moved"] and ran == []
    (row,) = _rows("Claude Code")
    assert row["action"] == "moved" and Path(row["backup"]).is_file()
    assert ran == [["plugin", "marketplace", "add", "owner/keepfold"],
                   ["plugin", "install", "keepfold@keepfold"],
                   ["plugin", "uninstall", "cortex@cortex"],
                   ["plugin", "marketplace", "remove", "cortex"]]
    assert json.loads(p.read_text(encoding="utf-8"))["enabledPlugins"] == {
        "keepfold@keepfold": True}
    _all_nothing()
    assert len(ran) == 4


def test_plugin_install_failing_keeps_the_old_one(home, monkeypatch):
    p = _settings({"cortex@cortex": True})
    monkeypatch.setattr(targets, "_which", lambda c: "C:\\bin\\" + c + ".exe")
    monkeypatch.setattr(targets, "_run", lambda args, timeout=300: (False, "no internet"))
    monkeypatch.setattr(targets.ClaudeCode, "_source", lambda self: "owner/keepfold")
    (row,) = _rows("Claude Code")
    assert row["action"] == "error" and "no internet" in row["note"]
    assert json.loads(p.read_text(encoding="utf-8"))["enabledPlugins"] == {"cortex@cortex": True}


# ---- doctor ---------------------------------------------------------------------------------


def test_doctor_lists_what_is_left_until_moved(home):
    assert doctor.check_old_connections()[0] == "PASS"
    p = _path("claude-desktop")
    _put(p, {"mcpServers": {"cortex": OLD}})
    status, _, detail, fix = doctor.check_old_connections()
    assert status == "WARN" and str(p) in detail and "Claude Desktop" in fix
    targets.migrate_old_entries()
    assert doctor.check_old_connections()[0] == "PASS"
