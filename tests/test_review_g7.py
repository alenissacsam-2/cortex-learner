"""G7: Review cards for lines that say nearly the same (Keep both / left / right), lines the
X-ray shows cut for space in most primes (Keep pins, Retire, Skip) and playbooks no AI app was
served lately (Keep for 60 days, Open); crowded and unused share 3 cards a month, near-duplicates
show 3 at a time, and a private page never shows."""

import datetime as dt
import os
import time

import pytest
from conftest import events, note
from test_review import app, changes, today  # noqa: F401  (app is a fixture)

from cortex import claims, review, xray
from cortex.app import server
from cortex.events import EventLog
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")


def page(root, name, lines, private=False):
    front = "---\nprivate: true\n---\n" if private else ""
    (root / "me" / f"{name}.md").write_text(
        f"{front}# {name.title()}\n\n## Coding\n" + "".join(f"- {ln}\n" for ln in lines),
        encoding="utf-8")


def ids(root, name):
    return {r["text"]: r["id"] for r in claims.rows(Vault(root)) if r["page"] == name}


def primes(root, rows, tool="cortex_prime"):
    """X-ray rows: each a (served claim ids, trimmed claim ids)."""
    v = Vault(root)
    for s, t in rows:
        xray.record(v, tool, "claude-code", {"preferences": [xray.claim(i) for i in s]},
                    {"preferences": [xray.claim(i) for i in t]}, 100)


def book(root, id, days_old, private=False):
    p = note(root, f"notes/{id}.md", f"title: Book {id}\nkind: note\ntags: [playbook]\n"
             + ("private: true\n" if private else ""), "# Steps\n- One.")
    t = time.time() - days_old * 86400
    os.utime(p, (t, t))
    return p


def cards(root, events_=None, day=None):
    v = Vault(root)
    return review.view(v, EventLog(v).iter_events() if events_ is None else events_,
                       day or today())


# ---- near-duplicates ----

def test_near_duplicates_on_one_page_three_at_a_time(root, app):  # noqa: F811
    page(root, "preferences", [
        "Uses Vim for editing code", "Uses Vim for code editing",  # a pair
        "Drinks green tea at night", "Drinks green tea at nights",
        "Writes Rust on weekends", "Writes Rust on the weekends",
        "Reads papers on trains", "Reads papers on the trains",  # a fourth pair: not shown yet
        "Never uses tabs for indentation", "Uses tabs for indentation"])  # a conflict, not a dup
    page(root, "profile", ["Uses Vim for editing code daily"])  # another page: never paired
    view = cards(root)
    dups = view["duplicates"]
    assert len(dups) == review.DUP_SHOWN == 3
    assert all(d["why"] == "same" and d["left"]["page"] == d["right"]["page"] == "preferences"
               for d in dups)
    assert [d["left"]["text"] for d in dups] == ["Uses Vim for editing code",
                                                 "Drinks green tea at night",
                                                 "Writes Rust on weekends"]
    assert [c["why"] for c in view["conflicts"]] == ["opposite"]  # the tabs pair stays there
    # Keep both: never shown again for that pair; the fourth pair takes its place
    view = app.review_conflict({"key": dups[0]["key"], "keep": "both"})
    assert dups[0]["key"] not in [d["key"] for d in view["duplicates"]]
    assert view["duplicates"][-1]["left"]["text"] == "Reads papers on trains"
    *_, c = changes(root, "preferences")
    assert (c["op"], c["pair"], c["card"]) == ("keep_both", dups[0]["key"], "duplicate")
    # Keep left retires the right line through the retire route
    d = view["duplicates"][0]
    view = app.review_conflict({"key": d["key"], "keep": "left"})
    text = (root / "me" / "preferences.md").read_text("utf-8")
    assert d["right"]["text"] in text.split("## Past")[1]
    assert d["left"]["text"] in text.split("## Past")[0]
    *_, c = changes(root, "preferences")
    assert (c["op"], c["target"], c["card"]) == ("retire", d["right"]["id"], "duplicate")
    assert d["key"] not in [x["key"] for x in view["duplicates"]]


def test_near_duplicates_never_on_a_private_page(root, app):  # noqa: F811
    page(root, "preferences", ["Uses Vim for editing code", "Uses Vim for code editing"],
         private=True)
    assert cards(root)["duplicates"] == []
    with pytest.raises(server.NotFound):
        app.review_conflict({"key": review.pair_key(
            {"page": "preferences", "id": "c-aaaaa"}, {"page": "preferences", "id": "c-bbbbb"}),
            "keep": "both"})


# ---- crowded out ----

def test_a_line_cut_in_most_primes_is_offered_and_keep_pins_it(root, app):  # noqa: F811
    page(root, "preferences", ["Crowded line", "Served line", "Few primes line", "Cut sometimes"])
    k = ids(root, "preferences")
    a, b, c, d = (k[t] for t in ("Crowded line", "Served line", "Few primes line",
                                 "Cut sometimes"))
    primes(root, [([b], [a, d])] * 3 + [([b, d], [a])] * 2 + [([a, b, d], [])])
    primes(root, [([], [c])] * 4)  # cut every time, but offered only 4 times: too few to judge
    primes(root, [([], [b])] * 9, tool="cortex_context")  # not a prime: not counted
    view = cards(root)
    assert [(x["id"], x["cut"], x["seen"], x["window"]) for x in view["crowded"]] == [
        (a, 5, 6, 10)]  # d was cut in 3 of 6: not most
    assert view["crowded"][0]["text"] == "Crowded line" and view["count"] == 1
    view = app.review_crowded_keep({"id": a})
    assert xray.settings(Vault(root))["pin"] == {xray.claim(a)}
    assert view["crowded"] == []  # pinned: prime serves it first from now on
    (c_,) = changes(root, "crowded")
    assert (c_["tool"], c_["card"], c_["hash"]) == ("dismiss", "crowded", review.text_hash(a))
    with pytest.raises(server.NotFound):
        app.review_crowded_keep({"id": b})  # never crowded


