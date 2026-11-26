"""v5 Phase 1 claims: ids (F1), revise/retire/reject (F2), authorship tiers (F3), serving."""

import re
from datetime import UTC, datetime

import pytest
from conftest import run, tiers
from typer.testing import CliRunner

from keepfold import claims
from keepfold.cli import app
from keepfold.events import EventLog, local_day
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")


@pytest.fixture
def root(root_plain):
    return root_plain


ID = re.compile(r"^c-[a-z2-7]{5}$")


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))  # a hand edit: straight to disk, no Keepfold


def read(root, rel):
    return (root / rel).read_bytes().decode("utf-8")


def today():
    return local_day(datetime.now(UTC))


# ---- F1: ids --------------------------------------------------------------------

def test_id_format_stability_and_clash():
    a = claims.make_id("preferences", "Prefers short answers (confirmed 2026-11-14)", "2026-11-14")
    assert ID.match(a)
    assert a == claims.make_id("preferences", "  prefers SHORT   answers ", "2026-11-14")
    assert a != claims.make_id("preferences", "Prefers short answers", "2026-11-15")
    assert a != claims.make_id("goals", "Prefers short answers", "2026-11-14")
    b = claims.make_id("preferences", "Prefers short answers", "2026-11-14", {a})
    assert ID.match(b) and b != a


def test_parse_derived_ids_and_status():
    text = ("---\ntags:\n- x\n---\n# Prefs\n- not a claim\n\n## Coding\n- Vim\n- Vim\n"
            "- Tabs ^c-aaaaa\n### Sub\n- Nested section line\n\n## Past\n- Emacs (until 2026-01-01)"
            " ^c-bbbbb\n\n## Not me\n- Night owl (rejected 2026-01-02)\n")
    rows = claims.parse("preferences", text)
    assert [r["text"] for r in rows] == ["Vim", "Vim", "Tabs", "Nested section line",
                                         "Emacs (until 2026-01-01)",
                                         "Night owl (rejected 2026-01-02)"]
    assert [r["status"] for r in rows] == ["live"] * 4 + ["past", "not_me"]
    assert rows[0]["id"] == claims.make_id("preferences", "Vim", "")
    assert rows[1]["id"] != rows[0]["id"] and ID.match(rows[1]["id"])  # same text: next 5
    assert (rows[2]["id"], rows[2]["explicit_id"], rows[0]["explicit_id"]) == ("c-aaaaa", True,
                                                                               False)
    assert rows[3]["section"] == "Coding" and text.splitlines()[rows[2]["line_no"]].startswith(
        "- Tabs")
    # a derived id does not move when other lines are added
    again = claims.parse("preferences", text.replace("## Coding\n", "## Coding\n- New\n"))
    assert again[1]["id"] == rows[0]["id"]


# ---- F2 + F3 over MCP -------------------------------------------------------------

def test_ids_written_and_tiers(root):
    write(root, "me/preferences.md", "# Preferences\n\n## Coding\n- Old habit\n")
    write(root, "me/goals.md", "# Goals\n\n## Now\n")
    write(root, "raw/src.md", "outside text\n")
    pref = {"id": "me/preferences", "op": "append_to_section", "target": "Coding",
            "user_confirmed": True}
    # R3-2: an exposed yes on preferences waits in Review, so exposure is seen on goals lines
    goal = {"id": "me/goals", "op": "append_to_section", "target": "Now"}

    async def body(call):
        out = await call("keepfold_patch", {**pref, "text": "- Vim ^c-zzzzz"})  # model id ignored
        assert ID.match(out["claim"]) and out["claim"] != "c-zzzzz"
        await call("keepfold_patch", {**goal, "text": "Ship v5"})
        await call("keepfold_read", {"id": "raw/src.md"})
        await call("keepfold_patch", {**goal, "text": "- Tabs"})
        assert (await call("keepfold_patch", {**pref, "text": "- Spaces"}))["changed"] is True
        await call("keepfold_end")
        await call("keepfold_read", {"id": "raw/src.md"})  # no session: marks the next ambient
        await call("keepfold_patch", {**goal, "text": "- Fish shell"})
        await call("keepfold_end")
        await call("keepfold_patch", {**goal, "text": "- Dark mode"})  # v6 T2: a close keeps it
        await call("keepfold_start", {"mode": "capture"})
        await call("keepfold_patch", {**goal, "text": "- Light mode"})

    run(root, body)
    prefs = read(root, "me/preferences.md")
    assert re.search(r"^- Vim \(confirmed \d{4}-\d\d-\d\d\) \^c-[a-z2-7]{5}$", prefs, re.M)
    assert re.search(r"^- Ship v5 \^c-[a-z2-7]{5}$", read(root, "me/goals.md"), re.M)
    v = Vault(root)
    for _ in range(2):  # the first fold records the baseline; the second finds it
        claims.tiers(v, EventLog(v).iter_events(), EventLog(v))
    write(root, "me/preferences.md", prefs + "- Hand typed\n")
    assert tiers(root) == {"Old habit": "legacy", "Vim": "confirmed", "Ship v5": "agent",
                           "Tabs": "exposed", "Fish shell": "exposed", "Dark mode": "exposed",
                           "Light mode": "exposed", "Hand typed": "typed", "Spaces": "exposed",
                           "Test learner, synthetic.": "legacy"}  # the fixture profile
    evs = list(EventLog(Vault(root)).iter_events())
    assert sum(e.get("payload", {}).get("note") == "claims_baseline" for e in evs) == 1
    ch = [e["payload"]["change"] for e in evs if e["kind"] == "change"
          and "change" in e["payload"] and e["payload"]["change"].get("claim")]
    assert len(ch) == 7 and all(c["tier"] in claims.TIERS for c in ch)


