"""v5 Phase 3 H1 (claim cards) and H2 (eras and surfaces)."""

import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from conftest import events, run
from typer.testing import CliRunner

from cortex import bets
from cortex import events as events_mod
from cortex.cli import app
from cortex.events import local_day
from cortex.vault import Vault, VaultError

pytestmark = pytest.mark.usefixtures("home")

FIXTURES = Path(__file__).parent / "fixtures"
_local_day = events_mod.local_day
C = "c-aaaaa"


@pytest.fixture(autouse=True)
def utc_days(monkeypatch):
    # Days follow UTC so due dates do not depend on the machine's zone (as in test_levels).
    monkeypatch.setattr(events_mod, "local_day", lambda ts, tz=None: _local_day(ts, UTC))


def D(n: int) -> str:
    return (date(2026, 1, 1) + timedelta(days=n)).isoformat()


_n = [0]


def ev(day: int, payload: dict, kind: str = "log") -> dict:
    _n[0] += 1
    return {"id": f"e{_n[0]:05d}", "kind": kind, "day": D(day),
            "ts": f"{D(day)}T12:00:{_n[0] % 60:02d}Z", "payload": payload}


def bet(day: int, hit: str, stranger: bool = True, same: bool = False, claim: str = C) -> list:
    f = ev(day, {"kind": "forecast", "claims": [claim],
                 **({"stranger": "s", "same": same} if stranger else {})})
    return [f, ev(day, {"kind": "settle", "forecast": f["id"], "twin_hit": hit})]


def weak(day: int, claim: str = C) -> dict:
    return ev(day, {"note": "patched", "change": {"op": "append_to_section", "claim": claim,
                                                   "tier": "confirmed"}}, kind="change")


def card(evs: list, today: int, claim: str = C) -> bets.Bet:
    return bets.fold(evs, D(today))[claim]


RELIABLE = bet(0, "yes") + bet(3, "yes")  # 2 hit days, stability ≈ 13.8, due 2026-01-30


def test_discriminating_hit_is_good_plain_hit_is_no_review():
    sharp = card(bet(0, "yes"), 0)
    assert (sharp.state, sharp.hits, sharp.plain_hits) == ("learning", 1, 0)
    assert sharp.R is not None and sharp.stability > 0
    for plain in (bet(0, "yes", same=True), bet(0, "yes", stranger=False)):
        b = card(plain, 0)
        assert (b.state, b.hits, b.plain_hits, b.R, b.stability) == ("untested", 0, 1, None, None)
    partly = card(bet(0, "partly"), 0)  # Hard: a card, but no discriminating hit
    assert partly.state == "untested" and partly.R is not None and partly.hits == 0


def test_reliable_after_two_hit_days_with_stability_ten():
    one_day = card(bet(0, "yes") + bet(0, "yes"), 0)
    assert one_day.state == "learning" and one_day.hits == 2  # one review per day
    b = card(RELIABLE, 3)
    assert b.state == "reliable" and b.stability >= 10 and b.hits == 2
    assert card(bet(0, "yes") + bet(1, "yes"), 1).stability < 10  # too close: still learning
    assert card(bet(0, "yes") + bet(1, "yes"), 1).state == "learning"


def test_weak_yes_never_reaches_reliable():
    evs = [weak(d) for d in (0, 3, 10, 30, 80, 200)]
    evs.append(ev(300, {"kind": "reconfirm", "claim": C, "verdict": "yes"}))
    b = card(evs, 300)
    assert b.state == "untested" and b.hits == 0 and b.stability >= 10
    one_hit = card(bet(0, "yes") + [weak(d) for d in (3, 10, 30, 80)], 80)
    assert one_hit.state == "learning" and one_hit.stability >= 10  # needs 2 hit days
    retired = ev(0, {"note": "patched", "change": {"op": "retire", "claim": C,
                                                   "tier": "confirmed"}}, kind="change")
    assert bets.fold([retired], D(0)) == {}


