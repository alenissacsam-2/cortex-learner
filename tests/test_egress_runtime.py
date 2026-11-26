"""E1: the everyday paths make no outbound connection. Under `offline` every socket connect,
create_connection and getaddrinfo to anything but loopback raises and is recorded with its
stack; the flows below run on a temp vault (tests/fixtures, temp HOME) with the fake embedder
and a loopback fake runner, and must record nothing."""

import json
import socket
import threading
import traceback
from datetime import UTC, datetime

import pytest
from conftest import FakeEmbed, note
from test_app import fakes  # noqa: F401  (autouse: nothing real runs, opens or answers)
from test_chat import Stream

from keepfold import chat, dashboard, importers, prime_hook, review, semantic
from keepfold.catalog import Catalog
from keepfold.events import EventLog, local_day
from keepfold.index import Index
from keepfold.mcp_server import Keepfold
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _host(addr) -> str:
    return str(addr[0] if isinstance(addr, tuple) else addr)


@pytest.fixture
def offline(monkeypatch):
    """Every non-loopback connection attempt (host, stack), each one refused."""
    blocked: list = []

    def guard(addr):
        if _host(addr) not in LOOPBACK:
            blocked.append((_host(addr), "".join(traceback.format_stack(limit=12))))
            raise OSError(f"E1 test: outbound connection to {_host(addr)} blocked")

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_create, real_gai = socket.create_connection, socket.getaddrinfo

    def connect(self, addr):
        if self.family != getattr(socket, "AF_UNIX", None):
            guard(addr)
        return real_connect(self, addr)

    def connect_ex(self, addr):
        if self.family != getattr(socket, "AF_UNIX", None):
            guard(addr)
        return real_connect_ex(self, addr)

    def create_connection(addr, *a, **k):
        guard(addr)
        return real_create(addr, *a, **k)

    def getaddrinfo(host, *a, **k):
        if host is not None:  # None: a local bind
            guard((host.decode() if isinstance(host, bytes) else host,))
        return real_gai(host, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return blocked


def test_the_guard_blocks_an_outside_address_and_lets_loopback_through(offline):
    with pytest.raises(OSError, match="blocked"):
        socket.create_connection(("example.invalid", 443), timeout=1)
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        with socket.create_connection(srv.getsockname(), timeout=1):
            pass
    assert [h for h, _ in offline] == ["example.invalid"]


@pytest.fixture
def vault(root, monkeypatch):
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    note(root, "notes/engines.md", "title: Engines\nsummary: how a motor works\n",
         "# Engines\n\nAutomobile car motor basics.")
    note(root, "notes/pasta.md", "title: Pasta\n", "# Pasta\n\nA recipe from the kitchen.")
    (root / ".keepfold").mkdir(exist_ok=True)
    return Vault(root)


def test_everyday_paths_make_no_outbound_connection(vault, offline, monkeypatch, tmp_path):
    root = vault.root
    Index(vault, Catalog.load(vault)).refresh()  # build the index
    c = Keepfold(vault)
    assert c.keepfold_prime("How does a car motor work?")["next"]  # prime
    monkeypatch.setenv("KEEPFOLD_VAULT", str(root))
    hook = prime_hook.run(json.dumps({"session_id": "chat-e1", "prompt": "car motor",
                                      "hook_event_name": "UserPromptSubmit"}))
    assert json.loads(hook)["hookSpecificOutput"]["additionalContext"]  # the prime hook
    assert c.keepfold_recall("car motor")["next"]  # recall with the fake embedder
    s = Stream("ollama", ["Your notes say so [1]."])  # a Chat answer from a loopback runner
    try:
        events: list = []
        chat.reply(vault, None, "car motor", s.runner(), events.append, threading.Event(),
                   chat.get_settings())
    finally:
        s.close()
    assert any(e["t"] == "done" for e in events), events
    c.tidy()  # the review settle
    review.view(vault, EventLog(vault).iter_events(), local_day(datetime.now(UTC)))
    from conftest import FIXTURES  # an importer: a small ChatGPT export
    assert importers.chats.import_export(FIXTURES / "exports" / "chatgpt_conversations.json")
    dashboard.build(vault, now=datetime.now(UTC))  # the dashboard build
    assert not offline, "outbound connections:\n" + "\n".join(f"{h}\n{st}" for h, st in offline)


def test_the_app_status_board_makes_no_outbound_connection(vault, offline):
    from keepfold.app import server
    srv, app = server.make_server(vault.root)  # fakes: PORT 0, never the owner's port
    try:
        d = app.status()
    finally:
        srv.server_close()
    assert d["connects"] and all(isinstance(x, str) for x in d["connects"])
    assert not offline, "outbound connections:\n" + "\n".join(f"{h}\n{st}" for h, st in offline)
