"""R13: a line that replaced another says `(since DAY)` on its page and nowhere it is served
(R13a); prime orders the lines of a section by use, a fold that stays the same all day
(G5b-4); an AI app's guess no app used in GUESS_DAYS retires itself once a day, undoably
(G5b-2); Review's cards say how often chats said a line (G5b-4 `said`)."""

import datetime as dt
import json

import pytest
from conftest import events, run
from test_review import ago, app, changes, today  # noqa: F401  (app is a fixture)
from test_review_auto import commit, live, page, past, tidy
from test_review_conflicts import cid
from test_review_reinforce import back
from typer.testing import CliRunner

from keepfold import claims, delta, packs, prime, review, xray
from keepfold.events import EventLog
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

NOW = dt.datetime.now(dt.UTC)


def _ring(root, *calls):
    """X-ray rows as prime records them, one per (day, [claim ids served])."""
    (root / ".keepfold").mkdir(exist_ok=True)
    (root / xray.RING).write_text("".join(json.dumps({
        "ts": f"{d}T12:00:00Z", "tool": "keepfold_prime", "client": "cursor",
        "served": {"preferences": [xray.claim(i) for i in ids]}, "trimmed": {}, "chars": 1})
        + "\n" for d, ids in calls), encoding="utf-8")


# ---- R13a: since ----

def test_a_since_stamp_keeps_the_id_and_goes_once():
    text = "# Preferences\n\n## Coding\n- Uses Helix\n- Uses tabs (until 2027-01-01)\n" \
           "- Reads papers ^c-aaaaa\n"
    a, b, c = claims.parse("preferences", text)
    new, line = claims.stamp_since(text, "preferences", a["id"], "2026-11-20")
    assert line == "- Uses Helix (since 2026-11-20)"
    new, line = claims.stamp_since(new, "preferences", "c-aaaaa", "2026-11-20")
    assert line == "- Reads papers (since 2026-11-20) ^c-aaaaa"  # before the id
    a2, b2, c2 = claims.parse("preferences", new)
    assert [a2["id"], b2["id"], c2["id"]] == [a["id"], b["id"], c["id"]]  # norm drops it
    assert claims.since(a2["text"]) == "2026-11-20" and claims.since(b2["text"]) is None
    for x in (a, b):  # a line with a since or an until day is never stamped again
        assert claims.stamp_since(new, "preferences", x["id"], "2026-11-21") == (new, None)
    assert claims.strip_since(line) == "- Reads papers ^c-aaaaa"


def test_since_for_same_facts_keep_their_day_a_newer_line_gets_today():
    old = {"text": "Uses Vim for editing code (since 2026-01-05)", "day": "2026-01-05"}
    copy = {"text": "Uses Vim for code editing"}
    other = {"text": "Uses Helix for editing code", "day": "2026-11-20"}
    assert review.since_for(old, copy, "2026-11-23", True) == "2026-01-05"
    assert review.since_for(old, other, "2026-11-23") == "2026-11-23"  # newer by its tier day
    assert review.since_for(other, old, "2026-11-23") is None  # the older line kept: none
    assert review.since_for(old, other, "2026-11-23", False) is None


def test_a_line_that_replaces_another_says_since_and_no_served_text_does(root):
    (root / ".keepfold").mkdir(exist_ok=True)
    page(root, "preferences", ["Uses Vim for editing code", "Drinks green tea"])
    old = cid(root, "preferences", "Uses Vim")
    p = {"id": "me/preferences", "op": "append_to_section", "target": "Coding",
         "user_confirmed": True}

    async def body(call):
        await call("keepfold_patch", {**p, "text": "- Writes code in Helix",
                                    "supersedes": [f"^{old}"]})
        await call("keepfold_patch", {**p, "text": "- Reads papers at night"})
        ctx = (await call("keepfold_context"))["context"]
        assert "Writes code in Helix" in ctx and "(since" not in ctx
        got = json.dumps(await call("keepfold_prime", {"prompt": "which editor writes code"}))
        assert "Helix" in got and "(since" not in got

    run(root, body)
    rows = live(root, "preferences")
    assert [claims.since(t) for t in rows if t.startswith("Writes code")] == [today()]
    assert [claims.since(t) for t in rows if t.startswith("Reads papers")] == [None]  # new
    assert f"(until {today()}" in past(root, "preferences")  # only the replaced line


