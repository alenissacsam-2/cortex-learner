"""v5 Phase 1 F4 (backdated journal), F5 (decision options), F6 (probes)."""

from datetime import UTC, datetime

import pytest
from conftest import events, run
from typer.testing import CliRunner

from cortex import probes
from cortex import session as session_mod
from cortex.cli import app
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")


def test_backdated_journal_goes_to_its_day_and_events_keep_today(root, monkeypatch):
    now = datetime(2026, 3, 5, 9, 5).astimezone()
    monkeypatch.setattr(session_mod.SessionManager, "_now", lambda self: now.astimezone(UTC))

    async def body(call):
        await call("cortex_log", {"kind": "journal",
                                  "payload": {"text": "talked about graphs", "date": "2026-02-20"}})
        await call("cortex_log", {"kind": "journal",
                                  "payload": {"text": "same day", "date": "2026-03-05"}})
        for bad in ("2026-03-06", "1999-12-31", "2026-02-30", "20-02-2026", "2026-02-20x", 5):
            out = await call("cortex_log", {"kind": "journal",
                                            "payload": {"text": "x", "date": bad}})
            assert out["error"] == "invalid_argument", bad

    run(root, body)
    old = (root / "journal/2026-02-20.md").read_text(encoding="utf-8")
    assert "- — talked about graphs (added 2026-03-05)" in old
    assert "- 09:05 same day" in (root / "journal/2026-03-05.md").read_text(encoding="utf-8")
    assert {e["day"] for e in events(root)} == {"2026-03-05"}


def test_decision_options_and_chosen(root):
    async def body(call):
        out = await call("cortex_add", {"kind": "decision", "title": "Pick a database", "fields": {
            "options": ["SQLite", "Postgres"], "chosen": "SQLite"}})
        assert "error" not in out, out
        for f in ({"options": ["one"]}, {"options": ["a"] * 7}, {"options": ["a", "b" * 81]},
                  {"options": "a,b"}, {"options": ["a", "b"], "chosen": "c"},
                  {"chosen": "a"}):
            bad = await call("cortex_add", {"kind": "decision", "title": "Other thing",
                                            "fields": f})
            assert bad["error"] == "invalid_argument", f

    run(root, body)
    fields = Vault(root).read("decision", "pick-a-database").fields
    assert list(fields["options"]) == ["SQLite", "Postgres"] and fields["chosen"] == "SQLite"
    assert not (root / "decisions/other-thing.md").exists()


FC = {"question": "Tea or coffee?", "area": "taste", "twin": "tea", "stranger": "coffee",
      "same": False}


def test_forecast_settle_flow(root):
    async def body(call):
        refused = await call("cortex_log", {"kind": "forecast", "payload": FC})
        assert refused["error"] == "invalid_argument"  # not a probe session
        await call("cortex_start", {"mode": "probe"})
        for bad in ({**FC, "area": "health"}, {**FC, "twin": " "}):
            assert (await call("cortex_log", {"kind": "forecast", "payload": bad}))["error"]
        fid = (await call("cortex_log", {"kind": "forecast", "payload": FC}))["id"]
        settle = {"forecast": fid, "answer": "tea", "twin_hit": "yes", "stranger_hit": "no"}
        assert (await call("cortex_log", {"kind": "settle", "payload": {
            **settle, "twin_hit": "maybe"}}))["error"] == "invalid_argument"
        assert "error" not in await call("cortex_log", {"kind": "settle", "payload": settle})
        again = await call("cortex_log", {"kind": "settle", "payload": settle})
        assert again["error"] == "already_settled"
        unknown = await call("cortex_log", {"kind": "settle", "payload": {
            **settle, "forecast": "01JZZZZZZZZZZZZZZZZZZZZZZZ"}})
        assert unknown["error"] == "invalid_argument"
        jid = (await call("cortex_log", {"kind": "forecast", "payload": FC}))["id"]
        await call("cortex_log", {"kind": "void", "payload": {"event_id": jid, "reason": "typo"}})
        voided = await call("cortex_log", {"kind": "settle",
                                           "payload": {**settle, "forecast": jid}})
        assert voided["error"] == "invalid_argument"
        not_fc = [e for e in events(root) if e["kind"] == "void"][0]["id"]
        assert (await call("cortex_log", {"kind": "settle", "payload": {
            **settle, "forecast": not_fc}}))["error"] == "invalid_argument"
        assert (await call("cortex_reflect"))["probes"]["overall"]["n"] == 1

    run(root, body)
    out = CliRunner().invoke(app, ["probes", "--vault", str(root)])
    assert out.exit_code == 0, out.output
    assert "taste" in out.output and "overall" in out.output


