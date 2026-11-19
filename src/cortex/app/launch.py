"""The no-console entry points of the Windows install (gui-scripts in pyproject.toml).

`cortex-app` is what the installer's shortcut runs: the desktop app (desktop.py: its own window
and a tray icon; `--hidden`, as Start with Windows runs it, starts in the tray), or, when one
already runs (~/.cortex/app.json), that app's window brought up. `cortex-receiver` is Start at
login's browse receiver, as `cortex browse serve` runs it. With no console (a GUI script's
sys.stdout and sys.stderr are None) their output goes to ~/.cortex/logs/, and a failed app
start says so in a message box with the log's path."""

from __future__ import annotations

import os
import re
import sys
import traceback
import webbrowser
from pathlib import Path

LOG_MAX = 1_000_000  # bytes; a bigger log starts over

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
        # can start before PATH has the installer's bin folder, and `cortex.exe` sits next to us
        here = str(Path(sys.argv[0]).resolve().parent)
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
    """`cortex-receiver [--vault V] [--port N]`: `cortex browse serve` with no console."""
    _log_to("receiver.log")
    from cortex import cli
    cli.app(["browse", "serve", *sys.argv[1:]], prog_name="cortex")
