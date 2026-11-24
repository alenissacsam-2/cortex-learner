"""CortexSetup.exe: installs Cortex for the current user, no admin rights (docs/INSTALL.md).

Stdlib only. The exe bundles `payload/uv.exe` and `payload/cortex_learner-*.whl`
(installer/build.py); `uv tool install` then fetches Python 3.12 and the dependencies into the
install folder (pinned to uv.lock by `payload/constraints.txt`). `--uninstall` first takes
Cortex out of the AI apps (`cortex disconnect`, unless --keep-ai-apps), then deletes only what
install made, then the shortcuts, PATH entry, Start at login and registry key; never the vault
or ~/.cortex. `--update` (the app's Update now) updates the existing
install without questions, after `--wait-pid` (the app) exits, then reopens the app.
"""

from __future__ import annotations

import argparse
import os
import queue
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import app_exe  # beside this file; PyInstaller bundles it
import setup_progress as sp  # the window's pure parts, beside this file too

EXE = "CortexSetup.exe"
MARKER = "cortex-install.txt"  # uninstall deletes only a folder that holds this file
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Cortex"
LOG = Path(tempfile.gettempdir()) / "cortex-setup.log"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)
KEPT = ("Your vault (your notes) and your settings in ~/.cortex were kept. Delete them "
        "yourself only if you want them gone.")
ABOUT = ("Cortex is your local second brain: notes, learning and a memory your AI apps can "
         "use, all on this computer. This installs it for you only, without admin rights. "
         "It needs an internet connection once, to download Python and Cortex's parts.")


class SetupError(Exception):
    """A failure the window shows in plain words."""


def default_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "Programs" / "Cortex"


def installed_dir() -> Path:
    """Where Cortex is installed: the Apps & Features entry, else the default folder."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            p = Path(winreg.QueryValueEx(k, "InstallLocation")[0])
        if (p / MARKER).is_file():
            return p
    except (ImportError, OSError):
        pass
    return default_dir()


def payload_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "payload"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog=EXE, description="Install or uninstall Cortex.")
    p.add_argument("--quiet", action="store_true", help="No window; the log says what happened.")
    p.add_argument("--dir", type=Path, default=None, help="Install folder.")
    p.add_argument("--no-shortcuts", action="store_true")
    p.add_argument("--no-path", action="store_true")
    p.add_argument("--no-registry", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--keep-ai-apps", action="store_true",
                   help="Uninstall without removing Cortex from Claude and other AI apps.")
    p.add_argument("--update", action="store_true",
                   help="Update the existing install without questions, then open the app.")
    p.add_argument("--wait-pid", type=int, default=None,
                   help="First wait (up to 60 s) for this process to exit.")
    a = p.parse_args(argv)
    a.dir = (a.dir or (installed_dir() if a.update else default_dir())).expanduser().resolve()
    if a.update:  # the PATH entry already points at the same files; shortcuts: make_shortcuts
        a.no_path = True
    return a


def wheel_in(folder: Path) -> Path:
    wheels = sorted(folder.glob("cortex_learner-*.whl"))
    if not wheels:
        raise SetupError(f"This setup file is incomplete (no Cortex package in {folder}).")
    return wheels[-1]


def version_of(wheel: Path) -> str:
    return wheel.name.split("-")[1]


def uv_env(d: Path, base: dict | None = None) -> dict:
    """Everything uv writes stays inside the install folder; only uv's own Python is used."""
    env = dict(os.environ if base is None else base)
    env.update(UV_TOOL_DIR=str(d / "tools"), UV_TOOL_BIN_DIR=str(d / "bin"),
               UV_PYTHON_INSTALL_DIR=str(d / "python"), UV_CACHE_DIR=str(d / "cache"),
               UV_PYTHON_PREFERENCE="only-managed",
               # the Windows certificate store: school networks that inspect TLS still work
               UV_NATIVE_TLS="1",
               # compiled at install, so the first start (and Claude's first `cortex mcp`) is fast
               UV_COMPILE_BYTECODE="1")
    return env


def _same(a: str, b: str) -> bool:
    def norm(s):
        return os.path.normcase(os.path.expandvars(s.strip().strip('"')).rstrip("\\/"))
    return norm(a) == norm(b)


def merge_path(current: str, entry: str) -> str:
    """The user PATH with `entry` appended, unchanged when it is already there."""
    if any(_same(p, entry) for p in current.split(";") if p.strip()):
        return current
    return f"{current.rstrip(';')};{entry}" if current.strip(";") else entry


def drop_path(current: str, entry: str) -> str:
    return ";".join(p for p in current.split(";") if p.strip() and not _same(p, entry))


def uninstall_values(d: Path, version: str) -> dict:
    exe = d / EXE
    return {"DisplayName": "Cortex", "DisplayVersion": version, "Publisher": "Cortex",
            "InstallLocation": str(d), "DisplayIcon": str(app_exe.path(d)),
            "UninstallString": f'"{exe}" --uninstall --dir "{d}"',
            "QuietUninstallString": f'"{exe}" --uninstall --quiet --dir "{d}"'}


