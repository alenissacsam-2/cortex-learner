"""v5 PHASE4.md V1 and V2: voice samples, drafts, sent, the keep fold, the zone, the CLI and the
me/voice.md claim and confirm page."""

import json

import pytest
from conftest import events, run
from typer.testing import CliRunner

from cortex import claims, delegation, voice
from cortex.cli import ME_PAGES, app
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

SAMPLE = {"text": "Dear Prof. Lee,\nCould we meet on Friday?\nRegards, Sam",
          "audience": "professor", "act": "request", "channel": "email", "lang": "en"}


def test_voice_log_kinds_keep_fold_and_zone(root):
    seen = {}

    async def body(call):
        s = await call("cortex_log", {"kind": "voice_sample",
                                      "payload": {**SAMPLE, "id": "x", "day": "1999-01-01"}})
        assert s["display_line"] == "Voice sample saved"
        seen["sample"] = s["id"]
        for bad in ({**SAMPLE, "act": "rant"}, {**SAMPLE, "channel": "fax"},
                    {**SAMPLE, "audience": "Prof Lee"}, {**SAMPLE, "audience": "x" * 41},
                    {**SAMPLE, "text": " "}, {**SAMPLE, "text": "t" * 4001},
                    {**SAMPLE, "lang": "English"}):
            assert (await call("cortex_log", {"kind": "voice_sample",
                                              "payload": bad}))["error"] == "invalid_argument"
        leak = {**SAMPLE, "text": "my key is sk-ant-api03-" + "a" * 40}
        assert (await call("cortex_log", {"kind": "voice_sample",
                                          "payload": leak}))["error"] == "secret_detected"
        d = await call("cortex_log", {"kind": "draft", "payload": {
            "text": "Dear Professor Lee, could we meet Friday? Regards, Sam",
            "audience": "professor", "act": "request", "channel": "email"}})
        seen["draft"] = d["id"]
        sent = await call("cortex_log", {"kind": "sent", "payload": {
            "draft": d["id"], "text": "Dear Prof. Lee, could we meet on Friday? Regards, Sam"}})
        assert sent["display_line"].startswith("Sent text saved")
        assert (await call("cortex_log", {"kind": "sent", "payload": {
            "draft": "nope", "text": "x"}}))["error"] == "invalid_argument"
        assert (await call("cortex_find", {"query": "Friday"}))["hits"] == []

        delegation.set_kill(Vault(root), True)  # V5: no drafting while the switch is on
        killed = await call("cortex_log", {"kind": "draft", "payload": {
            "text": "x", "audience": "friend", "act": "reply", "channel": "chat"}})
        assert killed["error"] == "killed"
        delegation.set_kill(Vault(root), False)

    run(root, body)
    v = Vault(root)
    samples, drafts = voice.rows(v, voice.SAMPLES), voice.rows(v, voice.DRAFTS)
    assert [s["id"] for s in samples][0] == seen["sample"] and samples[0]["day"] != "1999-01-01"
    assert samples[0]["lang"] == "en" and samples[1]["from_draft"] == seen["draft"]
    assert samples[1]["audience"] == "professor" and samples[1]["act"] == "request"
    assert [d["id"] for d in drafts] == [seen["draft"]]
    k = voice.keep(v)["professor"]
    assert k["drafts"] == 1 and k["sent"] == 1 and 0.8 < k["kept"] < 1
    assert not any(e.get("payload", {}).get("text") for e in events(root))  # voice/ only
    # the zone: voice/ is no kind folder, so the index (find, recall, vectors) never sees it
    from cortex.mcp_server import Cortex
    assert not any("voice" in p.parts for p in Cortex(v).index._scan())


def test_exemplars_score_and_skip_ai(tmp_path):
    v = Vault(tmp_path)
    rows = [{"audience": "professor", "act": "request", "channel": "email", "text": "a"},
            {"audience": "professor", "act": "thanks", "channel": "chat", "text": "b"},
            {"audience": "friend", "act": "request", "channel": "email", "text": "c"},
            {"audience": "professor", "act": "request", "channel": "ai", "text": "d"},
            {"audience": "friend", "act": "reply", "channel": "sms", "text": "e"},
            {"audience": "professor", "act": "request", "channel": "email", "text": "f"}]
    voice.add(v, voice.SAMPLES, rows, __import__("datetime").datetime(2026, 11, 14))
    got = [s["text"] for s in voice.exemplars(v, {"professor"}, "request", "email")]
    assert got == ["f", "a", "b"]  # newest first on ties; ai and no-match never


def test_voice_cli_import_and_counts(root, tmp_path):
    f = tmp_path / "sent.txt"
    f.write_text("Hi Prof,\nthanks!\n---\nDear Prof,\nthank you.\n---\n\n", encoding="utf-8")
    args = ["--vault", str(root)]
    r = CliRunner().invoke(app, ["voice", "import", str(f), "--audience", "professor",
                                 "--act", "thanks", "--channel", "email", *args])
    assert r.exit_code == 0 and "Imported 2" in r.output, r.output
    assert [s["text"] for s in voice.rows(Vault(root), voice.SAMPLES)] == [
        "Hi Prof,\nthanks!", "Dear Prof,\nthank you."]
    f.write_text("fine\n---\ntoken ghp_" + "a" * 36 + "\n", encoding="utf-8")
    r = CliRunner().invoke(app, ["voice", "import", str(f), "--audience", "professor",
                                 "--act", "thanks", "--channel", "email", *args])
    assert r.exit_code == 1 and "secret_detected" in r.output
    r = CliRunner().invoke(app, ["voice", "import", str(f), "--audience", "professor",
                                 "--act", "nope", "--channel", "email", *args])
    assert r.exit_code == 1 and "act must be" in r.output
    assert len(voice.rows(Vault(root), voice.SAMPLES)) == 2  # all or none
    (root / voice.DRAFTS).write_text(json.dumps({"id": "d1", "audience": "professor",
                                                 "text": "x"}) + "\n", encoding="utf-8")
    r = CliRunner().invoke(app, ["voice", *args])
    assert r.exit_code == 0 and "professor" in r.output and "email" in r.output
    assert "drafts   1  sent   0  kept -" in r.output


def test_voice_page_is_a_claim_and_confirm_page(root):
    assert "## Everyone" in ME_PAGES["me/voice.md"] and "voice" in claims.PAGES
    (root / "me/voice.md").write_text(ME_PAGES["me/voice.md"], encoding="utf-8")
    rule = "Formal greeting, at most 5 sentences, signs Regards"

    async def body(call):
        p = await call("cortex_patch", {"id": "me/voice", "op": "append_to_section",
                                        "target": "professor", "text": rule})
        assert p["queued"] is True  # observe-then-confirm
        bad = await call("cortex_patch", {"id": "me/voice", "op": "append_to_section",
                                          "target": "Prof Lee", "text": rule,
                                          "user_confirmed": True})
        assert bad["error"] == "invalid_argument"
        ok = await call("cortex_patch", {"id": "me/voice", "op": "append_to_section",
                                         "target": "professor", "text": "- " + rule,
                                         "user_confirmed": True})
        assert ok["claim"].startswith("c-")
        ctx = await call("cortex_context", {})
        assert "Regards" not in ctx["context"]  # served only by cortex_as_me

    run(root, body)
    text = (root / "me/voice.md").read_text(encoding="utf-8")
    assert text.index("## professor") < text.index("## Past")
    assert f"- {rule} (confirmed " in text
    rows = [r for r in claims.tiers(Vault(root), events(root)) if r["page"] == "voice"]
    assert [(r["section"], r["tier"]) for r in rows] == [("professor", "confirmed")]
