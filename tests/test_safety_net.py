"""The chat safety net (keepfold.safety_net) on vault_mini with synthetic chat pages, a fake
runner and a fake embedder: the skip rules (private, hidden, old), what was logged already, layer 1
touches, layer 2 (match, create under a hub or in From chats, caps, junk), no model loaded,
the ledger's no-op second pass, undo, dry run and the CLI. Nothing real runs or loads."""

import json
import re
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from conftest import events, git, note
from test_app import app as app  # noqa: F401 (fixture)
from test_app import fakes as fakes  # noqa: F401 (fixture: nothing real runs or opens)
from test_app import vault as vault  # noqa: F401 (fixture)
from typer.testing import CliRunner

from keepfold import cli
from keepfold import safety_net as sn
from keepfold.catalog import Catalog
from keepfold.events import EventLog, local_day
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

NOW = datetime.now(UTC).replace(microsecond=0)
AT = NOW - timedelta(hours=2)  # the chats' turn time
WHEN = AT.astimezone().strftime("%Y-%m-%d %H:%M")
DAY = local_day(AT)
KEY = "sk-" + "a" * 24  # a secret pattern


class Embed:
    """Fake embedder: "sliding" and "window" on axis 0, "pasta" and "kitchen" on axis 1."""

    AXES = {"sliding": 0, "window": 0, "pasta": 1, "kitchen": 1}

    def __call__(self, texts):
        out = np.zeros((len(texts), 4), dtype=np.float32)
        for k, t in enumerate(texts):
            for w in re.findall(r"\w+", t.lower()):
                if w in self.AXES:
                    out[k, self.AXES[w]] += 1
        return out


class Runner:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def chat(self, system, user, **kw):
        self.calls.append(user)
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)


def chat(r, id, *turns, when=WHEN, day=DAY, private=False):
    body = f"# Chat {id}\n\nTimeline: · Source: Claude · Date: {day} · My turns only\n\n"
    body += "".join(f"### {when}\n**Me:** {t}\n\n" for t in turns)
    note(r, f"resources/{id}.md", f'title: "Chat {id}"\nkind: resource\nresource_type: other\n'
         + ("private: true\n" if private else ""), body)


def hub(r):
    """A topic hub `topic-windows` with three member pages on axis 0."""
    note(r, "notes/topic-windows.md", 'title: "Window tricks"\nkind: note\ntags: ["topic"]\n'
         "related: []\n", "# Window tricks\n")
    for k in range(3):
        note(r, f"resources/win-{k}.md", f'title: "Win {k}"\nkind: resource\n'
             'related: ["[[topic-windows]]"]\n', f"# Win {k}\n\nsliding window notes {k}\n")


def mine(root):
    return [e for e in events(root) if e.get("source") == "safety_net"]


def go(root, runner=None, embed=None, **kw):
    return sn.run(Vault(root), runner, embed, now=NOW, **kw)


# ---- skip rules ---------------------------------------------------------------------------

def test_layer1_touches_once_and_a_second_pass_is_a_no_op(root):
    chat(root, "c1", "How do two pointers work on an array?", "And array basics again?")
    out = go(root)
    assert out["chats"] == 1 and out["touches"] == 2
    [e] = mine(root)
    assert e["kind"] == "log" and e["session"] == "import"
    assert e["payload"] == {"kind": "touch", "ids": ["two-pointers", "arrays-basics"],
                            "auto": "safety_net", "chat": "c1"}
    assert "Safety net: from 1 chat, 0 new concepts, 0 taught, 2 touches" in \
        (root / "journal" / f"{local_day(NOW)}.md").read_text(encoding="utf-8")
    assert go(root) == {}  # the ledger: stats only
    with open(root / "resources" / "c1.md", "a", encoding="utf-8") as f:
        f.write(f"### {WHEN}\n**Me:** Two pointers once more, then 2 pointers\n")
    assert go(root).get("touches", 0) == 0  # grown, but each page once per chat
    assert len(mine(root)) == 1


def test_a_chat_with_some_learning_logged_still_gets_the_rest(root):
    chat(root, "c1", "Teach me two pointers, then heap sort and array basics")
    EventLog(Vault(root)).append(session="cli", kind="taught", source="tutor",
                                 fields={"object": "concept:two-pointers"},
                                 now=AT + timedelta(minutes=10))
    out = go(root, Runner({"concepts": ["Two pointers", "Heap sort"]}))
    assert out["chats"] == 1 and out["taught"] == 1 and out["new_ids"] == ["heap-sort"]
    [touch] = [e for e in mine(root) if e["kind"] == "log"]
    assert touch["payload"]["ids"] == ["arrays-basics"]  # two-pointers was logged already
    assert [e["object"] for e in mine(root) if e["kind"] == "taught"] == ["concept:heap-sort"]
    cat = Catalog.load(Vault(root))
    assert cat.entries["heap-sort"].parent == "dsa.arrays"  # logged + matched concepts' area


