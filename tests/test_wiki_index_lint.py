"""index.md, log.md and lint (docs/design/phase4-wiki.md §3, §4)."""

import os
from datetime import UTC, datetime, timedelta

import anyio
import pytest
from mcp import Client
from typer.testing import CliRunner

from cortex.cli import app
from cortex.dashboard import write_index
from cortex.lint import lint_report
from cortex.mcp_server import build_server
from cortex.session import SessionManager
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")


@pytest.fixture
def root(root_plain):
    return root_plain


T0 = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


def note(root, folder, id, front="", body=""):
    p = root / folder / f"{id}.md"
    p.parent.mkdir(exist_ok=True)
    p.write_text(f'---\nid: "{id}"\n{front}---\n{body}\n', encoding="utf-8")
    return p


def close_session(root, minutes):
    t = T0 + timedelta(minutes=minutes)
    m = SessionManager(Vault(root), clock=lambda: t)
    m.start()
    m.note("logged")  # a learning log: an empty or ledger-only session closes quietly
    m.close("")


def test_index_groups_and_excludes_private(root):
    note(root, "notes", "slip", 'title: "Slip"\nnote_type: "permanent"\nsummary: "An <b>idea</b>"\n'
         'related: ["[[two-pointers]]"]\n')
    note(root, "notes", "map", 'title: "Map"\nnote_type: "structure"\n')
    note(root, "notes", "hidden", 'title: "Hidden"\nprivate: true\n')
    note(root, "projects", "app", 'title: "App"\nstatus: "active"\nsummary: "Learner model"\n')
    write_index(Vault(root), now=T0)
    text = (root / "index.md").read_text(encoding="utf-8")
    assert "## Permanent notes\n- [[slip]] — An idea" in text and "<b>" not in text
    assert "## Structure notes\n- [[map]] — Map" in text  # title when no summary
    assert "## Projects\n- [[app]] — Learner model" in text
    assert "### " in text and "[[two-pointers]]" in text
    assert text.index("## Concepts") < text.index("## Permanent notes")
    assert "hidden" not in text and "secret-diary" not in text
    assert (root / "index.md").is_file() and "index.md" not in \
        (root / ".gitignore").read_text()


def test_log_md_appends_across_closes(root):
    close_session(root, 0)
    first = (root / "log.md").read_text(encoding="utf-8")
    assert first.endswith("## [2026-11-13] session | learn, 0 changes\n")
    assert not (root / "index.md").exists()  # built on demand by `cortex dashboard`, not close
    close_session(root, 60)
    second = (root / "log.md").read_text(encoding="utf-8")
    assert second.startswith(first) and second.count("] session |") == 2


def mcp(root, body):
    server = build_server(Vault(root))

    async def main():
        async with Client(server) as client:
            async def call(name, args=None):
                r = await client.call_tool(name, args or {})
                return r.structured_content

            await body(call)

    anyio.run(main)


def test_capture_log_line_and_validation(root):
    note(root, "resources", "clrs", 'title: "CLRS"\ncovers: []\n')

    async def body(call):
        r = await call("cortex_log", {"kind": "capture", "payload": {
            "resource": "clrs", "pages": ["two-pointers", "arrays-basics"]}})
        assert r["recorded"] == 1
        bad = await call("cortex_log", {"kind": "capture", "payload": {
            "resource": "clrs", "pages": "nope"}})
        assert bad["error"] == "invalid_argument"
        gone = await call("cortex_log", {"kind": "capture", "payload": {
            "resource": "nothere", "pages": []}})
        assert gone["error"] == "unknown_id"

    mcp(root, body)
    assert "## [" in (root / "log.md").read_text(encoding="utf-8")
    assert "] capture | CLRS (2 pages)\n" in (root / "log.md").read_text(encoding="utf-8")


def by_kind(findings, kind):
    return [f for f in findings if f["kind"] == kind]


