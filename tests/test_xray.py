"""Agent X-ray (v9 plan item 6): the ring of what cortex_prime and cortex_context served (ids
and counts, no text, bounded, trimmed ids), the app's view (private pages never by name), and
the owner's Pin and Mute (prime and context obey them; only the app sets them)."""

import json
import shutil
from datetime import UTC, datetime

import anyio
import pytest
from conftest import FakeEmbed, note
from mcp import Client
from test_app import _serve, call
from test_app_js import HARNESS, _run

from cortex import prime, semantic, xray
from cortex.mcp_server import Cortex, build_server
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

PREFS = ("# Preferences\n\n## Coding\n- Python first, type hints.\n\n## Personal\n"
         "- Weekend motor racing fan.\n")


@pytest.fixture
def vault(root, monkeypatch):
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    (root / ".cortex").mkdir(exist_ok=True)
    note(root, "notes/engines.md", "title: Engines\nsummary: how a motor works\n",
         "# Engines\n\nAutomobile car motor basics.")
    note(root, "notes/pasta.md", "title: Pasta\n", "# Pasta\n\nA recipe from the kitchen.")
    (root / "me/preferences.md").write_text(PREFS, encoding="utf-8")
    return root


def claim_id(root, text):
    ids = prime.claim_ids(Vault(root))
    return next(c for (p, _, t), c in ids.items() if t == text)


def related(ctx):
    return ctx.split("## Related\n", 1)[1].split("\n## ", 1)[0] if "## Related\n" in ctx else ""


def test_ring_records_ids_only_and_stays_bounded(vault, monkeypatch):
    monkeypatch.setattr(xray, "CAP", 3)
    c = Cortex(Vault(vault))
    c.sessions.client = "claude-ai"
    for n in range(4):
        c.cortex_prime(f"How does a car motor work? take {n}")
    c.cortex_context(topic="car motor")
    rows = xray.rows(Vault(vault))
    assert len(rows) == 3 and rows[-1]["tool"] == "cortex_context"
    first = rows[0]
    assert first["client"] == "claude-ai" and first["chars"] > 0
    assert "engines" in first["served"]["related"]
    assert first["served"]["profile"] == ["me/profile"]
    assert claim_id(vault, "Weekend motor racing fan.") in first["served"]["preferences"]
    raw = (vault / xray.RING).read_text("utf-8")
    for text in ("car motor", "take", "motor racing", "Python first", "Engines", "how a motor"):
        assert text not in raw  # no prompt, no page or claim text
    assert set(rows[-1]["served"]) >= {"profile", "related", "claims"}


def test_trimmed_ids_are_recorded(vault, monkeypatch):
    lines = "".join(f"- Preference number {n} about coding style and habits.\n"
                    for n in range(30))
    (vault / "me/preferences.md").write_text(f"# Preferences\n\n## Coding\n{lines}",
                                             encoding="utf-8")
    xr: dict = {}
    parts, _ = prime.core(Vault(vault), "coding style", [], datetime.now(UTC), xr=xr)
    served, cut = xr["served"]["preferences"], xr["trimmed"]["preferences"]
    assert served and cut and len(served) + len(cut) == 30
    assert all(c.startswith("^c-") for c in served + cut)
    every = set(prime.claim_ids(Vault(vault)).values())
    assert set(served) | set(cut) <= every


def test_view_never_names_a_private_page(vault):
    v = Vault(vault)
    xray.record(v, "cortex_prime", "claude-code", {"related": ["secret-diary", "engines"]},
                {"related": ["secret-diary"]}, 120)
    xray.set_flag(v, "mute", "secret-diary", True)
    view = xray.view(v)
    dumped = json.dumps(view)
    assert "secret-diary" not in dumped and "Private concept" not in dumped
    (g,) = view["clients"]
    assert g["client"] == "Claude Code"
    (served, *_), = [c["served"] for c in g["calls"]]
    assert [x["title"] for x in served["items"]] == ["a private page", "Engines"]
    (m,) = view["muted"]
    assert m["title"] == "a private page" and "id" not in m
    xray.set_flag(v, "mute", xray.resolve_token(v, m["token"]), False)  # undo by its token
    assert xray.settings(v)["mute"] == set()


def test_pin_serves_a_page_and_a_claim_in_prime(vault):
    v = Vault(vault)
    prompt = "How does a car motor work?"
    ctx = Cortex(v).cortex_prime(prompt)["context"]
    assert "[[pasta]]" not in ctx
    python = claim_id(vault, "Python first, type hints.")
    xray.set_flag(v, "pin", "pasta", True)
    xray.set_flag(v, "pin", python, True)
    ctx = Cortex(v).cortex_prime(prompt)["context"]
    assert related(ctx).index("[[pasta]]") < related(ctx).index("[[engines]]")  # pins lead
    prefs = ctx.split("## Preferences\n", 1)[1]
    assert prefs.index("Python first") < prefs.index("motor racing")
    assert "pasta" in xray.rows(v)[-1]["served"]["related"]