def test_revise_retire_reject_and_not_me(root):
    write(root, "me/preferences.md",
          "---\nsummary: x\n---\n# Preferences\n\n## Coding\n- Vim (confirmed 2026-01-01)\n"
          "- Tabs\n\n## Personal\n")
    write(root, "me/goals.md", "# Goals\n\n## Now\n- Pass the OA\n")
    rows = claims.parse("preferences", read(root, "me/preferences.md"))
    vim, tabs = rows[0]["id"], rows[1]["id"]
    goal = claims.parse("goals", read(root, "me/goals.md"))[0]["id"]
    d = today()

    async def body(call):
        p = {"id": "me/preferences", "target": vim}
        bad = await call("keepfold_patch", {**p, "op": "revise", "text": "Neovim"})
        assert bad["error"] == "invalid_argument" and "user_confirmed" in bad["message"]
        new = (await call("keepfold_patch", {**p, "op": "revise", "text": "- Neovim",
                                           "user_confirmed": True}))["claim"]
        assert ID.match(new) and new != vim
        assert (await call("keepfold_patch", {**p, "op": "retire", "target": f"^{tabs}"}))[
            "claim"] == tabs
        gone = await call("keepfold_patch", {**p, "op": "retire", "target": tabs})
        assert gone["error"] == "invalid_argument"
        g = await call("keepfold_patch", {"id": "me/goals", "op": "revise", "target": goal,
                                        "text": "Pass the OA with 3/3"})
        assert ID.match(g["claim"])
        no = await call("keepfold_patch", {"id": "me/portrait", "op": "retire", "target": vim})
        assert no["error"] == "invalid_argument"
        q = {"id": "me/preferences", "op": "append_to_section", "target": "Personal",
             "text": "Night owl"}
        write(root, "inbox.md", "# Inbox\n\n## About me (unconfirmed)\n- Personal: Night owl "
              f"(proposed {d}, me/preferences)\n")  # as an older Keepfold queued it (D3)
        rej = await call("keepfold_patch", {"id": "me/preferences", "op": "reject",
                                          "target": "Personal: Night owl"})
        assert rej["changed"] is True and "claim" not in rej
        again = await call("keepfold_patch", {**q, "text": "- night   OWL"})
        assert again["queued"] is False and "Not me" in again["summary"]
        ctx = (await call("keepfold_context"))["context"]
        assert "Neovim" in ctx and "Vim (" not in ctx and "Tabs" not in ctx
        assert "Night owl" not in ctx and "^c-" not in ctx and "(since" not in ctx  # R13a

    run(root, body)
    prefs = read(root, "me/preferences.md")
    new = claims.parse("preferences", prefs)[0]
    # R13a: a line that replaced another says since when it holds
    assert new["section"] == "Coding" and new["text"] == f"Neovim (confirmed {d}) (since {d})"
    assert prefs.startswith("---\nsummary: x\n---\n")
    assert f"## Past\n- Vim (confirmed 2026-01-01) (until {d} → ^{new['id']}) ^{vim}\n" \
           f"- Tabs (until {d}) ^{tabs}\n" in prefs
    assert f"## Not me\n- Night owl (rejected {d})\n" in prefs
    assert "Night owl" not in read(root, "inbox.md")
    goals = read(root, "me/goals.md")
    assert f"## Now\n- Pass the OA with 3/3 (since {d}) ^c-" in goals \
        and f"(until {d} → ^c-" in goals


