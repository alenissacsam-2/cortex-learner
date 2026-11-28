"""Quick capture on Mac and Linux (port Task 26): `keepfold capture` shows the running app's box
or starts the app; the Mac hotkey through Carbon (a FakeCarbon: conftest's _no_real_carbon);
GNOME's custom keybinding through gsettings (a fake targets._run). Never a real hotkey, a real
gsettings or a real app start."""

import itertools
import sys

import pytest
from test_app import _serve, call
from typer.testing import CliRunner

from keepfold import cli, update
from keepfold.app import bundle, desktop, server, targets

runner = CliRunner()
KB = desktop.GNOME_KB
SCHEMA = desktop.GNOME_SCHEMA


# ---- GNOME: the custom keybinding, merged into the list -----------------------------------


@pytest.mark.parametrize(("text", "want"), [
    ("@as []\n", []), ("[]", []), ("['/a/', '/b/']\n", ["/a/", "/b/"]),
    ("['/org/x/custom0/']", ["/org/x/custom0/"])])
def test_the_gsettings_list_parses(text, want):
    assert desktop._gnome_list(text) == want


@pytest.mark.parametrize("bad", ["", "nonsense", "[1, 2]", "'/a/'", "__import__('os')"])
def test_a_list_that_does_not_parse_is_refused(bad):
    with pytest.raises(ValueError):
        desktop._gnome_list(bad)


class FakeGsettings:
    """targets._run for gsettings: the custom-keybindings list and each entry's keys."""

    def __init__(self, listed="['/a/']", ok=True):
        self.listed, self.ok, self.calls, self.keys = listed, ok, [], {}

    def __call__(self, args, timeout=300):
        assert args[0] == "gsettings"
        self.calls.append(tuple(args[1:]))
        if not self.ok:
            return False, "No such schema"
        if args[1] == "get":
            return True, self.listed + "\n"
        if args[1] == "set" and args[2] == SCHEMA:
            self.listed = args[4]
        elif args[1] == "set":
            self.keys[args[3]] = args[4]
        elif args[1] == "reset-recursively":
            self.keys.clear()
        return True, ""

    def sets(self):
        return [c for c in self.calls if c[0] != "get"]


