"""The importer shape (sources.Source, sources.run): the never-capture filter, the tier asked
per source and remembered, for every source the same way (v9 item 13, C2 and C6)."""

import json
from pathlib import Path

import pytest
import typer.testing
from typer.testing import CliRunner

from cortex import sources
from cortex.cli import app
from cortex.vault import Vault

EXPORTS = Path(__file__).parent / "fixtures" / "exports"
runner = CliRunner()
L, C, S = sources.Audience.LOCAL, sources.Audience.CLOUD, sources.Audience.SKIP


def _src(*items, **kw) -> sources.Source:
    return sources.Source(kw.pop("id", "test"), "Test", "file", lambda: list(items), **kw)


def _page(v: Vault, nid: str) -> str:
    return (v.root / "resources" / f"{nid}.md").read_bytes().decode("utf-8")


def test_secrets_are_redacted_for_every_source(root_plain):
    v = Vault(root_plain)
    key = "sk-" + "a1B2" * 8
    sources.run(v, _src(sources.Item(f"Keys {key}", "2026-11-02", f"mine: {key}\n"
                                     "password: hunter2hunter2\n", f"https://x.example/?k={key}"),
                        sources.Item("Line", "2026-11-03", f"token {key}"), page="item"))
    sources.run(v, _src(sources.Item("Line", "2026-11-03", f"token {key}"), id="cal",
                        page="month"))
    text = _page(v, "test-keys-redacted") + _page(v, "cal-2026-11")
    assert key not in text and "hunter2" not in text and text.count("[redacted]") >= 4


def test_coordinates_are_rounded_to_a_city(root_plain):
    v = Vault(root_plain)
    sources.run(v, _src(sources.Item("Walk", "2026-11-02", "met at 12.971599, 77.594566 and "
                                     "maps @-33.86882,151.20929,15z; version 3.14159, 999.12345",
                                     "https://maps.example/@12.971599,77.594566"),
                        coarse_place=True))
    page = _page(v, "test-walk")
    assert "13.0, 77.6" in page and "@-33.9,151.2,15z" in page and "@13.0,77.6" in page
    assert "12.971599" not in page and "151.20929" not in page
    assert "3.14159, 999.12345" in page  # not a coordinate: longitude past 180


def test_quoted_mail_is_dropped(root_plain):
    v = Vault(root_plain)
    sources.run(v, _src(
        sources.Item("Reply", "2026-11-02", "Sounds good, see you Tuesday.\n\nOn Mon, 2 Nov "
                     "2026 at 09:30, Asha <asha@example.com>\nwrote:\n> Can we meet?\n> Her "
                     "secret plan\n"),
        sources.Item("Fwd", "2026-11-03", "FYI below.\n---------- Forwarded message "
                     "---------\nFrom: Ravi\nRavi's private words\n"),
        sources.Item("Note", "2026-11-04", "> A quote I keep in my notes\n"), strips_quotes=True))
    reply, fwd = _page(v, "test-reply"), _page(v, "test-fwd")
    assert "Sounds good" in reply and "Can we meet" not in reply and "Asha" not in reply
    assert "FYI below." in fwd and "Ravi" not in fwd
    assert "> A quote I keep in my notes" in _page(v, "test-note")  # no "wrote:": kept


def test_a_note_keeps_its_quoted_mail_and_coordinates(root_plain, tmp_path):
    """The owner's own notes are their writing: only secrets are filtered from them."""
    from cortex import importers
    notes = tmp_path / "notes"
    notes.mkdir()
    body = ("Kept this reply.\n\nOn Mon, 2 Nov 2026 at 09:30, Asha wrote:\n> Can we meet?\n"
            "---------- Forwarded message ---------\nFrom: Ravi\n\n"
            "Cafe at 12.971599, 77.594566\n```\nprint('x')\n```\n")
    (notes / "Mail.md").write_bytes(body.encode("utf-8"))
    importers.import_notes(Vault(root_plain), notes)
    assert body.strip() in _page(Vault(root_plain), "notes-mail")