def test_core_rules_files_and_the_hook_drop_the_stamp(root):
    (root / ".keepfold").mkdir(exist_ok=True)
    (root / "me" / "core.md").write_text(
        "# Core\n\n## Core\n- Keep answers short. (since 2026-11-01)\n", encoding="utf-8")
    page(root, "preferences", ["Uses Helix for code (since 2026-11-02)"])
    v, xr = Vault(root), {}
    parts, _ = prime.core(v, "code answers", [], NOW, xr=xr)
    text = "\n".join(parts) + packs.rule_block(v)
    assert "Keep answers short." in text and "Uses Helix for code" in text
    assert "(since" not in text
    assert xr["served"]["core"] == [xray.claim(cid(root, "core", "Keep answers short."))]
    assert (xray.claim(cid(root, "preferences", "Uses Helix")), "- [Coding] Uses Helix for code") \
        in delta.claim_lines(v, parts)  # the hook still knows the line


@pytest.mark.parametrize("tier", [None, "agent"])
def test_the_tidy_stamps_a_newer_line_it_keeps_and_r5a_owns_it(root, tier):
    (root / ".keepfold").mkdir(exist_ok=True)
    page(root, "setup", ["Edits code in Vim", "Writes code in Helix"], "Editor")
    commit(root)
    assert review.record_baseline(Vault(root))
    k = {t: r["id"] for t, r in live(root, "setup").items()}
    EventLog(Vault(root)).append(session="cli", kind="change", source="tutor", fields={
        "payload": {"change": {"tool": "patch", "id": "setup", "claim": k["Writes code in Helix"],
                               "supersedes": [{"page": "setup", "claim": k["Edits code in Vim"]}],
                               **({"tier": tier} if tier else {})}}})
    assert tidy(root)["retired"] == 1
    (c,) = [c for c in changes(root, "setup") if c.get("auto")]
    if tier:  # the owner's older line kept over a guess replaced nothing newer: no since
        assert list(live(root, "setup")) == ["Edits code in Vim"]
        assert "since" not in c
    else:
        assert list(live(root, "setup")) == [f"Writes code in Helix (since {today()})"]
        assert c["since"] == today() and c["kept"]["line"]
    assert review.outside_lines(Vault(root), events(root)) == []  # Keepfold's own write


def test_a_near_copy_kept_gets_no_since(root):
    (root / ".keepfold").mkdir(exist_ok=True)
    page(root, "preferences", ["Uses Vim for editing code", "Uses Vim for code editing"])
    commit(root)
    assert tidy(root)["retired"] == 1
    assert [claims.since(t) for t in live(root, "preferences")] == [None]  # same facts


@pytest.mark.parametrize("keep", ["left", "right"])
def test_keep_on_an_update_card_stamps_only_the_newer_line(root, app, keep):  # noqa: F811
    (root / "me" / "preferences.md").write_text(
        "# Preferences\n\n## Coding\n- Uses Vim for editing code\n", encoding="utf-8")

    async def body(call):
        await call("keepfold_patch", {"id": "me/preferences", "op": "append_to_section",
                                    "target": "Coding", "user_confirmed": True,
                                    "text": "- Uses Vim for editing code at work"})

    run(root, body)
    (c,) = app.review()["conflicts"]
    assert c["why"] == "maybe_update"
    app.review_conflict({"key": c["key"], "keep": keep})
    (text,) = live(root, "preferences")
    assert claims.since(text) == {"left": None, "right": today()}[keep]


# ---- G5b-4: use ----

def test_prime_orders_a_section_by_use_before_today(root):
    (root / ".keepfold").mkdir(exist_ok=True)
    page(root, "preferences", ["Uses Vim", "Uses tabs", "Uses dark mode"])
    v = Vault(root)
    k = {t: r["id"] for t, r in live(root, "preferences").items()}

    def order():
        parts, _ = prime.core(v, "hello", events(root), NOW)
        block = next(p for p in parts if p.startswith("## Preferences"))
        return [ln.split("] ")[1] for ln in block.splitlines() if ln.startswith("- [")]

    assert order() == ["Uses dark mode", "Uses tabs", "Uses Vim"]  # unused: newest first
    vim, tabs, dark = k["Uses Vim"], k["Uses tabs"], k["Uses dark mode"]
    _ring(root, (ago(2), [vim]), (ago(2), [vim]), (ago(1), [vim, tabs]), (ago(1), [vim]),
          *[(today(), [dark])] * 10)
    assert order() == ["Uses Vim", "Uses tabs", "Uses dark mode"]  # today's calls wait
    assert order() == order()
    used = review.use(v, events(root), today())
    assert used == {vim: {"n": 4, "last": ago(1)}, tabs: {"n": 1, "last": ago(1)}}
    assert review.use(v, events(root))[dark] == {"n": 10, "last": today()}
    EventLog(v).append(session="cli", kind="log", source="tutor", now=NOW - dt.timedelta(days=1),
                       fields={"payload": {"kind": "reconfirm", "claim": dark, "verdict": "yes"}})
    assert review.use(v, events(root), today())[dark] == {"n": 1, "last": ago(1)}


