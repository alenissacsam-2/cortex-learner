"""Plan 5b: cortex_patch's `possible_update_of` (word overlap, a warm embedder, never a model
load), the Review's conflict pairs and their Keep both / left / right, and repeated corrections
mined from imported chats (two or three chats by kind, private chats skipped, the incremental
scan, Approve into me/preferences or me/core, Dismiss remembered), with the API escaping note
text."""

import hashlib
import json

import numpy as np
import pytest
from conftest import events, run
from test_app import _serve, call
from test_review import app, changes, live, today  # noqa: F401  (app is a fixture)

from cortex import claims, corrections, review, semantic
from cortex.app import server
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")


def prefs(root, *lines):
    (root / "me" / "preferences.md").write_text(
        "# Preferences\n\n## Coding\n" + "".join(f"- {ln}\n" for ln in lines), encoding="utf-8")


def cid(root, page, text):
    p = (root / "me" / f"{page}.md").read_text("utf-8")
    return next(r["id"] for r in claims.parse(page, p) if r["text"].startswith(text))


def chat(root, id, turns, private=False, app_="Gemini"):
    body = (f"# Chat {id}\n\nTimeline: [[2026-03-01]] · Source: {app_} · Date: 2026-03-01\n\n"
            + "".join(f"### 2026-03-01 10:0{k}\n**Me:** {t}\n\n**{app_}:** an answer\n\n"
                      for k, t in enumerate(turns)))
    fm = f'title: "Chat {id}"\n' + ("private: true\n" if private else "")
    p = root / "resources" / f"{id}.md"
    p.parent.mkdir(exist_ok=True)
    p.write_text(f"---\n{fm}---\n{body}", encoding="utf-8")
    return p


class Fake:
    """A warm embedder: one direction per meaning word, else a fixed random direction."""
    MEAN = {"dark": 0, "black": 0, "night": 0, "tea": 1}

    def __init__(self):
        self.calls = []

    def __call__(self, texts):
        self.calls.append(list(texts))
        out = []
        for t in texts:
            hit = next((k for w, k in self.MEAN.items() if w in t.lower()), None)
            if hit is not None:
                v = np.eye(16, dtype=np.float32)[hit]
            else:
                seed = int(hashlib.sha256(t.encode()).hexdigest()[:8], 16)
                v = np.random.default_rng(seed).standard_normal(16).astype(np.float32)
            out.append(v)
        return np.asarray(out)


# ---- possible_update_of ----

def test_possible_update_of_by_word_overlap(root):
    prefs(root, "Uses Vim for editing code (confirmed 2026-01-01)", "Writes Rust on weekends")
    old = cid(root, "preferences", "Uses Vim")

    async def body(call):
        out = await call("cortex_patch", {"id": "me/preferences", "op": "append_to_section",
                                          "target": "Coding", "user_confirmed": True,
                                          "text": "- Uses Neovim for editing code"})
        # 22 Nov QA: "Neovim" / "Vim" may be a switch or two facts: both stay, the agent decides
        assert "replaced" not in out and out["possible_update_of"]["id"] == old
        assert "<untrusted_note" in out["possible_update_of"]["text"]
        assert "Both stay" in out["next"] and f"target='{old}'" in out["next"]
        out = await call("cortex_patch", {"id": "me/preferences", "op": "append_to_section",
                                          "target": "Coding", "user_confirmed": True,
                                          "text": "- Sleeps early on Sundays"})
        assert "possible_update_of" not in out
        out = await call("cortex_patch", {"id": "me/preferences", "op": "append_to_section",
                                          "target": "Coding",
                                          "text": "- Uses Neovim for code editing"})
        # a guess that only rewords the user's own line is not saved twice
        assert out["changed"] is False and out["duplicate_of"]["id"] == new_id(root)
        out = await call("cortex_patch", {"id": "me/preferences", "op": "append_to_section",
                                          "target": "Coding", "user_confirmed": True,
                                          "text": "- Never sleeps early on Sundays"})
        assert out["possible_update_of"]["id"] == cid(root, "preferences", "Sleeps early")
        assert out["next"].startswith("This line may update ^")
        assert "Cortex settles such a pair itself" in out["next"]  # two of theirs: a card

    run(root, body)
    text = (root / "me" / "preferences.md").read_text("utf-8")
    assert "## Past" not in text and "Uses Vim for editing code" in text  # nothing retired
    assert "Uses Neovim for code editing" not in text
    new = new_id(root)
    (c,) = [c for c in changes(root, "preferences") if c.get("update_of") and c["claim"] == new]
    assert (c["claim"], c["update_of"]) == (new, {"page": "preferences", "claim": old})


