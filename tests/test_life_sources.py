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
