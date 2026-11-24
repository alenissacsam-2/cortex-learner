"""Build dist/CortexSetup.exe (Windows only): `uv run python installer/build.py`.

uv build --wheel, then stage the wheel, uv.lock's versions as constraints.txt and this machine's
uv.exe as the exe's payload, then PyInstaller (fetched by uvx at build time, never a project
dependency) packs installer/setup_app.py into one windowed exe. It also writes
dist/cortex-skill.zip (the Chat skill) for the release. Staging lives in dist/ (git-ignored)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYINSTALLER = "pyinstaller==6.22.3"


def main() -> None:
    if sys.platform != "win32":
        sys.exit("CortexSetup.exe builds on Windows only.")
    uv = shutil.which("uv") or sys.exit("uv is not on PATH.")
    stage = ROOT / "dist" / "installer-build"
    shutil.rmtree(stage, ignore_errors=True)
    payload = stage / "payload"
    subprocess.run([uv, "build", "--wheel", "--out-dir", str(payload)], cwd=ROOT, check=True)
    # uv.lock's versions, so every install of this exe gets the same dependencies (-c at install);
    # the extras' too, so `cortex model download --voice` pins av and ctranslate2 (a constraint
    # never installs anything by itself)
    subprocess.run([uv, "export", "--frozen", "--no-dev", "--all-extras", "--no-emit-project",
                    "--no-hashes", "--quiet", "-o", str(payload / "constraints.txt")],
                   cwd=ROOT, check=True)
    shutil.copy2(uv, payload / "uv.exe")
    # the Chat skill, beside the exe: the release attaches it as cortex-skill.zip
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_skills
    _, zpath = build_skills.build(ROOT / "skills" / "cortex" / "SKILL.md", stage / "skill")
    shutil.copy2(zpath, ROOT / "dist" / "cortex-skill.zip")
    subprocess.run([uv, "tool", "run", "--python", "3.12", "--from", PYINSTALLER, "pyinstaller",
                    "--noconfirm", "--clean", "--onefile", "--windowed", "--name", "CortexSetup",
                    "--add-data", f"{payload}{os.pathsep}payload",
                    "--distpath", str(ROOT / "dist"), "--workpath", str(stage / "work"),
                    "--specpath", str(stage), str(ROOT / "installer" / "setup_app.py")],
                   cwd=ROOT, check=True)
    exe = ROOT / "dist" / "CortexSetup.exe"
    print(f"built {exe} ({exe.stat().st_size / 1e6:.1f} MB) and {zpath.name} beside it")


if __name__ == "__main__":
    main()
