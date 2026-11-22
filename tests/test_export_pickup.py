"""B2-b: a chat export in Downloads, offered on Home (cortex.app.exports). Tmp folders only:
CORTEX_DOWNLOADS_DIR points at a tmp Downloads, `home` at a tmp ~/.cortex."""

import os
import time
import zipfile
from pathlib import Path

import pytest

from cortex.app import exports, server, targets

EXPORTS = Path(__file__).parent / "fixtures" / "exports"
CLAUDE = ["conversations.json", "users.json", "projects.json"]
CHATGPT = ["conversations.json", "chat.html", "message_feedback.json", "user.json"]
GEMINI = ["Takeout/My Activity/Gemini Apps/MyActivity.json", "Takeout/archive_browser.html"]
CHROME = ["Takeout/My Activity/Chrome/MyActivity.html"]


@pytest.fixture
def dl(tmp_path, home, monkeypatch):
    d = tmp_path / "Downloads"
    d.mkdir()
    monkeypatch.setenv("CORTEX_DOWNLOADS_DIR", str(d))
    monkeypatch.setattr(targets.ClaudeDesktop, "connected", lambda self: False)
    exports.forget()
    exports._kinds.clear()
    return d


def make(d: Path, name: str, members: list[str], age_days: float = 0, data=None) -> Path:
    p = d / name
    with zipfile.ZipFile(p, "w") as z:
        for m in members:
            z.writestr(m, (data or {}).get(m, "[]"))
    t = time.time() - age_days * 86400
    os.utime(p, (t, t))
    return p


def test_each_kind_from_member_names():
    assert exports.kind_of(CLAUDE) == "claude"
    assert exports.kind_of(["data/conversations.json", "data/users.json"]) == "claude"
    assert exports.kind_of(CHATGPT) == "chatgpt"
    assert exports.kind_of(GEMINI) == "gemini"
    assert exports.kind_of(CHROME) == "google-activity"
    assert exports.kind_of(["conversations.json"]) is None
    assert exports.kind_of(["photos/a.jpg", "notes.md"]) is None
    assert exports.kind_of(["Takeout/Drive/a.pdf"]) is None


def test_only_new_recent_known_zips_are_offered_without_reading_them(dl, monkeypatch):
    real_open = zipfile.ZipFile.open

    def refuse_read(self, name, mode="r", *a, **k):  # writing the test zips stays allowed
        if mode == "r":
            raise AssertionError("a member was read before the click")
        return real_open(self, name, mode, *a, **k)
    monkeypatch.setattr(zipfile.ZipFile, "open", refuse_read)
    monkeypatch.setattr(zipfile.ZipFile, "read", refuse_read)
    make(dl, "old.zip", CLAUDE, age_days=40)
    make(dl, "other.zip", ["a.txt"])
    (dl / "conversations.json").write_text("[]")
    (dl / "notazip.zip").write_text("nope")
    (dl / "sub").mkdir()
    make(dl / "sub", "deep.zip", CLAUDE)  # no recursion
    assert exports.candidates() == []
    assert exports.offer() == {"found": None, "remind": False}
    gpt = make(dl, "gpt.zip", CHATGPT, age_days=2)
    cl = make(dl, "claude.zip", CLAUDE, age_days=1)
    assert [c["source"] for c in exports.candidates()] == ["claude", "chatgpt"]
    exports.forget()
    f = exports.offer()["found"]
    assert f["path"] == str(cl) and f["name"] == "claude.ai" and f["file"] == "claude.zip"
    exports.mark(exports.pick(str(cl)), "dismissed")
    assert exports.offer()["found"]["path"] == str(gpt)
    exports.mark(exports.pick(str(gpt)), "added")
    assert exports.offer()["found"] is None
    make(dl, "claude.zip", CLAUDE + ["extra.json"])  # downloaded again: new size and time
    exports.forget()
    assert exports.offer()["found"]["path"] == str(cl)


