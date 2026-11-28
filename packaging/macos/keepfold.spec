# PyInstaller spec for Keepfold.app (macOS, onedir), run by packaging/macos/build.sh, which first
# writes the icon to dist/macos-build/Keepfold.icns. Two executables share one bundle: the GUI
# `Keepfold` (keepfold.app.launch:main) and the console `keepfold-cli` (keepfold.taint:main), whose
# names differ by more than case (APFS ignores case). PyInstaller signs it ad hoc.
from importlib.metadata import version
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

HERE = Path(SPECPATH)
ROOT = HERE.parent.parent
VERSION = version("keepfold")
BUNDLE_ID = "io.github.alenissacsam-2.keepfold"

# what the wheel ships: the package data, pyproject's force-include (the Help tab's files), and the
# metadata that `--version`, the MCP server and fastembed's own import read
datas = [
    *collect_data_files("keepfold"),
    *copy_metadata("keepfold"),
    *copy_metadata("fastembed"),
    (str(ROOT / "skills" / "keepfold" / "modes" / "help.md"), "keepfold/assets/help"),
    (str(ROOT / "docs" / "GUIDE.md"), "keepfold/assets/help"),
]
# Keepfold imports most modules inside functions; pystray picks its backend by name
hidden = [*collect_submodules("keepfold"), "pystray._darwin"]

gui = Analysis([str(HERE / "keepfold_app.py")], datas=datas, hiddenimports=hidden)
cli = Analysis([str(HERE / "keepfold_cli.py")], datas=datas, hiddenimports=hidden)
gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name="Keepfold",
              console=False)
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="keepfold-cli",
              console=True)
coll = COLLECT(gui_exe, gui.binaries, gui.datas, cli_exe, cli.binaries, cli.datas,
               name="Keepfold")
app = BUNDLE(
    coll,
    name="Keepfold.app",
    icon=str(ROOT / "dist" / "macos-build" / "Keepfold.icns"),
    bundle_identifier=BUNDLE_ID,
    version=VERSION,
    # DESIGN §4.1. The name, executable and LSBackgroundOnly are explicit, so the console CLI in
    # the same COLLECT cannot make the app LSBackgroundOnly. No LSFileQuarantineEnabled.
    info_plist={
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": "Keepfold",
        "CFBundleDisplayName": "Keepfold",
        "CFBundleExecutable": "Keepfold",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSBackgroundOnly": False,
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
        # keepfold.app.exports: the chat-export zips the owner downloads, offered on Home
        "NSDownloadsFolderUsageDescription":
            "Keepfold looks in Downloads for the chat exports you download, so it can offer to "
            "add them.",
    },
)
