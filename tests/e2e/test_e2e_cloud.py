"""E4 Cloud opt-in (docs/design/v6/TEST-PLAN.md §4; data-access.md §2-3), in-process through
the CLI: `cortex browse deepen` with a real config file, the real CloudRunner over a fake HTTPS
opener and the fake Ollama as the local model. Off by default; when on, only cloud-audience
site pages are sent, a secret is skipped and counted, one counts-only audit line is written,
the switch stops the next call, max_items caps a run, and ask and import never use the cloud.
No network: the opener is a fake and the local model is a loopback fake."""

import io
import json
import urllib.error
import urllib.request
from datetime import timedelta

import pytest
from integration.builders import fake_profile_root
from integration.test_i3_browse_privacy import D0, DROPPED, MARKERS, history_rows
from ruamel.yaml import YAML
from test_browse import Embed, hubbed
from typer.testing import CliRunner

from cortex import cloudllm, localllm, semantic
from cortex.app import status as app_status
from cortex.browse import sites
from cortex.cli import app
from e2e.fake_ollama import FakeOllama

pytestmark = pytest.mark.usefixtures("home")

KEY_ENV, KEY = "E4_CLOUD_KEY", "e4-cloud-key-123"
SECRET = "api_key=ZQSECRETabcdefghijklmnop"
# a "maybe": a soft health word on an unknown site; private all the same
MAYBE = (D0 + timedelta(minutes=50), "https://blog.example.net/zqmaybepage",
         "ZQMAYBE therapy notes")
VISIBLE = [(D0 + timedelta(minutes=60 + k), url, title) for k, (url, title) in enumerate((
    ("https://en.wikipedia.org/wiki/Pasta", "Pasta - Wikipedia"),
    ("https://www.bbc.co.uk/food/pasta", "Pasta recipes - BBC Food")))]
NOT_CLOUD = (*DROPPED, *sum(MARKERS.values(), ()), "zqmaybe")
BROWSE = {"visible_to_claude": True, "hidden_from_claude": ["health", "money"],
          "skip_categories": ["adult"]}


class Opener:
    """The HTTPS endpoint: records each request; `after(n)` runs after the nth one."""

    def __init__(self):
        self.reqs, self.after = [], None

    def open(self, req, timeout=None):
        self.reqs.append(req)
        if self.after:
            self.after(len(self.reqs))
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(
            {"category": "other", "description": "A site.", "related": [],
             "topics": []})}}]}).encode())

    def sent(self) -> str:
        return "\n".join(r.data.decode("utf-8") for r in self.reqs)


class World:
    def __init__(self, root, home, ollama: FakeOllama, opener: Opener):
        self.root, self.home, self.ollama, self.opener = root, home, ollama, opener

    def config(self, *, cloud: bool, local: bool, **cloudllm_extra) -> None:
        data = {"vault": str(self.root), "browse": {**BROWSE, "cloud_processing": cloud},
                "cloudllm": {"provider": "openai", "model": "m", "api_key_env": KEY_ENV,
                             **cloudllm_extra}}
        if local:
            data["localllm"] = {"enabled": True, "url": self.ollama.url}
        p = self.home / ".cortex" / "config.yaml"
        p.parent.mkdir(exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            YAML(typ="safe").dump(data, f)

    def cli(self, *args) -> str:
        r = CliRunner().invoke(app, [*map(str, args), "--vault", str(self.root)])
        assert r.exit_code == 0, (args, r.output, r.exception)
        return r.output

    def forget_descriptions(self) -> None:
        """Every site page undescribed again, so the next deepen asks about each."""
        (self.root / sites.CACHE).unlink(missing_ok=True)

    def local_sites(self) -> str:
        return json.dumps(self.ollama.chats())

    def audit(self) -> list[dict]:
        p = self.root / cloudllm.LOG
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] \
            if p.is_file() else []


@pytest.fixture
def world(root, home, monkeypatch):
    fake_profile_root(home / "LOCALAPPDATA", {"chrome": [*history_rows(), MAYBE, *VISIBLE]})
    hubbed(root)
    monkeypatch.setattr(semantic, "_embedder", Embed())
    monkeypatch.setenv(KEY_ENV, KEY)
    monkeypatch.setattr(localllm, "_PROBES", [])  # only the configured fake, never :11434
    monkeypatch.setattr(localllm, "ensure_lmstudio", lambda *a, **k: None)
    real, opener = cloudllm._OPENER, Opener()
    monkeypatch.setattr(cloudllm, "_OPENER", opener)
    with FakeOllama() as ollama:
        w = World(root, home, ollama, opener)
        w.real_opener = real
        w.config(cloud=False, local=False)
        w.cli("browse", "import")
        yield w


