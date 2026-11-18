"""v5 PHASE4.md V3: cortex_as_me, the one "act as me" read."""

import pytest
from conftest import events, run

from cortex import delegation
from cortex.cli import ME_PAGES
from cortex.events import EventLog
from cortex.mcp_server import AS_ME_NEXT
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

DELEGATION = "# Delegation\n\n## Alone\n\n## Ask first\n- Meetings\n\n## Never\n- Money\n"


def _patch(id, target, text, ok=True):
    return {"id": id, "op": "append_to_section", "target": target, "text": text,
            "user_confirmed": ok}


def test_as_me_serves_act_grade_voice_person_and_logs_access(root, monkeypatch):
    for page in ("voice", "goals"):
        (root / f"me/{page}.md").write_text(ME_PAGES[f"me/{page}.md"], encoding="utf-8")
    goals = root / "me/goals.md"
    goals.write_text(goals.read_text("utf-8").replace("---\n", "---\nshare: work\n", 1), "utf-8")
    (root / "me/delegation.md").write_text(DELEGATION, encoding="utf-8")
    prefs = root / "me/preferences.md"
    prefs.write_text("---\nshare: work\n---\n# Preferences\n\n## Communication\n",
                     encoding="utf-8")
    calls = []
    real = EventLog.iter_events

    def counted(self, *a, **k):
        calls.append(1)
        return real(self, *a, **k)

    async def body(call):
        await call("cortex_add", {"kind": "person", "title": "Dr Lee",
                                  "fields": {"relation": "professor"}})
        await call("cortex_patch", _patch("me/preferences", "Communication", "- Short emails"))
        await call("cortex_patch", _patch("me/goals", "Now", "- Finish the thesis", ok=False))
        await call("cortex_patch", _patch("me/voice", "professor", "- Signs Regards"))
        await call("cortex_patch", _patch("me/voice", "Everyone", "- No emoji"))
        await call("cortex_patch", _patch("me/voice", "friend", "- Says hey"))
        for ch, text in (("email", "Dear Prof, may I ask..."), ("ai", "fix my code")):
            await call("cortex_log", {"kind": "voice_sample", "payload": {
                "text": text, "audience": "professor", "act": "request", "channel": ch}})
        await call("cortex_log", {"kind": "need", "payload": {
            "question": "How do you sign emails to staff?", "area": "habits"}})

        monkeypatch.setattr(EventLog, "iter_events", counted)
        out = await call("cortex_as_me", {"task": "Ask Dr Lee for an extension",
                                          "audience": "people/dr-lee", "act": "request",
                                          "channel": "email"})
        assert len(calls) == 1  # one event read per call
        monkeypatch.setattr(EventLog, "iter_events", real)
        me = out["self"]
        assert [(r["page"], r["section"]) for r in me["lines"]] == [
            ("preferences", "Communication")]
        assert "share" not in me["lines"][0]
        assert me["lines"][0]["text"].startswith('<untrusted_note id="claim">')
        assert me["unconfirmed"] >= 1 and "/cortex reflect" in me["hint"]  # goals (share: work)
        assert me["withheld"] >= 1  # lines on self-tier pages (no share:) never leave
        assert [c["section"] for c in out["voice"]["card"]] == ["professor", "Everyone"]
        ex = out["voice"]["exemplars"]
        assert len(ex) == 1 and "Dear Prof" in ex[0]["text"] and ex[0]["channel"] == "email"
        assert ex[0]["text"].startswith('<untrusted_note id="voice">')
        assert out["person"]["id"] == "people/dr-lee" and out["person"]["relation"] == "professor"
        assert out["person"]["text"].startswith('<untrusted_note id="people/dr-lee">')
        b = out["boundaries"]
        assert "- Money" in b["never"] and "- Meetings" in b["ask"] and b["alone"] == ""
        u = out["unknowns"]
        assert any("How do you sign" in x for x in u) and any("Finish the thesis" in x for x in u)
        assert not any(x.startswith("no voice") for x in u)
        assert out["fidelity"]["voice"] == {"n": 0, "kept": None, "status": "unmeasured"}
        assert out["next"] == AS_ME_NEXT

        other = await call("cortex_as_me", {"task": "Cover letter", "audience": "recruiter"})
        assert "no voice samples for recruiter" in other["unknowns"]
        assert "no voice card for recruiter" in other["unknowns"]
        assert [c["section"] for c in other["voice"]["card"]] == ["Everyone"]
        assert len(other["self"]["lines"]) == 1 and "person" not in other
        assert len((await call("cortex_as_me", {"task": "x", "audience": "friend"}))[
            "self"]["lines"]) == 1  # work pages reach the close tier too
        public = (await call("cortex_as_me", {"task": "x", "audience": "public"}))["self"]
        assert public["lines"] == [] and public["withheld"] >= 2  # work pages, held back
        assert public["unconfirmed"] == 0 and "hint" not in public
        out = await call("cortex_as_me", {"task": "x", "audience": "public"})
        assert "Short emails" not in str(out) and "Finish the thesis" not in str(out)

        for bad in ({"task": "t" * 301, "audience": "friend"},
                    {"task": "x", "audience": "Dr Lee"},
                    {"task": "x", "audience": "friend", "act": "rant"},
                    {"task": "x", "audience": "friend", "channel": "fax"}):
            assert (await call("cortex_as_me", bad))["error"] == "invalid_argument"
        assert (await call("cortex_as_me", {"task": "x", "audience": "people/nobody"}))[
            "error"] == "unknown_id"
        delegation.set_kill(Vault(root), True)
        assert (await call("cortex_as_me", {"task": "x", "audience": "friend"}))[
            "error"] == "killed"
        delegation.set_kill(Vault(root), False)

    run(root, body)
    acc = [e["payload"] for e in events(root) if e.get("payload", {}).get("kind") == "access"]
    assert [(a["access"], a["audience"]) for a in acc] == [
        ("as_me", "people/dr-lee"), ("as_me", "recruiter"), ("as_me", "friend"),
        ("as_me", "public"), ("as_me", "public")]
    assert acc[0]["task"] == "Ask Dr Lee for an extension"


def test_as_me_without_confirmed_lines(root):
    async def body(call):
        out = await call("cortex_as_me", {"task": "Thank a friend", "audience": "friend"})
        assert out["self"]["lines"] == [] and out["self"]["withheld"] >= 1
        assert out["voice"] == {"card": [], "exemplars": []}
        assert out["boundaries"] == {"alone": "", "ask": "", "never": ""}
        assert {"no voice samples for friend", "no voice card for friend"} <= set(
            out["unknowns"])

    run(root, body)
