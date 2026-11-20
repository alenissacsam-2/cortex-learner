"""The canary harness (SPEC-privacy-gate §5): a temp vault with a unique token planted in every
kind of record Claude and cloud models must never see, then every outward surface is run and
grepped for the tokens.

Planted (ids carry `zq7f3a`, text carries `CANARY-7F3A`):
- a private note, a private concept, a concept under a private area (privacy by inheritance,
  `planner.hidden`), a mistake about each of those two concepts, a private people page;
- browsing through the real import: a health site (local only), an adult site (skip: it must
  land nowhere at all), and a hand-written page in `browse/private/`;
- a voice sample (to a friend).

Surfaces: every MCP tool the server registers (the test fails if one is never called), the
prime hook, read-only CLI output, `cortex ask` without --private (and the prompt its runner
got), Today.md, index.md, the export snapshot, `cortex export context`, the recall log,
`.cortex/vectors.db`, the cloud runner's input and `cloud-log.jsonl`, the app log, and the app's
home endpoints while the Private switch is locked. A positive control shows the local model's
Chat still reads the private canaries when its Private switch is on.

A leak the central access gate will fix is listed in LEAKS and asserted in a strict xfail of
its own; every other surface must be clean now.

Not swept, by documented design (docs/PRIVACY.md, SPEC-privacy-gate §1): `cortex_read` of a
people page by its exact id, and `cortex_as_me` for the voice sample's own recipient. The sweep
calls `cortex_as_me` for another recipient, and must not see the sample there."""

import io
import json
import logging
import threading
from datetime import UTC, datetime, timedelta

import anyio
import pytest
from conftest import FakeEmbed, git, note
from integration.builders import fake_profile_root
from mcp import Client
from ruamel.yaml import YAML
from test_chat import Stream
from typer.testing import CliRunner

from cortex import (
    ask,
    chat,
    chat_study,
    cloudllm,
    dashboard,
    localllm,
    mcp_server,
    prime_hook,
    semantic,
    voice,
)
from cortex.app import server as app_server
from cortex.cli import app
from cortex.events import EventLog
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

# Each planted record and its lower-case markers (none is a prefix of another record's); a hit
# is a leak. Hosts too: a local-only site's name is private.
RECORDS = {
    "private note": ("canary-7f3a-note", "zq7f3a-note"),
    "private concept": ("canary-7f3a-conc", "zq7f3a-conc"),
    "concept under a private area": ("canary-7f3a-inhr", "zq7f3a-inhr", "canary-7f3a-area",
                                     "zq7f3a-area"),
    "mistake about a private concept": ("canary-7f3a-mist", "zq7f3a-mist"),
    "mistake about a concept under a private area": ("canary-7f3a-imis", "zq7f3a-imis"),
    "private people page": ("canary-7f3a-pers", "zq7f3a-pers"),
    "voice sample": ("canary-7f3a-voic",),
    "local-only site": ("canary-7f3a-lsit", "canary7f3alsit", "webmd"),
    "browse/private page": ("canary-7f3a-ppag", "zq7f3a-ppag"),
    "skipped site": ("canary-7f3a-skip", "canary7f3askip", "pornhub"),
}
ANY = ("canary-7f3a", "canary7f3a", "zq7f3a")  # a canary no record above claims: "unknown"
KEY_ENV = "CANARY_CLOUD_KEY"
NOW = datetime.now(UTC)
D0 = NOW - timedelta(days=3)

CATALOG = """schema: 1
domains:
  - id: "dsa"
    title: "Data structures and algorithms"
    aliases: ["DSA"]
    areas:
      - id: "dsa.arrays"
        title: "Arrays"
        concepts:
          - id: "arrays-basics"
            title: "Array basics"
            aliases: []
            requires: []
          - id: "two-pointers"
            title: "Two pointers"
            aliases: ["2 pointers", "two-pointer technique"]
            requires: ["arrays-basics"]
          - id: "secret-diary"
            title: "Private concept"
            aliases: []
            requires: []
          - id: "zq7f3a-conc"
            title: "CANARY-7F3A-CONC pointers"
            aliases: []
            requires: []
      - id: "dsa.zq7f3a-area"
        title: "CANARY-7F3A-AREA"
        concepts:
          - id: "zq7f3a-inhr"
            title: "CANARY-7F3A-INHR pointers"
            aliases: []
            requires: []
"""

