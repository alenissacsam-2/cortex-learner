"""R9: Zed, Cline, GitHub Copilot CLI and JetBrains Junie as Connect targets, on a temp home
(the `home` fixture sets HOME, APPDATA and LOCALAPPDATA; nothing reads the real configs)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cortex.app import targets

ENTRY = {"command": "C:\\tools\\cortex.exe", "args": ["mcp", "--vault", "C:\\vault"]}


def _paths(home):
    vs = targets._vscode_user()
    assert str(vs).startswith(str(home))
    return {
        "zed": (targets._zed_dir() / "settings.json", "context_servers", ENTRY),
        "cline": (vs / "globalStorage" / "saoudrizwan.claude-dev" / "settings"
                  / "cline_mcp_settings.json", "mcpServers", ENTRY),
        "copilot-cli": (home / ".copilot" / "mcp-config.json", "mcpServers",
                        {"type": "local", "tools": ["*"], **ENTRY}),
        "junie": (home / ".junie" / "mcp" / "mcp.json", "mcpServers", ENTRY),
    }


@pytest.mark.parametrize("tid", ["zed", "cline", "copilot-cli", "junie"])
def test_detect_add_keep_remove(home, tid):
    path, key, want = _paths(home)[tid]
    t = targets.by_id()[tid]
    assert not t.detect() and not t.connected()
    assert t.snippet(ENTRY) == json.dumps({key: {"cortex": want}}, indent=2)
    # the app's folder (not the file) decides detection; a missing file is then created
    folder = {"cline": path.parents[1], "copilot-cli": path.parent, "junie": path.parents[1],
              "zed": path.parent}[tid]
    folder.mkdir(parents=True)
    assert t.detect() and not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"theme": "dark", key: {"other": {"command": "x"}}}),
                    encoding="utf-8")
    original = path.read_bytes()
    assert [r["change"] for r in t.plan(True, ENTRY)] == ["add"]
    assert path.read_bytes() == original  # a plan writes nothing
    t.apply(ENTRY)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["theme"] == "dark" and data[key]["other"] == {"command": "x"}
    assert data[key]["cortex"] == want
    assert t.connected() and t.plan(True, ENTRY) == []
    assert original in [b.read_bytes() for b in path.parent.glob(path.name + ".cortex-*.bak")]
    t.remove()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "cortex" not in data[key] and data[key]["other"] == {"command": "x"}
    assert data["theme"] == "dark" and not t.connected()


@pytest.mark.parametrize("tid", ["zed", "cline", "copilot-cli", "junie"])
def test_a_missing_file_is_created_for_a_detected_app(home, tid):
    path, key, want = _paths(home)[tid]
    folder = path.parents[1] if tid in ("cline", "junie") else path.parent
    folder.mkdir(parents=True)
    assert [r["change"] for r in targets.plan_all({tid: True}, ENTRY)] == ["add"]
    targets.apply_all({tid: True}, ENTRY)
    assert json.loads(path.read_text(encoding="utf-8"))[key]["cortex"] == want


def test_an_app_that_is_not_installed_is_skipped(home):
    assert targets.plan_all({t: True for t in ("zed", "cline", "copilot-cli", "junie")},
                            ENTRY) == []


COMMENTED = '// Zed settings\n{\n  "theme": "One Dark", // mine\n  "context_servers": {},\n}\n'


def test_zed_with_comments_is_left_untouched(home):
    path = targets._zed_dir() / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(COMMENTED, encoding="utf-8")
    t = targets.by_id()["zed"]
    rows = t.plan(True, ENTRY)
    assert [(r["change"], r["detail"]) for r in rows] == [("error", targets.ZED_NOTE)]
    assert "context_servers" in t.snippet(ENTRY)  # the row shows what to paste
    with pytest.raises(ValueError, match="Zed's settings have comments"):
        t.apply(ENTRY)
    with pytest.raises(ValueError, match="Zed's settings have comments"):
        t.remove()
    row = {r["id"]: r for r in targets.describe(ENTRY)}["zed"]
    assert row["detected"] and not row["connected"] and "context_servers" in row["snippet"]
    done = targets.apply_all({"zed": True}, ENTRY)
    assert [(r["change"], r["detail"]) for r in done] == []  # plan_all drops error rows
    assert path.read_text(encoding="utf-8") == COMMENTED
    assert not list(path.parent.glob("*.bak"))


@pytest.mark.parametrize("bad", ['{"context_servers": []}', "[1]"])
def test_zed_other_bad_files_keep_their_own_message(home, bad):
    path = targets._zed_dir() / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(bad, encoding="utf-8")
    rows = targets.by_id()["zed"].plan(True, ENTRY)
    assert rows[0]["change"] == "error" and "untouched" in rows[0]["detail"]
    assert "comments" not in rows[0]["detail"]


def test_error_row_through_the_connect_plan(home):
    path = targets._zed_dir() / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(COMMENTED, encoding="utf-8")
    rows = targets.plan_all({"zed": True}, ENTRY)
    assert [(r["id"], r["change"], r["detail"]) for r in rows] == [
        ("zed", "error", targets.ZED_NOTE)]


def test_zed_paths_per_os(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert targets._zed_dir() == home / "APPDATA" / "Zed"  # %APPDATA%\Zed
    monkeypatch.setattr(sys, "platform", "darwin")
    assert targets._zed_dir() == home / ".config" / "zed"
    monkeypatch.setattr(sys, "platform", "linux")
    assert targets._zed_dir() == home / ".config" / "zed"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    assert targets._zed_dir() == home / "xdg" / "zed"


def test_cline_path_per_os(home, monkeypatch):
    t = targets.by_id()["cline"]
    tail = ("globalStorage", "saoudrizwan.claude-dev", "settings", "cline_mcp_settings.json")
    monkeypatch.setattr(sys, "platform", "win32")
    assert t.paths() == [home / "APPDATA" / "Code" / "User" / Path(*tail)]
    monkeypatch.setattr(sys, "platform", "darwin")
    assert t.paths() == [home / "Library" / "Application Support" / "Code" / "User"
                         / Path(*tail)]
    monkeypatch.setattr(sys, "platform", "linux")
    assert t.paths() == [home / ".config" / "Code" / "User" / Path(*tail)]


def test_new_targets_are_listed_and_connect_rows_exist(home):
    rows = {r["id"]: r for r in targets.describe(ENTRY)}
    for tid in ("zed", "cline", "copilot-cli", "junie"):
        assert rows[tid]["label"] and not rows[tid]["detected"] and not rows[tid]["connected"]
        assert "cortex" in rows[tid]["snippet"] and "error" not in rows[tid]