def test_lint_each_rule(root):
    old = datetime.now() - timedelta(days=40)
    note(root, "notes", "lonely", 'title: "Lonely"\nnote_type: "fleeting"\n')
    note(root, "notes", "broken", 'title: "Broken"\nrelated: ["[[two-pointers]]"]\n',
         "See [[ghost-page]] and [[two-pointers]].")
    note(root, "notes", "long", 'title: "Long"\nnote_type: "permanent"\n'
         'related: ["[[two-pointers]]"]\n', "word " * 400)
    note(root, "notes", "stale-ref", 'title: "S"\nrelated: ["[[old-concept]]"]\n')
    note(root, "concepts", "old-concept", 'title: "Old"\nmerged_into: "[[two-pointers]]"\n'
         'parent: "[[two-pointers]]"\n')
    note(root, "resources", "book", 'title: "Book"\ncovers: []\nrelated: ["[[two-pointers]]"]\n')
    p = note(root, "projects", "app", 'title: "App"\nstatus: "active"\n'
             'related: ["[[two-pointers]]"]\n')
    os.utime(p, (old.timestamp(), old.timestamp()))
    note(root, "problems", "two-pointers", 'title: "Clash"\n')  # id clash with the concept
    day = (datetime.now() - timedelta(days=10)).date().isoformat()
    fresh = datetime.now().date().isoformat()
    (root / "inbox.md").write_text(
        f"# Inbox\n\n- [ ] Old thought (fleeting, {day})\n- [ ] New thought (fleeting, {fresh})\n"
        f"- [ ] Unknown (felt weak, {day})\n", encoding="utf-8")
    f = lint_report(Vault(root))[0]
    orphans = [x["id"] for x in by_kind(f, "orphan")]
    assert "lonely" in orphans and "broken" not in orphans and "stale-ref" not in orphans
    assert [x["id"] for x in by_kind(f, "missing_link")] == ["broken"]
    assert [x["id"] for x in by_kind(f, "resource_no_covers")] == ["book"]
    assert [x["id"] for x in by_kind(f, "stale_project")] == ["app"]
    assert [x["id"] for x in by_kind(f, "long_note")] == ["long"]
    assert [x["id"] for x in by_kind(f, "merged_related")] == ["stale-ref"]
    assert [x["id"] for x in by_kind(f, "id_clash")] == ["two-pointers"]
    assert [x["id"] for x in by_kind(f, "old_fleeting")] == ["inbox:old-thought"]
    assert not any(x["id"] == "secret-diary" for x in f)  # private notes are not reported


def test_lint_cap_and_tool_and_cli(root):
    for i in range(35):
        note(root, "notes", f"n{i}", f'title: "N{i}"\n')
    assert len(lint_report(Vault(root))[0]) == 30

    async def body(call):
        r = await call("cortex_reflect", {"action": "lint"})
        assert len(r["findings"]) == 30 and {"kind", "id", "detail"} == set(r["findings"][0])

    mcp(root, body)
    assert not (root / "log.md").exists()  # the tool is read-only
    out = CliRunner().invoke(app, ["lint", "--vault", str(root)])
    assert out.exit_code == 0 and "30 findings" in out.output
    assert "] lint | 30 findings\n" in (root / "log.md").read_text(encoding="utf-8")


# ---- §15.2 N7, N8, N9 ----------------------------------------------------------------------

def test_index_hides_private_areas_and_lists_journal_and_me(root):
    area = root / "areas/dsa.arrays.md"
    area.write_text(area.read_text(encoding="utf-8").replace(
        'kind: "area"\n', 'kind: "area"\nprivate: true\n'), encoding="utf-8")
    for day in range(1, 33):
        note(root, "journal", f"2026-10-{day:02d}" if day <= 31 else "2026-11-01")
    note(root, "me", "diary", 'title: "Diary"\nprivate: true\n')
    write_index(Vault(root), now=T0)
    text = (root / "index.md").read_text(encoding="utf-8")
    for hidden in ("two-pointers", "arrays-basics", "Arrays", "diary"):
        assert hidden not in text
    assert "## About me\n- [[profile]]" in text
    journal = text.split("## Journal\n")[1].splitlines()
    assert journal[0].startswith("- [[2026-11-01]]")  # newest first
    assert len(journal) == 30 and journal[-1].startswith("- [[2026-10-03]]")
    assert "[[2026-10-02]]" not in text