PLANTED_IDS = ("zq7f3a-note", "zq7f3a-conc", "zq7f3a-inhr", "dsa.zq7f3a-area",
               "zq7f3a-mist", "zq7f3a-imis", "zq7f3a-ppag")

# Known leaks the access gate task fixes: (surface, record) -> why. Each is a strict xfail in
# test_known_leak and left out of the clean sweep, so any other leak still fails.
_SESSION = "the session page cortex_end writes names the private concepts studied in it"
LEAKS: dict[tuple[str, str], str] = {
    ("mcp cortex_ask probe", "mistake about a private concept"):
        "cortex_ask on a private concept's id is accepted and names its open mistakes",
    ("mcp cortex_grade probe", "concept under a private area"):
        "cortex_grade on a hidden concept's id is accepted and echoes its title",
    **{(s, r): _SESSION for s in ("app /api/home", "app /api/home/delta",
                                  "app /api/home?private=1 (locked)", "export snapshot")
       for r in ("private concept", "concept under a private area")},
}


def history_rows() -> list[tuple]:
    """Cooking on three days (a visible hub), a local-only health site and search, and an
    adult site in a skipped category."""
    rows = [(D0 + timedelta(days=d, minutes=k), f"https://food.example.com/pasta-{d}-{k}",
             f"Pasta recipe cooking day {d} part {k}") for d in range(3) for k in range(3)]
    t = D0 + timedelta(minutes=30)
    return rows + [
        (t, "https://www.webmd.com/canary7f3alsit-page", "CANARY-7F3A-LSIT flu"),
        (t + timedelta(minutes=1), "https://www.google.com/search?q=canary7f3alsitterm+symptoms",
         "canary7f3alsitterm symptoms - Google Search", "canary7f3alsitterm symptoms"),
        (t + timedelta(minutes=2), "https://www.pornhub.com/view_video.php?viewkey=canary7f3askip",
         "CANARY-7F3A-SKIP clip"),
    ]


def set_config(home, root) -> None:
    p = home / ".cortex" / "config.yaml"
    p.parent.mkdir(exist_ok=True)
    data = {"vault": str(root),
            "browse": {"visible_to_claude": True, "hidden_from_claude": ["health"],
                       "skip_categories": ["adult"], "cloud_processing": True},
            "cloudllm": {"provider": "openai", "model": "m", "api_key_env": KEY_ENV}}
    with p.open("w", encoding="utf-8") as f:
        YAML(typ="safe").dump(data, f)


class Opener:
    """The cloud endpoint: records every request body; answers like an OpenAI chat API."""

    def __init__(self):
        self.reqs = []

    def open(self, req, timeout=None):
        self.reqs.append(req)
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(
            {"category": "other", "description": "A site.", "related": [],
             "topics": []})}}]}).encode())

    def sent(self) -> str:
        return "\n".join(r.data.decode("utf-8") for r in self.reqs)


def cli(*args, ok=True) -> str:
    r = CliRunner().invoke(app, [str(a) for a in args])
    if ok:
        assert r.exit_code == 0, (args, r.output, r.exception)
    return r.output


def _grade(root, concept, mistakes=(), confidence=4):
    EventLog(Vault(root)).append(
        session="cli", kind="grade", source="external", now=NOW - timedelta(days=2),
        fields={"object": f"concept:{concept}", "verdict": "wrong", "item_type": "recall",
                "confidence": confidence, **({"mistakes": list(mistakes)} if mistakes else {})})


