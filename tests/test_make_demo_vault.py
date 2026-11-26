"""scripts/make_demo_vault.py: a throwaway demo vault with enough made-up evidence for the
dashboard's Study tab (plan, assessments, confidence mirror), and a re-run adds nothing."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from keepfold import dashboard, learning_eval, planner
from keepfold.cli import _init
from keepfold.events import EventLog
from keepfold.vault import Vault

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("make_demo_vault",
                                              ROOT / "scripts" / "make_demo_vault.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def test_demo_vault(tmp_path, monkeypatch):
    from keepfold import taint

    root = tmp_path / "vault"
    _init(root)  # the first init on a fresh home also makes it the configured vault
    monkeypatch.setattr(taint, "config", lambda: {})  # as with a real vault configured elsewhere
    demo.main([str(root)])
    v = Vault(root)
    n = sum(1 for _ in EventLog(v).iter_events())
    demo.main([str(root)])  # keyed events and existing pages: nothing new
    assert sum(1 for _ in EventLog(v).iter_events()) == n

    assert {a["id"] for a in planner.assessments(v)} == {"practice-oa", "intro-midterm"}
    data = dashboard.build_data(v, now=datetime.now(UTC))
    assert data["today"] and len(data["assessments"]) == 2
    mirror = data["confidence"]
    assert all(r["n"] >= learning_eval.MIN_BAND and r["share"] is not None for r in mirror)
    assert data["counts"]["solid"] >= 1 and len(data["mistakes"]) >= 3


def test_demo_refuses_a_real_vault(tmp_path, monkeypatch):
    # D17: made-up grades are permanent history in a real learner model
    import pytest

    from keepfold import taint

    root = tmp_path / "vault"
    _init(root)
    monkeypatch.delenv("KEEPFOLD_VAULT", raising=False)
    monkeypatch.setattr(taint, "config", lambda: {"vault": str(root)})
    with pytest.raises(SystemExit):
        demo.main([str(root)])
    assert not list(EventLog(Vault(root)).iter_events())
    monkeypatch.setattr(taint, "config", lambda: {})
    EventLog(Vault(root)).append(session="cli", kind="taught", source="external",
                                 now=datetime.now(UTC), fields={"object": "concept:x"})
    with pytest.raises(SystemExit):
        demo.main([str(root)])
    assert demo.refusal(root.resolve()).endswith("already has 1 events of its own")
    demo.main([str(root), "--force"])
    assert sum(1 for _ in EventLog(Vault(root)).iter_events()) > 1
