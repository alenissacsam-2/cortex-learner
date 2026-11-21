"""G3 and G5 (HERMES-GAPS): a later prompt of a primed chat gets only the Profile and
Preferences lines it brings that the chat was not served (the prime hook, chat_turn's
`context`), within 400 chars, never a muted, private or out-of-scope line; every 8th turn a
save nudge."""

import json

import anyio
import pytest
from mcp import Client
from mcp.types import Implementation

from cortex import delta, prime, prime_hook, xray
from cortex.index import _wrap
from cortex.mcp_server import build_server
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

DBMS = "".join(f"- DBMS rule {i}: tables first, then the SQL query, one worked example.\n"
               for i in range(14))  # past the 800-char preferences cap


@pytest.fixture
def vault(root, monkeypatch):
    (root / "me/preferences.md").write_text(
        "# Preferences\n\n## Learning\n" + DBMS
        + "\n## Communication\n- Email: formal greeting, at most five sentences.\n"
        "- Email subject lines under eight words.\n"
        "\n## Personal\n- Email my family in Malayalam.\n"
        "\n## Career\n- Email recruiters back within a day.\n", encoding="utf-8")
    (root / "me/personality.md").write_text(
        "---\nid: personality\ntitle: Personality\nprivate: true\n---\n"
        "# Personality\n\n## Communication\n"
        "- Email tone: warm, never stiff.\n", encoding="utf-8")
    (root / ".cortex").mkdir(exist_ok=True)
    # Claude Code's profile leaves out Personal; the owner muted the Career line
    (root / ".cortex/profiles.json").write_text(json.dumps({"profiles": [
        {"match": "claude", "scopes": ["core", "coding", "communication", "learning",
                                       "career"]}], "unknown": ["core"]}), encoding="utf-8")
    cid = next(c for (p, s, t), c in prime.claim_ids(Vault(root)).items()
               if t.startswith("Email recruiters"))
    (root / xray.SETTINGS).write_text(json.dumps({"mute": [cid]}), encoding="utf-8")
    monkeypatch.setenv("CORTEX_VAULT", str(root))
    return root


def hook(prompt, sid="chat-1"):
    out = prime_hook.run(json.dumps({"session_id": sid, "hook_event_name": "UserPromptSubmit",
                                     "prompt": prompt}))
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def leaks(text):
    return [w for w in ("Malayalam", "recruiters", "warm, never stiff") if w in text]


def test_a_new_topic_brings_only_its_new_lines_once(vault):
    first = hook("Revise DBMS normalisation")
    assert first.startswith(prime_hook.HEAD) and "DBMS rule 13" in first
    assert "Email" not in first  # cut by the preferences cap
    second = hook("Write an email to my professor")
    assert second.startswith(delta.HEAD)
    block = second.split("\n", 1)[1]
    assert block.startswith('<untrusted_note id="me">') and len(block) <= delta.MAX
    assert "[Communication] Email: formal greeting" in block and "subject lines" in block
    assert "DBMS" not in block and not leaks(second)  # served before; muted, private, scope
    assert hook("Another email for the dean") == ""  # nothing new on the same topic
    sessions = json.loads((vault / prime_hook.SERVED).read_text("utf-8"))
    assert sessions["chat-1"]["n"] == 3 and len(set(sessions["chat-1"]["ids"])) == len(
        sessions["chat-1"]["ids"])
    rows = [r for r in xray.rows(Vault(vault)) if r["tool"] == "prime-hook"]
    assert len(rows) == 2 and len(rows[1]["served"]["preferences"]) == 2  # the delta's ids


@pytest.mark.parametrize("prompt", ["thanks!", "ok", "Hello", "yes please", "/cortex email"])
def test_greetings_acks_and_slash_commands_add_nothing(vault, prompt):
    hook("Revise DBMS normalisation")
    assert hook(prompt) == ""
    assert delta.trivial(prompt) and not delta.trivial("email my professor")