def test_slip_is_hard_then_a_second_miss_contests_until_a_hit():
    slip = card(RELIABLE + bet(10, "no"), 10)
    assert slip.state == "learning" and slip.stability >= 10  # Hard, not a lapse
    assert (slip.misses, slip.first_miss) == (1, D(10))
    contested = card(RELIABLE + bet(10, "no") + bet(15, "no"), 15)  # a miss within 30 days
    assert contested.state == "contested" and contested.stability < 10
    assert contested.first_miss == D(10)
    cleared = card(RELIABLE + bet(10, "no") + bet(15, "no") + bet(20, "yes"), 20)
    assert cleared.state == "learning" and cleared.first_miss is None
    fresh = card(bet(0, "yes") + bet(1, "no"), 1)  # Again, but never reliable
    assert fresh.state == "learning" and fresh.stability < 1


def test_two_misses_in_a_row_contest_even_at_high_stability():
    never_reliable = bet(0, "yes") + [weak(d) for d in (3, 10, 30, 80)]  # one hit day
    one = card(never_reliable + bet(100, "no"), 100)
    assert one.state == "learning" and one.stability >= 10  # a slip
    two = card(never_reliable + bet(100, "no") + bet(140, "no"), 140)  # both slips (Hard)
    assert two.state == "contested" and two.stability >= 10
    assert card(bet(0, "no") + bet(1, "no"), 1).state == "contested"  # no hit ever
    assert card(bet(0, "no") + bet(1, "partly") + bet(2, "no"), 2).state == "untested"
    cleared = card(never_reliable + bet(100, "no") + bet(140, "no") + bet(150, "yes"), 150)
    assert cleared.state == "reliable"  # the hit clears it (and is the second hit day)


def test_a_miss_soon_after_a_confirmation_contests_unsoftened():
    soon = card(RELIABLE + [weak(5)] + bet(15, "no"), 15)  # 10 days after
    assert soon.state == "contested" and soon.stability < 10  # Again, no slip
    reconfirm = ev(5, {"kind": "reconfirm", "claim": C, "verdict": "yes"})
    assert card(RELIABLE + [reconfirm] + bet(15, "no"), 15).state == "contested"
    late = card(RELIABLE + [weak(5)] + bet(25, "no"), 25)  # 20 days after: the slip rule
    assert late.state == "learning" and late.stability >= 10
    cleared = card(RELIABLE + [weak(5)] + bet(15, "no") + bet(20, "yes"), 20)
    assert cleared.state != "contested"


def test_tap_forecasts_still_feed_the_bet():
    f = ev(0, {"kind": "forecast", "claims": [C], "need": "n1", "stranger": "s",
               "same": False})
    b = card([f, ev(0, {"kind": "settle", "forecast": f["id"], "twin_hit": "yes"})], 0)
    assert (b.state, b.hits) == ("learning", 1)


def test_recheck_past_due():
    b = card(RELIABLE, 3)
    assert card(RELIABLE, 40).state == "recheck" and D(40) > b.due
    assert card(RELIABLE, 27).state == "reliable" and D(27) <= b.due
    assert card(bet(0, "yes"), 30).state == "recheck"


