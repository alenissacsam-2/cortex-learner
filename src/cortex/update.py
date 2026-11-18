"""Check for updates and fetch the new installer, only when the user clicks (docs/INSTALL.md).

Stdlib only. `latest` asks GitHub for the latest release of `cli.REPO` and sends nothing about
the user (a generic User-Agent). `download` takes only that repo's release assets, follows
redirects only to GitHub's download hosts, caps the size and checks the SHA-256 that the release
publishes. `launch` starts the new CortexSetup.exe with `--update`; it waits for the app to exit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from importlib.metadata import version as _dist_version
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit

from cortex.cli import CHECKOUT, REPO

API = "https://api.github.com"
EXE = "CortexSetup.exe"
MARKER = "cortex-install.txt"  # installer/setup_app.py drops it in the install folder
MAX_BYTES = 200 * 2**20
TIMEOUT = 10
DOWNLOADS = f"https://github.com/{REPO}/releases/download/"
_VERSION = re.compile(r"v?(\d+(?:\.\d+){1,3})")
_REDIRECTS = (301, 302, 303, 307, 308)


class UpdateError(ValueError):
    """A failure the app shows in plain words (a ValueError: the app's jobs show its text)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # a 3xx then raises HTTPError; _get checks the target
        return None


_opener = urllib.request.build_opener(_NoRedirect).open


def parse_version(tag) -> tuple[int, ...]:
    m = _VERSION.fullmatch(tag.strip()) if isinstance(tag, str) else None
    if not m:  # pre-releases (v1.0.0-rc1) and odd tags are not offered
        raise UpdateError(f"{tag!r} is not a release version Cortex can update to.")
    return tuple(int(x) for x in m[1].split("."))


def newer(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
    """a > b, with 0.2 == 0.2.0."""
    return a + (0,) * (4 - len(a)) > b + (0,) * (4 - len(b))


def current() -> str:
    return _dist_version("cortex-learner")


def install_dir(exe: str | None = None) -> Path | None:
    """The CortexSetup.exe install folder this Python runs from, or None (a dev install)."""
    p = Path(exe or sys.executable).resolve()
    return next((d for d in p.parents if (d / MARKER).is_file()), None)


def mode() -> str:
    return "installer" if install_dir() else "dev"


def command(tag: str) -> str:
    """What a dev install runs to update, as docs/INSTALL.md says (a clone, else from git)."""
    if (CHECKOUT / ".git").exists():
        return f'cd "{CHECKOUT}"; git pull; uv tool install --editable . --reinstall'
    return f"uv tool install --force git+https://github.com/{REPO}@{tag}"


def _get(url: str, opener, accept: str):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "cortex-updater"})
    return (opener or _opener)(req, timeout=TIMEOUT)


def latest(opener=None, api: str = API) -> dict:
    try:
        with _get(f"{api}/repos/{REPO}/releases/latest", opener,
                  "application/vnd.github+json") as r:
            data = json.loads(r.read(2**20))
    except HTTPError as e:
        if e.code in (403, 429):
            raise UpdateError("GitHub is limiting update checks from this network right now. "
                              "Try again in an hour.") from None
        if e.code == 404:
            raise UpdateError("GitHub has no Cortex release yet.") from None
        raise UpdateError(f"GitHub answered {e.code}. Try again later.") from None
    except (URLError, OSError) as e:
        raise UpdateError("Could not reach GitHub. Check your internet connection and try "
                          f"again ({getattr(e, 'reason', e)}).") from None
    except ValueError:
        raise UpdateError("GitHub sent an answer Cortex could not read. Try again later.") \
            from None
    if not isinstance(data, dict):
        raise UpdateError("GitHub sent an answer Cortex could not read. Try again later.")
    tag = data.get("tag_name")
    if data.get("prerelease") or data.get("draft"):
        raise UpdateError(f"The newest release ({tag}) is a pre-release; Cortex only updates "
                          "to full releases.")
    ver = parse_version(tag)
    notes = f"https://github.com/{REPO}/releases/tag/{tag}"  # built, never taken from the answer
    assets = {a.get("name"): a for a in data.get("assets") or [] if isinstance(a, dict)}
    exe, sha = assets.get(EXE), assets.get(EXE + ".sha256")
    if not exe or not sha:
        raise UpdateError(f"Cortex {tag} has no {EXE} with its .sha256 on GitHub, so it cannot "
                          f"be installed from here. See {notes}")
    return {"tag": tag, "version": ver, "notes_url": notes,
            "exe_url": exe.get("browser_download_url"), "sha_url": sha.get("browser_download_url"),
            "exe_size": int(exe.get("size") or 0)}


