"""Harvest (docs/design/v6/arena/harvest-verdict.md): the user-turn parser, the queue and its
bundles, the duplicate fold, the `harvested` ledger, the guards while chats are served,
recall quieting, `cortex_read` exposure and `cortex eval harvest`. A synthetic vault of three
hubs and a dozen chat pages on vault_mini; FakeEmbed puts car words on one axis and kitchen
words on another, and two-pointers gets car words so it is the cars hub's concept."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeEmbed, events, git, note, tiers

from cortex import chats, harvest, semantic, topics
from cortex.catalog import Catalog
from cortex.events import local_day
from cortex.index import Index
from cortex.mcp_server import Cortex
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

HUBS = {"my-topic-map": ("My topic map", None), "area-study": ("Study", "my-topic-map"),
        "area-life": ("Life", "my-topic-map"), "cars": ("Cars", "area-study"),
        "food": ("Food", "area-life"), "health": ("Health", "area-life")}
KEY = "sk-" + "a" * 24  # a secret pattern, redacted when served


def hub(r, id, title, parent, know=True):
    rel = [f"[[{parent}]]"] if parent else []
    note(r, f"notes/{id}.md", f'title: "{title}"\nkind: note\nnote_type: structure\n'
         f'tags: ["topic"]\nrelated: {json.dumps(rel)}\n',
         f"# {title}\n\nHand text.\n" + ("\n## What I know\n- a line the import wrote\n"
                                         if know else ""))


def chat(r, id, turns, hub=None, day="2026-03-01", app="Gemini", reply="an answer",
         private=False):
    """An imported chat page: `turns` user turns, each answered by `app` (reply None: no
    answer, so the turn's end is ambiguous)."""
    rel = [f"[[{hub}]]"] if hub else []
    body = f"# Chat {id}\n\nTimeline: [[{day[:8]}01]] · Source: {app} · Date: {day}\n"
    body += (f"Topics: [[{hub}]]\n" if hub else "") + "\n"
    for k, t in enumerate(turns):
        body += f"### {day} 10:0{k}\n**Me:** {t}\n\n"
        if reply is not None:
            body += f"**{app}:** {reply} ASSISTANT-TEXT\n\nMore assistant lines.\n\n"
    note(r, f"resources/{id}.md", f'title: "Chat {id}"\nrelated: {json.dumps(rel)}\n'
         + ("private: true\n" if private else ""), body)


def build(r):
    for id, (title, parent) in HUBS.items():
        hub(r, id, title, parent, know=id != "food")
    p = r / "concepts/two-pointers.md"
    p.write_text(p.read_text(encoding="utf-8") + "- Car motor vehicle.\n", encoding="utf-8")
    chat(r, "chat-car-1", ["how does a car motor start"], "cars", "2026-03-01")
    chat(r, "chat-car-2", ["car gearbox basics", f"my key {KEY} car"], "cars", "2026-04-02",
         "Perplexity", reply="a long answer " * 20)
    chat(r, "chat-car-2-copy", ["car gearbox basics", f"my key {KEY} car"], "cars",
         "2026-04-02", "Perplexity")  # a shorter re-log of chat-car-2
    chat(r, "chat-car-3a", ["car brakes", "car pads"], "cars", "2026-04-09", "Perplexity")
    chat(r, "chat-car-3b", ["car brakes", "car tyres"], "cars", "2026-04-09", "Perplexity")
    chat(r, "chat-car-odd", ["car question with no answer"], "cars", "2026-02-01", reply=None)
    chat(r, "chat-car-private", ["car secret"], "cars", "2026-05-01", private=True)
    chat(r, "chat-food-1", ["pasta recipe for the kitchen"], "food", "2026-03-05")
    chat(r, "chat-health-1", ["kitchen diet question"], "health", "2026-03-06")
    chat(r, "chat-loose-1", ["a cooking question"], None, "2026-05-03")
    chat(r, "chat-loose-2", ["another car question"], None, "2026-06-04")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "chats")
    return r


@pytest.fixture
def vault(root):
    return build(root)


def index(r):
    v = Vault(r)
    return Index(v, Catalog.load(v))


def prep(r):
    idx = index(r)
    return idx, topics.tree(idx), topics.head_stack(idx, FakeEmbed())


def served(b):
    return [i for c in b["chats"] for i in (c["id"], *c["dupes"])]


# ---- 1. the user-turn parser ----------------------------------------------------------

