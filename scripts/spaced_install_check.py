"""A real install into a folder whose path has spaces and non-ASCII letters, with the real uv:
`uv run python scripts/spaced_install_check.py` (Windows; downloads Python and packages).

Builds the payload the way installer/build.py does, runs setup_app.install() into
<temp>\\Jöhn Smith ü\\Cortex under a temp home (no shortcuts, PATH or registry), then runs
bin\\cortex.exe --version and doctor there. Prints PASS or FAIL; deletes its temp folder.
The bug it guards: uv 0.9.15 split the `-c constraints.txt` path on spaces (uv#22290)."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # doctor prints arrows; a pipe
    uv = shutil.which("uv") or sys.exit("uv is not on PATH.")
    tmp = Path(tempfile.mkdtemp(prefix="cortex-spaced-"))
    try:
        payload = tmp / "payload"
        subprocess.run([uv, "build", "--wheel", "--quiet", "--out-dir", str(payload)], cwd=ROOT,
                       check=True)
        subprocess.run([uv, "export", "--frozen", "--no-dev", "--all-extras",
                        "--no-emit-project", "--no-hashes", "--quiet",
                        "-o", str(payload / "constraints.txt")], cwd=ROOT, check=True)
        shutil.copy2(uv, payload / "uv.exe")

        home = tmp / "home"
        for sub in ("AppData/Roaming", "AppData/Local/Temp", "chats", "exports"):
            (home / sub).mkdir(parents=True)
        # a fresh user's environment: nothing from this `uv run` (VIRTUAL_ENV, UV_*) leaks in
        for k in [k for k in os.environ if k.startswith("UV_") or k == "VIRTUAL_ENV"]:
            del os.environ[k]
        os.environ.update(USERPROFILE=str(home), HOME=str(home),
                          APPDATA=str(home / "AppData" / "Roaming"),
                          LOCALAPPDATA=str(home / "AppData" / "Local"),
                          TEMP=str(home / "AppData" / "Local" / "Temp"),
                          TMP=str(home / "AppData" / "Local" / "Temp"),
                          CORTEX_CHATS_DIR=str(home / "chats"),
                          CORTEX_CHAT_EXPORT_DIR=str(home / "exports"))
        tempfile.tempdir = None
        sys.path.insert(0, str(ROOT / "installer"))
        import setup_app

        d = tmp / "Jöhn Smith ü" / "Cortex"
        opts = setup_app.parse_args(["--dir", str(d), "--no-shortcuts", "--no-path",
                                     "--no-registry"])
        try:
            setup_app.install(d, opts, print, src=payload)
        except setup_app.SetupError as e:
            print(f"FAIL: install: {e}")
            return 1
        exe = d / "bin" / "cortex.exe"
        ver = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        print(f"cortex --version: exit {ver.returncode}: {(ver.stdout + ver.stderr).strip()}")
        doc = subprocess.run([str(exe), "doctor"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        print(f"cortex doctor: exit {doc.returncode}\n{(doc.stdout + doc.stderr).strip()}")
        ok = ver.returncode == 0
        print("PASS" if ok else "FAIL: cortex --version did not exit 0")
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, onexc=lambda f, p, e: (os.chmod(p, 0o700), f(p)))
        print(f"removed {tmp}" if not tmp.exists() else f"could not remove all of {tmp}")


if __name__ == "__main__":
    sys.exit(main())