def test_serving_newest_first_never_past_or_not_me(root):
    write(root, "me/preferences.md",
          "# Preferences\n\n## Coding\n- old one ^c-aaaaa\n- new one ^c-bbbbb\n\n## Past\n"
          "- gone (until 2026-01-01) ^c-ccccc\n\n## Not me\n- never (rejected 2026-01-01)\n"
          "\n## Personal\n" + "".join(f"- pref {i}\n" for i in range(400)))
    write(root, "me/personality.md",
          "# Me\n\n## Communication\n- Terse first\n- Terse later\n\n## Past\n- Old trait\n")
    write(root, "me/goals.md", "# Goals\n\n## Now\n- Ship Phase 1\n- Ship Phase 2\n\n"
                               "## Later\n- Learn Rust\n\n## Past\n- Old goal\n")

    async def body(call):
        ctx = (await call("keepfold_context"))["context"]
        assert len(ctx) <= 2400
        assert ctx.index("new one") < ctx.index("old one") and "^c-" not in ctx
        assert ctx.index("Terse later") < ctx.index("Terse first")
        assert ctx.index("Ship Phase 2") < ctx.index("Ship Phase 1")
        for hidden in ("gone", "never", "Old trait", "Old goal", "Learn Rust", "pref 0\n"):
            assert hidden not in ctx
        assert "pref 399" in ctx  # the cap trims the oldest lines
        assert '<untrusted_note id="me/goals">' in ctx
        coding = (await call("keepfold_context", {"scope": "coding"}))["context"]
        assert "Ship Phase" not in coding and "new one" in coding
        home = (await call("keepfold_start"))["preferences"]
        assert "Ship Phase 2" in home and "never" not in home

    run(root, body)


def test_own_until_marker_expires_on_read_and_lints(root):
    """Agenda 4: a live line's `(until DAY)` stops it being served after DAY (DAY itself still
    is); nothing moves until the user retires it, which lint suggests."""
    from keepfold import export_context, lint
    d = today()
    EventLog(Vault(root)).append(session="cli", kind="change", source="tutor", fields={
        "payload": {"note": "claims_baseline", "claims": {}}})  # hand lines: tier typed
    write(root, "me/preferences.md",
          "---\nshare: close\n---\n# Preferences\n\n## Communication\n"
          "- Old exam mode (until 2020-01-01) ^c-aaaaa\n"
          f"- Today only (until {d}) ^c-bbbbb\n- Far future (until 2999-01-01) ^c-ccccc\n"
          "- Plain line ^c-ddddd\n\n## Past\n- Gone (until 2019-01-01) ^c-eeeee\n")
    before = read(root, "me/preferences.md")
    assert claims.expired("x (until 2020-01-01)", d) == "2020-01-01"
    assert claims.expired(f"x (until {d})", d) is None and claims.expired("x", d) is None

    async def body(call):
        for out in ((await call("keepfold_context"))["context"],
                    (await call("keepfold_prime", {"prompt": "exam mode today"}))["context"],
                    str((await call("keepfold_as_me", {"task": "x", "audience": "friend"}))[
                        "self"])):
            assert "Old exam mode" not in out and "Gone" not in out
            assert "Today only" in out and "Far future" in out and "Plain line" in out
        added = await call("keepfold_patch", {"id": "me/preferences", "op": "append_to_section",
                                            "target": "Communication", "user_confirmed": True,
                                            "text": "- Added (until 2999-02-02)"})
        assert ID.match(added["claim"])

    run(root, body)
    after = read(root, "me/preferences.md")
    assert after.startswith(before.split("\n\n## Past")[0])  # nothing moved or rewritten
    assert "- Added (until 2999-02-02) (confirmed " in after  # the marker survives a write
    v = Vault(root)
    snap, _ = export_context.build(v, list(EventLog(v).iter_events()), datetime.now(UTC))
    assert "Old exam mode" not in snap and "Plain line" in snap
    found = [f for f in lint.lint_report(v)[0] if f["kind"] == "expired_claim"]
    assert len(found) == 1 and found[0]["id"] == "me/preferences"
    assert "Old exam mode" in found[0]["detail"] and "target='^c-aaaaa'" in found[0]["detail"]
    text, cid = claims.move_to_past(after, "preferences", "c-aaaaa", d)
    assert cid == "c-aaaaa" and "- Old exam mode (until 2020-01-01) ^c-aaaaa\n" in \
        text.split("## Past")[1]  # its own day dates the Past line, once


