"""The offline dashboard in a real headless Chrome or Edge (driven over the DevTools protocol by
node, which has a WebSocket built in): every control in every section, panel and brain view is
clicked or changed and must do something visible without an uncaught error; copy buttons show
Copied for 1.5 s and fail in an error toast; Tab reaches every control with a visible focus
ring; the tabs take the arrow keys; reduced motion stops the transitions. An empty vault says
one helpful sentence wherever a list is empty. Skipped without node or a Chromium browser."""

import importlib.util
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_brainmap import made  # noqa: F401  (a fixture: topic hubs, pages and vectors)

from cortex import dashboard, taint
from cortex.cli import _init
from cortex.vault import Vault

pytestmark = pytest.mark.slow
ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
CHECK = "M5 12.5l4.5 4.5L19 7.5"  # the check icon's path in dashboard.js
COPY = "M9 9h10v10H9z"


def _browser() -> str | None:
    """A Chromium browser (read at import, before conftest points the app-data folders at a
    temp home): CORTEX_TEST_BROWSER, one on PATH, or the usual install places."""
    if os.environ.get("CORTEX_TEST_BROWSER"):
        return os.environ["CORTEX_TEST_BROWSER"]
    for name in ("chrome", "msedge", "google-chrome", "chromium", "chromium-browser"):
        if found := shutil.which(name):
            return found
    places = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
              Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        if os.environ.get(var):
            base = Path(os.environ[var])
            places += [base / "Google/Chrome/Application/chrome.exe",
                       base / "Microsoft/Edge/Application/msedge.exe"]
    return next((str(p) for p in places if p.is_file()), None)


BROWSER = _browser()
# the browser runs with the real environment: conftest's temp app-data folders confuse it (its
# profile is a fresh folder under tmp_path all the same)
BROWSER_ENV = dict(os.environ)