def plant(root) -> None:
    (root / "catalog.yaml").write_text(CATALOG, encoding="utf-8")
    note(root, "areas/dsa.zq7f3a-area.md", 'id: "dsa.zq7f3a-area"\ntitle: "CANARY-7F3A-AREA"\n'
         'kind: "area"\ndomain: "dsa"\nparent: "[[dsa]]"\nprivate: true\n',
         "# CANARY-7F3A-AREA\n\n## Notes\n")
    note(root, "concepts/zq7f3a-conc.md", 'id: "zq7f3a-conc"\n'
         'title: "CANARY-7F3A-CONC pointers"\nparent: "[[dsa.arrays]]"\ndomain: "dsa"\n'
         'mistakes: ["[[zq7f3a-mist]]"]\nprivate: true\n',
         "# CANARY-7F3A-CONC\n\n## Notes\n- canary-7f3a-conc-body pointers.\n")
    note(root, "concepts/zq7f3a-inhr.md", 'id: "zq7f3a-inhr"\n'
         'title: "CANARY-7F3A-INHR pointers"\nparent: "[[dsa.zq7f3a-area]]"\ndomain: "dsa"\n'
         'mistakes: ["[[zq7f3a-imis]]"]\n',
         "# CANARY-7F3A-INHR\n\n## Notes\n- canary-7f3a-inhr-body pointers.\n")
    note(root, "mistakes/zq7f3a-mist.md", 'id: "zq7f3a-mist"\n'
         'title: "CANARY-7F3A-MIST off by one"\nconcepts: ["[[zq7f3a-conc]]"]\n',
         "# CANARY-7F3A-MIST\n\n## Pattern\ncanary-7f3a-mist-body.\n")
    note(root, "mistakes/zq7f3a-imis.md", 'id: "zq7f3a-imis"\n'
         'title: "CANARY-7F3A-IMIS off by one"\nconcepts: ["[[zq7f3a-inhr]]"]\n',
         "# CANARY-7F3A-IMIS\n\n## Pattern\ncanary-7f3a-imis-body.\n")
    note(root, "notes/zq7f3a-note.md", 'title: "CANARY-7F3A-NOTE car motor"\nkind: note\n'
         "private: true\nsummary: canary-7f3a-note-summary\n",
         "# CANARY-7F3A-NOTE\n\ncanary-7f3a-note-body car motor automobile.\n")
    note(root, "notes/pasta.md", 'title: "Pasta notes"\nkind: note\n',
         "# Pasta notes\n\n## Notes\nPasta recipe cooking in the kitchen.\n")
    note(root, "people/zq7f3a-pers.md", 'title: "CANARY-7F3A-PERS"\nkind: person\n'
         "relation: friend\ntier: close\n", "# CANARY-7F3A-PERS\n\ncanary-7f3a-pers-body.\n")
    for c, m in (("zq7f3a-conc", "zq7f3a-mist"), ("zq7f3a-inhr", "zq7f3a-imis"),
                 ("two-pointers", "dsa.off-by-one")):
        _grade(root, c, [m])
    voice.add(Vault(root), voice.SAMPLES, [{"audience": "friend", "act": "thanks",
                                           "channel": "chat",
                                           "text": "CANARY-7F3A-VOIC cheers mate"}], NOW)


