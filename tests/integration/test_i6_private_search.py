"""I6 MCP private search <-> settings (docs/design/v6/TEST-PLAN.md §3), on the I3 vault: the
private server refuses cloud clients by name and answers a local one; the normal server has no
private search at all. In-process only."""

import anyio
import pytest
from mcp import Client
from mcp.types import Implementation
from test_browse import Embed

from integration.test_i3_browse_privacy import PLAN, imp
from integration.test_i3_browse_privacy import local as local  # noqa: F401  (fixture)
from integration.test_i3_browse_privacy import vault as vault  # noqa: F401  (fixture)
from keepfold import mcp_server, semantic
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")


@pytest.fixture
def server(vault, local, home, monkeypatch):  # noqa: F811
    monkeypatch.setattr(semantic, "_embedder", Embed())
    imp(vault, local, PLAN)
    (home / ".keepfold").mkdir(exist_ok=True)
    (home / ".keepfold" / "config.yaml").write_text("browse:\n  private_mcp: true\n",
                                                  encoding="utf-8")
    return mcp_server.build_server(Vault(vault), private=True)


def search(server, name: str) -> dict:
    async def main():
        async with Client(server, client_info=Implementation(name=name, version="1")) as c:
            r = await c.call_tool("keepfold_browse_search",
                                  {"query": "pasta recipe cooking", "k": 20})
            return r.structured_content

    return anyio.run(main)


@pytest.mark.parametrize("name", ["claude-ai", "Claude Desktop", "gemini-cli", "ChatGPT",
                                  "cursor", "vscode", "Visual Studio Code", "code"])
def test_cloud_clients_are_refused(server, name):
    out = search(server, name)
    assert out["error"] == "refused", out
    assert "pasta" not in str(out).lower() and "browse/" not in str(out)


def test_a_local_client_gets_the_private_pages(server):
    out = search(server, "lm-studio")
    assert "error" not in out, out
    assert any(h["path"].startswith("browse/private/") for h in out["hits"])
    assert out["text"].startswith('<untrusted_note id="browse">')


def test_the_normal_server_has_no_private_search(vault, local):  # noqa: F811
    imp(vault, local, PLAN)
    tools = {t.name for t in mcp_server.build_server(Vault(vault))._tool_manager.list_tools()}
    assert "keepfold_browse_search" not in tools and "keepfold_find" in tools