def test_learning_outside_the_chat_span_does_not_skip(root):
    chat(root, "c1", "Teach me two pointers")
    EventLog(Vault(root)).append(session="cli", kind="taught", source="tutor",
                                 fields={"object": "concept:two-pointers"},
                                 now=AT - timedelta(hours=3))
    assert go(root)["touches"] == 1


def test_private_hidden_old_and_coding_pages_are_skipped(root):
    chat(root, "c-private", "two pointers in my diary", private=True)
    chat(root, "c-hidden", "two pointers and my Private concept")  # secret-diary is private
    old = (AT - timedelta(days=30)).astimezone()
    chat(root, "c-old", "two pointers", when=old.strftime("%Y-%m-%d %H:%M"),
         day=old.date().isoformat())
    note(root, "resources/c-code.md", 'title: "proj: fix"\nkind: resource\n',
         f"# proj: fix\n\nTimeline: · Source: Claude Code · Date: {DAY}\n\n"
         "Coding session in proj: two pointers\n")
    out = go(root, Runner({"concepts": ["Two pointers"]}), Embed())
    assert out == {"skipped_private": 1, "skipped_hidden": 1, "skipped_old": 1,
                   "skipped_not_chat": 1}
    assert mine(root) == []


# ---- layer 2 ------------------------------------------------------------------------------

def test_layer2_matches_existing_and_creates_new_under_the_hub(root):
    hub(root)
    chat(root, "c1", "Explain the sliding window idea next to two pointers",
         "and how a kitchen timer works")
    run = Runner({"concepts": ["Two pointers", "Sliding window", "programming",
                               "Quantum chromodynamics", KEY, "x" * 70]})
    out = go(root, run, Embed())
    assert out["taught"] == 2 and out["new"] == 1 and out["new_ids"] == ["sliding-window"]
    assert run.calls and "sliding window" in run.calls[0]
    cat = Catalog.load(Vault(root))
    assert cat.entries["sliding-window"].parent == "dsa.arrays"  # the chat's matched area
    page = (root / "concepts" / "sliding-window.md").read_text(encoding="utf-8")
    assert "[[topic-windows]]" in page  # related: the hub topics.attach picked
    taught = [e for e in mine(root) if e["kind"] == "taught"]
    assert [e["object"] for e in taught] == ["concept:two-pointers", "concept:sliding-window"]
    assert all(e["payload"] == {"kind": "taught", "auto": "safety_net", "chat": "c1"}
               for e in taught)
    [touch] = [e for e in mine(root) if e["kind"] == "log"]
    assert set(touch["payload"]["ids"]) == {"two-pointers", "topic-windows"}  # name + vector
    journal = (root / "journal" / f"{local_day(NOW)}.md").read_text(encoding="utf-8")
    assert "(new: [[sliding-window]])" in journal and "(taught: [[two-pointers]])" in journal


def test_layer2_puts_a_concept_with_no_area_in_from_chats(root):
    chat(root, "c1", "What is heap sort and why is it in place?")
    out = go(root, Runner('Sure! {"concepts": ["Heap sort"]}'))
    assert out["new_ids"] == ["heap-sort"]
    cat = Catalog.load(Vault(root))
    assert cat.entries["heap-sort"].parent == "from-chats.unsorted"
    assert cat.entries["from-chats"].title == "From chats"


def test_layer2_semantic_match_and_did_you_mean(root):
    hub(root)
    note(root, "concepts/window-sliding.md", 'title: "Window sliding"\nparent: "[[dsa.arrays]]"\n'
         'domain: "dsa"\n', "# Window sliding\n")
    Catalog.load(Vault(root)).add("concept", "Window sliding", "dsa.arrays")  # adopts the note
    chat(root, "c1", "the sliding window trick", "two-pointer techniques")
    out = go(root, Runner({"concepts": ["Sliding window", "Two-pointer techniques"]}), Embed())
    assert out.get("new", 0) == 0
    taught = sorted(e["object"] for e in mine(root) if e["kind"] == "taught")
    assert taught == ["concept:two-pointers", "concept:window-sliding"]


def test_layer2_caps_per_chat_and_per_pass(root, monkeypatch):
    for k in range(12):
        chat(root, f"c{k:02}", f"two pointers, heap sort, merge sort, quick sort, radix sort {k}")
    run = Runner({"concepts": ["Heap sort", "Merge sort", "Quick sort", "Radix sort",
                               "Two pointers"]})
    out = go(root, run)
    assert len(mine(root)) <= sn.MAX_EVENTS and out["left"] > 0
    per_chat = {}
    for e in mine(root):
        if e["kind"] == "taught":
            per_chat[e["payload"]["chat"]] = per_chat.get(e["payload"]["chat"], 0) + 1
    assert per_chat and max(per_chat.values()) <= sn.PER_CHAT
    for k in range(12, 24):
        chat(root, f"c{k:02}", f"two pointers {k}")
    monkeypatch.setattr(sn, "MAX_EVENTS", 1000)
    out = go(root, run)
    assert out["chats"] == sn.MAX_CHATS and out["left"] > 0  # the chat cap


