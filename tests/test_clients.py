"""Claude desktop config discovery per OS and the shared writer of the cortex entry."""

import json
from pathlib import Path

import pytest

from cortex import clients, doctor
from cortex.app import targets

pytestmark = pytest.mark.usefixtures("home")

CFG = clients.CONFIG


def test_windows_probes_all_three_layouts(home):
    assert clients.claude_desktop_config_paths("win32") == []
    classic = home / "APPDATA" / "Claude"
    profile = home / "LOCALAPPDATA" / "Claude-Data" / "p1"
    store = home / "LOCALAPPDATA" / "Packages" / "Claude_abc123" / "LocalCache/Roaming/Claude"
    for d in (classic, profile, store):
        d.mkdir(parents=True)
    (profile / CFG).write_text("{}")
    (home / "LOCALAPPDATA" / "Claude-Data" / "empty").mkdir()  # no config: not a profile
    # the Store app's profiles come first, so a first setup writes where that app reads
    assert clients.claude_desktop_config_paths("win32") == [profile / CFG, classic / CFG,
                                                            store / CFG]
    fresh = home / "LOCALAPPDATA" / "Claude-Data" / "claude_abc123"  # a profile, no config yet
    fresh.mkdir()
    paths = clients.claude_desktop_config_paths("win32")
    assert paths == [fresh / CFG, profile / CFG, classic / CFG, store / CFG]
    (profile / CFG).unlink()
    assert clients.setup_targets(clients.claude_desktop_config_paths("win32")) == [fresh / CFG]


def test_macos_and_linux(home, monkeypatch):
    mac = home / "Library" / "Application Support" / "Claude"
    assert clients.claude_desktop_config_paths("darwin") == []
    mac.mkdir(parents=True)
    assert clients.claude_desktop_config_paths("darwin") == [mac / CFG]
    lin = home / ".config" / "Claude"
    lin.mkdir(parents=True)
    assert clients.claude_desktop_config_paths("linux") == [lin / CFG]
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    assert clients.claude_desktop_config_paths("linux") == []


def test_desktop_writer_keeps_keys_backs_up_and_is_idempotent(tmp_path):
    """Setup and the app share one writer (app.targets): timestamped backups, and a `.bak`
    the user already has is never overwritten."""
    path = tmp_path / CFG
    old = {"theme": "dark", "mcpServers": {"other": {"command": "x"}}}
    path.write_text(json.dumps(old), encoding="utf-8")
    (tmp_path / (CFG + ".bak")).write_text("mine", encoding="utf-8")
    vault = tmp_path / "My Vault"
    entry = clients.server_entry(clients.cortex_command(), vault)
    assert Path(entry["command"]).is_absolute()
    assert entry["args"] == ["mcp", "--vault", str(vault)]

    assert targets.edit_json(path, "mcpServers", entry, dry_run=True) == "add"
    assert json.loads(path.read_text(encoding="utf-8")) == old  # dry run wrote nothing
    assert targets.edit_json(path, "mcpServers", entry) == "add"
    got = json.loads(path.read_text(encoding="utf-8"))
    assert got["theme"] == "dark" and got["mcpServers"]["other"] == {"command": "x"}
    assert got["mcpServers"]["cortex"] == entry
    assert (tmp_path / (CFG + ".bak")).read_text(encoding="utf-8") == "mine"
    baks = list(tmp_path.glob(CFG + ".cortex-*.bak"))
    assert [json.loads(b.read_text(encoding="utf-8")) for b in baks] == [old]
    before = path.read_bytes()
    assert targets.edit_json(path, "mcpServers", entry) == "none"
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))
    # doctor sees the merged entry as a Cortex server
    assert doctor._is_cortex_server(got["mcpServers"]["cortex"])


def test_setup_targets_prefers_existing_configs(tmp_path):
    a, b, c = (tmp_path / n / CFG for n in "abc")
    for p in (a, b, c):
        p.parent.mkdir()
    assert clients.setup_targets([a, b, c]) == [a]  # none exists: only the first folder
    b.write_text("{}", encoding="utf-8")
    c.write_text("{}", encoding="utf-8")
    assert clients.setup_targets([a, b, c]) == [b, c]
    assert clients.setup_targets([]) == []


def test_desktop_writer_accepts_a_bom(tmp_path):  # Notepad / PowerShell 5.1 "UTF-8"
    path = tmp_path / CFG
    path.write_text('{"mcpServers": {"other": {"command": "x"}}}', encoding="utf-8-sig")
    entry = clients.server_entry("/abs/cortex", tmp_path)
    assert targets.edit_json(path, "mcpServers", entry) == "add"
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"].keys() == {"other", "cortex"}
    assert doctor._read_json(path)["mcpServers"]["cortex"] == entry
