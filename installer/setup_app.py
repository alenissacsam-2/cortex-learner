"""CortexSetup.exe: installs Cortex for the current user, no admin rights (docs/INSTALL.md).

Stdlib only. The exe bundles `payload/uv.exe` and `payload/cortex_learner-*.whl`
(installer/build.py); `uv tool install` then fetches Python 3.12 and the dependencies into the
install folder. `--uninstall` removes the install folder, shortcuts, PATH entry and registry
key, and never the vault or ~/.cortex.
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
    a = p.parse_args(argv)
    a.dir = (a.dir or default_dir()).expanduser().resolve()
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
    say("Cortex is installed.")


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

    root.title("Install Cortex")
    root.minsize(560, 420)
    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=ABOUT, wraplength=520, justify="left").pack(anchor="w")
    ttk.Label(frame, text="Install folder:").pack(anchor="w", pady=(12, 2))
    folder = tk.StringVar(value=str(opts.dir))
    entry = ttk.Entry(frame, textvariable=folder)
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
            status.configure(text="Cortex was not installed.")
            install_btn.configure(state="normal")
            entry.configure(state="normal")
            messagebox.showerror("Install Cortex", f"{err}\n\nThe full log is at {LOG}")
            return
        result["code"] = 0
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

    def start() -> None:
        opts.dir = Path(folder.get()).expanduser().resolve()
        install_btn.configure(state="disabled")
        entry.configure(state="disabled")
        status.configure(text="Installing... this can take a few minutes.")
        threading.Thread(target=work, args=(opts.dir,), daemon=True).start()

    def open_app() -> None:
        subprocess.Popen([str(opts.dir / "bin" / "cortex-app.exe")], env=child_env(),
                         creationflags=DETACHED)
        root.destroy()

    install_btn = ttk.Button(buttons, text="Install", command=start)
    install_btn.pack(side="left")
    install_btn.focus_set()
    root.bind("<Return>", lambda _e: str(install_btn["state"]) == "normal" and start())
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

    if not opts.quiet:
        return window(opts, say_file)
    try:
        (uninstall if opts.uninstall else install)(opts.dir, opts, say_file)
        return 0
    except Exception as e:  # noqa: BLE001
        say_file(traceback.format_exc() if not isinstance(e, SetupError) else f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
