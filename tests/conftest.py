"""Shared fixtures and helpers. Plain helpers (`git`, `events`, `run`, `note`, `tiers`,
`FakeEmbed`) are imported with `from conftest import ...`; a file whose tests all need the
isolated `home` sets `pytestmark = pytest.mark.usefixtures("home")`."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import anyio
import numpy as np
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
CONTRACT = "# Teaching contract\n\n- Ask before you teach.\n- Keep **answers** short — ✓\n"


@pytest.fixture(autouse=True)
def _no_real_home(tmp_path_factory, monkeypatch):
    """Tests never read or write the real ~/.cortex (the vault pointer every chat reads),
    ~/.claude or the Claude desktop configs. Per-file `home` fixtures override this with their
    own folder; this one uses a separate temp dir so it never collides with tmp_path."""
    h = tmp_path_factory.mktemp("home")
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    for var in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):  # clients.py, doctor.py
        monkeypatch.setenv(var, str(h / var))
    for var in ("CORTEX_VAULT", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(var, raising=False)
    # git reads ~/.gitconfig from HOME; commits still need an identity
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "Test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "test@example.invalid")
    return h


@pytest.fixture(autouse=True)
def _no_real_installs(tmp_path_factory, monkeypatch):
    """Tests find git only on PATH (never in the real Program Files or the registry's PATH),
    and never the real LM Studio `lms` on PATH (a test must not run it)."""
    from cortex import session

    monkeypatch.setenv("ProgramFiles", str(tmp_path_factory.mktemp("programfiles")))
    monkeypatch.setattr(session, "_registry_path", lambda: "")
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda cmd, *a, **k: None if cmd == "lms"
                        else real(cmd, *a, **k))


@pytest.fixture(autouse=True)
def _no_real_run_key(monkeypatch):
    """Tests never read or write the real HKCU Run key (Start at login, Start with Windows): an
    in-memory dict of value name -> command instead. Returns the dict."""
    from cortex.app import status

    run: dict = {}
    monkeypatch.setattr(status, "run_get", lambda name=status.LOGIN_TASK: run.get(name))
    monkeypatch.setattr(status, "run_set",
                        lambda v, name=status.LOGIN_TASK: run.__setitem__(name, v))
    monkeypatch.setattr(status, "run_delete", lambda name=status.LOGIN_TASK: run.pop(name, None))
    return run


@pytest.fixture(autouse=True)
def _no_real_chat_export(tmp_path_factory, monkeypatch):
    """Tests never see the user's imported Claude Chat conversations."""
    monkeypatch.setenv("CORTEX_CHAT_EXPORT_DIR", str(tmp_path_factory.mktemp("chat-export")))


@pytest.fixture(autouse=True)
def _relayed_cutoff_later(monkeypatch):
    """Tests write relayed yeses on the real clock; keep them act-grade whatever the date.
    Tests of the cutoff (claims.late_relay) set claims.RELAYED_FROM themselves."""
    from cortex import claims

    monkeypatch.setattr(claims, "RELAYED_FROM", "9999-12-31")


@pytest.fixture(autouse=True)
def _no_real_reranker(monkeypatch):
    """Tests never load a real cross-encoder from ~/.cortex/models; they inject a fake one."""
    from cortex import semantic

    monkeypatch.setattr(semantic, "_reranker", None)
    real = semantic.load_reranker
    monkeypatch.setattr(semantic, "load_reranker", lambda download=False, config=True: (
        real(download, config) if semantic._reranker is not None else None))


@pytest.fixture(autouse=True)
def _no_real_embedder(monkeypatch):
    """Tests never load the real embedding model (cortex_add now embeds new pages to file them
    under a topic); they inject a FakeEmbed as semantic._embedder. No build_server warm-up
    thread either: one that outlived its test could call the unpatched loader."""
    from cortex import mcp_server, semantic

    monkeypatch.setattr(mcp_server, "WARM_EMBEDDER", False)
    # a source edit during the run must not turn every tool into restart_needed
    monkeypatch.setattr(mcp_server, "CODE_MTIME", float("inf"))
    monkeypatch.setattr(semantic, "_embedder", None)
    real = semantic.load_embedder
    monkeypatch.setattr(semantic, "load_embedder", lambda download=False: (
        real(download) if semantic._embedder is not None else None))


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A home folder at tmp_path/home, for tests that look inside it: HOME, Path.home(),
    APPDATA and LOCALAPPDATA (doctor and clients never read the real Claude configs), no
    XDG_CONFIG_HOME."""
    h = tmp_path / "home"
    h.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    for var in ("LOCALAPPDATA", "APPDATA"):
        monkeypatch.setenv(var, str(h / var))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return h


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=True).stdout


def _vault(tmp_path, *, commit, contract=False):
    r = tmp_path / "vault"
    shutil.copytree(FIXTURES / "vault_mini", r)
    (r / ".gitignore").write_text(".cortex/*\n")
    if contract:
        (r / "me/teaching-contract.md").write_bytes(CONTRACT.encode())
    git(r, "init", "-q")
    if commit:
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "init")
    return r


@pytest.fixture
def root(tmp_path):
    """vault_mini with the test CONTRACT, in a git repo with one commit."""
    return _vault(tmp_path, commit=True, contract=True)


@pytest.fixture
def root_committed(tmp_path):
    """vault_mini as shipped, in a git repo with one commit."""
    return _vault(tmp_path, commit=True)


@pytest.fixture
def root_plain(tmp_path):
    """vault_mini as shipped, in a fresh git repo with no commits."""
    return _vault(tmp_path, commit=False)


def run(root, body, role="owner"):
    """Run `body(call)` against a fresh server over the SDK's in-memory connection."""
    from mcp import Client

    from cortex.mcp_server import build_server
    from cortex.vault import Vault

    server = build_server(Vault(root), role)

    async def main():
        async with Client(server) as client:
            async def call(name, args=None):
                r = await client.call_tool(name, args or {})
                assert not r.is_error, r.content
                out = r.structured_content
                assert out.get("next"), out  # every result, success or error, has `next`
                return out

            await body(call)

    anyio.run(main)


def events(root):
    """Every event line in the vault, file by file."""
    out = []
    for p in sorted((root / "events").glob("*/*/*.jsonl")):
        out += [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]
    return out


def tiers(root):
    """{claim text: tier} over the vault's me/* claims."""
    from cortex import claims
    from cortex.events import EventLog
    from cortex.vault import Vault

    v = Vault(root)
    return {r["text"].split(" (")[0]: r["tier"] for r in claims.tiers(v, EventLog(v).iter_events())}


# The fake embedder: words on the same axis mean the same thing; other words count for nothing.
AXES = {"vehicle": 0, "automobile": 0, "car": 0, "motor": 0,
        "recipe": 1, "kitchen": 1, "cooking": 1, "pasta": 1}


class FakeEmbed:
    def __init__(self):
        self.texts: list[str] = []

    def __call__(self, texts):
        self.texts += texts
        out = np.zeros((len(texts), 4), dtype=np.float32)
        for k, t in enumerate(texts):
            for w in re.findall(r"\w+", t.lower()):
                if w in AXES:
                    out[k, AXES[w]] += 1
        return out


def note(root, rel, front, body):
    """Write root/rel as a page with front matter `front` (YAML lines) and `body`."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{front}---\n{body}\n", encoding="utf-8")
    return p