# ---- G5b-2: old guesses ----

def _guess(root, name, text, days, tier="agent"):
    """`text` on me/<name> as an AI app's guess (`tier`) written `days` days ago; its id."""
    i = cid(root, name, text)
    EventLog(Vault(root)).append(
        session="cli", kind="change", source="tutor", now=NOW - dt.timedelta(days=days),
        fields={"payload": {"change": {"tool": "patch", "id": name, "kind": "me",
                                       "op": "append_to_section", "tier": tier, "claim": i}}})
    return i


def test_an_old_guess_no_app_used_retires_once_a_day_undoably(root):
    from keepfold.cli import app as cli
    (root / ".keepfold").mkdir(exist_ok=True)
    page(root, "preferences", ["Uses Vim", "Likes jazz", "Drinks green tea", "Reads papers",
                               "Runs at dawn"])
    (root / "me" / "core.md").write_text("# Core\n\n## Core\n- Keep answers short.\n",
                                         encoding="utf-8")
    commit(root)
    vim = _guess(root, "preferences", "Uses Vim", 31)
    jazz = _guess(root, "preferences", "Likes jazz", 40)
    _guess(root, "preferences", "Drinks green tea", 29)
    _guess(root, "preferences", "Reads papers", 40, "confirmed")
    xray.set_flag(Vault(root), "pin", xray.claim(_guess(root, "preferences", "Runs at dawn",
                                                        40)), True)
    _guess(root, "core", "Keep answers short.", 40)
    assert tidy(root) == {"moved": 0, "retired": 0}  # no X-ray call yet: nothing to judge by
    _ring(root, (ago(20), []), (ago(3), [jazz]))
    assert tidy(root) == {"moved": 0, "retired": 0}  # older than the ring: may have been served
    _ring(root, (ago(45), []), (ago(3), [jazz]))
    assert tidy(root) == {"moved": 0, "retired": 0, "guesses": 1}
    assert set(live(root, "preferences")) == {"Likes jazz", "Drinks green tea", "Reads papers",
                                              "Runs at dawn"}
    assert "Uses Vim" in past(root, "preferences") and "Keep answers short." in live(root, "core")
    (c,) = [c for c in changes(root, "preferences") if c.get("auto")]
    assert (c["op"], c["claim"], c["why"], "kept" in c) == ("retire", vim, "unused", False)
    (receipt,) = (root / "sessions").glob("*-tidy-*.md")
    assert "Keepfold retired 1 old guess no app used" in receipt.read_text("utf-8")
    assert tidy(root) == {"moved": 0, "retired": 0}  # once a day
    (sid,) = {e["session"] for e in events(root) if e["kind"] == "open"}
    r = CliRunner().invoke(cli, ["undo", sid, "--vault", str(root)])
    assert r.exit_code == 0, r.output
    assert "Uses Vim" in live(root, "preferences")
    state = root / ".keepfold" / "tidy.json"
    state.write_text(json.dumps({**json.loads(state.read_text("utf-8")), "guesses": ago(1)}),
                     encoding="utf-8")  # as on the next day
    assert tidy(root) == {"moved": 0, "retired": 0}  # an undone retire is never redone


# ---- G5b-4: said ----

def test_review_cards_say_how_often_chats_said_a_line(root, app):  # noqa: F811
    (root / "me" / "preferences.md").write_text(
        "# Preferences\n\n## Coding\n- Uses tabs for indentation\n"
        "- Never uses tabs for indentation\n", encoding="utf-8")
    tabs = cid(root, "preferences", "Uses tabs")
    back(root, tabs, 3)
    (c,) = app.review()["conflicts"]
    said = {s["id"]: s.get("said") for s in (c["left"], c["right"])}
    assert said.pop(tabs) == {"times": 3, "last": today()}
    assert list(said.values()) == [None]
