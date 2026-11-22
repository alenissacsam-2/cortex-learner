"""The Windows PATH in the registry: the system PATH (read only) and the user PATH
(HKCU\\Environment `Path`), which Status's Claude Code Fix rewrites with the installed Cortex's
bin folder first. Twin of installer/setup_app.py's `_same`/`get_user_path`/`set_user_path`
(the installer does not import cortex code); a fix to one belongs in both."""

from __future__ import annotations

import os

SYSTEM_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"


def _same(a: str, b: str) -> bool:
    def norm(s):
        return os.path.normcase(os.path.expandvars(s.strip().strip('"')).rstrip("\\/"))
    return norm(a) == norm(b)


def first(current: str, entry: str) -> str:
    """`current` (a `;` PATH) with `entry` first: every other entry kept in order, no copy of
    `entry` left behind, empty entries dropped."""
    return ";".join([entry, *(p for p in current.split(";") if p.strip() and not _same(p, entry))])


def in_system(folder: str) -> bool:
    """`folder` is on the system PATH, which Windows puts before the user PATH."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, SYSTEM_KEY) as k:
            value = str(winreg.QueryValueEx(k, "Path")[0])
    except OSError:
        return False
    return any(_same(p, folder) for p in value.split(";") if p.strip())


def put_first(folder: str) -> None:
    """The user PATH with `folder` first (its registry type kept), then the change announced."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
        try:
            value, kind = winreg.QueryValueEx(k, "Path")
        except FileNotFoundError:
            value, kind = "", winreg.REG_EXPAND_SZ
        if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            kind = winreg.REG_EXPAND_SZ
        winreg.SetValueEx(k, "Path", 0, kind, first(str(value), folder))
    broadcast()


def broadcast() -> None:  # tests replace this
    """WM_SETTINGCHANGE "Environment" to every window, so programs started next see the PATH."""
    import ctypes
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, None)
