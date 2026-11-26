"""v6 agenda 7: `keepfold export context`, the prime core as a paste-ready snapshot."""

import json
import re
from datetime import UTC, datetime

import pytest
from conftest import git
from typer.testing import CliRunner

from keepfold import export_context as ec
from keepfold.cli import app
from keepfold.events import EventLog
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

PREFS = """---
summary: Preferences
---
# Preferences

## Communication
- Short answers please (confirmed 2026-11-01) ^c-aaaaa
- Uses emoji a lot ^c-bbbbb
- Likes long essays ^c-ccccc
- My key is sk-abcdefghijklmnopqrstuvwx ^c-ddddd
- Typed by hand, see [docs](https://evil.example/x) ^c-eeeee

## Past
- Used to like tabs (until 2026-10-01) ^c-fffff

## Not me
- Loves meetings (rejected 2026-11-01)
"""
PERSONALITY = """# Personality

## Values
- Honesty first ^c-ggggg

## Communication
- Direct talker ^c-hhhhh
"""
GOALS = "# Goals\n\n## Now\n- Finish thesis ^c-iiiii\n\n## Later\n- Learn Rust ^c-jjjjj\n"
TIERS = {"aaaaa": ("preferences", "confirmed"), "bbbbb": ("preferences", "agent"),
         "ccccc": ("preferences", "exposed")}


def _setup(root, prefs=PREFS):
    for page, text in (("preferences", prefs), ("personality", PERSONALITY), ("goals", GOALS)):
        (root / f"me/{page}.md").write_text(text, encoding="utf-8")
    with (root / "inbox.md").open("a", encoding="utf-8") as f:
        f.write("\n## About me (unconfirmed)\n- Communication: Secretly loves jazz\n")
    log = EventLog(Vault(root))
    log.append(session="cli", kind="change", source="tutor",  # every line after it: typed
               fields={"payload": {"note": "claims_baseline", "claims": {}}})
    for cid, (page, tier) in TIERS.items():
        log.append(session="cli", kind="change", source="tutor", fields={"payload": {
            "change": {"tool": "keepfold_patch", "id": page, "claim": f"c-{cid}", "tier": tier}}})


def _build(root):
    v = Vault(root)
    return ec.build(v, list(EventLog(v).iter_events()), datetime(2026, 11, 16, 9, tzinfo=UTC))


def test_only_confirmed_and_typed_live_lines_leave(root):
    _setup(root)
    text, skipped = _build(root)
    assert text.startswith("# About me (Keepfold snapshot, 2026-11-16)")
    for want in ("Test learner, synthetic.", "- Ask before you teach.",
                 "- [Communication] Short answers please",
                 "- [Communication] Direct talker", "- Finish thesis"):
        assert want in text, want
    # S1/W1b: a web link on me/preferences is held (G6) until Keep it, so that line stays out
    for leak in ("Typed by hand", "emoji", "long essays", "sk-", "tabs", "meetings", "jazz",
                 "Honesty", "Rust", "^c-", "(confirmed", "evil.example", "<untrusted_note"):
        assert leak not in text, leak
    assert skipped == 2  # the agent and the exposed line
    assert text.index("## Profile") < text.index("## How to help me") < text.index(
        "## Current goals") < text.index("## Preferences")


def test_profile_without_summary_keeps_only_act_grade_lines(root):
    _setup(root)
    (root / "me/profile.md").write_text(
        "# Profile\n\n## Brief\n- Typed fact ^c-kkkkk\n- Exposed guess ^c-lllll\n"
        "- Old fact (until 2020-01-01) ^c-mmmmm\n", encoding="utf-8")
    EventLog(Vault(root)).append(session="cli", kind="change", source="tutor", fields={
        "payload": {"change": {"tool": "keepfold_patch", "id": "profile", "claim": "c-lllll",
                               "tier": "exposed"}}})
    text, _ = _build(root)
    profile = text.split("## Profile\n")[1].split("\n\n")[0]
    assert profile == "- Typed fact"
    for leak in ("Exposed", "Old fact", "^c-", "until"):
        assert leak not in text, leak


def test_the_init_template_summary_is_no_profile(root):
    _setup(root)
    (root / "me/profile.md").write_text(
        "---\nsummary: Who I am\n---\n# Profile\n\n## Brief\n- Typed fact ^c-kkkkk\n",
        encoding="utf-8")
    text, _ = _build(root)
    assert text.split("## Profile\n")[1].split("\n\n")[0] == "- Typed fact"


def test_private_page_is_left_out(root):
    _setup(root, PREFS.replace("summary: Preferences", "private: true"))
    text, _ = _build(root)
    assert "Short answers" not in text and "Direct talker" in text


def test_cap_trims_whole_preference_lines(root):
    lines = "".join(f"- Preference number {n:03d} " + "x" * 60 + "\n" for n in range(200))
    _setup(root, PREFS.replace("## Past", lines + "\n## Past"))
    text, _ = _build(root)
    assert len(text) <= ec.MAX_CHARS
    assert "- Finish thesis" in text and "Ask before you teach" in text
    kept = [ln for ln in text.splitlines() if "Preference number" in ln]
    assert 0 < len(kept) < 200
    assert all(ln.endswith("x" * 60) for ln in kept)


def test_cli_writes_outside_the_vault_only_and_writes_nothing_in_it(root, tmp_path):
    _setup(root)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "setup")
    head = git(root, "rev-parse", "HEAD")
    run = CliRunner().invoke
    out = tmp_path / "about-me.md"
    r = run(app, ["export", "context", str(out), "--vault", str(root)])
    assert r.exit_code == 0, r.output
    # the CLI stamps today in the header; the fixed-date build is compared below it
    undated = lambda t: re.sub(r"\d{4}-\d\d-\d\d", "<day>", t)  # noqa: E731
    assert undated(out.read_text(encoding="utf-8")) == undated(_build(root)[0])
    r = run(app, ["export", "context", "--vault", str(root)])
    assert r.exit_code == 0 and "Short answers please" in r.stdout and "emoji" not in r.stdout
    for bad in (root / "me" / "x.md", ec.REPO / "about-me.md"):
        r = run(app, ["export", "context", str(bad), "--vault", str(root)])
        assert r.exit_code == 1 and "Refusing to write inside" in r.output
        assert not bad.exists()
    assert git(root, "rev-parse", "HEAD") == head and not git(root, "status", "--porcelain")


def test_cli_refuses_an_excluded_folder(root, tmp_path, home):
    work = tmp_path / "Work"
    work.mkdir()
    (home / ".keepfold").mkdir()
    (home / ".keepfold" / "config.yaml").write_text(f"exclude:\n  - '{work}'\n", encoding="utf-8")
    r = CliRunner().invoke(app, ["export", "context", str(work / "me.md"), "--vault", str(root)])
    assert r.exit_code == 1 and "excluded" in r.output and not (work / "me.md").exists()


def test_mcp_config_snippet(root):
    r = CliRunner().invoke(app, ["export", "context", "--mcp-config", "--vault", str(root)])
    assert r.exit_code == 0, r.output
    entry = json.loads(r.stdout)["mcpServers"]["keepfold"]
    assert entry["args"] == ["mcp", "--vault", str(root.resolve())]
    new = CliRunner().invoke(app, ["setup", "--print-mcp-config", "--vault", str(root)])
    assert new.exit_code == 0 and new.output == r.output  # review r4 S8
