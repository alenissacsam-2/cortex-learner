# The frozen Keepfold for Ubuntu (DESIGN.md 4.2), from the S2 spike's spec; build.sh runs it.
# One onedir, dist/keepfold/, with two programs: `keepfold-app` (the app) and `keepfold` (the CLI).
# It bundles no system library: the app loads Ubuntu's own GTK 3, WebKitGTK 4.1, girepository and
# AppIndicator (the .deb's Depends:). Every library from /usr, /lib and /etc is dropped, not only
# those four: a bundled 24.04 glib, cairo or pango next to 26.04's GTK would fail on newer
# symbols, because the bootloader puts the bundle first on LD_LIBRARY_PATH.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = os.path.dirname(os.path.dirname(SPECPATH))
SYSTEM = ("/usr/", "/lib/", "/lib64/", "/etc/")
# What PyInstaller's gi hooks collect or generate for a self-contained GTK; the system has it all.
GI_DATA = ("gi_typelibs", "gio_modules", "lib/gdk-pixbuf", "share/", "etc/")
# The runtime hooks that point GI, GIO, gdk-pixbuf, GTK and XDG_DATA_DIRS into the bundle, which
# holds none of that; child programs (gsettings, xdg-open) would inherit them too. The spike
# removed those variables at start instead.
GI_RTHOOKS = {"pyi_rth_gi", "pyi_rth_gio", "pyi_rth_glib", "pyi_rth_gdkpixbuf", "pyi_rth_gtk"}

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    hiddenimports=[
        *collect_submodules("keepfold"),  # the lazy imports (`keepfold selftest` checks them)
        "webview.platforms.gtk", "pystray._appindicator", "gi._gi_cairo",
        *collect_submodules("gi.overrides"),
    ],
    datas=[
        *collect_data_files("keepfold"),
        *copy_metadata("keepfold"),  # importlib.metadata.version("keepfold")
        # the wheel's force-include (pyproject.toml): the dashboard's Help tab
        (os.path.join(ROOT, "skills", "keepfold", "modes", "help.md"), "keepfold/assets/help"),
        (os.path.join(ROOT, "docs", "GUIDE.md"), "keepfold/assets/help"),
    ],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy"],
    hooksconfig={"gi": {"icons": [], "themes": [], "languages": [],
                        "module-versions": {"Gtk": "3.0", "Gdk": "3.0", "WebKit2": "4.1",
                                            "Soup": "3.0"}}},
)
a.binaries = [b for b in a.binaries if not b[1].startswith(SYSTEM)]
assert any(os.path.basename(b[0]).startswith("libpython") for b in a.binaries), (
    "no libpython left: freeze with uv's own Python (build.sh sets UV_PYTHON_PREFERENCE)")
a.datas = [d for d in a.datas if not (d[1].startswith(SYSTEM) or d[0].startswith(GI_DATA))]
assert any(s[0] == "pyi_rth_gi" for s in a.scripts), "PyInstaller renamed its gi runtime hooks"
a.scripts = [s for s in a.scripts if s[0] not in GI_RTHOOKS]

pyz = PYZ(a.pure)
app = EXE(pyz, a.scripts, [], exclude_binaries=True, name="keepfold-app", upx=False)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="keepfold", upx=False)
coll = COLLECT(app, cli, a.binaries, a.datas, name="keepfold", upx=False)