def test_calibration_bins_and_log_loss():
    evs = []
    for p, hit, n in ((0.9, "yes", 10), (0.1, "no", 9), (0.5, "partly", 1)):
        for _ in range(n):
            f = ev(0, {"kind": "forecast", "claims": [C], "predicted_p": p})
            evs += [f, ev(0, {"kind": "settle", "forecast": f["id"], "twin_hit": hit})]
    evs.append(ev(0, {"kind": "forecast", "claims": [C], "predicted_p": 0.9}))  # open
    f = ev(0, {"kind": "forecast", "predicted_p": 0.9})  # no claims
    evs += [f, ev(0, {"kind": "settle", "forecast": f["id"], "twin_hit": "no"})]
    out = bets.calibration(evs)
    assert (out["n"], out["enough"], out["log_loss"]) == (20, True, 0.135)
    assert [b["n"] for b in out["bins"]] == [9, 0, 1, 0, 10]
    assert out["bins"][4] == {"lo": 0.8, "hi": 1.0, "n": 10, "p": 0.9, "hit": 1.0}
    assert out["bins"][2]["hit"] == 0.5 and "p" not in out["bins"][1]
    assert bets.calibration(evs[:2])["enough"] is False
    assert bets.calibration([]) == {"n": 0, "enough": False, "log_loss": None,
                                    "bins": bets.calibration([])["bins"]}


@pytest.fixture
def vault(tmp_path):
    r = tmp_path / "vault"
    shutil.copytree(FIXTURES / "vault_mini", r)
    (r / "me/preferences.md").write_bytes(
        b"# Preferences\n\n## Food\n- Tea over coffee (confirmed 2026-01-01) ^c-aaaaa\n"
        b"- Spicy food ^c-bbbbb\n- Night work ^c-ccccc\n- Early runs ^c-ddddd\n\n"
        b"## Past\n- Old habit (until 2026-01-01) ^c-eeeee\n")
    (r / "me/goals.md").write_bytes(b"# Goals\n\n## Now\n- Ship it\n- Spicy food\n")
    return Vault(r)


def test_resolve_by_id_and_by_text(vault):
    assert bets.resolve(vault, "^c-aaaaa") == bets.resolve(vault, " c-aaaaa") == C
    assert bets.resolve(vault, "Tea over coffee") == C
    assert bets.resolve(vault, "- tea  over COFFEE (confirmed 2026-01-01)") == C
    ship = bets.resolve(vault, "Ship it")
    assert ship.startswith("c-") and ship in {r["id"] for r in bets.live(vault)}
    for bad in ("c-eeeee", "Old habit", "Spicy food", "nope", "", None, 5):  # Past, 2 pages
        with pytest.raises(VaultError) as e:
            bets.resolve(vault, bad)
        assert e.value.code == "invalid_argument"


def test_reflect_pack(vault):
    evs = (RELIABLE + bet(10, "no") + bet(15, "no")  # c-aaaaa contested
           + bet(0, "yes", claim="c-bbbbb") + bet(1, "yes", claim="c-ccccc")
           + bet(2, "yes", claim="c-ddddd")
           + [ev(5, {"kind": "reconfirm", "claim": "c-ccccc", "verdict": "no"}),
              ev(6, {"kind": "reconfirm", "claim": "c-ddddd", "verdict": "no"}),
              ev(7, {"kind": "reconfirm", "claim": "c-ddddd", "verdict": "yes"})])
    out = bets.pack(vault, evs, D(60))
    assert out["contested"] == [{"id": C, "page": "preferences",
                                 "text": "Tea over coffee (confirmed 2026-01-01)",
                                 "first_miss": D(10)}]
    assert [r["id"] for r in out["recheck"]] == ["c-bbbbb", "c-ccccc", "c-ddddd"]
    assert [r["id"] for r in out["retire"]] == ["c-ccccc"]  # the latest reconfirm wins
    # untested: the two goals and the fixture profile's line
    assert out["counts"] == {"untested": 3, "learning": 0, "reliable": 0, "recheck": 3,
                             "contested": 1}
    assert out["calibration"]["n"] == 0


