"""Check for updates and fetch the new installer (docs/INSTALL.md).

The app checks quietly a minute after it starts and every 6 hours (`server.update_quietly`), and
for KeepfoldSetup installs downloads the new installer in the background. Nothing is installed
without a click.

Stdlib only. `latest` asks GitHub for the latest release of `REPO` and sends nothing about
the user (a generic User-Agent). `download` takes only that repo's release assets, follows
redirects only to GitHub's download hosts, caps the size and checks the SHA-256 that the release
publishes. `launch` starts the new KeepfoldSetup.exe with `--update`; it waits for the app to exit.

The frozen Mac and Linux app downloads only on the Update now click (`download_asset`), then
`install` swaps the new version in and restarts it (port DESIGN.md 5.1, 5.2).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform as _platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.request
from datetime import datetime
from importlib.metadata import version as _dist_version
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit

from keepfold._proc import NO_WINDOW, proxy_env

REPO = "alenissacsam-2/keepfold"  # the GitHub repo that holds the plugin marketplace
HERE = Path(__file__).resolve()
CHECKOUT = HERE.parents[2]  # the source checkout of an editable install

API = "https://api.github.com"
EXE = "KeepfoldSetup.exe"
SHA = EXE + ".sha256"  # the release checksum, saved beside a downloaded installer
MARKER = "keepfold-install.txt"  # installer/setup_app.py drops it in the install folder
MAX_BYTES = 200 * 2**20
# shortcut: Mac/Linux caps are guesses; part 2 sets them from the real archive sizes in CI
MAX_BYTES_BY_OS = {"win32": MAX_BYTES, "darwin": 400 * 2**20, "linux": 400 * 2**20}
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
    """"app" for the frozen Mac or Linux app, "installer" for KeepfoldSetup.exe, else "dev"."""
    if getattr(sys, "frozen", False) and (sys.platform == "darwin"
                                          or sys.platform.startswith("linux")):
        return "app"
    return "installer" if install_dir() else "dev"


def asset_name(version: tuple[int, ...], platform: str | None = None,
               machine: str | None = None) -> str | None:
    """This computer's file in a release (each beside its .sha256), or None (none is built)."""
    platform, machine = platform or sys.platform, machine or _platform.machine()
    v = ".".join(map(str, version))
    if platform == "win32":
        return EXE
    if platform == "darwin" and machine == "arm64":
        return f"Keepfold-{v}-macos-arm64.zip"
    if platform.startswith("linux") and machine == "x86_64":
        return f"keepfold-{v}-linux-x86_64.tar.gz"
    return None


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
    name = asset_name(ver)
    exe, sha = (assets.get(name), assets.get(name + ".sha256")) if name else (None, None)
    if not exe or not sha:
        if sys.platform == "win32":
            raise UpdateError(f"Keepfold {tag} has no {EXE} with its .sha256 on GitHub, so it "
                              f"cannot be installed from here. See {notes}")
        # Mac/Linux: CI attaches this computer's file 20-30 minutes after the release (the app
        # waits for it); a dev install updates with uv and needs none
        return {"tag": tag, "version": ver, "notes_url": notes, "exe_url": None, "sha_url": None,
                "exe_size": 0, **({"waiting": True} if mode() == "app" else {})}
    want = f"{DOWNLOADS}{tag}/{name}"  # exactly this release's files, no `..` tricks
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
    what = EXE if sys.platform == "win32" else "the new version"
    limited = UpdateError("GitHub is refusing update checks from this network for now (it "
                          "limits how often one network may ask, and a VPN or a campus network "
                          f"is shared by many). Try again {when}, or download {what} "
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
    name = asset_name(ver)  # this computer's file; None where none is built
    want = f"{DOWNLOADS}{tag}/{name}" if name else None
    return {"tag": tag, "version": ver, "exe_url": want, "sha_url": want and want + ".sha256",
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


def download(info: dict, dest: Path, on_progress=None, cancel=None, opener=None,
             cap: int | None = None) -> Path | None:
    """`dest`/KeepfoldSetup.exe (or the file `exe_url` names), checked against the release's
    .sha256 and at most `cap` bytes (MAX_BYTES); None when `cancel` (an Event) is set. A partial
    or mismatched file is deleted."""
    cap = cap or MAX_BYTES
    if info["exe_size"] > cap:
        raise UpdateError(f"The update is larger than {cap // 2**20} MB; stopped.")
    with _fetch(info["sha_url"], opener) as r:
        m = re.match(rb"\s*([0-9a-fA-F]{64})\b", r.read(4096))
    if not m:
        raise UpdateError("The release's checksum file is not readable; stopped.")
    want = m[1].decode().lower()
    dest.mkdir(parents=True, exist_ok=True)
    name = str(info["exe_url"]).rsplit("/", 1)[-1]  # _fetch refuses any url not this release's
    out = dest / name
    if out.is_file() and _sha256(out) == want:  # prepared before a restart: not fetched again
        (dest / (name + ".sha256")).write_text(want, encoding="ascii")
        return out
    part = out.with_name(name + ".part")
    h, got, said = hashlib.sha256(), 0, -1
    try:
        with _fetch(info["exe_url"], opener) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or info["exe_size"] or 0)
            if total > cap:
                raise UpdateError(f"The update is larger than {cap // 2**20} MB; stopped.")
            while chunk := r.read(1 << 16):
                if cancel is not None and cancel.is_set():
                    break
                got += len(chunk)
                if got > cap:
                    raise UpdateError(f"The update is larger than {cap // 2**20} MB; "
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
    # what `prepared` (Windows) or `install` (Mac, Linux) checks before using it
    (dest / (name + ".sha256")).write_text(want, encoding="ascii")
    return out


def download_asset(info: dict, dest_dir: Path, on_progress=None, cancel=None,
                   opener=None) -> Path | None:
    """This computer's update file (the Mac zip, the Linux tarball), fetched and checked as
    `download` fetches the installer, under this OS's size cap."""
    return download(info, dest_dir, on_progress, cancel, opener,
                    MAX_BYTES_BY_OS.get(sys.platform, MAX_BYTES))


def verify(path: Path, sha: str) -> None:
    """The one check right before a swap: the file is the release's (SHA-256 now; PLAN-1.0 G7's
    Ed25519 signature goes here too)."""
    try:
        ok = bool(sha.strip()) and _sha256(path) == sha.strip().lower()
    except OSError:
        ok = False
    if not ok:
        raise UpdateError("The update does not match the release's checksum; nothing was "
                          "changed. Try again later.")


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


# ---- Mac and Linux: Update now swaps in the new version (port DESIGN.md 5.1, 5.2) ------------

LAUNCHER = Path("/usr/bin/keepfold-app")  # the .deb's: runs the newest copy (tests replace this)
_BUSY = ("Keepfold is in use by {n} program{s} (the Keepfold connection of AI apps such as "
         "Claude, or the browsing receiver). Nothing is closed until you choose.")


def can_replace() -> bool:
    """Update now can install here. Mac: write access to the app and its folder (a standard user
    cannot change /Applications); Linux: the .deb's launcher, which runs the per-user copy."""
    if sys.platform != "darwin":
        return LAUNCHER.is_file()
    from keepfold.app import bundle
    app = bundle.app_path()
    return app is not None and _writable(app.parent) and _writable(app)


def install(archive: Path, info: dict, close=()) -> dict:
    """Swap in the version `download_asset` fetched, once `verify` passes, and start it after
    this app exits (the caller then quits): {} when done. Mac: Keepfold programs still running
    from the bundle are ended only once the user agreed to each (`close`, their pids); until
    then {"busy": [(pid, path), ...], "note"} and nothing changes. Never a version that is not
    newer than this one."""
    v = ".".join(map(str, info["version"]))
    if not newer(info["version"], parse_version(current())):
        raise UpdateError(f"Keepfold {v} is not newer than this copy ({current()}); not "
                          "installed.")
    try:
        sha = archive.with_name(archive.name + ".sha256").read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        sha = ""
    verify(archive, sha)
    if sys.platform == "darwin":
        return _install_mac(archive, info, set(close))
    return _install_linux(archive, v)


def _install_mac(archive: Path, info: dict, close: set[int]) -> dict:
    """DESIGN.md 5.1: unpack beside the app (the same volume), check the signature, wait for the
    Programs in use consent, swap the two in one step, keep the old copy for going back."""
    from keepfold.app import bundle
    app = bundle.app_path()
    if app is None:
        raise UpdateError("Only the Mac app can update itself.")
    stage = app.parent / ".Keepfold-update"
    new = stage / "Keepfold.app"  # the zip's top folder (ditto -c -k --keepParent)
    unchanged = f"nothing was changed. You can download the new version from {info['notes_url']}"
    shutil.rmtree(stage, ignore_errors=True)
    try:
        ok, out = _run(["ditto", "-x", "-k", str(archive), str(stage)])  # zipfile drops symlinks
        if not ok or not new.is_dir():
            raise UpdateError(f"Could not unpack the update ({out.strip()}); {unchanged}")
        ok, out = _run(["codesign", "--verify", "--deep", "--strict", str(new)])
        if not ok:
            raise UpdateError(f"The new version's signature does not check out ({out}); "
                              f"{unchanged}")
        busy = _in_use(app)
        if busy and {pid for pid, _ in busy} <= close:  # the user agreed to end exactly these
            _end([pid for pid, _ in busy])
            busy = _in_use(app)
        if busy:  # old processes would load files from the new copy at the same path
            return {"busy": busy, "note": _BUSY.format(n=len(busy), s="s" * (len(busy) != 1))}
        try:
            _exchange(new, app)  # a power cut leaves the old copy or the new one, never neither
        except OSError as e:
            raise UpdateError(f"Could not swap in the new version ({e}); {unchanged}") from None
        try:
            _relaunch(["open", str(app)], os.getpid())
        except (OSError, subprocess.SubprocessError) as e:
            _exchange(new, app)  # the old copy goes back
            raise UpdateError(f"Could not start the new version ({e}); {unchanged}") from None
        prev = Path.home() / "Library" / "Application Support" / "Keepfold" / "previous"
        shutil.rmtree(prev, ignore_errors=True)  # one go-back copy, outside Launchpad's view
        with contextlib.suppress(OSError, shutil.Error):
            prev.mkdir(parents=True)
            shutil.move(new, prev / app.name)
        return {}
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _install_linux(archive: Path, v: str) -> dict:
    """DESIGN.md 5.2: unpack into ~/.local/share/keepfold/app/<v>/ (the launcher reads $HOME,
    not XDG_DATA_HOME) through a dot-folder it ignores, keep the version before, restart through
    the launcher. Old processes run on from their own folders, so none is ended."""
    root = Path.home() / ".local" / "share" / "keepfold" / "app"
    target = root / v
    if not target.exists():  # one unpacked before is complete: it was renamed in whole
        stage = root / f".{v}.staging"
        shutil.rmtree(stage, ignore_errors=True)
        try:
            stage.mkdir(parents=True)
            with tarfile.open(archive) as t:  # one top folder, <v>/ (packaging/linux/build.sh)
                t.extractall(stage, filter="data")  # nothing outside the folder, no devices
            if not (stage / v / "keepfold-app").is_file():
                raise UpdateError(f"The download does not hold Keepfold {v}; nothing was "
                                  "changed.")
            os.replace(stage / v, target)
        except (OSError, tarfile.TarError) as e:
            raise UpdateError(f"Could not unpack Keepfold {v} ({e}); nothing was changed.") \
                from None
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    _prune(root)
    _relaunch([str(LAUNCHER)], os.getpid())
    return {}


def _prune(root: Path) -> None:
    """Keep the newest per-user version and the one before it (to go back); delete older ones
    that no running program uses (an AI app's Keepfold connection may still run from one)."""
    found = []
    for d in root.iterdir():
        with contextlib.suppress(UpdateError):  # not a version folder: never ours to delete
            if d.is_dir():
                found.append((parse_version(d.name), d))
    used = _proc_exes()
    for _, d in sorted(found)[:-2]:
        if not any(e.startswith(os.path.join(str(d), "")) for e in used):
            shutil.rmtree(d, ignore_errors=True)


def _in_use(app: Path) -> list[tuple[int, str]]:
    """Keepfold programs running from inside `app`, never this app itself. An AI app may start
    one through the ~/.local/bin/keepfold link, so each path is also resolved."""
    roots = tuple({os.path.join(str(app), ""), os.path.join(os.path.realpath(app), "")})
    return [(pid, path) for pid, path in _ps() if pid != os.getpid() and (
        path.startswith(roots) or (os.path.isabs(path)
                                   and os.path.realpath(path).startswith(roots)))]


# The OS calls below are replaced in tests: no real ditto, codesign, ps, process, /proc or open.

def _run(args: list[str], timeout: float = 300) -> tuple[bool, str]:
    """(ok, output): stdout when it succeeded, else the error text, trimmed."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", stdin=subprocess.DEVNULL, timeout=timeout,
                           creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if r.returncode == 0:
        return True, r.stdout
    return False, (r.stderr or r.stdout).strip()[-2000:]


def _ps() -> list[tuple[int, str]]:
    """(pid, program path) of every process: on a Mac, ps's comm is the path it was started by."""
    ok, out = _run(["ps", "-axo", "pid=,comm="])
    if not ok:
        raise UpdateError(f"Could not list the running programs ({out}); nothing was changed.")
    procs = []
    for line in out.splitlines():
        pid, _, path = line.strip().partition(" ")
        if pid.isdigit():
            procs.append((int(pid), path.strip()))
    return procs


def _end(pids: list[int]) -> None:
    """End programs the user agreed to close: SIGTERM, then SIGKILL after 5 s for any left."""
    for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, sig)
        for _ in range(25):
            if not any(_alive(pid) for pid in pids):
                return
            time.sleep(0.2)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: it runs, as another user
        pass
    return True


def _exchange(a: Path, b: Path) -> None:
    """Swap two folders in one step: macOS's renamex_np with RENAME_SWAP (APFS)."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renamex_np(os.fsencode(a), os.fsencode(b), 0x2) != 0:  # 0x2: RENAME_SWAP
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(a))


def _writable(p: Path) -> bool:
    return os.access(p, os.W_OK)


def _proc_exes() -> list[str]:
    """The program path of every process this user may read (Linux: /proc/<pid>/exe)."""
    out = []
    for p in Path("/proc").glob("[0-9]*/exe"):
        with contextlib.suppress(OSError):
            out.append(os.readlink(p))
    return out


def _relaunch(cmd: list[str], pid: int) -> None:
    """Run `cmd` once process `pid` (this app) has ended: while it runs, its local server would
    answer the new copy's start and only show this window."""
    subprocess.Popen(["/bin/sh", "-c", 'while kill -0 "$1" 2>/dev/null; do sleep 0.2; done; '
                      'shift; exec "$@"', "sh", str(pid), *cmd],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, creationflags=NO_WINDOW,
                     env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})


