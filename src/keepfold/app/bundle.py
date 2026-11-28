"""Where the frozen Mac or Linux app lives: its `.app`, the `keepfold` CLI AI apps start, and
Move to Applications for a Mac app run from Downloads or the DMG (translocated), whose path is
temporary (port DESIGN.md §4.1)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from keepfold import update

CLI = "keepfold-cli"  # the Mac bundle's CLI; `Keepfold` (the GUI) differs by more than case
LINUX_CLI = Path("/usr/bin/keepfold")  # the .deb's
LINUX_APP = Path("/usr/bin/keepfold-app")  # the .deb's launcher: it runs the newest copy
APPLICATIONS = Path("/Applications")  # tests replace this


def _mac_app() -> bool:
    return sys.platform == "darwin" and update.mode() == "app"


def app_path() -> Path | None:
    """The `.app` that holds `sys.executable` (the frozen Mac app), else None."""
    if not _mac_app():
        return None
    return next((d for d in Path(sys.executable).parents if d.suffix == ".app"), None)


def translocated() -> bool:
    """The Mac app runs from a temporary path: Gatekeeper's App Translocation, or the DMG."""
    p = app_path()
    return p is not None and ("/AppTranslocation/" in p.as_posix()
                              or p.as_posix().startswith("/Volumes/"))


def cli_path() -> Path | None:
    """The app's own `keepfold` command: inside the Mac bundle, the .deb's /usr/bin/keepfold on
    Linux; None outside the frozen app."""
    if update.mode() != "app":
        return None
    if sys.platform == "darwin":
        app = app_path()
        return app / "Contents" / "MacOS" / CLI if app else None
    return LINUX_CLI


def link_local_bin() -> Path | None:
    """Mac app: `~/.local/bin/keepfold` -> cli_path(), for the terminal and the Claude Code
    plugin's bare `keepfold`. Replaces only a symlink, never a real file; None when nothing is
    linked (not the Mac app, a real file there, or the link could not be made)."""
    cli = cli_path() if sys.platform == "darwin" else None
    if cli is None:
        return None
    link = Path.home() / ".local" / "bin" / "keepfold"
    try:
        if link.is_symlink():
            if Path(os.readlink(link)) == cli:
                return link
            link.unlink()
        elif link.exists():
            return None
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(cli)
    except OSError:  # connecting AI apps goes on without the terminal link
        return None
    return link


def _ditto(src: Path, dest: Path) -> tuple[bool, str]:  # tests replace this
    from keepfold.app import targets
    return targets._run(["ditto", str(src), str(dest)])


def _open_after(dest: Path, pid: int) -> None:  # tests replace this
    """Open `dest` once process `pid` (this app) has ended; while it runs, its local server
    would answer the new copy's start and only show this window."""
    from keepfold._proc import NO_WINDOW
    subprocess.Popen(["/bin/sh", "-c", 'while kill -0 "$1" 2>/dev/null; do sleep 0.2; done; '
                      'exec open "$2"', "sh", str(pid), str(dest)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True,
                     creationflags=NO_WINDOW)


def move_to_applications() -> Path:
    """Copy the app with `ditto` to /Applications (~/Applications when that is not writable),
    and open the copy once this app quits; the caller then quits. Returns the copy's path."""
    src = app_path()
    if src is None:
        raise ValueError("Only the Mac app can move itself to Applications.")
    folder = APPLICATIONS
    if not os.access(folder, os.W_OK):
        folder = Path.home() / "Applications"
        folder.mkdir(exist_ok=True)
    dest = folder / src.name
    if dest.exists():  # never copied over: ditto would merge two versions' files
        raise ValueError(f"Keepfold is already in {folder}. Quit this copy and open that one, "
                         "or drag this one over it in Finder.")
    ok, out = _ditto(src, dest)
    if not ok:
        raise ValueError(f"Could not copy Keepfold to {folder}: {out}")
    _open_after(dest, os.getpid())
    return dest