def test_forecast_claims_reconfirm_until_and_cli(root):
    (root / "me/preferences.md").write_bytes(
        b"# Preferences\n\n## Food\n- Tea over coffee ^c-aaaaa\n- Night work ^c-bbbbb\n")
    today = local_day(datetime.now(UTC))
    fc = {"question": "Tea?", "area": "taste", "twin": "tea", "stranger": "coffee"}

    async def body(call):
        await call("cortex_start", {"mode": "probe"})
        no_same = await call("cortex_log", {"kind": "forecast", "payload": fc})
        assert no_same["error"] == "invalid_argument" and "same" in no_same["message"]
        for bad in (["nope"], [], ["c-aaaaa"] * 4, "c-aaaaa"):
            out = await call("cortex_log", {"kind": "forecast", "payload": {
                **fc, "same": False, "claims": bad}})
            assert out["error"] == "invalid_argument", bad
        fid = (await call("cortex_log", {"kind": "forecast", "payload": {
            **fc, "same": False, "claims": ["^c-aaaaa", "- Night work"],
            "predicted_p": 0.99}}))["id"]
        await call("cortex_log", {"kind": "settle", "payload": {
            "forecast": fid, "answer": "tea", "twin_hit": "yes", "stranger_hit": "no"}})
        await call("cortex_log", {"kind": "forecast", "payload": {
            **fc, "same": True, "claims": ["Tea over coffee"]}})
        for bad in ({"claim": "c-aaaaa", "verdict": "maybe"}, {"claim": "x", "verdict": "no"}):
            out = await call("cortex_log", {"kind": "reconfirm", "payload": bad})
            assert out["error"] == "invalid_argument", bad
        await call("cortex_log", {"kind": "reconfirm", "payload": {"claim": "Night work",
                                                                    "verdict": "no"}})
        pack = (await call("cortex_reflect"))["bets"]
        assert [r["id"] for r in pack["retire"]] == ["c-bbbbb"]
        assert pack["retire"][0]["text"].startswith('<untrusted_note id="claim">')
        assert pack["counts"]["learning"] == 2
        tomorrow = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
        for args in ({"op": "retire", "until": tomorrow}, {"op": "retire", "until": "2026-1-5"},
                     {"op": "append_to_section", "until": "2026-01-05", "text": "x"}):
            out = await call("cortex_patch", {"id": "me/preferences", "target": "c-bbbbb",
                                              **args})
            assert out["error"] == "invalid_argument", args
        await call("cortex_patch", {"id": "me/preferences", "op": "retire",
                                    "target": "c-bbbbb", "until": "2026-01-05"})
        await call("cortex_patch", {"id": "me/preferences", "op": "revise", "target": "c-aaaaa",
                                    "text": "Green tea", "user_confirmed": True,
                                    "until": "2026-01-06"})

    run(root, body)
    fcs = [e for e in events(root) if e["kind"] == "log" and e["payload"]["kind"] == "forecast"]
    assert fcs[0]["payload"]["claims"] == ["c-aaaaa", "c-bbbbb"] and "predicted_p" not in fcs[0]
    assert fcs[0]["payload"]["predicted_p"] == 0.5 and fcs[0]["payload"]["same"] is False
    assert fcs[1]["payload"]["predicted_p"] > 0.9  # a card reviewed moments ago
    prefs = (root / "me/preferences.md").read_text(encoding="utf-8")
    assert "- Night work (until 2026-01-05) ^c-bbbbb" in prefs
    assert "- Tea over coffee (until 2026-01-06 → ^c-" in prefs
    out = CliRunner().invoke(app, ["claims", "--vault", str(root)])
    assert out.exit_code == 0, out.output
    line = next(ln for ln in out.output.splitlines() if "Green tea" in ln)
    assert "◌ untested" in line and "0/0/0" in line  # the revised line is a new era


def test_an_earlier_hour_the_next_day_is_still_a_day_later():
    # board L1: settles at 10:00, 09:00, 08:00 on consecutive days are a day apart, not same-day
    noon = bet(0, "yes") + bet(1, "yes") + bet(2, "yes")
    earlier = [{**e, "ts": e["ts"].replace("T12:", f"T{h:02}:")}
               for e, h in zip(noon, (10, 10, 9, 9, 8, 8), strict=True)]
    assert card(earlier, 2).stability == card(noon, 2).stability
