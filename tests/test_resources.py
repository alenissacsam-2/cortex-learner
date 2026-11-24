"""R32b: read-only MCP resources (cortex://core, cortex://pack/<name>[/5000]) an app attaches
by hand, under the same per-app limits as the tools: an unnamed app gets the core only, a
profiled app the packs its scopes hold, never a private me/ line, nothing in an excluded
folder, and every read lands in the X-ray."""

import anyio
import pytest
from conftest import note
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import Implementation

from cortex import mcp_server, packs, share, xray
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

CORE = "- Call me Sam.\n- Reply in British English.\n"
PREFS = ("# Preferences\n\n## Coding\n- Python first, type hints always.\n\n"
         "## Communication\n- Short answers, no fluff.\n\n"
         "## Career\n- CANARY-CARE-5R wants a backend role.\n\n"
         "## Personal\n- CANARY-PERS-5R likes long walks.\n")
PERSONALITY = "# Personality\n\n## Communication\n- CANARY-DIRECT-5R is direct.\n"
PACK_URIS = [u for u in mcp_server.RESOURCES if u.startswith("cortex://pack/")]


@pytest.fixture
def vroot(tmp_path):
    r = tmp_path / "vault"
    (r / ".cortex").mkdir(parents=True)
    (r / "catalog.yaml").write_text("schema: 1\ndomains: []\n", encoding="utf-8")
    note(r, "me/core.md", "id: core\n", "# Core\n\n## Core\n" + CORE)
    note(r, "me/preferences.md", "id: preferences\n", PREFS)
    note(r, "me/personality.md", "id: personality\n", PERSONALITY)
    return r


def attach(root, client, reads=(), **kw):
    """(resources/list as {uri: resource}, {uri: text or the read's error message})."""
    server = mcp_server.build_server(Vault(root), **kw)

    async def main():
        async with Client(server, client_info=Implementation(name=client, version="1")) as c:
            listed = {str(r.uri): r for r in (await c.list_resources()).resources}
            got = {}
            for uri in reads:
                try:
                    got[uri] = (await c.read_resource(uri)).contents[0].text
                except MCPError as e:
                    got[uri] = f"ERROR {e}"
            return listed, got

    return anyio.run(main)


def test_every_pack_has_a_resource_with_a_plain_title():
    assert set(mcp_server.PACK_PURPOSE) == set(packs.PACKS)
    assert len(mcp_server.RESOURCES) == 1 + len(packs.PACKS) * len(packs.SIZES)
    name, title, desc = mcp_server.RESOURCES["cortex://pack/interview"]
    assert title == "Cortex: Interview pack" and "1,500" in desc
    assert mcp_server.RESOURCES["cortex://pack/interview/5000"][1] == (
        "Cortex: Interview pack (5,000 characters)")


def test_an_unnamed_app_gets_the_core_only(vroot):
    listed, got = attach(vroot, "some-other-app", [mcp_server.CORE_URI, "cortex://pack/coding"])
    assert list(listed) == [mcp_server.CORE_URI]
    r = listed[mcp_server.CORE_URI]
    assert r.title == "Cortex: Core" and r.mime_type == "text/plain" and r.description
    assert got[mcp_server.CORE_URI] == (
        '## Core\n<untrusted_note id="me/core">\n- Call me Sam.\n- Reply in British English.\n'
        "</untrusted_note>")
    assert got["cortex://pack/coding"].startswith("ERROR") and "not shared" in \
        got["cortex://pack/coding"]


def test_a_coding_app_gets_the_coding_pack_and_no_pack_beyond_its_scopes(vroot):
    listed, got = attach(vroot, "cursor", ["cortex://pack/coding", "cortex://pack/coding/5000",
                                           "cortex://pack/interview", "cortex://pack/general"])
    assert sorted(listed) == [mcp_server.CORE_URI, "cortex://pack/coding",
                              "cortex://pack/coding/5000"]
    coding = got["cortex://pack/coding"]
    assert coding == mcp_server._wrap("pack/coding", packs.pack(Vault(vroot), "coding"))
    assert "Call me Sam." in coding and "Python first" in coding
    assert "Short answers" not in coding and "CANARY" not in coding
    assert got["cortex://pack/coding/5000"] == mcp_server._wrap(
        "pack/coding", packs.pack(Vault(vroot), "coding", 5000))
    for uri in ("cortex://pack/interview", "cortex://pack/general"):  # listed nowhere, read no
        assert got[uri].startswith("ERROR"), uri


