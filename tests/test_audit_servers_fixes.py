"""Audit 27 Nov (servers): a private raw source is matched by file, not by path spelling
(finding 3), and _auto_retire keeps another writer's TIDY_FILE keys (finding 6)."""

import json
import os

import pytest
from conftest import note
from test_review_auto import VIM, commit, live, page

from keepfold.mcp_server import TIDY_FILE, Keepfold
from keepfold.vault import Vault, VaultError

pytestmark = pytest.mark.usefixtures("home")


def test_a_private_raw_source_is_refused_under_another_path_to_the_same_file(root):
    """macOS's APFS is case-insensitive while Path == is not, so raw/DIARY.md reached
    raw/diary.md's text. A hard link is that case on every OS: another path, the same file."""
    note(root, "resources/diary-src.md", 'title: "Diary"\nprivate: true\n'
         'source: "raw/diary.md"\n', "# Diary")
    (root / "raw").mkdir(exist_ok=True)
    (root / "raw/diary.md").write_text("my diary", encoding="utf-8")
    os.link(root / "raw/diary.md", root / "raw/other-name.md")
    (root / "raw/open.md").write_text("open text", encoding="utf-8")
    c = Keepfold(Vault(root))
    for rel in ("raw/diary.md", "raw/other-name.md"):
        with pytest.raises(VaultError) as e:
            c.keepfold_read(rel)
        assert e.value.code == "unknown_id"
    assert "open text" in c.keepfold_read("raw/open.md")["text"]


def test_auto_retire_keeps_keys_another_writer_added_meanwhile(root, monkeypatch):
    (root / ".keepfold").mkdir(exist_ok=True)
    page(root, "preferences", VIM[:2])
    commit(root)
    (root / TIDY_FILE).write_text(json.dumps({"done": ["old/key"], "repaired": 1}), "utf-8")
    c = Keepfold(Vault(root))
    rows = live(root, "preferences")
    c.session = c.sessions.start("ambient")
    note_ = c.sessions.note

    def other_writer_then_note(*a, **k):  # the app's sweep writes between the read and write
        (root / TIDY_FILE).write_text(
            json.dumps({"done": ["old/key", "sweep/key"], "repaired": 1}), "utf-8")
        return note_(*a, **k)

    monkeypatch.setattr(c.sessions, "note", other_writer_then_note)
    gone, kept = rows[VIM[1]], rows[VIM[0]]
    assert c._auto_retire(gone, kept, "duplicate")
    state = json.loads((root / TIDY_FILE).read_text("utf-8"))
    assert {"old/key", "sweep/key", f"preferences/{gone['id']}"} <= set(state["done"])
    assert state["repaired"] == 1