# ---- Windows: registry, PATH broadcast, shortcuts -------------------------------------------

# twin: src/cortex/app/winpath.py (the app's Fix puts Cortex first); a fix belongs in both
def get_user_path() -> str:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, "Path")[0]
    except FileNotFoundError:
        return ""


def set_user_path(value: str) -> None:
    import ctypes
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
        if value:
            winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, value)
        else:
            winreg.DeleteValue(k, "Path")
    # WM_SETTINGCHANGE to every window, so new terminals see the new PATH
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, None)


def register(d: Path, version: str) -> None:
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        for name, value in uninstall_values(d, version).items():
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        for name in ("NoModify", "NoRepair"):
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, 1)


def unregister() -> None:
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except FileNotFoundError:
        pass


def shortcut_paths() -> list[Path]:
    """Start menu Programs\\Cortex.lnk and Desktop\\Cortex.lnk (wherever those folders are)."""
    import ctypes
    out = []
    for csidl in (0x02, 0x10):  # CSIDL_PROGRAMS, CSIDL_DESKTOPDIRECTORY
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0:
            out.append(Path(buf.value) / "Cortex.lnk")
    return out


def make_shortcuts(d: Path, say, existing_only: bool = False) -> None:
    """The shortcuts run Cortex.exe; `existing_only` (an update) rewrites only those still
    there, so older ones that ran cortex-app.exe pin as Cortex too."""
    exe = app_exe.path(d)
    for lnk in shortcut_paths():
        if existing_only and not lnk.is_file():
            continue
        lnk.parent.mkdir(parents=True, exist_ok=True)
        app_exe.shortcut(lnk, exe, subprocess.list2cmdline(app_exe.ENTRY), exe)
        say(f"shortcut: {lnk}")


# ---- Windows: the programs that hold files in the install folder ----------------------------

def _kernel32():
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = w.HANDLE
    k.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
    k.CreateToolhelp32Snapshot.restype = w.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = (w.DWORD, w.DWORD)
    k.WaitForSingleObject.argtypes = (w.HANDLE, w.DWORD)
    k.TerminateProcess.argtypes = (w.HANDLE, ctypes.c_uint)
    k.CloseHandle.argtypes = (w.HANDLE,)
    k.QueryFullProcessImageNameW.argtypes = (w.HANDLE, w.DWORD, w.LPWSTR, w.PDWORD)
    return k


def wait_pid(pid: int, timeout: float = 60) -> None:
    """Wait for process `pid` to exit; one that is already gone is fine."""
    k = _kernel32()
    h = k.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if h:
        k.WaitForSingleObject(h, int(timeout * 1000))
        k.CloseHandle(h)


def _snapshot() -> list[tuple[int, int]]:
    """(pid, parent pid) of every process."""
    import ctypes
    from ctypes import wintypes as w

    class Entry(ctypes.Structure):  # PROCESSENTRY32W
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD),
                    ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                    ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD),
                    ("szExeFile", w.WCHAR * 260)]

    k = _kernel32()
    snap = k.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == w.HANDLE(-1).value:
        return []
    pids, e = [], Entry()
    e.dwSize = ctypes.sizeof(Entry)
    ok = k.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        pids.append((e.th32ProcessID, e.th32ParentProcessID))
        ok = k.Process32NextW(snap, ctypes.byref(e))
    k.CloseHandle(snap)
    return pids


def parents() -> dict[int, int]:
    """pid -> parent pid (the window counts cortex.exe's python.exe with it)."""
    return dict(_snapshot()) if sys.platform == "win32" else {}


def processes() -> list[tuple[int, str]]:
    """(pid, image path) of every process whose path this user may read."""
    import ctypes
    from ctypes import wintypes as w
    k = _kernel32()
    out = []
    for pid, _ in _snapshot():
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        buf, n = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            out.append((pid, buf.value))
        k.CloseHandle(h)
    return out


def in_use(d: Path, procs: list[tuple[int, str]] | None = None) -> list[tuple[int, str]]:
    """Running programs from inside `d` (they lock its files), never this setup itself: a
    one-file exe runs as two processes, the second a child of the first."""
    if procs is None:
        procs = processes() if sys.platform == "win32" else []
    root = os.path.join(os.path.normcase(str(d.resolve())), "")
    me = {os.getpid(), os.getppid()}
    return [(pid, path) for pid, path in procs
            if pid not in me and os.path.normcase(path).startswith(root)]


def terminate(pids: list[int], d: Path | None = None) -> None:
    """End `pids`; with `d`, only a process still running from inside `d`: the list is from
    before the Close dialog, and a pid freed meanwhile may now belong to another program."""
    import ctypes
    from ctypes import wintypes as w
    k = _kernel32()
    root = os.path.join(os.path.normcase(str(d.resolve())), "") if d else None
    for pid in pids:
        # SYNCHRONIZE | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
        h = k.OpenProcess(0x00101001, False, pid)
        if not h:
            continue
        buf, n = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
        same = root is None or (k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
                                and os.path.normcase(buf.value).startswith(root))
        if same:
            k.TerminateProcess(h, 1)
            k.WaitForSingleObject(h, 5000)
        k.CloseHandle(h)


