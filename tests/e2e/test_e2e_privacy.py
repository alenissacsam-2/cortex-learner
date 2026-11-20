"""E2 Privacy (docs/design/v6/TEST-PLAN.md §4), in-process. The owner's rule: Claude may see
browsing except the hidden categories. A synthetic History with adult, health, money, bank,
sign-in and secret-URL rows goes through `cortex browse import` under three settings; then
every surface that leaves the vault (public browse files, index rows, the MCP read tools,
prime, Today.md, the dashboard, the app's status JSON, the main vectors, the cloud runner's
input and every command's output) is collected and grepped for every hidden host, title and
term. A BOM + CRLF `private: true` note and a private assessment must stay out of all of it."""

import io
import json
from datetime import UTC, date, datetime, timedelta

import anyio
import pytest
from conftest import FakeEmbed, events
from integration.builders import fake_profile_root
from integration.test_i3_browse_privacy import DROPPED, MARKERS, history_rows
from mcp import Client
from ruamel.yaml import YAML
from test_browse import Embed, hubbed
from typer.testing import CliRunner

from cortex import cloudllm, dashboard, localllm, mcp_server, semantic
from cortex.app import server as app_server
from cortex.app import status as app_status
from cortex.cli import app
from cortex.events import live
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

ADULT = MARKERS["adult"]
SENSITIVE = MARKERS["health"] + MARKERS["money"]
PRIVATE_NOTE = ("zqprivnote", "zqdiaryline")
PRIVATE_OA = ("zqprivco", "zqoaline")
NEVER_OUT = DROPPED + ADULT + SENSITIVE + PRIVATE_NOTE + PRIVATE_OA
KEY_ENV, KEY = "E2E_CLOUD_KEY", "e2e-cloud-key-123"


class Opener:
    """The cloud endpoint: records every request body; answers like an OpenAI chat API."""

    def __init__(self):
        self.reqs = []

    def open(self, req, timeout=None):
        self.reqs.append(req)
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(
            {"category": "other", "description": "A site.", "related": [],
             "topics": []})}}]}).encode())

    def sent(self) -> str:
        return "\n".join(r.data.decode("utf-8") for r in self.reqs)


def set_config(home, browse: dict, vault) -> None:
    p = home / ".cortex" / "config.yaml"
    p.parent.mkdir(exist_ok=True)
    data = {"vault": str(vault), "browse": browse,
            "cloudllm": {"provider": "openai", "model": "m", "api_key_env": KEY_ENV}}
    with p.open("w", encoding="utf-8") as f:
        YAML(typ="safe").dump(data, f)


def cli(*args) -> str:
    r = CliRunner().invoke(app, [str(a) for a in args])
    assert r.exit_code == 0, (args, r.output, r.exception)
    return r.output


def private_pages(root) -> None:
    """Step 6's inputs, written as an editor would: a BOM + CRLF private note (verdict #1) on
    a topic recall would find, and a private assessment due within the planner's window (#3)."""
    (root / "notes").mkdir(exist_ok=True)
    (root / "notes" / "kitchen-diary.md").write_bytes(
        "﻿---\r\nid: kitchen-diary\r\ntitle: ZQPRIVNOTE pasta recipe cooking\r\n"
        "kind: note\r\nprivate: true\r\n---\r\n# ZQPRIVNOTE\r\n\r\nzqdiaryline pasta recipe "
        "cooking kitchen\r\n".encode())
    due = (date.today() + timedelta(days=3)).isoformat()
    (root / "assessments" / "secret-oa.md").write_text(
        f'---\nid: "secret-oa"\ncompany: "ZQPRIVCO"\nkind: "oa"\ndate: "{due}"\n'
        'outcome: "pending"\nprivate: true\ntests: {"domain:dsa": 1.0}\n---\n'
        "# ZQPRIVCO OA\n\n## Notes\nzqoaline\n", encoding="utf-8")


