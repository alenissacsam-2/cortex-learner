"""scripts/make_demo_vault.py: a throwaway demo vault with enough made-up evidence for the
dashboard's Study tab (plan, assessments, confidence mirror), and a re-run adds nothing."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from cortex import dashboard, learning_eval, planner
from cortex.cli import _init
from cortex.events import EventLog
from cortex.vault import Vault

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("make_demo_vault",
                                              ROOT / "scripts" / "make_demo_vault.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def test_demo_vault(tmp_path):
    root = tmp_path / "vault"
    _init(root)
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