@pytest.fixture
def world(root, home, monkeypatch):
    """vault_mini with every canary planted, browsing imported through the real filter, the
    fake embedder, a fake cloud endpoint and no local model. Returns (root, opener)."""
    plant(root)
    fake_profile_root(home / "LOCALAPPDATA", {"chrome": history_rows()})
    set_config(home, root)
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    monkeypatch.setenv(KEY_ENV, "canary-cloud-key-123")
    opener = Opener()
    monkeypatch.setattr(cloudllm, "_OPENER", opener)

    def no_local_model(url, body, timeout):
        raise OSError("no local model in the canary harness")
    monkeypatch.setattr(localllm, "_request", no_local_model)
    cli("browse", "import", "--vault", root)
    note(root, "browse/private/zq7f3a-ppag.md", "kind: browse\nprivate: true\n",
         "# Browsing (private)\n\n- CANARY-7F3A-PPAG car motor repair\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "canaries")
    assert list((root / "browse" / "private").glob("browse-private-*.md")), "import wrote no " \
        "private page"
    return root, opener


def leaks(blobs: dict[str, str]) -> list[tuple[str, str, str]]:
    """(surface, record, the text around its first hit) for every planted record a surface
    holds."""
    out = []
    for name, text in blobs.items():
        low = rest = text.lower()
        for record, markers in RECORDS.items():
            at = min((i for m in markers if (i := low.find(m)) >= 0), default=-1)
            if at >= 0:
                out.append((name, record, text[max(0, at - 80):at + 60]))
            for m in markers:
                rest = rest.replace(m, "")
        if (at := min((i for m in ANY if (i := rest.find(m)) >= 0), default=-1)) >= 0:
            out.append((name, "unknown", rest[max(0, at - 80):at + 60]))
    return sorted(out)


# ---- MCP -------------------------------------------------------------------------------------


async def _mcp(root) -> tuple[dict[str, str], set[str], set[str]]:
    """{"mcp <tool>" or "mcp <tool> probe": every result's text, the call's own string args cut
    out (an echoed argument is not a leak)}, the registered tools and the tools called."""
    srv = mcp_server.build_server(Vault(root))
    out: dict[str, str] = {}
    called: set[str] = set()
    async with Client(srv) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}

        async def call(name, args=None, probe=False):
            args = args or {}
            called.add(name)
            r = await c.call_tool(name, args)
            text = json.dumps(r.structured_content, ensure_ascii=False) + "".join(
                getattr(x, "text", "") for x in r.content)
            for v in args.values():
                if isinstance(v, str) and v:
                    text = text.replace(v, "").replace(json.dumps(v)[1:-1], "")
            key = f"mcp {name}" + (" probe" if probe else "")
            out[key] = out.get(key, "") + "\n" + text
            return r.structured_content or {}

        def enum(tool, field):
            p = tools[tool].input_schema["properties"][field]
            for alt in [p, *p.get("anyOf", [])]:
                if "enum" in alt:
                    return alt["enum"]
            raise AssertionError((tool, field, p))

        await call("cortex_prime", {"prompt": "What do I know about pasta recipe cooking and "
                                              "pointers, and where am I weak?"})
        for scope in enum("cortex_context", "scope"):
            await call("cortex_context", {"scope": scope})
        for topic in ("pasta recipe cooking", "car motor", "pointers", "flu symptoms", "arrays"):
            await call("cortex_context", {"topic": topic})
        found = set()
        for q in ("pasta recipe cooking", "car motor", "pointers", "off by one", "flu symptoms",
                  "private", "browse", "friend", "pasta"):
            got = await call("cortex_find", {"query": q})
            found |= {h["id"] for h in got.get("hits", [])}
        for kind in enum("cortex_find", "kind"):
            got = await call("cortex_find", {"kind": kind})
            found |= {h["id"] for h in got.get("hits", [])}
            got = await call("cortex_find", {"kind": kind, "query": "pointers"})
            found |= {h["id"] for h in got.get("hits", [])}
        assert "pasta" in found, "search finds the public control page"
        for q in ("pasta recipe cooking kitchen", "car motor automobile", "pointers off by one",
                  "flu symptoms health", "what did I browse this week"):
            got = await call("cortex_recall", {"query": q})
            found |= {h["id"] for h in got.get("hits", [])}
            await call("cortex_recall", {"query": q, "chats": True})
        await call("cortex_recall", {"query": "I cook pasta every week.", "check": True})
        for i in sorted(found - set(PLANTED_IDS)):
            await call("cortex_read", {"id": i})
        browse_ids = sorted(p.stem for p in (root / "browse").rglob("*.md"))
        for i in [*PLANTED_IDS, *browse_ids, "notes/zq7f3a-note.md",
                  "browse/private/zq7f3a-ppag.md", "raw/../voice/samples.jsonl",
                  "voice/samples.jsonl", "raw/../notes/zq7f3a-note.md"]:
            await call("cortex_read", {"id": i}, probe=True)
        for action in enum("cortex_chats", "action"):
            await call("cortex_chats", {"action": action})
        await call("cortex_reflect", {"action": "pack"})
        await call("cortex_reflect", {"action": "lint"})
        await call("cortex_as_me", {"task": "Ask my professor for an extension.",
                                    "audience": "professor", "act": "request",
                                    "channel": "email"})

        # a study session with the happy paths that return text
        await call("cortex_start", {"mode": "learn", "model": "test-model"})
        ask_ = await call("cortex_ask", {"concept": "two-pointers", "item_type": "recall",
                                         "question": "Which pointer moves?", "answer_key": "B"})
        await call("cortex_grade", {"item_id": ask_.get("item_id"), "learner_answer": "b",
                                    "verdict": "correct"})
        await call("cortex_grade", {"concept": "two-pointers", "question": "Explain",
                                    "learner_answer": "x", "verdict": "wrong",
                                    "mistakes": ["dsa.off-by-one"]})
        for kind, payload in (("journal", {"text": "Cooked pasta and studied pointers."}),
                              ("touch", {"ids": ["pasta"]}),
                              ("self_report", {"concept": "two-pointers",
                                               "verdict": "correct"}),
                              ("need", {"question": "Which pasta shape do I like?",
                                        "area": "personal"})):
            await call("cortex_log", {"kind": kind, "payload": payload})
        await call("cortex_patch", {"id": "pasta", "op": "append_to_section",
                                    "target": "Notes", "text": "- Salt the water."})
        await call("cortex_add", {"kind": "note", "title": "Pasta shapes",
                                  "fields": {"summary": "pasta recipe cooking shapes"}})
        await call("cortex_end", {"summary": "Pointers and pasta."})
        for mode in enum("cortex_start", "mode"):
            await call("cortex_start", {"mode": mode})
            await call("cortex_end", {})

        # asking about the private subjects by id: nothing about them may come back
        await call("cortex_ask", {"concept": "zq7f3a-conc", "item_type": "recall",
                                  "question": "What is it?"}, probe=True)
        await call("cortex_grade", {"concept": "zq7f3a-inhr", "question": "Explain",
                                    "learner_answer": "x", "verdict": "wrong"}, probe=True)
        await call("cortex_end", {}, probe=True)
    return out, set(tools), called


