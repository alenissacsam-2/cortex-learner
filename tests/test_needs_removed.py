"""R3-9: the `need` feature was removed on 21 Nov 2026. An old vault keeps its need and
need_skip events (the log is append-only): every fold and reader ignores them, and a new
need is refused with an error that says the kind was removed."""

from datetime import UTC, datetime

import pytest
from conftest import events, run

from keepfold import dashboard, know
from keepfold.catalog import Catalog
from keepfold.events import EventLog, local_day
from keepfold.reflect import reflect
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")


def test_old_need_events_load_and_new_ones_are_refused(root):
    log = EventLog(Vault(root))
    ev = log.append(session="cli", kind="log", source="tutor", fields={"payload": {
        "kind": "need", "question": "Morning or night?", "area": "habits"}})
    log.append(session="cli", kind="log", source="tutor", fields={"payload": {
        "kind": "need_skip", "need": ev["id"], "forever": False}})
    v, now = Vault(root), datetime.now(UTC)
    know.report(v, log.iter_events(), local_day(now))
    assert "needs" not in reflect(v, Catalog.load(v), now)
    assert "needs" not in dashboard.build_data(v, now=now)
    before = len(events(root))

    async def body(call):
        home = await call("keepfold_start", {"mode": "home"})
        assert "need" not in home and "question for you" not in home["display"]
        for kind in ("need", "need_skip"):
            out = await call("keepfold_log", {"kind": kind, "payload": {"question": "x?"}})
            assert out["error"] == "invalid_argument" and "was removed" in out["message"]
        await call("keepfold_end", {"summary": "done"})

    run(root, body)
    kinds = [e.get("payload", {}).get("kind") for e in events(root)[before:]]
    assert "need" not in kinds and "need_skip" not in kinds
