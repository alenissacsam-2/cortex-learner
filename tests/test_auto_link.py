"""Unknown names a chat model logs become linked concepts (safety_net.Linker over MCP):
keepfold_log taught/self_report, felt_weak, problem_status concepts, an assessment's
fields.concepts and an application's assessments. vault_mini, a fake embedder, no model."""

import pytest
from conftest import events, run
from test_safety_net import KEY, Embed, hub

from keepfold import planner, semantic
from keepfold.catalog import Catalog
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")


def concepts(root):
    return {p.stem for p in (root / "concepts").glob("*.md")}


def taught(root):
    return [e["object"] for e in events(root) if e["kind"] == "taught"]


def test_taught_unknown_name_is_created_under_the_session_area_and_reused(root, monkeypatch):
    hub(root)
    monkeypatch.setattr(semantic, "_embedder", Embed())  # already in memory: used, not loaded
    outs = []

    async def body(call):
        await call("keepfold_grade", {"concept": "two-pointers", "question": "q",
                                    "learner_answer": "a", "verdict": "correct"})
        for _ in range(2):
            outs.append(await call("keepfold_log", {"kind": "taught", "payload": {
                "concept": "Sliding window", "summary": "a window that slides"}}))

    run(root, body)
    assert outs[0]["display_line"].startswith(
        "Created Sliding window under Data structures and algorithms › Arrays.")
    assert outs[0]["id"] == "sliding-window" and "id" not in outs[1]
    assert outs[1]["display_line"] == "Taught, not yet checked: Sliding window"
    e = Catalog.load(Vault(root)).entries["sliding-window"]
    assert e.parent == "dsa.arrays"  # the area the session's graded concept is in
    page = (root / "concepts" / "sliding-window.md").read_text(encoding="utf-8")
    assert "[[topic-windows]]" in page  # related: the hub topics.attach picked
    assert taught(root) == ["concept:sliding-window"] * 2
    assert not (root.parent / "home" / ".keepfold" / "models").exists()  # nothing downloaded


def test_no_session_area_and_no_embedder_goes_to_from_chats(root):
    async def body(call):
        await call("keepfold_log", {"kind": "self_report", "payload": {
            "concept": "OOP", "verdict": "partial"}})

    run(root, body)
    assert Catalog.load(Vault(root)).entries["oop"].parent == "from-chats.unsorted"
    [r] = [e for e in events(root) if e.get("source") == "self"]
    assert r["object"] == "concept:oop" and "predicted_p" in r


def test_near_duplicate_resolves_to_the_existing_concept(root):
    before = concepts(root)

    async def body(call):
        out = await call("keepfold_log", {"kind": "taught", "payload": {"concept": "Two pointer"}})
        assert out["display_line"] == "Taught, not yet checked: Two pointers"

    run(root, body)
    assert concepts(root) == before and taught(root) == ["concept:two-pointers"]


def test_junk_secret_and_hidden_names_create_nothing(root):
    before, cat = concepts(root), dict(Catalog.load(Vault(root)).entries)

    async def body(call):
        for name in ("programming", "Private concept", "my Private concept notes"):
            out = await call("keepfold_log", {"kind": "taught", "payload": {"concept": name}})
            assert "inbox.md" in out["display_line"] and "Created" not in out["display_line"]
        out = await call("keepfold_log", {"kind": "taught", "payload": {"concept": KEY}})
        assert "error" in out or "inbox" in out["display_line"]

    run(root, body)
    assert concepts(root) == before and Catalog.load(Vault(root)).entries.keys() == cat.keys()
    assert "concept:secret-diary" not in taught(root)
    inbox = (root / "inbox.md").read_text(encoding="utf-8")
    assert "programming" in inbox and KEY not in inbox


def test_felt_weak_name_becomes_a_concept(root):
    async def body(call):
        await call("keepfold_log", {"kind": "assessment_result", "payload": {
            "company": "Initech", "assessment_kind": "oa", "date": "2026-11-26",
            "outcome": "pending", "felt_weak": ["grid DP", "Two pointers"]}})

    run(root, body)
    [a] = [e for e in events(root) if e.get("payload", {}).get("kind") == "assessment_result"]
    assert a["payload"]["felt_weak"] == ["grid-dp", "two-pointers"]
    assert (root / "concepts" / "grid-dp.md").exists()


def test_assessment_concepts_are_tested_and_an_application_links_it(root):
    async def body(call):
        out = await call("keepfold_add", {"kind": "assessment", "title": "Hyland OA", "fields": {
            "company": "Hyland", "kind": "oa", "date": "2026-12-01", "outcome": "pending",
            "concepts": ["Two pointers", "Docker volumes", "help"],
            "tests": {"Array basics": 2}}})
        assert out["id"] == "hyland-oa"
        out = await call("keepfold_add", {"kind": "application", "title": "Hyland SDE",
                                        "fields": {"company": "Hyland", "role": "SDE"}})
        assert out["id"] == "hyland-sde"

    run(root, body)
    v = Vault(root)
    tests = v.read("assessment", "hyland-oa").fields["tests"]
    assert dict(tests) == {"concept:arrays-basics": 2, "concept:two-pointers": 1,
                           "concept:docker-volumes": 1}
    [a] = [a for a in planner.assessments(v) if a["id"] == "hyland-oa"]
    assert set(planner._weights(Catalog.load(v), a["tests"])) == {
        "arrays-basics", "two-pointers", "docker-volumes"}
    assert list(v.read("application", "hyland-sde").fields["assessments"]) == ["[[hyland-oa]]"]
    assert "help" in (root / "inbox.md").read_text(encoding="utf-8")


def test_problem_status_creates_an_unknown_problem_with_its_concepts(root):
    async def body(call):
        await call("keepfold_log", {"kind": "problem_status", "payload": {
            "problem": "Trapping Rain Water", "status": "attempted",
            "concepts": ["Two pointers", "Monotonic stack"]}})
        await call("keepfold_log", {"kind": "problem_status", "payload": {
            "problem": "Trapping Rain Water", "status": "solved"}})
        await call("keepfold_log", {"kind": "problem_status", "payload": {
            "problem": "lc-167", "status": "attempted"}})

    run(root, body)
    f = Vault(root).read("problem", "trapping-rain-water").fields
    assert list(f["concepts"]) == ["[[two-pointers]]", "[[monotonic-stack]]"]
    assert f["status"] == "solved"
    objs = [e["object"] for e in events(root) if e.get("payload", {}).get("kind") ==
            "problem_status"]
    assert objs == ["problem:trapping-rain-water"] * 2 + ["problem:lc-167"]
    assert not (root / "problems" / "trapping-rain-water-2.md").exists()