def mcp_outputs(root) -> dict[str, str]:
    out, tools, called = anyio.run(_mcp, root)
    assert tools - called == set(), f"MCP tools the harness never calls: {tools - called}"
    return out


# ---- the CLI, files and logs -----------------------------------------------------------------


class FakeRunner(localllm.Runner):
    """A local runner that records every prompt it gets."""

    def __init__(self):
        super().__init__("ollama", "http://127.0.0.1:11434", "gemma3:2b")
        self.asked: list[str] = []

    def chat(self, system, user, **kw):
        self.asked.append(system + "\n" + user)
        return "From your notes [1]."


def cli_outputs(root, monkeypatch) -> dict[str, str]:
    out = {}
    for args in (("today",), ("brief",), ("reflect",), ("lint",), ("recall", "pasta recipe"),
                 ("recall", "pointers"), ("concept", "duplicates"), ("claims", "--summary"),
                 ("chats",), ("needs",), ("browse", "status"), ("dashboard", "--no-open")):
        out["cli " + " ".join(args)] = cli(*args, "--vault", root)
    out["cli export context"] = cli("export", "context", "--vault", root)
    runner = FakeRunner()
    monkeypatch.setattr(localllm, "find", lambda *a, **k: runner)
    out["cli ask"] = cli("ask", "pasta recipe cooking pointers flu", "--vault", root)
    out["cli ask: the local model's prompt"] = "\n".join(runner.asked)
    assert runner.asked, "cortex ask reached its runner"
    monkeypatch.setenv("CORTEX_VAULT", str(root))
    out["prime-hook"] = prime_hook.run(json.dumps({
        "session_id": "canary-chat", "hook_event_name": "UserPromptSubmit",
        "prompt": "What do I know about pasta recipe cooking and pointers?"}))
    assert out["prime-hook"], "the prime hook printed its context"
    return out


def file_outputs(root, opener) -> dict[str, str]:
    cli("browse", "deepen", "--vault", root)  # the cloud runner: cloud-audience sites only
    cortex = root / ".cortex"
    out = {"Today.md": (root / "Today.md").read_text(encoding="utf-8"),
           "index.md": (root / "index.md").read_text(encoding="utf-8"),
           "export snapshot": dashboard.snapshot(Vault(root), now=NOW),
           "recall log": (cortex / "recall-log.jsonl").read_text(encoding="utf-8"),
           ".cortex/vectors.db": " ".join(f"{i} {e[0]}" for i, e in semantic._read(root).items()),
           "cloud runner input": opener.sent(),
           "cloud log": (cortex / "cloud-log.jsonl").read_text(encoding="utf-8")}
    assert opener.reqs and "pasta" in out["cloud runner input"].lower(), "the cloud ran"
    assert "pasta" in out[".cortex/vectors.db"], "the vectors hold the public pages"
    return out


