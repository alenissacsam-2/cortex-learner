"""The no-console entry points of the Windows install (gui-scripts in pyproject.toml).

The installer's shortcut runs `Cortex.exe -m cortex.app.launch` (installer/app_exe.py: a copy
of pythonw named and drawn as Cortex, so Windows shows Cortex, not Python); `cortex-app` runs
the same, handing over to that Cortex.exe when it sits beside its Python. It is the desktop
app (desktop.py: its own window and a tray icon; `--hidden`, as Start with Windows runs it,
starts in the tray), or, when one already runs (~/.cortex/app.json), that app's window
brought up. `cortex-receiver` is Start at login's browse receiver (the background history
import), as `cortex browse serve` runs it. With no console (a GUI script's sys.stdout and
sys.stderr are None) their output goes to ~/.cortex/logs/, and a failed app start says so in a
message box with the log's path."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import traceback
import webbrowser
from pathlib import Path

LOG_MAX = 1_000_000  # bytes; a bigger log starts over
APP_EXE = "Cortex.exe"  # installer/app_exe.py NAME


def installed_app() -> Path | None:
    """The install's Cortex.exe (the tool venv's app\\, installer/app_exe.py), else None (a
    dev install)."""
    exe = Path(sys.prefix) / "app" / APP_EXE
    return exe if exe.is_file() else None


_KEY = re.compile(r"\?t=[\w-]+")


def _log_to(name: str) -> Path:
    log = Path.home() / ".cortex" / "logs" / name
    if sys.stdout is None or sys.stderr is None:
        log.parent.mkdir(parents=True, exist_ok=True)
        big = log.is_file() and log.stat().st_size > LOG_MAX
        sys.stdout = sys.stderr = open(log, "w" if big else "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    return log


def _alert(text: str) -> None:  # tests replace this
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Cortex", 0x10)  # MB_ICONERROR
    else:
        print(text, file=sys.stderr)


def main() -> None:
    log = None
    hidden = "--hidden" in sys.argv[1:]
    try:
        log = _log_to("app.log")
        exe = installed_app()
        if exe and Path(sys.executable).resolve() != exe.resolve():
            # an older shortcut or Start with Windows value: the window must belong to Cortex.exe
            from cortex._proc import NO_WINDOW
            subprocess.Popen([str(exe), "-m", "cortex.app.launch", *sys.argv[1:]],
                             creationflags=NO_WINDOW)
            return
        from cortex.app import server
        if url := server.running_url():  # double-clicked again: the open app, not a second one
            if hidden:  # Start with Windows while it runs already
                return
            print("Cortex app: already running; showing it")
            if sys.platform == "win32":  # the running app may bring its window to the front
                import ctypes
                ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
            if not server.show_window(url):  # the browser app has no window
                webbrowser.open(url)
            return
        # clients.cortex_command() finds `cortex` on PATH for the AI-app configs; a shortcut
        # can start before PATH has the installer's bin folder (with `cortex.exe`), next to the
        # cortex-app.exe that started us in a dev install
        from cortex import update
        d = update.install_dir()
        here = str(d / "bin") if d else str(Path(sys.argv[0]).resolve().parent)
        os.environ["PATH"] = here + os.pathsep + os.environ.get("PATH", "")
        from cortex import taint
        from cortex.app import desktop
        # the log keeps the address, never the key (?t=): the page holds it, the log may be shared
        desktop.run(taint._vault_root(), hidden=hidden, echo=lambda s: print(_KEY.sub("", s)))
    except Exception:  # noqa: BLE001  (a double-click must never fail silently)
        traceback.print_exc()
        _alert("Cortex could not start.\n\nWhat went wrong is written in "
               f"{log or Path.home() / '.cortex' / 'logs' / 'app.log'}")
        raise SystemExit(1) from None


def receiver() -> None:
    """`cortex-receiver [--vault V]`: `cortex browse serve` (the background import) with no
    console."""
    _log_to("receiver.log")
    from cortex import cli
    cli.app(["browse", "serve", *sys.argv[1:]], prog_name="cortex")


if __name__ == "__main__":  # Cortex.exe -m cortex.app.launch [--hidden]
    main()