def test_user_turns_stop_at_the_apps_own_speaker_line():
    body = ("Timeline: [[2026-03-01]] · Source: Claude · Date: 2026-03-01\n\n"
            "### 2026-03-01 10:00\n**Me:** first\nstill mine\n**Note:** my own bold label\n\n"
            "**Claude:** answer\n**Me:** not a turn start (inside the answer? no: a new turn)\n"
            "\n**Claude:** second answer\n### 2026-03-01 10:05\n")
    assert harvest.user_turns(body) == [
        "first\nstill mine\n**Note:** my own bold label",
        "not a turn start (inside the answer? no: a new turn)"]


@pytest.mark.parametrize("body", [
    "**Me:** no Timeline line\n\n**Gemini:** answer\n",  # no app: no speaker to stop at
    "Timeline: · Source: Gemini · Date: 2026-03-01\n\n**Me:** a\n\n**Me:** b\n\n**Gemini:** x\n",
    "Timeline: · Source: Gemini · Date: 2026-03-01\n\n**Me:** asked, never answered\n",
    "Timeline: · Source: Gemini · Date: 2026-03-01\n\n**Gemini:** an answer, no question\n",
    "Timeline: · Source: Gemini · Date: 2026-03-01\n\n**Me:** a\n\n**Claude:** wrong app\n",
])
def test_user_turns_refuse_rather_than_guess(body):
    assert harvest.user_turns(body) is None


# ---- 2. the queue and the bundle ------------------------------------------------------

def test_queue_orders_hubs_by_concept_cosine_then_unfiled_months(vault):
    idx, t, stack = prep(vault)
    units = harvest.queue(idx, t, stack, set(), set())
    assert [u["unit"] for u in units] == ["cars", "health", "food", "month:2026-06",
                                          "month:2026-05"]
    cars = units[0]
    assert cars["score"] > 0.99 and cars["concepts"][0][0] == "two-pointers"
    assert [c for c, _ in cars["concepts"]] == ["two-pointers"]  # the rest are under 0.68
    assert cars["chats"] == ["chat-car-3b", "chat-car-3a", "chat-car-2-copy", "chat-car-2",
                             "chat-car-1", "chat-car-odd"]  # newest first; private never
    assert units[1]["score"] == 0.0 and units[1]["concepts"] == []
    done = {"chat-car-1", "chat-food-1"}
    assert "chat-car-1" not in harvest.queue(idx, t, stack, done, set())[0]["chats"]
    skip = harvest.queue(idx, t, stack, set(), {"area-life"})  # a skipped area's subtree
    assert [u["unit"] for u in skip][:2] == ["cars", "month:2026-06"]
    within = harvest.queue(idx, t, stack, set(), set(), within="area-life")
    assert [u["unit"] for u in within] == ["health", "food"]


def test_a_private_hub_is_never_served_and_can_be_skipped(core, vault):
    """Review r4 L3: a chat filed under a private hub is not served as Unfiled, and the
    first-run skip may name that hub without echoing its title."""
    p = vault / "notes/health.md"
    p.write_bytes(p.read_bytes().replace(b"tags:", b"private: true\ntags:", 1))
    idx, t, stack = prep(vault)
    assert "health" not in t.parent
    units = harvest.queue(idx, t, stack, set(), set())
    assert not any("chat-health-1" in u["chats"] for u in units)
    assert [u["unit"] for u in units] == ["cars", "food", "month:2026-06", "month:2026-05"]
    out = call(core, "cortex_log", kind="harvested", payload={"outcome": "skipped",
                                                                "hub": "health"})
    assert out["recorded"] == 1 and "private" in out["display_line"], out
    assert "Health" not in (vault / "log.md").read_text(encoding="utf-8")
    bad = call(core, "cortex_log", kind="harvested", payload={"outcome": "skipped",
                                                                "hub": "nope"})
    assert bad["error"] == "invalid_argument"


