"""`cortex know` (v6 agenda 9): read-only per-page fidelity of the user model."""

from datetime import UTC, datetime, timedelta

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


def _ev(n, day, payload):
    return {"id": f"e{n:04d}", "kind": "log", "day": day, "ts": f"{day}T12:00:{n:02d}Z",
            "payload": payload}


def _settled(start, claim, p, hit, day="2026-01-01"):
    f = _ev(start, day, {"kind": "forecast", "claims": [claim], "predicted_p": p,
                         "stranger": "s", "same": False})
    return [f, _ev(start + 1, day, {"kind": "settle", "forecast": f["id"], "twin_hit": hit})]


def test_report_counts_and_per_page_calibration_gate(root_plain):
    (root_plain / "me/preferences.md").write_text(PREFS, encoding="utf-8")
    evs = []
    for i in range(20):  # 20 settled forecasts on c-aaaaa: enough for preferences only
        evs += _settled(2 * i, "c-aaaaa", 0.8, "yes")
    out = know.report(Vault(root_plain), evs, "2026-01-02")
    pages = {p["page"]: p for p in out["pages"]}
    prefs, profile = pages["preferences"], pages["profile"]
    assert prefs["live"] == 2 and prefs["tiers"]["legacy"] == 2  # no baseline: lines are legacy
    assert prefs["states"]["learning"] == 1 and prefs["states"]["untested"] == 1
    assert prefs["calibration"]["enough"] is True and prefs["calibration"]["n"] == 20
    assert profile["calibration"]["n"] == 0  # its claims were never forecast
    assert "not enough yet (0 of 20" in know.calibration_line(profile["calibration"])
    assert out["total"]["live"] == 3 and out["calibration"]["n"] == 20
    assert pages["goals"]["live"] == 0


def test_cli_private_escaped_needs_and_writes_nothing(root_committed):
    root = root_committed
    (root / "me/preferences.md").write_text(PREFS, encoding="utf-8")
    note(root, "me/personality.md", 'id: "personality"\nprivate: true\n',
         "# Personality\n\n## Core\n- Secret trait ^c-ccccc\n")
    log = EventLog(Vault(root))
    t0 = datetime.now(UTC) - timedelta(days=2)
    for d in (0, 1):  # a miss on two settle days in a row: c-aaaaa is contested
        f = log.append(session="cli", kind="log", source="tutor", now=t0 + timedelta(days=d),
                       fields={"payload": {"kind": "forecast", "claims": ["c-aaaaa"],
                                           "predicted_p": 0.7}})
        log.append(session="cli", kind="log", source="self", now=t0 + timedelta(days=d),
                   fields={"payload": {"kind": "settle", "forecast": f["id"], "twin_hit": "no"}})
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
    assert "contested 1" in out and "contested: Tea over coffee" in out
    assert "<b>" not in out and "<i>" not in out and "http" not in out
    assert "not enough yet (2 of 20 settled forecasts)" in out
    assert "Open needs: 1 (habits 1)" in out and "Morning &lt;i&gt;or&lt;/i&gt; night?" not in out
    assert "Morning or night?" in out

    assert git(root, "rev-parse", "HEAD") == head
    assert git(root, "status", "--porcelain") == ""
    assert {p: p.read_bytes() for p in root.rglob("*")
            if p.is_file() and ".git" not in p.parts} == before
