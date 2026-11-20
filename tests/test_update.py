"""cortex.update: the latest-release check, the checked download and the installer launch, with a
fake GitHub (an injected opener; no network)."""

import hashlib
import io
import json
import os
import subprocess
import sys
import threading
from urllib.error import HTTPError, URLError

import pytest

from cortex import update
from cortex.cli import REPO

API = f"{update.API}/repos/{REPO}/releases/latest"
DL = f"https://github.com/{REPO}/releases/download/v0.2.0/"
EXE = b"MZ new installer " * 1000
SHA = hashlib.sha256(EXE).hexdigest()


class Resp(io.BytesIO):
    def __init__(self, body: bytes, headers: dict | None = None):
        super().__init__(body)
        self.headers = headers or {}


def release(tag="v0.2.0", assets=("CortexSetup.exe", "CortexSetup.exe.sha256"), **extra):
    return {"tag_name": tag, "html_url": "https://evil.example/notes", "prerelease": False,
            "draft": False, **extra,
            "assets": [{"name": n, "browser_download_url": DL + n, "size": len(EXE)}
                       for n in assets]}


def moved(base: str) -> dict:
    """A release whose asset links point under `base` instead of its own tag."""
    names = ("CortexSetup.exe", "CortexSetup.exe.sha256")
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
            raise HTTPError(req.full_url, r[0], "x", {"Location": r[1]}, None)
        return Resp(r if isinstance(r, bytes) else json.dumps(r).encode())
    open_.seen = seen
    return open_


def info(**over):
    return {"tag": "v0.2.0", "version": (0, 2, 0), "notes_url": "n",
            "exe_url": DL + "CortexSetup.exe", "sha_url": DL + "CortexSetup.exe.sha256",
            "exe_size": len(EXE), **over}


def files(routes=None, sha=SHA):
    return {DL + "CortexSetup.exe": EXE,
            DL + "CortexSetup.exe.sha256": f"{sha}  CortexSetup.exe\n".encode(), **(routes or {})}


# ---- latest ---------------------------------------------------------------------------------


def test_latest_reads_the_release_with_a_generic_request():
    op = fake({API: release()})
    got = update.latest(opener=op)
    assert got == {"tag": "v0.2.0", "version": (0, 2, 0),
                   "notes_url": f"https://github.com/{REPO}/releases/tag/v0.2.0",  # never html_url
                   "exe_url": DL + "CortexSetup.exe", "sha_url": DL + "CortexSetup.exe.sha256",
                   "exe_size": len(EXE)}
    (req,) = op.seen
    assert req["timeout"] == 10
    assert req["headers"] == {"Accept": "application/vnd.github+json",
                              "User-agent": "cortex-updater"}


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
    (release(assets=("CortexSetup.exe",)), r"\.sha256"),
    (release(assets=("CortexSetup.exe.sha256", "other.zip")), "CortexSetup.exe"),
    ([1, 2], "could not read"),
    # links that are not exactly this release's files (another tag, a `..` path)
    (moved(DL.replace("v0.2.0", "v0.1.0")), "not this release"),
    (moved(DL + "../../../x/y/"), "not this release"),
])
def test_latest_refuses_what_cannot_be_installed(data, match):
    with pytest.raises(update.UpdateError, match=match):
        update.latest(opener=fake({API: data}))


@pytest.mark.parametrize("route,match", [(403, "limiting"), (429, "limiting"),
                                         (404, "no Cortex release"), (500, "answered 500"),
                                         (None, "internet connection"), (b"{not json", "read")])
def test_latest_errors_say_what_happened(route, match):
    with pytest.raises(update.UpdateError, match=match):
        update.latest(opener=fake({} if route is None else {API: route}))