def test_bundle_packs_whole_hubs_user_turns_only_redacted_and_wrapped(vault):
    idx, t, stack = prep(vault)
    b = harvest.bundle(idx, t, stack, set(), set())
    assert [h["id"] for h in b["hubs"]] == ["cars", "health", "food"]
    cars = b["hubs"][0]
    assert cars["path"] == "Study › Cars" and cars["title"] == "Cars"
    assert cars["concepts"] == [{"id": "two-pointers", "title": "Two pointers", "state": "○"}]
    assert cars["know"].startswith('<untrusted_note id="know:cars">')
    assert "a line the import wrote" in cars["know"]
    assert b["hubs"][2]["know"] == ""  # food has no ## What I know
    text = b["text"]
    assert text.startswith('<untrusted_note id="harvest">') and "## cars: Study › Cars" in text
    assert "Me: how does a car motor start" in text and "ASSISTANT-TEXT" not in text
    assert KEY not in text and "[redacted]" in text
    assert "[[chat-car-odd]] (turns unreadable: title only)\nTitle: Chat chat-car-odd" in text
    assert "car question with no answer" not in text
    assert "## Unfiled chats, 2026-06" in text and "chat-car-private" not in text
    odd = next(c for c in b["chats"] if c["id"] == "chat-car-odd")
    assert odd["title_only"] and odd["turns"] == 0 and odd["hub"] == "cars"
    loose = next(c for c in b["chats"] if c["id"] == "chat-loose-1")
    assert loose["hub"] is None
    assert b["progress"] == {"chats": 10, "harvested": 0, "left": 10}
    assert len(b["bundle"]) == 16
    assert harvest.bundle(idx, t, stack, set(), set(), stamp="later")["bundle"] != b["bundle"]


def test_bundle_pages_a_big_first_hub_and_stops_before_a_hub_that_does_not_fit(vault,
                                                                             monkeypatch):
    idx, t, stack = prep(vault)
    monkeypatch.setattr(chats, "PART_CHARS", 300)
    b = harvest.bundle(idx, t, stack, set(), set())
    assert [h["id"] for h in b["hubs"]] == ["cars"]  # cars alone is over 300: paged by date
    first = [c["id"] for c in b["chats"]]
    assert first[0] == "chat-car-3b" and len(b["text"]) < 400
    rest = harvest.bundle(idx, t, stack, set(served(b)), set())
    assert rest["chats"][0]["id"] not in first and rest["hubs"][0]["id"] == "cars"
    monkeypatch.setattr(chats, "PART_CHARS", 12_000)
    monkeypatch.setattr(harvest, "CHATS_MAX", 3)  # the id cap pages too
    b = harvest.bundle(idx, t, stack, set(), set())
    assert len(served(b)) <= 3
    assert harvest.bundle(idx, t, stack, {c for u in harvest.queue(
        idx, t, stack, set(), set()) for c in u["chats"]}, set()) is None  # nothing left


# ---- 3. the duplicate fold ------------------------------------------------------------

def test_duplicates_fold_under_the_longest_and_a_diverging_pair_stays(vault):
    idx, t, stack = prep(vault)
    hv = topics.heads(stack)
    ids = harvest.queue(idx, t, stack, set(), set())[0]["chats"]
    assert harvest.dupes(idx, hv, ids) == {"chat-car-2-copy": "chat-car-2"}
    b = harvest.bundle(idx, t, stack, set(), set())
    row = next(c for c in b["chats"] if c["id"] == "chat-car-2")
    assert row["dupes"] == ["chat-car-2-copy"]
    assert "chat-car-2-copy" not in [c["id"] for c in b["chats"]]
    assert "[[chat-car-2]] (×2 same day)" in b["text"]
    assert "Me: car tyres" in b["text"] and "Me: car pads" in b["text"]  # both 3a and 3b
    # same turns on another day, or from another app: not a copy
    chat(vault, "chat-car-2-copy", ["car gearbox basics", f"my key {KEY} car"], "cars",
         "2026-04-03", "Perplexity")
    idx, t, stack = prep(vault)
    assert harvest.dupes(idx, topics.heads(stack), ids) == {}
    chat(vault, "chat-car-2-copy", ["car gearbox basics", f"my key {KEY} car"], "cars",
         "2026-04-02", "Gemini")
    idx, t, stack = prep(vault)
    assert harvest.dupes(idx, topics.heads(stack), ids) == {}


# ---- 4. the fold and quiet ------------------------------------------------------------

def ev(id, payload, kind="log"):
    return {"id": id, "kind": kind, "payload": {"kind": "harvested", **payload}}


def test_fold_respects_voids_and_quiet_hides_copies():
    evs = [ev("e1", {"chats": ["chat-a", "chat-b"], "dupes": {"chat-b": "chat-a"},
                     "outcome": "harvested"}),
           ev("e2", {"chats": ["chat-c"], "outcome": "empty"}),
           ev("e3", {"outcome": "skipped", "hub": "health"}),
           ev("e4", {"chats": ["chat-d"], "outcome": "harvested"}),
           {"id": "v1", "kind": "void", "ref": "e4"}]
    assert harvest.fold(evs) == ({"chat-a", "chat-b", "chat-c"}, {"health"},
                                 {"chat-b": "chat-a"})
    assert harvest.quiet(evs) == {"chat-a": 0.4, "chat-b": 0.0, "chat-c": 0.4}


