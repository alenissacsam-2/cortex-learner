"""`cortex digest` (council item 9): new imported chats since the last digest, mapped to
concepts by local search, written only on the student's yes, as touches plus a marker."""

import json

import pytest
from conftest import events
from typer.testing import CliRunner

from cortex import digest, planner
from cortex.catalog import Catalog
from cortex.cli import app
from cortex.index import Index
from cortex.vault import Vault

runner = CliRunner()
UUID = "6700aaaa-1111-2222-3333-44445555{:04d}"


@pytest.fixture
def export(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_CHATS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("CORTEX_CHAT_EXPORT_DIR", str(tmp_path / "exported"))
    return tmp_path


def chat(base, folder, cid, *user_lines, ts="2026-11-10T09:00:00Z"):
    p = base / folder / f"{cid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for text in user_lines:
        lines += [{"type": "user", "message": {"content": text}, "timestamp": ts},
                  {"type": "assistant", "message": {"content": "Sure."}, "timestamp": ts}]
    p.write_text("\n".join(map(json.dumps, lines)) + "\n", encoding="utf-8")
    return f"{folder}/{cid}"


def cli(root, *args, input=None):
    out = runner.invoke(app, ["digest", *args, "--vault", str(root)], input=input)
    assert out.exit_code == 0, out.output
    return out.output


def logs(root, kind):
    return [e for e in events(root) if e.get("kind") == "log"
            and e.get("payload", {}).get("kind") == kind]


def test_new_chats_skips_reviewed_claude_code_and_old(export):
    a = chat(export / "exported", "chatgpt", UUID.format(1), "two pointers on arrays")
    chat(export / "exported", "gemini", UUID.format(2), "old one", ts="2026-01-01T09:00:00Z")
    chat(export / "projects", "proj", UUID.format(3), "a Claude Code chat")
    rows = digest.new_chats(set(), since="2026-11-01")
    assert [r["id"] for r in rows] == [a] and rows[0]["source"] == "chatgpt"
    assert rows[0]["title"] == "two pointers on arrays"
    assert digest.new_chats({a}, since="2026-11-01") == []
    assert len(digest.new_chats(set(), limit=1)) == 1


def test_mapping_keeps_visible_concepts_only(root_committed):
    v = Vault(root_committed)
    idx = Index(v, Catalog.load(v))
    got = digest.concepts_for(idx, "two pointers on a sorted array")
    assert got and got[0]["id"] == "two-pointers" and len(got) <= digest.MAX_CONCEPTS
    assert {c["id"] for c in got} <= {"two-pointers", "arrays-basics"}  # concepts, no problems
    assert digest.concepts_for(idx, "private concept secret diary") == []  # private: true


def test_dry_run_lists_and_writes_nothing(root_committed, export):
    chat(export / "exported", "chatgpt", UUID.format(1), "two pointers on a sorted array")
    before = events(root_committed)
    out = cli(root_committed, "--dry-run")
    assert "Since your last digest" in out and "Two pointers (two-pointers)" in out
    assert "dry run: nothing written" in out
    assert events(root_committed) == before
    assert "Two pointers" in cli(root_committed, "--dry-run")  # still new next time


def test_yes_logs_touches_only_and_advances_the_marker(root_committed, export):
    a = chat(export / "exported", "chatgpt", UUID.format(1), "two pointers on a sorted array")
    out = cli(root_committed, "--yes")
    assert "Recorded" in out and "context only" in out
    touches = logs(root_committed, "touch")
    assert touches and all("object" not in e and e["payload"]["chat"] == a for e in touches)
    assert "two-pointers" in {e["payload"]["ids"][0] for e in touches}
    assert not any(planner._studied(e) for e in events(root_committed))  # never mastery
    (mark,) = logs(root_committed, "digest")
    assert mark["payload"]["chats"] == [a]
    assert "No new imported chats" in cli(root_committed, "--yes")
    b = chat(export / "exported", "perplexity", UUID.format(2), "array basics question")
    out = cli(root_committed, "--dry-run")
    assert "array basics question" in out and "two pointers on a sorted" not in out
    assert digest.reviewed(events(root_committed)) == {a}
    assert b not in digest.reviewed(events(root_committed))


def test_confirm_each_topic(root_committed, export):
    chat(export / "exported", "chatgpt", UUID.format(1), "two pointers on a sorted array")
    v = Vault(root_committed)
    shown = digest.concepts_for(Index(v, Catalog.load(v)), "two pointers on a sorted array")
    answers = "y\n" + "n\n" * (len(shown) - 1)
    cli(root_committed, input=answers)
    assert [e["payload"]["ids"] for e in logs(root_committed, "touch")] == [[shown[0]["id"]]]
    assert len(logs(root_committed, "digest")) == 1


def test_declining_everything_still_marks_the_chats_seen(root_committed, export):
    chat(export / "exported", "chatgpt", UUID.format(1), "two pointers on a sorted array")
    cli(root_committed, input="n\n" * digest.MAX_CONCEPTS)
    assert logs(root_committed, "touch") == [] and len(logs(root_committed, "digest")) == 1


def test_bad_since_is_refused(root_committed):
    out = runner.invoke(app, ["digest", "--since", "yesterday", "--vault", str(root_committed)])
    assert out.exit_code == 1 and "--since" in out.output

