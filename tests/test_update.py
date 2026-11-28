"""keepfold.update: the latest-release check, the checked download and the installer launch, with a
fake GitHub (an injected opener; no network)."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from keepfold import update
from keepfold.cli import REPO

API = f"{update.API}/repos/{REPO}/releases/latest"
DL = f"https://github.com/{REPO}/releases/download/v0.2.0/"
EXE = b"MZ new installer " * 1000
SHA = hashlib.sha256(EXE).hexdigest()


class Resp(io.BytesIO):
    def __init__(self, body: bytes, headers: dict | None = None):
        super().__init__(body)
        self.headers = headers or {}


def release(tag="v0.2.0", assets=("KeepfoldSetup.exe", "KeepfoldSetup.exe.sha256"), **extra):
    return {"tag_name": tag, "html_url": "https://evil.example/notes", "prerelease": False,
            "draft": False, **extra,
            "assets": [{"name": n, "browser_download_url": DL + n, "size": len(EXE)}
                       for n in assets]}


def moved(base: str) -> dict:
    """A release whose asset links point under `base` instead of its own tag."""
    names = ("KeepfoldSetup.exe", "KeepfoldSetup.exe.sha256")
    return {**release(), "assets": [{"name": n, "browser_download_url": base + n} for n in names]}


def fake(routes: dict):
    """An opener over `routes`: bytes, a dict (JSON), an int (an HTTP error) or (code, location)
    (a redirect). It records every request."""
    seen = []

    def open_(req, timeout):
        seen.append({"url": req.full_url, "headers": dict(req.header_items()), "timeout": timeout})
        r = routes.get(req.full_url)
        if r is None:
            raise URLError("no route")
        if isinstance(r, int):
            raise HTTPError(req.full_url, r, "x", {}, None)
        if isinstance(r, tuple):
            hdrs = r[1] if isinstance(r[1], dict) else {"Location": r[1]}
            raise HTTPError(req.full_url, r[0], "x", hdrs, None)
        return Resp(r if isinstance(r, bytes) else json.dumps(r).encode())
    open_.seen = seen
    return open_


def info(**over):
    return {"tag": "v0.2.0", "version": (0, 2, 0), "notes_url": "n",
            "exe_url": DL + "KeepfoldSetup.exe", "sha_url": DL + "KeepfoldSetup.exe.sha256",
            "exe_size": len(EXE), **over}


def files(routes=None, sha=SHA):
    return {DL + "KeepfoldSetup.exe": EXE,
            DL + "KeepfoldSetup.exe.sha256": f"{sha}  KeepfoldSetup.exe\n".encode(),
            **(routes or {})}


# ---- latest ---------------------------------------------------------------------------------


@pytest.fixture
def windows(monkeypatch):
    """The Windows release file, KeepfoldSetup.exe, whatever OS runs the tests."""
    monkeypatch.setattr(sys, "platform", "win32")


@pytest.mark.usefixtures("windows")
def test_latest_reads_the_release_with_a_generic_request():
    op = fake({API: release()})
    got = update.latest(opener=op)
    assert got == {"tag": "v0.2.0", "version": (0, 2, 0),
                   "notes_url": f"https://github.com/{REPO}/releases/tag/v0.2.0",  # never html_url
                   "exe_url": DL + "KeepfoldSetup.exe", "sha_url": DL + "KeepfoldSetup.exe.sha256",
                   "exe_size": len(EXE)}
    (req,) = op.seen
    assert req["timeout"] == 10
    assert req["headers"] == {"Accept": "application/vnd.github+json",
                              "User-agent": "keepfold-updater"}


@pytest.mark.parametrize("tag,want", [("v0.2.0", (0, 2, 0)), ("1.2", (1, 2)),
                                      ("v1.2.3.4", (1, 2, 3, 4)), (" v3.0.1 ", (3, 0, 1))])
def test_parse_version(tag, want):
    assert update.parse_version(tag) == want


@pytest.mark.parametrize("tag", ["v1", "v1.2.3-rc1", "1.2.3b1", "latest", "v1.2.3.4.5", "", None,
                                 "V1.2.3", "v1..2"])
def test_parse_version_refuses_prereleases_and_odd_tags(tag):
    with pytest.raises(update.UpdateError):
        update.parse_version(tag)


def test_newer_older_same():
    assert update.newer((0, 2, 0), (0, 1, 0))
    assert not update.newer((0, 1, 0), (0, 1, 0))
    assert not update.newer((0, 1), (0, 1, 0))  # 0.1 == 0.1.0
    assert not update.newer((0, 0, 9), (0, 1, 0))
    assert update.newer((0, 1, 0, 1), (0, 1))


@pytest.mark.parametrize("data,match", [
    (release(prerelease=True), "pre-release"),
    (release(tag="v0.3.0-beta"), "not a release version"),
    (release(tag="nightly"), "not a release version"),
    (release(assets=("KeepfoldSetup.exe",)), r"\.sha256"),
    (release(assets=("KeepfoldSetup.exe.sha256", "other.zip")), "KeepfoldSetup.exe"),
    ([1, 2], "could not read"),
    # links that are not exactly this release's files (another tag, a `..` path)
    (moved(DL.replace("v0.2.0", "v0.1.0")), "not this release"),
    (moved(DL + "../../../x/y/"), "not this release"),
])
@pytest.mark.usefixtures("windows")
def test_latest_refuses_what_cannot_be_installed(data, match):
    with pytest.raises(update.UpdateError, match=match):
        update.latest(opener=fake({API: data}))


@pytest.mark.parametrize("route,match", [(403, "refusing"), (429, "refusing"),
                                         (404, "no Keepfold release"), (500, "answered 500"),
                                         (None, "internet connection"), (b"{not json", "read")])
def test_latest_errors_say_what_happened(route, match):
    with pytest.raises(update.UpdateError, match=match):
        update.latest(opener=fake({} if route is None else {API: route}))


PAGE = f"https://github.com/{REPO}/releases/latest"


@pytest.mark.usefixtures("windows")
def test_a_rate_limited_api_falls_back_to_the_latest_page(tmp_path):
    """A campus network shares 60 API calls an hour; the page's redirect names the tag."""
    for code in (403, 429):
        got = update.latest(opener=fake({API: code,
                                         PAGE: (302, f"/{REPO}/releases/tag/v0.2.0")}))
        assert got == {**info(), "notes_url": f"https://github.com/{REPO}/releases/tag/v0.2.0",
                       "exe_size": 0}
    # the download then works as usual: the size comes from the response, the hash is checked
    assert update.download(got, tmp_path, opener=fake(files())).read_bytes() == EXE