def in_use_text(procs: list[tuple[int, str]]) -> str:
    n = len(procs)
    return (f"Cortex is in use by {n} program{'s' * (n != 1)} (the Cortex connection of AI apps "
            "such as Claude, or the browsing receiver).")


# ---- install and uninstall ------------------------------------------------------------------

def run(args: list, env: dict, say) -> None:
    say("running: " + subprocess.list2cmdline([str(a) for a in args]))
    p = subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                         creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace")
    for line in p.stdout:
        say(line.rstrip())
    if p.wait():
        raise SetupError(f"{Path(str(args[0])).name} stopped with exit code {p.returncode}.")


def check_dir(d: Path) -> None:
    if (d / ".cortex").is_dir():
        raise SetupError(f"{d} is a Cortex vault (your notes). Pick another folder for the "
                         "program.")
    # a folder holding only CortexSetup.exe is what an uninstall left while its window was open
    others = [p for p in d.iterdir() if p.name != EXE] if d.is_dir() else []
    if others and not (d / MARKER).is_file():
        raise SetupError(f"{d} already holds other files. Pick an empty folder, or the "
                         "folder Cortex was installed in before.")


INSTALLED = ("bin", "tools", "python", "cache", "uv.exe", "constraints.txt", EXE)


def installed_items(d: Path) -> list[Path]:
    """What install() put in `d` (uninstall deletes these and nothing else), marker aside."""
    return [p for p in d.iterdir() if p.name in INSTALLED
            or (p.name.startswith("cortex_learner-") and p.suffix == ".whl")]


def install(d: Path, opts: argparse.Namespace, say, src: Path | None = None) -> None:
    src = src or payload_dir()
    wheel = wheel_in(src)
    check_dir(d)
    if opts.update and not (d / MARKER).is_file():
        raise SetupError(f"Cortex is not installed in {d}; run {EXE} without --update.")
    if busy := in_use(d):  # uv cannot replace a running bin\cortex.exe (os error 32)
        raise SetupError(in_use_text(busy) + " Close them, then run setup again: "
                         + "; ".join(f"{path} (process {pid})" for pid, path in busy))
    d.mkdir(parents=True, exist_ok=True)
    (d / MARKER).write_text("Cortex is installed in this folder (CortexSetup.exe).\n",
                            encoding="utf-8")
    for old in d.glob("cortex_learner-*.whl"):
        if old.name != wheel.name:
            old.unlink()
    shutil.copy2(src / "uv.exe", d / "uv.exe")
    shutil.copy2(wheel, d / wheel.name)
    # uv.lock's versions (installer/build.py): the same exe installs the same packages every day
    pins = []
    if (src / "constraints.txt").is_file():
        shutil.copy2(src / "constraints.txt", d / "constraints.txt")
        pins = ["-c", d / "constraints.txt"]
    say(f"Installing Cortex {version_of(wheel)} into {d} (downloads Python and packages)...")
    try:
        run([d / "uv.exe", "tool", "install", "--force", "--reinstall-package", "cortex-learner",
             "--python", "3.12", *pins, d / wheel.name], uv_env(d), say)
    except SetupError as e:
        raise SetupError(f"{e} Check your internet connection; a school or work network may "
                         "block the download, so another Wi-Fi can help. If Cortex is open, "
                         "close it (Quit on its page) and run setup again.") from None
    shutil.rmtree(d / "cache", ignore_errors=True)
    try:
        app_exe.make(d, version_of(wheel), say)
    except OSError as e:
        raise SetupError(f"Could not make {app_exe.NAME} ({e}).") from None
    me = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False) and me != (d / EXE).resolve():
        shutil.copy2(me, d / EXE)  # Apps & Features runs this copy to uninstall
    if not opts.no_path:
        set_user_path(merge_path(get_user_path(), str(d / "bin")))
        say(f"added to your PATH: {d / 'bin'} (new terminals know `cortex`)")
    if not opts.no_shortcuts:
        try:  # a managed PC can block the shortcut object: Cortex is installed anyway
            make_shortcuts(d, say, existing_only=opts.update)
        except OSError as e:
            say(f"warning: no Start menu shortcut ({e}). Open Cortex with "
                f"{d / 'bin' / 'cortex-app.exe'}.")
    if not opts.no_registry:
        register(d, version_of(wheel))
        say("added to Apps & Features (uninstall from there)")
    say("Cortex is updated. AI apps connected to Cortex reconnect when you restart them."
        if opts.update else "Cortex is installed.")


def _force(func, path, _exc):  # uv's Python ships read-only files
    os.chmod(path, stat.S_IWRITE)
    func(path)


LOGIN_NAME = "Cortex browse receiver"  # the Run value (and the older scheduled task) the app adds
AUTOSTART_NAME = "Cortex"  # the desktop app's Start with Windows (cortex.app.desktop.AUTOSTART)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def drop_login() -> None:
    """Remove Start at login and Start with Windows: the app's HKCU Run values and the
    scheduled task older versions made (they would fail at every login once the files are
    gone)."""
    import winreg
    for name in (LOGIN_NAME, AUTOSTART_NAME):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        except FileNotFoundError:
            pass
    subprocess.run(["schtasks", "/Delete", "/F", "/TN", LOGIN_NAME], capture_output=True,
                   stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)