# ---- 5. the server: the harvest action and the `harvested` ledger ----------------------

@pytest.fixture
def core(vault, monkeypatch):
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    return Cortex(Vault(vault))


def call(c, name, **kw):
    return c._call(name, getattr(c, name), kw)


def harvested(root):
    return [e for e in events(root) if e["kind"] == "log"
            and e["payload"].get("kind") == "harvested"]


def test_harvest_action_serves_without_exposing_and_arms_the_guards(core, vault):
    call(core, "cortex_start", mode="history")
    b = call(core, "cortex_chats", action="harvest")
    assert b["hubs"][0]["id"] == "cars" and b["budget"] == {"about_me": 4}
    assert "cortex_log(kind='harvested'" in b["next"]
    assert core.sessions.current.exposed is False
    assert core.sessions.served == set(served(b)) and "chat-car-2-copy" in core.sessions.served
    life = call(core, "cortex_chats", action="harvest", id="area-life")
    assert [h["id"] for h in life["hubs"]] == ["health", "food"]
    bad = call(core, "cortex_chats", action="harvest", id="chat-car-1")
    assert bad["error"] == "invalid_argument"
    call(core, "cortex_end")
    assert core.sessions.served == set()  # cleared when the session closes


def test_harvest_action_needs_the_model(vault):
    out = call(Cortex(Vault(vault)), "cortex_chats", action="harvest")
    assert out["error"] == "invalid_argument" and "cortex model download" in out["message"]


@pytest.mark.parametrize("payload, why", [
    ({"chats": ["chat-nope"]}, "payload.chats"),
    ({"chats": ["two-pointers"]}, "payload.chats"),  # not an imported chat page
    ({"chats": []}, "payload.chats"),
    ({"chats": ["chat-car-2"], "dupes": {"chat-car-2-copy": "chat-car-2"}}, "dupes"),
    ({"chats": ["chat-car-1", "chat-car-2"], "dupes": {"chat-car-1": "chat-car-2"}}, "dupes"),
    ({"chats": ["chat-car-1"], "pages": ["Not An Id"]}, "payload.pages"),
    ({"chats": ["chat-car-1"], "found": 51}, "payload.found"),
    ({"chats": ["chat-car-1"], "outcome": "maybe"}, "outcome"),
    ({"outcome": "skipped"}, "payload.hub"),
    ({"outcome": "skipped", "hub": "chat-car-1"}, "payload.hub"),
])
def test_harvested_validation(core, payload, why):
    out = call(core, "cortex_log", kind="harvested", payload=payload)
    assert out["error"] == "invalid_argument" and why in out["message"], out


def test_harvested_ledger_key_repeats_void_and_no_journal(core, vault):
    call(core, "cortex_start", mode="history")
    b = call(core, "cortex_chats", action="harvest")
    ids = served(b)
    pl = {"chats": ids, "dupes": {"chat-car-2-copy": "chat-car-2"}, "pages": ["two-pointers"],
          "found": 1}
    out = call(core, "cortex_log", kind="harvested", payload=pl, key=b["bundle"])
    assert out["recorded"] == 1 and "10 chats" in out["display_line"]
    core._done.clear()  # a new process: the event log's key, not the reply cache, answers
    again = call(core, "cortex_log", kind="harvested", payload=pl, key=b["bundle"])
    assert again["recorded"] == 0
    [e] = harvested(vault)
    assert e["payload"] == {"kind": "harvested", "outcome": "harvested", "chats": ids,
                            "dupes": {"chat-car-2-copy": "chat-car-2"},
                            "pages": ["two-pointers"], "found": 1}
    log = (vault / "log.md").read_text(encoding="utf-8")
    assert log.count("harvest | 10 chats → 1 pages") == 1
    assert not list((vault / "journal").glob("*.md"))  # no journal line per bundle
    dup = call(core, "cortex_log", kind="harvested", payload={"chats": ids[:2]})
    assert dup["error"] == "already_harvested"
    assert call(core, "cortex_chats", action="harvest")["bundle"] is None
    call(core, "cortex_log", kind="void", payload={"event_id": e["id"], "reason": "redo"})
    back = call(core, "cortex_chats", action="harvest")
    assert set(served(back)) == set(ids)  # a void re-queues the bundle, with a new bundle id
    assert back["bundle"] != b["bundle"]
    skip = call(core, "cortex_log", kind="harvested", payload={"outcome": "skipped",
                                                                 "hub": "area-life"})
    assert skip["recorded"] == 1
    assert {h["id"] for h in call(core, "cortex_chats", action="harvest")["hubs"]} == {"cars"}