PAGE = f"https://github.com/{REPO}/releases/latest"


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
    (None, "limiting"),  # the page is unreachable too
    (b"<html>", "limiting"),  # no redirect: no release
    ((302, "https://github.com/someone/else/releases/tag/v9.9.9"), "limiting"),
    ((302, f"https://evil.example/{REPO}/releases/tag/v9.9.9"), "limiting"),
    ((302, f"https://github.com/{REPO}/releases/tag/v0.3.0-rc1"), "not a release version"),
    ((302, f"https://github.com/{REPO}/releases/tag/..%2F..%2Fx"), "not a release version"),
])
def test_the_latest_page_takes_only_this_repos_release_tags(page, match):
    routes = {API: 403, **({} if page is None else {PAGE: page})}
    with pytest.raises(update.UpdateError, match=match):
        update.latest(opener=fake(routes))


def test_update_error_is_a_value_error():  # the app shows its text, not a traceback
    assert issubclass(update.UpdateError, ValueError)


# ---- download -------------------------------------------------------------------------------


def test_download_checks_the_hash_and_reports_progress(tmp_path):
    seen = []
    out = update.download(info(), tmp_path / "d", lambda g, t: seen.append((g, t)),
                          threading.Event(), opener=fake(files()))
    assert out == tmp_path / "d" / "CortexSetup.exe" and out.read_bytes() == EXE
    assert sorted(os.listdir(tmp_path / "d")) == ["CortexSetup.exe", "CortexSetup.exe.sha256"]
    assert (tmp_path / "d" / "CortexSetup.exe.sha256").read_text() == SHA  # no .part left
    assert seen and seen[-1][1] == len(EXE)


def test_an_installer_already_there_is_not_fetched_again(tmp_path):
    update.download(info(), tmp_path, opener=fake(files()))
    op = fake(files())
    assert update.download(info(), tmp_path, opener=op).read_bytes() == EXE
    assert [s["url"] for s in op.seen] == [DL + "CortexSetup.exe.sha256"]  # only the checksum
    (tmp_path / "CortexSetup.exe").write_bytes(b"MZ changed")  # not the release's: fetched again
    op = fake(files())
    assert update.download(info(), tmp_path, opener=op).read_bytes() == EXE and len(op.seen) == 2


def test_prepared_is_a_newer_installer_that_still_matches_its_checksum(monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(update, "current", lambda: "0.2.0")
    tmp = Path.home() / ".cortex" / "tmp"
    assert update.prepared() is None
    for ver, body, sha in (("0.1.9", EXE, SHA), ("0.2.0", EXE, SHA), ("0.3.0", EXE, SHA),
                           ("0.4.0", b"MZ half", SHA), ("0.5.0", EXE, None)):
        (tmp / f"update-{ver}").mkdir(parents=True)
        (tmp / f"update-{ver}" / "CortexSetup.exe").write_bytes(body)
        if sha:
            (tmp / f"update-{ver}" / "CortexSetup.exe.sha256").write_text(sha)
    (tmp / "update-junk").mkdir()
    assert update.prepared() == tmp / "update-0.3.0"  # older, same, torn, unchecked: not kept


def test_a_hash_mismatch_deletes_the_file(tmp_path):
    with pytest.raises(update.UpdateError, match="checksum"):
        update.download(info(), tmp_path, opener=fake(files(sha="0" * 64)))
    assert os.listdir(tmp_path) == []


def test_an_unreadable_checksum_file_stops_before_the_download(tmp_path):
    op = fake(files({DL + "CortexSetup.exe.sha256": b"<html>not a hash</html>"}))
    with pytest.raises(update.UpdateError, match="checksum file"):
        update.download(info(), tmp_path / "d", opener=op)
    assert not (tmp_path / "d").exists() and len(op.seen) == 1


def test_redirects_only_to_githubusercontent_over_https(tmp_path):
    cdn = "https://objects.githubusercontent.com/x/CortexSetup.exe"
    op = fake(files({DL + "CortexSetup.exe": (302, cdn), cdn: EXE}))
    assert update.download(info(), tmp_path, opener=op).read_bytes() == EXE
    cdn2 = "https://release-assets.githubusercontent.com/y/CortexSetup.exe"
    op = fake(files({DL + "CortexSetup.exe": (302, cdn2), cdn2: EXE}))
    assert update.download(info(), tmp_path / "ok2", opener=op).read_bytes() == EXE
    for bad in ("https://evil.example/CortexSetup.exe", "http://objects.githubusercontent.com/x",
                "https://githubusercontent.com.evil.example/x",
                "https://raw.githubusercontent.com/someone/repo/main/CortexSetup.exe"):
        op = fake(files({DL + "CortexSetup.exe": (302, bad), bad: EXE}))
        with pytest.raises(update.UpdateError, match="not GitHub"):
            update.download(info(), tmp_path / "bad", opener=op)
        assert bad not in [r["url"] for r in op.seen]
        assert os.listdir(tmp_path / "bad") == []


def test_only_this_repos_release_files(tmp_path):
    for url in ("https://github.com/someone/else/releases/download/v1/CortexSetup.exe",
                f"http://github.com/{REPO}/releases/download/v1/CortexSetup.exe", None):
        with pytest.raises(update.UpdateError, match="not a Cortex release"):
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


def test_dest_dir_is_under_cortex_tmp(tmp_path):
    assert update.dest_dir(info()) == update.Path.home() / ".cortex" / "tmp" / "update-0.2.0"


# ---- mode, command, launch ------------------------------------------------------------------


def test_mode_installer_when_a_parent_holds_the_marker(tmp_path, monkeypatch):
    py = tmp_path / "Cortex" / "tools" / "cortex-learner" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(py))
    assert update.mode() == "dev" and update.install_dir() is None
    (tmp_path / "Cortex" / update.MARKER).write_text("x")
    assert update.mode() == "installer" and update.install_dir() == tmp_path / "Cortex"