def test_claims_cli_and_doctor(root):
    write(root, "me/goals.md", "# Goals\n\n## Now\n- Ship <b>it</b>\n")
    cli = CliRunner()
    out = cli.invoke(app, ["claims", "--vault", str(root)])
    assert out.exit_code == 0 and "legacy" in out.output and "me/goals § Now" in out.output
    assert "<b>" not in out.output
    write(root, "me/goals.md", "# Goals\n\n## Now\n- Ship <b>it</b>\n- Typed later\n")
    typed = cli.invoke(app, ["claims", "--tier", "typed", "--vault", str(root)])
    assert "Typed later" in typed.output and "Ship" not in typed.output
    assert cli.invoke(app, ["claims", "--tier", "nope", "--vault", str(root)]).exit_code != 0
    doc = cli.invoke(app, ["doctor", "--vault", str(root)])
    # legacy: the goal and the fixture profile's `## Brief` line
    assert "Claim tiers: confirmed 0, agent 0, exposed 0, typed 1, legacy 2" in doc.output


def test_identical_lines_get_distinct_ids():
    text = "## Likes\n" + "- filler line\n" * 40
    ids = [r["id"] for r in claims.parse("preferences", text)]
    assert len(set(ids)) == 40


def test_confirming_an_inferred_line_replaces_it(root):
    write(root, "me/preferences.md", "# Preferences\n\n## Coding\n")
    p = {"id": "me/preferences", "op": "append_to_section", "target": "Working with AI",
         "text": "Short answers (from a chat on 2026-11-07)"}

    async def body(call):
        guess = await call("keepfold_patch", p)  # D3: saved at once, as a guess
        assert guess["changed"] is True and "queued" not in guess
        assert (await call("keepfold_reflect"))["unconfirmed"] == []
        out = await call("keepfold_patch", {**p, "user_confirmed": True})
        assert out["changed"] is True and [r["id"] for r in out["replaced"]] == [guess["claim"]]

    run(root, body)
    assert "## Working with AI\n- Short answers (from a chat on 2026-11-07) (confirmed" in read(
        root, "me/preferences.md")


# ---- v6 T4 + U3: what was saved about you -------------------------------------------

def test_saved_list_and_reflect_yes_promotes_only_when_clean(root):
    write(root, "me/goals.md", "# Goals\n\n## Now\n")
    write(root, "raw/src.md", "outside text\n")
    goal = {"id": "me/goals", "op": "append_to_section", "target": "Now"}

    def yes(cid):
        return {"kind": "reconfirm", "payload": {"claim": cid, "verdict": "yes"}}

    async def body(call):
        ship = (await call("keepfold_patch", {**goal, "text": "Ship v5"}))["claim"]
        other = (await call("keepfold_patch", {**goal, "text": "Agent <b>two</b>"}))["claim"]
        await call("keepfold_log", yes(ship))  # ambient: no promotion
        await call("keepfold_end")
        await call("keepfold_start", {"mode": "reflect"})
        saved = (await call("keepfold_reflect"))["saved"]
        assert [r["id"] for r in saved["claims"]] == [ship, other]
        assert saved["claims"][1]["text"].startswith('<untrusted_note id="claim">')
        assert saved["claims"][0]["undo"].startswith("keepfold undo s_")
        await call("keepfold_log", yes(ship))  # a clean, user-started reflect: promotes
        await call("keepfold_end")
        await call("keepfold_read", {"id": "raw/src.md"})
        planted = (await call("keepfold_patch", {**goal, "text": "Planted"}))["claim"]
        await call("keepfold_end")
        await call("keepfold_start", {"mode": "reflect"})  # starts exposed (read < 2 h ago)
        await call("keepfold_log", yes(other))
        await call("keepfold_log", yes(planted))

    run(root, body)
    assert {k: v for k, v in tiers(root).items() if k != "Test learner, synthetic."} == {
        "Ship v5": "confirmed", "Agent <b>two</b>": "agent", "Planted": "exposed"}
    rec = [e["payload"] for e in EventLog(Vault(root)).iter_events()
           if e["kind"] == "log" and e["payload"].get("kind") == "reconfirm"]
    assert [p.get("promote") for p in rec] == [None, True, None, None]
    out = CliRunner().invoke(app, ["saved", "--vault", str(root)])
    assert out.exit_code == 0 and "<b>" not in out.output
    new = CliRunner().invoke(app, ["claims", "--unasked", "--vault", str(root)])  # review r4 S1
    assert new.exit_code == 0 and new.output == out.output
    lines = out.output.splitlines()
    assert lines[0] == "Claim lines saved unasked: 2"
    assert "from outside text" in lines[1] and "Planted" in lines[1]
    assert lines[2].strip().startswith("undo: keepfold undo s_")
    assert "unasked" in lines[3] and "Agent" in lines[3]
    assert "Ship" not in out.output.split("me/ changes")[0]
    assert "append_to_section Now" in out.output  # Ship v5's write, now a confirmed line