def test_harvested_takes_the_lines_as_found_and_me_prefixed_pages(core, vault):
    """dogfood r4 #1: harvest.md read literally sends the lines and me/ ids; both are kept."""
    call(core, "cortex_start", mode="history")
    b = call(core, "cortex_chats", action="harvest")
    out = call(core, "cortex_log", kind="harvested", key=b["bundle"], payload={
        "chats": served(b), "dupes": {"chat-car-2-copy": "chat-car-2"},
        "pages": ["two-pointers", "me/personality"], "found": ["likes cars", "drives"]})
    assert out["recorded"] == 1, out
    [e] = harvested(vault)
    assert e["payload"]["found"] == 2 and e["payload"]["pages"] == ["two-pointers", "personality"]
    bad = call(core, "cortex_log", kind="harvested", payload={"chats": ["chat-car-1"],
                                                              "pages": ["me/Not An Id"]})
    assert bad["error"] == "invalid_argument" and "bare page ids" in bad["message"]


# ---- 6. the guards while chats are served ---------------------------------------------

CITE = " (from [[chat-car-1]])"


def harvesting(core):
    call(core, "cortex_start", mode="history")
    return call(core, "cortex_chats", action="harvest")


def patch(core, id, target, text, **kw):
    return call(core, "cortex_patch", id=id, op="append_to_section", target=target, text=text,
                **kw)


def inbox(root):
    return (root / "inbox.md").read_text(encoding="utf-8")


def test_claim_lines_become_cited_proposals_capped_by_outstanding(core, vault):
    (vault / "me/preferences.md").write_text("# Preferences\n\n## Learning\n", encoding="utf-8")
    harvesting(core)
    out = patch(core, "me/profile", "Brief", "drives to college" + CITE)  # a goals/profile page
    assert out["queued"] is True and "Brief: drives to college" in inbox(vault)
    assert "drives to college" not in (vault / "me/profile.md").read_text(encoding="utf-8")
    for text in ("likes cars", "likes cars (from [[chat-car-private]])",  # never served
                 "likes cars (from [[chat-car-1]])\n- and vans"):  # each line cites
        out = patch(core, "me/preferences", "Learning", text)
        assert out["error"] == "invalid_argument" and "Cite the chat" in out["message"], text
    for k in range(3):
        assert patch(core, "me/preferences", "Learning", f"pref {k}{CITE}")["queued"] is True
    out = patch(core, "me/preferences", "Learning", f"pref 3{CITE}")  # a 5th waiting line
    assert out["queued"] is False and "budget" in out["summary"]
    assert "pref 3" not in inbox(vault)
    call(core, "cortex_patch", id="me/preferences", op="reject", target=f"pref 0{CITE}")
    assert patch(core, "me/preferences", "Learning", f"pref 3{CITE}")["queued"] is True
    yes = patch(core, "me/preferences", "Learning", f"likes worked examples{CITE}",
                user_confirmed=True)  # a yes on the card is written at once
    assert yes["changed"] is True and yes["claim"]
    assert tiers(vault)["likes worked examples"] == "confirmed"  # the bundle exposed nothing


def test_the_per_session_cap_resets_with_the_next_harvest(core, vault):
    (vault / "me/preferences.md").write_text("# Preferences\n\n## Learning\n", encoding="utf-8")
    harvesting(core)
    for k in range(4):
        assert patch(core, "me/preferences", "Learning", f"pref {k}{CITE}")["queued"] is True
    call(core, "cortex_end", summary="first harvest")
    b = harvesting(core)  # 4 lines still wait, but none is this session's
    assert b["budget"]["about_me"] == 4


def test_twelve_waiting_lines_stop_proposals(core, vault):
    today = local_day(datetime.now(UTC))
    (vault / "inbox.md").write_text("# Inbox\n\n## About me (unconfirmed)\n" + "".join(
        f"- Learning: old line {k} (proposed {today})\n" for k in range(12)), encoding="utf-8")
    assert harvesting(core)["budget"] == {"about_me": 0}
    out = patch(core, "me/preferences", "Learning", f"likes cars{CITE}")
    assert out["queued"] is False and "reflect" in out["next"]


