"""I4 import-chats <-> index <-> recall (docs/design/v6/TEST-PLAN.md §3): each of the four chat
exports (claude.ai, ChatGPT, Gemini, Perplexity) goes through `chats.import_export`; its
redacted turns become an imported chat page in the vault; then `Index`, `semantic.search` and
the MCP tools. A secret never gets past the import, a re-import dedupes on (source, id), the
chat page ranks below the user's own page, and `cortex_chats` lists every source."""

import json

import pytest
from conftest import FakeEmbed, note, run

from cortex import chats, semantic
from cortex.catalog import Catalog
from cortex.index import Index
from cortex.vault import Vault
from integration.builders import EXPORTS

pytestmark = pytest.mark.usefixtures("home")

SECRETS = {"claude": "ghp_claudeTESTtokenAAAAAAAAAAAA",
           "chatgpt": "sk-chatgpttesttesttesttest99",
           "gemini": "api_key=abcdefghijklmnop",
           "perplexity": "AKIAABCDEFGHIJKLMNOP"}
LABEL = {"claude": "Claude", "chatgpt": "ChatGPT", "gemini": "Gemini", "perplexity": "Perplexity"}


def _exports(tmp) -> dict:
    """{source: export path}: the shipped fixtures, plus a claude.ai export and a Perplexity
    thread holding a secret, both synthetic."""
    tmp.mkdir(parents=True, exist_ok=True)
    claude = tmp / "conversations.json"
    claude.write_text(json.dumps([{
        "uuid": "c1aude00-1111-2222-3333-444455556666", "updated_at": "2026-10-03T10:00:00Z",
        "chat_messages": [
            {"sender": "human", "created_at": "2026-10-03T09:00:00Z",
             "content": [{"type": "text", "text": f"car motor help, token {SECRETS['claude']}"}]},
            {"sender": "assistant", "created_at": "2026-10-03T09:01:00Z",
             "content": [{"type": "text", "text": "Check the vehicle battery."}]},
            {"sender": "human", "created_at": "2026-10-03T09:02:00Z", "text": "and the motor?"},
        ]}]), encoding="utf-8")
    pplx = tmp / "thread.md"
    pplx.write_text((EXPORTS / "perplexity_thread.md").read_text(encoding="utf-8")
                    + f"\n# My key is {SECRETS['perplexity']} ok?\n\nNever share it.\n",
                    encoding="utf-8")
    return {"claude": claude, "chatgpt": EXPORTS / "chatgpt_conversations.json",
            "gemini": EXPORTS / "gemini_MyActivity.json", "perplexity": pplx}


def _files(source: str) -> list:
    return sorted((chats.export_root() / chats.EXPORT_DIRS[source][0]).glob("*.jsonl"))


def _chat_page(root, source: str, path) -> str:
    """The imported chat page for one transcript, built from its (already redacted) turns."""
    cid = f"chat-{source}-{path.stem[:8]}"
    body = f"# Chat {cid}\n\nTimeline: [[2026-10-01]] · Source: {LABEL[source]} · " \
           "Date: 2026-10-03\n\n"
    for role, _, text in chats.turns(path):
        body += f"**{'Me' if role == 'user' else LABEL[source]}:** {text}\n\n"
    note(root, f"resources/{cid}.md", f'title: "Chat {cid}"\n', body + "car motor vehicle\n")
    return cid


@pytest.fixture
def imported(root, tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    out = {}
    for source, path in _exports(tmp_path / "exports").items():
        got, n = chats.import_export(path)
        assert got == source and n >= 1, (source, n)
        out[source] = [_chat_page(root, source, p) for p in _files(source)]
    return out


def test_secrets_never_reach_transcripts_index_or_recall(root, imported):
    for source, secret in SECRETS.items():
        for p in _files(source):
            assert secret not in p.read_text(encoding="utf-8"), source
        assert "[redacted]" in "".join(p.read_text(encoding="utf-8") for p in _files(source))
    idx = Index(Vault(root), Catalog.load(Vault(root)))
    rows = json.dumps([list(r) for r in idx.db.execute("SELECT * FROM fts")]
                      + [n["body"] for n in idx._notes.values()])
    assert all(cid in rows for ids in imported.values() for cid in ids)
    assert not any(s in rows for s in SECRETS.values())

    got = []

    async def body(call):
        for q in ("car motor token key", "Dijkstra weighted graphs", "minimum spanning tree"):
            got.append(await call("cortex_recall", {"query": q, "chats": True}))
        listed = await call("cortex_chats", {"limit": 50})
        got.append(listed)
        for c in listed["chats"]:
            got.append(await call("cortex_chats", {"action": "read", "id": c["id"]}))

    run(root, body)
    assert any("chat-" in json.dumps(r) for r in got[:3]), got[:3]
    text = json.dumps(got)
    assert not any(s in text for s in SECRETS.values()), text
    assert {c["source"] for c in got[3]["chats"]} == {"claude-chat", "chatgpt", "gemini",
                                                      "perplexity"}


def test_reimport_dedupes_on_source_and_id(imported, tmp_path):
    before = {s: _files(s) for s in SECRETS}
    for source, path in _exports(tmp_path / "again").items():
        assert chats.import_export(path, dry_run=True) == (source, 0, len(before[source]))
        assert chats.import_export(path) == (source, len(before[source]))
        assert _files(source) == before[source]


def test_imported_chat_ranks_below_the_users_own_page(root, imported):
    note(root, "notes/car-motor.md", 'title: "Car motor"\n',
         "# Car motor\n\ncar motor vehicle\n")
    idx = Index(Vault(root), Catalog.load(Vault(root)))
    hits = [h["id"] for h in semantic.search(idx, "car motor", 20, rerank=False)[0]]
    chat_ids = [c for ids in imported.values() for c in ids]
    assert hits[0] == "car-motor" and set(hits) & set(chat_ids), hits
    assert all(hits.index("car-motor") < hits.index(c) for c in chat_ids if c in hits)
    # chats=True lifts the chat weight, so a chat page can tie or win
    lifted = semantic.search(idx, "car motor", 20, rerank=False, chats=True)[0]
    assert {h["id"] for h in lifted} >= set(chat_ids) & set(hits)
