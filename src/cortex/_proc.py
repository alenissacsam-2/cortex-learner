"""Shared subprocess flags. The installed app (`cortex-app.exe`) has no console, and on Windows
every console child of such a process opens its own visible window unless it is started with
CREATE_NO_WINDOW: pass `creationflags=NO_WINDOW` to every subprocess (0 elsewhere)."""

import subprocess

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
