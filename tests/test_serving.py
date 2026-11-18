"""v5 PHASE3 H3: served claims are plain or labelled (ask), are ranked by bits × R, and
the average-pull line warns where the twin's guesses drift to the typical student."""

import pytest

from cortex import bets, claims, probes, surprisal
from cortex.mcp_server import LEGEND, Cortex
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

PREFS = ("# Preferences\n\n## Coding\nWorks in VS Code.\n- A fact line ^c-aaaaa\n"
         "- A said line ^c-bbbbb\n- An ask line ^c-ccccc\n- A recheck line ^c-ddddd\n"
         "- A legacy reliable line ^c-eeeee\n")
TIER = {"c-aaaaa": "typed", "c-bbbbb": "legacy", "c-ccccc": "agent", "c-ddddd": "confirmed",
        "c-eeeee": "legacy"}


@pytest.fixture
def served(root, monkeypatch):
    (root / "me/preferences.md").write_bytes(PREFS.encode())
    real = claims.tiers
    monkeypatch.setattr(claims, "tiers", lambda v, ev, log=None: [
        {**r, "tier": TIER.get(r["id"], r["tier"])} for r in real(v, ev)])
    cards = {"c-aaaaa": bets.Bet(state="reliable", R=0.9),
             "c-ddddd": bets.Bet(state="recheck", R=0.5),
             "c-eeeee": bets.Bet(state="reliable", R=0.95)}  # c-bbbbb, c-ccccc: no card
    monkeypatch.setattr(bets, "fold", lambda ev, today, now=None: cards)
    monkeypatch.setattr(surprisal, "bits", lambda ev: {"c-bbbbb": 3.0})
    return root


def test_labels_order_legend_and_ids_stripped(served):
    out = Cortex(Vault(served)).cortex_context()["context"]
    body = out.split("## Coding\n")[1].split("\n</untrusted_note>")[0].splitlines()
    # scores: untested legacy 3 × 0.5 = 1.5, legacy reliable 0.95, fact 0.9, both (ask)
    # 0.5 × 0.3 (tie: newest first); the non-list line stays first; the user's own untested
    # line is served plain (no "(said)")
    assert body == ["Works in VS Code.", "- A said line", "- A legacy reliable line",
                    "- A fact line", "- A recheck line (ask)", "- An ask line (ask)"]
    assert "^c-" not in out
    assert out.count(LEGEND) == 1
    assert out.index(LEGEND) < out.index("## Preferences")  # before the claims, unwrapped
    assert "drift to the typical student" not in out


def test_no_label_no_legend(served, monkeypatch):
    monkeypatch.setattr(claims, "tiers", lambda v, ev, log=None: [])  # nothing labelled
    out = Cortex(Vault(served)).cortex_context()["context"]
    assert LEGEND not in out and "## How to read the claims" not in out
    assert "- A fact line\n" in out


def test_pull_line(served, monkeypatch):
    real = probes.summary
    monkeypatch.setattr(probes, "summary", lambda ev: {**real(ev), "areas": {
        "taste": {"n": 9, "pull": 0.8, "misses": 5}, "work": {"n": 9, "pull": 0.9, "misses": 4},
        "habits": {"n": 9, "pull": 0.6, "misses": 9}, "social": {"n": 3}}})
    cortex = Cortex(Vault(served))
    out = cortex.cortex_context()["context"]
    assert "In taste, my guesses drift to the typical student; ask instead." in out
    assert "In work," not in out and "In habits," not in out and "In social," not in out
    assert "In taste," in cortex._home_preferences()  # the home card serves it too


def test_resolve_ignores_a_label_suffix(root):
    (root / "me/preferences.md").write_bytes(PREFS.encode())
    v = Vault(root)
    assert bets.resolve(v, "A said line (said)") == "c-bbbbb"
    assert bets.resolve(v, "- An ask line (ask)") == "c-ccccc"
    assert bets.resolve(v, "^c-ddddd") == "c-ddddd"