def _ev(id, payload, kind="log"):
    return {"id": id, "kind": kind, "payload": payload}


def test_summary_counts_partly_as_half_and_skips_voids():
    evs = [_ev("f1", {"kind": "forecast", "area": "taste"}),
           _ev("f2", {"kind": "forecast", "area": "taste"}),
           _ev("f3", {"kind": "forecast", "area": "work"}),
           _ev("f4", {"kind": "forecast", "area": "work"}),
           _ev("s1", {"kind": "settle", "forecast": "f1", "twin_hit": "yes", "stranger_hit": "no"}),
           _ev("s2", {"kind": "settle", "forecast": "f2", "twin_hit": "partly",
                      "stranger_hit": "partly"}),
           _ev("s3", {"kind": "settle", "forecast": "f3", "twin_hit": "no", "stranger_hit": "yes"}),
           _ev("s4", {"kind": "settle", "forecast": "f4", "twin_hit": "yes", "stranger_hit": "no"}),
           {"id": "v1", "kind": "void", "ref": "s4"}]
    out = probes.summary(evs)
    assert out["areas"]["taste"] == {"n": 2, "twin": 0.75, "paired": 2, "stranger": 0.25,
                                     "uplift": 0.5}
    assert out["areas"]["work"] == {"n": 1, "twin": 0.0, "paired": 1, "stranger": 1.0,
                                    "uplift": -1.0}
    assert out["overall"] == {"n": 3, "twin": 0.5, "paired": 3, "stranger": 0.5, "uplift": 0.0}
    assert out["open"] == 1  # f4: its settle was voided
    assert probes.summary([]) == {"overall": {"n": 0}, "areas": {}, "open": 0, "taps": 0}


def test_a_hand_edited_settle_is_skipped_not_fatal():
    """B14: a settle with a missing or unknown twin_hit raised KeyError in every prime."""
    evs = [_ev("f1", {"kind": "forecast", "area": "taste"}),
           _ev("f2", {"kind": "forecast", "area": "taste"}),
           _ev("s1", {"kind": "settle", "forecast": "f1"}),
           _ev("s2", {"kind": "settle", "forecast": "f2", "twin_hit": "maybe"}),
           _ev("s3", {"kind": "settle", "forecast": "f2", "twin_hit": "yes",
                      "stranger_hit": "yes"})]
    out = probes.summary(evs)
    assert out["overall"] == {"n": 1, "twin": 1.0, "paired": 1, "stranger": 1.0, "uplift": 0.0}
    assert out["open"] == 1  # f1 has no readable settle


def test_summary_pairs_stranger_and_counts_taps():
    """G2: twin over all settles; stranger and uplift over the paired ones only."""
    evs = [_ev("f1", {"kind": "forecast", "area": "taste", "stranger": "x"}),
           _ev("f2", {"kind": "forecast", "area": "taste", "need": "n1"}),
           _ev("f3", {"kind": "forecast", "area": "work", "need": "n2"}),
           _ev("s1", {"kind": "settle", "forecast": "f1", "twin_hit": "no", "stranger_hit": "yes"}),
           _ev("s2", {"kind": "settle", "forecast": "f2", "twin_hit": "yes"}),
           _ev("s3", {"kind": "settle", "forecast": "f3", "twin_hit": "partly"})]
    out = probes.summary(evs)
    assert out["areas"]["taste"] == {"n": 2, "twin": 0.5, "paired": 1, "stranger": 1.0,
                                     "uplift": -1.0}  # paired uplift: twin on f1 only (0)
    assert out["areas"]["work"] == {"n": 1, "twin": 0.5, "paired": 0}
    assert out["overall"]["n"] == 3 and out["overall"]["twin"] == 0.5
    assert out["taps"] == 2 and out["open"] == 0