@pytest.mark.parametrize("page,match", [
    (None, "refusing"),  # the page is unreachable too
    (b"<html>", "refusing"),  # no redirect: no release
    ((302, "https://github.com/someone/else/releases/tag/v9.9.9"), "refusing"),
    ((302, f"https://evil.example/{REPO}/releases/tag/v9.9.9"), "refusing"),
    ((302, f"https://github.com/{REPO}/releases/tag/v0.3.0-rc1"), "not a release version"),
    ((302, f"https://github.com/{REPO}/releases/tag/..%2F..%2Fx"), "not a release version"),
])
def test_the_latest_page_takes_only_this_repos_release_tags(page, match):
    routes = {API: 403, **({} if page is None else {PAGE: page})}
    with pytest.raises(update.UpdateError, match=match):
        update.latest(opener=fake(routes))


def test_both_refused_says_when_to_try_again_and_where_to_download():
    reset = 1_900_000_000  # X-RateLimit-Reset, shown as a local time
    with pytest.raises(update.UpdateError) as e:
        update.latest(opener=fake({API: (403, {"X-RateLimit-Remaining": "0",
                                               "X-RateLimit-Reset": str(reset)})}))
    msg = str(e.value)
    assert "refusing update checks from this network" in msg
    assert "after " + datetime.fromtimestamp(reset).strftime("%H:%M") in msg
    assert PAGE in msg
    with pytest.raises(update.UpdateError, match="in an hour"):  # no or odd header
        update.latest(opener=fake({API: (429, {"X-RateLimit-Reset": "soon"})}))


def test_update_error_is_a_value_error():  # the app shows its text, not a traceback
    assert issubclass(update.UpdateError, ValueError)


# ---- download -------------------------------------------------------------------------------


def test_download_checks_the_hash_and_reports_progress(tmp_path):
    seen = []
    out = update.download(info(), tmp_path / "d", lambda g, t: seen.append((g, t)),
                          threading.Event(), opener=fake(files()))
    assert out == tmp_path / "d" / "KeepfoldSetup.exe" and out.read_bytes() == EXE
    assert sorted(os.listdir(tmp_path / "d")) == ["KeepfoldSetup.exe", "KeepfoldSetup.exe.sha256"]
    assert (tmp_path / "d" / "KeepfoldSetup.exe.sha256").read_text() == SHA  # no .part left
    assert seen and seen[-1][1] == len(EXE)