def test_chat_pages_are_read_only_and_a_bare_hub_gets_its_heading(core, vault):
    hub_path = vault / "notes/food.md"
    hub_path.write_text(hub_path.read_text(encoding="utf-8")
                        + "\n## Conversations\n- 2026-03-05 · Gemini · [[chat-food-1]]\n",
                        encoding="utf-8")
    line = "- pasta timing kept slipping (from [[chat-food-1]])"
    out = patch(core, "food", "What I explored", line)  # not harvesting: no heading is made
    assert out["error"] == "invalid_argument"
    harvesting(core)
    for op, target, text in (("append_to_section", "Notes", "x"), ("set_topics", "", "food"),
                             ("set_field", "summary", "x")):
        out = call(core, "cortex_patch", id="chat-car-1", op=op, target=target, text=text)
        assert out["error"] == "invalid_argument" and "read-only" in out["message"], op
    before = (vault / "notes/cars.md").read_bytes()
    out = patch(core, "cars", "What I know", line)  # the hub's own section: never harvested into
    assert out["error"] == "invalid_argument" and "What I explored" in out["message"]
    assert (vault / "notes/cars.md").read_bytes() == before
    assert patch(core, "food", "What I explored", line)["changed"] is True
    text = hub_path.read_text(encoding="utf-8")
    assert text.index("## What I explored\n" + line) < text.index("## Conversations")


def test_know_reads_both_headings_old_first():
    class Idx:
        _notes = {"h": {"body": "# H\n\n## What I explored\n- asked about pistons\n\n"
                                "## What I know\n- engines burn fuel\n\n## Conversations\n- x\n"}}
    know = harvest._know(Idx, "h")
    assert know.index("engines burn fuel") < know.index("asked about pistons")
    assert "Conversations" not in know


@pytest.mark.parametrize("name, kw", [
    ("cortex_ask", {"concept": "two-pointers", "item_type": "recall", "question": "q?"}),
    ("cortex_grade", {"concept": "two-pointers", "question": "q?", "learner_answer": "a",
                      "verdict": "correct"}),
    ("cortex_log", {"kind": "self_report", "payload": {"concept": "two-pointers",
                                                       "verdict": "correct"}}),
    ("cortex_log", {"kind": "taught", "payload": {"concept": "two-pointers"}}),
    ("cortex_log", {"kind": "assessment_result", "payload": {"assessment": "acme-oa-2026",
                                                             "outcome": "pass"}}),
    ("cortex_add", {"kind": "person", "title": "A friend"}),
    ("cortex_log", {"kind": "problem_status", "payload": {"problem": "two-sum",
                                                          "status": "solved"}}),
    ("cortex_log", {"kind": "spot_check", "payload": {"event_id": "x", "verdict": "correct"}}),
    ("cortex_patch", {"id": "people/a-friend", "op": "append_to_section", "target": "Notes",
                      "text": "- likes cars"}),
])
def test_no_evidence_or_people_while_harvesting(core, vault, name, kw):
    harvesting(core)
    out = call(core, name, **kw)
    assert out["error"] == "harvesting" and out["next"], out
    assert call(core, "cortex_log", kind="journal", payload={"text": "harvested"})["recorded"]
    call(core, "cortex_end")
    assert call(core, name, **kw).get("error") != "harvesting"  # a fresh session may


def test_a_bare_people_id_is_refused_while_harvesting(core, vault):
    note(vault, "people/a-friend.md", 'title: "A friend"\n', "# A friend\n\n## Notes\n")
    harvesting(core)
    out = patch(core, "a-friend", "Notes", "- likes cars")
    assert out["error"] == "harvesting", out
    assert "likes cars" not in (vault / "people/a-friend.md").read_text(encoding="utf-8")


def test_guards_follow_the_session_that_served_the_bundle(core, vault):
    """Review r4 M1: a harvest with no session open opens a history one; the guards hold only
    while that session is current, so a mock started afterwards can grade."""
    grade = {"concept": "two-pointers", "question": "q?", "learner_answer": "a",
             "verdict": "correct"}
    call(core, "cortex_chats", action="harvest")  # no session open
    assert core.sessions.current.mode == "history" and core.sessions.harvesting
    assert call(core, "cortex_grade", **grade)["error"] == "harvesting"
    call(core, "cortex_start", mode="mock")
    assert not core.sessions.served and not core.sessions.harvesting
    assert call(core, "cortex_grade", **grade).get("error") is None


