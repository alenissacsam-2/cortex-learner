"""R27b: X-ray's Trace a reply (C2), Compare two apps (G5b-7) and the broad-read flag over
keepfold_find, keepfold_recall and keepfold_read rows (G5b-5)."""

import json
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FakeEmbed, note
from test_app import _serve, call
from test_app_js import HARNESS, _run
from test_xray import PREFS, PROMPT, _aperture, claim_id

from keepfold import semantic, xray
from keepfold.events import local_day
from keepfold.mcp_server import Keepfold
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")


@pytest.fixture
def vault(root, monkeypatch):
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    (root / ".keepfold").mkdir(exist_ok=True)
    note(root, "notes/engines.md", "title: Engines\nsummary: how a motor works\n",
         "# Engines\n\nAutomobile car motor basics.")
    note(root, "notes/pasta.md", "title: Pasta\n", "# Pasta\n\nA recipe from the kitchen.")
    (root / "me/preferences.md").write_text(PREFS, encoding="utf-8")
    return root


# ---- Trace a reply ---------------------------------------------------------------------------
def test_trace_ranks_by_the_rare_words_shared(vault):
    (vault / "me/preferences.md").write_text(
        "# Preferences\n\n## Coding\n- Uses Pipenv for every project.\n"
        "- Python first, type hints.\n", encoding="utf-8")
    for n in range(12):  # "python" and "setup" are common here, "pipenv" is not
        note(vault, f"notes/py-{n}.md", f"title: Python setup {n}\n",
             "# Python setup\n\nA python setup guide.")
    v = Vault(vault)
    pipenv, python = (claim_id(vault, t) for t in ("Uses Pipenv for every project.",
                                                   "Python first, type hints."))
    xray.record(v, "keepfold_prime", "claude-ai", {"preferences": [pipenv, python],
                                                 "related": ["py-0", "engines"]}, {}, 100)
    got = xray.trace(v, "claude-ai", "Since you prefer Pipenv, here is a Python setup.")
    assert got["calls"] == 1
    assert [(h["id"], h["words"]) for h in got["hits"]] == [
        (pipenv, ["pipenv"]), ("py-0", ["python", "setup"]), (python, ["python"])]
    first = got["hits"][0]
    assert (first["kind"], first["tool"], first["title"]) == (
        "claim", "keepfold_prime", "Uses Pipenv for every project.")
    assert first["ts"] == xray.rows(v)[-1]["ts"] and "muted" in first and "pinned" in first
    # plural folding: "setups" and "guides" meet "setup" and "guide"
    (hit, *_), = [xray.trace(v, "claude-ai", "Setups and guides.")["hits"]]
    assert hit["id"] == "py-0" and hit["words"] == ["setups", "guides"]


def test_trace_shows_a_private_page_by_no_name(vault):
    note(vault, "notes/zanzibar.md", "title: Zanzibar diary\nprivate: true\n",
         "# Zanzibar diary\n\nZanzibar carburettor rebuild notes.")
    v = Vault(vault)
    xray.record(v, "keepfold_find", "cursor", {"found": ["zanzibar", "engines"]}, {}, 50)
    got = xray.trace(v, "cursor", "Rebuild the carburettor like your Zanzibar notes said.")
    (hit,) = got["hits"]
    assert hit == {"title": xray.PRIVATE, "kind": "page", "tool": "keepfold_find",
                   "ts": xray.rows(v)[-1]["ts"]}
    dumped = json.dumps(got).lower()
    assert "zanzibar" not in dumped and "carburettor" not in dumped


def test_trace_reads_only_the_apps_last_ten_calls(vault):
    v = Vault(vault)
    xray.record(v, "keepfold_prime", "claude-ai", {"related": ["engines"]}, {}, 10)  # 11th back
    for _ in range(xray.TRACE_CALLS):
        xray.record(v, "keepfold_prime", "claude-ai", {"related": ["pasta"]}, {}, 10)
    xray.record(v, "keepfold_find", "cursor", {"found": ["engines"]}, {}, 10)
    reply = "An automobile motor, as in your engines page."
    assert xray.trace(v, "claude-ai", reply) == {"calls": 10, "hits": []}
    (hit,) = xray.trace(v, "cursor", reply)["hits"]
    assert (hit["id"], hit["words"]) == ("engines", ["automobile", "motor", "engines"])
    assert xray.trace(v, "", reply) == {"calls": 0, "hits": []}  # no unnamed app called
    assert xray.trace(v, "cursor", "the and of")["hits"] == []  # no content word