def change(cid, tier, page):
    return {"kind": "change", "session": "s_x", "day": "2026-11-01",
            "payload": {"note": "patched", "change": {"id": page, "claim": cid, "tier": tier}}}


def test_a_clean_reflect_yes_promotes_an_exposed_line(root):
    """Review r4 M3: a yes in a clean, user-started reflect confirms an exposed line too."""
    write(root, "me/goals.md", "# Goals\n\n## Now\n- Planted ^c-aaaaa\n")
    v, ch = Vault(root), change("c-aaaaa", "exposed", "goals")
    yes = {"kind": "log", "id": "e2", "payload": {"kind": "reconfirm", "claim": "c-aaaaa",
                                                  "verdict": "yes", "promote": True}}

    def tier(evs):
        return {r["id"]: r["tier"] for r in claims.tiers(v, evs)}["c-aaaaa"]

    assert tier([ch]) == "exposed"
    assert tier([ch, yes]) == "confirmed"
    assert claims.saved(v, [ch, yes], today())["claims"] == []


def test_a_relayed_yes_from_the_cutoff_is_ask_until_a_clean_reflect_yes(root, monkeypatch):
    """A relayed yes from RELAYED_FROM on is served (ask) and is not act-grade (export,
    unasked list); earlier relayed and clicked yeses are the user's, served plain. A clean
    reflect yes stamps `reflected`, which is act-grade."""
    from keepfold import export_context, know, prime

    monkeypatch.setattr(claims, "RELAYED_FROM", "2026-11-17")
    write(root, "me/preferences.md", "# Preferences\n\n## Study\n- Early relayed ^c-aaaaa\n"
          "- Late relayed ^c-bbbbb\n- Late clicked ^c-ccccc\n")
    v, now = Vault(root), datetime(2026, 11, 20, 12, tzinfo=UTC)

    def ch(cid, day, how):
        return {"kind": "change", "session": "s_x", "day": day, "ts": f"{day}T10:00:00Z",
                "payload": {"note": "patched",
                "change": {"id": "preferences", "claim": cid, "tier": "confirmed",
                           "confirm": how}}}

    evs = [ch("c-aaaaa", "2026-11-10", "relayed"), ch("c-bbbbb", "2026-11-17", "relayed"),
           ch("c-ccccc", "2026-11-20", "clicked")]

    def labels(evs):
        served = prime.served_claims(v, evs)
        return {t: lab for (p, _, t), (lab, _) in served.items() if p == "preferences"}

    assert labels(evs) == {"Early relayed": "", "Late relayed": " (ask)",
                           "Late clicked": ""}
    assert [r["id"] for r in claims.saved(v, evs, "2026-11-20")["claims"]] == ["c-bbbbb"]
    text, skipped = export_context.build(v, evs, now)
    assert "Early relayed" in text and "Late clicked" in text and "(said)" not in text
    assert "Late relayed" not in text and skipped >= 1
    yes = {"kind": "log", "id": "e9", "day": "2026-11-20", "ts": "2026-11-20T11:00:00Z",
           "payload": {"kind": "reconfirm", "claim": "c-bbbbb", "verdict": "yes",
                       "promote": True}}
    row = next(r for r in claims.tiers(v, [*evs, yes]) if r["id"] == "c-bbbbb")
    assert (row["tier"], row["confirm"], claims.late_relay(row)) == (
        "confirmed", "reflected", False)
    assert labels([*evs, yes])["Late relayed"] == ""
    assert claims.saved(v, [*evs, yes], "2026-11-20")["claims"] == []
    assert know.report(v, [*evs, yes], "2026-11-20")["total"]["reflected"] == 1


