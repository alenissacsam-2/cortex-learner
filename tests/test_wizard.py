"""The local-model recommendation by hardware kind; the free-space check, winget's flags and
finding git right after it installs (nothing installs or downloads here)."""

import shutil
import subprocess
import sys
from collections import namedtuple

import pytest

from keepfold import session
from keepfold.app import targets, wizard


def test_without_an_nvidia_card_the_recommendation_stops_at_4b():
    assert wizard.recommend(16, "ram") == "4b"
    assert wizard.recommend(4, "ram") == "2b"
    assert wizard.recommend(12, "gpu") == "9b"
    assert wizard.recommend(None, "ram") == "2b"


def test_free_space_ok_says_both_sizes_and_blocks_below_size_plus_1gb(tmp_path, monkeypatch):
    """Deps A6: "Needs about 2.0 GB; 1.1 GB free on C:." blocks; enough space passes; the
    nearest existing folder counts; a failed check never blocks."""
    du = namedtuple("du", "total used free")
    seen = []
    monkeypatch.setattr(shutil, "disk_usage", lambda p: (seen.append(p), du(0, 0, 1.1e9))[1])
    ok, msg = wizard.free_space_ok(tmp_path / "not" / "yet", 2 * 10**9)
    where = tmp_path.drive or "this disk"
    assert not ok and msg.startswith(f"Needs about 2.0 GB; 1.1 GB free on {where}.")
    assert seen == [tmp_path.absolute()]
    assert wizard.free_space_ok(tmp_path, 70 * 10**6) == (
        True, f"Needs about 70 MB; 1.1 GB free on {where}.")
    assert wizard.free_space_ok(tmp_path, 10**8)[0] is True  # 1.1 GB >= 0.1 GB + 1 GB
    assert wizard.free_space_ok(tmp_path, 2 * 10**8)[0] is False

    def broken(p):
        raise OSError("no such drive")
    monkeypatch.setattr(shutil, "disk_usage", broken)
    assert wizard.free_space_ok(tmp_path, 10**12) == (True, "")


def test_ollama_install_never_asks_and_names_package_and_license(monkeypatch):
    """Deps A1: winget has no console in the app: --disable-interactivity, and the agreements
    are accepted only with a License link to show beside the button."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(targets, "_which", lambda c: "winget" if c == "winget" else None)
    plan = wizard.install_plan()
    assert plan["run"][:5] == ["winget", "install", "--id", "Ollama.Ollama", "-e"]
    assert {"--disable-interactivity", "--accept-package-agreements",
            "--accept-source-agreements"} <= set(plan["run"])
    assert "Ollama.Ollama" in plan["source"] and plan["size"] == "about 1 GB"
    assert plan["license"].startswith("https://")


@pytest.mark.skipif(sys.platform != "win32", reason="Git for Windows folders")
def test_git_exe_finds_git_where_the_installer_put_it(tmp_path, monkeypatch):
    """Deps A2: PATH first, then Program Files and the per-user Programs folder, then PATH as
    the registry has it now; the vault's git calls use what it finds."""
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "L"))
    monkeypatch.chdir(tmp_path)
    assert session.git_exe() is None
    exe = tmp_path / "L" / "Programs" / "Git" / "cmd" / "git.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert session.git_exe().lower() == str(exe).lower()
    exe.unlink()
    reg = tmp_path / "reg"
    reg.mkdir()
    (reg / "git.exe").write_bytes(b"")
    monkeypatch.setattr(session, "_registry_path", lambda: f";{reg};")
    assert session.git_exe().lower() == str(reg / "git.exe").lower()
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: (
        ran.append(cmd), subprocess.CompletedProcess(cmd, 0, "", ""))[1])
    session._git(tmp_path, "status")
    assert ran[0][0].lower() == str(reg / "git.exe").lower()
    assert ran[0][1:] == ["-C", str(tmp_path), "status"]