def new_id(root):
    return cid(root, "preferences", "Uses Neovim")


def test_possible_update_of_with_a_warm_embedder(root, monkeypatch):
    prefs(root, "Prefers dark mode in editors", "Drinks green tea")
    v, day = Vault(root), today()
    assert review.collision(v, "preferences", "Likes a black theme everywhere", day) is None
    monkeypatch.setattr(semantic, "_embedder", Fake())
    hit = review.collision(v, "preferences", "Likes a black theme everywhere", day)
    assert hit and hit["text"] == "Prefers dark mode in editors"
    assert hit["apart"]  # no shared word: a guess the agent settles, never Cortex
    assert review.collision(v, "preferences", "Plays chess on Fridays", day) is None


def test_private_pages_are_never_matched(root):
    (root / "me" / "personality.md").write_text(
        "---\nprivate: true\n---\n# P\n\n## Values\n- Uses Vim for editing code\n", "utf-8")
    assert review.collision(Vault(root), "preferences", "Uses Neovim for editing code",
                            today()) is None


@pytest.mark.parametrize("warm", [False, True])
def test_the_patch_never_loads_a_model(root, monkeypatch, warm):
    def no(*a, **k):
        raise AssertionError("cortex_patch must never load or look for a model")

    monkeypatch.setattr(semantic, "load_embedder", no)
    monkeypatch.setattr(semantic, "models_dir", no)
    monkeypatch.setattr(semantic, "_embedder", Fake() if warm else None)
    prefs(root, "Prefers dark mode in editors")

    async def body(call):
        out = await call("cortex_patch", {"id": "me/preferences", "op": "append_to_section",
                                          "target": "Coding", "user_confirmed": True,
                                          "text": "- Prefers dark mode at night"})
        assert out.get("replaced") or "possible_update_of" in out  # the pair was seen

    run(root, body)


# ---- conflicts ----

def test_conflict_pairs_and_the_three_buttons(root, app):  # noqa: F811
    prefs(root, "Never use tabs for indentation", "Always use tabs for indentation",
          "Avoid long meetings in the morning", "Prefer long meetings in the morning",
          "Don't send me summaries by email", "Send me summaries by email",
          "Drinks tea", "Doesn't drink coffee")
    pairs = app.review()["conflicts"]
    assert [(p["left"]["text"], p["right"]["text"], p["why"]) for p in pairs] == [
        ("Never use tabs for indentation", "Always use tabs for indentation", "opposite"),
        ("Avoid long meetings in the morning", "Prefer long meetings in the morning", "opposite"),
        ("Don't send me summaries by email", "Send me summaries by email", "opposite")]
    with pytest.raises(ValueError):
        app.review_conflict({"key": pairs[0]["key"], "keep": "neither"})
    with pytest.raises(server.NotFound):
        app.review_conflict({"key": "nope", "keep": "both"})
    view = app.review_conflict({"key": pairs[0]["key"], "keep": "both"})
    assert len(view["conflicts"]) == 2
    *_, c = changes(root, "preferences")
    assert (c["tool"], c["pair"], c["confirm"]) == ("keep_both", pairs[0]["key"], "clicked")
    view = app.review_conflict({"key": pairs[1]["key"], "keep": "left"})
    view = app.review_conflict({"key": pairs[2]["key"], "keep": "right"})
    assert view["conflicts"] == []
    past = (root / "me" / "preferences.md").read_text("utf-8").split("## Past")[1]
    assert "Prefer long meetings" in past and "Don't send me summaries" in past
    kept = [r["text"] for r in live(root, "preferences")]
    assert "Avoid long meetings in the morning" in kept and "Send me summaries by email" in kept
    assert "Never use tabs for indentation" in kept and "Always use tabs for indentation" in kept
    retires = [c for c in changes(root, "preferences") if c.get("op") == "retire"]
    assert [c["confirm"] for c in retires] == ["clicked", "clicked"]