def test_expired_claims_drop_out_of_every_consumer(root):
    """Review r4 L1: a line past its own (until …) day is not served by reflect's saved list,
    keepfold know or the claim resolver."""
    from keepfold import know
    from keepfold.vault import VaultError

    write(root, "me/preferences.md", "# Preferences\n\n## Study\n"
          "- prefers night study (until 2026-10-01) ^c-bbbbb\n- likes tea ^c-ccccc\n")
    v, day = Vault(root), "2026-11-16"
    agent = [change("c-bbbbb", "agent", "preferences")]
    assert "c-bbbbb" in {r["id"] for r in claims.tiers(v, agent)}  # no day: unchanged
    assert "c-bbbbb" not in {r["id"] for r in claims.tiers(v, agent, today=day)}
    assert claims.saved(v, agent, day)["claims"] == []
    assert claims.resolve(v, "c-bbbbb") == "c-bbbbb"
    with pytest.raises(VaultError):
        claims.resolve(v, "c-bbbbb", day)
    prefs = next(p for p in know.report(v, [], day)["pages"] if p["page"] == "preferences")
    assert prefs["live"] == 1


def test_act_grade():
    assert claims.act_grade({"tier": "typed"}, "") and claims.act_grade({"tier": "confirmed"}, "")
    assert not claims.act_grade({"tier": "agent"}, "")
    assert not claims.act_grade({"tier": "confirmed"}, " (ask)")


def test_an_old_setup_page_becomes_a_claim_page_and_keeps_every_line(root):
    """G2: me/setup.md from before it was a claim page (no ids, no `## Past` / `## Not me`)
    loads as is: every line gets a distinct id and is served, and a retire adds the tail."""
    old = ("---\nsummary: My setup\n---\n\n# My setup\n\n## Hardware\n- Laptop with an RTX 4060\n"
           "- 16 GB RAM\n- 16 GB RAM\n\n## Software\n- Windows 11 and VS Code\n")
    write(root, "me/setup.md", old)
    rows = [r for r in claims.rows(Vault(root)) if r["page"] == "setup"]
    assert [r["text"] for r in rows] == ["Laptop with an RTX 4060", "16 GB RAM", "16 GB RAM",
                                         "Windows 11 and VS Code"]
    assert len({r["id"] for r in rows}) == 4 and all(ID.match(r["id"]) for r in rows)
    assert all(r["status"] == "live" for r in rows) and read(root, "me/setup.md") == old
    assert {t: tier for t, tier in tiers(root).items() if t in old} == {
        "Laptop with an RTX 4060": "legacy", "16 GB RAM": "legacy",
        "Windows 11 and VS Code": "legacy"}  # no recorded baseline: the lines as they are

    async def body(call):
        ctx = (await call("keepfold_context", {"scope": "environment"}))["context"]
        assert "RTX 4060" in ctx and "Windows 11" in ctx and "^c-" not in ctx
        prime = (await call("keepfold_prime", {"prompt": "my laptop GPU"}))["context"]
        assert "- [Hardware] Laptop with an RTX 4060" in prime
        out = await call("keepfold_patch", {"id": "me/setup", "op": "retire",
                                          "target": rows[0]["id"]})
        assert out["claim"] == rows[0]["id"]

    run(root, body)
    text = read(root, "me/setup.md")
    assert text.startswith(old.split("## Hardware")[0])  # front matter and title untouched
    assert "## Hardware\n- 16 GB RAM\n- 16 GB RAM\n\n## Software\n- Windows 11 and VS Code\n" \
        in text
    assert f"## Past\n- Laptop with an RTX 4060 (until {today()}) ^{rows[0]['id']}\n" in text
    left = [r["id"] for r in claims.rows(Vault(root)) if r["page"] == "setup"
            and r["status"] == "live"]
    assert left == [r["id"] for r in rows[1:]]  # the untouched lines keep their ids
