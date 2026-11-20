"""Plan v9 item 8: what other agents get. The Cortex block in Cursor, VS Code (Copilot) and
Codex rules files (add, refresh, remove; the rest of the file untouched; backups), per-agent
profiles enforced over MCP (a restricted client gets no personality, profile, journal or
people text; profiles are set from the app only), and the context packs (at most 2,500
characters, never a private page or a hidden subject) with `cortex pack`."""

import json

import anyio
import pytest
from conftest import note
from mcp import Client
from mcp.types import Implementation
from typer.testing import CliRunner

from cortex import access, mcp_server, packs, profiles
from cortex.app import targets
from cortex.cli import app
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

CORE = "- Call me Sam.\n- Reply in British English.\n"
PREFS = ("# Preferences\n\n## Coding\n- Python first, type hints always.\n\n"
         "## Communication\n- Short answers, no fluff.\n\n"
         "## Personal\n- CANARY-PERS-4Q likes long walks.\n\n"
         "## Learning\n- I practise tarot reading daily.\n")
PERSONALITY = ("# Personality\n\n## Communication\n- Direct.\n\n"
               "## How I think\n- CANARY-THNK-4Q thinks in pictures.\n")
CATALOG = """schema: 1
domains:
  - id: "misc"
    title: "Misc"
    areas:
      - id: "misc.all"
        title: "All"
        concepts:
          - id: "zq4q-tarot"
            title: "Tarot reading"
            aliases: []
            requires: []
"""


@pytest.fixture
def vroot(tmp_path):
    r = tmp_path / "vault"
    (r / ".cortex").mkdir(parents=True)
    (r / "catalog.yaml").write_text(CATALOG, encoding="utf-8")
    note(r, "concepts/zq4q-tarot.md", "id: zq4q-tarot\ntitle: Tarot reading\nprivate: true\n",
         "# Tarot reading")
    note(r, "me/core.md", "id: core\n", "# Core\n\n## Core\n" + CORE)
    note(r, "me/preferences.md", "id: preferences\n", PREFS)
    note(r, "me/personality.md", "id: personality\n", PERSONALITY)
    note(r, "me/profile.md", "id: profile\nsummary: CANARY-PROF-4Q student in Kochi.\n",
         "# Profile")
    note(r, "journal/2026-11-20.md", "id: 2026-11-20\ntitle: Day\n",
         "- CANARY-JRNL-4Q saw the zebracorn today.")
    return r


def entry(root):
    return {"command": "C:\\tools\\cortex.exe", "args": ["mcp", "--vault", str(root)]}


def _baks(p):
    return sorted(p.parent.glob(p.name + ".cortex-*.bak"))


# ---- rules-file blocks -------------------------------------------------------------------


def test_codex_agents_md_block_add_refresh_remove(home, vroot):
    (home / ".codex").mkdir()
    md = home / ".codex" / "AGENTS.md"
    mine = "# My rules\n\nAlways run the tests.\n"
    md.write_bytes(mine.encode())
    t = targets.by_id()["codex"]
    rows = t.plan(True, entry(vroot))
    assert (str(md), "add") in [(r["file"], r["change"]) for r in rows]
    t.apply(entry(vroot))
    text = md.read_text(encoding="utf-8")
    assert text.startswith(mine) and text.count(targets.START) == 1
    assert "- Call me Sam.\n- Reply in British English.\n" in text
    assert packs.PRIME_LINE in text and "^c-" not in text
    assert [b.read_bytes() for b in _baks(md)] == [mine.encode()]  # backed up first
    t.apply(entry(vroot))  # idempotent: no second block, no new backup
    assert md.read_text(encoding="utf-8") == text and len(_baks(md)) == 1
    assert t.plan(True, entry(vroot)) == []

    # the core changes: the app's periodic check rewrites the block, nothing else
    md.write_text(text.replace(mine, "# My rules\n\nEdited by me.\n"), encoding="utf-8")
    note(vroot, "me/core.md", "id: core\n", "# Core\n\n## Core\n- Call me Alex.\n")
    done = targets.refresh_rules(entry(vroot))
    assert [(r["id"], r["change"]) for r in done] == [("codex", "update")]
    text = md.read_text(encoding="utf-8")
    assert "Call me Alex." in text and "Call me Sam." not in text
    assert text.startswith("# My rules\n\nEdited by me.\n")
    assert targets.refresh_rules(entry(vroot)) == []  # unchanged: nothing written

    t.remove()
    assert md.read_text(encoding="utf-8") == "# My rules\n\nEdited by me.\n"


def test_cursor_and_vscode_rules_files(home, vroot):
    (home / ".cursor").mkdir()
    vs = targets._vscode_user()
    vs.mkdir(parents=True)
    assert str(vs).startswith(str(home))
    for tid, path, head in (("cursor", home / ".cursor" / "rules" / "cortex.mdc",
                             targets.CURSOR_HEAD),
                            ("vscode", vs / "prompts" / "cortex.instructions.md",
                             targets.VSCODE_HEAD)):
        t = targets.by_id()[tid]
        t.apply(entry(vroot))
        text = path.read_text(encoding="utf-8")
        assert text.startswith(head) and "Call me Sam." in text and packs.PRIME_LINE in text
        assert not _baks(path)  # a new file: nothing to back up
        assert str(path) in next(r for r in targets.describe(entry(vroot))
                                 if r["id"] == tid)["files"]
        t.remove()
        assert not path.exists() and len(_baks(path)) == 1  # only Cortex's own rule was in it