def test_an_app_with_every_scope_gets_every_pack(vroot):
    listed, got = attach(vroot, "claude-ai", ["cortex://pack/interview"])
    assert list(listed) == list(mcp_server.RESOURCES)
    text = got["cortex://pack/interview"]
    assert text.startswith('<untrusted_note id="pack/interview">\n' + packs.PACK_HEAD)
    assert "CANARY-CARE-5R" in text and "Python first" not in text


def test_a_private_me_line_never_appears(vroot):
    note(vroot, "me/personality.md", "id: personality\nprivate: true\n", PERSONALITY)
    note(vroot, "me/preferences.md", "id: preferences\nprivate: true\n", PREFS)
    listed, got = attach(vroot, "claude-ai", list(mcp_server.RESOURCES))
    out = "".join(got.values())
    assert "Call me Sam." in out and "CANARY" not in out and "Python first" not in out
    note(vroot, "me/core.md", "id: core\nprivate: true\n", "# Core\n\n## Core\n" + CORE)
    listed, got = attach(vroot, "claude-ai", [mcp_server.CORE_URI, *PACK_URIS])
    assert mcp_server.CORE_URI not in listed and got[mcp_server.CORE_URI].startswith("ERROR")
    assert "Call me Sam." not in "".join(got.values())


def test_an_excluded_folder_lists_none(vroot):
    listed, got = attach(vroot, "claude-ai", [mcp_server.CORE_URI], excluded="D:/x")
    assert listed == {} and got[mcp_server.CORE_URI].startswith("ERROR")


def test_a_reader_serves_only_pages_shared_with_work(vroot):
    listed, got = attach(vroot, "claude-ai", ["cortex://pack/general"], role="reader")
    assert mcp_server.CORE_URI not in listed and "cortex://pack/general" in listed
    assert "Call me Sam." not in got["cortex://pack/general"]  # me/core is not shared
    share.set_kill(Vault(vroot), True)
    _, got = attach(vroot, "claude-ai", ["cortex://pack/general"], role="reader")
    assert got["cortex://pack/general"].startswith("ERROR") and "kill switch" in \
        got["cortex://pack/general"]


def test_the_private_server_lists_what_the_owners_lists(vroot):
    assert list(attach(vroot, "lmstudio", private=True)[0]) == list(attach(vroot, "lmstudio")[0])


def test_a_read_lands_in_the_xray_with_the_client(vroot):
    attach(vroot, "claude-ai", ["cortex://pack/interview"])
    row = xray.rows(Vault(vroot))[-1]
    assert row["tool"] == "resource:cortex://pack/interview" and row["client"] == "claude-ai"
    assert len(row["served"]["core"]) == 2 and len(row["served"]["preferences"]) == 3
    assert all(i.startswith("^c-") for ids in row["served"].values() for i in ids)
    shown = xray.view(Vault(vroot))["clients"][0]
    assert shown["calls"][0]["tool"] == "resource:cortex://pack/interview"
    assert {i["title"] for b in shown["calls"][0]["served"] for i in b["items"]} >= {
        "Call me Sam.", "CANARY-CARE-5R wants a backend role."}


def test_resources_never_wait_for_the_index(vroot, monkeypatch):
    """A read that arrives before the start-up index build finishes is served at once: the
    resource path (_withheld, profiles, packs, the X-ray) never touches the index."""
    monkeypatch.setattr(mcp_server.Cortex, "_build_index", lambda self: None)  # never ready
    listed, got = attach(vroot, "cursor", ["cortex://pack/coding"])
    assert mcp_server.CORE_URI in listed and "Python first" in got["cortex://pack/coding"]


def test_listing_alone_records_nothing_and_instructions_are_unchanged(vroot):
    attach(vroot, "claude-ai")
    assert xray.rows(Vault(vroot)) == []
    server = mcp_server.build_server(Vault(vroot))
    assert server.instructions == mcp_server.INSTRUCTIONS and len(server.instructions) < 1550