# ---- Uninstall Keepfold… in Settings (port Task 25): after cli.uninstall_steps ----

def remove_app(say) -> dict:
    """Remove the Mac or Linux app itself, only on the user's confirmed click: the Mac .app to
    the Trash; on Linux the per-user update copies, then the .deb through pkexec. Returns
    {"removed": True}, or {"removed": False, "note": what to do by hand}; the caller quits only
    when it was removed."""
    if sys.platform == "darwin":
        from keepfold.app import bundle
        app = bundle.app_path()
        try:
            if app is None:
                raise OSError("this is not the Keepfold app")
            _trash(app)
        except Exception as e:  # pyobjc missing or refused: the user can still drag it
            return {"removed": False, "note": f"Could not move Keepfold to the Trash ({e}). "
                    "Quit Keepfold, then drag it from Applications to the Trash."}
        say(f"moved {app} to the Trash")
        return {"removed": True}
    say("asking for your password to remove the keepfold package")
    ok, out = _apt_remove()
    if not ok:
        return {"removed": False, "note": "Keepfold was not removed: the password prompt was "
                f"cancelled or apt-get failed ({out or 'no details'}). Remove it in a terminal: "
                "sudo apt remove keepfold"}
    say("removed the keepfold package")
    # last, just before the quit: this app may run from one of these copies
    copies = Path.home() / ".local" / "share" / "keepfold" / "app"  # Update now's (_install_linux)
    shutil.rmtree(copies, ignore_errors=True)
    say(f"removed {copies}")
    return {"removed": True}


def _trash(path: Path) -> None:  # tests replace this
    """Move `path` to the Trash through NSFileManager, so Finder's Put Back works."""
    from Foundation import NSURL, NSFileManager  # pyobjc, the Mac app only
    ok, _, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(str(path)), None, None)
    if not ok:
        raise OSError(str(err))


def _apt_remove() -> tuple[bool, str]:  # tests replace this
    """`pkexec apt-get remove -y keepfold`: polkit asks for the password; (ok, output)."""
    try:
        r = subprocess.run(["pkexec", "apt-get", "remove", "-y", "keepfold"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=900, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-500:]