def disconnect_apps(d: Path, say) -> None:
    """`cortex disconnect`: the installed program takes Cortex out of every AI app it was added
    to (as Connect AI apps does), so they do not fail on Cortex at each start. A failure is a
    warning: the files still go."""
    exe = d / "bin" / "cortex.exe"
    if not exe.is_file():
        return
    say("removing Cortex from your AI apps (Claude and others)")
    try:
        run([exe, "disconnect"], child_env(), say)
    except (SetupError, OSError) as e:
        say(f"warning: some AI apps still list Cortex ({e}); remove it there by hand.")


def uninstall(d: Path, opts: argparse.Namespace, say) -> bool:
    """Remove what install() made, files first; True when this exe runs from `d` and the
    caller must `remove_later(d)` once it has said goodbye."""
    marker = d / MARKER
    if not marker.is_file():
        raise SetupError(f"{d} is not a Cortex install folder (no {MARKER}); nothing was removed.")
    if not opts.keep_ai_apps:
        disconnect_apps(d, say)
    retry = (f"then uninstall again from Installed apps (or run {EXE} --uninstall in {d}). "
             "Nothing else was removed.")
    if busy := in_use(d):
        raise SetupError(in_use_text(busy) + " Close them, " + retry + " Still running: "
                         + "; ".join(f"{path} (process {pid})" for pid, path in busy))
    me = Path(sys.executable).resolve()
    try:  # only the names install() creates: never a vault or other files kept here
        for p in installed_items(d):
            if p != me:
                shutil.rmtree(p, onexc=_force) if p.is_dir() else p.unlink()
    except OSError as e:
        raise SetupError(f"Could not remove {e.filename}: a program still uses it. Close "
                         "it, " + retry) from None
    # the files are gone: now the entries that point at them (kept until here, for a retry)
    if not opts.no_shortcuts:
        for lnk in shortcut_paths():
            lnk.unlink(missing_ok=True)
    if not opts.no_path:
        set_user_path(drop_path(get_user_path(), str(d / "bin")))
    if not opts.no_registry:
        drop_login()
        unregister()
    marker.unlink()
    later = me.parent == d
    if not later:
        try:
            d.rmdir()
        except OSError:  # files Cortex did not make stay where they are
            say(f"{d} still holds files Cortex did not make; they were kept.")
    say(f"Cortex was removed from {d}. {KEPT}")
    return later


def remove_later(d: Path) -> None:
    """A running exe cannot delete itself: cmd retries for 30 s after we exit, deleting only
    CortexSetup.exe and then the folder if it is empty (never anything else in it)."""
    exe = d / EXE
    subprocess.Popen(f'cmd /c for /l %i in (1,1,30) do @(ping -n 2 127.0.0.1 >nul & '
                     f'del /f /q "{exe}" 2>nul & if not exist "{exe}" (rmdir "{d}" 2>nul & '
                     f'exit))', creationflags=DETACHED | NO_WINDOW)


# ---- the window -----------------------------------------------------------------------------

def child_env() -> dict:
    # the app must not inherit PyInstaller's temp-folder variables (they vanish when we exit)
    return {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}


def start_app(d: Path) -> None:
    subprocess.Popen([str(app_exe.path(d)), *app_exe.ENTRY], env=child_env(),
                     creationflags=DETACHED)


def ask_close(root, title: str, busy: list, verb: str, d: Path) -> bool:
    """Ask before ending the programs that hold the folder; OK ends exactly those."""
    import tkinter as tk
    from tkinter import ttk

    dlg, ok = tk.Toplevel(root), []
    dlg.title(title)
    dlg.transient(root)
    f = ttk.Frame(dlg, padding=16)
    f.pack(fill="both", expand=True)
    ttk.Label(f, text=in_use_text(busy) + f" Close them and {verb}?", wraplength=420,
              justify="left").pack(anchor="w")
    row = ttk.Frame(f)
    row.pack(anchor="e", pady=(12, 0))
    ttk.Button(row, text=f"Close them and {verb}",
               command=lambda: (ok.append(1), dlg.destroy())).pack(side="left")
    ttk.Button(row, text="Cancel", command=dlg.destroy).pack(side="left", padx=(8, 0))
    dlg.grab_set()
    root.wait_window(dlg)
    if ok:
        terminate([pid for pid, _ in busy], d)  # install() and uninstall() check again
    return bool(ok)


# night colours from src/cortex/assets/app/app.css; rule is its amber .16 over the ground
NIGHT = {"ground": "#0b0b0a", "panel": "#141412", "band": "#1b1a17", "ink": "#efe7d6",
         "ink2": "#b9ab90", "ink3": "#958870", "lit": "#ffb000", "rule": "#322509",
         "refresh": "#d0607f", "ok": "#1D9E75"}
# the window (install has the About lines too) and the open log, in 96-dpi pixels
SIZE, DETAILS = {"update": (560, 380), "install": (560, 420)}, 190


