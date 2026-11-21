"""RR-4 and R3-3: cortex_patch's explicit `supersedes` pair (validated ids, shown in Review like
an `update_of` pair, never retiring the old line itself, carried from a proposal to its
Approve) and, while a recorded pair waits, prime, cortex_context and the hook serving only
the newer line until the user resolves it."""

import pytest
from conftest import run
from test_review import app, changes, live, today  # noqa: F401  (app is a fixture)
from test_review_conflicts import cid, prefs

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
