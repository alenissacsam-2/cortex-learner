"""The local-model recommendation by hardware kind; the free-space check, winget's flags and
finding git right after it installs (nothing installs or downloads here)."""

import io
import os
import shutil
import stat
import subprocess
import sys
import threading
import urllib.error
import zipfile
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest

from keepfold import session, update
from keepfold.app import actions, targets, wizard


def test_without_an_nvidia_card_the_recommendation_stops_at_4b():
    assert wizard.recommend(16, "ram") == "4b"
    assert wizard.recommend(4, "ram") == "2b"
    assert wizard.recommend(12, "gpu") == "9b"
    assert wizard.recommend(None, "ram") == "2b"


def test_free_space_ok_says_both_sizes_and_blocks_below_size_plus_1gb(tmp_path, monkeypatch):
    """Deps A6: "Needs about 2.0 GB; 1.1 GB free on C:." blocks; enough space passes; the
    nearest existing folder counts; a failed check never blocks."""
    du = namedtuple("du", "total used free")
    seen = []
    monkeypatch.setattr(shutil, "disk_usage", lambda p: (seen.append(p), du(0, 0, 1.1e9))[1])
    ok, msg = wizard.free_space_ok(tmp_path / "not" / "yet", 2 * 10**9)
    where = tmp_path.drive or "this disk"
    assert not ok and msg.startswith(f"Needs about 2.0 GB; 1.1 GB free on {where}.")
    assert seen == [tmp_path.absolute()]
    assert wizard.free_space_ok(tmp_path, 70 * 10**6) == (
        True, f"Needs about 70 MB; 1.1 GB free on {where}.")
    assert wizard.free_space_ok(tmp_path, 10**8)[0] is True  # 1.1 GB >= 0.1 GB + 1 GB
    assert wizard.free_space_ok(tmp_path, 2 * 10**8)[0] is False

    def broken(p):
        raise OSError("no such drive")
    monkeypatch.setattr(shutil, "disk_usage", broken)
    assert wizard.free_space_ok(tmp_path, 10**12) == (True, "")


def test_ollama_install_never_asks_and_names_package_and_license(monkeypatch):
    """Deps A1: winget has no console in the app: --disable-interactivity, and the agreements
    are accepted only with a License link to show beside the button."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(targets, "_which", lambda c: "winget" if c == "winget" else None)
    plan = wizard.install_plan()
    assert plan["run"][:5] == ["winget", "install", "--id", "Ollama.Ollama", "-e"]
    assert {"--disable-interactivity", "--accept-package-agreements",
            "--accept-source-agreements"} <= set(plan["run"])
    assert "Ollama.Ollama" in plan["source"] and plan["size"] == "about 1 GB"
    assert plan["license"].startswith("https://")


@pytest.mark.skipif(sys.platform != "win32", reason="Git for Windows folders")
def test_git_exe_finds_git_where_the_installer_put_it(tmp_path, monkeypatch):
    """Deps A2: PATH first, then Program Files and the per-user Programs folder, then PATH as
    the registry has it now; the vault's git calls use what it finds."""
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "L"))
    monkeypatch.chdir(tmp_path)
    assert session.git_exe() is None
    exe = tmp_path / "L" / "Programs" / "Git" / "cmd" / "git.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert session.git_exe().lower() == str(exe).lower()
    exe.unlink()
    reg = tmp_path / "reg"
    reg.mkdir()
    (reg / "git.exe").write_bytes(b"")
    monkeypatch.setattr(session, "extra_path", lambda: f";{reg};")
    assert session.git_exe().lower() == str(reg / "git.exe").lower()
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: (
        ran.append(cmd), subprocess.CompletedProcess(cmd, 0, "", ""))[1])
    session._git(tmp_path, "status")
    assert ran[0][0].lower() == str(reg / "git.exe").lower()
    assert ran[0][1:] == ["-C", str(tmp_path), "status"]