def test_off_when_the_chat_import_setting_is_off(dl, monkeypatch):
    from cortex import chat_import
    make(dl, "c.zip", CLAUDE)
    monkeypatch.setattr(chat_import, "enabled", lambda: False)
    assert exports.offer() == {"found": None, "remind": False}
    with pytest.raises(ValueError):
        exports.pick(str(dl / "c.zip"))


def test_the_reminder_needs_claude_desktop_and_no_recent_claude_import(dl, monkeypatch):
    from cortex import chats
    assert exports.offer()["remind"] is False
    monkeypatch.setattr(targets.ClaudeDesktop, "connected", lambda self: True)
    exports.forget()
    assert exports.offer() == {"found": None, "remind": True}
    make(dl, "c.zip", CLAUDE)  # a found export wins
    exports.forget()
    assert exports.offer()["found"] and exports.offer()["remind"] is False
    (dl / "c.zip").unlink()
    d = chats.export_root() / chats.EXPORT_DIRS["claude"][0]
    d.mkdir(parents=True)
    (d / "x.jsonl").write_text("{}")
    exports.forget()
    assert exports.offer()["remind"] is False  # imported in the last 30 days
    old = time.time() - 40 * 86400
    os.utime(d, (old, old))
    exports.forget()
    assert exports.offer()["remind"] is True
    exports.later()
    assert exports.offer()["remind"] is False


# ---- the routes ----------------------------------------------------------------------------

@pytest.fixture
def app(root_committed, dl, monkeypatch):
    from test_app import _serve
    monkeypatch.setattr(server, "PORT", 0)
    (root_committed / ".cortex").mkdir(exist_ok=True)
    srv, a = _serve(root_committed)
    yield a
    srv.shutdown()
    srv.server_close()


ROUTES = ("/api/import/pickup/add", "/api/import/pickup/dismiss", "/api/import/pickup/later")


def test_routes_need_the_token_and_origin(app):
    from test_app import call
    assert call(app, "GET", "/api/import/pickup", token=False)[0] == 401
    for path in ROUTES:
        assert call(app, "POST", path, {}, token=False)[0] == 401, path
        assert call(app, "POST", path, {}, origin="http://evil.example")[0] == 403, path
        assert call(app, "GET", path)[0] == 405, path


def test_add_starts_the_import_with_the_found_source(app, dl):
    from test_app import call, wait_job

    from cortex import chats
    data = {"conversations.json": (EXPORTS / "chatgpt_conversations.json").read_text("utf-8")}
    p = make(dl, "gpt.zip", CHATGPT, data=data)
    code, got, _, _ = call(app, "GET", "/api/import/pickup")
    assert code == 200 and got["found"]["source"] == "chatgpt" and got["found"]["name"] == "ChatGPT"
    elsewhere = {"path": str(dl / "elsewhere.zip")}  # only an offered path is imported
    assert call(app, "POST", "/api/import/pickup/add", elsewhere)[0] == 400
    assert call(app, "POST", "/api/import/pickup/add", {"path": str(p)})[0] == 200
    job = wait_job(app)
    assert job["error"] is None and job["result"]["counts"]["conversations"] >= 1
    assert any((chats.export_root() / "chatgpt").glob("*.jsonl"))
    assert call(app, "GET", "/api/import/pickup")[1]["found"] is None  # not offered again


def test_not_this_one_and_later_over_http(app, dl, monkeypatch):
    from test_app import call
    p = make(dl, "c.zip", CLAUDE)
    assert call(app, "POST", "/api/import/pickup/dismiss", {"path": str(p)})[0] == 200
    monkeypatch.setattr(targets.ClaudeDesktop, "connected", lambda self: True)
    exports.forget()
    assert call(app, "GET", "/api/import/pickup")[1] == {"found": None, "remind": True}
    assert call(app, "POST", "/api/import/pickup/later", {})[0] == 200
    assert call(app, "GET", "/api/import/pickup")[1]["remind"] is False
