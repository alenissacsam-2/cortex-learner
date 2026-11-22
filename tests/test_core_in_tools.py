"""B2-c: the owner's pinned core (exactly what packs.rule_block writes into a rules file for
that client) rides in cortex_prime's tool description, so an app with no rules file (Claude
Desktop, Gemini) has the standing rules even when its model skips cortex_prime."""

import os

import anyio
import pytest
from conftest import note
from mcp import Client
from mcp.types import Implementation

from cortex import mcp_server, profiles
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

CORE = "- Answer in short sentences.\n- Never use semicolons.\n"
HEAD = mcp_server.CORE_RULES_HEAD


@pytest.fixture
def vroot(tmp_path):
    r = tmp_path / "vault"
    (r / ".cortex").mkdir(parents=True)
    (r / "catalog.yaml").write_text("schema: 1\ndomains: []\n", encoding="utf-8")
    note(r, "me/core.md", "id: core\n", "# Core\n\n## Core\n" + CORE)
    return r


def tools(root, client, **kw):
    server = mcp_server.build_server(Vault(root), **kw)

    async def main():
        async with Client(server, client_info=Implementation(name=client, version="1")) as c:
            return {t.name: t.description for t in (await c.list_tools()).tools}

    return anyio.run(main)


def test_core_in_prime_description_for_desktop_and_gemini(vroot):
    plain = mcp_server.DESCRIPTIONS["cortex_prime"]
    for client in ("claude-ai", "gemini-cli-mcp-client"):
        got = tools(vroot, client)
        assert got["cortex_prime"] == (f"{plain}\n\n{HEAD}\n- Answer in short sentences.\n"
                                       "- Never use semicolons.")
        assert "cortex_prime(prompt=" not in got["cortex_prime"]  # no prime line from the block
        for name, desc in mcp_server.DESCRIPTIONS.items():  # no other tool changes
            if name != "cortex_prime":
                assert got[name] == desc


@pytest.mark.parametrize("kw", [{"role": "reader"}, {"excluded": "D:/x"}])
def test_not_for_reader_or_excluded(vroot, kw):
    assert HEAD not in tools(vroot, "claude-ai", **kw)["cortex_prime"]


def test_not_for_claude_code(vroot):
    assert tools(vroot, "claude-code")["cortex_prime"] == mcp_server.DESCRIPTIONS["cortex_prime"]


def test_nothing_without_pinned_lines_or_private_core(vroot):
    note(vroot, "me/core.md", "id: core\n", "# Core\n\n## Core\n")
    assert HEAD not in tools(vroot, "claude-ai")["cortex_prime"]
    note(vroot, "me/core.md", "id: core\nprivate: true\n", "# Core\n\n## Core\n" + CORE)
    assert "short sentences" not in tools(vroot, "claude-ai")["cortex_prime"]


def test_profile_without_core_scope_gets_none(vroot):
    profiles.save(Vault(vroot), {"profiles": [{"match": "gemini", "scopes": ["coding"]}],
                                 "unknown": ["core"]})
    assert HEAD not in tools(vroot, "gemini-cli-mcp-client")["cortex_prime"]
    assert "short sentences" in tools(vroot, "some-other-app")["cortex_prime"]


def test_cap_whole_lines(vroot):
    many = "".join(f"- Rule {i}: " + "x" * 90 + "\n" for i in range(40))
    note(vroot, "me/core.md", "id: core\n", "# Core\n\n## Core\n" + many)
    desc = tools(vroot, "claude-ai")["cortex_prime"]
    added = desc[len(mcp_server.DESCRIPTIONS["cortex_prime"]) + 2:]
    assert added.startswith(HEAD) and len(added) <= mcp_server.CORE_RULES_MAX
    assert all(ln == HEAD or ln.endswith("x" * 90) for ln in added.splitlines())
    assert len(desc) < 1500  # well under the 2,048 some clients allow


def test_changed_core_shows_at_next_list(vroot):
    server = mcp_server.build_server(Vault(vroot))

    async def main():
        async with Client(server, client_info=Implementation(name="claude-ai", version="1")) as c:
            async def prime():
                return next(t.description for t in (await c.list_tools()).tools
                            if t.name == "cortex_prime")
            before = await prime()
            assert before == await prime()  # cached: same key, same text
            p = vroot / "me" / "core.md"
            note(vroot, "me/core.md", "id: core\n", "# Core\n\n## Core\n- Always cite pages.\n")
            st = p.stat()
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
            return before, await prime()

    before, after = anyio.run(main)
    assert "short sentences" in before and "short sentences" not in after
    assert "- Always cite pages." in after


def _prefix(root, client, monkeypatch, now):
    """Everything a client caches in its prompt prefix from the server: the instructions and
    each tool's name, description and annotations, at server time `now`."""
    monkeypatch.setattr(mcp_server.Cortex, "_now", lambda self: now)
    server = mcp_server.build_server(Vault(root))

    async def main():
        async with Client(server, client_info=Implementation(name=client, version="1")) as c:
            return c.instructions, [(t.name, t.description, t.annotations.model_dump())
                                    for t in (await c.list_tools()).tools]

    return anyio.run(main)


def test_prefix_text_is_the_same_bytes_across_a_day_with_no_edits(vroot, monkeypatch):
    """P1: the server instructions, the tool list (core lines included) and the rules-file
    block hold no date, count or recent item: the next day, with no edit, they are identical,
    so a client's prompt cache keeps working. Only a line's own (until …) day ends it."""
    from datetime import UTC, datetime, timedelta

    from cortex import packs
    from cortex.events import local_day

    day1 = datetime(2026, 11, 23, 12, tzinfo=UTC)
    day2 = day1 + timedelta(days=1)
    v = Vault(vroot)
    a, b = _prefix(vroot, "claude-ai", monkeypatch, day1), _prefix(vroot, "claude-ai",
                                                                     monkeypatch, day2)
    assert a == b and "short sentences" in dict((n, d) for n, d, _ in a[1])["cortex_prime"]
    assert packs.rule_block(v, day1).encode() == packs.rule_block(v, day2).encode()
    assert packs.rule_block(v, day1) == packs.rule_block(v, day1 + timedelta(days=30))

    until = f"- Exam week focus (until {local_day(day1)})."
    note(vroot, "me/core.md", "id: core\n", "# Core\n\n## Core\n" + CORE + until + "\n")
    a, b = _prefix(vroot, "claude-ai", monkeypatch, day1), _prefix(vroot, "claude-ai",
                                                                     monkeypatch, day2)
    assert a[0] == b[0] and [t for t in a[1] if t[0] != "cortex_prime"] == [
        t for t in b[1] if t[0] != "cortex_prime"]
    prime_a, prime_b = (dict((n, d) for n, d, _ in x[1])["cortex_prime"] for x in (a, b))
    assert "Exam week" in prime_a and prime_b == prime_a.replace("\n" + until, "")
    assert "Exam week" in packs.rule_block(v, day1)
    assert "Exam week" not in packs.rule_block(v, day2)


def test_tool_annotations(vroot):
    server = mcp_server.build_server(Vault(vroot))

    async def main():
        async with Client(server) as c:
            return {t.name: t.annotations for t in (await c.list_tools()).tools}

    got = anyio.run(main)
    assert {n for n, a in got.items() if a.read_only_hint} == set(mcp_server.READ_ONLY_TOOLS)
    assert all(a.open_world_hint is False for a in got.values())
    assert got["cortex_patch"].read_only_hint is False