def test_an_installer_already_there_is_not_fetched_again(tmp_path):
    update.download(info(), tmp_path, opener=fake(files()))
    op = fake(files())
    assert update.download(info(), tmp_path, opener=op).read_bytes() == EXE
    assert [s["url"] for s in op.seen] == [DL + "KeepfoldSetup.exe.sha256"]  # only the checksum
    (tmp_path / "KeepfoldSetup.exe").write_bytes(b"MZ changed")  # not the release's: fetched again
    op = fake(files())
    assert update.download(info(), tmp_path, opener=op).read_bytes() == EXE and len(op.seen) == 2


def test_prepared_is_a_newer_installer_that_still_matches_its_checksum(monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(update, "current", lambda: "0.2.0")
    tmp = Path.home() / ".keepfold" / "tmp"
    assert update.prepared() is None
    for ver, body, sha in (("0.1.9", EXE, SHA), ("0.2.0", EXE, SHA), ("0.3.0", EXE, SHA),
                           ("0.4.0", b"MZ half", SHA), ("0.5.0", EXE, None)):
        (tmp / f"update-{ver}").mkdir(parents=True)
        (tmp / f"update-{ver}" / "KeepfoldSetup.exe").write_bytes(body)
        if sha:
            (tmp / f"update-{ver}" / "KeepfoldSetup.exe.sha256").write_text(sha)
    (tmp / "update-junk").mkdir()
    assert update.prepared() == tmp / "update-0.3.0"  # older, same, torn, unchecked: not kept


def test_a_hash_mismatch_deletes_the_file(tmp_path):
    with pytest.raises(update.UpdateError, match="checksum"):
        update.download(info(), tmp_path, opener=fake(files(sha="0" * 64)))
    assert os.listdir(tmp_path) == []


def test_an_unreadable_checksum_file_stops_before_the_download(tmp_path):
    op = fake(files({DL + "KeepfoldSetup.exe.sha256": b"<html>not a hash</html>"}))
    with pytest.raises(update.UpdateError, match="checksum file"):
        update.download(info(), tmp_path / "d", opener=op)
    assert not (tmp_path / "d").exists() and len(op.seen) == 1


def test_redirects_only_to_githubusercontent_over_https(tmp_path):
    cdn = "https://objects.githubusercontent.com/x/KeepfoldSetup.exe"
    op = fake(files({DL + "KeepfoldSetup.exe": (302, cdn), cdn: EXE}))
    assert update.download(info(), tmp_path, opener=op).read_bytes() == EXE
    cdn2 = "https://release-assets.githubusercontent.com/y/KeepfoldSetup.exe"
    op = fake(files({DL + "KeepfoldSetup.exe": (302, cdn2), cdn2: EXE}))
    assert update.download(info(), tmp_path / "ok2", opener=op).read_bytes() == EXE
    for bad in ("https://evil.example/KeepfoldSetup.exe", "http://objects.githubusercontent.com/x",
                "https://githubusercontent.com.evil.example/x",
                "https://raw.githubusercontent.com/someone/repo/main/KeepfoldSetup.exe"):
        op = fake(files({DL + "KeepfoldSetup.exe": (302, bad), bad: EXE}))
        with pytest.raises(update.UpdateError, match="not GitHub"):
            update.download(info(), tmp_path / "bad", opener=op)
        assert bad not in [r["url"] for r in op.seen]
        assert os.listdir(tmp_path / "bad") == []


def test_only_this_repos_release_files(tmp_path):
    for url in ("https://github.com/someone/else/releases/download/v1/KeepfoldSetup.exe",
                f"http://github.com/{REPO}/releases/download/v1/KeepfoldSetup.exe", None):
        with pytest.raises(update.UpdateError, match="not a Keepfold release"):
            update.download(info(exe_url=url), tmp_path, opener=fake(files()))


def test_size_cap(tmp_path, monkeypatch):
    with pytest.raises(update.UpdateError, match="larger than 200 MB"):
        update.download(info(exe_size=201 * 2**20), tmp_path, opener=fake(files()))
    monkeypatch.setattr(update, "MAX_BYTES", 1000)  # a release that says less than it sends
    with pytest.raises(update.UpdateError, match="larger than"):
        update.download(info(exe_size=10), tmp_path, opener=fake(files()))
    assert os.listdir(tmp_path) == []


def test_cancel_leaves_nothing(tmp_path):
    stop = threading.Event()
    stop.set()
    assert update.download(info(), tmp_path, cancel=stop, opener=fake(files())) is None
    assert os.listdir(tmp_path) == []


def test_dest_dir_is_under_keepfold_tmp(tmp_path):
    assert update.dest_dir(info()) == update.Path.home() / ".keepfold" / "tmp" / "update-0.2.0"


# ---- mode, command, launch ------------------------------------------------------------------


def test_mode_installer_when_a_parent_holds_the_marker(tmp_path, monkeypatch):
    py = tmp_path / "Keepfold" / "tools" / "keepfold" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(py))
    assert update.mode() == "dev" and update.install_dir() is None
    (tmp_path / "Keepfold" / update.MARKER).write_text("x")
    assert update.mode() == "installer" and update.install_dir() == tmp_path / "Keepfold"


