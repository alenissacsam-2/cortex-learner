"""Shared fixtures and helpers. Plain helpers (`git`, `events`, `run`, `note`, `tiers`,
`FakeEmbed`) are imported with `from conftest import ...`; a file whose tests all need the
isolated `home` sets `pytestmark = pytest.mark.usefixtures("home")`."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import anyio
import numpy as np
import pytest

# typer forces colour (ANSI codes inside --help text) under these, GitHub Actions sets the first;
# tests read plain help, as a terminal user does. Before any test module imports typer
for _name in ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS"):
    os.environ.pop(_name, None)

FIXTURES = Path(__file__).parent / "fixtures"
CONTRACT = "# Teaching contract\n\n- Ask before you teach.\n- Keep **answers** short — ✓\n"


def pytest_collection_modifyitems(config, items):
    """Timing benchmarks need the machine to themselves: under `-n` the other workers load every
    core and the bounds fail, so they skip there and run serially with `-m perf`."""
    if os.environ.get("PYTEST_XDIST_WORKER"):
        skip = pytest.mark.skip(reason="timing benchmark: run alone with uv run pytest -m perf")
        for item in items:
            if "perf" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_real_home(tmp_path_factory, monkeypatch):
    """Tests never read or write the real ~/.keepfold (the vault pointer every chat reads),
    ~/.claude or the Claude desktop configs. Per-file `home` fixtures override this with their
    own folder; this one uses a separate temp dir so it never collides with tmp_path."""
    h = tmp_path_factory.mktemp("home")
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    for var in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):  # clients.py, doctor.py
        monkeypatch.setenv(var, str(h / var))
    # the other coding agents' homes (chats.codex_files...) follow Path.home() too
    for var in ("KEEPFOLD_VAULT", "CLAUDE_PROJECT_DIR", "CODEX_HOME", "GEMINI_CLI_HOME",
                "COPILOT_HOME"):
        monkeypatch.delenv(var, raising=False)
    # git reads ~/.gitconfig from HOME; commits still need an identity
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "Test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "test@example.invalid")
    return h


@pytest.fixture(autouse=True)
def _own_temp_folder(tmp_path_factory, monkeypatch):
    """The 'system temp folder' is a separate empty dir, so the vaults tests keep in tmp_path
    are not 'in a temporary folder' (the warning in status/_init); a test that wants one
    monkeypatches tempfile.gettempdir itself."""
    import tempfile
    t = tmp_path_factory.mktemp("systemp")
    monkeypatch.setattr(tempfile, "tempdir", str(t))  # what gettempdir() returns once set


@pytest.fixture(autouse=True)
def _no_real_installs(tmp_path_factory, monkeypatch):
    """Tests find git only on PATH (never in the real Program Files or the registry's PATH),
    and never the real LM Studio `lms` on PATH (a test must not run it)."""
    from keepfold import session

    monkeypatch.setenv("ProgramFiles", str(tmp_path_factory.mktemp("programfiles")))
    monkeypatch.setattr(session, "_registry_path", lambda: "")
    from keepfold import clients
    monkeypatch.setattr(clients, "_registry_install", lambda: None)  # never the real install
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda cmd, *a, **k: None if cmd == "lms"
                        else real(cmd, *a, **k))


@pytest.fixture(autouse=True)
def _no_real_run_key(monkeypatch):
    """Tests never read or write the real HKCU Run key (Start at login, Start with Windows): an
    in-memory dict of value name -> command instead. Returns the dict."""
    from keepfold.app import status

    run: dict = {}
    monkeypatch.setattr(status, "run_get", lambda name=status.LOGIN_TASK: run.get(name))
    monkeypatch.setattr(status, "run_set",
                        lambda v, name=status.LOGIN_TASK: run.__setitem__(name, v))
    monkeypatch.setattr(status, "run_delete", lambda name=status.LOGIN_TASK: run.pop(name, None))
    return run


class FakeUser32:
    """The user32 calls quick capture's hotkey thread makes (keepfold.app.desktop.Hotkey): a
    message queue per process, `held` combinations another app holds, and every call
    recorded in `calls`. `press()` is a WM_HOTKEY."""

    def __init__(self, held=()):
        import queue
        self.q, self.held, self.calls, self.registered = queue.Queue(), set(held), [], set()

    def PeekMessageW(self, *a):  # noqa: N802
        return 0

    def RegisterHotKey(self, hwnd, id, mods, vk):  # noqa: N802, A002
        self.calls.append(("register", mods, vk))
        if (mods, vk) in self.held:
            return 0
        self.registered.add(id)
        return 1

    def UnregisterHotKey(self, hwnd, id):  # noqa: N802, A002
        self.calls.append(("unregister",))
        self.registered.discard(id)
        return 1

    def GetMessageW(self, pmsg, hwnd, lo, hi):  # noqa: N802
        m = self.q.get()
        pmsg._obj.message = m
        return 0 if m == 0x12 else 1  # WM_QUIT

    def PostThreadMessageW(self, tid, m, w, lp):  # noqa: N802
        self.q.put(m)
        return 1

    def press(self):
        self.q.put(0x312)  # WM_HOTKEY


@pytest.fixture(autouse=True)
def _no_real_hotkey(monkeypatch):
    """Tests never register a real global hotkey: the desktop app's hotkey thread gets a
    FakeUser32. Returns it."""
    from keepfold.app import desktop

    fake = FakeUser32()
    monkeypatch.setattr(desktop, "_user32", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def _no_real_windows_hello(monkeypatch):
    """Tests never show a real Windows Hello prompt: the Private switch's tests mock it."""
    from keepfold.app import hello

    def refuse(*a, **k):
        raise AssertionError("a test reached the real Windows Hello prompt")
    monkeypatch.setattr(hello, "verify", refuse)


@pytest.fixture(autouse=True)
def _no_real_chat_export(tmp_path_factory, monkeypatch):
    """Tests never see the user's imported chat exports or Claude Code transcripts."""
    monkeypatch.setenv("KEEPFOLD_CHAT_EXPORT_DIR", str(tmp_path_factory.mktemp("chat-export")))
    monkeypatch.setenv("KEEPFOLD_CHATS_DIR", str(tmp_path_factory.mktemp("claude-projects")))
    monkeypatch.setenv("KEEPFOLD_DOWNLOADS_DIR", str(tmp_path_factory.mktemp("downloads")))


@pytest.fixture(autouse=True)
def _status_probes_fresh(monkeypatch):
    """Status's kept probes are asked every time in tests (a test changes a fake runner and
    reads Status again); test_app's cache test puts `status.kept` back. No warm-up thread at
    `start` either: one that outlived its test could run the real probes."""
    from keepfold.app import server, status

    monkeypatch.setattr(status, "kept", lambda key, fn: fn())
    monkeypatch.setattr(server, "WARM_STATUS", False)
    # nor a fact-harvest piggyback after a Chat turn (test_fact_harvest.py turns it on)
    monkeypatch.setattr(server, "PIGGYBACK", False)
    # the Claude Code row: never a real `keepfold --version` (the venv's keepfold is on PATH) or
    # the real system PATH; tests/test_status_copies.py sets its own
    from keepfold import update
    from keepfold.app import winpath
    monkeypatch.setattr(status, "keepfold_version", lambda exe: update.current())
    monkeypatch.setattr(winpath, "in_system", lambda folder: False)


@pytest.fixture(autouse=True)
def _relayed_cutoff_later(monkeypatch):
    """Tests write relayed yeses on the real clock; keep them act-grade whatever the date.
    Tests of the cutoff (claims.late_relay) set claims.RELAYED_FROM themselves."""
    from keepfold import claims

    monkeypatch.setattr(claims, "RELAYED_FROM", "9999-12-31")


@pytest.fixture(autouse=True)
def _mcp_client_sees_all(monkeypatch):
    """An MCP test client that passes no `client_info` is named like a local model app, which
    the default agent profile gives every scope (keepfold.profiles), so it sees what the SDK's
    default name "mcp" saw before profiles: "mcp" is now an unknown, restricted client
    (tests/test_agent_context.py). Not a Claude or cloud name: no chat log, private search ok."""
    from mcp.client import session
    from mcp.types import Implementation

    monkeypatch.setattr(session, "DEFAULT_CLIENT_INFO",
                        Implementation(name="lmstudio-test", version="0"))


@pytest.fixture(autouse=True)
def _no_real_reranker(monkeypatch):
    """Tests never load a real cross-encoder from ~/.keepfold/models; they inject a fake one."""
    from keepfold import semantic

    monkeypatch.setattr(semantic, "_reranker", None)
    monkeypatch.setattr(semantic, "_ranker", semantic._Held(semantic._ranker.load))
    real = semantic.load_reranker
    monkeypatch.setattr(semantic, "load_reranker", lambda download=False, config=True: (
        real(download, config) if semantic._reranker is not None else None))


@pytest.fixture(autouse=True)
def _no_real_embedder(monkeypatch):
    """Tests never load the real embedding model (keepfold_add now embeds new pages to file them
    under a topic); they inject a FakeEmbed as semantic._embedder. No build_server warm-up
    thread either: one that outlived its test could call the unpatched loader."""
    from keepfold import mcp_server, semantic

    monkeypatch.setattr(mcp_server, "WARM_EMBEDDER", False)
    # a source edit during the run must not turn every tool into restart_needed
    monkeypatch.setattr(mcp_server, "CODE_MTIME", float("inf"))
    monkeypatch.setattr(semantic, "_embedder", None)
    for held in ("_main", "_capped"):  # a model an earlier test loaded is not this test's
        monkeypatch.setattr(semantic, held, semantic._Held(getattr(semantic, held).load))
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
    (r / ".gitignore").write_text(".keepfold/*\n")
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

    from keepfold.mcp_server import build_server
    from keepfold.vault import Vault

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
    from keepfold import claims
    from keepfold.events import EventLog
    from keepfold.vault import Vault

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