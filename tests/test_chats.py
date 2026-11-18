import json
import os
import shutil
from pathlib import Path

import pytest

from cortex import chats
from cortex.vault import VaultError

FIXTURES = Path(__file__).parent / "fixtures" / "chats"
ALPHA, BETA = "D--proj-alpha/aaaa-1111", "D--proj-beta/cccc-3333"


@pytest.fixture
def chats_dir(tmp_path, monkeypatch):
    d = tmp_path / "projects"
    shutil.copytree(FIXTURES, d)
    for i, rel in enumerate((ALPHA, "D--proj-alpha/bbbb-2222", BETA)):
        os.utime(d / f"{rel}.jsonl", (1_000_000 + i, 1_000_000 + i))
    monkeypatch.setenv("CORTEX_CHATS_DIR", str(d))
    return d


def test_parser_keeps_text_only(chats_dir):
    ts = chats.turns(chats_dir / f"{ALPHA}.jsonl")
    assert [(r, t) for r, _, t in ts] == [
        ("user", "How does BFS find shortest paths?"),
        ("assistant", "BFS explores level by level."),
        ("user", "Thanks, my key is sk-testtesttesttesttesttest123"),
        ("assistant", "Please rotate it.")]
    blob = json.dumps(ts)
    for dropped in ("reminder", "secret thoughts", "TOOL OUTPUT", "META", "SIDECHAIN",
                    "/clear"):
        assert dropped not in blob


def test_list_order_since_limit_saved(chats_dir):
    out = chats.list_chats({BETA})
    assert [c["id"] for c in out] == [BETA, ALPHA]  # bbbb has one real user turn: left out
    assert out[0] == {"id": BETA, "source": "claude-code", "project": "D--proj-beta",
                      "started": "2026-11-12T08:00:00.000Z",
                      "updated": "2026-11-12T08:01:00.000Z", "turns": 3,
                      "first_user_line": "Plan my DP revision", "saved": True,
                      "learned": False}
    assert out[1]["saved"] is False and out[1]["turns"] == 4
    assert [c["id"] for c in chats.list_chats(set(), since="2026-11-11")] == [BETA]
    assert len(chats.list_chats(set(), limit=1)) == 1
    for bad in ({"limit": 0}, {"limit": 51}, {"since": "yesterday"}):
        with pytest.raises(VaultError):
            chats.list_chats(set(), **bad)


def test_read_parts_redacts_and_truncates(chats_dir):
    (part,) = chats.read_parts(ALPHA)
    assert part.splitlines()[0] == "User: How does BFS find shortest paths?"
    assert "my key is [redacted]" in part and "sk-test" not in part
    long = chats_dir / "D--proj-beta" / "long.jsonl"
    line = {"type": "user", "timestamp": "2026-11-12T00:00:00Z",
            "message": {"content": "x" * 3000}}
    long.write_text("\n".join(json.dumps(line) for _ in range(20)), encoding="utf-8")
    parts = chats.read_parts("D--proj-beta/long")
    assert len(parts) == 3 and all(len(p) <= chats.PART_CHARS for p in parts)
    assert all(len(ln) == chats.LINE_CHARS for p in parts for ln in p.splitlines())


@pytest.mark.parametrize("bad", ["../outside/x", "D--proj-beta/../../outside/x", "cccc-3333",
                                 "D--proj-beta/x/y", "D--proj-beta\\cccc-3333"])
def test_traversal_refused(chats_dir, bad):
    (chats_dir.parent / "outside").mkdir()
    (chats_dir.parent / "outside" / "x.jsonl").write_text("{}", encoding="utf-8")
    with pytest.raises(VaultError) as e:
        chats.read_parts(bad)
    assert e.value.code == "invalid_id"


def test_saved_ids_skips_voided():
    evs = [{"id": "e1", "kind": "log", "payload": {"kind": "chat_saved",
                                                   "source": "claude-code", "chat_id": "a/b"}},
           {"id": "e2", "kind": "log", "payload": {"kind": "chat_saved",
                                                   "source": "claude-chat", "chat_id": "c"}},
           {"id": "e3", "kind": "void", "ref": "e2"}]
    assert chats.saved_ids(evs) == {("claude-code", "a/b")}


def test_import_claude_chat_export(chats_dir, tmp_path):
    import json
    import zipfile

    export = [
        {"uuid": "11111111-aaaa", "name": "Plan", "updated_at": "2026-10-01T10:00:00Z",
         "chat_messages": [
             {"sender": "human", "text": "", "created_at": "2026-10-01T09:00:00Z",
              "content": [{"type": "text", "text": "Help me plan my week"}]},
             {"sender": "assistant", "text": "Sure.", "created_at": "2026-10-01T09:01:00Z"},
             {"sender": "human", "text": "I study late at night",
              "created_at": "2026-10-01T09:02:00Z"}]},
        {"uuid": "../escape", "chat_messages": [{"sender": "human", "text": "x"}]},
        {"uuid": "22222222-bbbb", "chat_messages": []}]
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("data/conversations.json", json.dumps(export))
    assert chats.import_export(z) == ("claude", 1)  # bad uuid and empty chat skipped
    cid = "claude-ai/11111111-aaaa"
    row = next(c for c in chats.list_chats(set(), limit=50) if c["id"] == cid)
    assert row["source"] == "claude-chat" and row["started"] == "2026-10-01T09:00:00Z"
    assert chats.read_parts(cid, "user") == [
        "User: Help me plan my week\nUser: I study late at night"]
    assert chats.import_export(z) == ("claude", 1)  # re-import overwrites, no duplicate
    assert sum(c["id"] == cid for c in chats.list_chats(set(), limit=50)) == 1