def test_mode_app_when_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    for plat in ("darwin", "linux"):
        monkeypatch.setattr(sys, "platform", plat)
        assert update.mode() == "app"
    monkeypatch.setattr(sys, "platform", "win32")  # Windows keeps installer/dev
    assert update.mode() == "dev"
    monkeypatch.setattr(sys, "frozen", False)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert update.mode() == "dev"


# ---- per-platform release files (Mac and Linux) ----------------------------------------------


MAC = "Keepfold-0.2.0-macos-arm64.zip"


def test_asset_name_per_platform():
    assert update.asset_name((0, 7, 0), "darwin", "arm64") == "Keepfold-0.7.0-macos-arm64.zip"
    assert update.asset_name((0, 7, 0), "linux", "x86_64") == "keepfold-0.7.0-linux-x86_64.tar.gz"
    assert update.asset_name((0, 7, 0), "darwin", "x86_64") is None
    assert update.asset_name((0, 7, 0), "linux", "aarch64") is None
    assert update.asset_name((0, 7, 0), "win32", "AMD64") == update.EXE


def _on(monkeypatch, plat, frozen, machine="arm64"):
    monkeypatch.setattr(sys, "platform", plat)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr("platform.machine", lambda: machine)


def test_app_mode_missing_asset_is_not_ready(monkeypatch):
    """CI attaches the Mac file 20-30 minutes after the release: until then, no update yet."""
    _on(monkeypatch, "darwin", True)
    got = update.latest(opener=fake({API: release()}))  # only KeepfoldSetup.exe so far
    assert got["waiting"] is True and got["version"] == (0, 2, 0)
    assert (got["exe_url"], got["sha_url"], got["exe_size"]) == (None, None, 0)
    got = update.latest(opener=fake({API: release(assets=(MAC,))}))  # no .sha256 yet
    assert got["waiting"] is True
    got = update.latest(opener=fake({API: release(assets=(MAC, MAC + ".sha256"))}))
    assert "waiting" not in got
    assert (got["exe_url"], got["sha_url"]) == (DL + MAC, DL + MAC + ".sha256")


def test_app_mode_still_refuses_links_that_are_not_this_release(monkeypatch):
    _on(monkeypatch, "linux", True, "x86_64")
    name = "keepfold-0.2.0-linux-x86_64.tar.gz"
    data = {**release(), "assets": [{"name": n, "browser_download_url": DL + "../x/" + n}
                                    for n in (name, name + ".sha256")]}
    with pytest.raises(update.UpdateError, match="not this release"):
        update.latest(opener=fake({API: data}))


def test_dev_mode_needs_no_platform_asset(monkeypatch):
    """A PyPI or uv install on a Mac offers the uv command, never an error."""
    _on(monkeypatch, "darwin", False)
    got = update.latest(opener=fake({API: release()}))
    assert got["version"] == (0, 2, 0) and "waiting" not in got and got["exe_url"] is None


def test_windows_missing_exe_still_raises(monkeypatch):
    _on(monkeypatch, "win32", True, "AMD64")
    with pytest.raises(update.UpdateError, match="has no KeepfoldSetup.exe"):
        update.latest(opener=fake({API: release(assets=(MAC, MAC + ".sha256"))}))


def test_dev_command_matches_the_install_guide(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "CHECKOUT", tmp_path)
    assert update.command("v0.2.0") == \
        f"uv tool install --force git+https://github.com/{REPO}@v0.2.0"
    (tmp_path / ".git").mkdir()
    assert update.command("v0.2.0") == \
        f'cd "{tmp_path}"; git pull; uv tool install --editable . --reinstall'