def test_summary_pull_over_twin_misses_with_same():
    """H5: pull = share of twin misses (stranger arm, `same` set) where the guesses matched."""
    def fc(id, area, same=None):
        return _ev(id, {"kind": "forecast", "area": area, "stranger": "s",
                        **({} if same is None else {"same": same})})

    def st(fid, twin):
        return _ev("s" + fid, {"kind": "settle", "forecast": fid, "twin_hit": twin,
                               "stranger_hit": "no"})

    evs = [fc("f1", "taste", True), fc("f2", "taste", True), fc("f3", "taste", False),
           fc("f4", "taste", True), fc("f5", "taste"), fc("f6", "work", False),
           _ev("f7", {"kind": "forecast", "area": "work", "need": "n1", "same": True}),
           st("f1", "no"), st("f2", "no"), st("f3", "no"), st("f4", "yes"), st("f5", "no"),
           st("f6", "partly"), _ev("sf7", {"kind": "settle", "forecast": "f7", "twin_hit": "no"})]
    out = probes.summary(evs)
    assert out["areas"]["taste"]["misses"] == 3 and out["areas"]["taste"]["pull"] == 0.667
    assert "misses" not in out["areas"]["work"] and "pull" not in out["areas"]["work"]
    assert out["overall"]["misses"] == 3 and out["overall"]["pull"] == 0.667


def test_taps_stay_out_of_the_benchmark():
    """Q3: a tap's stranger may be anchored on the vault: in n, twin and taps, never in
    paired, stranger, uplift or pull."""
    base = [_ev("f1", {"kind": "forecast", "area": "taste", "stranger": "s", "same": False}),
            _ev("s1", {"kind": "settle", "forecast": "f1", "twin_hit": "no",
                       "stranger_hit": "yes"})]
    tap = [_ev("f2", {"kind": "forecast", "area": "taste", "need": "n1", "stranger": "s",
                      "same": True}),
           _ev("s2", {"kind": "settle", "forecast": "f2", "twin_hit": "no",
                      "stranger_hit": "no"})]
    without, out = probes.summary(base)["areas"]["taste"], probes.summary(base + tap)
    row = out["areas"]["taste"]
    assert {k: row[k] for k in ("paired", "stranger", "uplift", "misses", "pull")} == \
        {k: without[k] for k in ("paired", "stranger", "uplift", "misses", "pull")}
    assert row["n"] == 2 and out["taps"] == 1


def test_tap_text_asks_for_the_stranger():
    from pathlib import Path

    from cortex.mcp_server import HOME_NEED

    for word in ("stranger", "same", "claims", "stranger_hit"):
        assert word in HOME_NEED, word
    probe = (Path(__file__).resolve().parent.parent / "skills" / "cortex" / "modes" /
             "probe.md").read_text(encoding="utf-8")
    assert "same, claims}" in probe and "tell-me tap" in probe


def test_probe_mode_has_a_start_hint_and_bank():
    from pathlib import Path

    from cortex.mcp_server import START_NEXT

    assert "probe" in START_NEXT
    skill = Path(__file__).resolve().parent.parent / "skills" / "cortex"
    assert "| `probe` |" in (skill / "SKILL.md").read_text(encoding="utf-8")
    bank = (skill / "modes" / "probe.md").read_text(encoding="utf-8").split("## Question bank")[1]
    lines = [ln for ln in bank.splitlines() if ln.startswith("- ")]
    assert len(lines) >= 25 and len({ln.split(":")[0] for ln in lines}) == 10