def test_trace_endpoint_keeps_no_reply(vault, tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    v = Vault(vault)
    xray.record(v, "keepfold_prime", "claude-ai", {"related": ["engines"]}, {}, 10)
    marker = "zqxjvplonkr"
    reply = f"Your automobile motor {marker} engines."
    srv, app = _serve(vault)
    try:
        body = {"client": "claude-ai", "text": reply}
        assert call(app, "POST", "/api/xray/trace", body, token=False)[0] == 401
        for bad in ({"client": "claude-ai", "text": 7}, {"client": "claude-ai", "text": " "},
                    {"client": "claude-ai", "text": reply + "a" * 4000}, {"text": reply},
                    {"client": 7, "text": reply}, {"client": "x" * 101, "text": reply}):
            status, _, raw, _ = call(app, "POST", "/api/xray/trace", bad)
            assert status == 400 and marker.encode() not in raw, bad
        status, js, raw, _ = call(app, "POST", "/api/xray/trace", body)
        assert status == 200 and js["calls"] == 1
        assert [(h["id"], h["words"]) for h in js["hits"]] == [
            ("engines", ["automobile", "motor", "engines"])]
        assert marker.encode() not in raw

        def boom(*a):
            raise RuntimeError(reply)

        monkeypatch.setattr(xray, "trace", boom)
        status, js, raw, _ = call(app, "POST", "/api/xray/trace", body)
        assert (status, js) == (500, {"error": "internal error (RuntimeError)"})
    finally:
        srv.shutdown()
        srv.server_close()
    assert marker not in caplog.text
    for p in Path(tmp_path).rglob("*"):  # the vault, .keepfold and the home folder
        if p.is_file():
            assert marker.encode() not in p.read_bytes(), p


# ---- Compare two apps ------------------------------------------------------------------------
def _ids(blocks):
    return {x.get("id") for b in blocks for x in b["items"]}


def test_compare_marks_what_each_gets_and_what_its_rule_withholds(vault):
    _aperture(vault)
    v = Vault(vault)
    racing = claim_id(vault, "Weekend motor racing fan.")
    python = claim_id(vault, "Python first, type hints.")
    got = xray.compare(v, PROMPT, "claude-ai", "cursor")
    a, b = got["a"], got["b"]
    assert set(a) == set(b) == {"served", "trimmed", "chars", "withheld"}
    assert racing in _ids(a["served"]) and racing not in _ids(b["served"])  # only Claude
    assert python in _ids(a["served"]) and python in _ids(b["served"])
    assert a["withheld"] == []  # every scope: nothing kept from it
    assert "personal" in b["withheld"] and "coding" not in b["withheld"]
    dumped = json.dumps(got)
    assert "secret-motor" not in dumped and "Secret motor" not in dumped
    # an app no rule names: the core only; its vault pages need any one scope ("all")
    w = xray.compare(v, PROMPT, "mcp", "cursor")["a"]["withheld"]
    assert w[0] == "all" and {"coding", "personal"} <= set(w)
    assert not (vault / xray.RING).exists()  # nothing logged


def test_compare_endpoint(vault):
    _aperture(vault)
    srv, app = _serve(vault)
    try:
        body = {"prompt": PROMPT, "a": "claude-ai", "b": "cursor"}
        assert call(app, "POST", "/api/xray/compare", body, token=False)[0] == 401
        for bad in ({**body, "b": "claude-ai"}, {**body, "a": "", "b": "unknown"},
                    {**body, "prompt": " "}, {**body, "prompt": "a" * 2001},
                    {**body, "a": 7}, {"prompt": PROMPT, "a": "cursor"}):
            assert call(app, "POST", "/api/xray/compare", bad)[0] == 400, bad
        status, js, *_ = call(app, "POST", "/api/xray/compare", body)
        assert status == 200 and set(js) == {"a", "b"} and "personal" in js["b"]["withheld"]
        assert not (vault / xray.RING).exists()
    finally:
        srv.shutdown()
        srv.server_close()


# ---- find, recall and read rows --------------------------------------------------------------
def test_find_recall_and_read_record_ids_only(vault):
    note(vault, "people/dr-rao.md", "title: Dr Rao\nrelation: professor\n",
         "# Dr Rao\n\n## Who they are to me\nZebra mentor.\n")
    v = Vault(vault)
    c = Keepfold(v)
    c.sessions.client = "cursor"
    found = c.keepfold_find("automobile")
    c.keepfold_recall("automobile kitchen")
    c.keepfold_read("engines")
    c.keepfold_read("people/dr-rao")
    rs = xray.rows(v)
    assert [r["tool"] for r in rs] == ["keepfold_find", "keepfold_recall", "keepfold_read",
                                       "keepfold_read"]
    assert {r["client"] for r in rs} == {"cursor"} and all(r["chars"] > 0 for r in rs[2:])
    assert rs[0]["served"] == {"found": [h["id"] for h in found["hits"]]}
    assert "engines" in rs[0]["served"]["found"]
    assert set(rs[1]["served"]) <= {"recalled"}
    assert rs[2]["served"] == {"read": ["engines"]} and rs[3]["served"] == {"read": ["dr-rao"]}
    raw = (vault / xray.RING).read_text("utf-8")
    for text in ("automobile", "Automobile", "kitchen", "Zebra", "Dr Rao", "how a motor"):
        assert text not in raw  # no query, no page text
    view = xray.view(v)
    (g,) = view["clients"]
    assert [c_["tool"] for c_ in g["calls"]][0] == "keepfold_read"
    assert g["calls"][0]["served"] == [{"block": "read", "items": [
        {"title": xray.PRIVATE, "kind": "page", "token": xray.token("dr-rao"),
         "pinned": False, "muted": False}]}]
    assert "dr-rao" not in json.dumps(view)


def test_find_shapes_never_break_the_tool(vault, monkeypatch):
    v = Vault(vault)
    c = Keepfold(v)
    c.sessions.client = "cursor"
    c.keepfold_find(kind="resurface")
    c.keepfold_find("engines", kind="timeline")
    monkeypatch.setattr(Keepfold, "_resurface", lambda self, limit: {
        "items": [{"title": "x"}, "y", {"id": 7}, {"id": "pasta"}], "next": ""})
    assert c.keepfold_find(kind="resurface")["items"][0] == {"title": "x"}
    rs = xray.rows(v)
    assert [r["tool"] for r in rs] == ["keepfold_find"] * 3
    assert all(isinstance(i, str) for r in rs for ids in r["served"].values() for i in ids)
    assert rs[-1]["served"] == {"found": ["pasta"]}


def test_served_most_often_counts_first_calls_only(vault):
    v = Vault(vault)
    xray.record(v, "keepfold_prime", "cursor", {"related": ["engines"]}, {}, 1)
    for _ in range(3):  # Mute does nothing for a search's hits: they never lead this list
        xray.record(v, "keepfold_find", "cursor", {"found": ["pasta", "engines"]}, {}, 1)
    xray.record(v, "keepfold_read", "cursor", {"read": ["pasta"]}, {}, 1)
    (g,) = xray.view(v)["clients"]
    assert [(x["id"], x["count"]) for x in g["often"]] == [("engines", 1)]
    assert len(g["calls"]) == 5  # the searches and reads still list


def test_old_guesses_judge_by_the_first_calls_not_older_reads(root):
    from test_review import ago
    from test_review_auto import commit, page, past, tidy
    from test_time_salience import _guess

    (root / ".keepfold").mkdir(exist_ok=True)
    page(root, "preferences", ["Uses Vim"])
    commit(root)
    _guess(root, "preferences", "Uses Vim", 31)

    def ring(*rows):
        (root / xray.RING).write_text("".join(json.dumps({
            "ts": f"{d}T12:00:00Z", "tool": tool, "client": "cursor", "served": {},
            "trimmed": {}, "chars": 1}) + "\n" for d, tool in rows), encoding="utf-8")

    # finds reach 45 days back, first calls only 20: a guess older than those may have
    # been served by a first call the ring has dropped
    ring((ago(45), "keepfold_find"), (ago(20), "keepfold_prime"))
    assert tidy(root) == {"moved": 0, "retired": 0}
    assert "Uses Vim" not in past(root, "preferences")
    ring((ago(45), "keepfold_prime"), (ago(20), "keepfold_prime"))
    assert tidy(root) == {"moved": 0, "retired": 0, "guesses": 1}
    assert "Uses Vim" in past(root, "preferences")


def test_reads_are_capped_apart(vault, monkeypatch):
    monkeypatch.setattr(xray, "CAP", 3)
    monkeypatch.setattr(xray, "READ_CAP", 2)
    v = Vault(vault)
    for n in range(4):
        xray.record(v, "keepfold_prime", "claude-ai", {"related": [f"p{n}"]}, {}, 1)
    for n in range(3):
        xray.record(v, "keepfold_find", "claude-ai", {"found": [f"f{n}"]}, {}, 1)
    xray.record(v, "resource:keepfold://core", "claude-ai", {"core": ["me/core"]}, {}, 1)
    assert [next(iter(r["served"].values()))[0] for r in xray.rows(v)] == [
        "p2", "p3", "f1", "f2", "me/core"]  # resource rows count with the CAP family


# ---- the broad-read flag ---------------------------------------------------------------------
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def _reads(start, pages, per=5, step=timedelta(minutes=1), tool="keepfold_find",
           client="cursor"):
    """One call per `step` from `start`, `per` pages each."""
    return [{"ts": (start + step * (k // per)).strftime(xray.TS), "tool": tool,
             "client": client, "served": {"found": pages[k:k + per]}, "trimmed": {},
             "chars": 10} for k in range(0, len(pages), per)]


def _pages(n, p="p"):
    return [f"{p}{k}" for k in range(n)]


def test_the_window_maths():
    t0 = NOW - timedelta(hours=2)
    assert xray.wide(_reads(t0, _pages(49)), now=NOW) is None
    assert xray.wide(_reads(t0, _pages(50)), now=NOW) is None  # over 50 only
    w = xray.wide(_reads(t0, _pages(51)), now=NOW)  # 11 calls, a minute apart
    end = t0 + timedelta(minutes=10)
    assert w == {"n": 51, "day": local_day(end), "end": end.strftime(xray.TS)}
    assert xray.wide(_reads(t0, _pages(5) * 30), now=NOW) is None  # the same pages again
    # two calls of 26 pages: within 15 minutes one window, 16 minutes apart two
    for gap, flagged in ((14, True), (16, False)):
        rs = _reads(t0, _pages(26), per=26) + _reads(t0 + timedelta(minutes=gap),
                                                    _pages(26, "q"), per=26)
        assert (xray.wide(rs, now=NOW) is not None) is flagged, gap
    # the last 7 days only
    week = NOW - timedelta(days=xray.WIDE_DAYS)
    assert xray.wide(_reads(week - timedelta(minutes=11), _pages(51)), now=NOW) is None
    assert xray.wide(_reads(week + timedelta(minutes=1), _pages(51)), now=NOW)["n"] == 51
    # first calls and resource reads are not searches or reads
    for tool in ("keepfold_prime", "resource:keepfold://core", "keepfold_context"):
        assert xray.wide(_reads(t0, _pages(60), tool=tool), now=NOW) is None, tool
    # recall and read count with find
    rs = _reads(t0, _pages(20), tool="keepfold_recall") + _reads(
        t0, _pages(20, "r"), tool="keepfold_read") + _reads(t0, _pages(20, "f"))
    assert xray.wide(rs, now=NOW)["n"] == 60
    # after a Got it, only later calls count
    assert xray.wide(_reads(t0, _pages(51)), after=w["end"], now=NOW) is None


def _ring(v, rs):
    (v.root / xray.RING).write_text("".join(json.dumps(r) + "\n" for r in rs), "utf-8")


def test_got_it_hides_the_warning_until_a_newer_window(vault):
    v = Vault(vault)
    t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=3)
    first = _reads(t0, _pages(51))
    _ring(v, first + _reads(t0, _pages(3), client="claude-ai"))
    g = {c["name"]: c for c in xray.view(v)["clients"]}
    w = g["cursor"]["wide"]
    assert w["n"] == 51 and g["claude-ai"]["wide"] is None
    with pytest.raises(ValueError):
        xray.ack(v, "cursor", "2020-01-01T00:00:00Z")  # not the window shown
    with pytest.raises(ValueError):
        xray.ack(v, "claude-ai", w["end"])  # that app shows none
    srv, app = _serve(vault)
    try:
        body = {"client": "cursor", "end": w["end"]}
        assert call(app, "POST", "/api/xray/ack", body, token=False)[0] == 401
        assert call(app, "POST", "/api/xray/ack", {"client": "cursor", "end": 7})[0] == 400
        status, js, *_ = call(app, "POST", "/api/xray/ack", body)
        assert status == 200
        assert {c["name"]: c["wide"] for c in js["clients"]}["cursor"] is None
        assert call(app, "POST", "/api/xray/ack", body)[0] == 400  # shown no more
    finally:
        srv.shutdown()
        srv.server_close()
    xray.set_flag(v, "pin", "engines", True)  # a pin keeps the Got it
    stored = json.loads((vault / xray.SETTINGS).read_text("utf-8"))
    assert stored["ack"] == {"cursor": w["end"]} and stored["pin"] == ["engines"]
    assert {c["name"]: c["wide"] for c in xray.view(v)["clients"]}["cursor"] is None
    _ring(v, first + _reads(t0 + timedelta(hours=1), _pages(51, "q")))  # a newer burst
    w2 = {c["name"]: c["wide"] for c in xray.view(v)["clients"]}["cursor"]
    assert w2["n"] == 51 and w2["end"] > w["end"]


def test_a_normal_chat_reads_far_below_the_flag(vault, monkeypatch):
    for n in range(60):
        note(vault, f"notes/topic-{n}.md", f"title: Motor topic {n}\n",
             f"# Motor topic {n}\n\nCar motor fact number {n}.")
    v = Vault(vault)
    c = Keepfold(v)
    c.sessions.client = "claude-ai"
    c.keepfold_prime("How does a car motor work?")
    for q in ("car motor", "motor fact", "automobile"):
        c.keepfold_find(q)
    c.keepfold_recall("car motor")
    for i in ("topic-1", "topic-2", "engines"):
        c.keepfold_read(i)
    c.keepfold_context(topic="car motor")
    assert xray.wide(xray.rows(v)) is None
    monkeypatch.setattr(xray, "WIDE_PAGES", 0)
    assert xray.wide(xray.rows(v))["n"] <= 35  # 3 finds of 8, a recall of 8, 3 reads at most


# ---- the page --------------------------------------------------------------------------------
TRACE_JS = HARNESS[:HARNESS.index("const out = {};")] + r"""
const out = { errors };
(async () => {
  try {
    vm.runInThisContext(fs.readFileSync(appPath, 'utf8'), { filename: appPath });
    await tick();
    nav.querySelectorAll('button').find((b) => b.dataset.s === 'xray').fire('click');
    await tick();
    const brd = (t) => $('xray').querySelectorAll('.board').find((b) => b.querySelector('h2').textContent === t);
    const btns = (e) => e.querySelectorAll('button').map((b) => b.textContent);
    const posts = () => fetched.filter((f) => f.method === 'POST').map((f) => [f.path, f.body]);
    const hits = (b) => b.querySelectorAll('.brow').filter((r) => r.querySelectorAll('.sub').length
      && r.querySelector('.sub').textContent.startsWith('served by'))
      .map((r) => [r.querySelector('.ttl').textContent, r.querySelectorAll('.sub').map((s) => s.textContent), btns(r),
        r.querySelectorAll('button').map((b) => b.getAttribute('aria-label'))]);
    const notes = (b) => b.querySelectorAll('p').filter((p) => p.className === 'pad ink-2').map((p) => p.textContent);
    out.boards = $('xray').querySelectorAll('h2').map((x) => x.textContent);
    // the broad-read warning leads its app's board
    const cc = () => brd('Claude Code');
    const warn = cc().querySelectorAll('.brow')[0];
    out.warn = [warn.querySelector('.ttl').textContent, btns(warn), warn.querySelector('button').getAttribute('aria-label')];
    out.firstOther = brd('mystery-app').querySelectorAll('.brow').map((r) => r.querySelector('.ttl').textContent);
    // Trace a reply
    const area = cc().querySelector('textarea');
    out.area = [area.id, area.getAttribute('rows'), area.getAttribute('maxlength'),
      cc().querySelectorAll('label').map((l) => [l.textContent, l.getAttribute('for')])];
    const traceBtn = () => cc().querySelectorAll('button').find((b) => b.textContent === 'Trace');
    out.disabled = [traceBtn().disabled];
    area.value = '   '; area.fire('input');
    out.disabled.push(traceBtn().disabled);
    area.value = 'You use Pipenv.'; area.fire('input');
    out.disabled.push(traceBtn().disabled);
    area.fire('keydown', { key: 'Enter' });  // a plain Enter is a new line
    await tick();
    out.plainEnter = posts().length;
    area.fire('keydown', { key: 'Enter', ctrlKey: true });
    // busy while it runs: a second Ctrl+Enter or click posts nothing more
    out.busy = [traceBtn().getAttribute('aria-busy')];
    area.fire('keydown', { key: 'Enter', ctrlKey: true });
    traceBtn().fire('click');
    await tick();
    out.busy.push(traceBtn().getAttribute('aria-busy'), posts().length);
    out.traced = [notes(cc()), hits(cc())];
    cc().querySelectorAll('button').find((b) => b.textContent === 'Retire').fire('click');
    await tick();
    out.retired = [cc().querySelector('textarea').value, notes(cc()), hits(cc()).map((x) => x[0]), $('toast-msg').textContent];
    // no match, on the other app's own trace
    DATA.routes['/api/xray/trace'] = { calls: 1, hits: [] };
    const other = brd('mystery-app');
    const area2 = other.querySelector('textarea');
    area2.value = 'Something new.'; area2.fire('input');
    other.querySelectorAll('button').find((b) => b.textContent === 'Trace').fire('click');
    await tick();
    out.none = [notes(brd('mystery-app')), hits(cc()).length];
    // Compare two apps
    const cmp = () => brd('Compare two apps');
    const sel = (w) => cmp().querySelectorAll('select').find((s) => s.id === `xray-cmp-${w}`);
    const show = () => cmp().querySelectorAll('button').find((b) => b.textContent === 'Show');
    out.cmpForm = [cmp().querySelectorAll('label').map((l) => [l.textContent, l.getAttribute('for')]),
      ['a', 'b'].map((w) => [sel(w).value, sel(w).querySelectorAll('option').map((o) => [o.value, o.textContent])]),
      notes(cmp()), cmp().querySelectorAll('.xray-col').length];
    out.show = [show().disabled];
    const input = cmp().querySelector('input');
    input.value = 'car motor'; input.fire('input');
    out.show.push(show().disabled);
    sel('b').value = 'claude-code'; sel('b').fire('change');
    out.show.push(show().disabled);  // the same app twice
    sel('b').value = 'mystery-app'; sel('b').fire('change');
    out.show.push(show().disabled);
    input.fire('keydown', { key: 'Enter' });
    out.busy.push(show().getAttribute('aria-busy'));
    input.fire('keydown', { key: 'Enter' });
    show().fire('click');
    await tick();
    out.busy.push(show().getAttribute('aria-busy'));
    const cols = () => cmp().querySelectorAll('.xray-col').map((c) => [c.querySelector('.lbl').textContent,
      c.querySelector('.help').textContent, c.querySelectorAll('.brow').map((r) => [r.querySelector('.ttl').textContent,
        r.querySelectorAll('.tag').map((t) => t.textContent), r.className])]);
    out.cols = [cmp().querySelectorAll('.xray-cmp').length, cols()];
    // Got it: the warning goes; the compare and trace results stay
    warn.querySelector('button').fire('click');
    await tick();
    out.acked = [cc().querySelectorAll('.brow')[0].querySelector('.ttl').textContent, $('toast-msg').textContent,
      cols().length, hits(cc()).length];
    out.posted = posts();
  } catch (e) { errors.push(String(e && e.stack || e)); }
  out.sinks = sinks;
  process.stdout.write(JSON.stringify(out) + '\n', () => process.exit(0));  // a pipe write is async on POSIX: exit once flushed
})();
"""  # noqa: E501  (JavaScript)


def test_the_trace_compare_and_warning_page(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    ts = (datetime.now(UTC) - timedelta(hours=3)).strftime(xray.TS)
    engines = {"id": "engines", "kind": "page", "title": "Engines", "pinned": False,
               "muted": False}
    profile = {"id": "me/profile", "kind": "me", "title": "Profile"}
    line = {"id": "^c-abcde", "kind": "claim", "title": "Uses Pipenv for every project.",
            "where": "preferences · Coding", "pinned": False, "muted": False}
    racing = {**line, "id": "^c-fghij", "title": "Weekend motor racing fan."}
    call_ = {"ts": ts, "tool": "keepfold_prime", "chars": 900,
             "served": [{"block": "related", "items": [engines]}], "trimmed": []}
    end = (datetime.now(UTC) - timedelta(days=2)).strftime(xray.TS)  # 2 local days back
    wide = {"n": 64, "day": end[:10], "end": end}
    profiles = {"profiles": [{"match": "claude", "scopes": ["all"]}], "unknown": ["core"]}
    clients = [{"client": "Claude Code", "name": "claude-code", "total": 1, "scopes": ["all"],
                "rule": "claude", "shared": [], "often": [], "wide": wide, "calls": [call_]},
               {"client": "mystery-app", "name": "mystery-app", "total": 1, "scopes": ["core"],
                "rule": None, "shared": [], "often": [], "wide": None, "calls": []}]
    view = {"pinned": [], "muted": [], "profiles": profiles, "profiles_max": 30,
            "all_scopes": ["all", "core", "coding", "personal"], "clients": clients}
    acked = {**view, "clients": [{**clients[0], "wide": None}, clients[1]]}
    traced = {"calls": 3, "hits": [
        {**line, "tool": "keepfold_prime", "ts": ts, "words": ["pipenv"]},
        {**engines, "tool": "keepfold_find", "ts": ts, "words": ["python", "setup"]},
        {"title": "a private page", "kind": "page", "tool": "keepfold_read", "ts": ts},
        {**profile, "tool": "keepfold_prime", "ts": ts, "words": ["python"]}]}
    compared = {
        "a": {"served": [{"block": "preferences", "items": [racing]},
                         {"block": "related", "items": [engines]}],
              "trimmed": [], "chars": 640, "withheld": []},
        "b": {"served": [{"block": "related", "items": [engines]}],
              "trimmed": [{"block": "profile", "items": [profile]}], "chars": 210,
              "withheld": ["coding", "personal"]}}
    status = {"first_run": False, "rows": [], "vault": "V", "land": "status",
              "update": {"current": "0.1.0", "mode": "installer"}}
    out = _run(node, tmp_path, "xray-trace", TRACE_JS, {
        "routes": {"/api/xray": view, "/api/xray/trace": traced, "/api/xray/compare": compared,
                   "/api/xray/ack": acked, "/api/review/retire": {"count": 0},
                   "/api/profiles": {"profiles": profiles}, "/api/status": status,
                   "/api/review": {"count": 0}}, "stream": []})
    assert out["errors"] == [] and out["sinks"] == [], out["errors"]
    assert out["boards"] == ["Pinned and muted", "Compare two apps", "Claude Code",
                             "mystery-app"]
    said = ("Claude Code read 64 pages in 15 minutes 2 days ago. Chats rarely need that "
            "many. If you didn't ask for it, narrow what it reads below, or remove the app "
            "under Connect.")
    assert out["warn"] == [said, ["Got it"], "Got it: Claude Code read 64 pages"]
    assert out["firstOther"] == []
    assert out["area"] == ["xray-trace-0", "4", "4000", [
        ["What would Claude Code get for this prompt?", "xray-dry-0"],
        ["Paste a reply from Claude Code that surprised you", "xray-trace-0"]]]
    assert out["disabled"] == [True, True, False] and out["plainEnter"] == 0
    assert out["busy"] == ["true", None, 1, "true", None]  # the app's busy dots (pressable)
    when = "served by {} · 3 hours ago"
    matched = ("Matched against what Keepfold gave Claude Code in its last 3 calls · the reply "
               "was not saved")
    assert out["traced"] == [[matched], [
        ["Uses Pipenv for every project.", [when.format("keepfold_prime"), "Shared words: pipenv"],
         ["Mute", "Retire"], ["Mute: Uses Pipenv for every project.",
                              "Retire: Uses Pipenv for every project."]],
        ["Engines", [when.format("keepfold_find"), "Shared words: python, setup"], ["Mute"],
         ["Mute: Engines"]],
        ["a private page", [when.format("keepfold_read")], [], []],
        ["Profile", [when.format("keepfold_prime"), "Shared words: python"], [], []]]]
    assert out["retired"] == ["You use Pipenv.", [matched],
                              ["Engines", "a private page", "Profile"],
                              "Retired. AI apps no longer get this line."]
    assert out["none"] == [[
        "Matched against what Keepfold gave mystery-app in its last 1 call · the reply was not "
        "saved", "None of what Keepfold gave mystery-app recently matches this reply. The reply "
        "may come from the app's own memory or the model itself."], 3]
    apps = [["claude-code", "Claude Code"], ["mystery-app", "mystery-app"]]
    assert out["cmpForm"] == [
        [["Prompt", "xray-cmp-prompt"], ["First app", "xray-cmp-a"],
         ["Second app", "xray-cmp-b"]],
        [["claude-code", apps], ["mystery-app", apps]],
        ["Dry run · nothing was sent or logged"], 0]
    assert out["show"] == [True, False, True, False]
    assert out["cols"] == [1, [
        ["Claude Code", "640 characters", [
            ["Weekend motor racing fan.", ["only Claude Code"], "brow"],
            ["Engines", [], "brow"]]],
        ["mystery-app", "210 characters", [
            ["Engines", [], "brow"], ["Profile", [], "brow cut"],
            ["Not given: coding", [], "brow cut"], ["Not given: personal", [], "brow cut"]]]]]
    assert out["acked"] == ["Engines", "Hidden until it reads that widely again.", 2, 3]
    assert out["posted"] == [
        ["/api/xray/trace", {"client": "claude-code", "text": "You use Pipenv."}],
        ["/api/review/retire", {"id": "c-abcde"}],
        ["/api/xray/trace", {"client": "mystery-app", "text": "Something new."}],
        ["/api/xray/compare", {"prompt": "car motor", "a": "claude-code", "b": "mystery-app"}],
        ["/api/xray/ack", {"client": "claude-code", "end": end}]]


def test_compare_columns_stack_on_a_narrow_window():
    css = (Path(xray.__file__).parent / "assets/app/app.css").read_text("utf-8")
    block = css[css.index("/* R27b"):css.index("pre {", css.index("/* R27b"))]
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in block  # no overflow
    narrow = block[block.index("@media (max-width: 640px)"):]
    assert "grid-template-columns: minmax(0, 1fr)" in narrow  # one column at 375px
    assert ".xray-col { min-width: 0; }" in block
    assert "#" not in block and "rgb" not in block  # theme tokens only, both themes