def test_lint_unlinked_mention(root):
    note(root, "notes", "talk", 'title: "Talk"\nrelated: ["[[arrays-basics]]"]\n',
         "Used TWO POINTERS here; see [[Two pointers]] later. Array basics too. "
         "Private concept stays quiet. two pointersx is no word match.")
    note(root, "notes", "linked", 'title: "Linked"\nrelated: ["[[two-pointers]]"]\n',
         "Two pointers again.")
    found = by_kind(lint_report(Vault(root))[0], "unlinked_mention")
    assert [(f["id"], f["detail"]) for f in found] == [
        ("talk", "mentions 'TWO POINTERS' without [[two-pointers]]")]


def test_lint_severity_order_caps_and_totals(root):
    from cortex.lint import lint_report, not_shown

    for i in range(40):  # 40 unlinked mentions, written before the broken link in vault order
        note(root, "notes", f"a{i:02d}", 'title: "A"\nrelated: ["[[arrays-basics]]"]\n',
             "Two pointers")
    note(root, "notes", "zz-broken", 'title: "Z"\n', "See [[ghost-page]].")
    day = (datetime.now() - timedelta(days=10)).date().isoformat()
    (root / "inbox.md").write_text(f"# Inbox\n\n- [ ] Old (fleeting, {day})\n", encoding="utf-8")
    f, totals = lint_report(Vault(root))
    kinds = [x["kind"] for x in f]
    assert kinds[0] == "missing_link" and "old_fleeting" in kinds
    assert kinds.index("old_fleeting") < kinds.index("unlinked_mention")
    assert kinds.count("unlinked_mention") == 10 and totals["unlinked_mention"] == 40
    assert "30 more unlinked_mention" in not_shown(f, totals)

    async def body(call):
        r = await call("cortex_reflect", {"action": "lint"})
        assert r["totals"]["unlinked_mention"] == 40 and "30 more unlinked_mention" in r["next"]

    mcp(root, body)


def test_lint_permanent_needs_summary_and_link_reasons(root):
    perm = 'note_type: "permanent"\nrelated: ["[[two-pointers]]"]\n'
    note(root, "notes", "good", f'title: "G"\nsummary: "One idea"\n{perm}',
         "# G\n\n## Links\n- [[two-pointers]] — the same shrink step\n")
    note(root, "notes", "bare", f'title: "B"\nsummary: "x"\n{perm}',
         "# B\n\n## Links\n- [[two-pointers]]\n- [[good]] — why\n")
    note(root, "notes", "empty", f'title: "E"\n{perm}', "# E\n\n## Links\n\n## Other\ntext\n")
    f = lint_report(Vault(root))[0]
    assert [x["id"] for x in by_kind(f, "no_summary")] == ["empty"]
    assert [(x["id"], x["detail"]) for x in by_kind(f, "links_no_reason")] == [
        ("bare", "a ## Links bullet has no reason"), ("empty", "## Links is empty")]


def test_lint_body_links_skip_code():
    from cortex.lint import _out_edges
    body = ("real [[a|x]] and `[[1,2]]`\n```cpp\nv[[3]];\n```\n~~~\n[[4]]\n~~~\n"
            "[[people/b#h]]\n")
    assert _out_edges({}, body) == (set(), {"a", "b"})


def test_lint_skips_imported_pages(root):
    """An imported chat, YouTube or NotebookLM page is filed by topic and its text is not the
    user's to link: no resource_no_covers and no unlinked_mention for it."""
    note(root, "resources", "chat-gemini-2026-05-01-hi", 'title: "Two pointers chat"\n',
         "Timeline: [[2026-05-01]] · Source: Gemini · Date: 2026-05-01\n\nTwo pointers again.")
    note(root, "resources", "youtube-channel-x", 'title: "X (YouTube channel)"\n',
         "Source: YouTube · Watched 5 videos\n\nTwo pointers tutorial.")
    note(root, "resources", "own-book", 'title: "Own book"\n', "Two pointers in here.")
    f = lint_report(Vault(root))[0]
    for kind in ("resource_no_covers", "unlinked_mention"):
        ids = [x["id"] for x in by_kind(f, kind)]
        assert "chat-gemini-2026-05-01-hi" not in ids and "youtube-channel-x" not in ids
    assert "own-book" in [x["id"] for x in by_kind(f, "resource_no_covers")]
