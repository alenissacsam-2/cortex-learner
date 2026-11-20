"""v5 PHASE3 H3: served claims are plain or labelled (ask) and ranked by tier: a plain line
scores 0.5, an (ask) line 0.5 × 0.3 (bets and surprisal were removed on 20 Nov 2026)."""

import pytest

from cortex import claims
from cortex.mcp_server import LEGEND, Cortex
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

PREFS = ("# Preferences\n\n## Coding\nWorks in VS Code.\n- A fact line ^c-aaaaa\n"
         "- A said line ^c-bbbbb\n- An ask line ^c-ccccc\n- A confirmed line ^c-ddddd\n"
         "- An exposed line ^c-eeeee\n")
TIER = {"c-aaaaa": "typed", "c-bbbbb": "legacy", "c-ccccc": "agent", "c-ddddd": "confirmed",
        "c-eeeee": "exposed"}


@pytest.fixture
def served(root, monkeypatch):
    (root / "me/preferences.md").write_bytes(PREFS.encode())
    real = claims.tiers
    monkeypatch.setattr(claims, "tiers", lambda v, ev, log=None: [
        {**r, "tier": TIER.get(r["id"], r["tier"])} for r in real(v, ev)])
    return root


def test_labels_order_legend_and_ids_stripped(served):
    out = Cortex(Vault(served)).cortex_context()["context"]
    body = out.split("## Coding\n")[1].split("\n</untrusted_note>")[0].splitlines()
    # the user's own lines (typed, legacy, confirmed) score 0.5 and come first, newest first;
    # agent and exposed lines score 0.15 and are labelled; the non-list line stays first
    assert body == ["Works in VS Code.", "- A confirmed line", "- A said line", "- A fact line",
                    "- An exposed line (ask)", "- An ask line (ask)"]
    assert "^c-" not in out
    assert out.count(LEGEND) == 1
    assert out.index(LEGEND) < out.index("## Preferences")  # before the claims, unwrapped
    assert "drift to the typical student" not in out


def test_no_label_no_legend(served, monkeypatch):
    monkeypatch.setattr(claims, "tiers", lambda v, ev, log=None: [])  # nothing labelled
    out = Cortex(Vault(served)).cortex_context()["context"]
    assert LEGEND not in out and "## How to read the claims" not in out
    assert "- A fact line\n" in out


def test_resolve_ignores_a_label_suffix(root):
    (root / "me/preferences.md").write_bytes(PREFS.encode())
    v = Vault(root)
    assert claims.resolve(v, "A said line (said)") == "c-bbbbb"
    assert claims.resolve(v, "- An ask line (ask)") == "c-ccccc"
    assert claims.resolve(v, "^c-ddddd") == "c-ddddd"
    with pytest.raises(Exception, match="live claim"):
        claims.resolve(v, "Not a claim")
