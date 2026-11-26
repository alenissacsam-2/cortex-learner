"""Context related pages (phase4-wiki §7.1) and the reflect pack (§7.4)."""

import datetime as dt
import os
import time
from datetime import UTC, datetime

import anyio
import pytest
from mcp import Client
from typer.testing import CliRunner

from keepfold.cli import app
from keepfold.events import local_day
from keepfold.mcp_server import build_server
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")


@pytest.fixture
def root(root_plain):
    return root_plain



def note(root, folder, id, front="", body="", age_days=0):
    p = root / folder / f"{id}.md"
    p.parent.mkdir(exist_ok=True)
    p.write_text(f'---\nid: "{id}"\n{front}---\n{body}\n', encoding="utf-8")
    t = time.time() - age_days * 86400
    os.utime(p, (t, t))


def call_tool(root, name, args=None):
    out = {}

    async def main():
        async with Client(build_server(Vault(root))) as client:
            r = await client.call_tool(name, args or {})
            assert not r.is_error, r.content
            out.update(r.structured_content)

    anyio.run(main)
    return out


def test_context_related_from_fts_and_neighbours(root):
    for i in range(7):
        note(root, "notes", f"graph-{i}", f'title: "Graph idea {i}"\nsummary: "About graphs {i}"\n')
    note(root, "projects", "linked", 'title: "Linked project"\nsummary: "Hop one"\n'
         'related: ["[[graph-0]]"]\n')
    note(root, "notes", "hidden", 'title: "Graph hidden"\nprivate: true\n')
    ctx = call_tool(root, "keepfold_context", {"topic": "graph"})["context"]
    assert ctx.count("- [[") <= 5 and "- [[graph-" in ctx and "hidden" not in ctx
    assert '<untrusted_note id="related">' in ctx and len(ctx) <= 2400
    # a topic with one FTS hit pulls in its linked neighbour
    note(root, "notes", "lonely", 'title: "Zebra stripes"\nrelated: ["[[linked]]"]\n')
    ctx = call_tool(root, "keepfold_context", {"topic": "zebra"})["context"]
    assert "- [[lonely]] — Zebra stripes" in ctx and "- [[linked]] — Hop one" in ctx


def test_reflect_pack(root):
    today = dt.date.fromisoformat(local_day(datetime.now(UTC)))
    recent, older = (today - dt.timedelta(days=3)).isoformat(), (
        today - dt.timedelta(days=10)).isoformat()
    stale = (today - dt.timedelta(days=61)).isoformat()
    inbox = ("# Inbox\n- [ ] old idea (fleeting, 2020-01-01)\n\n## About me (unconfirmed)\n"
             f"- Coding: likes vim (proposed {older})\n- Coding: likes vim (proposed {recent})\n"
             f"- Mood: grumpy mornings (proposed {stale})\n- Undated guess\n\n## Other\n"
             "- not this\n")
    (root / "inbox.md").write_text(inbox, encoding="utf-8")
    for i in "abc":  # three recent notes sharing a tag and a link
        note(root, "notes", f"n-{i}", f'title: "Note {i}"\ntags: ["ml", "stats"]\n'
             'related: ["[[two-pointers]]"]\n')
    for i in "xy":  # a pair only
        note(root, "notes", f"p-{i}", 'title: "Pair"\ntags: ["solo", "duo"]\n')
    for i in "def":  # old notes sharing the same
        note(root, "notes", f"o-{i}", 'title: "Old"\ntags: ["old", "dusty"]\n', age_days=40)
    out = call_tool(root, "keepfold_reflect")
    # deduped (latest stamp kept), 60+ days old left out and counted, undated kept
    assert out["unconfirmed"] == [
        {"text": '<untrusted_note id="inbox">\nCoding: likes vim\n</untrusted_note>',
         "since": recent},
        {"text": '<untrusted_note id="inbox">\nUndated guess\n</untrusted_note>', "since": None}]
    assert out["unconfirmed_dropped"] == 1
    assert (root / "inbox.md").read_text("utf-8") == inbox  # user text is never deleted
    assert out["old_fleeting"][0]["text"].startswith('<untrusted_note id="inbox">')
    assert [c["members"] for c in out["emerging"]] == [["n-a", "n-b", "n-c"]]
    assert out["emerging"][0]["suggested_title"] and out["emerging"][0]["shared"]
    assert [f["since"] for f in out["old_fleeting"]] == ["2020-01-01"]
    assert "sessions" in out["week"]


def test_reflect_cli(root):
    r = CliRunner().invoke(app, ["reflect", "--vault", str(root)])
    assert r.exit_code == 0, r.output
    assert "Emerging clusters: 0" in r.output and "Week " in r.output
