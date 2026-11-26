"""v5 PHASE4 V5: server roles (owner, reader; delegate was removed on 20 Nov 2026), the kill
switch and the access log."""

import pytest
from conftest import events, git, run
from typer.testing import CliRunner

from keepfold import doctor, people, share
from keepfold.cli import app
from keepfold.mcp_server import Keepfold, build_server
from keepfold.vault import Vault, VaultError

pytestmark = pytest.mark.usefixtures("home")



def test_reader_reads_but_never_writes(root):
    people.add(Vault(root), "Asha", {"relation": "friend"})

    async def body(call):
        assert "hits" in await call("keepfold_find", {"query": "pointers"})
        assert "id" in await call("keepfold_read", {"id": "two-pointers"})
        assert "items" in await call("keepfold_find", {"kind": "resurface"})
        assert "findings" in await call("keepfold_reflect", {"action": "lint"})
        for name, args in (
                ("keepfold_chats", {}), ("keepfold_read", {"id": "people/asha"}),
                ("keepfold_reflect", {"action": "split", "hub": "x", "hash": "y",
                                    "children": []}),
                ("keepfold_grade", {"items": [{"question": "q", "answer": "a",
                                             "correct": True}], "concept": "two-pointers"}),
                ("keepfold_patch", {"id": "me/profile", "op": "append_to_section",
                                  "target": "Background", "text": "- x"}),
                ("keepfold_add", {"kind": "note", "title": "x"}),
                ("keepfold_log", {"kind": "journal", "payload": {"text": "x"}}),
                ("keepfold_grade", {"learner_answer": "x", "concept": "two-pointers",
                                  "question": "q", "verdict": "correct"}),
                ("keepfold_start", {}), ("keepfold_end", {}),
                ("keepfold_log", {"kind": "void", "payload": {"event_id": "x", "reason": "y"}})):
            out = await call(name, args)
            assert out["error"] == "role_refused" and "reader" in out["next"], name

    run(root, body, "reader")
    assert not (root / "events").exists() or not list((root / "events").rglob("*.jsonl"))


def test_bad_role(root):
    for role in ("admin", "delegate"):  # delegate: removed on 20 Nov 2026
        with pytest.raises(VaultError):
            Keepfold(Vault(root), role)
    r = CliRunner().invoke(app, ["mcp", "--role", "admin", "--vault", str(root)])
    assert r.exit_code == 1 and "--role" in r.output


def test_kill_switch(root):
    v = Vault(root)
    cli = CliRunner()
    assert doctor.check_kill(root)[1:3] == ("Kill switch", "off")
    assert cli.invoke(app, ["kill", "--vault", str(root)]).exit_code == 0
    assert share.killed(v) and doctor.check_kill(root)[2].startswith("on")
    owner = Keepfold(v)
    assert owner._gate("keepfold_as_me", {})["error"] == "killed"
    assert owner._gate("keepfold_log", {"kind": "journal"}) is None

    async def study(call):
        assert "hits" in await call("keepfold_find", {"query": "pointers"})
        assert "error" not in await call("keepfold_log", {"kind": "resurfaced",
                                                        "payload": {"ids": ["two-pointers"]}})

    async def off(call):
        out = await call("keepfold_find", {"query": "pointers"})
        assert out["error"] == "killed" and "kill --off" in out["next"]

    run(root, study, "owner")
    run(root, off, "reader")
    assert cli.invoke(app, ["kill", "--off", "--vault", str(root)]).exit_code == 0
    assert not share.killed(v) and Keepfold(v, "reader")._gate("keepfold_find", {}) is None


def test_access_log(root):
    v = Vault(root)
    core = Keepfold(v)
    out = core._call("keepfold_log", core.keepfold_log, {"kind": "access", "payload": {}})
    assert out["error"] == "invalid_argument"
    core._access("as_me", task="Email <b>Dr Rao</b> about [the](http://x.y) deadline",
                 audience="professor")

    rows = share.access_rows(events(root))
    assert [(r["access"], r["audience"]) for r in rows] == [("as_me", "professor")]
    assert "<b>" not in rows[0]["task"] and "http" not in rows[0]["task"]
    r = CliRunner().invoke(app, ["access", "--vault", str(root), "--days", "1"])
    assert r.exit_code == 0, r.output
    assert "as_me" in r.output and "professor" in r.output
    assert share.access_rows(events(root), "2999-01-01") == []


# Round 2 (outside review §6 items 6, 7): a non-owner server serves only me/ pages shared with
# work, through every read tool, and writes nothing at start-up.
SECRETS = ("Zorblat", "Quuxify", "glimmerfrost")  # in pages not shared with work (profile,
# contract: self; personality: close); "plonkwise" is in me/preferences.md, share: work