def test_fixed_tool_copy_updates_from_its_source_folder(tmp_path, monkeypatch):
    tool = tmp_path / "uv" / "tools" / "keepfold"
    here = tool / "Lib" / "site-packages" / "keepfold" / "update.py"
    monkeypatch.setattr(update, "HERE", here)
    monkeypatch.setattr(update, "CHECKOUT", here.parents[2])  # site-packages has no .git
    src = tmp_path / "K-Graph"
    (src / ".git").mkdir(parents=True)
    git_cmd = f"uv tool install --force git+https://github.com/{REPO}@v0.2.0"
    assert update.command("v0.2.0") == git_cmd  # no receipt yet
    tool.mkdir(parents=True)
    receipt = tool / "uv-receipt.toml"
    receipt.write_text("[tool]\nrequirements = [{ name = \"keepfold\", editable = "
                       f"'{src}' }}]\n", encoding="utf-8")
    assert update.command("v0.2.0") == git_cmd  # editable receipts are not a fixed copy
    receipt.write_text("[tool]\nrequirements = [{ name = \"keepfold\", directory = "
                       f"'{src}' }}]\n", encoding="utf-8")
    assert update.tool_source() == src
    assert update.command("v0.2.0") == \
        f'cd "{src}"; git pull; uv tool install --force --reinstall "{src}"'
    receipt.write_text("not toml [", encoding="utf-8")
    assert update.command("v0.2.0") == git_cmd


