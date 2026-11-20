"""E2E infra (docs/design/v6/TEST-PLAN.md §4): every process runs `cortex` through `_launch.py`
under the uv venv's Python, with a fresh home and no real vault, browser profile or port."""

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from e2e.fake_ollama import FakeOllama

LAUNCH = Path(__file__).with_name("_launch.py")
CLI = [sys.executable, "-I", str(LAUNCH)]  # -I: no cwd, PYTHON* or user site on sys.path


def free_port_number() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def free_port() -> int:
    return free_port_number()


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    """The environment for every process: HOME, USERPROFILE, APPDATA and LOCALAPPDATA under
    tmp_path/home; no CORTEX_* or Claude variables; and PATH without the venv's `cortex` (a
    new user has not put it on PATH yet)."""
    home = tmp_path / "home"
    e = {k: v for k, v in os.environ.items()
         if not k.upper().startswith(("CORTEX_", "CLAUDE")) and k.upper() not in
         ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "PYTHONPATH")}
    e.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "AppData" / "Roaming"),
             LOCALAPPDATA=str(home / "AppData" / "Local"),
             PATH=os.pathsep.join(p for p in e.get("PATH", "").split(os.pathsep)
                                  if p and not shutil.which("cortex", path=p)))
    for k in ("APPDATA", "LOCALAPPDATA"):
        Path(e[k]).mkdir(parents=True)
    return e


@pytest.fixture
def run_cli(env, tmp_path):
    """run_cli(*args, timeout=120) -> CompletedProcess (text, utf-8), cwd tmp_path."""
    def run(*args, timeout: float = 120) -> subprocess.CompletedProcess:
        return subprocess.run([*CLI, *map(str, args)], env=env, cwd=tmp_path,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    return run


@pytest.fixture
def fake_ollama(env):
    """A FakeOllama; every process this test starts sends Ollama's 11434 traffic to it."""
    with FakeOllama() as f:
        env["CORTEX_E2E_OLLAMA"] = f.url
        yield f