def test_code_is_dropped_only_from_sources_that_say_so(root_plain):
    """Code from AI chats and coding sessions is left out; the owner's notes keep theirs."""
    v = Vault(root_plain)
    text = "Before\n```python\nprint('x')\n```\nAfter\n~~~\nrm -rf /\n"  # the last is unclosed
    sources.run(v, _src(sources.Item("Mine", "2026-11-02", text)))
    sources.run(v, _src(sources.Item("Agent", "2026-11-02", "AI " + text), id="ai",
                        drops_code=True))
    assert "print('x')" in _page(v, "test-mine") and "rm -rf" in _page(v, "test-mine")
    ai = _page(v, "ai-agent")
    assert "print" not in ai and "rm -rf" not in ai and "Before" in ai and "After" in ai
    assert ai.count(sources.CODE_LEFT_OUT) == 2


def test_tiers_private_and_skip(root_plain):
    v = Vault(root_plain)
    item = sources.Item("Day", "2026-11-02", "line")
    assert sources.run(v, _src(item), S)["imported"] == 0
    assert not list((root_plain / "resources").glob("test-*"))
    r = sources.run(v, _src(item), L)
    assert r["private"] == r["imported"] == 1 and "private: true" in _page(v, "test-day")
    sources.run(v, _src(item, id="m", page="month"), L)
    assert "private: true" in _page(v, "m-private-2026-11")
    assert sources.run(v, _src(item), L, dry_run=True)["duplicate"] == 1


def test_the_tier_is_given_asked_or_remembered(root_plain):
    v, src, asked = Vault(root_plain), _src(id="notes"), []
    assert sources.pick_tier(v, src) == (C, "the default")
    assert sources.pick_tier(v, src, "private", save=False) == (L, "chosen")  # a dry run
    assert sources.pick_tier(v, src) == (C, "the default")
    assert sources.pick_tier(v, src, ask=lambda s: asked.append(s.id) or "skip") == (S, "chosen")
    assert sources.pick_tier(v, src, ask=lambda s: asked.append(s.id)) == \
        (S, "your last choice")  # asked once
    assert asked == ["notes"]
    assert sources.pick_tier(v, src, "cloud") == (C, "chosen")  # the app sends audience values
    saved = json.loads((root_plain / sources.TIERS_FILE).read_bytes())
    assert saved == {"notes": "cloud"}
    with pytest.raises(ValueError, match="tier must be one of"):
        sources.tier("public")


def _cli(root, *args, **kw):
    return runner.invoke(app, ["import", *map(str, args), "--vault", str(root)], **kw)


def test_cli_tier_flag_default_and_memory(root_plain):
    ics = EXPORTS / "calendar.ics"
    out = _cli(root_plain, ics, "--dry-run").output
    assert "Calendar: tier normal (the default; change it with --tier)" in out
    out = _cli(root_plain, ics, "--tier", "skip").output
    assert "Calendar: skip, so nothing was imported." in out
    assert not list((root_plain / "resources").glob("calendar-*"))
    out = _cli(root_plain, ics).output
    assert "tier skip (your last choice; change it with --tier)" in out
    out = _cli(root_plain, ics, "--tier", "private").output
    assert "calendar: read 3, imported 3, private 3" in out
    assert (root_plain / "resources" / "calendar-private-2026-11.md").is_file()
    r = _cli(root_plain, EXPORTS / "perplexity_thread.md", "--tier", "normal")
    assert r.exit_code == 1 and "--tier applies to notes, calendars, docs" in r.output
    r = _cli(root_plain, ics, "--tier", "public")
    assert r.exit_code == 1 and "--tier must be one of private, normal, skip" in r.output


def test_cli_asks_once_per_source_in_a_terminal(root_plain, tmp_path, monkeypatch):
    monkeypatch.setattr(typer.testing._NamedTextIOWrapper, "isatty", lambda self: True,
                        raising=False)
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "One.md").write_bytes(b"# One\n\nfirst\n")
    out = _cli(root_plain, notes, input="private\n").output
    assert "Notes: who may read these pages?" in out and "[normal]" in out
    assert "Notes: tier private (your answer)" in out
    assert "private: true" in (root_plain / "resources" / "notes-one.md").read_text("utf-8")
    (notes / "Two.md").write_bytes(b"# Two\n\nsecond\n")
    out = _cli(root_plain, notes).output  # remembered: not asked again
    assert "who may read" not in out and "tier private (your last choice" in out


