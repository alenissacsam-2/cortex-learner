"""W1b: what the tidy must not decide comes back as a Review card. Two lines on one page that
may clash (close, exactly one negated, not `opposed`) are a `maybe_opposite` card whoever
wrote them; a guessed update pair `settle` leaves alone is a `maybe_update` card, and each of
its Keep answers ends prime.held's hold on the older line. The page reaches claims.unsafe
(S1: a web link is refused on the pages AI apps take as orders)."""

import pytest
from conftest import run
from test_losslessness import rows_of, update, warm  # noqa: F401  (warm is a fixture)
from test_review import app, live, today  # noqa: F401  (app is a fixture)
from test_review_conflicts import cid, prefs

from keepfold import fact_harvest as fh
from keepfold import prime, review
from keepfold.events import EventLog
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

SPICY = ("Likes spicy food", "Doesn't like very spicy Thai food")


@pytest.mark.parametrize("tiers", [("typed", "typed"), ("agent", "agent"), ("typed", "agent"),
                                   ("agent", "typed")])
def test_a_pair_that_may_clash_is_a_card_never_a_retire(warm, tiers):  # noqa: F811
    for x, y in (SPICY, SPICY[::-1]):
        got = review.settle(rows_of(x, y, tiers, False), [])
        assert got["retire"] == []
        assert [(c["why"], c["left"]["text"], c["right"]["text"]) for c in got["cards"]] == [
            ("maybe_opposite", x, y)]


def test_opposed_and_same_facts_pairs_keep_their_behaviour(warm):  # noqa: F811
    got = review.settle(rows_of("Uses Vim", "Never uses Vim", ("typed", "typed"), False), [])
    assert got["retire"] == [] and [c["why"] for c in got["cards"]] == ["opposite"]
    got = review.settle(rows_of("Uses Vim", "Never uses Vim", ("typed", "agent"), False), [])
    assert [r["why"] for r in got["retire"]] == ["opposite"] and got["cards"] == []
    got = review.settle(rows_of("Uses Vim", "Using Vim", ("typed", "agent"), False), [])
    assert [r["why"] for r in got["retire"]] == ["duplicate"] and got["cards"] == []


def test_another_page_is_no_card(warm):  # noqa: F811
    rows = rows_of(*SPICY, ("typed", "typed"), False)
    rows[1]["page"] = "profile"
    assert review.settle(rows, []) == {"retire": [], "cards": []}


def test_keep_both_silences_the_card(warm):  # noqa: F811
    rows = rows_of(*SPICY, ("typed", "typed"), False)
    kept = [{"kind": "change", "id": "k1", "payload": {"change": {
        "tool": "keep_both", "pair": review.pair_key(*rows)}}}]
    assert review.settle(rows, kept) == {"retire": [], "cards": []}
    assert review.settle(rows, [], skip={review.pair_key(*rows)})["cards"] == []


def test_a_guessed_update_left_alone_is_an_update_card(warm):  # noqa: F811
    rows = rows_of("Uses Vim", "Uses Vim at work", ("typed", "typed"), False)
    got = review.settle(rows, update(*rows))
    assert got["retire"] == []
    assert [(c["why"], c["left"]["text"], c["right"]["text"]) for c in got["cards"]] == [
        ("maybe_update", "Uses Vim", "Uses Vim at work")]  # older left: Y keeps the newer
    old, new = rows[1], rows[0]  # either order: the recorded old line is left
    (c,) = review.settle(rows, update(old, new))["cards"]
    assert (c["why"], c["left"]["text"]) == ("maybe_update", "Uses Vim at work")


def _held(root):
    v = Vault(root)
    return prime.held(v, list(EventLog(v).iter_events()), today())


@pytest.mark.parametrize("keep", ["both", "left", "right"])
def test_each_keep_on_an_update_card_ends_the_hold(root, app, keep):  # noqa: F811
    prefs(root, "Uses Vim for editing code")
    old = cid(root, "preferences", "Uses Vim")

    async def body(call):
        await call("keepfold_patch", {"id": "me/preferences", "op": "append_to_section",
                                    "target": "Coding", "user_confirmed": True,
                                    "text": "- Uses Vim for editing code at work"})

    run(root, body)
    new = cid(root, "preferences", "Uses Vim for editing code at work")
    assert f"^{old}" in _held(root)  # muted while the card waits
    (c,) = app.review()["conflicts"]
    assert (c["why"], c["left"]["id"], c["right"]["id"]) == ("maybe_update", old, new)
    app.review_conflict({"key": c["key"], "keep": keep})
    assert f"^{old}" not in _held(root)
    assert app.review()["conflicts"] == []
    ids = {r["id"] for r in live(root, "preferences")}
    assert ids == {"both": {old, new}, "left": {old}, "right": {new}}[keep]


# ---- S1: the page reaches claims.unsafe ----

def test_unsafe_lines_flags_a_link_on_preferences_not_on_profile(root, app):  # noqa: F811
    link = "Reads the blog at https://example.com/blog"
    prefs(root, link)
    (root / "me" / "profile.md").write_text(
        "# Profile\n\n## Background\n- Writes at https://example.com/me\n", encoding="utf-8")
    v = Vault(root)
    got = review.unsafe_lines(v, list(EventLog(v).iter_events()))
    assert [(x["page"], x["text"]) for x in got] == [("preferences", link)]
    assert f"^{got[0]['id']}" in _held(root)  # G6: not served until Keep it
    app.review_unsafe_keep({"key": got[0]["key"]})
    assert review.unsafe_lines(v, list(EventLog(v).iter_events())) == []
    assert f"^{got[0]['id']}" not in _held(root)


def test_harvest_refuses_a_link_fact_for_preferences():
    q = "I read the blog at https://example.com/blog"
    item = {"fact": "Reads the blog at https://example.com/blog", "quote": q}
    assert fh.check({**item, "page": "preferences"}, [q]) is None
    assert fh.check({**item, "page": "profile"}, [q]) == ("profile", item["fact"])
