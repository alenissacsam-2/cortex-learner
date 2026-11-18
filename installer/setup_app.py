"""CortexSetup.exe: installs Cortex for the current user, no admin rights (docs/INSTALL.md).

Stdlib only. The exe bundles `payload/uv.exe` and `payload/cortex_learner-*.whl`
(installer/build.py); `uv tool install` then fetches Python 3.12 and the dependencies into the
install folder. `--uninstall` removes the install folder, shortcuts, PATH entry and registry
key, and never the vault or ~/.cortex. `--update` (the app's Update now) updates the existing
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
    p.add_argument("--update", action="store_true",
                   help="Update the existing install without questions, then open the app.")
    p.add_argument("--wait-pid", type=int, default=None,
                   help="First wait (up to 60 s) for this process to exit.")
    a = p.parse_args(argv)
    a.dir = (a.dir or (installed_dir() if a.update else default_dir())).expanduser().resolve()
    if a.update:  # the shortcuts and the PATH entry already point at the same files
        a.no_shortcuts = a.no_path = True
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
               UV_PYTHON_PREFERENCE="only-managed")
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
            "InstallLocation": str(d), "DisplayIcon": str(d / "bin" / "cortex-app.exe"),
            "UninstallString": f'"{exe}" --uninstall --dir "{d}"',
            "QuietUninstallString": f'"{exe}" --uninstall --quiet --dir "{d}"'}


# ---- Windows: registry, PATH broadcast, shortcuts -------------------------------------------

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


SHORTCUT_PS = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:CORTEX_LNK); "
               "$s.TargetPath = $env:CORTEX_TARGET; $s.WorkingDirectory = $env:USERPROFILE; "
               "$s.Description = 'Open the Cortex app'; $s.Save()")


def make_shortcuts(target: Path, say) -> None:
    for lnk in shortcut_paths():
        lnk.parent.mkdir(parents=True, exist_ok=True)
        run(["powershell", "-NoProfile", "-NonInteractive", "-Command", SHORTCUT_PS],
            {**os.environ, "CORTEX_LNK": str(lnk), "CORTEX_TARGET": str(target)}, say)
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


def processes() -> list[tuple[int, str]]:
    """(pid, image path) of every process whose path this user may read."""
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
        pids.append(e.th32ProcessID)
        ok = k.Process32NextW(snap, ctypes.byref(e))
    k.CloseHandle(snap)
    out = []
    for pid in pids:
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


def terminate(pids: list[int]) -> None:
    k = _kernel32()
    for pid in pids:
        h = k.OpenProcess(0x00100001, False, pid)  # SYNCHRONIZE | PROCESS_TERMINATE
        if h:
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
    if d.is_dir() and any(d.iterdir()) and not (d / MARKER).is_file():
        raise SetupError(f"{d} already holds other files. Pick an empty folder, or the "
                         "folder Cortex was installed in before.")


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
    say(f"Installing Cortex {version_of(wheel)} into {d} (downloads Python and packages)...")
    try:
        run([d / "uv.exe", "tool", "install", "--force", "--reinstall-package", "cortex-learner",
             "--python", "3.12", d / wheel.name], uv_env(d), say)
    except SetupError as e:
        raise SetupError(f"{e} Check your internet connection. If Cortex is open, close it "
                         "(Quit on its page) and run setup again.") from None
    shutil.rmtree(d / "cache", ignore_errors=True)
    me = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False) and me != (d / EXE).resolve():
        shutil.copy2(me, d / EXE)  # Apps & Features runs this copy to uninstall
    if not opts.no_path:
        set_user_path(merge_path(get_user_path(), str(d / "bin")))
        say(f"added to your PATH: {d / 'bin'} (new terminals know `cortex`)")
    if not opts.no_shortcuts:
        make_shortcuts(d / "bin" / "cortex-app.exe", say)
    if not opts.no_registry:
        register(d, version_of(wheel))
        say("added to Apps & Features (uninstall from there)")
    say("Cortex is updated. AI apps connected to Cortex reconnect when you restart them."
        if opts.update else "Cortex is installed.")


def _force(func, path, _exc):  # uv's Python ships read-only files
    os.chmod(path, stat.S_IWRITE)
    func(path)


def uninstall(d: Path, opts: argparse.Namespace, say) -> None:
    marker = d / MARKER
    if not marker.is_file():
        raise SetupError(f"{d} is not a Cortex install folder (no {MARKER}); nothing was removed.")
    if not opts.no_shortcuts:
        for lnk in shortcut_paths():
            lnk.unlink(missing_ok=True)
    if not opts.no_path:
        set_user_path(drop_path(get_user_path(), str(d / "bin")))
    if not opts.no_registry:
        unregister()
    me = Path(sys.executable).resolve()
    try:
        for p in d.iterdir():
            if p in (me, marker):
                continue
            shutil.rmtree(p, onexc=_force) if p.is_dir() else p.unlink()
    except OSError as e:
        raise SetupError(f"Could not remove {e.filename}. If Cortex is open, close it (Quit on "
                         "its page) and uninstall again.") from None
    marker.unlink()
    if me.parent == d:  # a running exe cannot delete itself: cmd removes the folder after we exit
        subprocess.Popen(f'cmd /c ping -n 4 127.0.0.1 >nul & rmdir /s /q "{d}"',
                         creationflags=DETACHED | NO_WINDOW)
    else:
        d.rmdir()
    say(f"Cortex was removed from {d}. {KEPT}")


# ---- the window -----------------------------------------------------------------------------

def child_env() -> dict:
    # the app must not inherit PyInstaller's temp-folder variables (they vanish when we exit)
    return {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}


def start_app(d: Path) -> None:
    subprocess.Popen([str(d / "bin" / "cortex-app.exe")], env=child_env(),
                     creationflags=DETACHED)


def window(opts: argparse.Namespace, say_file) -> int:
    import tkinter as tk
    from tkinter import messagebox, scrolledtext, ttk

    root = tk.Tk()
    if opts.uninstall:
        root.withdraw()
        if not messagebox.askyesno("Uninstall Cortex", f"Remove Cortex from {opts.dir}?\n\n"
                                   "Your vault (your notes) and your settings in ~/.cortex "
                                   "are kept."):
            return 1
        try:
            uninstall(opts.dir, opts, say_file)
        except Exception as e:  # noqa: BLE001
            say_file(traceback.format_exc())
            messagebox.showerror("Uninstall Cortex", f"{e}\n\nThe full log is at {LOG}")
            return 1
        messagebox.showinfo("Uninstall Cortex", f"Cortex was removed.\n\n{KEPT}")
        return 0

    title, verb = ("Updating Cortex", "updated") if opts.update else ("Install Cortex", "installed")
    root.title(title)
    root.minsize(560, 420)
    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)
    folder = tk.StringVar(value=str(opts.dir))
    entry = ttk.Entry(frame, textvariable=folder)
    if not opts.update:  # an update goes into the existing folder, without questions
        ttk.Label(frame, text=ABOUT, wraplength=520, justify="left").pack(anchor="w")
        ttk.Label(frame, text="Install folder:").pack(anchor="w", pady=(12, 2))
        entry.pack(fill="x")
    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", pady=8)
    status = ttk.Label(frame, text="")
    status.pack(anchor="w")
    log = scrolledtext.ScrolledText(frame, height=12, state="disabled")
    log.pack(fill="both", expand=True, pady=(4, 0))
    lines: queue.Queue = queue.Queue()
    result = {"code": 1}

    def say(line: str) -> None:
        say_file(line)
        lines.put(line)

    def pump() -> None:
        while not lines.empty():
            log.configure(state="normal")
            log.insert("end", lines.get() + "\n")
            log.see("end")
            log.configure(state="disabled")
        root.after(100, pump)

    def done(err: str | None) -> None:
        if err:
            status.configure(text=f"Cortex was not {verb}.")
            install_btn.configure(state="normal")
            entry.configure(state="normal")
            messagebox.showerror(title, f"{err}\n\nThe full log is at {LOG}")
            return
        result["code"] = 0
        if opts.update:
            open_app()
            return
        status.configure(text="Cortex is installed. Open it to finish setup.")
        install_btn.pack_forget()
        ttk.Button(buttons, text="Open Cortex", command=open_app).pack(side="left")
        ttk.Button(buttons, text="Close", command=root.destroy).pack(side="left", padx=8)

    def work(d: Path) -> None:
        err = None
        try:
            install(d, opts, say)
        except SetupError as e:
            err = str(e)
        except Exception as e:  # noqa: BLE001
            say_file(traceback.format_exc())
            err = f"Something went wrong: {e}"
        root.after(0, done, err)

    def close_them(busy: list) -> bool:
        """Ask before ending the programs that hold the folder; OK ends exactly those."""
        dlg, ok = tk.Toplevel(root), []
        dlg.title(title)
        dlg.transient(root)
        f = ttk.Frame(dlg, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=in_use_text(busy) + " Close them and update?", wraplength=420,
                  justify="left").pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(anchor="e", pady=(12, 0))
        ttk.Button(row, text="Close them and update",
                   command=lambda: (ok.append(1), dlg.destroy())).pack(side="left")
        ttk.Button(row, text="Cancel", command=dlg.destroy).pack(side="left", padx=(8, 0))
        dlg.grab_set()
        root.wait_window(dlg)
        if ok:
            terminate([pid for pid, _ in busy])  # install() checks again and names any left
        return bool(ok)

    def start() -> None:
        if not opts.update:
            opts.dir = Path(folder.get()).expanduser().resolve()
        if (busy := in_use(opts.dir)) and not close_them(busy):
            status.configure(text=f"Cortex was not {verb}.")
            return
        install_btn.configure(state="disabled")
        entry.configure(state="disabled")
        status.configure(text=("Updating" if opts.update else "Installing")
                         + "... this can take a few minutes.")
        threading.Thread(target=work, args=(opts.dir,), daemon=True).start()

    def open_app() -> None:
        start_app(opts.dir)
        root.destroy()

    install_btn = ttk.Button(buttons, text="Update" if opts.update else "Install", command=start)
    install_btn.pack(side="left")
    install_btn.focus_set()
    root.bind("<Return>", lambda _e: str(install_btn["state"]) == "normal" and start())
    if opts.update:
        root.after(200, start)
    pump()
    root.mainloop()
    return result["code"]


def main(argv: list[str] | None = None) -> int:
    opts = parse_args(argv)
    logf = open(LOG, "w", encoding="utf-8", buffering=1)  # noqa: SIM115

    def say_file(line: str) -> None:
        logf.write(line + "\n")
        if sys.stdout is not None:
            print(line)

    if opts.wait_pid:
        say_file(f"waiting for process {opts.wait_pid} (the Cortex app) to exit")
        wait_pid(opts.wait_pid)
        for _ in range(10):  # its launcher, bin\cortex-app.exe, exits a moment later
            if not in_use(opts.dir):
                break
            time.sleep(0.3)
    if not opts.quiet:
        return window(opts, say_file)
    try:
        (uninstall if opts.uninstall else install)(opts.dir, opts, say_file)
        if opts.update:
            start_app(opts.dir)
        return 0
    except Exception as e:  # noqa: BLE001
        say_file(traceback.format_exc() if not isinstance(e, SetupError) else f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
