"""R2: Unicode words in the tidy (R30c), a line that keeps coming back (R13b: `reinforce`, its
fold, stale and prime's tie-break) and the monthly "Pin it to Core?" card (R28a)."""

import datetime as dt
import unicodedata

import pytest
from conftest import events
from test_review import ago, app, changes, live, today  # noqa: F401  (app is a fixture)

from cortex import claims, prime, review, xray
from cortex.app import server
from cortex.events import EventLog
from cortex.mcp_server import Cortex
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

HINDI = "मैं दिल्ली में रहता हूँ"


# ---- R30c: Unicode words ----

def test_words_keep_accents_hindi_and_cjk():
    assert review._words("Lives in München") == ["lives", "in", "münchen"]
    assert review._words(HINDI) == ["मैं", "दिल्ली", "में", "रहता", "हूँ"]  # matras stay
    assert review._words("東京に住んでいます。") == ["東京に住んでいます"]
    assert review._words("snake_case don't") == ["snake", "case", "don't"]  # as before
    nfd = unicodedata.normalize("NFD", "Lives in München")
    assert review._words(nfd) == review._words("Lives in München")
    assert not review.same_facts("Lives in München", "Lives in Munich")
    assert not review.same_facts("Lives in München", "Lives in Mönchen")  # was "m nchen" twice
    assert not review.same_facts(HINDI, HINDI.replace("दिल्ली", "मुंबई"))  # was two empty bags
    assert not review.same_facts("東京に住んでいます", "大阪に住んでいます")
    assert review.same_facts(HINDI, f"  {HINDI} ")
    assert review.negated("Doesn't like café au lait") and not review.negated("Likes café")
    assert review._facts("Runs 5 किलोमीटर")[4] == (("5", "किलोमीटर"),)  # the unit is a word
    assert review._swapped("Prefers chá over café") == (("chá", "over", "café"),)


def test_non_english_copies_still_merge_and_others_stay(root):
    (root / ".cortex").mkdir(exist_ok=True)
    (root / "me" / "profile.md").write_text(
        f"# Profile\n\n## About\n- {HINDI}\n- {HINDI} \n- {HINDI.replace('दिल्ली', 'मुंबई')}\n"
        "- Lives in München\n- Lives in Mönchen\n", encoding="utf-8")
    assert Cortex(Vault(root)).tidy()["retired"] == 1  # only the exact Hindi copy
    assert len([r for r in claims.rows(Vault(root)) if r["status"] == "live"]) == 4


# ---- R13b: reinforce ----

def patch(root, client, text, confirmed=False):
    c = Cortex(Vault(root))
    c.sessions.client = client  # as the MCP handshake sets it
    try:
        return c._call("cortex_patch", c.cortex_patch, {
            "id": "me/preferences", "op": "append_to_section", "target": "Coding",
            "text": text, "user_confirmed": confirmed})
    finally:
        c.sessions.close()


def test_a_line_said_again_adds_up_across_sessions_and_apps(root, app):  # noqa: F811
    first = patch(root, "claude-code", "Drinks green tea at night")
    assert first["changed"] and review.reinforced(events(root)) == {}  # said once: nothing
    second = patch(root, "cursor", "Drinks green tea at night")
    assert [r["id"] for r in second["replaced"]] == [first["claim"]]  # the copy was retired
    third = patch(root, "claude-code", "Drinks green tea at night")
    assert review.reinforced(events(root)) == {}  # an update retire needs the two texts
    got = review.reinforced(events(root), review.known_texts(Vault(root)))
    assert set(got) == {third["claim"]}  # the kept line took over its copies' sightings
    x = got[third["claim"]]
    assert (len(x["sessions"]), x["apps"], x["last"]) == (3, {"claude-code", "cursor"}, today())


def test_a_swapped_comparative_is_a_change_of_mind_not_a_repeat(root, app):  # noqa: F811
    patch(root, "claude-code", "Prefers Rust over Go")
    out = patch(root, "cursor", "Prefers Go over Rust")
    assert out["replaced"]  # Cortex settled the update itself
    assert review.reinforced(events(root), review.known_texts(Vault(root))) == {}


