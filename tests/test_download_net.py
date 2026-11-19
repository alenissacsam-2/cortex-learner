"""The search model download on campus networks (DEPS-UX-AUDIT A3-A5): a plain sentence per
failure kind and a non-zero exit, the OS certificate store only inside the download, and the
system proxy copied into the env of the download children. Nothing is downloaded: the embedder
loader, getproxies and every process start are fakes."""

import os
import sys
import threading
import types

import pytest
from typer.testing import CliRunner

from cortex import _proc, cli, semantic, update
from cortex.app import actions, status, wizard

pytestmark = pytest.mark.usefixtures("home")
NO_PROXY_ENV = {k: "" for k in _proc._PROXY_VARS}


@pytest.fixture
def trust(monkeypatch):
    """A fake truststore that records inject_into_ssl() calls."""
    calls = []
    mod = types.ModuleType("truststore")
    mod.inject_into_ssl = lambda: calls.append(1)
    monkeypatch.setitem(sys.modules, "truststore", mod)
    monkeypatch.setattr(_proc.urllib.request, "getproxies", lambda: {})
    return calls


@pytest.fixture(autouse=True)
def _loguru_quiet():
    """loguru's default sink keeps the stderr of whichever test imported it first (closed by
    now); only the download's own capture sink may run here."""
    from loguru import logger
    logger.remove()
    yield
    logger.add(sys.stderr)


def _download(monkeypatch, err):
    def fail(download=False):
        assert download
        if isinstance(err, tuple):  # (what fastembed logs, what it then raises)
            from loguru import logger
            logger.error(err[0])
            raise err[1]
        raise err
    monkeypatch.setattr(semantic, "load_embedder", fail)
    return CliRunner().invoke(cli.app, ["model", "download"])


@pytest.mark.parametrize("err, words", [
    (OSError(28, "No space left on device"), "disk is full"),
    (ValueError("[WinError 112] os error 112"), "disk is full"),
    (OSError("407 Proxy Authentication Required"), "proxy refused"),
    (("Could not download model from HuggingFace: [SSL: CERTIFICATE_VERIFY_FAILED] "
      "certificate verify failed", ValueError("Could not load model x from any source.")),
     "Your network blocked huggingface.co"),
    (TimeoutError("The read operation timed out"), "took too long"),
    (ConnectionError("[Errno 11001] getaddrinfo failed"), "Could not reach huggingface.co"),
    (RuntimeError("something odd"), "download stopped"),
])
def test_each_failure_is_one_plain_sentence_and_exit_1(monkeypatch, trust, err, words, home):
    r = _download(monkeypatch, err)
    assert r.exit_code == 1, r.output
    assert words in r.output and "Traceback" not in r.output
    log = home / ".cortex" / "logs" / "model-download.log"
    assert f"Details are in the log: {log}" in r.output
    assert "Traceback" in log.read_text(encoding="utf-8")  # the detail goes to the log only
    if "disk" not in words:
        assert "the download starts over" in r.output  # hf_hub deletes partial files: no resume
    assert trust == [1]


def test_truststore_only_in_the_download_and_its_absence_is_fine(monkeypatch, trust):
    import cortex  # noqa: F401  (importing cortex never injects)
    assert trust == []
    monkeypatch.setitem(sys.modules, "truststore", None)  # import fails
    r = _download(monkeypatch, ConnectionError("getaddrinfo failed"))
    assert r.exit_code == 1 and "Could not reach huggingface.co" in r.output


def test_proxy_env_copies_the_system_proxy_only_when_unset(monkeypatch):
    sys_proxy = {"https": "http://proxy.campus:8080", "http": "http://proxy.campus:8080",
                 "ftp": "ftp://x", "no": "localhost"}
    monkeypatch.setattr(_proc.urllib.request, "getproxies", lambda: sys_proxy)
    env = _proc.proxy_env({"PATH": "p"})
    assert env == {"PATH": "p", "HTTPS_PROXY": "http://proxy.campus:8080",
                   "HTTP_PROXY": "http://proxy.campus:8080", "NO_PROXY": "localhost"}
    mine = {"https_proxy": "http://mine:1"}
    assert _proc.proxy_env(mine) == mine  # the user's own setting wins, nothing added


class _Job:
    def __init__(self):
        self.cancelled, self.proc = threading.Event(), None

    def say(self, s):
        pass


class _Proc:
    def __init__(self):
        self.stdout = types.SimpleNamespace(read=lambda n: b"")

    def wait(self):
        return 0


def test_download_children_get_the_system_proxy(monkeypatch):
    monkeypatch.setattr(_proc.urllib.request, "getproxies",
                        lambda: {"https": "http://proxy.campus:8080"})
    for k in _proc._PROXY_VARS:
        monkeypatch.setenv(k, "")
    envs = []
    monkeypatch.setattr(actions, "_popen", lambda args, **k: (envs.append(k["env"]), _Proc())[1])
    monkeypatch.setattr(wizard, "_cortex", lambda: "cortex")
    monkeypatch.setattr(wizard, "ollama_exe", lambda: "ollama")
    monkeypatch.setattr(status, "ollama_models", lambda: [])
    assert wizard.embed(_Job(), None)["ok"]
    assert wizard.pull_ollama(_Job(), "qwen3.5:2b")["ok"]
    started = []
    monkeypatch.setattr(update.subprocess, "Popen", lambda args, **k: started.append(k["env"]))
    update.launch(update.Path("CortexSetup.exe"), 1)
    for env in [*envs, *started]:
        assert env["HTTPS_PROXY"] == "http://proxy.campus:8080"
    assert started[0]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert os.environ.get("HTTPS_PROXY", "") == ""  # spawning never changes our own env