def tree(root, under: str = "") -> dict[str, bytes]:
    base = root / under if under else root
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in base.rglob("*") if p.is_file()} if base.is_dir() else {}


def hits(blobs: dict[str, str | bytes], markers) -> list[tuple[str, str]]:
    out = []
    for name, data in blobs.items():
        low = (data if isinstance(data, bytes) else data.encode("utf-8")).lower()
        out += [(name, w) for w in markers if w.encode() in low]
    return out


async def _mcp(root) -> tuple[dict[str, str], str, set[str]]:
    """Every MCP read surface, the index rows, and the ids the index holds."""
    srv = mcp_server.build_server(Vault(root))
    out: dict[str, str] = {}
    async with Client(srv) as c:
        async def call(name, args):
            r = await c.call_tool(name, args)
            assert not r.is_error, (name, args, r.content)
            text = json.dumps(r.structured_content) + "".join(
                getattr(x, "text", "") for x in r.content)
            out[f"{name} {json.dumps(args)}"] = text
            return r.structured_content or {}

        found = set()
        for q in ("pasta recipe cooking", "browsing", "browse", "flu", "holdings", "clip",
                  "google search", "ZQPRIVNOTE", "ZQPRIVCO", "zqhealthterm", "Acme"):
            got = await call("cortex_find", {"query": q})
            found |= {h["id"] for h in got.get("hits", [])}
        for kind in ("assessment", "note", "due"):
            got = await call("cortex_find", {"kind": kind})
            found |= {h["id"] for h in got.get("hits", [])}
        for q in ("pasta recipe cooking kitchen", "health flu symptoms money holdings adult",
                  "what did I browse this week"):
            got = await call("cortex_recall", {"query": q})
            found |= {h["id"] for h in got.get("hits", [])}
        await call("cortex_prime", {"prompt": "What have I been browsing and cooking lately, "
                                              "and which assessment is next?"})
        for i in sorted(found | {"browse-index", "browse-private-index", "kitchen-diary",
                                 "secret-oa", "browse-private-2026-11-10", "browse-profile",
                                 "browse-sites"}):
            await call("cortex_read", {"id": i})
        index = srv.cortex.index
        rows = "\n".join(index.db.iterdump())
        ids = {r[0] for r in index.db.execute("SELECT id FROM nodes")}
    return out, rows, ids


def outward(root, opener: Opener, outputs: list[str]) -> dict[str, str | bytes]:
    """Step 4: everything that leaves the vault."""
    out: dict[str, str | bytes] = {
        f"file {k}": v for k, v in tree(root, "browse").items()
        if not k.startswith("browse/private/")}
    mcp, rows, _ = anyio.run(_mcp, root)
    out.update(mcp)
    out["index rows"] = rows
    outputs.append(cli("today", "--vault", root))
    out["Today.md"] = (root / "Today.md").read_bytes()
    outputs.append(cli("dashboard", "--no-open", "--vault", root))
    out["index.md"] = (root / "index.md").read_bytes()
    out["snapshot"] = dashboard.snapshot(Vault(root), now=datetime.now(UTC))  # Export snapshot
    a = app_server.App(root)
    out["app status"] = json.dumps([a.status(), a.browse(), a.vault()], default=str)
    out["vectors"] = " ".join(f"{i} {e[0]}" for i, e in semantic._read(root).items())
    out["cloud input"] = opener.sent()
    out["command output"] = "\n".join(outputs)
    return out


@pytest.fixture
def world(root, home, monkeypatch):
    """The I3 History under the fake %LOCALAPPDATA%, the hubbed vault with step 6's private
    pages, the fake embedder, the fake cloud endpoint, and no local model or receiver."""
    fake_profile_root(home / "LOCALAPPDATA", {"chrome": history_rows()})
    hubbed(root)
    private_pages(root)
    monkeypatch.setattr(semantic, "_embedder", Embed())
    monkeypatch.setenv(KEY_ENV, KEY)
    opener = Opener()
    monkeypatch.setattr(cloudllm, "_OPENER", opener)

    def no_network(url, body, timeout):  # status probes of Ollama and LM Studio
        raise OSError("no local model in E2")
    monkeypatch.setattr(localllm, "_request", no_network)
    monkeypatch.setattr(app_status, "RECEIVER_PORT", 1)  # nothing listens; never a real one
    return root, opener