def test_reinforce_event_shape_and_fold(root):
    (root / ".cortex").mkdir(exist_ok=True)
    (root / "me" / "preferences.md").write_text("# Preferences\n\n## Coding\n- Uses Vim\n",
                                                encoding="utf-8")
    (cid,) = [r["id"] for r in claims.rows(Vault(root)) if r["page"] == "preferences"]
    log = EventLog(Vault(root))
    review.reinforce(log, "cli", "preferences", cid, "cursor")
    (e,) = [e for e in events(root) if e["payload"].get("note") == review.REINFORCE]
    assert e["kind"] == "change" and e["session"] == "cli" and e["day"] == today()
    assert e["payload"] == {"note": "reinforce", "page": "preferences", "claim": cid,
                            "client": "cursor"}
    assert review.reinforced(events(root)) == {
        cid: {"sessions": {"cli"}, "apps": {"cursor"}, "last": today()}}
    review.reinforce(log, "not a session", "preferences", cid)  # refused by the log: no raise
    assert len(review.reinforced(events(root))[cid]["sessions"]) == 1


def test_a_line_that_came_back_is_not_stale(root):
    (root / ".cortex").mkdir(exist_ok=True)
    (root / "me" / "preferences.md").write_text(
        f"# Preferences\n\n## Coding\n- Old habit (confirmed {ago(300)})\n"
        f"- Other old habit (confirmed {ago(300)})\n", encoding="utf-8")
    v = Vault(root)
    k = {r["text"].split(" (")[0]: r["id"] for r in claims.rows(v)}
    rows = review.live_rows(v, events(root), today())
    assert len(review.stale(rows, events(root), today())) == 2
    review.reinforce(EventLog(v), "cli", "preferences", k["Old habit"])
    (x,) = review.stale(rows, events(root), today())
    assert x["id"] == k["Other old habit"]


def test_prime_ranks_a_line_that_came_back_first_among_equals(root):
    (root / ".cortex").mkdir(exist_ok=True)
    (root / "me" / "preferences.md").write_text(
        "# Preferences\n\n## Coding\n- Uses Vim\n- Uses tabs\n- Uses dark mode\n",
        encoding="utf-8")
    v = Vault(root)
    k = {r["text"]: r["id"] for r in claims.rows(v)}
    now = dt.datetime.now(dt.UTC)

    def order():
        parts, _ = prime.core(v, "hello", events(root), now)
        block = next(p for p in parts if p.startswith("## Preferences"))
        return [ln for ln in block.splitlines() if ln.startswith("- [")]

    before = order()
    assert [ln.split("] ")[1] for ln in before] == ["Uses dark mode", "Uses tabs", "Uses Vim"]
    review.reinforce(EventLog(v), "cli", "preferences", k["Uses Vim"])
    after = order()
    assert after == [before[2], *before[:2]] and order() == after  # stable from call to call
    rows = prime.pref_rows(v, "", {}, today(), [], back={xray.claim(k["Uses tabs"]): "2026-01-01",
                                                         xray.claim(k["Uses Vim"]): "2026-02-01"})
    assert [r[6] for r in rows] == [xray.claim(k[t]) for t in
                                    ("Uses Vim", "Uses tabs", "Uses dark mode")]


# ---- R28a: promote to Core ----

def prefs(root, lines):
    (root / "me" / "preferences.md").write_text(
        "# Preferences\n\n## Coding\n" + "".join(f"- {ln}\n" for ln in lines), encoding="utf-8")
    return {r["text"].split(" (")[0]: r["id"] for r in claims.rows(Vault(root))
            if r["page"] == "preferences"}


def back(root, cid, n, apps=("claude-code", "cursor")):
    log = EventLog(Vault(root))
    for i in range(n):
        log.append(session=f"s_{'0' * 25}{'ABCDEFGH'[i]}", kind="change", source="tutor",
                   fields={"payload": {"note": "reinforce", "page": "preferences", "claim": cid,
                                       "client": apps[i % len(apps)]}})


def cards(root, day=None):
    v = Vault(root)
    return review.view(v, EventLog(v).iter_events(), day or today())["promote"]


