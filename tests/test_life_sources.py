"""Sources B, part 1 (life_sources): sent mail, notes-to-self chats, a city per day, photo
dates and Spotify years. Every fixture is synthetic and built in tmp_path."""

import re

from typer.testing import CliRunner

from cortex import importers, life_sources, sources
from cortex.cli import app
from cortex.vault import Vault

runner = CliRunner()
C = sources.Audience.CLOUD
_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _pages(v: Vault, prefix: str) -> dict[str, str]:
    return {p.stem: p.read_text("utf-8") for p in sorted((v.root / "resources").glob(
        f"{prefix}-*.md"))}


# ---- mail ----

def _mail(frm: str, to: str, date: str, subject: str, body: str, labels: str = "Sent",
          html: bool = False, mid: str = "") -> str:
    ctype = "text/html" if html else "text/plain"
    return (f"From 1234@xxx Mon Mar 02 10:00:00 +0000 2026\nX-Gmail-Labels: {labels}\n"
            f"From: {frm}\nTo: {to}\nDate: {date}\nSubject: {subject}\n"
            f"Message-ID: <{mid or subject.replace(' ', '')}@example.test>\n"
            f"Content-Type: {ctype}; charset=utf-8\n\n{body}\n\n")


MBOX = (
    _mail("Me <me@example.test>", '"Asha Rao" <asha@example.test>, ben@example.test',
          "Tue, 03 Mar 2026 09:30:00 +0000", "Lunch plan",
          "Hi Asha,\nSee you at noon by the library.\nWrite to me at me@example.test.\n\n-- \n"
          "Me\nme@example.test\n\nOn Mon, 2 Mar 2026 at 08:00, Asha <asha@example.test> "
          "wrote:\n> quoted secret plan from Asha\n")
    + _mail("Me <me@example.test>", "Chen <chen@example.test>",
            "Fri, 10 Apr 2026 18:00:00 +0000", "Notes",
            "<html><body><p>My own html words</p><div class=\"gmail_quote\">On Thu wrote:"
            "<blockquote>chen quoted html text</blockquote></div></body></html>", html=True)
    + _mail("Me <me@example.test>", "Chen <chen@example.test>",
            "Sat, 11 Apr 2026 18:00:00 +0000", "Fwd: article",
            "Worth a read.\n\n---------- Forwarded message ---------\nFrom: someone\n"
            "the forwarded article text\n")
    + _mail("Asha <asha@example.test>", "Me <me@example.test>",
            "Tue, 03 Mar 2026 10:30:00 +0000", "Re: Lunch plan", "asha's own reply",
            labels="Inbox")
    + _mail("Me <me@example.test>", "Chen <chen@example.test>", "not a date", "Odd",
            "text")
    + _mail("Me <me@example.test>", "Chen <chen@example.test>",
            "Sun, 12 Apr 2026 18:00:00 +0000", "Draft one", "unsent words", labels="Draft"))


def test_mail_keeps_own_words_only(tmp_path, root_plain):
    box = tmp_path / "Sent.mbox"
    box.write_text(MBOX, encoding="utf-8")
    v = Vault(root_plain)
    src = life_sources.mail_source(box)
    assert src.default_tier is sources.Audience.LOCAL
    r = sources.run(v, src, C)
    assert r["imported"] == 2 and r["reasons"]["not sent by you"] == 1
    assert r["reasons"]["no readable date"] == 1 and r["reasons"]["a draft"] == 1
    pages = _pages(v, "mail")
    assert set(pages) == {"mail-sent-mail-2026-03", "mail-sent-mail-2026-04"}
    march, april = pages["mail-sent-mail-2026-03"], pages["mail-sent-mail-2026-04"]
    assert "See you at noon by the library." in march and "To Asha Rao." in march
    assert "quoted secret plan" not in march and "wrote:" not in march
    assert "\nMe\n" not in march.split("library.")[1]  # the signature is cut
    assert "My own html words" in april and "chen quoted html text" not in april
    assert "Worth a read." in april and "forwarded article text" not in april
    assert "asha's own reply" not in march and "unsent words" not in april
    for text in pages.values():
        assert not _ADDRESS.search(text.split("Source: ")[1].split("\n", 1)[1]), text
    assert "Source: Sent mail · Date: 2026-04-11" in april


def test_mail_reimport_is_a_no_op_and_bad_file_is_counted(tmp_path, root_plain):
    box = tmp_path / "Sent.mbox"
    box.write_text(MBOX, encoding="utf-8")
    v = Vault(root_plain)
    sources.run(v, life_sources.mail_source(box), C)
    before = _pages(v, "mail")
    r = sources.run(v, life_sources.mail_source(box), C)
    assert r["imported"] == 0 and r["updated"] == 0 and r["duplicate"] == 2
    assert _pages(v, "mail") == before
    junk = tmp_path / "junk.mbox"
    junk.write_bytes(b"\x00\x01 not mail at all")
    r = sources.run(v, life_sources.mail_source(junk), C)
    assert r["reasons"]["not an mbox file"] == 1 and r["imported"] == 0


def test_own_text_cuts():
    assert life_sources.own_text("Mine.\n> theirs\nmore mine") == "Mine.\nmore mine"
    assert life_sources.own_text("Mine.\nFrom: Asha\nSent: Monday\ntheirs") == "Mine."
    assert life_sources.own_text("mail x@y.test now") == "mail [address] now"


