"""The v5 "second me" experiment (bets, probes, surprisal, letters, circling, drafts and
delegation) was removed on 20 Nov 2026. An old vault keeps its events and me/ files of those
features: every fold and reader must ignore them, and nothing rewrites or deletes them."""

import json
from datetime import UTC, datetime

import anyio
import pytest
from conftest import events, run

from cortex import claims, know, prime
from cortex.events import EventLog, local_day
from cortex.reflect import reflect
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

OLD_KINDS = [
    {"kind": "forecast", "question": "Tea?", "area": "taste", "twin": "tea", "stranger": "coffee",
     "same": False, "claims": ["c-aaaaa"]},
    {"kind": "settle", "forecast": "nope", "answer": "tea", "twin_hit": "yes"},
    {"kind": "baseline", "claim": "c-aaaaa", "p0": 0.4},
    {"kind": "reconfirm", "claim": "c-aaaaa", "verdict": "no"},
    {"kind": "claim_flag", "flagged": "two-pointers", "sentence": "I know it", "fair": False},
    {"kind": "letter_shown", "letter": "l-aaaaa"},
    {"kind": "letter_feedback", "letter": "l-aaaaa", "verdict": "helped"},
    {"kind": "circling", "key": "unvisited:x", "verdict": "dropped"},
    {"kind": "access", "access": "draft", "audience": "professor", "task": "", "ref": "d1"},
]


def _old_vault(root):
    """Write the removed features' events (one with the removed `probe` session mode) and
    files, as an older Cortex left them."""
    log = EventLog(Vault(root))
    for p in OLD_KINDS:
        log.append(session="cli", kind="log", source="tutor", fields={"payload": p})
    ev = log.append(session="cli", kind="log", source="tutor",
                    fields={"payload": {"kind": "settle", "forecast": "x", "twin_hit": "no"}})
    path = next((root / "events").glob(f"*/*/{ev['session']}.jsonl"))
    line = dict(ev, id=ev["id"][:-1] + "Z", mode="probe")  # no longer a valid mode
    path.write_text(path.read_text(encoding="utf-8") + json.dumps(line) + "\n",
                    encoding="utf-8")
    (root / "me/letters.md").write_text("# Letters\n\n- Keep going ^l-aaaaa\n", encoding="utf-8")
    (root / "me/delegation.md").write_text("# What Cortex may do as me\n\n## Never\n\n"
                                          "- Pay anyone\n", encoding="utf-8")
    (root / "voice").mkdir(exist_ok=True)
    (root / "voice/drafts.jsonl").write_text(json.dumps(
        {"id": "d1", "day": "2026-11-01", "audience": "professor", "act": "request",
         "channel": "email", "text": "Dear Prof"}) + "\n", encoding="utf-8")
    return {p: (root / p).read_bytes() for p in ("me/letters.md", "me/delegation.md",
                                                  "voice/drafts.jsonl")}


def test_an_old_vault_with_removed_kinds_still_loads(root):
    files = _old_vault(root)
    before = len(events(root))
    v, now = Vault(root), datetime.now(UTC)
    evs = list(EventLog(v).iter_events())
    day = local_day(now)
    claims.tiers(v, evs)
    know.report(v, evs, day)
    prime.served_claims(v, evs)
    prime.core(v, "help me revise", evs, now)
    from cortex.catalog import Catalog
    pack = reflect(v, Catalog.load(v), now)
    assert not {"bets", "probes", "surprisal", "letters", "circling", "claim_check"} & set(pack)

    async def body(call):
        assert (await call("cortex_prime", {"prompt": "help me write an email"}))["next"]
        home = await call("cortex_start", {"mode": "home"})
        assert "letter" not in home
        assert "error" not in await call("cortex_reflect")
        me = await call("cortex_as_me", {"task": "ask for an extension", "audience": "professor"})
        assert not {"boundaries", "fidelity"} & set(me) and "notes" not in me["self"]
        await call("cortex_end", {"summary": "done"})

    run(root, body)
    # the old files are untouched, and the old events are all still there
    assert {p: (root / p).read_bytes() for p in files} == files
    assert len(events(root)) >= before
    assert sum(e.get("payload", {}).get("kind") == "circling" for e in events(root)) == 1


def test_removed_log_kinds_get_a_clear_refusal(root):
    """An old client sending a removed kind gets the SDK's validation error, which lists the
    kinds there are; no crash, and nothing is written."""
    from mcp import Client

    from cortex.mcp_server import build_server

    async def main():
        async with Client(build_server(Vault(root))) as c:
            for kind in ("forecast", "settle", "baseline", "claim_flag", "letter",
                         "letter_feedback", "circling", "draft", "sent"):
                r = await c.call_tool("cortex_log", {"kind": kind, "payload": {"text": "x"}})
                text = r.content[0].text
                assert r.is_error and "Input should be" in text and "'touch'" in text, text

    before = len(events(root))
    anyio.run(main)
    assert len(events(root)) == before
