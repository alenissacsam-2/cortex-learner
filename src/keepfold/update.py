"""Check for updates and fetch the new installer (docs/INSTALL.md).

The app checks quietly a minute after it starts and every 6 hours (`server.update_quietly`), and
for KeepfoldSetup installs downloads the new installer in the background. Nothing is installed
without a click.

Stdlib only. `latest` asks GitHub for the latest release of `REPO` and sends nothing about
the user (a generic User-Agent). `download` takes only that repo's release assets, follows
redirects only to GitHub's download hosts, caps the size and checks the SHA-256 that the release
publishes. `launch` starts the new KeepfoldSetup.exe with `--update`; it waits for the app to exit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.request
from datetime import datetime
from importlib.metadata import version as _dist_version
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit

from keepfold._proc import proxy_env

REPO = "alenissacsam-2/keepfold"  # the GitHub repo that holds the plugin marketplace
HERE = Path(__file__).resolve()
CHECKOUT = HERE.parents[2]  # the source checkout of an editable install

API = "https://api.github.com"
EXE = "KeepfoldSetup.exe"
SHA = EXE + ".sha256"  # the release checksum, saved beside a downloaded installer
MARKER = "keepfold-install.txt"  # installer/setup_app.py drops it in the install folder
MAX_BYTES = 200 * 2**20
TIMEOUT = 10
DOWNLOADS = f"https://github.com/{REPO}/releases/download/"
_VERSION = re.compile(r"v?(\d+(?:\.\d+){1,3})")
_REDIRECTS = (301, 302, 303, 307, 308)
# where GitHub sends release downloads (raw.githubusercontent.com serves anyone's files: never)
_CDN = ("objects.githubusercontent.com", "release-assets.githubusercontent.com")


class UpdateError(ValueError):
    """A failure the app shows in plain words (a ValueError: the app's jobs show its text)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # a 3xx then raises HTTPError; _get checks the target
        return None


_opener = urllib.request.build_opener(_NoRedirect).open


def parse_version(tag) -> tuple[int, ...]:
    m = _VERSION.fullmatch(tag.strip()) if isinstance(tag, str) else None
    if not m:  # pre-releases (v1.0.0-rc1) and odd tags are not offered
        raise UpdateError(f"{tag!r} is not a release version Keepfold can update to.")
    return tuple(int(x) for x in m[1].split("."))


def newer(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
    """a > b, with 0.2 == 0.2.0."""
    return a + (0,) * (4 - len(a)) > b + (0,) * (4 - len(b))


def current() -> str:
    return _dist_version("keepfold")


def install_dir(exe: str | None = None) -> Path | None:
    """The KeepfoldSetup.exe install folder this Python runs from, or None (a dev install)."""
    p = Path(exe or sys.executable).resolve()
    return next((d for d in p.parents if (d / MARKER).is_file()), None)


def mode() -> str:
    return "installer" if install_dir() else "dev"


def tool_source() -> Path | None:
    """The local folder a non-editable `uv tool install <folder>` built this copy from (read
    from the tool's uv-receipt.toml), else None."""
    receipt = next((d / "uv-receipt.toml" for d in HERE.parents
                    if (d / "uv-receipt.toml").is_file()), None)
    try:
        reqs = tomllib.loads(receipt.read_text(encoding="utf-8"))["tool"]["requirements"]
    except (AttributeError, OSError, ValueError, KeyError, TypeError):  # no or odd receipt
        return None
    src = next((r.get("directory") for r in reqs
                if isinstance(r, dict) and r.get("name") == "keepfold"), None)
    return Path(src) if isinstance(src, str) else None


def command(tag: str) -> str:
    """What a dev install runs to update, as docs/INSTALL.md says (a clone, else from git)."""
    if (CHECKOUT / ".git").exists():
        return f'cd "{CHECKOUT}"; git pull; uv tool install --editable . --reinstall'
    if (src := tool_source()) is not None:  # a fixed copy of a folder: rebuild it from there
        pull = f'cd "{src}"; git pull; ' if (src / ".git").exists() else ""
        return f'{pull}uv tool install --force --reinstall "{src}"'
    return f"uv tool install --force git+https://github.com/{REPO}@{tag}"


def _get(url: str, opener, accept: str):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "keepfold-updater"})
    return (opener or _opener)(req, timeout=TIMEOUT)


def latest(opener=None, api: str = API) -> dict:
    try:
        with _get(f"{api}/repos/{REPO}/releases/latest", opener,
                  "application/vnd.github+json") as r:
            data = json.loads(r.read(2**20))
    except HTTPError as e:
        if e.code in (403, 429):  # 60 API calls an hour per address: a campus network runs out
            return _latest_from_page(opener, (e.headers or {}).get("X-RateLimit-Reset"))
        if e.code == 404:
            raise UpdateError("GitHub found no Keepfold release just now (the project page may be "
                              "unavailable for a while). Try again later.") from None
        raise UpdateError(f"GitHub answered {e.code}. Try again later.") from None
    except (URLError, OSError) as e:
        raise UpdateError("Could not reach GitHub. Check your internet connection and try "
                          f"again ({getattr(e, 'reason', e)}).") from None
    except ValueError:
        raise UpdateError("GitHub sent an answer Keepfold could not read. Try again later.") \
            from None
    if not isinstance(data, dict):
        raise UpdateError("GitHub sent an answer Keepfold could not read. Try again later.")
    tag = data.get("tag_name")
    if data.get("prerelease") or data.get("draft"):
        raise UpdateError(f"The newest release ({tag}) is a pre-release; Keepfold only updates "
                          "to full releases.")
    ver = parse_version(tag)
    notes = f"https://github.com/{REPO}/releases/tag/{tag}"  # built, never taken from the answer
    assets = {a.get("name"): a for a in data.get("assets") or [] if isinstance(a, dict)}
    exe, sha = assets.get(EXE), assets.get(EXE + ".sha256")
    if not exe or not sha:
        raise UpdateError(f"Keepfold {tag} has no {EXE} with its .sha256 on GitHub, so it cannot "
                          f"be installed from here. See {notes}")
    want = f"{DOWNLOADS}{tag}/{EXE}"  # exactly this release's files, no `..` tricks
    links = (exe.get("browser_download_url"), sha.get("browser_download_url"))
    if links != (want, want + ".sha256"):
        raise UpdateError(f"Keepfold {tag}'s download links are not this release's files; stopped. "
                          f"See {notes}")
    return {"tag": tag, "version": ver, "notes_url": notes,
            "exe_url": want, "sha_url": want + ".sha256",
            "exe_size": int(exe.get("size") or 0)}