def test_rules_file_with_owner_text_keeps_it(home, vroot):
    (home / ".cursor").mkdir()
    p = home / ".cursor" / "rules" / "cortex.mdc"
    p.parent.mkdir()
    own = b"---\r\nalwaysApply: false\r\n---\r\nMy own rule.\r\n"  # a Windows file
    p.write_bytes(own)
    t = targets.by_id()["cursor"]
    t.apply(entry(vroot))
    assert p.read_bytes().startswith(own) and b"Call me Sam.\r\n" in p.read_bytes()
    t.remove()
    assert p.read_bytes() == own


@pytest.mark.parametrize("bad", [f"x\n{targets.START}\nno end\n", "\udcff"])
def test_unparseable_rules_file_is_left_alone(home, vroot, bad):
    (home / ".codex").mkdir()
    md = home / ".codex" / "AGENTS.md"
    raw = bad.encode("utf-8", "surrogateescape")
    md.write_bytes(raw)
    t = targets.by_id()["codex"]
    row = next(r for r in t.plan(True, entry(vroot)) if r["file"] == str(md))
    assert row["change"] == "error" and "untouched" in row["detail"]
    with pytest.raises(ValueError, match="untouched"):
        t.apply(entry(vroot))
    assert md.read_bytes() == raw and not _baks(md)


