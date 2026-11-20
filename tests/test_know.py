"""`cortex know` (v6 agenda 9): read-only per-page fidelity of the user model."""

import pytest
from conftest import git, note
from typer.testing import CliRunner

from cortex import know
from cortex.cli import app
from cortex.events import EventLog
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

PREFS = ("# Preferences\n\n## Food\n- Tea <b>over</b> [coffee](http://x.test) ^c-aaaaa\n"
         "- Spicy food ^c-bbbbb\n")


def test_report_counts(root_plain):
    (root_plain / "me/preferences.md").write_text(PREFS, encoding="utf-8")
    out = know.report(Vault(root_plain), [], "2026-01-02")
    pages = {p["page"]: p for p in out["pages"]}
    prefs = pages["preferences"]
    assert prefs["live"] == 2 and prefs["tiers"]["legacy"] == 2  # no baseline: lines are legacy
    assert not {"states", "calibration", "contested"} & set(prefs)
    assert out["total"]["live"] == 3 and "calibration" not in out
    assert pages["goals"]["live"] == 0


def test_cli_private_escaped_needs_and_writes_nothing(root_committed):
    root = root_committed
    (root / "me/preferences.md").write_text(PREFS, encoding="utf-8")
    note(root, "me/personality.md", 'id: "personality"\nprivate: true\n',
         "# Personality\n\n## Core\n- Secret trait ^c-ccccc\n")
    log = EventLog(Vault(root))
    log.append(session="cli", kind="log", source="tutor", fields={"payload": {
        "kind": "need", "question": "Morning <i>or</i> night?", "area": "habits"}})
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "setup")
    head, before = git(root, "rev-parse", "HEAD"), {p: p.read_bytes() for p in root.rglob("*")
                                                    if p.is_file() and ".git" not in p.parts}

    r = CliRunner().invoke(app, ["know", "--vault", str(root)])
    assert r.exit_code == 0, r.output
    out = r.output
    assert CliRunner().invoke(app, ["claims", "--summary", "--vault", str(root)]).output == out
    assert "me/personality: private" in out and "Secret trait" not in out
    assert "me/preferences: 2 live claims" in out
    assert "contested" not in out and "calibrat" not in out.lower()
    assert "<b>" not in out and "<i>" not in out and "http" not in out
    assert "Open needs: 1 (habits 1)" in out and "Morning &lt;i&gt;or&lt;/i&gt; night?" not in out
    assert "Morning or night?" in out

    assert git(root, "rev-parse", "HEAD") == head
    assert git(root, "status", "--porcelain") == ""
    assert {p: p.read_bytes() for p in root.rglob("*")
            if p.is_file() and ".git" not in p.parts} == before
