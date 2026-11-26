"""C5, the chat archive (keepfold.chat_archive): imported AI chat pages move from resources/ to the
git-ignored archive/chats/ and back; they stay readable, searchable and linkable, a private one
stays private, git stops tracking them, and Keepfold's commit never adds a file from there."""

import json
import threading
import types

import pytest
from conftest import FakeEmbed, git, note, run
from typer.testing import CliRunner

from keepfold import chat_archive, chat_import, doctor, relink, semantic, session, taint
from keepfold.events import EventLog
from keepfold.mcp_server import Keepfold
from keepfold.vault import CHAT_ARCHIVE, Vault

pytestmark = pytest.mark.usefixtures("home")

SECRET = "zanzibarquux"


def _chat(root, id, words, private=False):
    note(root, f"resources/{id}.md",
         f'title: "{id}"\nkind: "resource"\n' + ("private: true\n" if private else ""),
         f"# {id}\n\nTimeline: · Source: Claude · Date: 2026-11-10\n\n**Me:** {words}\n")


@pytest.fixture
def chats(root_committed):
    """Three chat pages (one private, one known only by the import's ledger), a resource that
    is no chat, and a note linking a chat; all committed."""
    r = root_committed
    _chat(r, "chat-cars", "automobile engines and car motor")
    _chat(r, "chat-diary", f"my {SECRET} diary", private=True)
    note(r, "resources/chat-ledger.md", 'title: "Ledger"\nkind: "resource"\n',
         "# Ledger\n\nNo timeline line any more.\n")
    chat_import._event(EventLog(Vault(r)), {}, "claude-code", "p/x", "Ledger", "chat-ledger",
                       "h")
    note(r, "resources/book.md", 'title: "Book"\nkind: "resource"\n', "# Book\n\nA book.\n")
    note(r, "notes/trip.md", 'title: "Trip"\nkind: "note"\n', "# Trip\n\nSee [[chat-cars]].\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "pages")
    return r


IDS = ["chat-cars", "chat-diary", "chat-ledger"]


def test_there_and_back_with_git_and_links(chats):
    v = Vault(chats)
    assert chat_archive.move(v, dry_run=True)["ids"] == IDS
    assert not (chats / CHAT_ARCHIVE).exists()  # a dry run changes nothing

    r = chat_archive.move(v)
    assert r["ids"] == IDS and r["kept"] == []
    assert "git rm --cached" in r["git"] and "3 pages" in r["git"]
    assert sorted(p.stem for p in (chats / CHAT_ARCHIVE).glob("*.md")) == IDS
    assert (chats / "resources/book.md").is_file()  # not a chat: stays
    assert chat_archive.IGNORE in (chats / ".gitignore").read_text("utf-8").splitlines()
    tracked = git(chats, "ls-files").splitlines()
    assert "resources/book.md" in tracked and not any("chat-" in t for t in tracked)
    assert git(chats, "log", "-1", "--format=%s").strip() == chat_archive.MESSAGE
    assert "chat-" not in git(chats, "status", "--porcelain")  # ignored, nothing to commit
    assert (chats / chat_archive.MARKER).is_file()

    # the same ids: [[chat-cars]] resolves, reads and the index still knows the page
    assert v.path_for("resource", "chat-cars") == chats / CHAT_ARCHIVE / "chat-cars.md"
    assert v.read("resource", "chat-cars").kind == "resource"
    assert "chat-cars" not in {u["target"] for u in relink.run(v)["unresolved"]}
    assert "automobile" in Keepfold(v).keepfold_read("chat-cars")["text"]
    assert {n.id for n in v.iter_notes("resource")} >= set(IDS)

    assert chat_archive.auto(v) is None  # the marker: the app never moves twice

    r = chat_archive.move(v, undo=True)
    assert r["ids"] == IDS and "committed them back" in r["git"]
    assert not list((chats / CHAT_ARCHIVE).glob("*.md"))
    assert {f"resources/{i}.md" for i in IDS} <= set(git(chats, "ls-files").splitlines())
    assert not (chats / chat_archive.MARKER).exists()
    assert taint.config()["app"]["chats_archive"] is False
    assert chat_archive.folder() is None and chat_archive.auto(v) is None  # stays off

    from keepfold.cli import app
    out = CliRunner().invoke(app, ["chats", "archive", "--vault", str(chats)])
    assert out.exit_code == 0, out.output
    assert "Moved 3 chat pages from resources/ to archive/chats/." in out.output
    assert "git rm --cached" in out.output
    assert chat_archive.folder() == CHAT_ARCHIVE  # on again


def test_a_page_already_in_the_other_folder_is_kept(chats):
    note(chats, f"{CHAT_ARCHIVE}/chat-cars.md", 'title: "Other"\n', "# Other\n")
    r = chat_archive.move(Vault(chats))
    assert r["kept"] == ["chat-cars"] and (chats / "resources/chat-cars.md").is_file()


def test_find_and_recall_hit_an_archived_chat_and_a_private_one_stays_hidden(chats,
                                                                             monkeypatch):
    chat_archive.move(Vault(chats))
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    c = Keepfold(Vault(chats))
    assert "chat-cars" in [h["id"] for h in c.keepfold_find("automobile engines")["hits"]]
    assert "chat-cars" in [h["id"] for h in c.keepfold_recall("vehicle", chats=True)["hits"]]
    diary = next(n for n in Vault(chats).iter_notes("resource") if n.id == "chat-diary")
    assert diary.fields["private"] is True  # still private, still read locally

    got = []

    async def body(call):
        got.append(await call("keepfold_find", {"query": f"{SECRET} diary"}))
        got.append(await call("keepfold_recall", {"query": f"{SECRET} diary", "chats": True}))
        got.append(await call("keepfold_read", {"id": "chat-diary"}))

    run(chats, body)
    assert got[2]["error"] == "unknown_id"
    for out in got[:2]:
        assert "chat-diary" not in json.dumps(out) and SECRET not in json.dumps(out[
            "hits"]), out


def test_the_commit_never_adds_an_archive_file(chats):
    (chats / ".gitignore").write_bytes(b".keepfold/*\n")  # even with the line missing
    v = Vault(chats)
    v.create_note("resource", "chat-new", {"title": "New", "kind": "resource"}, "# New",
                  folder=CHAT_ARCHIVE)
    v.create_note("note", "plain", {"title": "Plain", "kind": "note"}, "# Plain")
    assert session._commit(v, "keepfold: test", v.wrote_since(0))
    tracked = git(chats, "ls-files").splitlines()
    assert "notes/plain.md" in tracked and f"{CHAT_ARCHIVE}/chat-new.md" not in tracked


def test_init_and_doctor_for_the_gitignore_line(tmp_path, root_committed):
    from keepfold.cli import _init
    _init(tmp_path / "new")
    assert "archive/chats/" in (tmp_path / "new/.gitignore").read_text("utf-8").splitlines()

    assert doctor.check_chat_archive_git(root_committed)[0] == "WARN"  # an older vault
    _init(root_committed)  # the repair path, as for browse/private/
    assert "archive/chats/" in (root_committed / ".gitignore").read_text("utf-8").splitlines()
    assert doctor.check_chat_archive_git(root_committed)[0] == "PASS"


def test_the_app_pass_moves_once(chats):
    from keepfold.app import actions

    said = []
    job = types.SimpleNamespace(say=said.append, cancelled=threading.Event())
    actions.chat_import(job, chats)
    assert any("3 chat pages moved" in s for s in said)
    assert (chats / CHAT_ARCHIVE / "chat-cars.md").is_file()
    said.clear()
    actions.chat_import(job, chats)
    assert not any("moved" in s for s in said)


def test_status_says_what_moved_and_how_to_undo(chats):
    """R3-10: one Status line after the automatic move, gone after `--undo`."""
    from keepfold.app import status

    v = Vault(chats)
    assert status.archive_row(chats) is None  # never moved
    chat_archive.auto(v)
    row = status.archive_row(chats)
    assert row["value"].startswith("3 chat pages moved to archive/chats/ on 20")
    assert "keepfold chats archive --undo" in row["detail"]
    assert row in status.rows(chats)
    (chats / chat_archive.MARKER).write_text("2026-11-01T00:00:00+00:00\n", "utf-8")  # older
    assert status.archive_row(chats)["value"].endswith("archive/chats/ on 2026-11-01")
    chat_archive.move(v, undo=True)
    assert status.archive_row(chats) is None