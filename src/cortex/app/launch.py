"""`cortex-app`: the no-console launcher the Windows installer's shortcut runs. It starts the
app exactly like `cortex app` (cli.app_cmd) does, with output in ~/.cortex/logs/app.log when
there is no console (a GUI script's sys.stdout and sys.stderr are None)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

LOG_MAX = 1_000_000  # bytes; a bigger log starts over


def main() -> None:
    if sys.stdout is None or sys.stderr is None:
        log = Path.home() / ".cortex" / "logs" / "app.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        big = log.is_file() and log.stat().st_size > LOG_MAX
        sys.stdout = sys.stderr = open(log, "w" if big else "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    # clients.cortex_command() finds `cortex` on PATH for the AI-app configs; a shortcut can
    # start before PATH has the installer's bin folder, and `cortex.exe` sits next to us
    here = str(Path(sys.argv[0]).resolve().parent)
    os.environ["PATH"] = here + os.pathsep + os.environ.get("PATH", "")
    from cortex import taint
    from cortex.app import server

    server.run(taint._vault_root(), echo=print)