def icon_path() -> Path | None:
    """Cortex's .ico: beside the bundled payload (build.py), else the package's own."""
    here = Path(__file__).resolve().parent
    for p in (Path(getattr(sys, "_MEIPASS", here)) / "cortex.ico",
              here.parent / "src" / "cortex" / "assets" / "cortex.ico"):
        if p.is_file():
            return p
    return None


def _dpi_aware() -> None:
    """Per-monitor DPI awareness before the Tk root, so text is sharp; older Windows falls
    back to system awareness, anything else to none."""
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def _dark_title(root) -> None:
    """A dark title bar (DWMWA_USE_IMMERSIVE_DARK_MODE: 20, 19 on early Windows 10)."""
    import ctypes
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        on = ctypes.c_int(1)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), 4) == 0:
                return
    except (AttributeError, OSError):
        pass


def _place(root, w: int, h: int) -> None:
    """Centred, a little above the middle."""
    x, y = (root.winfo_screenwidth() - w) // 2, (root.winfo_screenheight() - h) // 3
    root.geometry(f"{w}x{h}+{x}+{y}")


def _chrome(root, title: str) -> None:
    root.title(title)
    if ico := icon_path():
        try:
            root.iconbitmap(default=str(ico))  # never Tk's feather; dialogs get it too
        except Exception:  # noqa: BLE001 - a broken icon must not stop setup
            pass


def _style(root, c: dict, ui: tuple, px):
    from tkinter import ttk
    st = ttk.Style(root)
    st.theme_use("clam")  # the one built-in theme whose colours can all be set
    st.configure(".", background=c["ground"], foreground=c["ink"], font=ui,
                 bordercolor=c["rule"], focuscolor=c["ink"])
    for name, bg, fg, hover, ring in (("Lit", c["lit"], c["ground"], "#ffc23d", c["ground"]),
                                      ("Quiet", c["band"], c["ink"], "#26241f", c["ink"])):
        st.configure(f"{name}.TButton", background=bg, foreground=fg, padding=(px(16), px(6)),
                     bordercolor=bg, lightcolor=bg, darkcolor=bg, focuscolor=ring,
                     focusthickness=1, relief="flat")
        st.map(f"{name}.TButton", background=[("pressed", bg), ("active", hover)],
               lightcolor=[("active", hover)], darkcolor=[("active", hover)],
               bordercolor=[("focus", ring)])
    st.configure("Link.TButton", background=c["ground"], foreground=c["ink2"],
                 padding=(px(2), px(4)),
                 bordercolor=c["ground"], lightcolor=c["ground"], darkcolor=c["ground"],
                 focuscolor=c["ink2"], relief="flat")
    st.map("Link.TButton", foreground=[("active", c["lit"])],
           background=[("active", c["ground"])])
    st.configure("Lit.Horizontal.TProgressbar", background=c["lit"], troughcolor=c["panel"],
                 bordercolor=c["panel"], lightcolor=c["lit"], darkcolor=c["lit"],
                 arrowsize=px(6))  # clam's bar thickness
    st.configure("Failed.Horizontal.TProgressbar", background=c["refresh"],
                 troughcolor=c["panel"], bordercolor=c["panel"], lightcolor=c["refresh"],
                 darkcolor=c["refresh"], arrowsize=px(6))
    st.configure("TEntry", fieldbackground=c["panel"], foreground=c["ink"],
                 insertcolor=c["ink"], bordercolor=c["rule"], lightcolor=c["panel"],
                 darkcolor=c["panel"], padding=(px(8), px(5)), selectbackground=c["lit"],
                 selectforeground=c["ground"])
    st.map("TEntry", bordercolor=[("disabled", c["rule"]), ("focus", c["lit"])],
           fieldbackground=[("disabled", c["ground"])], foreground=[("disabled", c["ink3"])])
    st.configure("Vertical.TScrollbar", background=c["band"], troughcolor=c["panel"],
                 bordercolor=c["panel"], arrowcolor=c["ink3"], lightcolor=c["band"],
                 darkcolor=c["band"])
    # clam greys a disabled scrollbar (a short log) light: keep it dark
    st.map("Vertical.TScrollbar", background=[("disabled", c["band"]), ("active", "#26241f")],
           arrowcolor=[("disabled", c["ink3"])])
    return st


