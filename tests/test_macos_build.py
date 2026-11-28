"""packaging/macos, the Mac app build, checked on any OS: keepfold.spec run against recording
fakes of PyInstaller's names, build.sh's syntax and the icon set's names. The build itself runs
on Mac CI (release-apps.yml)."""

import importlib.util
import shutil
import subprocess
import sys
import types
from importlib.metadata import version
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
MAC = ROOT / "packaging" / "macos"
SPEC = MAC / "keepfold.spec"


def run_spec(monkeypatch) -> dict[str, list]:
    """Exec keepfold.spec; each call of Analysis, PYZ, EXE, COLLECT or BUNDLE, by name."""
    calls: dict[str, list] = {}

    def fake(kind):
        def make(*args, **kw):
            obj = types.SimpleNamespace(args=args, kw=kw, pure=[], scripts=[], binaries=[],
                                        datas=[])
            calls.setdefault(kind, []).append(obj)
            return obj
        return make

    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_data_files = hooks.collect_submodules = hooks.copy_metadata = lambda name: []
    for name in ("PyInstaller", "PyInstaller.utils"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    names = {k: fake(k) for k in ("Analysis", "PYZ", "EXE", "COLLECT", "BUNDLE")}
    exec(compile(SPEC.read_text(encoding="utf-8"), str(SPEC), "exec"),
         {"SPECPATH": str(MAC), **names})
    return calls


def test_spec_builds_two_executables_in_one_app(monkeypatch):
    calls = run_spec(monkeypatch)
    assert {e.kw["name"]: e.kw["console"] for e in calls["EXE"]} == \
        {"Keepfold": False, "keepfold-cli": True}
    for a in calls["Analysis"]:
        assert all(Path(s).is_file() for s in a.args[0])  # the entry scripts
        assert all(Path(src).is_file() for src, _ in a.kw["datas"])  # the Help tab's files
        assert "pystray._darwin" in a.kw["hiddenimports"]
    (app,) = calls["BUNDLE"]
    assert app.args == tuple(calls["COLLECT"])
    v = version("keepfold")
    assert app.kw["name"] == "Keepfold.app"
    assert app.kw["bundle_identifier"] == "io.github.alenissacsam-2.keepfold"
    assert app.kw["version"] == v
    plist = app.kw["info_plist"]
    want = {"CFBundleIdentifier": "io.github.alenissacsam-2.keepfold",
            "CFBundleName": "Keepfold", "CFBundleDisplayName": "Keepfold",
            "CFBundleExecutable": "Keepfold", "CFBundleShortVersionString": v,
            "CFBundleVersion": v, "LSBackgroundOnly": False, "LSMinimumSystemVersion": "14.0",
            "NSHighResolutionCapable": True}
    assert {k: plist.get(k) for k in want} == want
    assert plist["NSDownloadsFolderUsageDescription"]
    assert "LSFileQuarantineEnabled" not in plist  # DESIGN §4.1


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"),
                    reason="bash -n runs on the Mac and Linux CI")
def test_build_sh_parses():
    subprocess.run(["bash", "-n", str(MAC / "build.sh")], check=True)


def test_iconset_has_iconutils_names_and_sizes(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("make_icon", ROOT / "scripts" / "make_icon.py")
    make_icon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make_icon)
    monkeypatch.setattr(make_icon, "mark", lambda size: Image.new("RGBA", (size, size)))
    make_icon.iconset(tmp_path / "Keepfold.iconset")
    sizes = {}
    for p in (tmp_path / "Keepfold.iconset").iterdir():
        with Image.open(p) as img:
            sizes[p.name] = img.width
    # Apple's iconset names (`man iconutil`)
    assert sizes == {"icon_16x16.png": 16, "icon_16x16@2x.png": 32,
                     "icon_32x32.png": 32, "icon_32x32@2x.png": 64,
                     "icon_128x128.png": 128, "icon_128x128@2x.png": 256,
                     "icon_256x256.png": 256, "icon_256x256@2x.png": 512,
                     "icon_512x512.png": 512, "icon_512x512@2x.png": 1024}