def test_a_recorded_update_pair_is_settled_never_shown(root, app):  # noqa: F811
    """22 Nov (D2): the newer line of a recorded pair stays and the older is retired at once,
    recorded as an automatic change the user can undo; Review never asks. (A guessed pair of
    two facts, "Neovim" / "Vim", is left alone: test_possible_update_of_by_word_overlap.)"""
    prefs(root, "Uses Vim for editing code")
    old = cid(root, "preferences", "Uses Vim")

    async def body(call):
        await call("cortex_patch", {"id": "me/preferences", "op": "append_to_section",
                                    "target": "Coding", "user_confirmed": True,
                                    "text": "- Uses Vim for editing code at work"})

    run(root, body)
    assert app.review()["conflicts"] == []
    assert "Uses Vim" in (root / "me" / "preferences.md").read_text("utf-8").split("## Past")[1]
    (c,) = [c for c in changes(root, "preferences") if c.get("auto")]
    assert (c["op"], c["target"], c["why"], c["card"]) == ("retire", old, "update", "conflict")


# ---- repeated corrections ----

def corrections_vault(root):
    chat(root, "c-one", ["don't use bullet points", "explain monads", "don't you think so?"])
    chat(root, "c-two", ["Please stop using bullet points"])
    chat(root, "c-three", ["Never answer in French"])
    chat(root, "c-four", ["Never answer in French"], private=True)  # private: not counted


def test_a_correction_seen_in_two_chats_is_offered(root, app):  # noqa: F811
    corrections_vault(root)
    (rule,) = app.review()["rules"]  # French: one non-private chat only; the question: none
    assert (rule["text"], rule["chats"], rule["page"]) == (
        "don't use bullet points", 2, "preferences")
    cache = json.loads((root / corrections.CACHE).read_text("utf-8"))["chats"]
    assert cache["c-four"]["lines"] == [] and "French" not in json.dumps(cache["c-four"])
    view = app.review_rule_approve({"key": rule["key"]})
    assert view["rules"] == []  # now a live line about the user
    page = (root / "me" / "preferences.md").read_text("utf-8")
    assert f"- don't use bullet points (confirmed {today()}) ^c-" in page.split("## Personal")[1]
    (row,) = [r for r in live(root, "preferences") if "bullet" in r["text"]]
    assert (row["tier"], row["confirm"]) == ("confirmed", "clicked")


def test_a_core_rule_or_a_soft_wording_needs_three_chats(root, app):  # noqa: F811
    """RR-25: a standing always/never line is pinned to every chat, and a soft opener is often
    no correction at all, so both need 3 chats; a preference line needs 2."""
    chat(root, "s-one", ["Never answer in French", "too long, keep it short"])
    chat(root, "s-two", ["never answer in French", "Too long. Keep it short"])
    assert app.review()["rules"] == []
    chat(root, "s-three", ["Never answer in French!", "way too long, keep it short"])
    got = {r["text"]: (r["chats"], r["page"]) for r in app.review()["rules"]}
    assert got == {"Never answer in French": (3, "core"),
                   "Too long. Keep it short": (3, "preferences")}
    key = next(r["key"] for r in app.review()["rules"] if r["page"] == "core")
    app.review_rule_approve({"key": key})
    assert "- Never answer in French (confirmed" in (root / "me" / "core.md").read_text("utf-8")
    assert corrections.bound("from now on, use metric units")[::2] == ("core", 3)
    assert corrections.bound("I prefer metric units")[::2] == ("preferences", 3)
    assert corrections.bound("please use metric units")[::2] == ("preferences", 2)