def test_mac_git_plan_is_xcode_select(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert wizard.git_plan() == {
        "run": ["xcode-select", "--install"], "source": "Apple's Command Line Tools",
        "size": "about 1 GB", "page": wizard.GIT_PAGE, "license": wizard.GIT_LICENSE}


def test_mac_install_git_opens_the_os_dialog(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    ran = []
    monkeypatch.setattr(targets, "_run", lambda args, timeout=300: (ran.append(args),
                                                                    (True, ""))[1])
    out = wizard.install_git(None)
    assert ran == [["xcode-select", "--install"]]
    assert out == {"ok": True, "lines": [
        "macOS is asking to install the Command Line Tools; click Install, then come back "
        "and click Check again."]}


def test_linux_git_plan_shows_apt(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    plan = wizard.git_plan()
    assert plan["run"] is None and plan["show"] == "sudo apt install git"
    assert plan["page"] == wizard.GIT_PAGE and plan["license"] == wizard.GIT_LICENSE


# ---- hardware off Windows (Task 8) ----------------------------------------------------------


@pytest.fixture
def no_nvidia(monkeypatch):
    monkeypatch.setattr(targets, "_which", lambda c: None)
    wizard.hardware.cache_clear()
    yield
    wizard.hardware.cache_clear()


def test_apple_silicon_counts_unified_memory(monkeypatch, no_nvidia):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(wizard.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(wizard, "_ram_gb", lambda: 16.0)
    hw = wizard.hardware()
    assert hw == {"kind": "gpu", "gb": 16.0,
                  "label": "16 GB of unified memory (Apple Silicon)"}
    assert wizard.recommend(hw["gb"], hw["kind"]) == "9b"


def test_amd_vram_from_sysfs(tmp_path, monkeypatch, no_nvidia):
    f = tmp_path / "card0" / "device" / "mem_info_vram_total"
    f.parent.mkdir(parents=True)
    f.write_text("8589934592\n")
    (tmp_path / "card1-DP-1").mkdir()  # a connector, not a card: no memory file
    assert wizard._amd_vram_gb(tmp_path) == 8.0
    assert wizard._amd_vram_gb(tmp_path / "none") is None
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(wizard, "_amd_vram_gb", lambda: 8.0)
    assert wizard.hardware() == {"kind": "gpu", "gb": 8.0,
                                 "label": "8 GB of graphics memory (AMD)"}


def test_amd_vram_under_2gb_is_an_apu_carve_out_so_the_ram_rule_applies(
        tmp_path, monkeypatch, no_nvidia):
    f = tmp_path / "card0" / "device" / "mem_info_vram_total"
    f.parent.mkdir(parents=True)
    f.write_text(str(512 * 2**20))  # 512 MB carved out of RAM
    assert wizard._amd_vram_gb(tmp_path) is None
    real = wizard._amd_vram_gb
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(wizard, "_amd_vram_gb", lambda: real(tmp_path))
    monkeypatch.setattr(wizard, "_ram_gb", lambda: 16.0)
    assert wizard.hardware()["kind"] == "ram"
    f.write_text(str(2 * 2**30))  # 2 GB: a real card
    assert real(tmp_path) == 2.0


def test_ram_label_off_windows(monkeypatch, no_nvidia):
    monkeypatch.setattr(wizard, "_ram_gb", lambda: 32.0)
    monkeypatch.setattr(wizard, "_amd_vram_gb", lambda: None)
    monkeypatch.setattr(sys, "platform", "linux")
    hw = wizard.hardware()
    assert hw["kind"] == "ram" and "NVIDIA" not in hw["label"]
    assert hw["label"] == "32 GB of memory (no graphics memory found)"
    wizard.hardware.cache_clear()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(wizard.platform, "machine", lambda: "x86_64")  # an Intel Mac
    assert "NVIDIA" not in wizard.hardware()["label"]
    wizard.hardware.cache_clear()
    monkeypatch.setattr(sys, "platform", "win32")
    assert wizard.hardware()["label"] == "32 GB of memory (no NVIDIA graphics card found)"


# ---- Ollama without Homebrew or sudo (Task 9) -----------------------------------------------


def _job():
    lines = []
    return SimpleNamespace(say=lines.append, cancelled=threading.Event(), lines=lines)


def _zip(path, members):
    """members: (name, data, mode); a mode with S_IFLNK makes `data` a symlink's target."""
    with zipfile.ZipFile(path, "w") as z:
        for name, data, mode in members:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            z.writestr(info, data)
    return path


def _fake_download(monkeypatch, archive, sums_text):
    got = []

    def fake(url, dest, job):
        got.append(url)
        if url.endswith(wizard.OLLAMA_SUMS):
            dest.write_text(sums_text)
        else:
            dest.write_bytes(archive.read_bytes())
        return dest
    monkeypatch.setattr(wizard, "_download", fake)
    return got


def _app_zip(tmp_path):
    return _zip(tmp_path / "src.zip", [
        ("Ollama.app/Contents/Info.plist", b"<plist/>", stat.S_IFREG | 0o644),
        ("Ollama.app/Contents/Resources/ollama", b"#!bin", stat.S_IFREG | 0o755),
        ("Ollama.app/Contents/MacOS/Ollama", b"#!app", stat.S_IFREG | 0o755)])


def test_mac_plan_without_brew_fetches(monkeypatch, home):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(targets, "_which", lambda c: None)
    plan = wizard.install_plan()
    assert plan["run"] is None and plan["fetch"].endswith("Ollama-darwin.zip")
    assert plan["fetch"] == wizard.OLLAMA_RELEASE + "Ollama-darwin.zip"
    assert plan["sums"] == wizard.OLLAMA_RELEASE + "sha256sum.txt"
    assert plan["to"] == str(home / "Applications") and plan["license"]
    assert plan["source"] == "github.com/ollama/ollama releases"
    assert wizard.install_dir() == home / "Applications"
    monkeypatch.setattr(targets, "_which", lambda c: "/opt/homebrew/bin/brew")
    plan = wizard.install_plan()
    assert plan["run"] == ["brew", "install", "ollama"] and plan["license"]
    assert "fetch" not in plan


def test_fetch_ollama_checks_sum(tmp_path, monkeypatch, home):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(targets, "_which", lambda c: None)
    plan = wizard.install_plan()
    _fake_download(monkeypatch, _app_zip(tmp_path), "0" * 64 + "  ./Ollama-darwin.zip\n")
    with pytest.raises(ValueError, match="checksum"):
        wizard.fetch_ollama(_job(), plan)
    to = home / "Applications"
    assert not to.exists() or not any(to.iterdir())
    assert not any((home / ".keepfold" / "tmp").iterdir())  # the download is deleted
    _fake_download(monkeypatch, _app_zip(tmp_path), "1" * 64 + "  ./ollama-darwin.tgz\n")
    with pytest.raises(ValueError, match="checksum"):  # no line for the asset: refused too
        wizard.fetch_ollama(_job(), plan)


def test_fetch_ollama_mac_unzips_the_app_keeping_exec_bits(tmp_path, monkeypatch, home):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(targets, "_which", lambda c: None)
    monkeypatch.setattr(targets, "_run", lambda *a, **k: pytest.fail("nothing may run"))
    archive = _app_zip(tmp_path)
    sums = f"{'2' * 64}  ./ollama-darwin.tgz\n{update._sha256(archive)}  ./Ollama-darwin.zip\n"
    got = _fake_download(monkeypatch, archive, sums)
    modes, chmod = {}, os.chmod
    monkeypatch.setattr(os, "chmod", lambda p, m, **k: (
        modes.__setitem__(os.path.basename(p), m), chmod(p, m, **k))[1])
    out = wizard.fetch_ollama(_job(), wizard.install_plan())
    assert out["ok"] and out["lines"]
    assert got == [wizard.OLLAMA_RELEASE + "sha256sum.txt",
                   wizard.OLLAMA_RELEASE + "Ollama-darwin.zip"]
    app = home / "Applications" / "Ollama.app"
    exe = app / "Contents" / "Resources" / "ollama"
    assert exe.read_bytes() == b"#!bin" and (app / "Contents" / "Info.plist").is_file()
    assert modes["ollama"] == 0o755 and modes["Ollama"] == 0o755
    assert modes["Info.plist"] == 0o644
    if os.name != "nt":  # sys.platform is faked here
        assert os.stat(exe).st_mode & 0o111
    assert not any((home / ".keepfold" / "tmp").iterdir())  # the archive is not kept
    assert wizard.ollama_exe() == str(exe)


def test_unzip_refuses_a_path_outside_the_folder(tmp_path):
    to = tmp_path / "to"
    bad = _zip(tmp_path / "slip.zip", [("ok.txt", b"x", stat.S_IFREG | 0o644),
                                        ("../evil.txt", b"x", stat.S_IFREG | 0o644)])
    with pytest.raises(ValueError, match="outside"):
        wizard._unpack(bad, to)
    assert not (tmp_path / "evil.txt").exists() and not (to / "ok.txt").exists()
    link = _zip(tmp_path / "link.zip", [("a/up", b"../../..", stat.S_IFLNK | 0o777)])
    with pytest.raises(ValueError, match="outside"):
        wizard._unpack(link, to)
    assert not (to / "a" / "up").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin on Windows")
def test_unzip_keeps_symlinks_inside_the_app(tmp_path):
    to = tmp_path / "to"
    z = _zip(tmp_path / "fw.zip", [
        ("F.framework/Versions/A/lib", b"L", stat.S_IFREG | 0o755),
        ("F.framework/Versions/Current", b"A", stat.S_IFLNK | 0o777)])
    wizard._unpack(z, to)
    cur = to / "F.framework" / "Versions" / "Current"
    assert cur.is_symlink() and os.readlink(cur) == "A" and (cur / "lib").read_bytes() == b"L"


def test_start_cmd_user_applications(monkeypatch, home):
    monkeypatch.setattr(sys, "platform", "darwin")
    app = home / "Applications" / "Ollama.app"
    app.mkdir(parents=True)
    assert wizard._ollama_start_cmd() == ["open", "-a", str(app)]


def test_install_ollama_fetches_when_the_plan_says_so(monkeypatch):
    monkeypatch.setattr(wizard, "install_plan", lambda: {"run": None, "fetch": "u"})
    seen = []
    monkeypatch.setattr(wizard, "fetch_ollama", lambda job, plan: (
        seen.append(plan), {"ok": True, "lines": []})[1])
    assert wizard.install_ollama(_job())["ok"] and seen == [{"run": None, "fetch": "u"}]


def test_download_follows_only_github_redirects(tmp_path, monkeypatch):
    def redirect(to):
        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 302, "Found", {"Location": to}, None)
        return opener
    url = wizard.OLLAMA_RELEASE + "Ollama-darwin.zip"
    monkeypatch.setattr(update, "_opener", redirect("https://evil.example/Ollama-darwin.zip"))
    with pytest.raises(ValueError, match="not GitHub"):
        wizard._download(url, tmp_path / "x.zip", _job())
    with pytest.raises(ValueError, match="not an Ollama release"):
        wizard._download("https://evil.example/x.zip", tmp_path / "x.zip", _job())
    cdn = "https://release-assets.githubusercontent.com/x"

    class R(io.BytesIO):
        headers = {"Content-Length": str(3 << 20)}

    def opener(req, timeout):
        if req.full_url == cdn:
            return R(b"z" * (3 << 20))
        raise urllib.error.HTTPError(req.full_url, 302, "Found", {"Location": cdn}, None)
    monkeypatch.setattr(update, "_opener", opener)
    job = _job()
    out = wizard._download(url, tmp_path / "x.zip", job)
    assert out.read_bytes() == b"z" * (3 << 20) and len(job.lines) >= 3
    job.cancelled.set()
    with pytest.raises(actions.Cancelled):
        wizard._download(url, tmp_path / "y.zip", job)
    assert not (tmp_path / "y.zip").exists() and not (tmp_path / "y.zip.part").exists()


# ---- Ollama on Linux: the release tarball, unpacked whole, and a systemd user unit ---------


def _linux(monkeypatch, machine="x86_64"):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(wizard.platform, "machine", lambda: machine)
    monkeypatch.setattr(targets, "_which", lambda c: None)


def _fake_unpack(monkeypatch, fail=False):
    """The tarball unpack (zstandard is Linux-only): writes bin/ollama, or a partial copy and
    then fails."""
    def fake(src, to):
        (to / "bin").mkdir(parents=True)
        (to / "lib").mkdir()
        (to / "lib" / "half.so").write_bytes(b"half")
        if fail:
            raise ValueError("Could not unpack: truncated")
        (to / "bin" / "ollama").write_bytes(b"#!new")
    monkeypatch.setattr(wizard, "_unpack", fake)


def _tarball(tmp_path):
    archive = tmp_path / "a.tar.zst"
    archive.write_bytes(b"tarball")
    return archive, f"{update._sha256(archive)}  ./ollama-linux-amd64.tar.zst\n"


def test_linux_plan_fetches_tarball(monkeypatch, home):
    _linux(monkeypatch)
    plan = wizard.install_plan()
    assert plan["run"] is None and "show" not in plan
    assert plan["fetch"] == wizard.OLLAMA_RELEASE + "ollama-linux-amd64.tar.zst"
    assert plan["sums"] == wizard.OLLAMA_RELEASE + "sha256sum.txt"
    assert plan["to"] == str(home / ".local" / "share" / "keepfold" / "ollama")
    assert plan["size"] == "about 1.7 GB" and plan["license"]
    assert wizard.install_dir() == home / ".local" / "share"
    assert wizard._ollama_size() == ("about 1.7 GB", 8 * 10**9)  # the free-space figure


def test_linux_on_arm_keeps_showing_the_install_command(monkeypatch, home):
    _linux(monkeypatch, "aarch64")
    plan = wizard.install_plan()
    assert plan["run"] is None and plan["show"] == wizard.LINUX_INSTALL and "fetch" not in plan
    with pytest.raises(ValueError, match="Install Ollama yourself"):
        wizard.install_ollama(_job())


def test_ollama_sizes_per_os(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert wizard._ollama_size() == ("about 250 MB", 10**9)
    monkeypatch.setattr(sys, "platform", "win32")
    assert wizard._ollama_size() == ("about 1 GB", 10**9)


def test_fetch_ollama_linux_checksum_mismatch_installs_nothing(tmp_path, monkeypatch, home):
    _linux(monkeypatch)
    monkeypatch.setattr(wizard, "_unpack", lambda *a: pytest.fail("unpacked a bad download"))
    monkeypatch.setattr(targets, "_run", lambda *a, **k: pytest.fail("nothing may run"))
    archive, _ = _tarball(tmp_path)
    _fake_download(monkeypatch, archive, "0" * 64 + "  ./ollama-linux-amd64.tar.zst\n")
    plan = wizard.install_plan()
    with pytest.raises(ValueError, match="checksum"):
        wizard.fetch_ollama(_job(), plan)
    assert not os.path.exists(plan["to"])
    assert not any((home / ".keepfold" / "tmp").iterdir())
    assert not wizard._ollama_unit().exists()


def test_fetch_ollama_linux_writes_user_unit(tmp_path, monkeypatch, home):
    from keepfold.app import status
    _linux(monkeypatch)
    monkeypatch.setattr(status, "_systemd", lambda: True)
    ran = []
    monkeypatch.setattr(targets, "_run",
                        lambda args, timeout=300: (ran.append(args), (True, ""))[1])
    _fake_download(monkeypatch, *_tarball(tmp_path))
    _fake_unpack(monkeypatch)
    plan = wizard.install_plan()
    to = Path(plan["to"])
    (to / "bin").mkdir(parents=True)  # an older copy, replaced whole
    (to / "bin" / "ollama").write_bytes(b"#!old")
    (to / "stale.txt").write_text("old")
    out = wizard.fetch_ollama(_job(), plan)
    assert out["ok"]
    assert (to / "bin" / "ollama").read_bytes() == b"#!new" and not (to / "stale.txt").exists()
    assert sorted(p.name for p in to.parent.iterdir()) == ["ollama"]  # the staging is gone
    unit = home / ".config" / "systemd" / "user" / "keepfold-ollama.service"
    text = unit.read_text(encoding="utf-8")
    exe = str(to / "bin" / "ollama").replace("\\", "\\\\")  # systemd quoting, on Windows too
    assert f'ExecStart="{exe}" "serve"' in text and "WantedBy=default.target" in text
    assert ran == [["systemctl", "--user", "daemon-reload"],
                   ["systemctl", "--user", "enable", "keepfold-ollama"],
                   ["systemctl", "--user", "restart", "keepfold-ollama"]]
    assert wizard.ollama_exe() == str(to / "bin" / "ollama")
    assert wizard._ollama_start_cmd() == ["systemctl", "--user", "start", "keepfold-ollama"]


def test_fetch_ollama_linux_without_working_systemctl_falls_back_to_serve(
        tmp_path, monkeypatch, home):
    from keepfold.app import status
    _linux(monkeypatch)
    monkeypatch.setattr(status, "_systemd", lambda: True)
    monkeypatch.setattr(targets, "_run", lambda args, timeout=300: (False, "no user bus"))
    _fake_download(monkeypatch, *_tarball(tmp_path))
    _fake_unpack(monkeypatch)
    out = wizard.fetch_ollama(_job(), wizard.install_plan())
    assert out["ok"] and "no user bus" in out["lines"][-2]
    assert not wizard._ollama_unit().exists()
    exe = wizard.ollama_exe()
    assert exe and wizard._ollama_start_cmd() == [exe, "serve"]


def test_fetch_ollama_failed_unpack_leaves_no_partial_copy(tmp_path, monkeypatch, home):
    _linux(monkeypatch)
    monkeypatch.setattr(targets, "_run", lambda *a, **k: pytest.fail("nothing may run"))
    _fake_download(monkeypatch, *_tarball(tmp_path))
    _fake_unpack(monkeypatch, fail=True)
    plan = wizard.install_plan()
    to = Path(plan["to"])
    with pytest.raises(ValueError, match="truncated"):
        wizard.fetch_ollama(_job(), plan)
    assert not to.exists() and not any(to.parent.iterdir())  # no copy, no staging
    (to / "bin").mkdir(parents=True)  # an older copy stays untouched
    (to / "bin" / "ollama").write_bytes(b"#!old")
    with pytest.raises(ValueError, match="truncated"):
        wizard.fetch_ollama(_job(), plan)
    assert (to / "bin" / "ollama").read_bytes() == b"#!old" and not (to / "lib").exists()
    assert sorted(p.name for p in to.parent.iterdir()) == ["ollama"]
    assert not any((home / ".keepfold" / "tmp").iterdir())


def test_unzip_masks_setuid_setgid_and_sticky_bits(tmp_path, monkeypatch):
    z = _zip(tmp_path / "m.zip", [("su", b"x", stat.S_IFREG | 0o4755),
                                  ("sg", b"x", stat.S_IFREG | 0o2750),
                                  ("st", b"x", stat.S_IFREG | 0o1777)])
    modes, chmod = {}, os.chmod
    monkeypatch.setattr(os, "chmod", lambda p, m, **k: (
        modes.__setitem__(os.path.basename(p), m), chmod(p, m, **k))[1])
    wizard._unpack(z, tmp_path / "to")
    assert modes == {"su": 0o755, "sg": 0o750, "st": 0o777}


def test_linux_start_cmd_uses_the_user_unit_when_present(monkeypatch, home):
    _linux(monkeypatch)
    exe = home / ".local" / "share" / "keepfold" / "ollama" / "bin" / "ollama"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"#!")
    assert wizard._ollama_start_cmd() == [str(exe), "serve"]
    unit = home / ".config" / "systemd" / "user" / "keepfold-ollama.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n")
    assert wizard._ollama_start_cmd() == ["systemctl", "--user", "start", "keepfold-ollama"]


def test_unpack_real_tar_zst_refuses_paths_outside(tmp_path):
    zstandard = pytest.importorskip("zstandard")  # Linux only: the Ubuntu CI job runs this
    import tarfile

    def tzst(name, members):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as t:
            for path, data, mode in members:
                info = tarfile.TarInfo(path)
                info.size, info.mode = len(data), mode
                t.addfile(info, io.BytesIO(data))
        out = tmp_path / name
        out.write_bytes(zstandard.ZstdCompressor().compress(raw.getvalue()))
        return out
    good = tzst("ok.tar.zst", [("bin/ollama", b"#!elf", 0o4755),
                               ("lib/ollama/x.so", b"so", 0o644)])
    wizard._unpack(good, tmp_path / "to")
    exe = tmp_path / "to" / "bin" / "ollama"
    assert exe.read_bytes() == b"#!elf" and (tmp_path / "to" / "lib" / "ollama" / "x.so").is_file()
    assert stat.S_IMODE(exe.stat().st_mode) & 0o7000 == 0  # the data filter drops setuid
    bad = tzst("bad.tar.zst", [("../evil", b"x", 0o644)])
    with pytest.raises(ValueError, match="Could not unpack"):
        wizard._unpack(bad, tmp_path / "to2")
    assert not (tmp_path / "evil").exists()
    junk = tmp_path / "junk.tar.zst"
    junk.write_bytes(b"not zstd")
    with pytest.raises(ValueError, match="Could not unpack"):
        wizard._unpack(junk, tmp_path / "to3")


def test_temp_refused_off_windows_never_says_windows(monkeypatch):
    """Port T17: the temp-folder refusal and the example path follow the OS; Windows unchanged."""
    monkeypatch.setattr(sys, "platform", "win32")
    assert wizard.temp_refused() == wizard.TEMP_REFUSED
    for plat, example in (("darwin", "/Users/you/Keepfold"), ("linux", "/home/you/Keepfold")):
        monkeypatch.setattr(sys, "platform", plat)
        assert "Windows" not in wizard.temp_refused()
        assert "system's temporary folder" in wizard.temp_refused()
        with pytest.raises(ValueError, match=example):
            wizard.vault_path("relative/folder")
