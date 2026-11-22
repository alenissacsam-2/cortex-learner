"""W1: the automatic tidy never loses a fact. `review.settle` retires a line by itself only when
the kept line says the same facts (`review.same_facts`: equal bags, polarity, tense, numbers,
the `(until …)` stamp, comparative order), or the two say opposite things (`review.opposed`)
and the owner's beats an inferred one. Pairs only a model could merge stay two lines. Both
scorer paths: word overlap, and a warm embedder that scores every pair 1.0 (the worst case).
fact_harvest's quote must back its fact: same polarity and tense, half its content words."""

import itertools

import numpy as np
import pytest

from cortex import fact_harvest as fh
from cortex import review, semantic

# the lead's table and the outside review's Q1 (pairs only): each side holds a fact the other
# does not, or a model would be needed to know they agree: never one line
LOSSY = [
    ("Allergic to peanuts and shellfish", "Allergic to peanuts"),
    ("Uses Vim at work", "Uses Vim"),
    ("Lives in Pune", "Lived in Pune"),
    ("Preparing for GATE exam (until 2027-02-15)", "Preparing for GATE exam"),
    ("Preparing for GATE exam (until 2027-02-15)", "Preparing for GATE exam (until 2027-05-01)"),
    ("Reads news every morning", "Reads papers every morning"),
    ("Uses Python", "Uses Rust"),
    ("Prefers concise answers for code reviews", "Prefers concise answers"),
    ("Drinks coffee only in the morning", "Drinks coffee"),
    ("Works 4 days a week remotely", "Works remotely"),
    ("Works at Acme", "Worked at Acme"),
    ("Used to live in Pune", "Lives in Pune"),
    ("Wants to learn Rust", "Knows Rust"),
    ("Always prefers concise code diffs", "Prefers concise code diffs"),
    ("Runs 5 km every morning", "Runs 10 km every morning"),
    ("Has 16 GB RAM", "Has 32 GB RAM"),
    ("Runs 5 km and 10 miles", "Runs 10 km and 5 miles"),
    ("Uses tabs", "Refuses tabs"),
    ("Is an ex-smoker", "Is a smoker"),
    ("Based in Pune, uses Vim", "Based in Pune, used Vim"),
    ("Reads before bed", "Before bed, reads"),
    # only a model knows these agree: Stage 1 keeps both
    ("Prefers brief replies", "Prefers short answers"),
    ("Lives in Bangalore", "Lives in Bengaluru"),
    ("Prefers dark mode", "Likes dark mode"),
    ("Uses JavaScript for frontend", "Uses JS for frontend"),
]
OPPOSED = [
    ("Prefers Rust over Go", "Prefers Go over Rust"),
    ("Prefers tea rather than coffee", "Prefers coffee rather than tea"),
    ("Uses Vim", "Doesn't use Vim"),
    ("Uses Vim", "Never uses Vim"),
    ("Uses Vim", "Stopped using Vim"),
    ("Uses Vim", "Quit using Vim"),
    ("Uses Pipenv", "No longer uses Pipenv"),
    ("Drinks coffee", "Gave up drinking coffee"),
    ("Likes async Python", "Dislikes async Python"),
]
SAME = [
    ("Uses Vim", "Using Vim"),
    ("Uses Vim", "uses vim"),
    ("Studies algorithms daily", "Studying algorithm daily"),
    ("Uses Vim for editing code", "Uses Vim for code editing"),
    ("Preparing for GATE exam (until 2027-02-15)",
     "Preparing for the GATE exam (until 2027-02-15)"),
    ("Walks 5 km every morning", "Walking 5 km every morning"),
    ("Prefers Rust over Go", "Prefers Rust over Go."),
]
MIXES = list(itertools.product(("typed", "agent"), repeat=2))  # owner/inferred on each side