# node driver: start the browser headless, open the page, run the phases, print one JSON line
DRIVER = r"""
const { spawn } = require('child_process');
const fs = require('fs'), path = require('path');
const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const J = JSON.stringify;
const proc = spawn(cfg.browser, ['--headless=new', '--remote-debugging-port=0', '--user-data-dir=' + cfg.profile,
  '--no-first-run', '--no-default-browser-check', '--disable-extensions', '--disable-sync',
  '--disable-background-networking', '--mute-audio', '--window-size=1400,900', 'about:blank'], { stdio: 'ignore' });
const out = { errors: [], scopes: {}, walk: {} };
let cur = 'load';
// a page that hangs must not hold node and the browser until pytest's timeout: kill both
setTimeout(() => {
  try { proc.kill(); } catch (e) { /* already gone */ }
  console.log(J({ fatal: `watchdog: no finish in ${cfg.limit || 300} s (at ${cur})` }));
  process.exit(3);
}, (cfg.limit || 300) * 1000).unref();
(async () => {
  let port = null;
  for (let i = 0; i < 400 && !port; i++) {
    try { port = fs.readFileSync(path.join(cfg.profile, 'DevToolsActivePort'), 'utf8').split('\n')[0].trim() || null; }
    catch (e) { await sleep(50); }
  }
  const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const ws = new WebSocket(list.find((t) => t.type === 'page').webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener('open', r));
  let id = 0;
  const waiting = new Map(), once = [];
  ws.addEventListener('message', (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && waiting.has(msg.id)) { waiting.get(msg.id)(msg); waiting.delete(msg.id); return; }
    if (msg.method === 'Runtime.exceptionThrown') {
      const d = msg.params.exceptionDetails;
      out.errors.push(cur + ': ' + ((d.exception && d.exception.description) || d.text));
    } else if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
      out.errors.push(cur + ': console.error ' + msg.params.args.map((a) => a.value || a.description).join(' '));
    }
    for (const w of once.splice(0)) if (!w(msg)) once.push(w);
  });
  const send = (method, params) => new Promise((r) => { const i = ++id; waiting.set(i, r); ws.send(J({ id: i, method, params: params || {} })); });
  const ev = async (expression) => {
    const r = (await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })).result;
    if (r.exceptionDetails) throw new Error(cur + ': ' + (r.exceptionDetails.exception || {}).description);
    return r.result.value;
  };
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.addScriptToEvaluateOnNewDocument', { source: fs.readFileSync(cfg.helpers, 'utf8') });
  await send('Emulation.setDeviceMetricsOverride', { width: 1400, height: 900, deviceScaleFactor: 1, mobile: false });
  if (cfg.reduced) await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
  const url = cfg.url || 'file:///' + path.resolve(cfg.page).replace(/\\/g, '/');
  async function load() {
    const loaded = new Promise((r) => once.push((m) => m.method === 'Page.loadEventFired' && (r(), true)));
    await send('Page.navigate', { url });
    await loaded;
    for (let i = 0; i < 100 && !(await ev('!!document.body.dataset.brainReadyMs')); i++) await sleep(50);
  }
  const key = async (k, code, vk) => {
    await send('Input.dispatchKeyEvent', { type: 'keyDown', key: k, code, windowsVirtualKeyCode: vk });
    await send('Input.dispatchKeyEvent', { type: 'keyUp', key: k, code, windowsVirtualKeyCode: vk });
  };
  await load();
  if (cfg.sweep) {
    for (const scope of await ev('__T.scopes()')) {
      await load();
      cur = scope;
      const keys = await ev(`__T.keys(${J(scope)})`);
      out.scopes[scope] = {};
      for (const k of keys) {
        cur = scope + ' / ' + k;
        const hit = async (fail) => {
          const r = await ev(`__T.hit(${J(scope)}, ${J(k)}, ${fail})`);
          if (!r.gone) return r;
          await load();  // an earlier control filtered it away, or it still says Copied
          return ev(`__T.hit(${J(scope)}, ${J(k)}, ${fail})`);
        };
        const r = await hit(false);
        if (r.copied && r.copied.length) r.bad = await hit(true);
        out.scopes[scope][k] = r;
      }
    }
  }
  if (cfg.walk) {  // real Tab presses from each section's tab round the page
    for (const s of await ev('__T.SECTIONS')) {
      await load();
      cur = 'walk ' + s;
      await ev(`__T.go(${J(s)}).then(() => document.getElementById('tab-${s}').focus())`);
      const seen = [];
      for (let i = 0; i < 250; i++) {
        await key('Tab', 'Tab', 9);
        const f = await ev('__T.focused()');
        if (f && seen.length && f.path === seen[0].path) break;
        if (f) seen.push(f);
      }
      out.walk[s] = seen;
    }
  }
  if (cfg.checks) { await load(); cur = 'checks'; out.checks = await ev('__T.checks()'); }
  if (cfg.texts) { await load(); cur = 'texts'; out.texts = await ev('__T.texts()'); }
  if (cfg.stem) {  // the brain stem: a picture of it, then a site chosen and its panel read
    cur = 'stem';
    const shot = async (file) => { const s = await send('Page.captureScreenshot', { format: 'png' }); fs.writeFileSync(file, Buffer.from(s.result.data, 'base64')); };
    await ev('__L.ready()');
    await shot(cfg.stem.shots[0]);
    out.stem = await ev(`__L.stem(${J(cfg.stem)})`);
    await sleep(2500);
    await shot(cfg.stem.shots[1]);
    // a first visit in a 1024x768 window: the legend starts folded so it leaves the stem clear
    await ev("localStorage.removeItem('cortex.legend')");
    await send('Emulation.setDeviceMetricsOverride', { width: 1024, height: 768, deviceScaleFactor: 1, mobile: false });
    await load();
    out.stem.smallLegend = await ev("__T.go('brain').then(() => [document.getElementById('legend-toggle').getAttribute('aria-expanded'), document.getElementById('legend').hidden, localStorage.getItem('cortex.legend')])");
  }
  if (cfg.live) {  // the page as loaded: move the camera, let the test add a note, watch
    cur = 'live';
    const before = await ev('__L.before()');
    fs.writeFileSync(cfg.ready, '1');
    out.live = { before, after: await ev(`__L.after(${J(cfg.live)})`), leak: await ev('__L.leak(30)') };
  }
  const v = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
  const bws = new WebSocket(v.webSocketDebuggerUrl);
  await new Promise((r) => bws.addEventListener('open', r));
  bws.send(J({ id: 1, method: 'Browser.close' }));
  await sleep(300);
})().catch((e) => { out.fatal = String(e && e.stack || e); }).finally(() => {
  console.log(J(out));
  try { proc.kill(); } catch (e) { /* gone */ }
  process.exit(0);
});
"""  # noqa: E501  (JavaScript)