def test_layer2_taught_is_capped_per_keepfold_day_across_chats_and_passes(root):
    chat(root, "c1", "Explain heap sort and two pointers")
    out = go(root, Runner({"concepts": ["Heap sort"]}))
    assert out["taught"] == 1
    chat(root, "c2", "merge sort and quick sort and radix sort please")
    chat(root, "c3", "bubble sort and two pointers")
    run = Runner({"concepts": ["Merge sort", "Quick sort", "Radix sort", "Bubble sort"]})
    out = go(root, run)  # the next pass: one taught left today, then no model call at all
    assert out["taught"] == 1 and len(run.calls) == 1 and out["touches"] >= 1
    taught = [e for e in mine(root) if e["kind"] == "taught"]
    assert len(taught) == sn.TAUGHT_PER_DAY and {e["day"] for e in taught} == {local_day(NOW)}


def test_junk_names_are_refused():
    text = "binary search on a sorted list and the heap"
    assert sn.clean_name("Binary search", text) == "Binary search"
    assert sn.clean_name("  heap.  ", text) == "heap"
    for bad in ("programming", "help", "the", "a" * 70, "one two three four five six seven",
                KEY, "[[x]]", "http://x.y", "Quantum gravity", 42, "binary\nsearch"):
        assert sn.clean_name(bad, text + " gravity " + KEY) is None or bad == "Quantum gravity"
    assert sn.clean_name("Quantum gravity", text) is None  # not in the chat
    chat_ = sn.Chat("c", "t", [text], None, None, DAY)
    assert sn.ask_model(Runner("no json here"), chat_) == []
    assert sn.ask_model(Runner({"concepts": "Binary search"}), chat_) == []
    assert sn.ask_model(Runner({"concepts": ["Binary search", 3]}), chat_) == []


def test_no_model_skips_layer2_until_one_is_loaded(root):
    chat(root, "c1", "Explain heap sort and two pointers")
    assert go(root)["touches"] == 1
    assert go(root) == {}  # no model: the chat waits, stats only
    out = go(root, Runner({"concepts": ["Heap sort"]}))
    assert out["taught"] == 1 and out.get("touches", 0) == 0
    assert go(root, Runner({"concepts": ["Heap sort"]})) == {}


# ---- undo, dry run, CLI -------------------------------------------------------------------

def test_undo_voids_every_safety_net_event(root):
    chat(root, "c1", "Explain heap sort and two pointers")
    chat(root, "c2", "array basics please")
    go(root, Runner({"concepts": ["Heap sort"]}))
    n = len(mine(root))
    assert sn.undo(Vault(root), "c2") == 1
    assert sn.undo(Vault(root)) == n - 1
    voided = {e["ref"] for e in events(root) if e["kind"] == "void"}
    assert voided == {e["id"] for e in mine(root)}
    assert go(root, Runner({"concepts": ["Heap sort"]})) == {}  # the ledger keeps them done


def test_dry_run_writes_nothing(root):
    chat(root, "c1", "Explain heap sort and two pointers")
    before = sorted(p.relative_to(root) for p in root.rglob("*") if ".git" not in p.parts)
    out = go(root, Runner({"concepts": ["Heap sort"]}), dry_run=True)
    assert out["touches"] == 1 and out["new"] == 1
    assert sorted(p.relative_to(root) for p in root.rglob("*") if ".git" not in p.parts) == before


def test_cli(root, monkeypatch):
    chat(root, "c1", "Explain two pointers")
    monkeypatch.setattr(sn, "cli_tools", lambda: (None, None))
    runner = CliRunner()
    r = runner.invoke(cli.app, ["safety-net", "--dry-run", "--vault", str(root)])
    assert r.exit_code == 0, r.output
    assert "No local model is loaded" in r.output and "dry run" in r.output
    r = runner.invoke(cli.app, ["safety-net", "--vault", str(root)])
    assert r.exit_code == 0 and "1 touch" in r.output and mine(root)
    r = runner.invoke(cli.app, ["safety-net", "--undo", "--vault", str(root)])
    assert r.exit_code == 0 and "voided 1 safety-net event" in r.output


def test_the_app_pass_waits_for_jobs_and_commits(app, monkeypatch):  # noqa: F811
    chat(app.root, "c1", "Explain two pointers")
    monkeypatch.setattr(app.jobs, "running", lambda: True)
    assert app.safety_net_auto() is None and mine(app.root) == []
    monkeypatch.setattr(app.jobs, "running", lambda: False)
    assert app.safety_net_auto()["touches"] == 1  # no model loaded in tests: layer 1 only
    assert git(app.root, "log", "-1", "--format=%s").strip() == "keepfold: app import safety net"
    assert not app.safety_net_auto()  # nothing new (it only asks again for a loaded model)
