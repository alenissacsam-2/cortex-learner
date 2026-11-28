"""packaging/linux: the launcher runs the newest Keepfold (Review Focus 4: 0.10.0 beats 0.9.0,
a per-user copy beats an older /opt seed), and the .deb's control and desktop files."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from keepfold.app.desktop import DESKTOP_ID

LINUX = Path(__file__).resolve().parent.parent / "packaging" / "linux"
DEPENDS = {"gir1.2-gtk-3.0", "gir1.2-webkit2-4.1", "libgirepository-2.0-0",
           "gir1.2-ayatanaappindicator3-0.1"}  # DESIGN.md 11: all the frozen app needs


def fields(path: Path) -> dict[str, str]:
    return dict(line.split("=" if path.suffix == ".desktop" else ": ", 1)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line[:1].isalpha())


def copy(root: Path, version: str) -> Path:
    """A fake Keepfold <version> under root whose two programs print their own path and args."""
    d = root / version
    d.mkdir(parents=True)
    for name in ("keepfold", "keepfold-app"):
        (d / name).write_text('#!/bin/sh\necho "$0" "$@"\n', encoding="utf-8")
        (d / name).chmod(0o755)
    return d


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="launcher.sh runs on Linux (CI)")
def test_launcher_picks_newest_version(tmp_path):
    home, opt, bin_dir = tmp_path / "home dir", tmp_path / "opt", tmp_path / "bin"
    user = home / ".local" / "share" / "keepfold" / "app"
    bin_dir.mkdir()
    for name in ("keepfold", "keepfold-app"):  # the .deb installs it under both names
        shutil.copy(LINUX / "launcher.sh", bin_dir / name)
    env = {**os.environ, "HOME": str(home), "KEEPFOLD_OPT_ROOT": str(opt)}

    def run(name: str = "keepfold", *args: str) -> str:
        r = subprocess.run(["bash", str(bin_dir / name), *args], env=env, capture_output=True,
                           text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    r = subprocess.run(["bash", str(bin_dir / "keepfold")], env=env, capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 1 and "not installed" in r.stderr

    copy(opt, "0.9.0")
    seed = copy(opt, "0.10.0")
    assert run() == f"{seed}/keepfold"  # sort -V: a string sort would pick 0.9.0
    assert run("keepfold-app", "--hidden", "a b") == f"{seed}/keepfold-app --hidden a b"

    same = copy(user, "0.10.0")
    assert run() == f"{same}/keepfold"  # same version: the per-user copy
    newer = copy(user, "0.10.1")
    assert run() == f"{newer}/keepfold"  # Update now's copy beats the older /opt seed
    (user / "0.11.0").mkdir()  # an update still unpacking: no program yet
    copy(user, ".0.13.0")  # an update's dot-folder, before its rename into place
    assert run() == f"{newer}/keepfold"
    deb = copy(opt, "0.12.0")
    assert run() == f"{deb}/keepfold"  # a newer .deb installed later wins


def test_deb_control_and_desktop_entry():
    control = fields(LINUX / "DEBIAN" / "control")
    assert set(control["Depends"].split(", ")) == DEPENDS
    assert control["Recommends"] == "tesseract-ocr"
    assert control["Architecture"] == "amd64"
    entry = fields(LINUX / f"{DESKTOP_ID}.desktop")
    assert entry["Exec"] == "/usr/bin/keepfold-app"
    assert entry["StartupWMClass"] == "keepfold-app"  # the frozen program's file name
    assert entry["Icon"] == "keepfold"


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="the .deb's prerm runs under /bin/sh on Linux (CI)")
def test_prerm_disconnects_the_sudo_user_only_on_remove_and_never_fails(tmp_path):
    """Port Task 25: `apt remove` takes Keepfold out of the AI apps of the user who ran sudo;
    an upgrade, root or no SUDO_USER does nothing; a failing disconnect never stops apt."""
    prerm = LINUX / "DEBIAN" / "prerm"
    assert subprocess.run(["sh", "-n", str(prerm)]).returncode == 0
    fake, log = tmp_path / "bin", tmp_path / "log"
    fake.mkdir()
    (fake / "timeout").write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 3\n',
                                  encoding="utf-8")  # also the failing disconnect
    (fake / "timeout").chmod(0o755)

    def run(*args: str, **env: str) -> None:
        e = {k: v for k, v in os.environ.items() if k != "SUDO_USER"}
        r = subprocess.run(["sh", str(prerm), *args], capture_output=True, text=True, timeout=30,
                           env={**e, "PATH": f"{fake}:{e.get('PATH', '/usr/bin:/bin')}", **env})
        assert r.returncode == 0, r.stderr

    run("upgrade", "0.7.0", SUDO_USER="alice")
    run("remove")
    run("remove", SUDO_USER="root")
    assert not log.exists()
    run("remove", SUDO_USER="alice")
    assert log.read_text().split() == ["60", "runuser", "-u", "alice", "--", "/usr/bin/keepfold",
                                      "disconnect"]