def test_launch_starts_the_installer_detached(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: seen.update(args=args, **k))
    update.launch(tmp_path / "KeepfoldSetup.exe", 4242, tmp_path / "Keepfold")
    assert seen["args"] == [str(tmp_path / "KeepfoldSetup.exe"), "--update", "--wait-pid", "4242",
                            "--dir", str(tmp_path / "Keepfold")]
    assert seen["close_fds"] and seen["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    if sys.platform == "win32":
        assert seen["creationflags"] == subprocess.DETACHED_PROCESS | \
            subprocess.CREATE_NEW_PROCESS_GROUP


# ---- Mac and Linux: Update now swaps in the new version (port DESIGN.md 5.1, 5.2) ------------


def test_page_fallback_names_platform_asset(monkeypatch):
    """The rate-limited fallback builds this computer's file, not KeepfoldSetup.exe."""
    routes = {API: 403, PAGE: (302, f"/{REPO}/releases/tag/v0.2.0")}
    _on(monkeypatch, "darwin", True)
    got = update.latest(opener=fake(routes))
    assert (got["exe_url"], got["sha_url"]) == (DL + MAC, DL + MAC + ".sha256")
    with pytest.raises(update.UpdateError) as e:  # both refused: no Windows file named
        update.latest(opener=fake({API: 403}))
    assert "KeepfoldSetup.exe" not in str(e.value) and PAGE in str(e.value)
    _on(monkeypatch, "linux", True, "x86_64")
    tgz = "keepfold-0.2.0-linux-x86_64.tar.gz"
    assert update.latest(opener=fake(routes))["exe_url"] == DL + tgz
    _on(monkeypatch, "darwin", False, "x86_64")  # no file is built for it: nothing to fetch
    assert update.latest(opener=fake(routes))["exe_url"] is None
    _on(monkeypatch, "win32", True, "AMD64")
    assert update.latest(opener=fake(routes))["exe_url"] == DL + update.EXE


def test_download_asset_fetches_this_platforms_file_under_its_cap(tmp_path, monkeypatch):
    _on(monkeypatch, "darwin", True)
    body = b"PK update zip " * 1000
    sha = hashlib.sha256(body).hexdigest()
    routes = {DL + MAC: body, DL + MAC + ".sha256": f"{sha}  {MAC}\n".encode()}
    i = info(exe_url=DL + MAC, sha_url=DL + MAC + ".sha256", exe_size=300 * 2**20)
    out = update.download_asset(i, tmp_path, opener=fake(routes))  # over Windows' 200 MB cap
    assert out == tmp_path / MAC and out.read_bytes() == body
    assert (tmp_path / (MAC + ".sha256")).read_text() == sha  # what install() verifies
    with pytest.raises(update.UpdateError, match="larger than 400 MB"):
        update.download_asset({**i, "exe_size": 401 * 2**20}, tmp_path, opener=fake(routes))


def test_verify_is_the_one_check_before_a_swap(tmp_path):
    f = tmp_path / "a.zip"
    f.write_bytes(EXE)
    update.verify(f, SHA.upper() + "\n")
    for path, sha in ((f, "0" * 64), (f, ""), (tmp_path / "gone.zip", SHA)):
        with pytest.raises(update.UpdateError, match="checksum"):
            update.verify(path, sha)


def _tree(app: Path) -> str:
    """Which copy a .app holds ("old" or "new"), from its executable."""
    return (app / "Contents" / "MacOS" / "Keepfold").read_text()


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """The frozen Mac app at tmp/Applications/Keepfold.app and its downloaded update zip, with
    fakes for ditto, codesign, ps, ending processes, the exchange and the reopen: never the real
    /Applications, processes or home."""
    from keepfold.app import bundle
    _on(monkeypatch, "darwin", True)
    monkeypatch.setattr(update, "current", lambda: "0.1.0")
    app = tmp_path / "Applications" / "Keepfold.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "Keepfold").write_text("old")
    monkeypatch.setattr(bundle, "app_path", lambda: app)
    monkeypatch.setattr(update, "_writable", lambda p: True)
    archive = tmp_path / "dl" / MAC
    archive.parent.mkdir()
    archive.write_bytes(b"zip")
    (archive.parent / (MAC + ".sha256")).write_text(hashlib.sha256(b"zip").hexdigest())
    s = SimpleNamespace(app=app, stage=app.parent / ".Keepfold-update", archive=archive, ran=[],
                        ps=[], ended=[], relaunched=[], codesign=(True, ""), end_works=True)

    def run(args, *a, **k):
        s.ran.append(args)
        if args[0] == "ditto":  # the zip holds Keepfold.app (ditto -c -k --keepParent)
            new = Path(args[-1]) / "Keepfold.app" / "Contents" / "MacOS"
            new.mkdir(parents=True)
            (new / "Keepfold").write_text("new")
            return True, ""
        if args[0] == "codesign":
            return s.codesign
        if args[0] == "ps":  # pid= right-aligned, then the full program path
            return True, "\n".join(f"{pid:>6} {path}" for pid, path in s.ps)
        pytest.fail(f"ran {args}")

    def end(pids):
        s.ended.append(pids)
        if s.end_works:
            s.ps[:] = [(p, path) for p, path in s.ps if p not in pids]

    def exchange(a, b):
        t = a.with_name("swap-tmp")
        os.rename(a, t)
        os.rename(b, a)
        os.rename(t, b)
    s.exchange = exchange
    monkeypatch.setattr(update, "_run", run)
    monkeypatch.setattr(update, "_end", end)
    monkeypatch.setattr(update, "_exchange", exchange)
    monkeypatch.setattr(update, "_relaunch", lambda cmd, pid: s.relaunched.append((cmd, pid)))
    return s


PREVIOUS = ("Library", "Application Support", "Keepfold", "previous")


def test_mac_update_swaps_keeps_the_previous_copy_and_reopens(mac):
    prev = Path.home().joinpath(*PREVIOUS)
    (prev / "Keepfold.app").mkdir(parents=True)  # the go-back copy of the update before
    assert update.install(mac.archive, info(), ()) == {}
    assert _tree(mac.app) == "new" and _tree(prev / "Keepfold.app") == "old"
    assert os.listdir(prev) == ["Keepfold.app"] and not mac.stage.exists()
    assert [a[0] for a in mac.ran] == ["ditto", "codesign", "ps"]
    assert mac.ran[0] == ["ditto", "-x", "-k", str(mac.archive), str(mac.stage)]  # keeps symlinks
    assert mac.ran[1] == ["codesign", "--verify", "--deep", "--strict",
                          str(mac.stage / "Keepfold.app")]
    assert mac.relaunched == [(["open", str(mac.app)], os.getpid())]


def test_swap_failure_restores_previous(mac, monkeypatch):
    """Review Focus 2: a swap that fails leaves one complete copy, the old one, in place."""
    def unsupported(a, b):  # a volume without APFS's one-step exchange
        raise OSError(45, "Operation not supported")
    monkeypatch.setattr(update, "_exchange", unsupported)
    with pytest.raises(update.UpdateError, match="nothing was changed"):
        update.install(mac.archive, info(), ())
    assert _tree(mac.app) == "old" and not mac.stage.exists() and mac.relaunched == []

    def no_start(cmd, pid):  # swapped, but the new copy cannot be started: the old one goes back
        raise OSError("no /bin/sh")
    monkeypatch.setattr(update, "_exchange", mac.exchange)
    monkeypatch.setattr(update, "_relaunch", no_start)
    with pytest.raises(update.UpdateError, match="nothing was changed"):
        update.install(mac.archive, info(), ())
    assert _tree(mac.app) == "old" and not mac.stage.exists()
    assert not Path.home().joinpath(*PREVIOUS).exists()

    mac.codesign = (False, "a sealed resource is missing or invalid")
    with pytest.raises(update.UpdateError, match="signature"):
        update.install(mac.archive, info(), ())
    assert _tree(mac.app) == "old" and not mac.stage.exists()
    mac.archive.write_bytes(b"changed on disk")  # verify runs before anything is unpacked
    n = len(mac.ran)
    with pytest.raises(update.UpdateError, match="checksum"):
        update.install(mac.archive, info(), ())
    assert len(mac.ran) == n and _tree(mac.app) == "old"


def test_swap_waits_for_running_programs(mac):
    """Review Focus 5: Keepfold programs still running from the bundle (an AI app's MCP server,
    the receiver) would load files from the new copy. They are listed, and ended only once the
    user agreed to exactly those; until then nothing is swapped."""
    cli = str(mac.app / "Contents" / "MacOS" / "keepfold-cli")
    mac.ps[:] = [(os.getpid(), str(mac.app / "Contents" / "MacOS" / "Keepfold")),  # this app
                 (501, cli), (502, cli), (77, "/usr/sbin/cfprefsd"),
                 (78, str(mac.app) + " 2.app/Contents/MacOS/Keepfold")]  # another app, a prefix
    got = update.install(mac.archive, info(), ())
    assert got["busy"] == [(501, cli), (502, cli)]
    assert got["note"].startswith("Keepfold is in use by 2 programs")
    assert _tree(mac.app) == "old" and not mac.stage.exists()
    assert mac.ended == [] and mac.relaunched == []
    assert update.install(mac.archive, info(), [501])["busy"] == got["busy"]  # agreed to one
    assert mac.ended == [] and _tree(mac.app) == "old"
    mac.end_works = False  # one ignores the end: listed again, nothing swapped
    assert update.install(mac.archive, info(), [501, 502])["busy"] == got["busy"]
    assert mac.ended == [[501, 502]] and _tree(mac.app) == "old"
    mac.end_works = True
    assert update.install(mac.archive, info(), [502, 501, 9]) == {}
    assert mac.ended[-1] == [501, 502]  # exactly those still running from the bundle
    assert _tree(mac.app) == "new" and mac.relaunched


def test_mac_without_write_access_cannot_replace(mac, monkeypatch):
    assert update.can_replace()
    monkeypatch.setattr(update, "_writable", lambda p: p != mac.app.parent)  # /Applications
    assert not update.can_replace()
    monkeypatch.setattr(update, "_writable", lambda p: p != mac.app)  # an admin's copy
    assert not update.can_replace()


def test_refuses_lower_version(mac):
    for v in ((0, 0, 9), (0, 1, 0), (0, 1)):  # lower, the same, the same written shorter
        with pytest.raises(update.UpdateError, match="not newer"):
            update.install(mac.archive, info(version=v), ())
    assert mac.ran == [] and _tree(mac.app) == "old" and mac.relaunched == []


def _tarball(tmp_path, ver, members=("keepfold-app", "keepfold")) -> Path:
    """keepfold-<ver>-linux-x86_64.tar.gz with one top folder, <ver>/, as build.sh makes it,
    and the .sha256 download_asset saves beside it."""
    src = tmp_path / "build" / ver
    src.mkdir(parents=True)
    for m in members:
        (src / m).write_text(ver)
    tgz = tmp_path / f"keepfold-{ver}-linux-x86_64.tar.gz"
    with tarfile.open(tgz, "w:gz") as t:
        t.add(src, arcname=ver)
    (tmp_path / (tgz.name + ".sha256")).write_text(hashlib.sha256(tgz.read_bytes()).hexdigest())
    return tgz


def test_linux_update_keeps_one_previous(tmp_path, monkeypatch):
    """The new version unpacks beside the old ones (through a dot-folder the launcher ignores);
    the one before stays for going back, older ones go unless a process still runs from one."""
    _on(monkeypatch, "linux", True, "x86_64")
    monkeypatch.setattr(update, "current", lambda: "0.9.0")
    root = Path.home() / ".local" / "share" / "keepfold" / "app"  # $HOME's, as the launcher's
    for v in ("0.6.0", "0.7.0", "0.9.0", "0.7.0rc1"):  # rc1: not a version, never deleted
        (root / v).mkdir(parents=True)
        (root / v / "keepfold-app").write_text(v)
    launcher = tmp_path / "keepfold-app-launcher"
    monkeypatch.setattr(update, "LAUNCHER", launcher)
    assert not update.can_replace()  # not the .deb's app: no launcher to run the new copy
    launcher.write_text("#!/bin/bash\n")
    assert update.can_replace()
    monkeypatch.setattr(update, "_proc_exes", lambda: [str(root / "0.9.0" / "keepfold-app"),
                                                        str(root / "0.6.0" / "keepfold"),
                                                        "/usr/bin/bash"])
    relaunched = []
    monkeypatch.setattr(update, "_relaunch", lambda cmd, pid: relaunched.append((cmd, pid)))
    tgz = _tarball(tmp_path, "0.10.0")
    assert update.install(tgz, info(tag="v0.10.0", version=(0, 10, 0)), ()) == {}
    # 0.10.0 sorts after 0.9.0 (a version, not a string); 0.7.0 is unused and older than the
    # one before; 0.6.0 still runs an AI app's Keepfold connection
    assert sorted(os.listdir(root)) == ["0.10.0", "0.6.0", "0.7.0rc1", "0.9.0"]
    assert (root / "0.10.0" / "keepfold-app").read_text() == "0.10.0"
    assert relaunched == [([str(launcher)], os.getpid())]


def test_linux_update_refuses_a_tarball_that_is_not_the_version(tmp_path, monkeypatch):
    _on(monkeypatch, "linux", True, "x86_64")
    monkeypatch.setattr(update, "current", lambda: "0.9.0")
    monkeypatch.setattr(update, "_proc_exes", lambda: [])
    monkeypatch.setattr(update, "_relaunch", lambda *a: pytest.fail("restarted"))
    root = Path.home() / ".local" / "share" / "keepfold" / "app"
    tgz = _tarball(tmp_path, "0.10.0", members=("README",))  # no keepfold-app inside
    with pytest.raises(update.UpdateError, match="nothing was changed"):
        update.install(tgz, info(tag="v0.10.0", version=(0, 10, 0)), ())
    evil = tmp_path / "evil" / "x.tar.gz"  # a member outside the folder: never written
    evil.parent.mkdir()
    with tarfile.open(evil, "w:gz") as t:
        t.add(tgz, arcname="../../escaped")
    (evil.parent / "x.tar.gz.sha256").write_text(hashlib.sha256(evil.read_bytes()).hexdigest())
    with pytest.raises(update.UpdateError, match="nothing was changed"):
        update.install(evil, info(tag="v0.10.0", version=(0, 10, 0)), ())
    assert os.listdir(root) == [] and not (root.parent / "escaped").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="APFS's renamex_np is macOS only")
