"""E3 Learning loop (docs/design/v6/TEST-PLAN.md §4): start, ask, grade, a spot-check flip that
keeps the ask's links (G1), end and undo, then the fold against `cortex today` and `replay`.
In-process: the MCP server over the SDK's memory connection, the CLI through CliRunner."""

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import FIXTURES, events, git, run
from typer.testing import CliRunner

from cortex import levels
from cortex.cli import app
from cortex.events import EventLog, local_day
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

PRIVATE = ("secret-diary", "Private concept")
ASK = {"concept": "two-pointers", "item_type": "application", "question": "Pair sum in O(n)?",
       "problem": "lc-167", "also": ["arrays-basics"], "targets_mistakes": ["dsa.off-by-one"]}
LINE = re.compile(r"^\S+ (.+?): .*\((\S+)\) \[", re.M)  # "<glyph> <label>: <title> (<id>) [why]"


def cli(*args, vault: Path) -> str:
    r = CliRunner().invoke(app,
                           [*map(str, args), "--vault", str(vault)])
    assert r.exit_code == 0, r.output
    return r.output


def load_ladder(root: Path) -> None:
    """tests/fixtures/events/ladder.jsonl appended as CLI events, on their own dates."""
    log = EventLog(Vault(root))
    for line in (FIXTURES / "events" / "ladder.jsonl").read_text("utf-8").splitlines():
        spec = json.loads(line)
        log.append(session="cli", kind=spec["kind"], source=spec.get("source", "tutor"),
                   fields=spec["fields"], now=datetime.fromisoformat(spec["at"]))


def stamps(root: Path) -> dict[str, tuple]:
    return {n.id: (n.fields.get("cortex_status"), str(n.fields.get("cortex_next")))
            for n in Vault(root).iter_notes("concept")}


@pytest.mark.slow  # about 10 s
def test_e3_learning_loop(root, tmp_path):
    got = {}

    async def body(call):
        # 1. a learn session: the card holds no private text
        start = await call("cortex_start", {"mode": "learn"})
        got["session"] = start["session"]
        assert not any(s in json.dumps(start) for s in PRIVATE), start

        # 2. ask with the item's links; grade with one hint; a second grade is refused (#15)
        ask = await call("cortex_ask", ASK)
        got["ask"] = ask["item_id"]
        g = await call("cortex_grade", {"item_id": ask["item_id"], "learner_answer": "two ends",
                                        "verdict": "correct", "hints": 1, "confidence": 3})
        assert "error" not in g, g
        again = await call("cortex_grade", {"item_id": ask["item_id"], "learner_answer": "x",
                                            "verdict": "wrong"})
        assert again["error"] == "invalid_argument" and "already graded" in again["message"]

        # 3. reflect lists the grade to spot-check; a flip keeps the ask's links (G1)
        gid = next(e["id"] for e in events(root) if e["kind"] == "grade")
        rows = (await call("cortex_reflect"))["spot_check"]
        assert [r["event_id"] for r in rows] == [gid]
        flip = await call("cortex_log", {"kind": "spot_check",
                                         "payload": {"event_id": gid, "verdict": "wrong"}})
        assert "flipped correct to wrong" in flip["display_line"]

        # 4. end: the session's changes land in one commit
        got["commit"] = (await call("cortex_end", {"summary": "two pointers"}))["commit"]

    run(root, body)
    evs = events(root)
    ask = next(e for e in evs if e["id"] == got["ask"])
    (sr,) = [e for e in evs if (e.get("payload") or {}).get("kind") == "self_report"]
    assert sr["source"] == "self" and sr["payload"]["verdict"] == "wrong"
    assert sr["payload"]["hints"] == 1 and sr["confidence"] == 3
    assert ask["problem"] == "problem:lc-167"
    for k in ("problem", "also", "targets_mistakes", "item_type", "question"):
        assert sr[k] == ask[k], k
    assert sr["day"] == local_day(datetime.now(UTC))

    sid = got["session"]
    subjects = git(root, "log", "--format=%s").splitlines()
    assert [s for s in subjects if s.startswith(f"cortex: session {sid}")] == [subjects[0]]
    assert git(root, "rev-parse", "HEAD").strip().startswith(got["commit"])
    assert "reverted" in cli("undo", sid, vault=root)
    assert git(root, "log", "-1", "--format=%s").strip() == f"cortex: undo {sid}"
    assert not [e for e in events(root) if e.get("session") == sid]

    # 5. the fold: `cortex today` shows the fold's levels
    load_ladder(root)
    today = local_day(datetime.now(UTC))
    states = levels.replay(Vault(root), today)[0]
    out = cli("today", vault=root)
    shown = {cid: label for label, cid in LINE.findall(out) if cid in states}
    assert shown, out
    assert shown == {c: levels.LABEL[states[c].state] for c in shown}

    # replay twice is idempotent
    assert "0 note(s) updated" not in cli("replay", vault=root)
    before = {p: p.read_bytes() for p in (root / "concepts").glob("*.md")}
    assert "0 note(s) updated" in cli("replay", vault=root)
    assert before == {p: p.read_bytes() for p in (root / "concepts").glob("*.md")}
    first = stamps(root)
    assert first["arrays-basics"][0] == states["arrays-basics"].state

    # the same events in a fresh vault give equal stamps
    fresh = tmp_path / "fresh"
    shutil.copytree(FIXTURES / "vault_mini", fresh)
    git(fresh, "init", "-q")
    load_ladder(fresh)
    cli("replay", vault=fresh)
    assert stamps(fresh) == first

    # a void restores the prior status (#10)
    cli("log", "result", "two-pointers", "fail", vault=root)
    fail = next(e for e in reversed(events(root)) if e["kind"] == "grade")
    cli("replay", vault=root)
    assert stamps(root)["two-pointers"] != first["two-pointers"]
    cli("void", fail["id"], "--reason", "logged by mistake", vault=root)
    cli("replay", vault=root)
    assert stamps(root) == first
