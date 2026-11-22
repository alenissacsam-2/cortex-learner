"""RR-4 and R3-3: cortex_patch's explicit `supersedes` pair (validated ids) and, 22 Nov (D2),
settled at once: the newer line stays and the older is retired, unless the older is the user's
own and the newer a guess (then the guess is not saved). Prime, cortex_context and the hook
serve only the line that stays."""

import datetime as dt

import pytest
from conftest import run
from test_review import app, changes, live, today  # noqa: F401  (app is a fixture)
from test_review_conflicts import cid, prefs

from cortex import claims, prime
from cortex.events import EventLog
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

NEW = {"id": "me/preferences", "op": "append_to_section", "target": "Coding",
       "user_confirmed": True, "text": "- Writes code in Helix"}


def test_supersedes_retires_the_old_line_at_once(root, app):  # noqa: F811
    prefs(root, "Uses Vim for editing code", "Drinks green tea")
    old = cid(root, "preferences", "Uses Vim")

    async def body(call):
        out = await call("cortex_patch", {**NEW, "supersedes": [f"^{old}"]})
        assert out["supersedes"] == [old] and "possible_update_of" not in out
        assert [r["id"] for r in out["replaced"]] == [old] and "cortex undo" in out["next"]

    run(root, body)
    new = cid(root, "preferences", "Writes code in Helix")
    (c,) = [c for c in changes(root, "preferences") if c.get("supersedes")]
    assert (c["claim"], c["supersedes"]) == (new, [{"page": "preferences", "claim": old}])
    (r,) = [c for c in changes(root, "preferences") if c.get("auto")]
    assert (r["op"], r["target"], r["why"]) == ("retire", old, "supersedes")
    assert "Uses Vim for editing code" not in [r["text"] for r in live(root, "preferences")]
    assert app.review()["conflicts"] == []  # settled: never asked
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


def test_a_guess_never_supersedes_the_users_own_line(root, app):  # noqa: F811
    prefs(root, "Uses Vim for editing code")
    old = cid(root, "preferences", "Uses Vim")

    async def body(call):
        out = await call("cortex_patch", {**NEW, "user_confirmed": False, "supersedes": [old]})
        assert out["changed"] is False and out["clashes_with"]["id"] == old
        assert "Ask them" in out["next"]

    run(root, body)
    assert "Helix" not in (root / "me" / "preferences.md").read_text("utf-8")
    assert app.review()["conflicts"] == [] and app.review()["pending"] == []


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


def test_a_settled_pair_serves_only_the_newer_line(root):
    prefs(root, "Uses Vim for editing code", "Drinks green tea")
    old = cid(root, "preferences", "Uses Vim")

    async def body(call):
        await call("cortex_patch", {**NEW, "supersedes": [old]})

    run(root, body)
    for text in (light(root), context(root)):
        assert "Writes code in Helix" in text and "Uses Vim" not in text
        assert "Drinks green tea" in text  # only the old line of the pair is retired


def test_a_guessed_update_pair_and_a_profile_line_are_settled_too(root):
    prefs(root, "Uses Vim for editing code")
    (root / "me" / "profile.md").write_text(
        '---\nid: "profile"\nsummary: "Who I am"\n---\n# Profile\n\n## Background\n'
        "- Lives in Pune\n- Studies computer science\n", encoding="utf-8")
    home = cid(root, "profile", "Lives in Pune")
    vim = cid(root, "preferences", "Uses Vim")

    async def body(call):
        # W1: the newer line says more ("at work"), so the guessed pair is not the same
        # facts: both stay on the page (retiring the older would narrow its scope)
        out = await call("cortex_patch", {**NEW, "text": "- Uses Vim for editing code at work"})
        assert vim and not out.get("replaced")
        await call("cortex_patch", {"id": "me/profile", "op": "append_to_section",
                                    "target": "Background", "text": "- Lives in Mumbai",
                                    "user_confirmed": True, "supersedes": [home]})

    run(root, body)
    assert vim in {r["id"] for r in claims.rows(Vault(root))
                   if r["page"] == "preferences" and r["status"] == "live"}
    for text in (light(root), context(root)):
        assert "Uses Vim for editing code at work" in text  # (prime.held mutes the older line)
        assert "Lives in Mumbai" in text and "Pune" not in text
        assert "Studies computer science" in text