# ---- RR-2: the quality gate, for every source -------------------------------------------

PROSE = ("I spent the evening reading about B-trees and why databases like them: every node "
         "holds many keys, so a lookup touches only a few disk pages. Tomorrow I will try to "
         "write one in Python and compare it with a plain sorted list.\n")
CODE = ("def merge(a, b):\n    out, i, j = [], 0, 0\n    while i < len(a) and j < len(b):\n"
        "        if a[i] <= b[j]:\n            out.append(a[i]); i += 1\n        else:\n"
        "            out.append(b[j]); j += 1\n    return out + a[i:] + b[j:]\n")
HINDI = ("आज मैंने डेटा संरचनाओं के बारे में पढ़ा। बाइनरी सर्च ट्री में हर नोड की बाईं ओर छोटी "
         "और दाईं ओर बड़ी कुंजियाँ होती हैं, इसलिए खोज तेज़ होती है। कल मैं इसे खुद लिखकर "
         "देखूँगा और सूची से तुलना करूँगा।\n")


@pytest.mark.parametrize("text, why", [
    ("Notes on graphs " + "�" * 30 + PROSE, "garbled"),
    ("|=+-*/#@!{}[]<>~^%$&;:" * 12 + "ok", "mostly symbols"),
    ("".join(f"- https://example.com/articles/{i}/a-long-slug-here\n" for i in range(8))
     + "Links I saved\n", "link list"),
    ("same line again and again\n" * 12 + "one\ntwo\n", "repetitive"),
    (PROSE, None), (CODE * 2, None), (HINDI, None),
    ("�" * 50, None), ("https://example.com/x\n" * 5, None),  # short: never judged
])
def test_unreadable(text, why):
    assert sources.unreadable(text) == why


def test_garbage_items_are_skipped_and_counted_in_the_report(root_plain):
    from cortex import importers
    v = Vault(root_plain)
    links = "".join(f"https://example.com/page/{i}/with/a/long/path\n" for i in range(10))
    r = sources.run(v, _src(sources.Item("Bad", "2026-11-02", "x" + "�" * 250),
                            sources.Item("Worse", "2026-11-02", "�" * 300),
                            sources.Item("Links", "2026-11-02", links),
                            sources.Item("Good", "2026-11-02", PROSE),
                            sources.Item("Hindi", "2026-11-02", HINDI)))
    assert r["imported"] == 2 and r["read"] == 5
    assert r["unreadable"] == {"garbled": 2, "link list": 1}
    assert not (root_plain / "resources" / "test-bad.md").exists()
    assert importers.report_line("Test", r).endswith(
        "skipped 0. Skipped 3 as unreadable (2 garbled, 1 link list)")
    clean = sources.run(v, _src(sources.Item("Fine", "2026-11-03", PROSE + "more")))
    assert "unreadable" not in importers.report_line("Test", clean)


# ---- RR-29: a stricter tier reaches the pages already imported ---------------------------

def _private(v: Vault, nid: str) -> bool:
    return "private: true" in _page(v, nid)


def test_a_private_tier_makes_earlier_pages_private(root_plain):
    v, told = Vault(root_plain), []
    src = _src(sources.Item("One", "2026-11-02", "first"), sources.Item("Two", "2026-11-03", "x"),
               id="notes")
    sources.run(v, src, C)
    sources.run(v, sources.Source("o", "Other", "file", lambda: [
        sources.Item("Other", "2026-11-02", "theirs")]), C)
    (root_plain / "resources" / "notes-two.md").write_bytes(
        _page(v, "notes-two").replace("Source: Test", "Source: Obsidian").encode())  # same key
    assert sources.pick_tier(v, src, "private", save=False, said=told.append)[0] is L
    assert told == [] and not _private(v, "notes-one")  # a dry run changes nothing
    assert sources.pick_tier(v, src, "private", said=told.append) == (L, "chosen")
    assert told == ["Made 2 earlier pages from Test private."]
    assert _private(v, "notes-one") and _private(v, "notes-two") and not _private(v, "o-other")
    assert sources.pick_tier(v, src, "private", said=told.append)[0] is L
    assert len(told) == 1  # the same tier again: nothing to do