def test_exchange_swaps_two_folders_in_one_step(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "x").write_text("a")
    (b / "x").write_text("b")
    update._exchange(a, b)
    assert (a / "x").read_text() == "b" and (b / "x").read_text() == "a"


def test_apt_remove_runs_pkexec_with_no_window(monkeypatch):
    """Uninstall from Settings on Linux: one argv list, never a shell, never a console window."""
    seen = {}

    def run(args, **kw):
        seen.update(args=args, **kw)
        return SimpleNamespace(returncode=126, stdout="", stderr="Request dismissed")
    monkeypatch.setattr(subprocess, "run", run)
    assert update._apt_remove() == (False, "Request dismissed")
    assert seen["args"] == ["pkexec", "apt-get", "remove", "-y", "keepfold"]
    assert seen["creationflags"] == update.NO_WINDOW and not seen.get("shell")


def test_trash_uses_nsfilemanager(monkeypatch, tmp_path):
    """The Mac .app goes to the Trash through NSFileManager (Finder's Put Back works); a fake
    Foundation stands in for pyobjc."""
    got = []
    ok = [True]
    fm = SimpleNamespace(trashItemAtURL_resultingItemURL_error_=lambda url, r, e: (
        got.append(url) or (ok[0], None, None if ok[0] else "denied")))
    fake = SimpleNamespace(NSFileManager=SimpleNamespace(defaultManager=lambda: fm),
                           NSURL=SimpleNamespace(fileURLWithPath_=lambda p: ("url", p)))
    monkeypatch.setitem(sys.modules, "Foundation", fake)
    update._trash(tmp_path / "Keepfold.app")
    assert got == [("url", str(tmp_path / "Keepfold.app"))]
    ok[0] = False
    with pytest.raises(OSError, match="denied"):
        update._trash(tmp_path / "Keepfold.app")
