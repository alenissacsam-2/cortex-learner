"""Claude desktop (Chat) config files: where they live per OS, and the `keepfold` MCP server
entry. Used by `keepfold setup`, `keepfold doctor` and the app; `app.targets` writes the files."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

CONFIG = "claude_desktop_config.json"


def claude_desktop_config_paths(platform: str | None = None) -> list[Path]:
    """Candidate config files, in probe order, for each Claude folder that exists.

    Windows has three layouts (per-profile Claude-Data, classic, the Store/MSIX virtualised
    folder); callers see them all. The Store app reads its Claude-Data profile (`claude_<id>`,
    with or without a config yet), so that comes first: a first setup with no config file
    anywhere writes there, not to the classic folder the Store app left behind."""
    platform = platform or sys.platform
    out: list[Path] = []
    if platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            out += sorted(d / CONFIG for d in Path(local, "Claude-Data").glob("*/")
                          if (d / CONFIG).is_file() or d.name.lower().startswith("claude_"))
        if roaming := os.environ.get("APPDATA"):
            out.append(Path(roaming, "Claude", CONFIG))
        if local:
            out += sorted(p / CONFIG for p in Path(local, "Packages").glob(
                "Claude_*/LocalCache/Roaming/Claude"))
    elif platform == "darwin":
        out.append(Path.home() / "Library" / "Application Support" / "Claude" / CONFIG)
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        out.append(Path(base, "Claude", CONFIG))
    return [p for p in out if p.parent.is_dir()]


def setup_targets(paths: list[Path]) -> list[Path]:
    """Which candidates setup writes: every config file that already exists, else the first
    candidate folder only, so no stray configs appear in folders the app does not read."""
    return [p for p in paths if p.is_file()] or paths[:1]


def keepfold_command() -> str:
    """Absolute path of the `keepfold` executable: Claude desktop may not share the shell PATH."""
    found = shutil.which("keepfold")
    return str(Path(found or sys.argv[0]).resolve())


UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Keepfold"  # the installer's


def _registry_install() -> Path | None:  # tests replace this: never the real registry
    """The install folder Apps & Features names (installer/setup_app.py), else None."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            return Path(winreg.QueryValueEx(k, "InstallLocation")[0])
    except (ImportError, OSError):
        return None


def installed_keepfold() -> Path | None:
    """`bin\\keepfold.exe` of the KeepfoldSetup.exe install (the folder this Python runs from, else
    the one Apps & Features names, else the installer's default folder), else None."""
    from keepfold import update
    local = os.environ.get("LOCALAPPDATA")
    for d in (update.install_dir(), _registry_install(),
              Path(local, "Programs", "Keepfold") if local else None):
        if d and (d / update.MARKER).is_file() and (d / "bin" / "keepfold.exe").is_file():
            return d / "bin" / "keepfold.exe"
    return None


def mcp_command() -> str:
    """The `command` of the `keepfold` MCP entry: the installed Keepfold when there is one, so AI
    apps run the version the installer updates (not a separate `uv tool` copy); else
    `keepfold_command()`."""
    exe = installed_keepfold()
    return str(exe) if exe else keepfold_command()


def other_copy(entry) -> bool:
    """`entry` (a `keepfold` MCP entry) runs another keepfold executable while an installed Keepfold
    exists."""
    exe = installed_keepfold()
    cmd = entry.get("command") if isinstance(entry, dict) else None
    return bool(exe and isinstance(cmd, str) and cmd and Path(cmd).resolve() != exe.resolve())


def server_entry(command: str, vault: Path) -> dict:
    return {"command": command, "args": ["mcp", "--vault", str(vault)]}