def test_promote_needs_four_proofs_from_two_apps(root, app):  # noqa: F811
    k = prefs(root, ["Uses Vim", "Uses tabs", "Writes tests first"])
    back(root, k["Uses Vim"], 4, apps=("cursor",))  # one app only
    back(root, k["Uses tabs"], 3)  # three sessions only
    assert cards(root) == []
    back(root, k["Writes tests first"], 4)
    (x,) = cards(root)
    assert (x["id"], x["text"], x["proofs"], x["apps"]) == (
        k["Writes tests first"], "Writes tests first", 4, 2)
    assert app.review()["more"] >= 1


def test_promote_by_the_xray_top_five(root, app):  # noqa: F811
    k = prefs(root, [f"Line {n}" for n in range(7)])
    order = [xray.claim(k[f"Line {n}"]) for n in range(7)]
    v = Vault(root)
    for _ in range(9):
        xray.record(v, "cortex_prime", "claude-code", {"preferences": order}, {}, 100)
    xray.record(v, "cortex_context", "claude-code", {"preferences": order}, {}, 100)
    assert cards(root) == []  # 9 primes (a context call is not one)
    xray.record(v, "prime-hook", "claude-code", {"preferences": order[5:] + order[:5]}, {}, 100)
    (x,) = cards(root)  # lines 0-2 were in all 10 top fives; one card a month, the first by id
    assert x["served"] == 10 and x["id"] == min(k[f"Line {n}"] for n in range(3))


def test_pin_to_core_then_none_this_month(root, app):  # noqa: F811
    k = prefs(root, ["Writes tests first", "Uses tabs"])
    back(root, k["Writes tests first"], 4)
    back(root, k["Uses tabs"], 4)
    (x,) = app.review()["promote"]
    view = app.review_promote_pin({"id": x["id"]})
    assert view["promote"] == []  # one a month: the other line waits for next month
    core = (root / "me" / "core.md").read_text("utf-8")
    assert f"- {x['text']}" in core.split("## Core")[1]
    # shortcut of cortex_patch's own path: the preferences copy is settled as the older line
    assert x["text"] in (root / "me" / "preferences.md").read_text("utf-8").split("## Past")[1]
    (c,) = changes(root, "core")
    assert (c["op"], c["card"], c["confirm"]) == ("append_to_section", "promote_core", "clicked")
    nxt = (dt.date.fromisoformat(today()).replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    (y,) = cards(root, nxt.isoformat())
    assert y["id"] != x["id"]  # the pinned line is on Core now
    with pytest.raises(server.NotFound):
        app.review_promote_pin({"id": y["id"]})  # not offered this month


def test_not_now_keeps_a_line_off_for_six_months(root, app):  # noqa: F811
    k = prefs(root, ["Writes tests first"])
    back(root, k["Writes tests first"], 4)
    (x,) = app.review()["promote"]
    assert app.review_promote_later({"id": x["id"]})["promote"] == []
    (c,) = changes(root, "promote_core")
    assert (c["tool"], c["card"], c["hash"]) == ("dismiss", "promote_core",
                                                review.text_hash(x["id"]))
    assert "Writes tests first" in (root / "me" / "preferences.md").read_text("utf-8")
    day = dt.date.fromisoformat(today())
    assert cards(root, (day + dt.timedelta(days=150)).isoformat()) == []
    assert cards(root, (day + dt.timedelta(days=review.PROMOTE_QUIET + 1)).isoformat())


def test_promote_skips_core_inferred_private_and_unknown(root, app):  # noqa: F811
    k = prefs(root, ["Already core", "Muted line"])
    (root / "me" / "core.md").write_text("# Core\n\n## Core\n- Already core\n", encoding="utf-8")
    back(root, k["Already core"], 4)
    back(root, k["Muted line"], 4)
    xray.set_flag(Vault(root), "mute", xray.claim(k["Muted line"]), True)
    guess = patch(root, "cursor", "Drinks green tea at night")  # inferred: tier agent
    back(root, guess["claim"], 4)
    assert cards(root) == []
    for bad in ({"id": k["Already core"]}, {"id": "c-zzzzz"}, {}):
        with pytest.raises(server.NotFound):
            app.review_promote_later(bad)