# in the page from its first line: the clipboard stub, error capture and the sweep's helpers
HELPERS = r"""
window.__T = (() => {
  const clip = [], errs = [];
  let failCopy = false;
  addEventListener('error', (e) => errs.push(String(e.error && e.error.stack || e.message)));
  addEventListener('unhandledrejection', (e) => errs.push(String(e.reason && e.reason.stack || e.reason)));
  if (navigator.clipboard) navigator.clipboard.writeText = async (t) => { if (failCopy) throw new Error('denied'); clip.push(t); };
  const exec = Document.prototype.execCommand;
  Document.prototype.execCommand = function (...a) { return failCopy ? false : exec.apply(this, a); };
  const $ = (id) => document.getElementById(id);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const SECTIONS = ['home', 'brain', 'library', 'you', 'timeline', 'ideas', 'study', 'help'];
  const visible = (e) => e.getClientRects().length > 0 && !e.closest('[hidden]') && !e.disabled;
  const go = async (s) => { $('tab-' + s).click(); await sleep(40); };
  const view = async (v) => {
    await go('brain');
    const b = $('viewswitch').querySelector(`[data-view="${v}"]`);
    if (b && visible(b)) b.click();
    await sleep(80);
  };
  const first = async (root, sel) => { const e = [...root.querySelectorAll(sel)].find(visible); if (e) e.click(); await sleep(80); };
  const section = (s) => [() => $('view-' + s), () => go(s)];
  const SCOPES = {
    header: [() => $('top'), () => go('home')],
    home: section('home'),
    'brain 3d': [() => $('view-brain'), () => view('3d')],
    'brain growth': [() => $('brain-tools'), async () => { await view('3d'); await first($('brain-tools'), '[data-mode="growth"]'); }],
    'brain 2d': [() => $('view-brain'), () => view('2d')],
    'brain list': [() => $('view-brain'), () => view('list')],
    library: section('library'), you: section('you'), timeline: section('timeline'), ideas: section('ideas'),
    study: section('study'), help: section('help'),
    reader: [() => $('reader'), async () => { await go('home'); await first($('departures'), '.ttl.linkbtn'); }],
    'reader note': [() => $('reader'), async () => { await go('ideas'); await first($('view-ideas'), '.ttl.linkbtn'); }],
    inspector: [() => $('reader'), async () => { await view('3d'); await first($('outline'), '.lobe'); }],
    'inspector page': [() => $('reader'), async () => {
      await view('3d'); await first($('outline'), '.lobe'); await first($('reader'), '.ilist .linkbtn'); }],
    look: [() => $('look'), async () => { await view('3d'); await first($('brain-tools'), '[aria-controls="look"]'); }],
    palette: [() => $('palette'), async () => {
      $('open-search').click(); $('pq').value = 'review'; $('pq').dispatchEvent(new Event('input')); await sleep(250); }],
  };
  const SEL = 'button, input, select, [role="button"], [role="treeitem"], summary';
  const label = (e) => {
    const own = e.matches('[role="treeitem"]') ? e.firstElementChild : e;
    return (e.getAttribute('aria-label') || own.textContent.trim() || e.title || e.id || e.tagName).replace(/\s+/g, ' ').slice(0, 60);
  };
  function controls(scope) {
    const root = SCOPES[scope][0](), seen = {}, found = [];
    if (!root || !visible(root)) return found;
    for (const e of root.querySelectorAll(SEL)) {
      if (!visible(e)) continue;
      const k = label(e);
      seen[k] = (seen[k] || 0) + 1;
      found.push({ key: seen[k] > 1 ? `${k} #${seen[k]}` : k, el: e });
    }
    return found;
  }
  const sig = () => [document.body.innerText, location.hash, document.documentElement.dataset.theme, clip.length,
    [...document.querySelectorAll('[aria-pressed],[aria-expanded],[aria-selected],[aria-current]')]
      .map((e) => ['aria-pressed', 'aria-expanded', 'aria-selected', 'aria-current'].map((a) => e.getAttribute(a)).join()).join(';'),
    [...document.querySelectorAll('input, select')].map((i) => i.value + i.checked).join(';'),
    document.querySelectorAll('.dim, .hide, .sel, .show').length, $('brain-live').textContent].join('\u0001');
  function act(e) {
    const fire = (t) => e.dispatchEvent(new Event(t, { bubbles: true }));
    if (e.tagName === 'INPUT' && e.type === 'search') { e.value = e.value === 'binary' ? 'loop' : 'binary'; fire('input'); }
    else if (e.tagName === 'INPUT' && e.type === 'range') { e.value = e.value === e.min ? e.max : e.min; fire('input'); fire('change'); }
    else if (e.tagName === 'SELECT') { const o = [...e.options].find((x) => x.value !== e.value); if (o) e.value = o.value; fire('change'); }
    else if (e instanceof HTMLElement) e.click();
    else e.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  }
  async function hit(scope, key, fail) {
    for (let i = 0; i < 3; i++) document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    $('toast').hidden = true;
    await SCOPES[scope][1]();
    const c = controls(scope).find((x) => x.key === key);
    if (!c) return { gone: true };
    const e = c.el, before = sig(), clip0 = clip.length, err0 = errs.length;
    const current = e.getAttribute('aria-pressed') === 'true' || e.getAttribute('aria-selected') === 'true' || e.hasAttribute('aria-current');
    failCopy = fail;
    act(e);
    let changed = false;
    for (let t = 0; t < 25 && !changed; t++) { await sleep(20); changed = sig() !== before; }
    await sleep(30);
    failCopy = false;
    return { changed, current, copied: clip.slice(clip0), face: e.isConnected ? e.textContent.trim() : null,
      check: e.isConnected && !!e.querySelector(`path[d="CHECK"]`), toast: $('toast').hidden ? null : $('toast').textContent,
      toastCls: $('toast').className, toastRole: $('toast').getAttribute('role'), errors: errs.slice(err0) };
  }
  function focused() {
    const a = document.activeElement;
    if (!a || a === document.body) return null;
    const t = a.matches('[role="treeitem"]') ? a.firstElementChild : a, cs = getComputedStyle(t);
    return { path: [...document.querySelectorAll('*')].indexOf(a), key: label(a), fv: a.matches(':focus-visible'),
      ring: cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) >= 2 };
  }
  async function checks() {
    const r = {}, key = (k) => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true }));
    await go('home');
    r.tabs = [...$('tabs').children].map((t) => [t.getAttribute('role'), t.getAttribute('aria-selected'), t.tabIndex]);
    $('tab-home').focus();
    const step = async (k) => { key(k); await sleep(40); const a = document.activeElement.id; return [a, $(a).getAttribute('aria-selected'), $(a.replace('tab-', 'view-')).hidden]; };
    r.keys = [await step('ArrowRight'), await step('End'), await step('Home'), await step('ArrowLeft')];
    const sel = $('section-select');
    sel.value = 'study';
    sel.dispatchEvent(new Event('change'));
    await sleep(40);
    r.select = [$('tab-study').getAttribute('aria-selected'), $('view-study').hidden];
    // an icon-only copy button: a check and the toast, then the copy icon again after 1.5 s
    await go('home');
    const ib = [...$('view-home').querySelectorAll('button.act')].find((b) => (b.getAttribute('aria-label') || '').startsWith('Copy'));
    ib.click();
    await sleep(60);
    r.icon = [!!ib.querySelector('path[d="CHECK"]'), ib.textContent, $('toast').hidden ? null : $('toast').textContent];
    await sleep(1600);
    r.iconAfter = [!!ib.querySelector('path[d="CHECK"]'), !!ib.querySelector('path[d="COPY"]')];
    // a labelled copy button: a check and Copied, announced; a second click keeps the old face
    await go('study');
    const cb = [...$('coming').querySelectorAll('button.btn')][0];
    cb.click();
    await sleep(60);
    r.text = [cb.textContent.trim(), !!cb.querySelector('path[d="CHECK"]'), $('announce').textContent];
    cb.click();
    await sleep(60);
    r.again = cb.textContent.trim();
    await sleep(1600);
    r.textAfter = cb.textContent.trim();
    failCopy = true;
    cb.click();
    await sleep(60);
    failCopy = false;
    r.fail = [$('toast').textContent, $('toast').className, $('toast').getAttribute('role'), cb.textContent.trim()];
    await view('3d');
    const spin = [...$('brain-tools').querySelectorAll('button')].find((b) => b.textContent === 'Spin');
    r.motion = [getComputedStyle($('tab-home')).transitionDuration, getComputedStyle($('view-brain')).animationName,
      spin && spin.getAttribute('aria-pressed')];
    return r;
  }
  async function texts() {  // every visible empty-state line, per section and brain view
    const out = {};
    const grab = (root) => [...root.querySelectorAll('.empty, .foot')].filter(visible).map((e) => e.textContent.trim());
    for (const s of SECTIONS) { await go(s); out[s] = grab($('view-' + s)); }
    await view('2d'); out['brain 2d'] = grab($('view-brain'));
    await view('list'); out['brain list'] = grab($('view-brain'));
    return out;
  }
  return { SECTIONS, go, scopes: () => Object.keys(SCOPES), keys: async (s) => { await SCOPES[s][1](); return controls(s).map((c) => c.key); }, hit, focused, checks, texts };
})();
""".replace("CHECK", CHECK).replace("COPY", COPY)  # noqa: E501  (JavaScript)