def test_dismiss_is_remembered_by_hash(root, app):  # noqa: F811
    corrections_vault(root)
    (rule,) = app.review()["rules"]
    assert app.review_rule_dismiss({"key": rule["key"]})["rules"] == []
    *_, c = changes(root, "corrections")
    assert (c["tool"], c["hash"], c["confirm"]) == (
        "dismiss", review.text_hash("don't use bullet points"), "clicked")
    assert "bullet" not in json.dumps(events(root))
    chat(root, "c-five", ["Stop using bullet points"])  # seen again: still dismissed
    assert app.review()["rules"] == []
    with pytest.raises(server.NotFound):
        app.review_rule_approve({"key": rule["key"]})


def test_the_scan_reads_only_changed_chats(root, monkeypatch):
    corrections_vault(root)
    v, read = Vault(root), []
    real = corrections._lines
    monkeypatch.setattr(corrections, "_lines", lambda body: read.append(1) or real(body))
    assert set(corrections.scan(v)) == {"c-one", "c-two", "c-three"}
    assert len(read) == 3  # the private chat is not even parsed for turns
    read.clear()
    corrections.scan(v)
    assert read == []
    chat(root, "c-three", ["Never answer in French", "Always cite sources please"])
    (root / "resources" / "c-two.md").unlink()
    assert corrections.scan(v)["c-three"] == ["Never answer in French",
                                               "Always cite sources please"]
    assert len(read) == 1
    assert "c-two" not in json.loads((root / corrections.CACHE).read_text("utf-8"))["chats"]


def test_is_correction():
    for t in ("don't use emojis", "Stop apologising", "No, I said Python 3.12", "always cite",
              "Never answer in French", "please use metric units", "Please don't do that",
              "I told you: short answers", "I prefer metric units", "From now on, cite sources",
              "That's too long", "not what I meant", "Actually, use Rust"):
        assert corrections.is_correction(t), t
    for t in ("don't you think so?", "use the function above", "how do I stop a thread",
              "x" * 201, "Why don't you try", "", "actually is that right?", "actuallyx",
              "the list is too long",
              "never mind, let's do graphs", "Never mind that", "no, never mind"):  # audit M8
        assert not corrections.is_correction(t), t


def test_the_api_escapes_conflicts_and_rules(root, app):  # noqa: F811
    prefs(root, "Never write <b>bold</b> [docs](http://evil.test/x)",
          "Always write <b>bold</b> [docs](http://evil.test/x)")
    chat(root, "e-one", ["Never use <script>x</script> emojis ![i](http://img.test/a.png)"])
    chat(root, "e-two", ["never use <script>x</script> emojis ![i](http://img.test/a.png)"])
    chat(root, "e-three", ["Never use <script>x</script> emojis ![i](http://img.test/a.png)"])
    srv, a = _serve(root)
    try:
        code, js, raw, _ = call(a, "GET", "/api/review")
        assert code == 200 and len(js["conflicts"]) == 1 and len(js["rules"]) == 1
        for bad in (b"<script", b"<b>", b"evil.test", b"img.test", b"http"):
            assert bad not in raw
        assert js["conflicts"][0]["left"]["text"] == "Never write bold docs"
        assert call(a, "POST", "/api/review/conflict", {"key": "x", "keep": "both"})[0] == 404
        code, js, _, _ = call(a, "POST", "/api/review/rule/dismiss",
                              {"key": js["rules"][0]["key"]})
        assert code == 200 and js["rules"] == []
    finally:
        srv.shutdown()
        srv.server_close()