def test_e4_cloud_opt_in(world, monkeypatch):
    w, opener = world, world.opener
    # 1. configured and keyed, but cloud processing off: deepen sends nothing to the cloud
    out = w.cli("browse", "deepen")
    assert "no local model" in out and not opener.reqs and not w.audit()

    # 2. on: only cloud-audience pages go; a title holding a secret is skipped and counted
    days = json.loads((w.root / sites.DAYS).read_text(encoding="utf-8"))
    food = [a for d in days.values() for k, a in d.items() if k.startswith("example.com|")]
    assert food
    for a in food:  # an older cache line holding a secret (the filter drops new ones)
        for row in a["p"].values():
            row[0] = f"Pasta notes {SECRET}"
    (w.root / sites.DAYS).write_text(json.dumps(days), encoding="utf-8")
    w.config(cloud=True, local=True)
    w.cli("browse", "deepen")
    sent = opener.sent()
    assert opener.reqs and "wikipedia" in sent.lower()
    leaked = [m for m in (*NOT_CLOUD, "zqsecret", KEY) if m.lower() in sent.lower()]
    assert not leaked, leaked
    local = w.local_sites().lower()
    assert "webmd" in local and "zqmaybe" in local  # hidden and maybe pages went local only
    assert "pornhub" not in local + sent.lower()  # skipped: stored nowhere, asked of no one
    for req in opener.reqs:
        assert req.full_url == "https://api.openai.com/v1/chat/completions"
        assert req.get_header("Authorization") == f"Bearer {KEY}"

    # 3. one counts-only audit line, shown by the app
    runs = w.audit()
    assert len(runs) == 1, runs
    assert set(runs[0]) == {"ts", "provider", "model", "items", "chars", "skipped"}
    assert runs[0]["items"] == len(opener.reqs) and runs[0]["skipped"] == 1
    line = (w.root / cloudllm.LOG).read_text(encoding="utf-8")
    assert not any(m in line.lower() for m in ("pasta", "wikipedia", "zqsecret", KEY.lower()))
    shown = app_status.cloud(w.root)
    assert shown["cloud_processing"] is True and shown["runs"]
    assert KEY not in json.dumps(shown)

    # 4a. the kill switch: turned off after the first request, the next page goes local
    w.forget_descriptions()
    opener.reqs.clear()
    opener.after = lambda n: n == 1 and w.config(cloud=False, local=True)
    asked_local = len(w.ollama.chats())
    w.cli("browse", "deepen")
    opener.after = None
    assert len(opener.reqs) == 1, len(opener.reqs)
    later = json.dumps(w.ollama.chats()[asked_local:]).lower()
    assert "wikipedia" in later or "bbc" in later  # the visible page after the switch: local
    # 4b. max_items caps a run
    w.forget_descriptions()
    opener.reqs.clear()
    w.config(cloud=True, local=True, max_items=1)
    w.cli("browse", "deepen")
    assert len(opener.reqs) == 1 and w.audit()[-1]["items"] == 1
    # 4c. an http:// base gives no cloud runner at all; a redirect is refused, not followed
    w.forget_descriptions()
    opener.reqs.clear()
    w.config(cloud=True, local=False, provider="custom", base_url="http://cloud.example.com/v1")
    assert "no local model" in w.cli("browse", "deepen") and not opener.reqs
    no_redirect = next(h for h in w.real_opener.handlers
                       if isinstance(h, urllib.request.HTTPRedirectHandler))
    req = urllib.request.Request("https://api.openai.com/v1/chat/completions")
    with pytest.raises(urllib.error.HTTPError):
        no_redirect.redirect_request(req, None, 302, "Found", {}, "https://evil.example/x")

    # 5. ask and the import's summaries use the local model only
    def refuse(*a, **k):
        raise AssertionError("the cloud runner was reached")

    monkeypatch.setattr(cloudllm, "find", refuse)
    monkeypatch.setattr(cloudllm.CloudRunner, "_post", refuse)
    w.config(cloud=True, local=True)
    before = len(w.ollama.chats())
    out = w.cli("ask", "what pasta recipe did I read?")
    assert "answered by ollama" in out
    w.cli("browse", "import", "--since", "2026-11-09")
    assert len(w.ollama.chats()) > before + 1  # ask, then the day summaries
    assert not opener.reqs