# the live page's extras: every brain mounted (through window.CortexBrain), camera and feed probes
LIVE = r"""
window.__L = (() => {
  const mounts = [], sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let real = null, opts = null;
  Object.defineProperty(window, 'CortexBrain', { configurable: true, get: () => real, set(v) {
    real = v;
    const mount = v.mount;
    v.mount = (c, o) => { opts = o; const b = mount(c, o); mounts.push(b); return b; };
  } });
  const cam = () => mounts[mounts.length - 1].camera;
  const same = (a, b) => Object.keys(a).every((k) => Math.abs(a[k] - b[k]) < 1e-6);
  const feed = () => (document.querySelector('.feed') || {}).textContent || '';
  async function settle() {  // until the camera stops moving
    let a = cam();
    for (let i = 0; i < 50; i++) { await sleep(150); const b = cam(); if (same(a, b)) return b; a = b; }
    return a;
  }
  async function before() {
    for (let i = 0; i < 200 && !mounts.length; i++) await sleep(100);
    const lobe = (opts.brain.lobes || [])[0];  // a focused lobe stays focused through the remount
    if (lobe) { mounts[mounts.length - 1].focusCluster(lobe.id); await settle(); }
    const start = await settle();
    const box = document.querySelector('.brain-host').firstElementChild, r = box.getBoundingClientRect();
    box.dispatchEvent(new WheelEvent('wheel', { deltaY: -400, clientX: r.left + r.width / 3,
      clientY: r.top + r.height / 3, bubbles: true, cancelable: true }));
    const cam = await settle();
    return { moved: !same(start, cam), cam, feed: feed(), focused: mounts[mounts.length - 1].focused };
  }
  async function after(title) {
    const n = mounts.length, t0 = Date.now();
    while (Date.now() - t0 < 20000 && !(feed().includes(title) && mounts.length > n)) await sleep(100);
    const waited = Date.now() - t0;
    await sleep(400);
    return { waited, feed: feed().includes(title), mounts: mounts.length - n, cam: cam(),
      focused: mounts[mounts.length - 1].focused,
      fresh: [...document.querySelectorAll('.feed .brow.fresh')].map((e) => e.textContent),
      stale: document.getElementById('stale').hidden,
      llm: !!document.querySelector('.quick-chat a[href="/manage#llm"]'),
      settings: !!document.querySelector('a[aria-label="Settings"][href="/manage"]') };
  }
  async function leak(n) {  // n more mounts and destroys must not cost the page its WebGL context
    const cv = document.querySelector('.brain-host canvas'), gl = cv && cv.getContext('webgl2');
    const box = document.createElement('div');
    box.style.width = '320px'; box.style.height = '240px';
    document.body.append(box);
    for (let i = 0; i < n; i++) {
      const b = real.mount(box, { brain: opts.brain, pages: opts.pages, today: opts.today });
      await sleep(20);
      b.destroy();
    }
    box.remove();
    await sleep(300);
    return { webgl2: !!gl, lost: gl ? gl.isContextLost() : null, connected: !!(cv && cv.isConnected) };
  }
  async function ready() {  // Home, its brain mounted and still
    for (let i = 0; i < 200 && !mounts.length; i++) await sleep(100);
    await settle();
  }
  async function stem(c) {  // a site chosen as a click would: the inspector fills from the app's route
    const b = mounts[mounts.length - 1], box = document.querySelector('.brain-host').firstElementChild;
    const n = b.node(c.pick), out = { label: box.getAttribute('aria-label'), absent: c.absent.map((id) => b.node(id)),
      node: n && { kind: n.kind, title: n.title, private: n.private } };
    opts.onSelect(n, b.explore(c.pick));
    const r = document.getElementById('reader');
    for (let i = 0; i < 100 && !r.querySelector('h4'); i++) await sleep(50);
    out.reader = { text: r.textContent, tags: [...r.querySelectorAll('*')].map((e) => e.tagName.toLowerCase()),
      heads: [...r.querySelectorAll('h4')].map((h) => h.textContent) };
    await __T.go('brain');
    out.legend = document.getElementById('legend').textContent;
    // the legend: open on the first visit (stored closed for the next), a click toggles and keeps it
    const lt = document.getElementById('legend-toggle'), lg = document.getElementById('legend');
    const lstate = () => [lt.getAttribute('aria-expanded'), lg.hidden, localStorage.getItem('cortex.legend')];
    out.legendUi = { first: lstate(), dots: lg.querySelectorAll('.sw .dot').length };
    lt.click();
    out.legendUi.closed = lstate();
    lt.click();
    out.legendUi.reopened = lstate();
    // the Stem button flies to the stem and, pressed again, back
    const sb = [...document.querySelectorAll('button')].find((x) => x.textContent.trim() === 'Stem' && x.offsetParent);
    out.stemBtn = [!!sb];
    if (sb) {
      const c0 = await settle();
      sb.click();
      const c1 = await settle();
      out.stemBtn.push(sb.getAttribute('aria-pressed'), b.atStem, !same(c0, c1));
      sb.click();
      await settle();
      out.stemBtn.push(sb.getAttribute('aria-pressed'), b.atStem);
    }
    // the outline's Browsing group, by keyboard: open it, step to a site, Enter opens its panel
    const ri = document.querySelector('#outline [data-id="region:browsing"]');
    const key_ = (k) => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }));
    out.outline = { region: !!ri };
    if (ri) {
      ri.focus();
      out.outline.closed = ri.getAttribute('aria-expanded');
      key_('ArrowRight');
      out.outline.opened = ri.getAttribute('aria-expanded');
      key_('ArrowDown');
      const li = document.activeElement;
      out.outline.sites = ri.querySelectorAll('.site').length;
      out.outline.id = li.dataset.id;
      r.replaceChildren();
      key_('Enter');
      for (let i = 0; i < 100 && !r.querySelector('h4'); i++) await sleep(50);
      out.outline.panel = r.textContent.includes(li.dataset.id.slice(5));
      out.outline.selected = li.getAttribute('aria-selected');
    }
    const key = sessionStorage.getItem('cortex.key');
    out.codes = [];
    for (const s of c.sites) out.codes.push((await fetch('/api/home/site?site=' + encodeURIComponent(s), { headers: { 'X-Cortex-Token': key } })).status);
    return out;
  }
  return { before, after, leak, ready, stem };
})();
"""  # noqa: E501  (JavaScript)


