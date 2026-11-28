"""Task 20: where the frozen Mac or Linux app lives (keepfold.app.bundle). AI apps get the app's
own `keepfold` path; a Mac app run from Downloads or the DMG (translocated) moves itself to
Applications before it connects anything. Nothing here touches /Applications or runs ditto or
open: the helpers are faked."""

import os
import sys
import time
from pathlib import Path

import pytest

from keepfold import clients
from keepfold.app import bundle, server, targets

pytestmark = pytest.mark.usefixtures("home")
MAC_EXE = "/Applications/Keepfold.app/Contents/MacOS/Keepfold"


@pytest.fixture
def vault(root_committed):
    (root_committed / ".keepfold").mkdir(exist_ok=True)
    return root_committed


def _on(monkeypatch, plat="darwin", frozen=True, exe=MAC_EXE):
    monkeypatch.setattr(sys, "platform", plat)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "executable", exe)


def test_cli_path_mac_bundle(monkeypatch):
    _on(monkeypatch)
    assert bundle.app_path().as_posix() == "/Applications/Keepfold.app"
    cli = bundle.cli_path()
    assert cli.as_posix() == "/Applications/Keepfold.app/Contents/MacOS/keepfold-cli"
    assert clients.installed_keepfold() == cli and clients.mcp_command() == str(cli)
    _on(monkeypatch, "linux", exe="/opt/keepfold/keepfold-app")
    assert bundle.app_path() is None and bundle.cli_path() == Path("/usr/bin/keepfold")
    assert clients.installed_keepfold() == Path("/usr/bin/keepfold")
    for plat in ("darwin", "linux", "win32"):  # a dev or pip install: no app paths
        _on(monkeypatch, plat, frozen=False)
        assert bundle.app_path() is None and bundle.cli_path() is None


@pytest.mark.parametrize("exe, moved", [
    ("/private/var/folders/x1/T/AppTranslocation/0A1B-2C/d/Keepfold.app/Contents/MacOS/Keepfold",
     True),
    ("/Volumes/Keepfold/Keepfold.app/Contents/MacOS/Keepfold", True),  # run from the DMG
    (MAC_EXE, False),
    ("/Users/me/Applications/Keepfold.app/Contents/MacOS/Keepfold", False),
])
def test_translocated_paths(monkeypatch, exe, moved):
    _on(monkeypatch, exe=exe)
    assert bundle.translocated() is moved
    _on(monkeypatch, "linux", exe=exe)  # only a Mac app is translocated
    assert bundle.translocated() is False
    _on(monkeypatch, frozen=False, exe=exe)
    assert bundle.translocated() is False


def test_connect_refused_while_translocated(monkeypatch, home, vault):
    (home / ".cursor").mkdir()
    app = server.App(vault)
    monkeypatch.setattr(bundle, "translocated", lambda: True)
    want = {"cursor": True}
    assert app.connect_plan({"want": want}) == {"move": True}
    assert app.connect_apply({"want": want}) == {"move": True}
    assert not app.jobs.running() and not (home / ".cursor" / "mcp.json").exists()
    monkeypatch.setattr(bundle, "translocated", lambda: False)  # moved: connecting works
    assert app.connect_plan({"want": want})["rows"]


def test_move_endpoint_moves_and_stops(monkeypatch, vault):
    app = server.App(vault)
    moved = []
    monkeypatch.setattr(bundle, "move_to_applications",
                        lambda: moved.append(1) or Path("/Applications/Keepfold.app"))
    monkeypatch.setattr(server.threading, "Timer", lambda s, fn: type(
        "T", (), {"start": lambda self: fn()})())
    assert app.app_move({}) == {"ok": True}
    assert moved and app.stop.is_set()


@pytest.fixture
def mac_app(monkeypatch, tmp_path):
    """A translocated app; ditto and the relaunch recorded, never run."""
    src = "/private/var/folders/x1/T/AppTranslocation/0A1B/d/Keepfold.app/Contents/MacOS/Keepfold"
    _on(monkeypatch, exe=src)
    calls = []
    monkeypatch.setattr(bundle, "_ditto", lambda a, b: calls.append(("ditto", a, b)) or (True, ""))
    monkeypatch.setattr(bundle, "_open_after",
                        lambda dest, pid: calls.append(("open", dest, pid)))
    monkeypatch.setattr(bundle, "APPLICATIONS", tmp_path / "Applications")
    return calls


def test_move_to_applications(mac_app, tmp_path, home):
    (tmp_path / "Applications").mkdir()
    dest = bundle.move_to_applications()
    assert dest == tmp_path / "Applications" / "Keepfold.app"
    assert mac_app == [("ditto", bundle.app_path(), dest), ("open", dest, os.getpid())]


def test_move_falls_back_to_home_applications(mac_app, home):
    dest = bundle.move_to_applications()  # APPLICATIONS missing: not writable
    assert dest == home / "Applications" / "Keepfold.app" and dest.parent.is_dir()
    assert [c[0] for c in mac_app] == ["ditto", "open"]


def test_move_refuses_over_a_copy_and_on_failure(mac_app, tmp_path, monkeypatch):
    (tmp_path / "Applications" / "Keepfold.app").mkdir(parents=True)
    with pytest.raises(ValueError, match="already in"):
        bundle.move_to_applications()
    assert mac_app == []  # nothing copied over the other copy
    (tmp_path / "Applications" / "Keepfold.app").rmdir()
    monkeypatch.setattr(bundle, "_ditto", lambda a, b: (False, "no space"))
    with pytest.raises(ValueError, match="no space"):
        bundle.move_to_applications()
    assert mac_app == []  # no relaunch after a failed copy


def test_local_bin_link_never_replaces_a_file(monkeypatch, home):
    _on(monkeypatch)
    link = home / ".local" / "bin" / "keepfold"
    link.parent.mkdir(parents=True)
    link.write_text("#!/bin/sh\nmy own keepfold\n")
    assert bundle.link_local_bin() is None
    assert link.read_text() == "#!/bin/sh\nmy own keepfold\n" and not link.is_symlink()
    _on(monkeypatch, "win32", frozen=False)  # not the Mac app: nothing is linked
    link.unlink()
    assert bundle.link_local_bin() is None and not link.exists()


def test_local_bin_link_made_and_replaces_only_a_link(monkeypatch, home, tmp_path):
    _on(monkeypatch)
    link = home / ".local" / "bin" / "keepfold"
    try:
        os.symlink(tmp_path / "old-keepfold", tmp_path / "probe")
    except OSError:
        pytest.skip("no symlinks here (Windows without Developer Mode)")
    assert bundle.link_local_bin() == link and Path(os.readlink(link)) == bundle.cli_path()
    link.unlink()
    link.symlink_to(tmp_path / "old-keepfold")  # an older link (a dangling one too)
    assert bundle.link_local_bin() == link and Path(os.readlink(link)) == bundle.cli_path()


def test_connect_apply_links_local_bin(monkeypatch, vault):
    app = server.App(vault)
    linked = []
    monkeypatch.setattr(bundle, "link_local_bin", lambda: linked.append(1))
    monkeypatch.setattr(targets, "apply_all", lambda want, base, say: [])
    app.connect_apply({"want": {}})
    for _ in range(200):
        if not app.jobs.running():
            break
        time.sleep(0.05)
    assert linked == [1] and app.jobs.view()["job"]["result"] == {"ok": True, "rows": []}
