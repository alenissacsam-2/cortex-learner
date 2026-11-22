"""R3-2: a relayed `user_confirmed=true` on a me/ claim page is downgraded to unconfirmed
(22 Nov: saved now as a guess, served (ask); a downgraded revise of the user's own line clashes
with it, so it is not saved and the model asks) when the chat read outside text, or when the
server logged this chat's user turns and none of them says the line. No logged turns: the yes
stands; the owner's app click is never downgraded."""

import json
import time

import anyio
import pytest
from mcp import Client
from mcp.types import Implementation

from cortex import chats, mcp_server
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

PREFS = "# Preferences\n\n## Communication\n- Plain words please ^c-plain\n"
PATCH = {"id": "me/preferences", "op": "append_to_section", "target": "Communication",
         "user_confirmed": True}
PROMPT = "I always prefer short answers without any fluff, please remember that"


@pytest.fixture
def vault(root):
    (root / "me" / "preferences.md").write_text(PREFS, encoding="utf-8")
    (root / "raw").mkdir(exist_ok=True)
    (root / "raw" / "src.md").write_text("outside text\n", encoding="utf-8")
    return root


def serve(root, body, client="claude-ai"):
    server = mcp_server.build_server(Vault(root))
    got = {}

    async def main():
        async with Client(server, client_info=Implementation(name=client, version="1")) as c:
            async def call(name, args=None):
                r = await c.call_tool(name, args or {})
                assert not r.is_error, r.content
                return r.structured_content

            got["out"] = await body(call)

    anyio.run(main)
    return got["out"]


def page(root):
    return (root / "me" / "preferences.md").read_text(encoding="utf-8")


def inbox(root):
    p = root / "inbox.md"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def test_an_exposed_chat_saves_the_line_unconfirmed(vault):
    async def body(call):
        await call("cortex_prime", {"prompt": PROMPT})
        await call("cortex_read", {"id": "raw/src.md"})
        return await call("cortex_patch", {**PATCH, "text": "Prefers short answers"})

    out = serve(vault, body)
    assert out["changed"] is True and "queued" not in out, out
    assert out["next"].startswith("Saved now but unconfirmed (this chat read outside text)")
    assert "(ask)" in out["next"] and "stop" not in out["next"].lower()
    assert "- Prefers short answers ^c-" in page(vault)  # a guess: no (confirmed …) marker
    assert "short answers" not in inbox(vault)


def test_a_line_the_users_turn_says_stays_confirmed(vault):
    async def body(call):
        p = await call("cortex_prime", {"prompt": "hello"})
        await call("cortex_log", {"kind": "chat_turn", "payload": {
            "chat": p["chat"], "user": PROMPT, "gist": "Hi."}})
        return await call("cortex_patch", {**PATCH, "text": "Prefers short answers, no fluff"})

    out = serve(vault, body)
    assert out["changed"] is True and out.get("claim"), out
    assert "Prefers short answers, no fluff (confirmed " in page(vault)


def test_a_downgraded_revise_keeps_the_line_it_replaces(vault):
    """Audit 21 Nov M4: a relayed revise lost its pair to the old line. 22 Nov: the pair is
    settled at once, and a guess never replaces the user's own line: it is not saved."""
    async def body(call):
        await call("cortex_prime", {"prompt": PROMPT})
        return await call("cortex_patch", {"id": "me/preferences", "op": "revise",
                                           "target": "c-plain", "text": "Wants jargon everywhere",
                                           "user_confirmed": True})

    out = serve(vault, body)
    assert out["changed"] is False and out["clashes_with"]["id"] == "c-plain", out
    assert "Ask them" in out["next"]
    assert "- Plain words please ^c-plain" in page(vault) and "jargon" not in page(vault)


def test_a_downgraded_goals_revise_says_it_is_saved_not_waiting(vault):
    """Audit 21 Nov M5: a goals line takes a relayed revise at once (served marked ask), but
    the reply told the model it waits for Approve in the app."""
    (vault / "me" / "goals.md").write_text("# Goals\n\n## Now\n- Pass the OA ^c-goals\n",
                                           encoding="utf-8")

    async def body(call):
        await call("cortex_prime", {"prompt": PROMPT})
        return await call("cortex_patch", {"id": "me/goals", "op": "revise", "target": "c-goals",
                                           "text": "Pass the Hyland OA", "user_confirmed": True})

    out = serve(vault, body)
    assert out["changed"] is True and "waits" not in out["next"] and "(ask)" in out["next"]
    assert "- Pass the Hyland OA ^c-" in (vault / "me" / "goals.md").read_text("utf-8")


def test_a_line_no_turn_says_is_downgraded(vault):
    async def body(call):
        await call("cortex_prime", {"prompt": PROMPT})
        a = await call("cortex_patch", {**PATCH, "text": "Loves long essays about tarot"})
        b = await call("cortex_patch", {"id": "me/preferences", "op": "revise",
                                        "target": "c-plain", "text": "Wants jargon everywhere",
                                        "user_confirmed": True})
        return a, b

    a, b = serve(vault, body)
    assert a["changed"] is True and "do not say it" in a["next"] and "(ask)" in a["next"]
    assert b["changed"] is False and b["clashes_with"]["id"] == "c-plain"
    text = page(vault)
    assert "- Loves long essays about tarot ^c-" in text and "jargon" not in text
    assert "- Plain words please ^c-plain" in text  # the user's own line stays
    assert "tarot" not in inbox(vault) and "jargon" not in inbox(vault)


def test_no_logged_turns_leaves_the_yes_as_is(vault):
    async def body(call):  # Claude Code: Cortex logs no turns (its transcripts exist)
        await call("cortex_prime", {"prompt": PROMPT})
        return await call("cortex_patch", {**PATCH, "text": "Loves long essays about tarot"})

    out = serve(vault, body, client="claude-code")
    assert out["changed"] is True, out
    assert "Loves long essays about tarot (confirmed " in page(vault)


def test_the_app_click_is_never_downgraded(vault):
    core = mcp_server.Cortex(Vault(vault))
    core.app_clicks = True
    cid = "01JAAAAAAAAAAAAAAAAAAAAAAA"
    live = chats.export_root() / chats.LIVE
    live.mkdir(parents=True)
    (live / f"{cid}.jsonl").write_text(json.dumps(
        {"type": "user", "message": {"content": "hello there"}}) + "\n", encoding="utf-8")
    core._chat = (cid, time.monotonic())  # a logged turn that does not say the line
    core._call("cortex_read", core.cortex_read, {"id": "raw/src.md"})
    out = core._call("cortex_patch", core.cortex_patch,
                     {**PATCH, "text": "Loves long essays about tarot"})
    assert core.sessions.current.exposed  # outside text read, and still written confirmed
    assert out["changed"] is True, out
    assert "Loves long essays about tarot (confirmed " in page(vault)