@pytest.fixture(params=[False, True], ids=["words", "warm"])
def warm(request, monkeypatch):
    if request.param:  # the worst embedder: every pair scores 1.0
        monkeypatch.setattr(semantic, "_embedder", lambda t: np.ones((len(t), 4), np.float32))
    return request.param


def row(text, tier, n, day):
    return {"page": "preferences", "id": f"c-{'ab'[n] * 5}", "text": text, "tier": tier,
            "day": day, "line_no": n, "section": "Coding"}


def rows_of(a, b, tiers, newer_first):
    days = ("2026-11-21", "2026-11-20") if newer_first else ("2026-11-20", "2026-11-21")
    return [row(a, tiers[0], 0, days[0]), row(b, tiers[1], 1, days[1])]


def update(old, new):  # a guessed `update_of` pair cortex_patch recorded
    return [{"kind": "change", "id": "e1", "payload": {"change": {
        "id": "preferences", "claim": new["id"],
        "update_of": {"page": "preferences", "claim": old["id"]}}}}]


# ---- the predicates ----

@pytest.mark.parametrize("a,b", LOSSY)
def test_lossy_pairs_are_never_the_same_facts_nor_opposed(a, b):
    assert not review.same_facts(a, b) and not review.same_facts(b, a)
    assert not review.opposed(a, b) and not review.opposed(b, a)


@pytest.mark.parametrize("a,b", OPPOSED)
def test_opposites_are_seen(a, b):
    assert review.opposed(a, b) and review.opposed(b, a)
    assert not review.same_facts(a, b)


@pytest.mark.parametrize("a,b", SAME)
def test_the_same_facts_in_other_words(a, b):
    assert review.same_facts(a, b) and review.same_facts(b, a)
    assert not review.opposed(a, b)


def test_the_stemmer_and_the_negation_words():
    assert {review._stem(w) for w in ("use", "uses", "using", "used")} == {"us"}
    assert {review._stem(w) for w in ("study", "studies", "studying", "studied")} == {"study"}
    assert review._stem("need") == review._stem("needs")  # -eed is not a past ending
    for t in ("Stopped using Vim", "Quits sugar", "Refused the offer", "No longer uses Vim",
              "Gave up coffee", "Has given up coffee"):
        assert review.negated(t), t
    assert not review.negated("Uses Vim") and not review.negated("Gives updates weekly")
    assert review._bag("No longer uses Vim") == review._bag("Uses Vim")
    # apart keeps its meaning: each names something the other does not
    assert review.apart("Uses Python", "Uses Rust")
    assert not review.apart("Uses Vim at work", "Uses Vim")


# ---- through settle: no line holding an extra fact is ever retired ----

@pytest.mark.parametrize("a,b", LOSSY)
@pytest.mark.parametrize("tiers", MIXES)
@pytest.mark.parametrize("newer_first", [False, True])
def test_settle_never_retires_a_lossy_pair(warm, a, b, tiers, newer_first):
    for x, y in ((a, b), (b, a)):
        rows = rows_of(x, y, tiers, newer_first)
        got = review.settle(rows, [])
        # W1b: a pair where only one says no ("Uses tabs" / "Refuses tabs") may clash: a card
        assert got["retire"] == [] and {c["why"] for c in got["cards"]} <= {"maybe_opposite"}
        for old, new in (rows, rows[::-1]):  # a guessed update either way is left alone
            assert review.settle(rows, update(old, new))["retire"] == [], (x, y)


@pytest.mark.parametrize("a,b", OPPOSED)
@pytest.mark.parametrize("newer_first", [False, True])
def test_settle_opposites_owner_beats_inferred_else_a_card(warm, a, b, newer_first):
    for x, y in ((a, b), (b, a)):
        rows = rows_of(x, y, ("typed", "agent"), newer_first)
        got = review.settle(rows, [])
        assert [(r["retire"]["text"], r["why"]) for r in got["retire"]] == [(y, "opposite")]
        assert got["cards"] == []
        for tiers in (("typed", "typed"), ("agent", "agent")):
            got = review.settle(rows_of(x, y, tiers, newer_first), [])
            assert got["retire"] == [] and [{c["left"]["text"], c["right"]["text"]}
                                            for c in got["cards"]] == [{x, y}]