def test_the_save_nudge_comes_every_8th_prompt(vault):
    outs = [hook("Revise DBMS normalisation")] + [hook(f"more on joins {i}")
                                                  for i in range(2, 17)]
    nudged = [n for n, o in enumerate(outs, 1) if "holds for every chat" in o]
    assert nudged == [8, 16]
    assert outs[7] == delta.SAVE.format(n=1) and outs[15] == delta.SAVE.format(n=8)


def test_a_corrupt_sidecar_keeps_the_hook_as_before(vault):
    hook("Revise DBMS normalisation")
    (vault / prime_hook.SERVED).write_text("{not json", encoding="utf-8")
    assert hook("Write an email to my professor") == ""
    (vault / prime_hook.SERVED).write_text('{"chat-1": [1]}', encoding="utf-8")
    assert hook("Write an email to my professor") == ""
    assert hook("Revise DBMS", sid="chat-2").startswith(prime_hook.HEAD)  # a new chat heals it
    assert list(json.loads((vault / prime_hook.SERVED).read_text("utf-8"))) == ["chat-2"]


def test_the_sidecar_is_bounded_and_written_through_the_vault(vault, monkeypatch):
    writes = []
    real = Vault.write_text
    monkeypatch.setattr(Vault, "write_text", lambda self, p, t: writes.append(p) or real(
        self, p, t))
    monkeypatch.setattr(prime_hook, "KEEP", 3)
    for i in range(5):
        hook("Revise DBMS", sid=f"s{i}")
    assert list(json.loads((vault / prime_hook.SERVED).read_text("utf-8"))) == ["s2", "s3",
                                                                               "s4"]
    assert (vault / prime_hook.SERVED) in writes


def test_lines_match_a_prompt_word_and_skip_served_ids(monkeypatch):
    parts = ["## Preferences\n" + _wrap("me", "- [Writing] Emails: short.\n- [Food] Tea.")]
    monkeypatch.setattr(prime, "claim_ids", lambda v: {
        ("preferences", "Writing", "Emails: short."): "^c-aaaaa",
        ("preferences", "Food", "Tea."): "^c-bbbbb"})
    text, got = delta.lines(None, "an email note please", parts, set())
    assert got == ["^c-aaaaa"] and "Tea" not in text
    assert delta.lines(None, "an email please", parts, {"^c-aaaaa"}) == ("", [])
    assert delta.lines(None, "the database", parts, set()) == ("", [])


def serve(root, body, client="claude-ai"):
    server = build_server(Vault(root), "owner")

    async def main():
        async with Client(server, client_info=Implementation(name=client, version="1")) as c:
            async def call(name, args=None):
                r = await c.call_tool(name, args or {})
                assert not r.is_error, r.content
                return r.structured_content

            await body(call)

    anyio.run(main)


def test_chat_turn_returns_new_lines_as_context_and_nudges_every_8th(vault):
    got = []

    async def body(call):
        cid = (await call("cortex_prime", {"prompt": "Revise DBMS normalisation"}))["chat"]
        for user in ["Write an email to my professor", "Another email for the dean",
                     *(f"more on joins {i}" for i in range(4, 17))]:
            got.append(await call("cortex_log", {"kind": "chat_turn", "payload": {
                "chat": cid, "user": user, "gist": "ok"}}))

    serve(vault, body)
    ctx = got[0]["context"]
    assert "Email: formal greeting" in ctx and "DBMS" not in ctx and not leaks(ctx)
    assert len(ctx.split("\n", 1)[1]) <= delta.MAX and "`context`" in got[0]["next"]
    assert "context" not in got[1] and got[1]["next"].startswith("Continue")
    nudged = [n for n, r in enumerate(got, 2) if "holds for every chat" in r["next"]]
    assert nudged == [8, 16]
    rows = [r for r in xray.rows(Vault(vault)) if r["tool"] == "chat_turn"]
    assert len(rows) == 1 and len(rows[0]["served"]["preferences"]) == 2
