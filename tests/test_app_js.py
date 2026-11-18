"""G10 (docs/design/v6/TEST-PLAN.md §1): run the app page's `app.js` under node with a small
stub DOM (only what app.js touches), the real index.html ids and a stubbed fetch/XHR answering
with the server's own /api/setup. The page boots into Setup, renders the steps in the server's
order, never uses innerHTML (static and runtime), and the upload progress event moves the
<progress> bar in place. Skipped without node."""

import json
import re
import shutil
import socket
import subprocess

import pytest

from cortex import localllm
from cortex.app import actions, server, status, targets

pytestmark = pytest.mark.usefixtures("home")
APP_JS = server.ASSETS / "app.js"
HTML_SINKS = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|DOMParser|"
                        r"createContextualFragment|\beval\(|new Function")

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const [appPath, htmlPath, dataPath] = process.argv.slice(2);
const DATA = JSON.parse(fs.readFileSync(dataPath, 'utf8'));
const errors = [], sinks = [], fetched = [], xhrs = [], replaced = [];
process.on('unhandledRejection', (e) => errors.push(String(e && e.stack || e)));
process.on('uncaughtException', (e) => errors.push(String(e && e.stack || e)));

class Node {
  constructor() { this.childNodes = []; this.parentNode = null; }
  get firstChild() { return this.childNodes[0] || null; }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(''); }
  set textContent(v) { this.replaceChildren(String(v)); }
  append(...ns) {
    for (let n of ns) {
      if (!(n instanceof Node)) n = new Text(String(n));
      if (n.parentNode) n.parentNode.childNodes.splice(n.parentNode.childNodes.indexOf(n), 1);
      n.parentNode = this;
      this.childNodes.push(n);
    }
  }
  replaceChildren(...ns) { for (const c of this.childNodes) c.parentNode = null; this.childNodes = []; this.append(...ns); }
}
class Text extends Node {
  constructor(data) { super(); this.data = data; }
  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
}
class Element extends Node {
  constructor(tag) { super(); this.tagName = tag.toUpperCase(); this.attributes = {}; this.listeners = {}; this.dataset = {}; this.hidden = false; }
  get className() { return this.attributes.class || ''; }
  set className(v) { this.attributes.class = String(v); }
  get classList() {
    const set = (cs) => { this.className = cs.join(' '); };
    const cs = () => this.className.split(/\s+/).filter(Boolean);
    return { add: (c) => set([...cs(), c]), remove: (c) => set(cs().filter((x) => x !== c)) };
  }
  get id() { return this.attributes.id || ''; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  fire(t, ev) { for (const fn of this.listeners[t] || []) fn({ preventDefault() {}, target: this, ...ev }); }
  focus() {}
  scrollIntoView() {}
  showModal() {}
  close() {}
  *walk() { for (const c of this.childNodes) if (c instanceof Element) { yield c; yield* c.walk(); } }
  matches(s) { return s.startsWith('.') ? this.className.split(/\s+/).includes(s.slice(1)) : this.tagName === s.toUpperCase(); }
  querySelectorAll(sel) {
    const [a, b] = sel.split(' ');
    let out = [...this.walk()].filter((e) => e.matches(a));
    if (b) out = out.flatMap((e) => [...e.walk()].filter((x) => x.matches(b)));
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
for (const k of ['innerHTML', 'outerHTML']) {
  Object.defineProperty(Element.prototype, k, { get() { sinks.push('get ' + k); return ''; }, set() { sinks.push('set ' + k); } });
}
Element.prototype.insertAdjacentHTML = function () { sinks.push('insertAdjacentHTML'); };

// the real page's elements (every id in index.html) and its nav buttons
const body = new Element('body');
const html = fs.readFileSync(htmlPath, 'utf8');
for (const m of html.matchAll(/<(\w+)([^>]*?)\sid="([^"]+)"([^>]*)>/g)) {
  const e = new Element(m[1]);
  e.setAttribute('id', m[3]);
  e.hidden = /\shidden\b/.test(m[2] + m[4]);
  body.append(e);
}
const nav = new Element('nav');
nav.className = 'side';
body.append(nav);
for (const m of html.matchAll(/<button type="button" data-s="(\w+)">/g)) {
  const b = new Element('button');
  b.dataset.s = m[1];
  nav.append(b);
}
const store = () => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)) }; };
Object.assign(globalThis, {
  Node, window: globalThis, sessionStorage: store(), localStorage: store(),
  location: { search: '?t=KEY123', hash: '' },
  history: { replaceState: (s, t, u) => replaced.push(u), pushState: (s, t, u) => replaced.push('push ' + u) },
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  addEventListener() {}, confirm: () => true,
  document: {
    documentElement: new Element('html'), body,
    getElementById: (id) => [...body.walk()].find((e) => e.id === id) || null,
    createElement: (t) => new Element(t), createElementNS: (ns, t) => new Element(t),
    querySelectorAll: (s) => body.querySelectorAll(s),
  },
  fetch: async (path, opts) => {
    const o = opts || {};
    fetched.push({ path, method: o.method || 'GET', token: (o.headers || {})['X-Cortex-Token'], body: o.body ? JSON.parse(o.body) : null });
    const data = DATA.routes[path] || { job: 'stub', seq: 1 };
    return { status: 200, ok: true, json: async () => JSON.parse(JSON.stringify(data)) };
  },
  XMLHttpRequest: class { constructor() { this.headers = {}; this.upload = {}; xhrs.push(this); }
    open(m, u) { this.method = m; this.url = u; } setRequestHeader(k, v) { this.headers[k] = v; } send(b) { this.sent = b; } },
});

