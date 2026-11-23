"""Private browse search for local AIs: browse/vectors.py, `cortex mcp --private` and the
normal server, which never reaches browse/private/ (browse-brain.md, "Private search")."""

import anyio
import pytest
from conftest import FakeEmbed, note, run
from typer.testing import CliRunner

from cortex import mcp_server, semantic
from cortex.browse import vectors
from cortex.cli import app
from cortex.vault import Vault, VaultError

pytestmark = pytest.mark.usefixtures("home")

PRIV = "browse/private/browse-private-2026-11-09.md"
PUB = "browse/browse-2026-11-09.md"


def pages(root):
    note(root, PRIV, "private: true\n",
         "# Browsing 2026-11-09 (private)\n\n## Pages\n- Zebracorn pasta recipe kitchen\n")
    note(root, PUB, "", "# Browsing 2026-11-09\n\n## Hubs\n- 20 min, 3 pages\n")
    note(root, "browse/private/browse-private-2026-11-10.md", "private: true\n",
         "# Browsing 2026-11-10 (private)\n\n## Pages\n- Motor vehicle review\n")


@pytest.fixture
def fake(monkeypatch):
    f = FakeEmbed()
    monkeypatch.setattr(semantic, "_embedder", f)
    return f


def config(home, text):
    (home / ".cortex").mkdir(exist_ok=True)
    (home / ".cortex" / "config.yaml").write_text(text, encoding="utf-8")


# ---- vectors ------------------------------------------------------------------------------


def test_vector_update_is_incremental_and_counts_only(root, fake):
    pages(root)
    v, said = Vault(root), []
    out = vectors.update(v, said.append)
    assert out == {"pages": 3, "embedded": 3, "removed": 0, "chunks": 3}
    assert (root / vectors.CACHE).is_file()
    assert said and not any("Zebracorn" in s or "Motor" in s or "browse/" in s for s in said)

    fake.texts.clear()
    assert vectors.update(v)["embedded"] == 0
    assert fake.texts == ["cortex"]  # only the dimension probe: nothing re-embedded

    p = root / "browse/private/browse-private-2026-11-10.md"
    p.write_text(p.read_text(encoding="utf-8") + "- More car news\n", encoding="utf-8")
    (root / PUB).unlink()
    out = vectors.update(v)
    assert (out["pages"], out["embedded"], out["removed"]) == (2, 1, 1)
    assert set(vectors._read(v)) == {PRIV, "browse/private/browse-private-2026-11-10.md"}


def test_vector_search_reaches_private_pages(root, fake):
    pages(root)
    hits = vectors.search(Vault(root), "pasta cooking", k=8)
    assert hits[0]["path"] == PRIV and hits[0]["title"] == "Browsing 2026-11-09 (private)"
    assert "Zebracorn" in hits[0]["snippet"] and 0 < hits[0]["score"] <= 1
    assert [h["path"] for h in vectors.search(Vault(root), "automobile", 1)] == [
        "browse/private/browse-private-2026-11-10.md"]
    assert vectors.search(Vault(root), "   ") == []


def test_vector_chunks_long_pages(root, fake):
    note(root, PRIV, "private: true\n", "# Day\n" + "- filler line here\n" * 200
         + "- pasta at the end\n")
    vectors.update(Vault(root))
    starts = vectors._read(Vault(root))[PRIV][1]
    assert len(starts) > 2
    assert "pasta at the end" in vectors.search(Vault(root), "pasta")[0]["snippet"]


def test_vector_without_model_says_so(root):
    pages(root)
    with pytest.raises(VaultError) as e:
        vectors.update(Vault(root))
    assert e.value.code == "not_set_up" and not (root / vectors.CACHE).exists()


# ---- the MCP servers ----------------------------------------------------------------------


def tools(server):
    return {t.name for t in server._tool_manager.list_tools()}


def test_normal_server_has_no_private_search_and_hides_private_pages(root, fake):
    pages(root)
    assert "cortex_browse_search" not in tools(mcp_server.build_server(Vault(root)))

    async def body(call):
        found = await call("cortex_find", {"query": "Zebracorn"})
        assert "Zebracorn" not in str(found)
        recalled = await call("cortex_recall", {"query": "pasta recipe"})
        assert "Zebracorn" not in str(recalled) and "browse-private" not in str(recalled)
        read = await call("cortex_read", {"id": "browse-private-2026-11-09"})
        assert "error" in read and "Zebracorn" not in str(read)

    run(root, body)
    assert not (root / vectors.CACHE).exists()  # nothing embedded the private pages


def test_private_server_searches_and_wraps(root, home, fake):
    pages(root)
    config(home, "browse:\n  private_mcp: true\n")
    server = mcp_server.build_server(Vault(root), private=True)
    assert "cortex_browse_search" in tools(server)
    excluded = mcp_server.build_server(Vault(root), excluded="D:\\Work", private=True)
    assert "cortex_browse_search" not in tools(excluded)

    from mcp import Client

    async def main():
        async with Client(server) as c:
            r = await c.call_tool("cortex_browse_search", {"query": "pasta", "k": 2})
            return r.structured_content

    out = anyio.run(main)
    assert out["hits"][0]["path"] == PRIV and out["next"]
    config(home, "browse:\n  private_mcp: false\n")  # turned off while the server runs
    off = anyio.run(main)
    assert off["error"] == "refused" and "Zebracorn" not in str(off)
    config(home, "browse:\n  private_mcp: true\n")
    out = anyio.run(main)
    assert out["text"].startswith('<untrusted_note id="browse">') and "Zebracorn" in out["text"]
    assert "Zebracorn" not in str(out["hits"])  # page text only inside the wrapper


def test_cli_private_refused_unless_turned_on(root, home, monkeypatch):
    seen = []
    monkeypatch.setattr(mcp_server, "serve", lambda *a, **k: seen.append(k))
    r = CliRunner().invoke(app, ["mcp", "--private", "--vault", str(root)])
    assert r.exit_code == 1 and "private_mcp" in r.output and not seen
    config(home, "browse:\n  private_mcp: yes please\n")  # only a real true counts
    assert CliRunner().invoke(app, ["mcp", "--private", "--vault", str(root)]).exit_code == 1
    config(home, "browse:\n  private_mcp: true\n")
    r = CliRunner().invoke(app, ["mcp", "--private", "--vault", str(root)])
    assert r.exit_code == 0, r.output
    assert seen == [{"private": True}]
    CliRunner().invoke(app, ["mcp", "--vault", str(root)])
    assert seen[-1] == {"private": False}


@pytest.mark.parametrize("name, refused", [("claude-ai", True), ("antigravity-client", True),
                                            ("Cline", True), ("LM Studio", False)])
def test_private_search_refuses_cloud_clients(root, home, fake, name, refused):
    from mcp import Client
    from mcp.types import Implementation

    pages(root)
    config(home, "browse:\n  private_mcp: true\n")
    server = mcp_server.build_server(Vault(root), private=True)

    async def main():
        async with Client(server, client_info=Implementation(name=name, version="1")) as c:
            r = await c.call_tool("cortex_browse_search", {"query": "pasta"})
            return r.structured_content

    out = anyio.run(main)
    assert (out.get("error") == "refused") == refused and ("Zebracorn" in str(out)) != refused