def test_crowded_window_is_the_latest_40_primes(root):
    page(root, "preferences", ["Once crowded", "Other"])
    k = ids(root, "preferences")
    primes(root, [([], [k["Once crowded"]])] * 10 + [([k["Once crowded"]], [])] * 40)
    assert cards(root)["crowded"] == []  # the 10 cuts fell out of the window


def test_retire_on_a_crowded_card_counts_and_the_cap_is_shared(root, app):  # noqa: F811
    page(root, "preferences", [f"Line {n}" for n in range(4)])
    k = list(ids(root, "preferences").values())
    primes(root, [([], k)] * 6)
    for n in (1, 2):
        book(root, f"old-{n}", 90)
    view = cards(root)
    assert len(view["crowded"]) == 3 and view["unused"] == []  # 3 a month, crowded first
    view = app.review_retire({"id": view["crowded"][0]["id"]})
    *_, c = changes(root, "preferences")
    assert (c["op"], c["card"]) == ("retire", "crowded")
    assert len(view["crowded"]) == 2 and view["unused"] == []  # 2 left this month
    view = app.review_crowded_keep({"id": view["crowded"][0]["id"]})
    assert len(view["crowded"]) == 1 and view["unused"] == []
    next_month = (dt.date.fromisoformat(today()).replace(day=1) + dt.timedelta(days=32)
                  ).replace(day=1).isoformat()
    later = cards(root, day=next_month)  # a new month: 3 again, the playbooks after the lines
    assert len(later["crowded"]) == 2 and [x["id"] for x in later["unused"]] == ["old-1"]


def test_crowded_never_on_a_private_page(root, app):  # noqa: F811
    page(root, "personality", ["Private trait"], private=True)
    (cid,) = ids(root, "personality").values()
    primes(root, [([], [cid])] * 6)
    assert cards(root)["crowded"] == []
    with pytest.raises(server.NotFound):
        app.review_crowded_keep({"id": cid})
    assert xray.settings(Vault(root))["pin"] == set()


# ---- unused playbooks ----

def test_unused_playbooks_keep_and_open(root, app, monkeypatch):  # noqa: F811
    book(root, "old-unused", 90)
    book(root, "old-served", 90)
    book(root, "new-unused", 10)
    book(root, "old-private", 90, private=True)
    note(root, "notes/plain.md", "title: Plain\nkind: note\n", "# Not a playbook")
    assert cards(root)["unused"] == []  # an empty X-ray shows nothing to judge by
    xray.record(Vault(root), "cortex_prime", "cursor", {"playbooks": ["old-served"]}, {}, 10)
    (x,) = cards(root)["unused"]
    assert (x["id"], x["title"], x["path"], x["calls"], x["since"]) == (
        "old-unused", "Book old-unused", "notes/old-unused.md", 1, today())
    # Open: the existing open-in-Obsidian action takes the card's path
    opened = []
    monkeypatch.setattr(server, "_obsidian", lambda: True)
    monkeypatch.setattr(server, "_open", opened.append)
    assert app.open({"what": "page", "path": x["path"]})["obsidian"] is True
    assert opened[0].startswith("obsidian://open?path=") and "old-unused.md" in opened[0]
    # Keep: not asked again for 60 days
    view = app.review_playbook_keep({"id": "old-unused"})
    assert view["unused"] == []
    (c,) = changes(root, "playbooks")
    assert (c["card"], c["hash"]) == ("unused", review.text_hash("old-unused"))
    later = (dt.date.fromisoformat(today()) + dt.timedelta(days=review.UNUSED_DAYS + 1)
             ).isoformat()
    assert [x["id"] for x in cards(root, day=later)["unused"]] == ["old-unused", "new-unused"]
    for bad in ("old-served", "old-private", "new-unused", "plain"):
        with pytest.raises(server.NotFound):
            app.review_playbook_keep({"id": bad})


def test_the_api_escapes_and_guards_the_new_cards(root, app):  # noqa: F811
    from test_app import _serve, call

    page(root, "preferences", ["<b>Crowded</b> [x](http://evil.test/a)", "Other"])
    k = ids(root, "preferences")
    primes(root, [([], [k["<b>Crowded</b> [x](http://evil.test/a)"]])] * 6)
    srv, a = _serve(root)
    try:
        code, js, raw, _ = call(a, "GET", "/api/review")
        assert code == 200 and b"evil.test" not in raw and b"<b>" not in raw
        assert js["crowded"][0]["text"] == "Crowded x"
        assert call(a, "GET", "/api/review/crowded/keep")[0] == 405
        assert call(a, "POST", "/api/review/crowded/keep", {"id": "c-zzzzz"})[0] == 404
        assert call(a, "POST", "/api/review/playbook/keep", {"id": "nope"})[0] == 404
        code, js, _, _ = call(a, "POST", "/api/review/crowded/keep",
                              {"id": js["crowded"][0]["id"]})
        assert code == 200 and js["crowded"] == []
    finally:
        srv.shutdown()
        srv.server_close()
    assert len([e for e in events(root) if e["kind"] == "change"]) >= 1