def _latest_from_page(opener, reset=None) -> dict:
    """The latest release without the API: github.com/<repo>/releases/latest redirects to its
    tag (never a draft or a pre-release), and the asset links follow from the tag. The size is
    unknown until the download; the .sha256 check is the same."""
    page = f"https://github.com/{REPO}/releases/latest"
    try:  # X-RateLimit-Reset: when the API takes this network again (epoch seconds)
        when = "after " + datetime.fromtimestamp(int(reset)).strftime("%H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        when = "in an hour"
    limited = UpdateError("GitHub is refusing update checks from this network for now (it "
                          "limits how often one network may ask, and a VPN or a campus network "
                          f"is shared by many). Try again {when}, or download KeepfoldSetup.exe "
                          f"from the release page: {page}")
    try:
        with _get(page, opener, "text/html"):
            raise limited  # no redirect: no release to go to
    except HTTPError as e:
        loc = urljoin(page, e.headers.get("Location") or "") if e.code in _REDIRECTS else ""
    except (URLError, OSError):
        raise limited from None
    m = re.fullmatch(rf"https://github\.com/{re.escape(REPO)}/releases/tag/([^/?#]+)", loc)
    if not m:
        raise limited
    tag = unquote(m[1])
    ver = parse_version(tag)
    want = f"{DOWNLOADS}{tag}/{EXE}"
    return {"tag": tag, "version": ver, "exe_url": want, "sha_url": want + ".sha256",
            "notes_url": f"https://github.com/{REPO}/releases/tag/{tag}", "exe_size": 0}


def dest_dir(info: dict) -> Path:
    return Path.home() / ".keepfold" / "tmp" / f"update-{'.'.join(map(str, info['version']))}"


def _fetch(url: str, opener):
    """Open a release asset, following redirects only to GitHub's https download hosts."""
    if not isinstance(url, str) or not url.startswith(DOWNLOADS):
        raise UpdateError(f"Refused a download that is not a Keepfold release file: {url}")
    for _ in range(5):
        try:
            return _get(url, opener, "application/octet-stream")
        except HTTPError as e:
            if e.code not in _REDIRECTS:
                raise UpdateError(f"GitHub answered {e.code} for {url}. Try again later.") \
                    from None
            url = urljoin(url, e.headers.get("Location") or "")
            p = urlsplit(url)
            if p.scheme != "https" or (p.hostname or "") not in _CDN:
                raise UpdateError(f"The download was sent to {p.hostname or url}, which is not "
                                  "GitHub; stopped.") from None
        except (URLError, OSError) as e:
            raise UpdateError("The download stopped. Check your internet connection and try "
                              f"again ({getattr(e, 'reason', e)}).") from None
    raise UpdateError("The download was redirected too many times; stopped.")


def download(info: dict, dest: Path, on_progress=None, cancel=None, opener=None) -> Path | None:
    """`dest`/KeepfoldSetup.exe, checked against the release's .sha256; None when `cancel` (an
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
    if out.is_file() and _sha256(out) == want:  # prepared before a restart: not fetched again
        (dest / SHA).write_text(want, encoding="ascii")
        return out
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
    (dest / SHA).write_text(want, encoding="ascii")  # what `prepared` checks at the next launch
    return out


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def prepared() -> Path | None:
    """The update-<version> folder (dest_dir) of an installer already downloaded for a newer
    Keepfold than this one, whose file still matches the release checksum saved beside it; the
    newest such, else None. The launch sweep keeps it, so a restart does not fetch it again."""
    best: tuple | None = None
    try:
        cur = parse_version(current())
    except (UpdateError, ValueError):
        return None
    for d in (Path.home() / ".keepfold" / "tmp").glob("update-*"):
        try:
            v = parse_version(d.name.removeprefix("update-"))
            ok = newer(v, cur) and (best is None or newer(v, best[0])) and \
                _sha256(d / EXE) == (d / SHA).read_text(encoding="ascii").strip().lower()
        except (UpdateError, OSError, UnicodeDecodeError):
            continue
        if ok:
            best = (v, d)
    return best[1] if best else None


def launch(exe: Path, app_pid: int, install: Path | None = None) -> None:
    """Start the new installer detached; it waits for `app_pid` to exit, then updates."""
    args = [str(exe), "--update", "--wait-pid", str(app_pid)]
    if install is not None:
        args += ["--dir", str(install)]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    # PyInstaller's temp-folder variables must not reach the new installer (as child_env does);
    # the system proxy does, for its uv (which reads only HTTPS_PROXY)
    subprocess.Popen(args, creationflags=flags, close_fds=True,
                     env={**proxy_env(), "PYINSTALLER_RESET_ENVIRONMENT": "1"})