def app_outputs(root) -> dict[str, str]:
    """The app's home endpoints with the Private switch locked, in process."""
    a = app_server.App(root)
    out = {}
    home = a.home({})
    out["app /api/home"] = json.dumps(home, ensure_ascii=False)
    out["app /api/home?private=1 (locked)"] = json.dumps(a.home({"private": ["1"]}),
                                                          ensure_ascii=False)
    out["app /api/home/version"] = json.dumps(a.home_version())
    out["app /api/home/delta"] = json.dumps(a.home_delta({"since": ["0"]}), ensure_ascii=False)
    for site in ("webmd.com", "www.webmd.com", "pornhub.com", "food.example.com"):
        for q in ({}, {"private": ["1"]}):
            try:
                got = a.home_site({"site": [site], **q})
            except app_server.NotFound:
                got = "404"
            out[f"app /api/home/site {site} {q}"] = json.dumps(got, ensure_ascii=False)
    out["app status, browse, vault"] = json.dumps([a.status(), a.browse(), a.vault()],
                                                  default=str, ensure_ascii=False)
    assert "pasta" in out["app /api/home"].lower(), "the home shows the public pages"
    return out


def everything(world, monkeypatch, capsys, caplog) -> dict[str, str]:
    root, opener = world
    caplog.set_level(logging.DEBUG)  # the app log is the process's stderr and its log records
    out = mcp_outputs(root)
    out.update(cli_outputs(root, monkeypatch))
    out.update(file_outputs(root, opener))
    out.update(app_outputs(root))
    out["app log"] = capsys.readouterr().err + "\n".join(
        r.getMessage() for r in caplog.records)
    return out


# ---- the tests -------------------------------------------------------------------------------


_OUT: dict[str, dict[str, str]] = {}  # the sweep, run once per test session


@pytest.fixture
def outputs(request, monkeypatch, capsys, caplog) -> dict[str, str]:
    """Every surface's text. The sweep takes about 30 s, so the tests below share one run."""
    if "out" not in _OUT:
        _OUT["out"] = everything(request.getfixturevalue("world"), monkeypatch, capsys, caplog)
    return _OUT["out"]


def test_no_canary_reaches_claude_or_the_cloud(outputs):
    assert {s for s, _ in LEAKS} <= set(outputs), {s for s, _ in LEAKS} - set(outputs)
    found = [x for x in leaks(outputs) if x[:2] not in LEAKS]
    assert not found, "\n".join(map(repr, found))


@pytest.mark.parametrize("surface,record", [
    pytest.param(s, r, marks=pytest.mark.xfail(
        strict=True, reason=f"leak: {s} shows the {r} ({why}), fixed by the access gate task"))
    for (s, r), why in LEAKS.items()])
def test_known_leak(outputs, surface, record):
    assert not [x for x in leaks({surface: outputs[surface]}) if x[1] == record]


def test_a_skipped_site_is_stored_nowhere(world, home):
    root, _ = world
    files = {p: p.read_bytes().lower() for base in (root, home / ".cortex")
             for p in base.rglob("*") if p.is_file()}
    found = sorted({(str(p), m) for p, b in files.items() for m in RECORDS["skipped site"]
                    if m.encode() in b})
    assert not found, "\n".join(map(repr, found))


def test_weak_spots_never_name_a_private_subject(world):
    """OQ-22: "Where am I weak?" with Chat's Private switch off, and the start card's open
    mistakes (cortex_start covers it above)."""
    root, _ = world
    v, today = Vault(root), datetime.now().astimezone().date()
    text = chat_study.answer(v, "weak", None, today, private=False)
    assert "two pointers" in text.lower(), text  # the public weak concept shows
    assert not leaks({"weak": text}), text


def test_the_local_model_still_reads_private_canaries(world, stream):
    """Positive control: with Chat's Private switch on, the local model gets the private note
    and the weak spots name the private subjects, so the sweep above is not trivially clean."""
    root, _ = world
    v = Vault(root)
    today = datetime.now().astimezone().date()
    weak = chat_study.answer(v, "weak", None, today, private=True).lower()
    assert "canary-7f3a-conc" in weak and "canary-7f3a-mist" in weak, weak
    s = stream("ollama", ["From your notes [1]."])
    settings = dict(chat.get_settings(), sources=dict(ask.SOURCES, private=True))
    chat.reply(v, None, "car motor automobile", s.runner(), lambda e: None,
               threading.Event(), settings)
    sent = json.dumps(s.bodies).lower()
    assert "canary-7f3a-note-body" in sent and "canary-7f3a-ppag" in sent, sent[-3000:]


@pytest.fixture
def stream():
    made = []

    def make(*a, **k):
        made.append(Stream(*a, **k))
        return made[-1]

    yield make
    for s in made:
        s.close()