def test_loosening_never_unprivates_and_skip_only_says(root_plain):
    v, told = Vault(root_plain), []
    src = _src(sources.Item("One", "2026-11-02", "first"), sources.Item("Day", "2026-11-02",
                                                                         "line"))
    sources.run(v, src, C)
    sources.run(v, _src(sources.Item("Day", "2026-11-02", "line"), page="month"), C)
    sources.pick_tier(v, src, "private", said=told.append)
    assert told == ["Made 3 earlier pages from Test private."]  # month pages too
    sources.pick_tier(v, src, "normal", said=told.append)
    assert len(told) == 1 and _private(v, "test-one") and _private(v, "test-2026-11")
    sources.pick_tier(v, src, "skip", said=told.append)
    assert told[1].startswith("3 pages from Test are still in your vault; to forget them, "
                              'delete the pages in resources/ that say "Source: Test"')
    assert (root_plain / "resources" / "test-one.md").is_file()  # nothing deleted


def test_cli_and_app_say_what_a_stricter_tier_did(root_plain, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "One.md").write_bytes(b"# One\n\nfirst\n")
    _cli(root_plain, notes, "--tier", "normal")
    assert not _private(Vault(root_plain), "notes-one")
    out = _cli(root_plain, notes, "--tier", "private").output
    assert "Notes: tier private (--tier)\nMade 1 earlier page from Notes private." in out
    assert _private(Vault(root_plain), "notes-one")
    from cortex.app import actions

    class Job:
        def say(self, _):
            pass

    out = actions.import_run(Job(), "notes", notes, root_plain, "skip")
    assert out["lines"] == ["Nothing was imported: you chose Don't save.",
                            '1 page from Notes is still in your vault; to forget them, delete '
                            'the pages in resources/ that say "Source: Notes" under their title.']


def test_a_changed_day_line_replaces_the_old_one(root_plain):
    """Audit 21 Nov I8: a per-day summary line (photos, location) whose count changed replaces
    that day's line instead of adding a second one; other lines stay."""
    v = Vault(root_plain)

    def run(*texts):
        return sources.run(v, _src(*(sources.Item(t[:10], t[:10], t) for t in texts),
                                   page="month"), C)
    run("2026-11-02: 3 photos (Trip)", "2026-11-03: 1 photo (Home)")
    r = run("2026-11-02: 5 photos (Trip)", "2026-11-04: 2 photos (Home)")
    page = _page(v, "test-2026-11")
    assert page.count("2026-11-02:") == 1 and "- 2026-11-02: 5 photos (Trip)" in page
    assert "- 2026-11-03: 1 photo (Home)" in page and "- 2026-11-04: 2 photos" in page
    assert (r["imported"], r["updated"], r["duplicate"]) == (1, 1, 0)


def test_an_unchanged_reimport_keeps_the_owners_tags_and_lines(root_plain):
    """Audit 21 Nov I5: an `updates` source (Kindle highlights, by key) re-imported unchanged
    leaves the page alone; a changed one rewrites it and keeps the owner's fields."""
    v = Vault(root_plain)

    def src(text):
        return _src(sources.Item("Book", "2026-11-02", text, "clips.txt", key="book"),
                    updates=True)
    sources.run(v, src("- first highlight"), C)
    v._edit_fields("resource", "test-book", {"tags": ["mine"]})
    p = root_plain / "resources" / "test-book.md"
    p.write_text(p.read_text("utf-8") + "\nMy own line.\n", encoding="utf-8")
    before = _page(v, "test-book")
    r = sources.run(v, src("- first highlight"), C)
    assert (r["updated"], r["duplicate"]) == (0, 1)
    assert _page(v, "test-book") == before
    r = sources.run(v, src("- first highlight\n- second highlight"), C)
    page = _page(v, "test-book")
    assert r["updated"] == 1 and "second highlight" in page and 'tags: ["mine"]' in page
