"""Status's Claude Code row: Claude Code starts the first `keepfold` on PATH; when that is another
copy than the installed Keepfold, of another version, Status warns and its Fix puts the installed
bin folder first on the user PATH. A fake winreg throughout: never the real registry or PATH."""

import contextlib
import os
import shutil
import subprocess
import sys

import pytest
from test_app import _install, call
from test_app import app as app  # noqa: F401 (fixture)
from test_app import fakes as fakes  # noqa: F401 (fixture: nothing real runs or opens)
from test_app import vault as vault  # noqa: F401 (fixture)

from keepfold import update
from keepfold.app import status, winpath

pytestmark = pytest.mark.usefixtures("home")
REAL_IN_SYSTEM = winpath.in_system  # module import time: before conftest's fixture fakes it
REAL_PROBE = status.keepfold_version


class FakeReg:
    """The two PATH values winpath reads: HKCU\\Environment and the machine's, as (value, type)."""
    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = "HKCU", "HKLM"
    KEY_READ, KEY_SET_VALUE = 1, 2
    REG_SZ, REG_EXPAND_SZ = 1, 2

    def __init__(self, user=None, system=None):
        self.values = {"HKCU": user, "HKLM": system}

    def OpenKey(self, hive, key, *a):
        return contextlib.nullcontext(hive)

    def QueryValueEx(self, k, name):
        if self.values[k] is None:
            raise FileNotFoundError(name)
        return self.values[k]

    def SetValueEx(self, k, name, _, kind, value):
        assert (k, name) == ("HKCU", "Path")
        self.values[k] = (value, kind)


@pytest.fixture
def setup(home, monkeypatch):
    """An installed Keepfold, an `other` copy first on PATH and a fake registry; returns
    (installed exe, other exe, the fake registry, the version list the probe answers)."""
    exe = _install(home)
    other = home / ".local" / "bin" / "keepfold.exe"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"")
    found, real = {"keepfold": str(other)}, shutil.which
    monkeypatch.setattr(shutil, "which", lambda cmd, *a, **k: found.get(cmd) if cmd == "keepfold"
                        else real(cmd, *a, **k))
    ver = ["0.3.9"]
    monkeypatch.setattr(status, "keepfold_version", lambda e: ver[0])
    monkeypatch.setattr(update, "current", lambda: "0.4.1")
    reg = FakeReg(user=(f"C:\\Tools;{other.parent};{exe.parent};%USERPROFILE%\\x",
                        FakeReg.REG_SZ),
                  system=("C:\\Windows\\system32;C:\\Windows", FakeReg.REG_EXPAND_SZ))
    monkeypatch.setitem(sys.modules, "winreg", reg)
    monkeypatch.setattr(winpath, "in_system", REAL_IN_SYSTEM)
    sent = []
    monkeypatch.setattr(winpath, "broadcast", lambda: sent.append(1))
    monkeypatch.setenv("PATH", f"{other.parent}{os.pathsep}{os.environ['PATH']}")  # restored after
    return exe, other, reg, ver, found, sent


def _row(vault):
    return next((r for r in status.rows(vault) if r["id"] == "claude-code"), None)


def test_no_row_for_the_same_exe_or_the_same_version(setup, vault):
    exe, other, reg, ver, found, _ = setup
    ver[0] = "0.4.1"
    assert _row(vault) is None  # another exe, same version
    ver[0] = "0.3.9"
    found["keepfold"] = str(exe)
    assert _row(vault) is None  # the installed one comes first
    found.clear()
    assert _row(vault) is None  # no keepfold on PATH


def test_an_older_copy_first_warns_with_a_fix(setup, vault):
    _, other, *_ = setup
    r = _row(vault)
    assert r["state"] == "warn"
    assert r["value"] == "Claude Code runs Keepfold 0.3.9, the app is 0.4.1"
    assert str(other) in r["detail"] and "an older copy" in r["detail"]
    assert r["fix"] == {"post": "/api/connect/path", "label": "Fix"}
    names = [x["id"] for x in status.rows(vault)]
    assert names.index("claude-code") == names.index("apps") + 1


def test_an_unknown_version_still_warns(setup, vault):
    setup[3][0] = None
    r = _row(vault)
    assert "an unknown Keepfold version" in r["value"] and r["fix"]


def test_a_copy_on_the_system_path_gets_no_fix(setup, vault):
    _, other, reg, *_ = setup
    reg.values["HKLM"] = (f"C:\\Windows;{other.parent}\\", FakeReg.REG_EXPAND_SZ)
    r = _row(vault)
    assert r["state"] == "warn" and "fix" not in r and r["action"] is None
    assert "system PATH" in r["detail"] and str(other.parent) in r["detail"]


def test_fix_puts_keepfold_first_keeping_the_rest_and_the_type(setup, app, vault):  # noqa: F811
    exe, other, reg, _, _, sent = setup
    assert call(app, "POST", "/api/connect/path", {}, token=False)[0] == 401
    assert reg.values["HKCU"][0].startswith("C:\\Tools") and not sent
    code, js, *_ = call(app, "POST", "/api/connect/path", {})
    assert code == 200 and js["say"] == ("Done. Quit Claude completely and open it again, so "
                                         "its Keepfold tools use 0.4.1.")
    assert reg.values["HKCU"] == (f"{exe.parent};C:\\Tools;{other.parent};%USERPROFILE%\\x",
                                  FakeReg.REG_SZ)
    assert sent == [1] and os.environ["PATH"].startswith(f"{exe.parent};")


def test_first_and_a_missing_user_path(monkeypatch):
    assert winpath.first("a;;B\\;c", "b") == "b;a;c"
    assert winpath.first("", "b") == "b"
    reg = FakeReg()
    monkeypatch.setitem(sys.modules, "winreg", reg)
    monkeypatch.setattr(winpath, "broadcast", lambda: None)
    winpath.put_first(r"C:\Keepfold\bin")
    assert reg.values["HKCU"] == (r"C:\Keepfold\bin", FakeReg.REG_EXPAND_SZ)


def test_the_version_probe_parses_and_fails_quietly(monkeypatch):
    def answer(out):
        return lambda *a, **k: subprocess.CompletedProcess(a, 0, out, "")
    monkeypatch.setattr(subprocess, "run", answer("keepfold 0.3.9\n"))
    assert REAL_PROBE("x") == "0.3.9"
    monkeypatch.setattr(subprocess, "run", answer("something else"))
    assert REAL_PROBE("x") is None

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("x", 5)
    monkeypatch.setattr(subprocess, "run", boom)
    assert REAL_PROBE("x") is None