def test_guards_lift_when_the_serving_session_goes_idle(core, vault):
    harvesting(core)
    core.sessions.current.last_activity -= timedelta(hours=3)
    assert not core.sessions.harvesting


def test_a_yes_to_an_older_waiting_line_needs_no_citation_while_harvesting(core, vault):
    """Review r4 M2: the budget is used up, so harvest.md asks the waiting lines' card; a yes
    to a line from an earlier chat is the user's own word."""
    (vault / "me/preferences.md").write_text("# Preferences\n\n## Learning\n", encoding="utf-8")
    harvesting(core)
    out = patch(core, "me/preferences", "Learning", "likes worked examples", user_confirmed=True)
    assert out["changed"] is True and out["claim"], out
    out = patch(core, "me/preferences", "Learning", "likes diagrams")  # a proposal still cites
    assert out["error"] == "invalid_argument" and "Cite the chat" in out["message"]


# ---- 7. cortex_read exposure ----------------------------------------------------------

def test_reading_a_chat_page_exposes_the_session(core, vault):
    harvesting(core)
    call(core, "cortex_read", id="two-pointers")
    call(core, "cortex_read", id="food")
    assert core.sessions.current.exposed is False
    call(core, "cortex_read", id="chat-car-1")
    assert core.sessions.current.exposed is True
    patch(core, "me/preferences", "Learning", f"likes diagrams{CITE}", user_confirmed=True)
    assert tiers(vault)["likes diagrams"] == "exposed"


# ---- 8. quieting ----------------------------------------------------------------------

QUIET = {"chat-car-3a": harvest.QUIET_WEIGHT, "chat-car-2-copy": 0.0}


def scores(idx, query, **kw):
    kw = {"limit": 20, "hops": 0, **kw}
    return {h["id"]: h["score"] for h in semantic.recall(idx, query, FakeEmbed(), **kw)}


def test_quiet_hides_copies_and_ranks_harvested_chats_below_their_twins(vault):
    note(vault, "notes/brakes.md", 'title: Brakes\nrelated: ["[[chat-car-2-copy]]"]\n',
         "# Brakes\n\ncar car car")  # a seed whose graph neighbour is the copy
    idx = index(vault)
    base = scores(idx, "car brakes")
    assert base["chat-car-3a"] == base["chat-car-3b"] and "chat-car-2-copy" in base
    got = scores(idx, "car brakes", quiet=QUIET)
    assert got["chat-car-3a"] < got["chat-car-3b"] == base["chat-car-3b"]
    assert "chat-car-2-copy" not in got
    assert "chat-car-2-copy" not in scores(idx, "car", quiet=QUIET, hops=1)  # nor as linked
    lifted = scores(idx, "car brakes", quiet=QUIET, chats=True)
    assert lifted["chat-car-3a"] == scores(idx, "car brakes", chats=True)["chat-car-3a"]
    assert "chat-car-2-copy" not in lifted  # chats=true never brings a copy back
    assert list(scores(idx, "tyres", quiet={"chat-car-3b": 0.4})) == ["chat-car-3b"]  # only match
    flat = [h["id"] for h in semantic.recall(idx, "car brakes", FakeEmbed(), 20, 0, quiet=QUIET,
                                             rerank_with=lambda q, texts: [0.0] * len(texts))]
    assert "chat-car-2-copy" not in flat
    assert flat.index("chat-car-3b") < flat.index("chat-car-3a")


def test_recall_sites_pass_quiet(core, vault, monkeypatch):
    from typer.testing import CliRunner

    from cortex.cli import app

    call(core, "cortex_log", kind="harvested", payload={
        "chats": ["chat-car-2", "chat-car-2-copy"], "dupes": {"chat-car-2-copy": "chat-car-2"}})
    ids = [h["id"] for h in call(core, "cortex_recall", query="car gearbox", limit=20)["hits"]]
    assert "chat-car-2" in ids and "chat-car-2-copy" not in ids
    asked = []
    monkeypatch.setattr(core, "_quiet", lambda: asked.append(1) or {})
    call(core, "cortex_context", topic="car gearbox")
    call(core, "cortex_prime", prompt="car gearbox basics")
    assert len(asked) == 2  # context and prime fold it too
    out = CliRunner().invoke(app, ["recall", "car gearbox", "--limit", "20", "--vault",
                                   str(vault)]).output
    assert "chat-car-2 " in out and "chat-car-2-copy" not in out