def test_block_without_a_vault_is_the_prime_line(home):
    (home / ".codex").mkdir()
    t = targets.by_id()["codex"]
    t.apply({"command": "x", "args": ["mcp", "--vault", str(home / "nowhere")]})
    text = (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert packs.PRIME_LINE in text and "About me" not in text


def test_private_core_and_hidden_subjects_stay_out(home, vroot):
    note(vroot, "me/core.md", "id: core\nprivate: true\n",
         "# Core\n\n## Core\n- CANARY-CORE-4Q is private.\n")
    note(vroot, "me/preferences.md", "id: preferences\nprivate: true\n", PREFS)
    (home / ".codex").mkdir()
    targets.by_id()["codex"].apply(entry(vroot))
    block = (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    out = block + "".join(packs.pack(Vault(vroot), n) for n in packs.PACKS)
    for canary in ("canary-core", "canary-pers", "python first", "tarot"):
        assert canary not in out.lower(), canary
    assert "CANARY-THNK-4Q" in packs.pack(Vault(vroot), "general")  # personality is not private


# ---- context packs -----------------------------------------------------------------------


def test_packs_core_first_scoped_and_capped(vroot):
    v = Vault(vroot)
    general, coding, writing = (packs.pack(v, n) for n in ("general", "coding", "writing"))
    for p in (general, coding, writing):
        assert p.startswith(packs.PACK_HEAD) and len(p) <= packs.PACK_MAX
        assert p.index("Call me Sam.") < p.index("Preferences:")
        assert "tarot" not in p.lower()  # names a hidden (private) subject
        assert "CANARY-PROF" not in p and "CANARY-JRNL" not in p  # not preference lines
    assert "Python first" in coding and "Short answers" not in coding
    assert "Short answers" in writing and "Direct." in writing and "Python" not in writing
    assert "CANARY-PERS-4Q" in general and "CANARY-PERS-4Q" not in coding + writing
    # long pages: whole lines up to the cap, never a cut line
    many = "".join(f"- Coding rule {k} " + "x" * 200 + "\n" for k in range(40))
    note(vroot, "me/preferences.md", "id: preferences\n", "# P\n\n## Coding\n" + many)
    big = packs.pack(v, "coding")
    assert 2000 < len(big) <= packs.PACK_MAX
    served = [ln.replace("[Coding] ", "") for ln in big.splitlines()
              if ln.startswith("- [Coding]")]
    assert served and all(ln + "\n" in many for ln in served)


def test_unconfirmed_lines_are_not_packed(vroot):
    from cortex.events import EventLog
    note(vroot, "me/preferences.md", "id: preferences\n",
         "# P\n\n## Coding\n- Typed by me.\n- Agent guessed this. ^c-bbbbb\n")
    EventLog(Vault(vroot)).append(session="cli", kind="change", source="tutor",
                                  fields={"payload": {"change": {
                                      "id": "preferences", "kind": "me", "tier": "agent",
                                      "claim": "c-bbbbb"}}})
    p = packs.pack(Vault(vroot), "coding")
    assert "Typed by me." in p and "Agent guessed" not in p


def test_empty_vault_has_no_pack(tmp_path):
    (tmp_path / ".cortex").mkdir()
    assert packs.pack(Vault(tmp_path), "general") == ""
    with pytest.raises(ValueError):
        packs.pack(Vault(tmp_path), "poetry")


def test_cli_pack(vroot):
    r = CliRunner().invoke(app, ["pack", "coding", "--vault", str(vroot)])
    assert r.exit_code == 0 and "Python first" in r.output and "Call me Sam." in r.output
    assert "CANARY-PERS" not in r.output
    assert CliRunner().invoke(app, ["pack", "poetry", "--vault", str(vroot)]).exit_code == 2


# ---- per-agent profiles over MCP ---------------------------------------------------------


def serve(root, client, body):
    server = mcp_server.build_server(Vault(root))

    async def main():
        async with Client(server, client_info=Implementation(name=client, version="1")) as c:
            async def call(name, args=None):
                r = await c.call_tool(name, args or {})
                return r.structured_content or {"text": str(r.content)}
            return await body(call)

    return anyio.run(main)


async def _sweep(call):
    out = [await call("cortex_prime", {"prompt": "help me write python code and walk"}),
           await call("cortex_context", {"scope": "all"}),
           await call("cortex_context", {"scope": "personal"}),
           await call("cortex_find", {"query": "zebracorn"}),
           await call("cortex_find", {"query": "walks"}),
           await call("cortex_read", {"id": "2026-11-20"}),
           await call("cortex_read", {"id": "profile"}),
           await call("cortex_read", {"id": "personality"}),
           await call("cortex_chats", {})]
    return json.dumps(out)


@pytest.mark.parametrize("client, seen", [
    ("cursor-vscode", set()), ("Visual Studio Code", set()), ("codex-mcp-client", set()),
    ("mcp", {"THNK"}),  # unknown: core, coding and learning ("How I think")
    ("claude-ai", {"THNK", "PERS", "PROF", "JRNL"})])
def test_profiles_scope_what_a_client_gets(vroot, client, seen):
    out = serve(vroot, client, _sweep)
    assert "Call me Sam." in out and "Python first" in out  # core and coding: everyone
    for canary in ("THNK", "PERS", "PROF", "JRNL"):
        assert (f"CANARY-{canary}-4Q" in out) == (canary in seen), (client, canary)
    assert ("does not share personal pages" in out) == (client != "claude-ai")


def test_owner_profiles_change_it_and_bad_files_fall_back(vroot):
    v = Vault(vroot)
    data = profiles.load(v)
    assert data == profiles.DEFAULT and profiles.scopes_for(data, "claude-code") is None
    data["profiles"].insert(0, {"match": "Cursor", "scopes": ["core", "personal"]})
    profiles.save(v, data)
    out = serve(vroot, "cursor-vscode", _sweep)
    assert "CANARY-JRNL-4Q" in out and "CANARY-PROF-4Q" in out and "Python first" not in out
    (vroot / profiles.PATH).write_text("{broken", encoding="utf-8")
    assert profiles.load(v) == profiles.DEFAULT
    with pytest.raises(ValueError):
        profiles.check({"profiles": [{"match": "x", "scopes": ["everything"]}], "unknown": []})
    with pytest.raises(ValueError):
        profiles.check({"profiles": [{"match": "--", "scopes": []}], "unknown": []})


def test_no_mcp_tool_can_set_profiles(vroot):
    server = mcp_server.build_server(Vault(vroot))

    async def main():
        async with Client(server) as c:
            return (await c.list_tools()).tools

    for tool in anyio.run(main):
        assert not any("profile" in k or "scope" == k and tool.name != "cortex_context"
                       for k in (tool.input_schema.get("properties") or {})), tool.name
    assert not (vroot / profiles.PATH).exists()


def test_in_process_callers_see_everything(vroot):
    core = mcp_server.Cortex(Vault(vroot))  # the app, the CLI: no MCP client
    assert core._scopes() is None and core._withheld() == set()


def test_scope_rules():
    s = frozenset(("core", "coding"))
    assert access.scope_page(s, "core") and access.scope_page(s, "teaching-contract")
    assert not access.scope_page(s, "profile") and not access.scope_page(s, "voice")
    assert access.scope_section(s, "preferences", "Coding")
    assert not access.scope_section(s, "preferences", "Personal")
    assert not access.scope_section(s, "personality", "How I think")
    assert not access.scope_kind(s, "journal") and access.scope_kind(s, "concept")
    assert access.scope_section(None, "personality", "x") and access.scope_kind({"all"}, "person")


# ---- the app -----------------------------------------------------------------------------


def test_app_shows_packs_and_saves_profiles(vroot):
    from test_app import _serve, call
    srv, a = _serve(vroot)
    try:
        _, js, _, _ = call(a, "GET", "/api/connect")
        got = {p["id"]: p["text"] for p in js["packs"]}
        assert set(got) == set(packs.PACKS) and "Python first" in got["coding"]
        assert js["profiles"] == profiles.DEFAULT and js["scopes"] == list(access.SCOPES)
        new = {"profiles": [{"match": "cursor", "scopes": ["core"]}], "unknown": ["core"]}
        status, saved, _, _ = call(a, "POST", "/api/profiles", new)
        assert status == 200 and saved["profiles"] == new
        assert profiles.load(Vault(vroot)) == new
        assert call(a, "POST", "/api/profiles", {"profiles": "all"})[0] == 400
    finally:
        srv.shutdown()
        srv.server_close()