@pytest.mark.slow  # about 20 s
def test_e2_privacy(world, home):
    root, opener = world
    outputs: list[str] = []
    assert isinstance(semantic.load_embedder(), Embed | FakeEmbed)

    # 1-2. visible off (the default): only browse/private/ is written
    set_config(home, {}, root)
    outputs.append(cli("browse", "import", "--vault", root))
    browse = tree(root, "browse")
    assert browse and all(k.startswith("browse/private/") for k in browse), sorted(browse)
    assert not hits(tree(root), DROPPED + ADULT)  # dropped and skipped: nowhere at all

    # 3. visible on; health and money local only; adult never saved
    set_config(home, {"visible_to_claude": True, "hidden_from_claude": ["health", "money"],
                      "skip_categories": ["adult"], "cloud_processing": True}, root)
    outputs.append(cli("browse", "import", "--vault", root))
    everything = tree(root)  # .cortex and .git included
    assert not hits(everything, DROPPED + ADULT), hits(everything, DROPPED + ADULT)
    # private pages are committed to the vault's local git like any page (browse-brain.md §2,
    # "known and accepted"); .cortex/browse/ is the local archive's own cache
    outside = {k: v for k, v in everything.items()
               if not k.startswith(("browse/private/", ".cortex/browse/", ".git/"))}
    assert not hits(outside, SENSITIVE), hits(outside, SENSITIVE)
    private = tree(root, "browse/private")
    kept = b"".join(private.values()).lower()
    assert all(w.encode() in kept for w in SENSITIVE)
    public = tree(root, "browse")
    assert "browse/browse-2026-11-10.md" in public
    touches = [e for e in live(events(root))
               if str(e.get("key", "")).startswith("browse:touch:")]
    assert touches

    # 4. collect everything outward; the cloud runner (on, visible pages only) included
    outputs.append(cli("browse", "deepen", "--vault", root))
    assert opener.reqs, "the visible site pages went to the cloud runner"
    assert "pasta" in opener.sent().lower()
    out = outward(root, opener, outputs)
    leaks = hits(out, NEVER_OUT)
    assert not leaks, leaks
    assert any("browse-2026-11-10" in v for k, v in out.items()
               if k.startswith("cortex_find") and isinstance(v, str))  # the public page shows
    _, _, ids = anyio.run(_mcp, root)
    assert "browse-2026-11-10" in ids and not any(i.startswith("browse-private") for i in ids)

    # 5. visibility off and re-import: public pages, index rows and touches go; private stays
    set_config(home, {"hidden_from_claude": ["health", "money"],
                      "skip_categories": ["adult"], "cloud_processing": True}, root)
    outputs.append(cli("browse", "import", "--vault", root))
    assert all(k.startswith("browse/private/") for k in tree(root, "browse"))
    assert tree(root, "browse/private")
    assert not [e for e in live(events(root))
                if str(e.get("key", "")).startswith("browse:touch:")]
    sent_before = len(opener.reqs)
    cli("browse", "deepen", "--vault", root)
    assert len(opener.reqs) == sent_before  # nothing is cloud audience any more
    out = outward(root, opener, outputs)
    _, _, ids = anyio.run(_mcp, root)
    assert not any(i.startswith("browse") for i in ids), sorted(i for i in ids
                                                                 if i.startswith("browse"))
    assert not [k for k in out if k.startswith("file ")]
    # 6. the private note and assessment, and every hidden row, stay out of all of it
    leaks = hits(out, NEVER_OUT)
    assert not leaks, leaks