def share_vault(root):
    me = root / "me"
    (me / "profile.md").write_text('---\nid: "profile"\ntitle: "Profile"\n'
                                   'summary: "Zorblat learner"\n---\n# Profile\n')
    (me / "teaching-contract.md").write_text("# Teaching contract\n\n- Quuxify every answer.\n")
    (me / "personality.md").write_text('---\nid: "personality"\ntitle: "Personality"\n'
                                       "share: close\n---\n"
                                       "## How I think\n- Thinks in glimmerfrost pictures.\n")
    (me / "preferences.md").write_text('---\nid: "preferences"\ntitle: "Preferences"\n'
                                       "share: work\n---\n## Coding\n- Wants plonkwise code.\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "share tiers")


@pytest.mark.parametrize("role", ["reader"])
def test_non_owner_serves_only_me_pages_shared_with_work(root, role):
    share_vault(root)

    async def body(call):
        prime = (await call("keepfold_prime", {"prompt": "plonkwise glimmerfrost Zorblat"}))
        ctx = (await call("keepfold_context", {}))["context"]
        for text in (prime["context"], ctx):
            assert "plonkwise" in text and not any(s in text for s in SECRETS), text
        assert "withheld: 1 me/ page" in ctx  # the Profile block says why it is empty
        for id in ("profile", "me/profile", "personality", "teaching-contract"):
            assert (await call("keepfold_read", {"id": id}))["error"] == "unknown_id", id
        assert "plonkwise" in (await call("keepfold_read", {"id": "preferences"}))["text"]
        for q in SECRETS:
            assert (await call("keepfold_find", {"query": q}))["hits"] == [], q
            assert (await call("keepfold_recall", {"query": q}))["hits"] == [], q
        hits = (await call("keepfold_find", {"query": "plonkwise"}))["hits"]
        assert [h["id"] for h in hits] == ["preferences"]
        assert (await call("keepfold_reflect", {}))["error"] == "role_refused"
        out = await call("keepfold_as_me", {"task": "Say hi", "audience": "friend"})
        assert out["self"]["withheld"] == 1  # personality (close): the owner would serve it

    run(root, body, role)


@pytest.mark.parametrize("role", ["reader"])
def test_non_owner_lint_hides_expired_claims_of_withheld_pages(root, role):
    share_vault(root)
    (root / "me/personality.md").write_text('---\nid: "personality"\nshare: close\n---\n'
                                            "## How I think\n- Old glimmerfrost (until "
                                            "2020-01-01)\n")
    (root / "me/preferences.md").write_text('---\nid: "preferences"\nshare: work\n---\n'
                                            "## Coding\n- Old plonkwise (until 2020-01-01)\n")

    async def body(call):
        found = [f for f in (await call("keepfold_reflect", {"action": "lint"}))["findings"]
                 if f["kind"] == "expired_claim"]
        assert [f["id"] for f in found] == ["me/preferences"], found

    run(root, body, role)


def test_non_owner_preferences_block_says_withheld(root):
    share_vault(root)
    (root / "me/preferences.md").write_text('---\nid: "preferences"\n---\n## Coding\n- x\n')

    async def body(call):
        text = (await call("keepfold_prime", {"prompt": "hi"}))["context"]
        assert "withheld: 2 me/ page(s)" in text and "not recorded" not in text

    run(root, body, "reader")


def test_owner_sees_every_me_page(root):
    share_vault(root)

    async def body(call):
        prime = (await call("keepfold_prime", {"prompt": "plonkwise glimmerfrost"}))["context"]
        assert all(s in prime for s in SECRETS) and "plonkwise" in prime, prime
        assert "Zorblat" in (await call("keepfold_read", {"id": "profile"}))["text"]
        hits = (await call("keepfold_find", {"query": "glimmerfrost"}))["hits"]
        assert [h["id"] for h in hits] == ["personality"]
        out = await call("keepfold_as_me", {"task": "Say hi", "audience": "friend"})
        assert out["self"]["withheld"] == 0

    run(root, body, "owner")


@pytest.mark.parametrize("role", ["reader"])
def test_non_owner_start_up_writes_nothing(root, role):
    # a concept added in Obsidian: an owner start-up adopts it into catalog.yaml
    (root / "concepts/new-thing.md").write_text(
        '---\nid: "new-thing"\ntitle: "New thing"\nparent: "[[dsa.arrays]]"\ndomain: "dsa"\n'
        "---\n# New thing\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "obsidian edit")
    head = git(root, "rev-parse", "HEAD")

    async def body(call):
        assert "hits" in await call("keepfold_find", {"query": "pointers"})

    run(root, body, role)
    assert git(root, "status", "--porcelain") == "" and git(root, "rev-parse", "HEAD") == head
    build_server(Vault(root), "owner")  # control: the owner's start-up does write
    assert "catalog.yaml" in (git(root, "status", "--porcelain")
                              + git(root, "log", "-1", "--stat"))