def window(opts: argparse.Namespace, say_file) -> int:
    """Install and update in one dark window: the steps, a progress bar with the time, plain
    status words, the log under Show details, and the in-use question and errors inside it."""
    import tkinter as tk
    from tkinter import ttk

    _dpi_aware()
    root = tk.Tk()
    if opts.uninstall:
        return uninstall_window(root, opts, say_file)
    c, mode = NIGHT, "update" if opts.update else "install"
    verb = "updated" if opts.update else "installed"
    try:
        version = version_of(wheel_in(payload_dir()))
    except SetupError:
        version = ""  # install() says what is missing
    _chrome(root, "Updating Cortex" if opts.update else "Cortex setup")
    scale = root.winfo_fpixels("1i") / 96

    def px(n: float) -> int:
        return round(n * scale)

    w, h = px(SIZE[mode][0]), px(SIZE[mode][1])
    wrap = px(SIZE[mode][0] - 48)
    root.configure(bg=c["ground"])
    root.resizable(False, False)
    _place(root, w, h)
    ui, small = ("Segoe UI", 10), ("Segoe UI", 9)
    _style(root, c, ui, px)

    def label(parent, text="", fg="ink", font=ui, **kw):
        kw.setdefault("anchor", "w")
        return tk.Label(parent, text=text, bg=kw.pop("bg", c["ground"]), fg=c[fg], font=font,
                        justify="left", **kw)

    main_ = tk.Frame(root, bg=c["ground"], width=w, height=h)
    main_.pack(fill="x")
    main_.pack_propagate(False)
    head = tk.Frame(main_, bg=c["ground"])
    head.pack(fill="x", padx=px(24), pady=(px(18), 0))
    label(head, "Cortex", "lit", ("Segoe UI Semibold", 13)).pack(anchor="w")
    label(head, ("Updating to " + version if version else "Updating Cortex") if opts.update
          else f"Installing {sp.cortex(version)}", "ink2").pack(anchor="w")

    foot = tk.Frame(main_, bg=c["ground"])
    foot.pack(side="bottom", fill="x", padx=px(24), pady=(0, px(18)))
    body = tk.Frame(main_, bg=c["ground"])
    body.pack(fill="both", expand=True, padx=px(24), pady=(px(14), px(10)))

    bar_row = tk.Frame(foot, bg=c["ground"])
    bar_row.pack(fill="x")
    bar = ttk.Progressbar(bar_row, style="Lit.Horizontal.TProgressbar", maximum=1000)
    bar.pack(side="left", fill="x", expand=True)
    clock = label(bar_row, "", "ink3", small, width=10, anchor="e")
    clock.pack(side="right", padx=(px(12), 0))
    status = label(foot, "", wraplength=wrap)
    status.pack(fill="x", pady=(px(6), px(10)))
    row = tk.Frame(foot, bg=c["ground"])
    row.pack(fill="x")
    toggle = ttk.Button(row, text="Show details", style="Link.TButton", takefocus=True)
    toggle.pack(side="left")
    actions = tk.Frame(row, bg=c["ground"])
    actions.pack(side="right")

    details = tk.Frame(root, bg=c["ground"])
    log = tk.Text(details, height=1, bg=c["panel"], fg=c["ink2"], font=("Consolas", 9), bd=0,
                  highlightthickness=0, wrap="word", padx=px(8), pady=px(6), state="disabled",
                  insertbackground=c["ink"], selectbackground=c["lit"],
                  selectforeground=c["ground"])
    scroll = ttk.Scrollbar(details, orient="vertical", command=log.yview)
    log.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    log.pack(side="left", fill="both", expand=True)

    def show_details(open_: bool | None = None) -> None:
        open_ = not details.winfo_ismapped() if open_ is None else open_
        if open_:
            details.pack(fill="both", expand=True, padx=px(24), pady=(0, px(18)))
        else:
            details.pack_forget()
        root.geometry(f"{w}x{h + px(DETAILS) * open_}")
        toggle.configure(text="Hide details" if open_ else "Show details")
    toggle.configure(command=show_details)

    # ---- the body: steps, or the in-use question, or the error -----------------------------
    rows: dict = {}
    steps_panel = tk.Frame(body, bg=c["ground"])
    folder = tk.StringVar(value=str(opts.dir))
    entry = ttk.Entry(steps_panel, textvariable=folder)
    if not opts.update:
        label(steps_panel, ABOUT, "ink2", small, wraplength=wrap).pack(
            fill="x", pady=(0, px(10)))
    for key, text in sp.steps(mode, version):
        r = tk.Frame(steps_panel, bg=c["ground"])
        mark = label(r, "○", "ink3", ("Segoe UI Symbol", 10), width=2)
        mark.pack(side="left")
        name = label(r, text, "ink3")
        name.pack(side="left", fill="x")
        rows[key] = (r, mark, name)
        r.pack(fill="x", pady=px(2))
        if key == "folder":
            entry.pack(fill="x", padx=(px(26), 0), pady=(px(2), px(6)))
    if opts.update:
        rows["close"][0].pack_forget()  # shown only when programs must close
    shown = {"panel": None}

    def swap(panel, expand: bool = True) -> None:
        old = shown["panel"]
        if old is panel:
            return
        if old is steps_panel:
            old.pack_forget()  # kept: it comes back
        elif old is not None:
            old.destroy()
        panel.pack(fill="both" if expand else "x", expand=expand, anchor="n")
        shown["panel"] = panel

    st = {"s": sp.start(mode, version), "failed": False, "t0": None, "t1": None}
    result = {"code": 1}
    lines: queue.Queue = queue.Queue()

    def paint() -> None:
        s, order = st["s"], list(sp.SPANS[mode])
        finished = s.fraction >= 1
        for key, (_, mark, name) in rows.items():
            i, cur = order.index(key), order.index(s.step)
            if i < cur or (i == cur and finished):
                mark.configure(text="✓", fg=c["ok"])
                name.configure(fg=c["ink2"])
            elif i == cur:
                mark.configure(text="✕" if st["failed"] else "◐",
                               fg=c["refresh"] if st["failed"] else c["lit"])
                name.configure(fg=c["ink"])
            else:
                mark.configure(text="○", fg=c["ink3"])
                name.configure(fg=c["ink3"])
        bar["value"] = s.fraction * 1000
        if not st["failed"]:
            status.configure(text=s.status)

    def tick() -> None:
        if st["t0"] is not None:
            clock.configure(text=sp.elapsed((st["t1"] or time.monotonic()) - st["t0"]))
        root.after(500, tick)

    def say(line: str) -> None:
        say_file(line)
        lines.put(line)

    def drain() -> None:
        while not lines.empty():
            line = lines.get()
            log.configure(state="normal")
            log.insert("end", line + "\n")
            log.see("end")
            log.configure(state="disabled")
            st["s"] = sp.progress(st["s"], line)
        paint()

    def pump() -> None:
        drain()
        root.after(100, pump)

    keys = {"enter": None, "esc": None}

    def buttons(items: list, esc=None) -> dict:
        """(text, command, lit) in order; Enter runs the lit one, Esc `esc`."""
        for b in actions.winfo_children():
            b.destroy()
        keys.update(enter=None, esc=esc)
        made = {}
        for text, cmd, lit in items:
            b = made[text] = ttk.Button(actions, text=text, command=cmd,
                                        style="Lit.TButton" if lit else "Quiet.TButton")
            b.pack(side="left", padx=(px(8), 0))
            if lit:
                keys["enter"] = cmd
                b.focus_set()
        return made

    root.bind("<Return>", lambda _e: keys["enter"] and keys["enter"]())
    root.bind("<Escape>", lambda _e: keys["esc"] and keys["esc"]())

    def run_in_thread(fn, then) -> None:
        def go():
            try:
                out = fn()
            except Exception as e:  # noqa: BLE001 - a failed wait or close: the error panel
                say_file(traceback.format_exc())
                root.after(0, finished, f"Something went wrong: {e}")
                return
            root.after(0, then, out)
        threading.Thread(target=go, daemon=True).start()

    # ---- the flow -------------------------------------------------------------------------
    def begin() -> None:
        if not opts.update:
            opts.dir = Path(folder.get()).expanduser().resolve()
            entry.configure(state="disabled")
            root.focus_set()  # off the field, so its amber focus ring goes
        st.update(failed=False, t0=st["t0"] or time.monotonic(), t1=None)
        swap(steps_panel)
        buttons([])
        if opts.update and opts.wait_pid and not st.get("waited"):
            st["waited"] = True
            say(f"waiting for process {opts.wait_pid} (the Cortex app) to exit")
            run_in_thread(wait_app, lambda _: check_busy())
        else:
            check_busy()

    def wait_app() -> None:
        wait_pid(opts.wait_pid)
        for _ in range(10):  # its launcher, bin\cortex-app.exe, exits a moment later
            if not in_use(opts.dir):
                break
            time.sleep(0.3)

    def check_busy() -> None:
        if busy := in_use(opts.dir):
            ask_busy(busy)
        else:
            go()

    def ask_busy(busy: list) -> None:
        panel = tk.Frame(body, bg=c["ground"])
        label(panel, "Cortex is in use. These programs must close first:").pack(fill="x")
        for group in sp.group_in_use(busy, parents()):
            label(panel, group, "ink2").pack(fill="x", padx=(px(16), 0), pady=(px(4), 0))
        swap(panel)
        st["t1"] = time.monotonic()
        st["s"] = st["s"]._replace(status="Nothing is closed until you choose.")

        def close_them() -> None:
            st["t1"] = None
            swap(steps_panel)
            if "close" in rows:
                rows["close"][0].pack(fill="x", pady=px(2), before=rows["install"][0])
            buttons([])
            n = len(busy)
            say(f"closing {n} program{'s' * (n != 1)} that use Cortex: "
                + "; ".join(f"{path} (process {pid})" for pid, path in busy))
            # only those still running from the folder; install() checks again
            run_in_thread(lambda: terminate([pid for pid, _ in busy], opts.dir),
                          lambda _: go())

        def cancel() -> None:
            swap(steps_panel)
            st["s"] = st["s"]._replace(status=f"Cortex was not {verb}.")
            paint()
            if not opts.update:
                entry.configure(state="normal")
            buttons([("Update" if opts.update else "Install", begin, True),
                     ("Close", root.destroy, False)], esc=root.destroy)

        buttons([("Close them and continue", close_them, True), ("Cancel", cancel, False)],
                esc=cancel)

    def go() -> None:
        def work():
            try:
                install(opts.dir, opts, say)
            except SetupError as e:
                return str(e)
            except Exception as e:  # noqa: BLE001
                say_file(traceback.format_exc())
                return f"Something went wrong: {e}"
            return None
        run_in_thread(work, finished)

    def finished(err: str | None) -> None:
        drain()  # the lines install() said before it returned
        st["t1"] = time.monotonic()
        if err:
            return failed(err)
        result["code"] = 0
        if opts.update:
            start_app(opts.dir)
            say("opened the Cortex app")
            buttons([("Close", root.destroy, True)], esc=root.destroy)
            root.after(3000, root.destroy)
        else:
            buttons([("Open Cortex", open_app, True), ("Close", root.destroy, False)],
                    esc=root.destroy)

    def failed(err: str) -> None:
        st["failed"] = True
        paint()
        status.configure(text="")
        panel = tk.Frame(body, bg=c["panel"])
        tk.Frame(panel, bg=c["refresh"], width=px(3)).pack(side="left", fill="y")
        inner = tk.Frame(panel, bg=c["panel"])
        inner.pack(side="left", fill="both", expand=True, padx=px(14), pady=px(12))
        label(inner, f"Cortex was not {verb}.", "refresh", ("Segoe UI Semibold", 10),
              bg=c["panel"]).pack(fill="x")
        label(inner, err, "ink", bg=c["panel"], wraplength=wrap - px(34)).pack(
            fill="x", pady=(px(4), 0))
        label(inner, f"The full log is at {LOG}", "ink3", small, bg=c["panel"],
              wraplength=wrap - px(34)).pack(fill="x", pady=(px(6), 0))
        swap(panel, expand=False)
        bar.configure(style="Failed.Horizontal.TProgressbar")
        made: dict = {}

        def copy_log() -> None:
            try:
                text = LOG.read_text(encoding="utf-8")
            except OSError:
                text = log.get("1.0", "end")
            root.clipboard_clear()
            root.clipboard_append(text)
            made["Copy log"].configure(text="Copied")

        def again() -> None:  # a folder that cannot hold Cortex: pick another one
            entry.configure(state="normal")
            st["s"] = sp.start(mode, version)
            st.update(failed=False, t0=None, t1=None)
            clock.configure(text="")
            bar.configure(style="Lit.Horizontal.TProgressbar")
            swap(steps_panel)
            paint()
            buttons([("Install", begin, True), ("Cancel", root.destroy, False)],
                    esc=root.destroy)
            entry.focus_set()

        items = [("Try again", again, True)] if not opts.update else []
        items += [("Copy log", copy_log, False), ("Close", root.destroy, opts.update)]
        made.update(buttons(items, esc=root.destroy))

    def open_app() -> None:
        start_app(opts.dir)
        root.destroy()

    swap(steps_panel)
    paint()
    if opts.update:
        root.after(200, begin)
    else:
        buttons([("Install", begin, True), ("Cancel", root.destroy, False)], esc=root.destroy)
        entry.focus_set()
        entry.icursor("end")
    _dark_title(root)
    pump()
    tick()
    root.mainloop()
    return result["code"]