def test_mute_keeps_a_page_and_a_claim_out_of_prime_and_context(vault):
    v = Vault(vault)
    racing = claim_id(vault, "Weekend motor racing fan.")
    xray.set_flag(v, "mute", "engines", True)
    xray.set_flag(v, "mute", racing, True)
    c = Cortex(v)
    ctx = c.cortex_prime("How does a car motor work?")["context"]
    assert "engines" not in ctx and "motor racing" not in ctx and "Python first" in ctx
    ctx = c.cortex_context(topic="car motor")["context"]
    assert "engines" not in ctx and "motor racing" not in ctx and "Python first" in ctx
    assert all("engines" not in json.dumps(r["served"]) for r in xray.rows(v))
    xray.set_flag(v, "pin", "engines", True)  # pinning unmutes
    assert xray.settings(v) == {"pin": {"engines"}, "mute": {racing}}


def test_pin_and_mute_cannot_be_set_over_mcp(vault):
    server = build_server(Vault(vault), "owner")

    async def main():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            for t in tools:  # no tool takes a pin or mute argument
                props = set(t.input_schema.get("properties", {}))
                assert not props & {"pin", "mute", "pinned", "muted", "xray"}, t.name
            r = await client.call_tool("cortex_patch", {
                "id": ".cortex/xray", "op": "append_to_section", "target": "x", "text": "pin"})
            assert r.is_error or "error" in (r.structured_content or {})

    anyio.run(main)
    assert not (vault / xray.SETTINGS).exists()
    with pytest.raises(ValueError):
        xray.set_flag(Vault(vault), "pin", "../me/profile", True)


def test_endpoints_are_gated_and_pin_round_trips(vault):
    srv, app = _serve(vault)
    try:
        assert call(app, "GET", "/api/xray", token=False)[0] == 401
        assert call(app, "POST", "/api/xray/pin", {"id": "pasta"}, token=False)[0] == 401
        assert call(app, "POST", "/api/xray/mute", {"id": "pasta"}, token="nope")[0] == 401
        assert not (vault / xray.SETTINGS).exists()
        status, js, *_ = call(app, "POST", "/api/xray/pin", {"id": "pasta"})
        assert status == 200 and [x["id"] for x in js["pinned"]] == ["pasta"]
        assert call(app, "POST", "/api/xray/pin", {"id": "Bad Id!"})[0] == 400
        status, js, *_ = call(app, "GET", "/api/xray")
        assert status == 200 and js["pinned"][0]["title"] == "Pasta" and js["clients"] == []
    finally:
        srv.shutdown()
        srv.server_close()


XRAY_JS = HARNESS[:HARNESS.index("const out = {};")] + r"""
const out = { errors };
(async () => {
  try {
    vm.runInThisContext(fs.readFileSync(appPath, 'utf8'), { filename: appPath });
    await tick();
    nav.querySelectorAll('button').find((b) => b.dataset.s === 'xray').fire('click');
    await tick();
    out.boards = $('xray').querySelectorAll('h2').map((x) => x.textContent);
    out.rows = $('xray').querySelectorAll('.brow').map((r) => [r.querySelector('.ttl').textContent,
      r.querySelectorAll('button').map((b) => b.textContent)]);
    $('xray').querySelectorAll('button').find((b) => b.textContent === 'Mute').fire('click');
    await tick();
    out.posted = fetched.filter((f) => f.method === 'POST').map((f) => [f.path, f.body]);
  } catch (e) { errors.push(String(e && e.stack || e)); }
  out.sinks = sinks;
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""  # noqa: E501  (JavaScript)


def test_the_xray_page(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    item = {"id": "engines", "kind": "page", "title": "<b>Engines</b>", "pinned": False,
            "muted": False}
    private = {"kind": "page", "title": "a private page", "token": "t1", "pinned": False,
               "muted": True}
    call_ = {"ts": "2026-11-20T10:00:00Z", "tool": "cortex_prime", "chars": 900,
             "served": [{"block": "related", "items": [item]}],
             "trimmed": [{"block": "profile", "items": [
                 {"id": "me/profile", "kind": "me", "title": "Profile"}]}]}
    view = {"pinned": [], "muted": [private],
            "clients": [{"client": "Claude Code", "total": 1, "calls": [call_]}]}
    status = {"first_run": False, "rows": [], "vault": "V", "land": "status",
              "update": {"current": "0.1.0", "mode": "installer"}}
    out = _run(node, tmp_path, "xray", XRAY_JS, {
        "routes": {"/api/xray": view, "/api/xray/mute": view, "/api/status": status,
                   "/api/review": {"count": 0}}, "stream": []})
    assert out["errors"] == [] and out["sinks"] == [], out["errors"]
    assert out["boards"] == ["Pinned and muted", "Claude Code"]
    assert out["rows"] == [["a private page", ["Unmute"]],
                           ["<b>Engines</b>", ["Pin", "Mute"]],  # text, never markup
                           ["Profile", []]]
    assert out["posted"] == [["/api/xray/mute", {"id": "engines", "on": True}]]
