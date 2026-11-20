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
                                     "https://maps.example/@12.971599,77.594566")))
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
        sources.Item("Note", "2026-11-04", "> A quote I keep in my notes\n")))
    reply, fwd = _page(v, "test-reply"), _page(v, "test-fwd")
    assert "Sounds good" in reply and "Can we meet" not in reply and "Asha" not in reply
    assert "FYI below." in fwd and "Ravi" not in fwd
    assert "> A quote I keep in my notes" in _page(v, "test-note")  # no "wrote:": kept


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
    assert r.exit_code == 1 and "--tier applies to notes and calendars only" in r.output
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