def test_dev_command_matches_the_install_guide(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "CHECKOUT", tmp_path)
    assert update.command("v0.2.0") == \
        f"uv tool install --force git+https://github.com/{REPO}@v0.2.0"
    (tmp_path / ".git").mkdir()
    assert update.command("v0.2.0") == \
        f'cd "{tmp_path}"; git pull; uv tool install --editable . --reinstall'


def test_fixed_tool_copy_updates_from_its_source_folder(tmp_path, monkeypatch):
    tool = tmp_path / "uv" / "tools" / "cortex-learner"
    here = tool / "Lib" / "site-packages" / "cortex" / "update.py"
    monkeypatch.setattr(update, "HERE", here)
    monkeypatch.setattr(update, "CHECKOUT", here.parents[2])  # site-packages has no .git
    src = tmp_path / "K-Graph"
    (src / ".git").mkdir(parents=True)
    git_cmd = f"uv tool install --force git+https://github.com/{REPO}@v0.2.0"
    assert update.command("v0.2.0") == git_cmd  # no receipt yet
    tool.mkdir(parents=True)
    receipt = tool / "uv-receipt.toml"
    receipt.write_text("[tool]\nrequirements = [{ name = \"cortex-learner\", editable = "
                       f"'{src}' }}]\n", encoding="utf-8")
    assert update.command("v0.2.0") == git_cmd  # editable receipts are not a fixed copy
    receipt.write_text("[tool]\nrequirements = [{ name = \"cortex-learner\", directory = "
                       f"'{src}' }}]\n", encoding="utf-8")
    assert update.tool_source() == src
    assert update.command("v0.2.0") == \
        f'cd "{src}"; git pull; uv tool install --force --reinstall "{src}"'
    receipt.write_text("not toml [", encoding="utf-8")
    assert update.command("v0.2.0") == git_cmd


def test_launch_starts_the_installer_detached(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: seen.update(args=args, **k))
    update.launch(tmp_path / "CortexSetup.exe", 4242, tmp_path / "Cortex")
    assert seen["args"] == [str(tmp_path / "CortexSetup.exe"), "--update", "--wait-pid", "4242",
                            "--dir", str(tmp_path / "Cortex")]
    assert seen["close_fds"] and seen["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    if sys.platform == "win32":
        assert seen["creationflags"] == subprocess.DETACHED_PROCESS | \
            subprocess.CREATE_NEW_PROCESS_GROUP