def dest_dir(info: dict) -> Path:
    return Path.home() / ".cortex" / "tmp" / f"update-{'.'.join(map(str, info['version']))}"


def _fetch(url: str, opener):
    """Open a release asset, following redirects only to GitHub's https download hosts."""
    if not isinstance(url, str) or not url.startswith(DOWNLOADS):
        raise UpdateError(f"Refused a download that is not a Cortex release file: {url}")
    for _ in range(5):
        try:
            return _get(url, opener, "application/octet-stream")
        except HTTPError as e:
            if e.code not in _REDIRECTS:
                raise UpdateError(f"GitHub answered {e.code} for {url}. Try again later.") \
                    from None
            url = urljoin(url, e.headers.get("Location") or "")
            p = urlsplit(url)
            if p.scheme != "https" or not (p.hostname or "").endswith(".githubusercontent.com"):
                raise UpdateError(f"The download was sent to {p.hostname or url}, which is not "
                                  "GitHub; stopped.") from None
        except (URLError, OSError) as e:
            raise UpdateError("The download stopped. Check your internet connection and try "
                              f"again ({getattr(e, 'reason', e)}).") from None
    raise UpdateError("The download was redirected too many times; stopped.")


def download(info: dict, dest: Path, on_progress=None, cancel=None, opener=None) -> Path | None:
    """`dest`/CortexSetup.exe, checked against the release's .sha256; None when `cancel` (an
    Event) is set. A partial or mismatched file is deleted."""
    if info["exe_size"] > MAX_BYTES:
        raise UpdateError(f"The update is larger than {MAX_BYTES // 2**20} MB; stopped.")
    with _fetch(info["sha_url"], opener) as r:
        m = re.match(rb"\s*([0-9a-fA-F]{64})\b", r.read(4096))
    if not m:
        raise UpdateError("The release's checksum file is not readable; stopped.")
    want = m[1].decode().lower()
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / EXE
    part = out.with_name(EXE + ".part")
    h, got, said = hashlib.sha256(), 0, -1
    try:
        with _fetch(info["exe_url"], opener) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or info["exe_size"] or 0)
            if total > MAX_BYTES:
                raise UpdateError(f"The update is larger than {MAX_BYTES // 2**20} MB; stopped.")
            while chunk := r.read(1 << 16):
                if cancel is not None and cancel.is_set():
                    break
                got += len(chunk)
                if got > MAX_BYTES:
                    raise UpdateError(f"The update is larger than {MAX_BYTES // 2**20} MB; "
                                      "stopped.")
                h.update(chunk)
                f.write(chunk)
                if on_progress and got >> 20 != said:  # once per MB
                    said = got >> 20
                    on_progress(got, total)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if cancel is not None and cancel.is_set():
        part.unlink(missing_ok=True)
        return None
    if h.hexdigest() != want:
        part.unlink(missing_ok=True)
        raise UpdateError("The download does not match the release's checksum, so it was "
                          "deleted. Try again later.")
    os.replace(part, out)
    return out


def launch(exe: Path, app_pid: int, install: Path | None = None) -> None:
    """Start the new installer detached; it waits for `app_pid` to exit, then updates."""
    args = [str(exe), "--update", "--wait-pid", str(app_pid)]
    if install is not None:
        args += ["--dir", str(install)]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    # PyInstaller's temp-folder variables must not reach the new installer (as child_env does)
    subprocess.Popen(args, creationflags=flags, close_fds=True,
                     env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})
