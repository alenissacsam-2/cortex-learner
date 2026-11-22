"""0.4.1 one-time repair: the lines the 0.4.0 tidy retired as a near-copy (or update or
opposite) of a different fact (`review.apart`) go back from `## Past`, once, in the tidy's
session; a true rewording, an explicit supersedes, a hand retire and a private page stay."""

import json

import pytest
from conftest import events
from test_review import changes
from test_review_auto import VIM, commit, live, page, past, tidy
from typer.testing import CliRunner

from cortex import claims
from cortex.events import EventLog
from cortex.mcp_server import TIDY_FILE, Cortex
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

NEWS, PAPERS = "Reads news every morning", "Reads papers every morning"
PUNE, MUMBAI = "Lives in Pune", "Lives in Mumbai"
HAND = "Drinks green tea at night"


def retire_040(root, pairs):
    """What the 0.4.0 sweep wrote: each (page, retired text, kept text, why) through
    Cortex._auto_retire in one session, then a hand retire of HAND (no `auto`)."""
    c = Cortex(Vault(root))
    rows = {(r["page"], r["text"]): r
            for r in claims.tiers(c.vault, EventLog(c.vault).iter_events())}
    c.session = c.sessions.start("ambient")
    for pg, gone, kept, why in pairs:
        assert c._auto_retire(rows[(pg, gone)], rows[(pg, kept)], why)
    hand = rows[("preferences", HAND)]
    c.vault._mutate(c.vault.path_for("me", "preferences"),
                    lambda t: claims.move_to_past(t, "preferences", hand["id"], "2026-11-21")[0])
    c.sessions.note("patched", change={"tool": "patch", "id": "preferences", "kind": "me",
                                       "op": "retire", "target": hand["id"],
                                       "claim": hand["id"], "why": "duplicate",
                                       "kept": {"page": "preferences",
                                                "claim": rows[("preferences", NEWS)]["id"]}})
    c.sessions.close(summary="0.4.0 tidy")


@pytest.fixture
def merged(root):
    (root / ".cortex").mkdir(exist_ok=True)
    page(root, "preferences", [NEWS, PAPERS, *VIM[:2], PUNE, MUMBAI, HAND], "Habits")
    page(root, "profile", [NEWS, PAPERS], "About", private=True)
    commit(root)
    retire_040(root, [("preferences", PAPERS, NEWS, "duplicate"),
                      ("preferences", VIM[1], VIM[0], "duplicate"),
                      ("preferences", PUNE, MUMBAI, "supersedes")])
    # a private page's lines: the 0.4.0 sweep skipped them, so write the Past line by hand
    v = Vault(root)
    pid = {r["text"]: r["id"] for r in claims.rows(v) if r["page"] == "profile"}
    v._mutate(v.path_for("me", "profile"),
              lambda t: claims.move_to_past(t, "profile", pid[PAPERS], "2026-11-21")[0])
    c = Cortex(v)
    c.session = c.sessions.start("ambient")
    c._auto_retire({"page": "profile", "id": pid[PAPERS]}, {"page": "profile", "id": pid[NEWS]},
                   "duplicate")
    c.sessions.close(summary="0.4.0 tidy")
    return root


def test_repair_puts_back_only_different_facts(merged):
    root = merged
    before = past(root, "preferences")
    assert PAPERS in before and VIM[1] in before and PUNE in before and HAND in before
    got = tidy(root)
    assert got == {"moved": 0, "retired": 0, "restored": 1}
    rows = live(root, "preferences")
    assert PAPERS in rows and PAPERS not in past(root, "preferences")
    text = (root / "me" / "preferences.md").read_text("utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith(f"- {PAPERS}"))
    assert line == f"- {PAPERS} ^{rows[PAPERS]['id']}"  # same id, no (until …) stamp
    assert text.index(line) < text.index("## Past")  # back in its section
    after = past(root, "preferences")
    assert VIM[1] in after and PUNE in after and HAND in after  # rewording, supersedes, hand
    assert PAPERS in past(root, "profile")  # a private page is never touched
    fix = [c for c in changes(root, "preferences") if c.get("why") == "repair"]
    assert [(c["op"], c["claim"], c["auto"]) for c in fix] == [
        ("restore", rows[PAPERS]["id"], True)]
    state = json.loads((root / TIDY_FILE).read_text("utf-8"))
    assert state["repaired"] == 1 and any(rows[PAPERS]["id"] in k for k in state["done"])


def test_repair_runs_once_and_survives_a_later_auto_retire(merged):
    root = merged
    assert tidy(root)["restored"] == 1
    assert tidy(root) == {"moved": 0, "retired": 0}  # nothing left, no session
    # a later near-copy is retired by the sweep, which rewrites TIDY_FILE's "done"
    page(root, "goals", ["Uses Rust for systems code", "Uses Rust for system code"], "Now")
    commit(root)
    assert tidy(root) == {"moved": 0, "retired": 1}
    assert json.loads((root / TIDY_FILE).read_text("utf-8"))["repaired"] == 1
    # a fresh 0.4.0-style wrong retire now would not be repaired: the repair ran once
    c = Cortex(Vault(root))
    rows = live(root, "preferences")
    c.session = c.sessions.start("ambient")
    assert c._auto_retire(rows[NEWS], rows[PAPERS], "duplicate")
    c.sessions.close(summary="0.4.0 tidy")
    assert tidy(root) == {"moved": 0, "retired": 0}
    assert NEWS in past(root, "preferences")


def test_undo_takes_the_repair_back(merged):
    root = merged
    assert tidy(root)["restored"] == 1
    (sid,) = {e["session"] for e in events(root) if e["kind"] == "change"
              and e["payload"].get("change", {}).get("why") == "repair"}
    from cortex.cli import app as cli
    r = CliRunner().invoke(cli, ["undo", sid, "--vault", str(root)])
    assert r.exit_code == 0, r.output
    assert PAPERS in past(root, "preferences") and PAPERS not in live(root, "preferences")
    assert tidy(root) == {"moved": 0, "retired": 0}  # the undo sticks


def test_restore_from_past_picks_the_section():
    text = ("# P\n\n## A\n- one ^c-aaaaa\n\n## B\n- two ^c-bbbbb\n\n## Past\n"
            "- three (confirmed 2026-11-20) (until 2026-11-21) ^c-ccccc\n")
    got = claims.restore_from_past(text, "preferences", "c-ccccc", "b")
    assert "## B\n- two ^c-bbbbb\n- three (confirmed 2026-11-20) ^c-ccccc\n" in got
    assert "until" not in got
    got = claims.restore_from_past(text, "preferences", "^c-ccccc", None)
    assert "## A\n- one ^c-aaaaa\n- three (confirmed 2026-11-20) ^c-ccccc\n" in got
    with pytest.raises(Exception, match="No past claim"):
        claims.restore_from_past(text, "preferences", "c-aaaaa", None)
