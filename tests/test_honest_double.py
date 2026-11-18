"""v5 PHASE2.md G1: the Honest Double check on cortex_recall and claim_flag feedback."""

import re

import numpy as np
import pytest
from conftest import events, run

from cortex import semantic

pytestmark = pytest.mark.usefixtures("home")

# The fake embedder: words on the same axis mean the same thing; other words count for nothing.
AXES = {"pointer": 0, "pointers": 0, "array": 1, "arrays": 1}


def fake_embed(texts):
    out = np.zeros((len(texts), 3), dtype=np.float32)
    for k, t in enumerate(texts):
        for w in re.findall(r"\w+", t.lower()):
            if w in AXES:
                out[k, AXES[w]] += 1
    return out


@pytest.fixture(autouse=True)
def embed(monkeypatch):
    monkeypatch.setattr(semantic, "_embedder", fake_embed)


TEXT = ("I am strong at two pointers. Arrays are easy for me.\n"
        "- I enjoy cooking. My secret diary is private.")


def verdicts(out):
    return {c["concept"]: c["verdict"] for c in out["checks"]}


def test_check_gives_a_verdict_per_concept_sentence(root):
    async def body(call):
        await call("cortex_log", {"kind": "self_report",
                                  "payload": {"concept": "two-pointers", "verdict": "correct"}})
        out = await call("cortex_recall", {"query": TEXT, "check": True})
        # cooking matches no concept; the private concept is not in the index
        assert [c["sentence"] for c in out["checks"]] == [
            "I am strong at two pointers.", "Arrays are easy for me."]
        assert verdicts(out) == {"two-pointers": "hedge", "arrays-basics": "unverified"}
        assert out["checks"][0]["state"] in ("learning", "needs_refresh")
        assert out["checks"][0]["title"] == "Two pointers"
        assert "hedge Learning" in out["next"] and "hits" in out
        plain = await call("cortex_recall", {"query": TEXT})
        assert "checks" not in plain

        await call("cortex_add", {"kind": "project", "title": "Pair finder", "fields": {
            "status": "active", "applies": ["[[two-pointers]]"]}})
        out = await call("cortex_recall", {"query": TEXT, "check": True})
        tp = out["checks"][0]
        assert tp["verdict"] == "ok" and tp["applied"] is True

    run(root, body)


def test_a_shipped_project_counts_as_applied(root):
    """Audit F3: `shipped` is a real project status (`done` is not)."""
    async def body(call):
        await call("cortex_add", {"kind": "project", "title": "Pair finder", "fields": {
            "status": "shipped", "applies": ["[[two-pointers]]"]}})
        out = await call("cortex_recall", {"query": TEXT, "check": True})
        assert verdicts(out)["two-pointers"] == "ok" and out["checks"][0]["applied"] is True

    run(root, body)


def test_claim_flags_feed_the_inbox_and_dismiss(root):
    async def body(call):
        flag = {"concept": "arrays-basics", "sentence": "Arrays are easy for me.", "fair": False}
        out = await call("cortex_log", {"kind": "claim_flag", "payload": flag})
        assert "error" not in out and "not be flagged again" in out["next"]
        check = await call("cortex_recall", {"query": "arrays  are EASY for me.", "check": True})
        assert verdicts(check) == {"arrays-basics": "dismissed"}  # normalised sentence matches

        fair = {"concept": "two-pointers", "sentence": "I master [[two-pointers]] fully.",
                "fair": True}
        out = await call("cortex_log", {"kind": "claim_flag", "payload": fair})
        assert "To prove" in out["next"]
        for bad in ({**fair, "fair": "yes"}, {**fair, "sentence": ""},
                    {**fair, "sentence": "x" * 4001}, {"sentence": "x", "fair": True}):
            assert (await call("cortex_log", {"kind": "claim_flag", "payload": bad}))["error"]
        assert (await call("cortex_reflect"))["claim_check"] == {"fair": 1, "unfair": 1}
        # a flag is never evidence: arrays-basics is still not started
        assert verdicts(await call("cortex_recall", {
            "query": "I like arrays.", "check": True})) == {"arrays-basics": "unverified"}

    run(root, body)
    inbox = (root / "inbox.md").read_text(encoding="utf-8")
    day = [e for e in events(root) if e["kind"] == "log"][-1]["day"]
    assert f'## To prove\n- Prove [[two-pointers]]: claimed "I master two-pointers fully." ' \
           f"(claim check {day})" in inbox
    flags = [e["payload"] for e in events(root)
             if e.get("payload", {}).get("kind") == "claim_flag"]
    assert all("concept" not in p and p["flagged"] for p in flags)


def test_check_limits(root):
    async def body(call):
        for text in ("word. " * 21, "x" * 4001):
            out = await call("cortex_recall", {"query": text, "check": True})
            assert out["error"] == "invalid_argument"
        ok = await call("cortex_recall", {"query": "word. " * 20, "check": True})
        assert ok["checks"] == []
        # a keyword-only hit ("two" in Two pointers, no embedding match) is no claim
        kw = await call("cortex_recall", {"query": "Two friends.", "check": True})
        assert kw["hits"] and kw["checks"] == []

    run(root, body)


def test_the_reranker_never_touches_the_check(root, monkeypatch):
    """Rerank reorders plain recall only; the per-sentence check keeps recall's own order."""
    from cortex.mcp_server import Cortex
    from cortex.vault import Vault

    before = Cortex(Vault(root)).cortex_recall(TEXT, check=True)["checks"]
    seen = []

    def reverse(query, texts):
        seen.append(query)
        return list(range(len(texts)))  # the last hit first

    monkeypatch.setattr(semantic, "_reranker", reverse)
    after = Cortex(Vault(root)).cortex_recall(TEXT, check=True)
    assert after["checks"] == before and before
    assert seen == [TEXT]  # the main search only, never a checked sentence