def _run(tmp_path, page: Path | None, **cfg) -> dict:
    node = shutil.which("node")
    if node is None or BROWSER is None:
        pytest.skip("needs node and a Chromium browser (Chrome or Edge)")
    major = subprocess.run([node, "--version"], capture_output=True, text=True).stdout
    if int((major.strip().lstrip("v").split(".") or ["0"])[0] or 0) < 22:
        pytest.skip("needs node 22 or newer (a global WebSocket)")
    run = tmp_path / ("run-" + "-".join(sorted(cfg)))
    run.mkdir()
    (run / "driver.cjs").write_text(DRIVER, encoding="utf-8")
    (run / "helpers.js").write_text(HELPERS + (LIVE if cfg.get("live") or cfg.get("stem") else ""),
                                    encoding="utf-8")
    (run / "cfg.json").write_text(json.dumps({
        "browser": BROWSER, "profile": str(run / "profile"), "page": str(page or ""),
        "helpers": str(run / "helpers.js"), **cfg}), encoding="utf-8")
    r = subprocess.run([node, str(run / "driver.cjs"), str(run / "cfg.json")], capture_output=True,
                       text=True, encoding="utf-8", timeout=600, env=BROWSER_ENV)
    assert r.returncode == 0, r.stderr or r.stdout[-2000:]
    (run / "out.json").write_text(r.stdout, encoding="utf-8")  # kept for a failing run
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert "fatal" not in out, out["fatal"]
    return out


