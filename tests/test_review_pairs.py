"""RR-4 and R3-3: cortex_patch's explicit `supersedes` pair (validated ids, shown in Review like
an `update_of` pair, never retiring the old line itself, carried from a proposal to its
Approve) and, while a recorded pair waits, prime, cortex_context and the hook serving only
the newer line until the user resolves it."""

import datetime as dt

import pytest
from conftest import run
from test_review import app, changes, live, today  # noqa: F401  (app is a fixture)
from test_review_conflicts import cid, prefs

from cortex import prime
from cortex.events import EventLog
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

NEW = {"id": "me/preferences", "op": "append_to_section", "target": "Coding",
       "user_confirmed": True, "text": "- Writes code in Helix"}


def test_supersedes_records_a_pair_and_keeps_the_old_line(root, app):  # noqa: F811
    prefs(root, "Uses Vim for editing code", "Drinks green tea")
    old = cid(root, "preferences", "Uses Vim")

    async def body(call):
        out = await call("cortex_patch", {**NEW, "supersedes": [f"^{old}"]})
        assert out["supersedes"] == [old] and "possible_update_of" not in out
        assert "Review" in out["next"]

    run(root, body)
    new = cid(root, "preferences", "Writes code in Helix")
    (c,) = [c for c in changes(root, "preferences") if c.get("supersedes")]
    assert (c["claim"], c["supersedes"]) == (new, [{"page": "preferences", "claim": old}])
    assert "Uses Vim for editing code" in [r["text"] for r in live(root, "preferences")]
    (p,) = app.review()["conflicts"]  # no word overlap: shown only because it was named
    assert (p["why"], p["left"]["id"], p["right"]["id"]) == ("supersedes", old, new)
    assert app.review_conflict({"key": p["key"], "keep": "right"})["conflicts"] == []
    assert "Uses Vim" in (root / "me" / "preferences.md").read_text("utf-8").split("## Past")[1]


def test_supersedes_ids_are_checked(root):
    prefs(root, "Uses Vim for editing code")
    old = cid(root, "preferences", "Uses Vim")

    async def body(call):
        for args, said in (
                ({**NEW, "supersedes": ["c-zzzzz"]}, "'c-zzzzz' is not a live claim id"),
                ({**NEW, "supersedes": [old, "c-aaaaa", "c-bbbbb", "c-ccccc"]}, "1 to 3"),
                ({**NEW, "supersedes": []}, "1 to 3"),
                ({**NEW, "id": "me/teaching-contract", "supersedes": [old]}, "only when"),
                ({**NEW, "text": "- one\n- two", "supersedes": [old]}, "only when"),
                ({"id": "me/preferences", "op": "set_field", "target": "title", "text": "x",
                  "supersedes": [old]}, "only when")):
            out = await call("cortex_patch", args)
            assert out["error"] == "invalid_argument" and said in out["message"], out

    run(root, body)
    text = (root / "me" / "preferences.md").read_text("utf-8")
    assert "Helix" not in text and "one" not in text  # nothing written on a refusal


def test_a_proposal_carries_its_pair_to_approve(root, app):  # noqa: F811
    prefs(root, "Uses Vim for editing code")
    old = cid(root, "preferences", "Uses Vim")

    async def body(call):
        out = await call("cortex_patch", {**NEW, "user_confirmed": False, "supersedes": [old]})
        assert out["queued"] is True and out["supersedes"] == [old]

    run(root, body)
    assert app.review()["conflicts"] == []  # nothing to pair until the line is written
    (p,) = app.review()["pending"]
    (pair,) = app.review_approve({"key": p["key"]})["conflicts"]
    assert (pair["why"], pair["left"]["id"]) == ("supersedes", old)
    assert pair["right"]["text"] == "Writes code in Helix"


# ---- R3-3: while a pair waits, only the newer line is served ----

def light(root, prompt="editing code"):
    """prime.core's blocks (cortex_prime's light part and the prime hook's context)."""
    v = Vault(root)
    return "\n".join(prime.core(v, prompt, list(EventLog(v).iter_events()),
                                dt.datetime.now(dt.UTC))[0])


def context(root):
    got = {}

    async def body(call):
        got["context"] = (await call("cortex_context", {"scope": "all"}))["context"]

    run(root, body)
    return got["context"]


@pytest.mark.parametrize("keep", ["both", "left", "right"])
def test_a_waiting_pair_serves_the_newer_line_until_resolved(root, app, keep):  # noqa: F811
    prefs(root, "Uses Vim for editing code", "Drinks green tea")
    old = cid(root, "preferences", "Uses Vim")

    async def body(call):
        await call("cortex_patch", {**NEW, "supersedes": [old]})

    run(root, body)
    for text in (light(root), context(root)):
        assert "Writes code in Helix" in text and "Uses Vim" not in text
        assert "Drinks green tea" in text  # only the old line of the pair is held back
    assert "Uses Vim" in (root / "me" / "preferences.md").read_text("utf-8")  # nothing deleted
    (p,) = app.review()["conflicts"]
    app.review_conflict({"key": p["key"], "keep": keep})
    for text in (light(root), context(root)):
        assert ("Uses Vim" in text) == (keep in ("both", "left"))
        assert ("Writes code in Helix" in text) == (keep in ("both", "right"))


def test_a_guessed_update_pair_and_a_profile_line_are_held_too(root):
    prefs(root, "Uses Vim for editing code")
    (root / "me" / "profile.md").write_text(
        '---\nid: "profile"\nsummary: "Who I am"\n---\n# Profile\n\n## Background\n'
        "- Lives in Pune\n- Studies computer science\n", encoding="utf-8")
    home = cid(root, "profile", "Lives in Pune")

    async def body(call):
        out = await call("cortex_patch", {**NEW, "text": "- Uses Neovim for editing code"})
        assert out["possible_update_of"]["id"] == cid(root, "preferences", "Uses Vim")
        await call("cortex_patch", {"id": "me/profile", "op": "append_to_section",
                                    "target": "Background", "text": "- Lives in Mumbai",
                                    "user_confirmed": True, "supersedes": [home]})

    run(root, body)
    for text in (light(root), context(root)):
        assert "Uses Neovim" in text and "Uses Vim" not in text
        assert "Lives in Mumbai" in text and "Pune" not in text
        assert "Studies computer science" in text