const tick = async () => { for (let i = 0; i < 50; i++) await new Promise((r) => setImmediate(r)); };
const $ = (id) => document.getElementById(id);
const rowBtn = (title, text) => [...body.querySelectorAll('.brow')]
  .find((r) => (r.querySelector('.ttl') || {}).textContent === title)
  .querySelectorAll('button').find((b) => b.textContent === text);
const out = {};
(async () => {
  try {
    vm.runInThisContext(fs.readFileSync(appPath, 'utf8'), { filename: appPath });
    await tick();
    out.replaced = replaced;
    out.hidden = Object.fromEntries(['setup', 'ask', 'status', 'connect', 'llm', 'browse', 'vault'].map((s) => [s, $('s-' + s).hidden]));
    out.steps = $('setup').querySelectorAll('.step').map((b) => b.querySelector('h2').textContent);
    out.expanded = $('setup').querySelectorAll('.step').filter((b) => !b.className.split(' ').includes('collapsed'))
      .map((b) => b.querySelector('h2').textContent);
    out.bandNotes = $('setup').querySelectorAll('.step').map((b) => b.querySelector('.band-note').textContent);
    out.tokens = [...new Set(fetched.map((f) => f.token))];
    out.wordmark = $('wordmark').childNodes.length;
    out.startLabels = $('setup').querySelectorAll('button').map((b) => b.getAttribute('aria-label'))
      .filter((l) => l && l.startsWith('Start: '));
    // Add your history: ChatGPT, step 2, choose a file; the XHR's progress moves the bar
    rowBtn('ChatGPT', 'Start').fire('click');
    await tick();
    [...$('setup').querySelectorAll('button')].find((b) => b.textContent === 'I have the file').fire('click');
    await tick();
    const file = $('imp-file');
    file.files = [{ name: 'my export.zip', size: 4 * 2 ** 20 }];
    file.fire('change');
    await tick();
    const bar = $('up-bar'), x = xhrs[0];
    out.xhr = { method: x.method, url: x.url, headers: x.headers, sent: x.sent };
    out.before = { max: bar.getAttribute('max'), value: bar.value, text: $('up-text').textContent };
    x.upload.onprogress({ lengthComputable: true, loaded: 2 ** 20, total: 4 * 2 ** 20 });
    out.after = { same: $('up-bar') === bar, value: bar.value, text: $('up-text').textContent };
    x.upload.onprogress({ lengthComputable: false, loaded: 3 * 2 ** 20 });
    out.uncomputable = bar.value;
    Object.assign(x, { status: 200, responseText: JSON.stringify({ upload: 'u1', size: 4 * 2 ** 20 }) });
    x.onload();
    await tick();
    out.preview = fetched.filter((f) => f.path === '/api/import/preview').map((f) => f.body);
    out.barGone = $('up-bar') === null;
    // Connect's Skip is kept by the server (the landing rule); opening it again un-skips
    const connectStep = () => $('setup').querySelectorAll('.step').find((b) => b.querySelector('h2').textContent === 'Connect AI apps');
    const stepBtn = (text) => connectStep().querySelectorAll('button').find((b) => b.textContent === text);
    DATA.routes['/api/setup'].connect_skipped = true;  // Connect is the current step: Skip shows
    stepBtn('Skip').fire('click');
    await tick();
    out.skipNote = connectStep().querySelector('.band-note').textContent;
    stepBtn('Open').fire('click');
    await tick();
    out.skips = fetched.filter((f) => f.path === '/api/setup/skip').map((f) => f.body);
    // Ask with neither the search model nor a local model: both rows, each with its own button
    DATA.routes['/api/ask'] = { vault: true, embed: false };
    DATA.routes['/api/job'] = { seq: 2, job: { name: 'Asking your archive', section: 'ask', running: false, cancellable: false, line: '', seconds: 1, error: null, result: { kind: 'ask', error: 'missing', missing: ['embed', 'llm'] } } };
    nav.querySelectorAll('button').find((b) => b.dataset.s === 'ask').fire('click');
    await tick();
    $('ask-q').value = 'binary search';
    $('ask').querySelectorAll('button').find((b) => b.textContent === 'Ask').fire('click');
    await tick();
    out.missing = $('ask').querySelectorAll('.brow').map((r) => [r.querySelector('.ttl').textContent, r.querySelector('button').textContent]);
    out.pushed = replaced.filter((u) => u.startsWith('push '));
    // the Local model page says why a refused config is not used
    DATA.routes['/api/llm'] = { available: true, enabled: true, runner: 'auto', model: 'm-cloud', lms: null, server_port: null, loaded: [], ollama: null, models: [], error: 'localllm: a cloud model is refused' };
    nav.querySelectorAll('button').find((b) => b.dataset.s === 'llm').fire('click');
    await tick();
    out.llmText = $('llm').textContent;
    // Status → Updates: Check asks GitHub only on the click; Update now asks first, then the
    // page says Cortex is closing to install it
    DATA.routes['/api/status'] = { first_run: false, rows: [], vault: 'V', land: 'status', update: { current: '0.1.0', mode: 'installer' } };
    nav.querySelectorAll('button').find((b) => b.dataset.s === 'status').fire('click');
    await tick();
    const updRows = () => $('status').querySelectorAll('.brow').map((r) => r.querySelector('.ttl').textContent);
    out.updBefore = { rows: updRows(), asked: fetched.some((f) => f.path.startsWith('/api/update')) };
    const checked = { current: '0.1.0', latest: '0.2.0', newer: true, notes_url: 'https://github.com/o/r/releases/tag/v0.2.0', mode: 'installer', size: 31 * 2 ** 20 };
    DATA.routes['/api/job'] = { seq: 3, job: { name: 'Checking for updates', section: 'update', running: false, cancellable: false, line: '', seconds: 1, error: null, result: checked } };
    rowBtn('Cortex 0.1.0', 'Check for updates').fire('click');
    await tick();
    const avail = $('status').querySelectorAll('.brow').find((r) => r.querySelector('.ttl').textContent === 'Cortex 0.2.0 is available');
    const a = avail.querySelector('a');
    out.avail = { rows: updRows(), link: [a.getAttribute('href'), a.getAttribute('rel'), a.getAttribute('target'), a.textContent],
      buttons: avail.querySelectorAll('button').map((b) => b.textContent) };
    avail.querySelectorAll('button').find((b) => b.textContent === 'Update now').fire('click');
    await tick();
    out.updConfirm = { title: $('confirm-title').textContent, ok: $('confirm-ok').textContent,
      body: $('confirm-body').textContent, applied: fetched.some((f) => f.path === '/api/update/apply') };
    DATA.routes['/api/job'] = { seq: 4, job: { name: 'Updating Cortex', section: 'update', running: false, cancellable: true, line: '', seconds: 9, error: null, result: { ...checked, launched: true } } };
    $('confirm-ok').onclick();
    await tick();
    out.updAfter = { posts: fetched.filter((f) => f.path.startsWith('/api/update')).map((f) => f.path), gone: $('gone').hidden ? '' : $('gone').textContent };
    // Quit asks first in the page's own dialog; only its OK posts /api/quit
    $('quit').fire('click');
    await tick();
    out.quit = { title: $('confirm-title').textContent, ok: $('confirm-ok').textContent,
      body: $('confirm-body').textContent, before: fetched.some((f) => f.path === '/api/quit') };
    $('confirm-ok').onclick();
    await tick();
    out.quit.after = fetched.some((f) => f.path === '/api/quit');
  } catch (e) { errors.push(String(e && e.stack || e)); }
  out.errors = errors;
  out.sinks = sinks;
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""  # noqa: E501  (JavaScript)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_app_js_has_no_html_sinks():
    assert not HTML_SINKS.findall(APP_JS.read_text(encoding="utf-8"))


def test_app_js_boots_into_setup_and_upload_progress(root_committed, tmp_path, monkeypatch):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    def no_runner(*a, **k):
        raise OSError("no runner in tests")

    monkeypatch.setattr(localllm, "_request", no_runner)
    monkeypatch.setattr(localllm, "_lms_exe", lambda cfg: None)
    monkeypatch.setattr(targets, "_which", lambda c: None)
    monkeypatch.setattr(actions, "_popen", lambda *a, **k: pytest.fail("started a process"))
    monkeypatch.setattr(status, "RECEIVER_PORT", _free_port())
    (root_committed / ".cortex").mkdir(exist_ok=True)
    setup = server.App(root_committed).setup()  # the server's own shape, not a hand-made one
    data = tmp_path / "data.json"
    data.write_text(json.dumps({"routes": {
        "/api/status": {"first_run": True, "rows": [], "vault": None, "land": "setup"},
        "/api/setup": setup, "/api/job": {"job": None, "seq": 0}}}), encoding="utf-8")
    harness = tmp_path / "harness.cjs"
    harness.write_text(HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(harness), str(APP_JS), str(server.ASSETS / "index.html"),
                        str(data)], capture_output=True, text=True, encoding="utf-8",
                       timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["errors"] == [] and out["sinks"] == [], out
    # boots: the key leaves the URL and goes in the header; Setup shows, the rest hide
    assert out["replaced"][0] == "/" and out["tokens"] == ["KEY123"]
    assert out["hidden"] == {s: s != "setup" for s in out["hidden"]}
    assert out["wordmark"] == 1
    assert setup["order"] == ["vault", "connect", "embed", "history", "llm"]
    assert out["steps"] == ["Vault", "Connect AI apps", "Search model",
                            "Add your history (optional)", "Local model (optional)", "Try it"]
    # one step at a time: the vault is done (collapsed, check mark), Connect is current and
    # the only one expanded, the rest wait collapsed under "Later"
    assert out["expanded"] == ["Connect AI apps"]
    assert out["bandNotes"][0].startswith("✓ ")  # [1] is the NEXT dot-matrix cell
    assert out["bandNotes"][2:] == ["Later"] * 4
    # the upload: raw octet-stream XHR with the key, the name URL-encoded; the bar moves in place
    x = out["xhr"]
    assert (x["method"], x["url"]) == ("POST", "/api/import/upload")
    assert x["headers"] == {"X-Cortex-Token": "KEY123", "Content-Type": "application/octet-stream",
                            "X-Cortex-Name": "my%20export.zip"}
    assert out["before"] == {"max": str(4 * 2**20), "value": 0, "text": "0% · 0.0 of 4.0 MB"}
    assert out["after"] == {"same": True, "value": 2**20, "text": "25% · 1.0 of 4.0 MB"}
    assert out["uncomputable"] == 2**20
    assert out["preview"] == [{"source": "chatgpt", "upload": "u1"}] and out["barGone"]
    # every history card's Start names its source; notes and calendar are cards with guides
    assert "Start: Notes (Obsidian, Notion or a folder)" in out["startLabels"]
    assert "Start: Calendar (Google, Outlook or Apple)" in out["startLabels"]
    assert "Start: Browser history" in out["startLabels"]
    # R3-2: Ask names both missing pieces at once; a section click is a Back step (R3-10)
    assert out["missing"] == [["Search model", "Set up the search model"],
                              ["Local model", "Set up a local model"]]
    assert out["pushed"] == ["push #ask"]
    assert out["skipNote"] == "Skipped"
    assert out["skips"] == [{"skipped": True}, {"skipped": False}]
    assert "Not used: localllm: a cloud model is refused" in out["llmText"]
    assert out["updBefore"] == {"rows": ["Cortex 0.1.0"], "asked": False}
    assert out["avail"] == {"rows": ["Cortex 0.1.0", "Cortex 0.2.0 is available"],
                            "link": ["https://github.com/o/r/releases/tag/v0.2.0",
                                     "noopener noreferrer", "_blank", "What’s new"],
                            "buttons": ["Update now"]}
    assert out["updConfirm"] == {
        "title": "Update Cortex?", "ok": "Update now", "applied": False,
        "body": "Cortex will download the update (about 31 MB), close, and reopen when it's "
                "done. AI apps connected to Cortex may need a restart."}
    assert out["updAfter"]["posts"] == ["/api/update/check", "/api/update/apply"]
    assert out["updAfter"]["gone"].startswith("Cortex is closing to install the update.")
    assert out["quit"] == {"title": "Stop Cortex?", "ok": "Quit", "before": False, "after": True,
                           "body": "Running jobs are cancelled; your vault is not affected."}