def test_cli_imports_mail(tmp_path, root_plain):
    box = tmp_path / "Sent.mbox"
    box.write_text(MBOX, encoding="utf-8")
    assert importers.detect(box) == ["mail"]
    r = runner.invoke(app, ["import", str(box), "--vault", str(root_plain)])
    assert r.exit_code == 0, r.output
    assert "tier private (the default" in r.output and "Sent mail: read" in r.output
    assert "private: true" in (root_plain / "resources" / "mail-sent-mail-2026-03.md") \
        .read_text("utf-8")


# ---- notes to self ----

WHATSAPP = (
    "13/03/2024, 09:15 - Messages and calls are end-to-end encrypted.\n"
    "13/03/2024, 09:15 - Me: buy milk\nand bread\n"
    "13/03/2024, 21:05 - Me: IMG-20240313-WA0001.jpg (file attached)\n"
    "14/03/2024, 08:00 - Me: <Media omitted>\n"
    "02/04/2024, 7:30 pm - Me: idea: a study timer\n"
    "02/04/2024, 7:31 pm - Me: This message was deleted\n")
IOS = "[03/14/24, 9:15:23 AM] Me: video omitted\n[03/14/24, 9:16:00 AM] Me: note one\n"
TELEGRAM = {"chats": {"list": [
    {"type": "personal_chat", "name": "Asha", "messages": [
        {"type": "message", "date": "2024-03-01T10:00:00", "from_id": "user2",
         "text": "asha's private words"}]},
    {"type": "saved_messages", "messages": [
        {"type": "service", "date": "2024-03-01T10:00:00", "action": "x"},
        {"type": "message", "date": "2024-03-02T10:00:00", "from_id": "user1",
         "text": ["read ", {"type": "bold", "text": "SICP"}, " ch 3"]},
        {"type": "message", "date": "2024-03-03T11:00:00", "from_id": "user1",
         "photo": "photos/p1.jpg", "text": "whiteboard"},
        {"type": "message", "date": "2024-03-04T12:00:00", "from_id": "user1",
         "media_type": "voice_message", "file": "voice/v.ogg", "text": ""},
        {"type": "message", "date": "2024-03-05T12:00:00", "from_id": "user1",
         "forwarded_from": "News", "text": "someone else's post"}]}]}}


def test_selfchat_whatsapp_and_telegram(tmp_path, root_plain):
    import json
    folder = tmp_path / "self"
    folder.mkdir()
    (folder / "WhatsApp Chat with Me.txt").write_text(WHATSAPP, encoding="utf-8")
    (folder / "ios.txt").write_text(IOS, encoding="utf-8")
    (folder / "result.json").write_text(json.dumps(TELEGRAM), encoding="utf-8")
    v = Vault(root_plain)
    src = life_sources.selfchat_source(folder)
    assert src.default_tier is sources.Audience.LOCAL
    r = sources.run(v, src, C)
    assert r["reasons"]["a system line"] == 2 and r["reasons"]["deleted"] == 1
    assert r["reasons"]["forwarded"] == 1
    pages = _pages(v, "selfchat")
    assert set(pages) == {"selfchat-2024-03", "selfchat-2024-04"}
    march, april = pages["selfchat-2024-03"], pages["selfchat-2024-04"]
    assert "- 2024-03-13 09:15 WhatsApp: buy milk / and bread" in march
    assert "- 2024-03-13 21:05 WhatsApp: [photo]" in march
    assert "- 2024-03-14 08:00 WhatsApp: [media]" in march
    assert "- 2024-03-14 09:15 WhatsApp: [video]" in march  # month first, from the 14
    assert "- 2024-04-02 19:30 WhatsApp: idea: a study timer" in april
    assert "- 2024-03-02 10:00 Telegram: read SICP ch 3" in march
    assert "Telegram: [photo] whiteboard" in march and "Telegram: [voice note]" in march
    assert "asha's" not in march and "someone else's post" not in march
    assert "IMG-" not in march and "photos/" not in march
    r = sources.run(v, life_sources.selfchat_source(folder), C)
    assert r["imported"] == 0 and _pages(v, "selfchat") == pages


def test_selfchat_refuses_a_chat_with_others_and_counts_bad_files(tmp_path, root_plain):
    two = tmp_path / "two"
    two.mkdir()
    (two / "chat.txt").write_text("13/03/2024, 09:15 - Me: hi\n13/03/2024, 09:16 - Asha: "
                                  "her words\n", encoding="utf-8")
    (two / "result.json").write_text("{not json", encoding="utf-8")
    v = Vault(root_plain)
    r = sources.run(v, life_sources.selfchat_source(two), C)
    assert r["reasons"]["not a chat with yourself"] == 1
    assert r["reasons"]["malformed file"] == 1 and r["imported"] == 0
    assert not _pages(v, "selfchat")


def test_cli_imports_selfchat(tmp_path, root_plain):
    f = tmp_path / "WhatsApp Chat with Me.txt"
    f.write_text(WHATSAPP, encoding="utf-8")
    r = runner.invoke(app, ["import", str(f), "--source", "selfchat", "--vault",
                            str(root_plain)])
    assert r.exit_code == 0, r.output
    assert "Notes to self: read" in r.output
    assert "private: true" in (root_plain / "resources" / "selfchat-private-2024-03.md") \
        .read_text("utf-8")