def uninstall_window(root, opts: argparse.Namespace, say_file) -> int:
    """Uninstall keeps Windows' own questions (message boxes), with Cortex's icon."""
    from tkinter import messagebox

    root.withdraw()
    _chrome(root, "Uninstall Cortex")
    t = "Uninstall Cortex"
    if not messagebox.askyesno(t, f"Remove Cortex from {opts.dir}?\n\n"
                               "Your vault (your notes) and your settings in ~/.cortex "
                               "are kept."):
        return 1
    if not opts.keep_ai_apps:
        opts.keep_ai_apps = not messagebox.askyesno(
            t, "Also remove Cortex from Claude and other AI apps?\n\nYes is best: without "
            "Cortex installed, they would show a Cortex error each time they start.")
    try:
        if (busy := in_use(opts.dir)) and not ask_close(root, t, busy, "uninstall", opts.dir):
            return 1
        later = uninstall(opts.dir, opts, say_file)
    except Exception as e:  # noqa: BLE001
        say_file(traceback.format_exc())
        messagebox.showerror(t, f"{e}\n\nThe full log is at {LOG}")
        return 1
    messagebox.showinfo(t, f"Cortex was removed.\n\n{KEPT}")
    if later:  # after the message: this exe exits right away, so cmd can delete it
        remove_later(opts.dir)
    return 0


def main(argv: list[str] | None = None) -> int:
    opts = parse_args(argv)
    logf = open(LOG, "w", encoding="utf-8", buffering=1)  # noqa: SIM115

    def say_file(line: str) -> None:
        logf.write(line + "\n")
        if sys.stdout is not None:
            print(line)

    if opts.wait_pid and (opts.quiet or not opts.update):  # the update window waits itself
        say_file(f"waiting for process {opts.wait_pid} (the Cortex app) to exit")
        wait_pid(opts.wait_pid)
        for _ in range(10):  # its launcher, bin\cortex-app.exe, exits a moment later
            if not in_use(opts.dir):
                break
            time.sleep(0.3)
    if not opts.quiet:
        return window(opts, say_file)
    try:
        if opts.uninstall:
            if uninstall(opts.dir, opts, say_file):
                remove_later(opts.dir)
            return 0
        install(opts.dir, opts, say_file)
        if opts.update:
            start_app(opts.dir)
        return 0
    except Exception as e:  # noqa: BLE001
        say_file(traceback.format_exc() if not isinstance(e, SetupError) else f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