@pytest.mark.parametrize("a,b", SAME)
def test_settle_still_retires_the_same_facts(warm, a, b):
    rows = rows_of(a, b, ("typed", "agent"), False)
    got = review.settle(rows, [])
    assert [(r["retire"]["text"], r["why"]) for r in got["retire"]] == [(b, "duplicate")]
    old, new = rows[1], rows[0]  # a guessed update between the same facts is applied
    got = review.settle(rows, update(old, new))
    assert [r["retire"]["text"] for r in got["retire"]] == [b]


# ---- every other one-click grouping ----

def test_copies_and_collapse_never_group_a_line_holding_another_fact(warm):
    lines = ["Allergic to peanuts", "Allergic to peanuts and shellfish", "Lived in Pune",
             "Lives in Pune", "Using Vim", "Uses Vim", "Uses Vim at work"]
    rows = [row(t, "typed", 0, None) | {"id": f"c-{n}"} for n, t in enumerate(lines)]
    assert [x["text"] for x in review.copies(rows, rows[0])] == ["Allergic to peanuts"]
    assert [x["text"] for x in review.copies(rows, rows[2])] == ["Lived in Pune"]
    assert [x["text"] for x in review.copies(rows, rows[4])] == ["Using Vim", "Uses Vim"]
    got = review.collapse([{"key": t, "text": t} for t in lines])
    assert [[m["text"] for m in g["members"]] for g in got] == [
        ["Allergic to peanuts"], ["Allergic to peanuts and shellfish"], ["Lived in Pune"],
        ["Lives in Pune"], ["Using Vim", "Uses Vim"], ["Uses Vim at work"]]
    assert review.group([{"key": t, "text": t} for t in lines], "Uses Vim") == [
        "Uses Vim", "Using Vim"]


def test_keep_both_covers_only_lines_saying_the_same_facts(warm):
    known = {("preferences", "k1"): "Uses Vim", ("preferences", "k2"): "Doesn't use Vim"}
    ev = [{"kind": "change", "id": "e1", "payload": {"change": {
        "tool": "keep_both", "pair": "preferences/k1|preferences/k2"}}}]
    same = [row("Using Vim", "typed", 0, None), row("Don't use Vim", "typed", 1, None)]
    more = [row("Uses Vim at work", "typed", 0, None), row("Doesn't use Vim", "typed", 1, None)]
    assert review.kept_cover(same, ev, known)[0](0, 1)
    assert not review.kept_cover(more, ev, known)[0](0, 1)


# ---- fact_harvest: the quote must back the fact ----

@pytest.mark.parametrize("fact,quote", [
    ("Likes Python", "I don't like Python"),  # a negated quote
    ("Uses Vim", "I used to use Vim"),  # it ended
    ("Works at Acme", "I worked at Acme"),  # past tense
    ("Worked at Acme", "I work at Acme"),
    ("Works on the payments team at Acme in Pune", "I'm on the payments team"),  # low overlap
    ("Uses Vim", "I stopped using Vim"),
])
def test_harvest_drops_a_fact_its_quote_does_not_back(fact, quote):
    assert fh.check({"page": "preferences", "fact": fact, "quote": quote}, [quote]) is None


@pytest.mark.parametrize("fact,quote", [
    ("Lives in Pune", "I moved to Pune last month"),
    ("Works on the payments team", "I'm on the payments team"),
    ("Uses Neovim", "I use Neovim"),
    ("Used to live in Delhi", "I used to live in Delhi"),
    ("Doesn't like Java", "I don't like Java"),
])
def test_harvest_keeps_a_fact_its_quote_backs(fact, quote):
    assert fh.check({"page": "preferences", "fact": fact, "quote": quote}, [quote]) == (
        "preferences", fact)