# ---- 9. cortex eval harvest -----------------------------------------------------------

def test_eval_harvest_numbers(vault, monkeypatch):
    from typer.testing import CliRunner
    from ulid import ULID

    from cortex.cli import app
    from cortex.events import EventLog

    log, now = EventLog(Vault(vault)), datetime.now(UTC)
    old, new = f"s_{ULID()}", f"s_{ULID()}"
    log.append(session=old, kind="log", source="tutor", now=now - timedelta(days=8), fields={
        "payload": {"kind": "harvested", "outcome": "harvested", "pages": ["food", "gone"],
                    "chats": ["chat-car-1", "chat-car-3a"], "dupes": {}, "found": 1}})
    log.append(session=new, kind="log", source="tutor", fields={"payload": {
        "kind": "harvested", "outcome": "empty", "chats": ["chat-food-1"], "pages": []}})
    (vault / "me/preferences.md").write_text(
        "# Preferences\n\n## Learning\n- likes worked examples (from [[chat-car-1]])\n\n"
        "## Not me\n- likes noise (from [[chat-car-3a]]) (rejected 2026-11-16)\n"
        "- an older line, no chat\n", encoding="utf-8")
    (vault / "inbox.md").write_text("# Inbox\n\n## About me (unconfirmed)\n- Learning: waits "
                                    f"(proposed {local_day(now)})\n", encoding="utf-8")
    hub_path = vault / "notes/cars.md"
    hub_path.write_text(hub_path.read_text(encoding="utf-8") + "- starts [[two-pointers]]\n",
                        encoding="utf-8")

    def run(*args):
        r = CliRunner().invoke(app, ["eval", "harvest", *args, "--vault", str(vault)])
        assert r.exit_code == 0, r.output
        return r.output

    r = json.loads(run("--json"))
    assert r["placement"] is None and r["chats"] == 10 and r["harvested"] == 3  # no model
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    r = json.loads(run("--json"))
    assert r["placement"] == {"hubs": 1, "chats": 6, "harvested": 2, "coverage": 0.333,
                              "bridged": 1}
    assert r["bridged_hubs"] == 1 and r["hubs"] == 6 and r["waiting"] == 1
    assert r["yes_rate"] == {"yes": 1, "no": 1, "rate": 0.5}
    assert r["keep"] == {"pages": 2, "kept": 1, "share": 0.5}
    assert r["empty"] == {"Gemini": {"chats": 2, "empty": 1, "rate": 0.5},
                          "Perplexity": {"chats": 1, "empty": 0, "rate": 0.0}}
    assert [(x["session"], x["pages"], x["undo"]) for x in r["runs"]] == [
        (old, ["food", "gone"], f"cortex undo {old}"), (new, [], f"cortex undo {new}")]
    since = json.loads(run("--json", "--since", local_day(now)))
    assert since["keep"]["pages"] == 0 and [x["session"] for x in since["runs"]] == [new]
    text = run()
    assert CliRunner().invoke(  # review r4 S3: the new home prints the same
        app, ["harvest", "status", "--vault", str(vault)]).output == text
    assert "placement coverage: 0.33 (2 of 6 chats in 1 hubs" in text
    assert "7-day keep share: 0.50" in text and f"undo: cortex undo {old}" in text
    assert "--since must be a day" in CliRunner().invoke(
        app, ["eval", "harvest", "--since", "soon", "--vault", str(vault)]).output


def test_eval_harvest_escapes_page_text(vault, monkeypatch):
    """review r4 L6: an app name or page id from a chat page reaches the terminal escaped."""
    from typer.testing import CliRunner

    from cortex.cli import app

    real = harvest.status

    def status(*a, **kw):
        r = real(*a, **kw)
        r["empty"] = {"Gem\x1b[31mini [x](http://e.co)": {"chats": 1, "empty": 0, "rate": 0.0}}
        r["runs"] = [{"day": "2026-11-16", "session": "s_1", "bundles": 1, "chats": 1,
                      "pages": ["fo\x07od"], "undo": "cortex undo s_1"}]
        return r

    monkeypatch.setattr(harvest, "status", status)
    r = CliRunner().invoke(app, ["eval", "harvest", "--vault", str(vault)])
    assert r.exit_code == 0, r.output
    assert "empty rate Gem" in r.stdout and "pages: food" in r.stdout
    assert not any(c in r.stdout for c in ("\x1b", "\x07", "http", "]("))