@pytest.fixture
def gnome(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    monkeypatch.setattr(bundle, "cli_path", lambda: None)
    monkeypatch.setattr("keepfold.clients.keepfold_command", lambda: "/home/u/bin/keepfold")
    fake = FakeGsettings()
    monkeypatch.setattr(targets, "_run", fake)
    return fake


def test_gnome_on_adds_keepfolds_entry_keeping_the_others(gnome):
    h = desktop._hotkey(lambda: None)
    assert isinstance(h, desktop.GnomeHotkey)
    h.start()
    assert desktop._gnome_list(gnome.listed) == ["/a/", KB]  # merged, never replaced
    assert gnome.keys == {"name": "'Keepfold quick capture'",
                          "command": "'/home/u/bin/keepfold capture'",
                          "binding": "'<Control><Alt>space'"}
    # the entry is complete before the list names it
    assert gnome.sets()[-1][:3] == ("set", SCHEMA, "custom-keybindings")
    assert h.label() == "Ctrl+Alt+Space" and h.taken is False
    n = len(gnome.calls)
    h.start()  # again (the next app start): no second copy in the list
    assert desktop._gnome_list(gnome.listed) == ["/a/", KB]
    assert all(c[1] != SCHEMA for c in gnome.calls[n:] if c[0] == "set")
    h.stop()  # Quit keeps the binding: `keepfold capture` starts the app
    assert KB in gnome.listed


def test_gnome_change_and_off_remove_only_keepfolds_entry(gnome):
    gnome.listed = f"['/a/', '{KB}', '/b/']"
    h = desktop._hotkey(lambda: None)
    h.start()
    desktop.save_hotkey({"hotkey": "ctrl+alt+k"})
    h.change()
    assert gnome.keys["binding"] == "'<Control><Alt>k'" and h.label() == "Ctrl+Alt+K"
    desktop.save_hotkey({"hotkey": "off"})
    h.change()
    assert desktop._gnome_list(gnome.listed) == ["/a/", "/b/"] and gnome.keys == {}
    assert ("reset-recursively", f"{SCHEMA}.custom-keybinding:{KB}") in gnome.calls
    assert h.label() is None and h.taken is False
    h.change()  # off again: nothing to remove, nothing written
    assert gnome.sets()[-1][0] == "reset-recursively" and len(
        [c for c in gnome.calls if c[:2] == ("set", SCHEMA)]) == 1


def test_gnome_an_empty_list_and_the_deb_command(gnome, monkeypatch):
    gnome.listed = "@as []"
    monkeypatch.setattr(bundle, "cli_path", lambda: bundle.LINUX_CLI)
    desktop._hotkey(lambda: None).start()
    assert desktop._gnome_list(gnome.listed) == [KB]
    assert gnome.keys["command"] == "'/usr/bin/keepfold capture'"


def test_gnome_without_gsettings_or_an_odd_list_writes_nothing(gnome):
    gnome.ok = False
    h = desktop._hotkey(lambda: None)
    h.start()
    assert h.label() is None and gnome.sets() == []
    gnome.ok, gnome.listed = True, "something else"
    h.change()
    assert h.label() is None and gnome.sets() == []


def test_other_linux_desktops_have_no_hotkey_and_a_note(home, monkeypatch, root_committed):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setattr(targets, "_run", lambda *a, **k: pytest.fail("gsettings ran"))
    assert desktop._hotkey(lambda: None) is None
    srv, app = _serve(root_committed)
    try:
        assert call(app, "GET", "/api/app/hotkey")[1]["note"] is None  # the browser app
        app.desktop = desktop.Desktop(app, "http://x")
        out = call(app, "GET", "/api/app/hotkey")[1]
        assert out["desktop"] is False and "keepfold capture" in out["note"]
    finally:
        srv.shutdown()
        srv.server_close()


# ---- Mac: Carbon's RegisterEventHotKey ----------------------------------------------------


@pytest.fixture
def mac(home, monkeypatch, _no_real_carbon):
    monkeypatch.setattr(sys, "platform", "darwin")
    return _no_real_carbon


CTRL_OPT = desktop.MAC_CONTROL | desktop.MAC_OPTION


def test_the_mac_hotkey_installs_its_handler_then_registers(mac):
    pressed = []
    h = desktop._hotkey(lambda: pressed.append(1))
    assert isinstance(h, desktop.MacHotkey)
    h.start()
    assert mac.calls == [("target",), ("install", 7, 1, desktop.KEYB, desktop.HOTKEY_PRESSED),
                         ("target",), ("register", CTRL_OPT, desktop.KVK_SPACE, 7)]
    assert h.label() == "Ctrl+Option+Space" and not h.taken
    assert mac.press() == 0 and pressed == [1]  # noErr
    desktop.save_hotkey({"hotkey": "ctrl+alt+k"})
    h.change()
    assert mac.calls[4:] == [("unregister",), ("target",),
                             ("register", CTRL_OPT, desktop.KVK_K, 7)]
    assert h.label() == "Ctrl+Option+K"
    desktop.save_hotkey({"hotkey": "off"})
    h.change()
    assert mac.calls[-1] == ("unregister",) and h.label() is None and not mac.registered
    desktop.save_hotkey({"hotkey": "win+alt+space"})
    h.change()
    assert h.label() == "Cmd+Option+Space"
    h.stop()
    assert mac.calls[-1] == ("unregister",) and not mac.registered


def test_a_mac_key_another_app_holds_is_taken(mac):
    mac.held.add((CTRL_OPT, desktop.KVK_SPACE))
    h = desktop.MacHotkey(lambda: None)
    h.start()
    assert h.taken and h.label() is None
    h.stop()
    assert mac.calls[-1][0] == "register"  # nothing registered, nothing to unregister


def test_a_failed_press_never_ends_the_mac_hotkey(mac):
    def boom():
        raise RuntimeError("no box")
    h = desktop.MacHotkey(boom)
    h.start()
    assert mac.press() == 0


def test_the_box_is_centred_on_the_screen_with_the_mouse():
    main = ((0, 0, 1440, 900), (0, 0, 1440, 875))  # frame, visibleFrame; Cocoa's y runs up
    right = ((1440, -200, 1920, 1080), (1440, -200, 1920, 1055))
    size = (560, 120)
    assert desktop._box_origin((2000, 300), [main, right], size) == (
        1440 + (1920 - 560) / 2, -200 + 1055 * 2 / 3 - 60)
    assert desktop._box_origin((10, 10), [main, right], size) == (440, 875 * 2 / 3 - 60)
    assert desktop._box_origin((-5000, 0), [main, right], size) == (440, 875 * 2 / 3 - 60)


# ---- `keepfold capture` and the app's endpoint --------------------------------------------


class Box:
    def __init__(self):
        self.calls = []

    def show(self):
        self.calls.append("show")


def test_the_capture_endpoint_and_show_capture(root_committed, home):
    srv, app = _serve(root_committed)
    try:
        url = f"http://127.0.0.1:{app.port}/?t={app.token}"
        assert server.show_capture(url) is False  # the browser app: no box
        d = app.desktop = desktop.Desktop(app, "http://x")
        d.box = Box()
        assert server.show_capture(url) is True and d.box.calls == ["show"]
        assert call(app, "POST", "/api/window/capture", {}, token=False)[0] == 401
        assert d.box.calls == ["show"]
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def capture(monkeypatch):
    seen = {"started": 0, "shown": [], "urls": []}
    monkeypatch.setattr(bundle, "start_app", lambda: seen.__setitem__(
        "started", seen["started"] + 1))
    monkeypatch.setattr(server, "running_url", lambda: seen["urls"].pop(0) if seen["urls"]
                        else "http://127.0.0.1:1/?t=k" if seen["started"] else None)
    monkeypatch.setattr(server, "show_capture", lambda url: seen["shown"].append(url) or True)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    return seen


def test_keepfold_capture_asks_the_running_app(capture):
    capture["urls"] = ["http://127.0.0.1:5/?t=abc"]
    r = runner.invoke(cli.app, ["capture"])
    assert r.exit_code == 0 and capture["shown"] == ["http://127.0.0.1:5/?t=abc"]
    assert capture["started"] == 0


def test_keepfold_capture_starts_the_app_then_shows_the_box(capture):
    capture["urls"] = [None, None, None]  # not running, then still starting
    r = runner.invoke(cli.app, ["capture"])
    assert r.exit_code == 0, r.output
    assert capture["started"] == 1 and capture["shown"] == ["http://127.0.0.1:1/?t=k"]


def test_keepfold_capture_in_the_browser_app_says_so(capture, monkeypatch):
    capture["urls"] = ["http://127.0.0.1:5/?t=abc"]
    monkeypatch.setattr(server, "show_capture", lambda url: False)
    r = runner.invoke(cli.app, ["capture"])
    assert r.exit_code == 1 and capture["started"] == 0 and "window" in r.output


def test_keepfold_capture_gives_up_when_the_app_never_answers(capture, monkeypatch):
    monkeypatch.setattr(server, "running_url", lambda: None)
    clock = itertools.count(0, 5)
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock))
    r = runner.invoke(cli.app, ["capture"])
    assert r.exit_code == 1 and capture["started"] == 1 and capture["shown"] == []


@pytest.mark.parametrize(("platform", "mode", "exe", "want"), [
    ("darwin", "app", "/Applications/Keepfold.app/Contents/MacOS/keepfold-cli",
     ["open", "/Applications/Keepfold.app"]),
    ("linux", "app", "/opt/keepfold/0.7.0/keepfold", ["/usr/bin/keepfold-app"]),
    ("linux", "dev", "/venv/bin/python", ["/venv/bin/python", "-m", "keepfold.app.launch"]),
])
def test_start_app_per_os(monkeypatch, platform, mode, exe, want):
    seen = []
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "executable", exe)
    monkeypatch.setattr(update, "mode", lambda: mode)
    monkeypatch.setattr(bundle.subprocess, "Popen", lambda args, **kw: seen.append((args, kw)))
    bundle.start_app()
    ((args, kw),) = seen
    assert [a.replace("\\", "/") for a in args] == want
    assert kw["creationflags"] == bundle.NO_WINDOW and kw["start_new_session"] is True
