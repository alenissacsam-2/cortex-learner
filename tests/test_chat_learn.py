"""v5 Phase 2 G3: learning from past chats on /cortex. Synthetic transcripts only."""

import os
import shutil
import time
from pathlib import Path

import pytest
from conftest import events, run

from cortex import chats

pytestmark = pytest.mark.usefixtures("home")

FIXTURES = Path(__file__).parent / "fixtures" / "chats"
ALPHA, BBBB, BETA = "D--proj-alpha/aaaa-1111", "D--proj-alpha/bbbb-2222", "D--proj-beta/cccc-3333"


@pytest.fixture
def chats_dir(tmp_path, monkeypatch):
    d = tmp_path / "projects"
    shutil.copytree(FIXTURES, d)
    for i, rel in enumerate((ALPHA, BBBB, BETA)):
        os.utime(d / f"{rel}.jsonl", (1_000_000 + i, 1_000_000 + i))
    monkeypatch.setenv("CORTEX_CHATS_DIR", str(d))
    return d


def test_learned_flag_unlearned_filter_and_count(chats_dir):
    out = chats.list_chats(set(), learned={ALPHA})
    assert [(c["id"], c["learned"]) for c in out] == [(BETA, False), (ALPHA, True)]
    assert [c["id"] for c in chats.list_chats(set(), learned={ALPHA}, unlearned=True)] == [BETA]
    assert chats.unlearned_count(set()) == 2  # bbbb has one real user turn: never listed
    assert chats.unlearned_count({ALPHA, BETA}) == 0


def test_the_chat_being_written_is_left_out(chats_dir):
    now = time.time()
    os.utime(chats_dir / f"{BETA}.jsonl", (now, now))
    assert [c["id"] for c in chats.list_chats(set())] == [ALPHA]
    assert chats.unlearned_count(set()) == 1
    os.utime(chats_dir / f"{BETA}.jsonl", (now - 600, now - 600))  # older than 2 minutes
    assert chats.unlearned_count(set()) == 2


def test_read_user_turns_only(chats_dir):
    (part,) = chats.read_parts(ALPHA, "user")
    assert part.splitlines() == ["User: How does BFS find shortest paths?",
                                 "User: Thanks, my key is [redacted]"]


def test_home_count_list_read_and_ledger(root, chats_dir):
    async def body(call):
        start = await call("cortex_start", {})
        assert start["unlearned"] == 2
        assert "2 past chats to learn from" in start["display"]
        assert "chat_learned" in start["next"] and "who='user'" in start["next"]
        out = await call("cortex_chats", {"action": "read", "id": BETA, "who": "user"})
        assert "<untrusted_note" in out["text"] and "Assistant:" not in out["text"]
        assert "User: Plan my DP revision second line" in out["text"]
        assert "chat_learned" in out["next"]
        for p in ({"chat_id": BETA, "source": "slack", "found": 1},
                  {"chat_id": "", "source": "claude-code", "found": 1},
                  {"chat_id": BETA, "source": "claude-code", "found": -1},
                  {"chat_id": BETA, "source": "claude-code", "found": "2"}):
            out = await call("cortex_log", {"kind": "chat_learned", "payload": p})
            assert out["error"] == "invalid_argument", p
        await call("cortex_log", {"kind": "chat_learned", "payload": {
            "chat_id": BETA, "source": "claude-code", "found": 2, "id": "forged"}})
        listed = await call("cortex_chats", {"unlearned": True})
        assert [c["id"] for c in listed["chats"]] == [ALPHA]
        listed = await call("cortex_chats", {})
        assert [(c["id"], c["learned"]) for c in listed["chats"]] == [(BETA, True),
                                                                      (ALPHA, False)]
        again = await call("cortex_start", {})
        assert again["unlearned"] == 1 and "1 past chat to learn from" in again["display"]

    run(root, body)
    (ev,) = [e for e in events(root) if e.get("payload", {}).get("kind") == "chat_learned"]
    assert ev["payload"] == {"kind": "chat_learned", "source": "claude-code", "chat_id": BETA,
                             "found": 2}
    assert ev["id"] != "forged"


def test_user_only_read_does_not_expose(root, chats_dir):
    (root / "me" / "preferences.md").write_bytes(b"# Preferences\n\n## Coding\n")
    p = {"id": "me/preferences", "op": "append_to_section", "target": "Coding",
         "user_confirmed": True}

    async def body(call):
        await call("cortex_chats", {"action": "read", "id": ALPHA, "who": "user"})
        await call("cortex_patch", {**p, "text": "- Uses Vim"})
        await call("cortex_chats", {"action": "read", "id": ALPHA})  # assistant turns too
        out = await call("cortex_patch", {**p, "text": "- Uses tabs"})
        assert out["queued"] is True  # R3-2: exposed, so the relayed yes waits in Review

    run(root, body)
    tiers = [e["payload"]["change"]["tier"] for e in events(root) if e["kind"] == "change"
             and isinstance(e.get("payload", {}).get("change"), dict)
             and e["payload"]["change"].get("tier")]
    assert tiers == ["confirmed"]
