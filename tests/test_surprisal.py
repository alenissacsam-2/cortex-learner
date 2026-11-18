"""v5 PHASE3 H4: `baseline` log events, the surprisal folds and the reflect pack."""

import math

import pytest
from conftest import events, run

from cortex import surprisal

pytestmark = pytest.mark.usefixtures("home")

PREFS = ("# Preferences\n\n## Coding\n- Uses Python for everything\n"
         "- Likes [dark](https://x.example) mode editors\n- Writes tests first ^c-abcde\n")


def _ev(id, payload, kind="log"):
    return {"id": id, "kind": kind, "payload": payload}


def test_folds_latest_wins_and_voids_skip():
    evs = [_ev("e1", {"kind": "baseline", "claim": "c-aaaaa", "p0": 0.5}),
           _ev("e2", {"kind": "baseline", "claim": "c-aaaaa", "p0": 0.25}),
           _ev("e3", {"kind": "baseline", "claim": "c-bbbbb", "p0": 0.9}),
           _ev("e4", {"kind": "baseline", "claim": "c-bbbbb", "p0": 0.1}),
           {"id": "v1", "kind": "void", "ref": "e4"}]
    assert surprisal.bits(evs) == {"c-aaaaa": 2.0, "c-bbbbb": -math.log2(0.9)}
    assert surprisal.generic(evs) == {"c-bbbbb": 0.9}


def test_baseline_log_and_reflect_pack(root):
    (root / "me/preferences.md").write_bytes(PREFS.encode())

    async def body(call):
        pack = (await call("cortex_reflect"))["surprisal"]
        assert len(pack["unpriced"]) == 3 and pack["generic"] == []  # me/profile is not priced
        first = pack["unpriced"][0]
        assert first["text"].startswith('<untrusted_note id="claim">')
        assert "^c-" not in pack["unpriced"][2]["text"]  # ids stay out of served text
        assert "https" not in pack["unpriced"][1]["text"]  # plain: links stripped
        for bad in ({"claim": "^c-abcde", "p0": 1.0}, {"claim": "^c-abcde", "p0": 0},
                    {"claim": "^c-abcde", "p0": True}, {"claim": "^c-abcde", "p0": "0.5"},
                    {"claim": "^c-abcde"}, {"claim": "nobody wrote this", "p0": 0.5},
                    {"p0": 0.5}):
            out = await call("cortex_log", {"kind": "baseline", "payload": bad})
            assert out["error"] == "invalid_argument", bad
        await call("cortex_log", {"kind": "baseline",
                                  "payload": {"claim": "^c-abcde", "p0": 0.3}})
        await call("cortex_log", {"kind": "baseline",
                                  "payload": {"claim": "uses python for everything", "p0": 0.8}})
        pack = (await call("cortex_reflect"))["surprisal"]
        assert len(pack["unpriced"]) == 1 and "dark" in pack["unpriced"][0]["text"]
        assert [g["p0"] for g in pack["generic"]] == [0.8]
        assert "Python" in pack["generic"][0]["text"]

    run(root, body)
    logged = [p for e in events(root) if (p := e.get("payload") or {}).get("kind") == "baseline"]
    assert logged[0] == {"kind": "baseline", "claim": "c-abcde", "p0": 0.3}
    assert logged[1]["claim"].startswith("c-") and logged[1]["p0"] == 0.8


def test_unpriced_caps_at_five_and_skips_agent_tier(root):
    from cortex.events import EventLog
    from cortex.vault import Vault

    lines = "".join(f"- Claim number {i}\n" for i in range(7))
    (root / "me/preferences.md").write_bytes(f"# P\n\n## Coding\n{lines}".encode())
    v = Vault(root)
    pack = surprisal.pack(v, list(EventLog(v).iter_events()))
    assert len(pack["unpriced"]) == surprisal.UNPRICED_MAX
    agent = [{"kind": "change", "payload": {"change": {
        "id": "preferences", "claim": r["id"], "tier": "agent"}}} for r in pack["unpriced"]]
    rest = surprisal.pack(v, agent)["unpriced"]
    assert len(rest) == 2 and {r["id"] for r in rest}.isdisjoint(r["id"] for r in pack["unpriced"])