@pytest.fixture
def demo_page(tmp_path, monkeypatch) -> Path:
    """The dashboard of a throwaway demo vault (scripts/make_demo_vault.py: the packaged sample
    plus made-up study history), so every board, list and brain view has rows."""
    spec = importlib.util.spec_from_file_location("make_demo_vault",
                                                  ROOT / "scripts" / "make_demo_vault.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    root = tmp_path / "vault"
    _init(root)
    monkeypatch.setattr(taint, "config", lambda: {})
    demo.main([str(root)])
    page = tmp_path / "snapshot.html"
    page.write_text(dashboard.snapshot(Vault(root), now=datetime.now(UTC)), encoding="utf-8")
    return page


FAILED = "Copy failed. Select the text and copy it."
COPIED = "Copied. Paste it into Claude."


def test_every_control_does_something_visible(tmp_path, demo_page):
    """Only the current choice (a pressed, selected or current control) may leave the page as
    it was; anything else that changes nothing is a dead control."""
    out = _run(tmp_path, demo_page, sweep=True)
    assert out["errors"] == []
    S = out["scopes"]
    assert set(S) >= {"header", "home", "brain 3d", "brain 2d", "brain list", "library", "reader",
                      "inspector", "look", "palette"}
    for scope in ("header", "home", "brain 3d", "brain 2d", "library", "you", "study", "help",
                  "reader", "inspector", "look", "palette"):
        assert S[scope], scope  # each scope has controls to click
    dead, copies = [], 0
    for scope, cs in S.items():
        for k, r in cs.items():
            where = (scope, k)
            assert not r.get("gone"), where
            assert r["errors"] == [], where
            if not (r["changed"] or r["current"]):
                dead.append(where)
            if r["copied"]:  # copied: a check (and Copied on a labelled button); a failure says so
                copies += 1
                if scope == "palette":  # the palette closes; the toast says Copied
                    assert r["toast"] == COPIED, where
                else:
                    assert r["check"], where
                    assert r["face"] == "Copied" or r["toast"] == COPIED, where
                bad = r["bad"]
                assert bad["copied"] == [] and bad["toast"] == FAILED, where
                assert bad["toastCls"] == "toast error" and bad["toastRole"] == "alert", where
                assert not bad["check"], where
    assert dead == []
    assert copies >= 10


def test_tab_reaches_every_control_with_a_focus_ring(tmp_path, demo_page):
    out = _run(tmp_path, demo_page, walk=True)
    assert out["errors"] == []
    W = out["walk"]
    for s, seen in W.items():
        assert len(seen) > 3, s
        no_ring = [f["key"] for f in seen if not (f["fv"] and f["ring"])]
        assert no_ring == [], s
    keys = {s: {f["key"] for f in seen} for s, seen in W.items()}
    assert {"Search (Ctrl+K)", "Switch to the day theme"} & keys["home"]
    assert "Find in the brain" in keys["brain"] and "Search the library" in keys["library"]


def test_tabs_keys_copy_faces_and_motion(tmp_path, demo_page):
    out = _run(tmp_path, demo_page, checks=True)
    assert out["errors"] == []
    c = out["checks"]
    # a tablist: one tab in the Tab order, the selected one; arrows, Home and End move and show
    assert [t[0] for t in c["tabs"]] == ["tab"] * 8
    assert [t[1:] for t in c["tabs"]] == [["true", 0]] + [["false", -1]] * 7
    assert c["keys"] == [["tab-brain", "true", False], ["tab-help", "true", False],
                         ["tab-home", "true", False], ["tab-help", "true", False]]
    assert c["select"] == ["true", False]  # the phone's section menu
    assert c["icon"] == [True, "", "Copied. Paste it into Claude."]
    assert c["iconAfter"] == [False, True]
    assert c["text"] == ["Copied", True, "Copied"] and c["again"] == "Copied"
    assert c["textAfter"] == "Copy mock prompt"
    assert c["fail"] == ["Copy failed. Select the text and copy it.", "toast error", "alert",
                         "Copy mock prompt"]
    assert c["motion"][0].startswith("0.12s") and c["motion"][1] == "fade-in"
    assert c["motion"][2] == "false"  # the brain never spins until asked


def test_reduced_motion_stops_transitions_and_spin(tmp_path, demo_page):
    out = _run(tmp_path, demo_page, checks=True, reduced=True)
    assert out["errors"] == []
    assert out["checks"]["motion"] == ["0s", "none", "false"]


def test_an_empty_vault_says_how_to_fill_each_list(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    (root / "catalog.yaml").write_text("schema: 1\ndomains: []\n", encoding="utf-8")
    page = tmp_path / "snapshot.html"
    page.write_text(dashboard.snapshot(Vault(root), now=NOW), encoding="utf-8")
    out = _run(tmp_path, page, sweep=True, texts=True)
    assert out["errors"] == []
    for scope, cs in out["scopes"].items():
        for k, r in cs.items():
            assert not r.get("gone") and r["errors"] == [], (scope, k)
    T = out["texts"]
    for s, lines in T.items():
        for line in lines:  # one sentence that says what fills it, never a bare "None"
            assert len(line.split()) >= 5 and line.endswith((".", "…")), (s, line)
    for s in ("home", "brain", "brain 2d", "brain list", "library", "you", "timeline", "ideas",
              "study"):
        assert T[s], s
    assert any("Import your chats or notes" in x for x in T["library"])
    assert any("No topics yet" in x for x in T["brain list"])


def test_the_live_home_applies_a_new_note_in_place(tmp_path, made, home, monkeypatch):  # noqa: F811
    """`/` on the app server: a note added while the page is open reaches the feed within a few
    seconds and pops into the brain, which is remounted where the camera was with the same lobe
    focused; 30 more mounts leave the page's WebGL context alive."""
    from test_app import _serve

    from cortex import localllm
    from cortex.app import server
    from cortex.events import local_day

    def no_runner(*a, **k):
        raise OSError("no runner in tests")

    monkeypatch.setattr(localllm, "_request", no_runner)  # never this computer's LM Studio
    monkeypatch.setattr(localllm, "_lms_exe", lambda cfg: None)
    monkeypatch.setattr(server, "_open", lambda url: None)
    root = made[0]
    (root / ".cortex").mkdir(exist_ok=True)
    srv, app = _serve(root)
    ready, title = tmp_path / "ready", "Live test note"

    def add_note():
        for _ in range(3000):
            if ready.exists():
                break
            time.sleep(0.1)
        else:
            return
        (root / "notes" / "live-test-note.md").write_bytes(
            f'---\nkind: note\ntitle: "{title}"\ndate: "{local_day(datetime.now(UTC))}"\n'
            f'related: ["[[hub-0]]"]\n---\nSee [[hub-0]].\n'.encode())

    adder = threading.Thread(target=add_note, daemon=True)
    adder.start()
    try:
        out = _run(tmp_path, None, url=f"http://127.0.0.1:{app.port}/?t={app.token}",
                   ready=str(ready), live=title)
    finally:
        srv.shutdown()
        srv.server_close()
    assert out["errors"] == []
    before, after, leak = out["live"]["before"], out["live"]["after"], out["live"]["leak"]
    assert before["moved"] and title not in before["feed"]
    assert after["feed"] and after["mounts"] >= 1 and after["waited"] < 10000, after
    assert all(abs(after["cam"][k] - v) < 1e-6 for k, v in before["cam"].items()), after["cam"]
    assert before["focused"] and after["focused"] == before["focused"], (before, after)
    assert any(title in f for f in after["fresh"])
    assert after["stale"] and after["llm"] and after["settings"]
    assert leak["connected"] and leak["lost"] is not True, leak


def test_the_brain_stem_shows_websites_and_a_site_panel_as_text(tmp_path, made, home,  # noqa: F811
                                                                 monkeypatch):
    """The live home's brain with a synthetic browsing store: websites in the stem, a chosen
    site's panel from /api/home/site with an HTML-looking title and search shown as text (no
    element made from them), and the private site absent while locked. Then the Explore view: the
    Legend opens on the first visit (folded at 1024x768) and remembers a click, the Stem button
    flies there and back, and the outline's Browsing group opens a site's panel from the
    keyboard. Screenshots of the stem are kept in the run folder."""
    from test_app import _serve
    from test_home import _ago, _sites, _stem_sites

    from cortex import localllm
    from cortex.app import server

    def no_runner(*a, **k):
        raise OSError("no runner in tests")

    monkeypatch.setattr(localllm, "_request", no_runner)
    monkeypatch.setattr(localllm, "_lms_exe", lambda cfg: None)
    monkeypatch.setattr(server, "_open", lambda url: None)
    root = made[0]
    (root / ".cortex").mkdir(exist_ok=True)
    evil = '<img src=x onerror="window.__pwned=1">'
    _sites(root, *_stem_sites(40),
           {"site": "odd.example", "visits": 80, "days": _ago(30, 2), "category": "learning",
            "titles": [f"Title {evil}"], "searches": [[f"term {evil}", 3]],
            "hubs": [["hub-0", 5]], "related": ["s00.example"]},
           {"site": "secret.example", "hidden": 1, "category": "adult", "visits": 99,
            "days": _ago(1)})
    srv, app = _serve(root)
    run = tmp_path / "shots"
    run.mkdir()
    shots = [str(run / "stem-overview.png"), str(run / "stem-site.png")]
    try:
        out = _run(tmp_path, None, url=f"http://127.0.0.1:{app.port}/?t={app.token}",
                   stem={"pick": "site:odd.example", "absent": ["site:secret.example"],
                         "sites": ["odd.example", "secret.example"], "shots": shots})
    finally:
        srv.shutdown()
        srv.server_close()
    assert out["errors"] == []
    s = out["stem"]
    assert s["label"].endswith("with 41 websites in its stem"), s["label"]
    assert s["absent"] == [None]  # locked: the private site is not in the brain at all
    assert s["node"] == {"kind": "site", "title": "odd.example", "private": False}
    rd = s["reader"]
    assert f"Title {evil}" in rd["text"] and f"term {evil}" in rd["text"]
    assert "img" not in rd["tags"] and "script" not in rd["tags"]
    assert {"Searched here", "Pages read", "Topics", "Related websites"} <= set(rd["heads"])
    assert any(h.startswith("Visit days (2)") for h in rd["heads"])
    assert "41 websites" in s["legend"] and "secret" not in s["legend"]
    assert s["codes"] == [200, 404]
    lg = s["legendUi"]
    assert lg["first"] == ["true", False, "closed"] and lg["dots"] >= 1, lg
    assert lg["closed"] == ["false", True, "closed"], lg
    assert lg["reopened"] == ["true", False, "open"], lg
    assert s["smallLegend"] == ["false", True, "closed"], s["smallLegend"]
    assert s["stemBtn"] == [True, "true", True, True, "false", False], s["stemBtn"]
    o = s["outline"]
    assert o["region"] and o["closed"] == "false" and o["opened"] == "true", o
    assert o["sites"] == 20 and o["id"].startswith("site:") and "secret" not in o["id"], o
    assert o["panel"] and o["selected"] == "true", o
    assert all(Path(p).stat().st_size > 10_000 for p in shots)
