/* The Cortex app page (docs/design/cortex-app.md). Every string from the server goes in with
   textContent; nothing here parses HTML. The key arrives once in the URL, then the URL is
   cleaned and the key travels in the X-Cortex-Token header. */
(() => {
  'use strict';
  // the key arrives once in the URL; this tab keeps it so a reload still works
  let T = new URLSearchParams(location.search).get('t') || '';
  try {
    if (T) sessionStorage.setItem('cortex.key', T); else T = sessionStorage.getItem('cortex.key') || '';
  } catch (e) { /* storage blocked: reopen the printed link after a reload */ }
  history.replaceState(null, '', '/' + location.hash);
  const $ = (id) => document.getElementById(id);
  const SVGNS = 'http://www.w3.org/2000/svg';
  const SECTIONS = ['setup', 'ask', 'status', 'connect', 'llm', 'browse', 'vault'];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  /* ---- DOM helpers ---- */
  function h(tag, props, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = String(v);
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
      else if (k === 'value' || k === 'checked' || k === 'disabled') e[k] = v;
      else e.setAttribute(k, v === true ? '' : String(v));
    }
    for (const c of kids.flat()) if (c != null && c !== false) e.append(c instanceof Node ? c : String(c));
    return e;
  }
  function svgEl(tag, attrs) {
    const e = document.createElementNS(SVGNS, tag);
    for (const k in attrs || {}) e.setAttribute(k, attrs[k]);
    return e;
  }
  const btn = (text, onclick, cls, extra) => h('button', { class: cls || 'btn', type: 'button', text, onclick, ...extra });
  const ext = (href, text) => h('a', { href, text, target: '_blank', rel: 'noopener noreferrer' });

  /* ---- dot matrix: the dashboard's authored 5x7 font, lit dots over the unlit cell ---- */
  const FONT = {
    0: [14, 17, 19, 21, 25, 17, 14], 1: [4, 12, 4, 4, 4, 4, 14], 2: [14, 17, 1, 2, 4, 8, 31],
    3: [31, 2, 4, 2, 1, 17, 14], 4: [2, 6, 10, 18, 31, 2, 2], 5: [31, 16, 30, 1, 1, 17, 14],
    6: [6, 8, 16, 30, 17, 17, 14], 7: [31, 1, 2, 4, 8, 8, 8], 8: [14, 17, 17, 14, 17, 17, 14],
    9: [14, 17, 17, 15, 1, 2, 12], A: [14, 17, 17, 17, 31, 17, 17], B: [30, 17, 17, 30, 17, 17, 30],
    C: [14, 17, 16, 16, 16, 17, 14], D: [28, 18, 17, 17, 17, 18, 28], E: [31, 16, 16, 30, 16, 16, 31],
    F: [31, 16, 16, 30, 16, 16, 16], G: [14, 17, 16, 23, 17, 17, 15], H: [17, 17, 17, 31, 17, 17, 17],
    I: [14, 4, 4, 4, 4, 4, 14], J: [7, 2, 2, 2, 2, 18, 12], K: [17, 18, 20, 24, 20, 18, 17],
    L: [16, 16, 16, 16, 16, 16, 31], M: [17, 27, 21, 21, 17, 17, 17], N: [17, 17, 25, 21, 19, 17, 17],
    O: [14, 17, 17, 17, 17, 17, 14], P: [30, 17, 17, 30, 16, 16, 16], Q: [14, 17, 17, 17, 21, 18, 13],
    R: [30, 17, 17, 30, 20, 18, 17], S: [15, 16, 16, 14, 1, 1, 30], T: [31, 4, 4, 4, 4, 4, 4],
    U: [17, 17, 17, 17, 17, 17, 14], V: [17, 17, 17, 17, 17, 10, 4], W: [17, 17, 17, 21, 21, 21, 10],
    X: [17, 17, 10, 4, 10, 17, 17], Y: [17, 17, 17, 10, 4, 4, 4], Z: [31, 1, 2, 4, 8, 16, 31],
    ':': [0, 12, 12, 0, 12, 12, 0], '.': [0, 0, 0, 0, 0, 12, 12], '-': [0, 0, 0, 31, 0, 0, 0],
    '/': [0, 1, 2, 4, 8, 16, 0], '+': [0, 4, 4, 31, 4, 4, 0], '%': [24, 25, 2, 4, 8, 19, 3],
    ' ': [0, 0, 0, 0, 0, 0, 0],
  };
  function dm(text, size, label) {
    const t = String(text).toUpperCase();
    const pitch = size + 1, r = size / 2, w = Math.max(1, t.length * 6 - 1) * pitch, ht = 7 * pitch;
    let on = '', off = '';
    [...t].forEach((ch, ci) => {
      const rows = FONT[ch] || FONT[' '];
      for (let y = 0; y < 7; y++) {
        for (let x = 0; x < 5; x++) {
          const cx = (ci * 6 + x) * pitch + pitch / 2, cy = y * pitch + pitch / 2;
          const d = `M${cx - r} ${cy}a${r} ${r} 0 1 0 ${size} 0a${r} ${r} 0 1 0 ${-size} 0`;
          if (rows[y] & (16 >> x)) on += d; else off += d;
        }
      }
    });
    const s = svgEl('svg', { class: 'dm', viewBox: `0 0 ${w} ${ht}`, width: w, height: ht, role: 'img', 'aria-label': label || t });
    s.append(svgEl('path', { class: 'off', d: off }), svgEl('path', { class: 'on', d: on }));
    return s;
  }
  const ICONS = {
    sun: ['M12 8a4 4 0 1 0 0.01 0', 'M12 2v2', 'M12 20v2', 'M2 12h2', 'M20 12h2', 'M4.9 4.9l1.4 1.4',
      'M17.7 17.7l1.4 1.4', 'M4.9 19.1l1.4-1.4', 'M17.7 6.3l1.4-1.4'],
    moon: ['M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z'],
    copy: ['M9 9h10v10H9z', 'M5 15V5h10'],
    open: ['M14 4h6v6', 'M20 4l-9 9', 'M18 14v5H5V6h5'],
  };
  function icon(name) {
    const s = svgEl('svg', { class: 'ico', viewBox: '0 0 24 24', 'aria-hidden': 'true' });
    for (const d of ICONS[name]) s.append(svgEl('path', { d }));
    return s;
  }

  /* ---- talking to the app ---- */
  let dead = false;
  function gone(text) {
    dead = true;
    const g = $('gone');
    if (text) g.textContent = text;
    g.hidden = false;
  }
  async function api(path, body) {
    let r;
    try {
      r = await fetch(path, body === undefined ? { headers: { 'X-Cortex-Token': T } } : {
        method: 'POST', headers: { 'X-Cortex-Token': T, 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch (e) {
      gone();
      throw new Error('The app is not answering.');
    }
    let data = {};
    try { data = await r.json(); } catch (e) { /* no body */ }
    if (r.status === 401) gone('This page lost its key. Open the link that cortex app printed, or run it again.');
    if (!r.ok) throw new Error(data.error || `The app answered ${r.status}.`);
    return data;
  }
  let toastTimer;
  function toast(msg, long) {
    const t = $('toast');
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, long ? 6000 : 2200);
  }
  async function post(path, body, done) {
    try {
      const r = await api(path, body || {});
      if (done) done(r);
      return r;
    } catch (e) {
      toast(e.message, true);
      return null;
    }
  }

  /* ---- boards and rows ---- */
  function board(title, opts, ...kids) {
    const o = opts || {};
    const band = h('div', { class: 'band' }, h('h2', { text: title }),
      o.note != null ? h('span', { class: 'band-note' }, o.note) : null,
      o.btn ? btn(o.btn[0], o.btn[1], 'band-btn') : null);
    return h('div', { class: 'board' + (o.cls ? ' ' + o.cls : ''), id: o.id }, band, ...kids);
  }
  const STATE = { ok: ['●', 'Ready'], warn: ['◐', 'Check'], off: ['○', 'Off'], bad: ['↻', 'Failed'] };
  function stateCell(s, word) {
    return h('span', { class: 'state' }, h('span', { class: 'glyph g-' + s, 'aria-hidden': 'true', text: STATE[s][0] }),
      ' ' + (word || STATE[s][1]));
  }
  function row(title, value, sub, acts, cls) {
    return h('div', { class: 'brow' + (cls ? ' ' + cls : '') },
      h('div', { class: 't' },
        h('div', { class: 'line1' }, h('span', { class: 'ttl', text: title }),
          value != null && value !== '' ? h('span', { class: 'val', text: value, title: String(value) }) : null),
        sub ? h('span', { class: 'sub', text: sub }) : null),
      acts && acts.length ? h('div', { class: 'acts' }, ...acts) : null);
  }
  function note(text) { return h('p', { class: 'pad ink-2', text }); }
  function fail(s, msg) {
    $(s).replaceChildren(board('Not available', {}, note(msg)));
  }

  /* ---- jobs: one at a time, a progress line under the header ---- */
  const outputs = {};
  let polling = false, hideTimer;
  async function job(path, body) {
    const r = await post(path, body);
    if (r) poll();
    return r;
  }
  function showJob(j) {
    clearTimeout(hideTimer);
    const r = j.result || {};
    $('jobline').classList.remove('idle');
    $('job-name').textContent = j.running ? j.name : `${j.name}: ${j.error ? 'failed' : r.cancelled ? 'cancelled' : r.need === 'embed' ? 'stopped: needs the search model' : r.error === 'no_answer' ? 'no answer' : r.error ? 'stopped: needs setup' : 'done'}`;
    $('job-line').textContent = j.running ? j.line : (j.error || '');
    $('job-time').textContent = `${Math.round(j.seconds)} s`;
    $('job-cancel').hidden = !(j.running && j.cancellable);
  }
  async function poll(initial) {
    if (polling) return;
    polling = true;
    try {
      for (;;) {
        const v = await api('/api/job');
        const j = v.job;
        if (!j || (initial && !j.running)) break;  // on open: only a job still running
        initial = false;
        showJob(j);
        if (!j.running) { finish(j); break; }
        await sleep(600);
      }
    } catch (e) { /* the app stopped: gone() said so */ } finally { polling = false; }
  }
  function idle() {
    $('jobline').classList.add('idle');
    $('job-name').textContent = 'Nothing running';
    $('job-line').textContent = 'Long actions show their progress here, one at a time.';
    $('job-time').textContent = '';
    $('job-cancel').hidden = true;
  }
  function finish(j) {
    if (guide && guide.wait && j.section === 'setup') {  // a guided import's own job: shown in its panel
      const w = guide.wait;
      guide.wait = null;
      if (w === 'preview' && j.error) Object.assign(guide, { error: j.error, upload: null });
      else if (w === 'preview') guide.preview = j.result;
      else Object.assign(guide, { done: j, upload: j.result && j.result.need ? guide.upload : null });
    } else if (j.section === 'ask') asked = j;  // shown in the Ask board itself
    else if (j.section === 'update') {  // shown in the Updates board
      upd = j;
      if (j.result && j.result.launched) gone('Cortex is closing to install the update. It reopens by itself when it is done; you can close this tab.');
    }
    else outputs[j.section] = { title: j.name, nodes: resultNodes(j), error: !!j.error };
    hideTimer = setTimeout(idle, 8000);
    if (current) load(current);
  }
  const OPS = { add: 'Add', update: 'Update', remove: 'Remove', ran: 'Ran', error: 'Error', none: '' };
  function changeList(rows) {
    return h('div', { class: 'pad' }, rows.map((r) => h('div', { class: 'chg' },
      h('span', { class: 'op' + (r.change === 'error' ? ' g-bad' : ''), text: OPS[r.change] || r.change }),
      h('strong', { text: r.label || '' }),
      r.file ? h('span', { class: 'f mono', text: r.file }) : null,
      r.detail ? h('span', { class: 'd', text: r.detail }) : null)));
  }
  function countsTable(obj) {
    const keys = Object.keys(obj).sort();
    return h('div', { class: 'table-scroll' }, h('table', {},
      h('thead', {}, h('tr', {}, h('th', { text: 'Count' }), h('th', { class: 'n', text: 'Value' }))),
      h('tbody', {}, keys.map((k) => h('tr', {}, h('td', { text: k.replace(/_/g, ' ') }), h('td', { class: 'n', text: String(obj[k]) }))))));
  }
  function scanTable(rows) {
    const cols = ['visits', 'searches', 'kept', 'private', 'dropped', 'maybe'];
    return h('div', { class: 'table-scroll' }, h('table', {},
      h('thead', {}, h('tr', {}, h('th', { text: 'Browser' }), h('th', { text: 'From' }), h('th', { text: 'To' }),
        cols.map((c) => h('th', { class: 'n', text: c })))),
      h('tbody', {}, rows.map((r) => h('tr', {}, h('td', { text: r.source }),
        r.error ? h('td', { colspan: 2 + cols.length, text: 'not read' })
          : [h('td', { text: r.first || '-' }), h('td', { text: r.last || '-' }),
            cols.map((c) => h('td', { class: 'n', text: String(r[c] ?? 0) }))])))));
  }
  function resultNodes(j) {
    const r = j.result || {};
    const out = [];
    if (j.error) out.push(row('Failed', null, j.error, null));
    if (r.cancelled) out.push(row('Cancelled', null, 'Stopped before it finished; run it again any time.', null));
    if (j.section === 'connect' && r.rows) out.push(r.rows.length ? changeList(r.rows) : note('Nothing changed.'));
    if (r.kind === 'scan') out.push(r.rows.length ? scanTable(r.rows) : note('No browser history found on this computer.'));
    if (r.counts) out.push(countsTable(r.counts));
    if (r.answer) out.push(row('Answer', `${r.answer} in ${r.seconds} s`, r.runner, null));
    if (r.need === 'embed') out.push(row('Search model needed', null, r.lines[0], [btn('Download the search model', () => job('/api/setup/embed', {}), 'btn lit')]));
    else if (r.lines && r.lines.length) out.push(h('pre', { class: 'out', text: r.lines.join('\n') }));
    return out;
  }
  function outputBoard(s) {
    const o = outputs[s];
    return o ? board(`Last action: ${o.title}`, { note: o.error ? 'failed' : 'finished' }, ...o.nodes) : null;
  }

  /* ---- sections ---- */
  let current = null;
  function show(s, focus) {
    if (!SECTIONS.includes(s)) s = 'status';
    if (current !== s) pairToken = null;  // the key is shown once
    current = s;
    for (const x of SECTIONS) $('s-' + x).hidden = x !== s;
    document.querySelectorAll('.side button').forEach((b) => b.setAttribute('aria-current', b.dataset.s === s ? 'page' : 'false'));
    $('section-select').value = s;
    if (location.hash !== '#' + s) history[focus ? 'pushState' : 'replaceState'](null, '', '#' + s);  // a click is a Back step
    load(s);
    if (focus) {  // the heading lands below the sticky header (scroll-margin-top in app.css)
      $('s-' + s).focus({ preventScroll: true });
      $('s-' + s).scrollIntoView({ block: 'start' });
    }
  }
  async function load(s) {
    if (!$(s).firstChild) $(s).append(note('Checking…'));
    try {
      const d = await api('/api/' + s);
      if (current === s) RENDER[s](d);
    } catch (e) {
      if (!dead) fail(s, e.message);
    }
  }
  const put = (s, ...nodes) => $(s).replaceChildren(...nodes.filter(Boolean), outputBoard(s) || '');

  function renderStatus(d) {
    const rows = d.rows.map((r) => h('div', { class: 'brow srow' }, stateCell(r.state),
      h('div', { class: 't' },
        h('div', { class: 'line1' }, h('span', { class: 'ttl', text: r.label }), h('span', { class: 'val', text: r.value, title: r.value })),
        h('span', { class: 'sub', text: r.detail })),
      h('div', { class: 'acts' }, r.count != null ? dm(String(r.count), 2, String(r.count)) : null,
        r.action ? btn(r.action.label, () => show(r.action.go, true)) : null)));
    const t = new Date(), hm = `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`;
    put('status', board('This computer', { note: ['checked ', dm(hm, 2, hm)], btn: ['Check again', () => load('status')] }, ...rows),
      d.update ? updatesBoard(d.update) : null);
  }

  /* Updates: GitHub is asked only on a click, never in the background */
  let upd = null;  // the last check or update job
  function updatesBoard(u) {
    const r = upd && upd.result;
    const out = [row(`Cortex ${u.current}`, null, null, [btn('Check for updates', () => { upd = null; job('/api/update/check', {}); })])];
    if (upd && upd.error) out.push(row('That did not work', null, upd.error, null));
    else if (r && r.cancelled) out.push(row('Cancelled', null, 'Nothing was installed; update again any time.', null));
    else if (r && !r.newer) out.push(row('You have the latest version', r.latest, null, null));
    else if (r) {
      const mb = Math.max(1, Math.round(r.size / 2 ** 20));
      const now = () => confirmDialog('Update Cortex?', 'Update now',
        [h('p', { class: 'ink-2', text: `Cortex will download the update (about ${mb} MB), close, and reopen when it's done. AI apps connected to Cortex may need a restart.` })],
        () => job('/api/update/apply', {}));
      out.push(row(`Cortex ${r.latest} is available`, null, r.command ? 'Run this in a terminal to update:' : null,
        [ext(r.notes_url, 'What’s new'), r.mode === 'installer' ? btn('Update now', now, 'btn lit') : null]));
      if (r.command) {
        out.push(h('div', { class: 'pad' }, h('div', { class: 'token' }, h('code', { text: r.command }),
          btn('Copy', () => navigator.clipboard.writeText(r.command).then(() => toast('Copied.'), () => toast('Copy failed; select the command and copy it.'))))));
      }
    }
    return board('Updates', { note: 'checked only when you click' }, ...out);
  }

  /* Ask your archive: the local model answers from your notes and browsing; never a cloud model */
  let question = '', asked = null;
  const MISSING = {  // [title, value, why, button]
    embed: ['Search model', 'not downloaded', 'Finds the notes and pages that answer your question. A one-time download.', 'Set up the search model'],
    llm: ['Local model', 'not running', 'Writes the answer on this computer, never in the cloud. Start Ollama or LM Studio, or set one up (a download of a few GB).', 'Set up a local model'],
  };
  function renderAsk(d) {
    if (!d.vault) {
      put('ask', board('Ask your archive', {}, row('No vault yet', null, 'Set one up first.', [btn('Open Setup', () => show('setup', true))])));
      return;
    }
    const q = h('input', { type: 'text', id: 'ask-q', value: question, spellcheck: 'true', placeholder: 'What did I read about binary search?' });
    q.addEventListener('input', () => { question = q.value; });
    const go = () => {
      if (!q.value.trim()) { toast('Type a question first.'); return; }
      asked = null;
      job('/api/ask', { question: q.value });
    };
    q.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
    const parts = [h('div', { class: 'field' }, h('label', { for: 'ask-q', text: 'Your question' }),
      h('div', { class: 'inline' }, q, btn('Ask', go, 'btn lit')),
      h('p', { class: 'help', text: 'Answered by the local model on this computer (Ollama or LM Studio) from your notes and browsing, private pages included. Nothing is sent to a cloud model. A small model can take a minute.' }))];
    const j = asked, r = j && j.result;
    if (j && j.error) parts.push(row('That did not work', null, j.error, null));
    else if (r && r.error === 'missing') {  // every missing piece at once, each with its own way to Setup
      parts.push(note('Ask needs two things on this computer. Still missing:'));
      for (const k of r.missing) {
        const [title, value, why, label] = MISSING[k];
        parts.push(row(title, value, why, [btn(label, () => { opened.add(k); show('setup', true); }, 'btn lit')]));
      }
    } else if (r && r.error) {
      parts.push(row(r.error === 'no_local_model' ? 'No local model is running' : 'Not ready yet', null, r.message,
        [btn('Open Setup', () => show('setup', true), 'btn lit')]));
    } else if (r) {
      parts.push(h('div', { class: 'pad' }, h('strong', { text: 'Answer' }), h('p', { class: 'answer', text: r.answer })));
      if (r.sources.length) {
        parts.push(h('div', { class: 'pad' }, h('strong', { text: 'Sources' }),
          h('ol', { class: 'how' }, r.sources.map((s) => h('li', { value: s.n },
            btn(s.path, () => post('/api/open', { what: 'page', path: s.path }), 'linkbtn mono', { title: 'Open in Obsidian' }))))));
      }
      parts.push(row('Answered by', r.model, `Took ${Math.round(j.seconds)} s.`));
    }
    put('ask', board('Ask your archive', { note: d.embed ? 'local model only' : 'search model not downloaded' }, ...parts));
  }

  /* Connect AI apps */
  function renderConnect(d) {
    const boxes = [];
    const here = (t) => (t.detected || t.connected ? 0 : 1);  // apps on this computer first
    const rows = [...d.targets].sort((a, b) => here(a) - here(b)).map((t) => {
      const cb = h('input', { type: 'checkbox', id: 'cb-' + t.id, checked: t.connected, disabled: !t.detected && !t.connected });
      cb.dataset.id = t.id;
      boxes.push(cb);
      // LM Studio only, while Local AI access (Browsing) is on: the --private opt-in
      const priv = t.private_offer ? h('input', { type: 'checkbox', id: 'pv-' + t.id, checked: t.private }) : null;
      if (priv) cb.priv = priv;
      const sub = !t.detected && !t.connected ? 'Not found on this computer.'
        : t.connected ? 'Cortex is connected.' : 'Found on this computer; not connected.';
      const what = h('details', { class: 'what' }, h('summary', { text: 'What this changes' }),
        h('p', { text: 'Files:' }), h('ul', { class: 'files' }, t.files.map((f) => h('li', { class: 'mono', text: f }))),
        t.snippet ? [h('p', { text: 'The entry Cortex adds:' }), h('pre', { text: t.snippet })]
          : h('p', { text: 'Set up a vault first; the entry points at it.' }),
        t.note ? h('p', { text: t.note }) : null);
      return h('div', { class: 'brow trow' + (t.detected || t.connected ? '' : ' off') }, cb,
        h('div', { class: 't' },
          h('div', { class: 'line1' }, h('label', { class: 'ttl', for: 'cb-' + t.id, text: t.label }),
            t.connected ? h('span', { class: 'code-tag', text: 'Connected' }) : null),
          h('span', { class: 'sub', text: sub + (t.error ? ` ${t.error}` : '') }),
          priv ? h('label', { class: 'check' }, priv, h('span', { text: 'Include private browsing search (this computer only)' })) : null,
          what));
    });
    const found = d.targets.filter((t) => t.detected).length, on = d.targets.filter((t) => t.connected).length;
    const apply = () => {
      const want = {};
      for (const b of boxes) if (!b.disabled) want[b.dataset.id] = b.checked && b.priv && b.priv.checked ? 'private' : b.checked;
      post('/api/connect/plan', { want }, (p) => {
        if (!p.rows.length) { toast('Nothing to change.'); return; }
        confirmChanges(p.rows, () => job('/api/connect/apply', { want }));
      });
    };
    const skill = board('Claude Desktop skill', {},
      row('cortex-skill.zip', d.skill_zip, 'Skills cannot be installed by file. In Claude Desktop, open Settings → Capabilities → Skills and upload this zip; then /cortex works in Chat.',
        d.skill_zip_found ? [btn('Open folder', () => post('/api/open', { what: 'skill_zip' }))] : null),
      d.skill_zip_found ? null : note('Not built yet. In the Cortex folder run: uv run python scripts/build_skills.py'));
    put('connect', board('AI apps', { note: `${found} found · ${on} connected`, btn: ['Apply…', apply] }, ...rows,
      h('div', { class: 'foot', text: d.vault ? 'Apply shows the exact files first. Backups are named <file>.cortex-<date>.bak, next to each file.' : 'Set up a vault first (Setup); each entry points at it.' })), skill);
  }
  function confirmDialog(title, okText, body, ok) {  // the page's one confirm dialog
    const dlg = $('confirm');
    $('confirm-title').textContent = title;
    $('confirm-ok').textContent = okText;
    $('confirm-body').replaceChildren(...body);
    $('confirm-ok').onclick = () => { dlg.close(); ok(); };
    $('confirm-no').onclick = () => dlg.close();
    dlg.showModal();
  }
  const confirmChanges = (rows, ok) => confirmDialog('Apply these changes?', 'Apply',
    [h('p', { class: 'ink-2', text: 'Cortex will change these files, backing each one up first:' }), changeList(rows)], ok);

  /* Local model */
  const gb = (b) => (b ? `${(b / 1e9).toFixed(1)} GB` : '');
  function renderLlm(d) {
    if (!d.available) { put('llm', board('Local model', {}, note('Not available in this version yet.'))); return; }
    const runner = board('Runner', {},
      row('Ollama', d.ollama === null ? 'not answering' : `${d.ollama.length} models`, 'Recommended. Cortex uses it whenever it answers on this computer; set it up under Setup → Local model.'),
      row('LM Studio', d.lms ? 'found' : 'not found', d.lms || 'The alternative runner, with its own model library.',
        d.lms ? null : [ext('https://lmstudio.ai/download', 'Get LM Studio')]),
      row('LM Studio server', d.server_port ? `on, port ${d.server_port}` : 'off', 'Starts by itself when a model loads.'),
      row('Loaded now', d.loaded.length ? d.loaded.join(', ') : 'nothing', 'LM Studio unloads a model after 10 idle minutes.'));
    const sel = h('select', { id: 'llm-model', 'aria-label': 'Model' },
      h('option', { value: '', text: d.models.length ? 'Choose a model' : 'No models in LM Studio' }),
      d.models.map((m) => h('option', { value: m.key, text: [m.name, m.params, gb(m.size)].filter(Boolean).join(' · ') })));
    sel.value = d.model && d.models.some((m) => m.key === d.model) ? d.model : '';
    const chosen = () => sel.value;
    const enabled = h('input', { type: 'checkbox', id: 'llm-on', checked: d.enabled });
    enabled.addEventListener('change', () => post('/api/llm/settings', { enabled: enabled.checked }, () => { toast(enabled.checked ? 'Local model on.' : 'Local model off.'); load('llm'); }));
    const model = board('Model', { note: d.model ? `chosen: ${d.model}` : 'none chosen' },
      d.error ? note(`Not used: ${d.error}`) : null,
      h('div', { class: 'field' }, h('label', { for: 'llm-model', text: 'Model (from LM Studio)' }),
        h('div', { class: 'inline' }, sel,
          btn('Save choice', () => post('/api/llm/settings', { model: chosen() || null }, () => { toast('Saved.'); load('llm'); })),
          btn('Load', () => (chosen() ? job('/api/llm/load', { model: chosen() }) : toast('Choose a model first.'))),
          btn('Test', () => job('/api/llm/test', {}))),
        h('p', { class: 'help' }, 'Load starts LM Studio and loads the model (nothing is downloaded). Test sends one made-up sentence and shows the answer and time. ',
          ext('https://lmstudio.ai/models', 'Get models'), ' in LM Studio; Qwen 3.5 2B is a good small choice.')),
      h('div', { class: 'field' }, h('label', { class: 'check' }, enabled, h('span', { text: 'Use the local model' })),
        h('p', { class: 'help', text: 'On: the browse import asks it for short summaries of your private pages. Off: Cortex uses rules and the search model only.' })));
    put('llm', runner, model);
  }

  /* Past history: one list for the Setup step and the Browsing section */
  const BROWSERS = { chrome: 'Chrome', edge: 'Edge', brave: 'Brave', comet: 'Comet', firefox: 'Firefox' };
  const untick = new Set();
  function historyRows(hist, from) {
    if (!hist || !hist.available) return [note('Not available in this version yet.')];
    const intro = h('p', { class: 'pad ink-2', text: 'Cortex can read the history your browsers already keep, on this computer only, and turn it into private day pages. The first time can take a while. If the local model is on, it writes the day summaries; when its time budget runs out, later days get simple rule-made lines.' });
    if (!hist.sources.length) return [intro, note('No browser history found on this computer (looked for Comet, Chrome, Edge, Brave and Firefox).')];
    const rows = hist.sources.map((s) => {
      const id = `h-${from}-${s.key.replace(/[^A-Za-z0-9]/g, '_')}`;
      const cb = h('input', { type: 'checkbox', id, checked: !untick.has(s.key) });
      cb.addEventListener('change', () => { if (cb.checked) untick.delete(s.key); else untick.add(s.key); });
      const counted = s.visits != null;
      const sub = s.error ? 'Could not be read; close that browser and scan again.'
        : !counted ? 'Not counted yet: Scan reads the counts only.'
          : s.first ? `From ${s.first} to ${s.last}. The privacy filter would leave out ${s.dropped} and keep ${s.private} private.` : 'No visits.';
      return h('div', { class: 'brow trow' }, cb, h('div', { class: 't' },
        h('div', { class: 'line1' }, h('label', { class: 'ttl', for: id, text: `${BROWSERS[s.browser] || s.browser} · ${s.profile}` }),
          counted ? h('span', { class: 'val', text: `${s.visits.toLocaleString()} visits` }) : null),
        h('span', { class: 'sub', text: sub })));
    });
    const go = () => {
      const keys = hist.sources.map((s) => s.key).filter((k) => !untick.has(k));
      if (!keys.length) { toast('Tick at least one browser.'); return; }
      job('/api/browse/import', { sources: keys, from });
    };
    return [intro, ...rows, h('div', { class: 'foot' }, h('div', { class: 'inline' },
      btn('Scan (counts only)', () => job('/api/browse/scan', { from })),
      btn('Import my past history', go, 'btn lit'),
      h('span', { class: 'sub', text: hist.imported ? `Last import ${hist.imported}.` : 'Never imported.' })))];
  }

  /* The extension: one click pairs, starts the receiver and opens its folder; the key is
     shown with Copy and never copied by itself. Start at login stays its own click. */
  function tokenBox() {
    return pairToken ? h('div', { class: 'pad' }, h('strong', { text: 'The key for the extension (shown once):' }),
      h('div', { class: 'token' }, h('code', { text: pairToken }),
        btn('Copy', () => navigator.clipboard.writeText(pairToken).then(() => toast('Copied.'), () => toast('Copy failed; select the key and copy it.'))))) : null;
  }
  function extensionBtn(paired) {
    return btn('Set up the extension', () => {
      if (paired && !window.confirm('Set it up again? The key the extension has now stops working.')) return;
      post('/api/browse/setup', {}, (r) => {
        pairToken = r.token;
        toast(r.opened ? 'Paired, receiver started, folder opened.' : 'Paired and receiver started.');
        load(current);
      });
    }, 'btn lit');
  }
  function extensionSetup(x) {
    return [row('Set up the extension', x.paired ? 'paired' : 'not set up', 'One click pairs it, starts the receiver and opens the extension folder. Start at login is under Browsing.', [extensionBtn(x.paired)]),
      tokenBox(),
      h('div', { class: 'pad' }, h('strong', { text: 'Then, in Chrome or Comet' }),
        h('ol', { class: 'how' },
          h('li', {}, 'Open ', h('code', { text: 'chrome://extensions' }), ' and turn on Developer mode.'),
          h('li', {}, 'Click Load unpacked and choose the folder that opened: ', h('code', { text: x.folder })),
          h('li', { text: 'Open the Cortex capture popup, paste the key and press Save and test.' })))];
  }

  /* Add your history: one card per source; each export source opens a three-step guide */
  const CARDS = ['claude', 'chatgpt', 'gemini', 'perplexity', 'google-activity', 'notes', 'calendar', 'browser', 'extension'];
  const CARD = {
    claude: 'Your claude.ai chats.', chatgpt: 'Your ChatGPT chats.', gemini: 'Your Gemini prompts, from Google Takeout.',
    perplexity: 'Perplexity threads, exported as Markdown.',
    'google-activity': 'Searches, YouTube and Chrome activity, from Google Takeout.',
    notes: 'An Obsidian vault, a Notion export or a folder of .md and .txt notes.',
    calendar: 'Your events, from an .ics calendar file.',
    browser: ['Browser history', 'The history your browsers keep on this computer.'],
    extension: ['Browser extension', 'Captures new browsing as you go.'],
  };
  const MAX_UPLOAD = 4 * 2 ** 30;
  let guide = null;  // {source, step, upload, path, busy, wait, preview, done, error}
  const guideSrc = () => ({ source: guide.source, ...(guide.upload ? { upload: guide.upload } : { path: guide.path }) });
  async function guided(path, wait) {
    Object.assign(guide, { error: null, wait });
    try { await api(path, guideSrc()); poll(); } catch (e) { Object.assign(guide, { wait: null, error: e.message }); }
    load('setup');
  }
  function check() {
    Object.assign(guide, { step: 3, preview: null, done: null });
    guided('/api/import/preview', 'preview');
  }
  const mb = (n) => (n / 2 ** 20).toFixed(1);
  const sentText = (s) => `${Math.floor(100 * s.loaded / (s.total || 1))}% · ${mb(s.loaded)} of ${mb(s.total)} MB`;
  // XHR, not fetch: only XHR reports upload progress. Updates the bar in place, not by re-rendering.
  function sendFile(f) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open('POST', '/api/import/upload');
      x.setRequestHeader('X-Cortex-Token', T);
      x.setRequestHeader('Content-Type', 'application/octet-stream');
      x.setRequestHeader('X-Cortex-Name', encodeURIComponent(f.name));
      x.upload.onprogress = (e) => {
        if (!e.lengthComputable || !guide.sent) return;
        guide.sent.loaded = e.loaded;
        const bar = $('up-bar'), txt = $('up-text');
        if (bar) bar.value = e.loaded;
        if (txt) txt.textContent = sentText(guide.sent);
      };
      x.onload = () => resolve(x);
      x.onerror = () => reject(new TypeError('Failed to fetch'));  // the message fetch gave
      x.send(f);
    });
  }
  async function upload(f) {
    if (f.size > MAX_UPLOAD) { guide.error = 'The file is larger than 4 GB; unzip it and paste the path of the file inside instead.'; load('setup'); return; }
    Object.assign(guide, { busy: `Adding ${f.name}…`, error: null, sent: { loaded: 0, total: f.size } });
    load('setup');
    try {
      const x = await sendFile(f);
      let data = {};
      try { data = JSON.parse(x.responseText); } catch (e) { /* no body */ }
      if (x.status < 200 || x.status > 299) throw new Error(data.error || `The app answered ${x.status}.`);
      Object.assign(guide, { upload: data.upload, path: null, busy: null, sent: null });
      check();
    } catch (e) { Object.assign(guide, { busy: null, sent: null, error: e.message }); load('setup'); }
  }
  // a file dropped beside the drop zone must not open in this tab
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => e.preventDefault());

  function guidePanel(d, id) {
    if (id === 'browser') return historyRows(d.history, 'setup');
    if (id === 'extension') return extensionSetup(d.extension);
    const g = d.guides[id], s = guide.step;
    const back = (to) => btn('Back', () => { Object.assign(guide, { step: to, error: null }); load('setup'); });
    const steps = h('div', { class: 'gsteps' }, ['Get your export', 'Add it', 'Check and import'].map((t, i) =>
      h('span', { class: i + 1 === s ? 'on' : i + 1 < s ? 'done' : null, 'aria-current': i + 1 === s ? 'step' : null, text: `${i + 1}. ${t}` })));
    const err = guide.error ? row('That did not work', null, guide.error, null, 'gerr') : null;
    if (s === 1) {
      return [steps, h('ol', { class: 'how' }, g.steps.map((t) => h('li', { text: t }))),
        h('p', { class: 'help' }, ext(g.link, `Open ${new URL(g.link).hostname}`), ` · Typical wait: ${g.wait}.`),
        h('p', { class: 'help', text: `The file to pick: ${g.pick}.` }),
        h('div', { class: 'inline' }, btn('I have the file', () => { guide.step = 2; load('setup'); }, 'btn lit'))];
    }
    if (s === 2) {
      const file = h('input', { type: 'file', id: 'imp-file', accept: '.zip,.json,.html,.md,.txt,.ics', disabled: !!guide.busy });
      file.addEventListener('change', () => { if (file.files[0]) upload(file.files[0]); });
      const drop = h('div', { class: 'drop' }, h('strong', { text: guide.busy || 'Drop the file here' }),
        guide.busy && guide.sent ? [h('progress', { id: 'up-bar', max: guide.sent.total || 1, value: guide.sent.loaded, 'aria-label': 'Upload progress' }),
          h('span', { id: 'up-text', class: 'sub', text: sentText(guide.sent) })] : null,
        h('label', { class: 'check', for: 'imp-file' }, h('span', { text: 'or choose it:' })), file);
      drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
      drop.addEventListener('dragleave', () => drop.classList.remove('over'));
      drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('over'); if (!guide.busy && e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); });
      const path = h('input', { type: 'text', id: 'imp-path', spellcheck: 'false', placeholder: 'C:\\Users\\you\\Downloads\\export.zip', value: guide.path || '' });
      const usePath = () => {
        if (!path.value.trim()) { toast('Paste the path first.'); return; }
        Object.assign(guide, { upload: null, path: path.value });
        check();
      };
      return [steps, err, drop,
        h('div', { class: 'field' }, h('label', { for: 'imp-path', text: 'Or paste its full path (a file or a folder)' }),
          h('div', { class: 'inline' }, path, btn('Check this file', usePath)),
          h('p', { class: 'help', text: 'Windows: hold Shift, right-click the file and choose Copy as path. macOS: select the file in Finder and press Option-Command-C.' })),
        h('div', { class: 'inline' }, back(1))];
    }
    const p = guide.preview, done = guide.done, again = done && done.result && done.result.need;
    const out = [steps, err];
    if (guide.error) out.push(h('div', { class: 'inline' }, back(2)));
    else if (!p) out.push(note('Checking the file…'));
    else {
      out.push(h('div', {}, p.lines.map((t) => h('p', { class: 'gfound', text: t })), p.note ? h('p', { class: 'help', text: p.note }) : null));
      if (p.need === 'embed') {  // said at Check, before any Import (R3-3)
        out.push(h('div', { class: 'inline' }, btn('Download the search model', () => job('/api/setup/embed', {}), 'btn lit'),
          btn('Check again', check), back(2)));
      } else if (!done || again) {
        out.push(h('div', { class: 'inline' }, btn('Import', () => guided('/api/import/run', 'import'), 'btn lit', { disabled: !!guide.wait || !p.found }),
          guide.wait ? h('span', { class: 'sub', text: 'Importing; the progress shows at the top.' }) : back(2)));
      }
    }
    if (done) {
      out.push(...resultNodes(done));
      if (!again) out.push(h('div', { class: 'inline' }, btn('Done', () => { guide = null; load('setup'); }, 'btn lit')));
    }
    return out;
  }
  function historyBoard(d) {
    const hi = d.history, out = [note('Bring in what you already have, one source at a time. Each guide shows how to get the file, then checks it before anything is imported.')];
    for (const id of CARDS) {
      const imp = d.imports[id], open = !!guide && guide.source === id;
      const [name, blurb] = d.guides[id] ? [d.guides[id].name, CARD[id]] : CARD[id];
      const stat = imp ? (imp.count ? `imported ${imp.count.toLocaleString()} on ${imp.when.slice(0, 10)}` : 'not yet')
        : id === 'browser' || id === 'google-activity' ? (hi.imported ? `browsing last imported ${hi.imported.slice(0, 10)}` : 'not yet')
          : id === 'extension' ? (d.extension.paired ? 'paired' : 'not set up') : '';
      out.push(row(name, stat, blurb, [btn(open ? 'Close' : 'Start', () => {
        guide = open ? null : { source: id, step: 1 };
        opened.add('history');  // stays open when an import marks the step done
        load('setup');
      }, 'btn', { 'aria-expanded': open ? 'true' : 'false', 'aria-label': `${open ? 'Close' : 'Start'}: ${name}` })]));
      if (open) out.push(h('div', { class: 'guide' }, ...guidePanel(d, id)));
    }
    return out;
  }

  /* Browsing */
  const CATEGORY_NAMES = { adult: 'Adult', health: 'Health', money: 'Money and banking', betting: 'Betting', dating: 'Dating', sexuality: 'Sexuality', drugs: 'Drugs', selfharm: 'Self-harm' };
  const AUDIENCE = { skip: 'Don\'t save', local: 'Local AI only', cloud: 'Cloud AI apps can read' };
  const PROVIDERS = { openai: 'OpenAI', openrouter: 'OpenRouter', gemini: 'Google Gemini', anthropic: 'Anthropic', custom: 'Custom (OpenAI-compatible)' };
  function cloudBoard(c) {
    const on = h('input', { type: 'checkbox', id: 'c-on', checked: c.cloud_processing });
    const prov = h('select', { id: 'c-provider' }, h('option', { value: '', text: 'Choose a provider' }),
      [...c.presets, 'custom'].map((p) => h('option', { value: p, text: PROVIDERS[p] || p })));
    prov.value = c.provider || '';
    const text = (id, value, placeholder) => h('input', { type: 'text', id, value: value || '', placeholder, spellcheck: 'false', autocomplete: 'off' });
    const model = text('c-model', c.model, 'e.g. gpt-4o-mini'), base = text('c-base', c.base_url, 'e.g. https://example.com/v1');
    const env = text('c-env', c.api_key_env, 'e.g. OPENAI_API_KEY');
    const baseField = h('div', { class: 'field', hidden: prov.value !== 'custom' }, h('label', { for: 'c-base', text: 'Base URL (https only)' }), base);
    prov.addEventListener('change', () => { baseField.hidden = prov.value !== 'custom'; });
    const save = () => post('/api/browse/cloud', {
      cloud_processing: on.checked, provider: prov.value, model: model.value.trim(),
      base_url: prov.value === 'custom' ? base.value.trim() : '', api_key_env: env.value.trim(),
    }, () => { toast('Saved.'); load('browse'); });
    const name = c.api_key_env || 'NAME';
    const runs = c.runs.length ? h('div', { class: 'table-scroll' }, h('table', {},
      h('thead', {}, h('tr', {}, ['When', 'Provider', 'Model'].map((t) => h('th', { text: t })),
        ['Requests', 'Characters', 'Skipped for a secret'].map((t) => h('th', { class: 'n', text: t })))),
      h('tbody', {}, c.runs.map((r) => h('tr', {}, h('td', { text: (r.ts || '').replace('T', ' ').replace('Z', ' UTC') }),
        h('td', { text: r.provider }), h('td', { text: r.model }),
        ['items', 'chars', 'skipped'].map((k) => h('td', { class: 'n', text: String(r[k] ?? 0) })))))))
      : note('No cloud runs yet.');
    return [board('Cloud model', { btn: ['Save', save], note: c.cloud_processing ? 'on' : 'off' },
      h('div', { class: 'field' }, h('label', { class: 'check' }, on, h('span', { text: 'Let a cloud model sort and summarise' })),
        h('p', { class: 'help', text: 'Off (the default). On: when Cortex processes browsing, pages of categories set to Cloud AI apps can read (with that switch on) go to this cloud model for short descriptions. Local AI only pages never leave this computer and never fall back to the cloud. A request that looks like it holds a key or token is not sent, and each run sends at most 500 requests unless cloudllm.max_items in the config says otherwise.' })),
      h('div', { class: 'field' }, h('label', { for: 'c-provider', text: 'Provider' }), prov),
      baseField,
      h('div', { class: 'field' }, h('label', { for: 'c-model', text: 'Model' }), model),
      h('div', { class: 'field' }, h('label', { for: 'c-env', text: 'Name of the environment variable that holds your API key' }), env,
        h('p', { class: 'help', text: c.api_key_env ? `${c.api_key_env} is ${c.key_set ? 'set' : 'not set'} for this app.` : 'No variable named yet.' }),
        h('p', { class: 'help', text: 'Cortex never asks for the key itself and never stores it. Set the variable yourself, then restart cortex app:' }),
        h('ol', { class: 'how' },
          h('li', {}, 'Windows: in PowerShell run ', h('code', { text: `setx ${name} "your-key"` }), ', then open a new terminal.'),
          h('li', {}, 'macOS: add ', h('code', { text: `export ${name}="your-key"` }), ' to ~/.zshrc, then open a new terminal.')))),
    board('Recent cloud runs', { note: 'counts only' }, runs)];
  }
  let pairToken = null;
  function renderBrowse(d) {
    if (d.available === false) { put('browse', board('Browsing', {}, note('Not available in this version yet.'))); return; }
    const s = d.settings || {};
    const parts = [];
    if (s.available !== false) {
      const vis = h('input', { type: 'checkbox', id: 'b-vis', checked: s.visible_to_claude });
      const local = h('input', { type: 'checkbox', id: 'b-local', checked: s.private_mcp });
      const cats = (s.categories || []).map((c) => {
        const sel = h('select', { id: 'b-aud-' + c, 'aria-label': `${CATEGORY_NAMES[c] || c}: who may read it` },
          Object.entries(AUDIENCE).map(([v, t]) => h('option', { value: v, text: t })));
        sel.value = (s.audience || {})[c] || 'local';
        return { c, sel };
      });
      const list = (id, label, help, items) => h('div', { class: 'field' }, h('label', { for: id, text: label }),
        h('textarea', { id, spellcheck: 'false', placeholder: 'example.com', value: (items || []).join('\n') }), h('p', { class: 'help', text: help }));
      const save = () => post('/api/browse/settings', {
        visible_to_claude: vis.checked, private_mcp: local.checked,
        audience: Object.fromEntries(cats.map((x) => [x.c, x.sel.value])),
        deny: $('b-deny').value, allow: $('b-allow').value, private: $('b-private').value,
      }, () => { toast('Saved.'); load('browse'); });
      parts.push(board('Privacy', { btn: ['Save', save] },
        h('div', { class: 'field' }, h('label', { class: 'check' }, vis, h('span', { text: 'Cloud AI apps can read' })),
          h('p', { class: 'help', text: 'Off (the default): browsing goes only to private pages that Obsidian and local models read; no cloud AI app (Claude, Gemini, ChatGPT, Cursor…) sees it. On: Cortex also writes pages that cloud AI apps read through Cortex: your topics and time, interest lines in your journal, and the sites you visit with their page titles, the searches that led to them and the searches made on them. That covers all your browsing except the categories below set to Local AI only (or Don\'t save), which never reach a cloud AI app.' })),
        h('div', { class: 'field' }, h('span', { class: 'lbl', text: 'Sensitive categories' }),
          h('p', { class: 'help', text: 'Don\'t save: dropped before anything is stored. Local AI only: kept on your private pages; only models on this computer read them. Cloud AI apps can read: treated like the rest of your browsing, so cloud AI apps read its sites, page titles and searches while the switch above is on.' })),
        ...cats.map((x) => row(CATEGORY_NAMES[x.c] || x.c, null, null, [x.sel])),
        h('div', { class: 'field' }, h('label', { class: 'check' }, local, h('span', { text: 'Local AI access (LM Studio only)' })),
          h('p', { class: 'help', text: 'Off (the default). On: Connect AI apps offers LM Studio a private search over all your browsing, private pages included. It runs only with a model on this computer; no other app is ever given it.' })),
        h('div', { class: 'field' }, h('span', { class: 'lbl', text: 'Domain lists' }),
          h('p', { class: 'help', text: 'One domain per line; a domain covers its subdomains.' })),
        h('div', { class: 'fields3' },
          list('b-deny', 'Never record', 'These sites are dropped before anything is stored.', s.deny),
          list('b-allow', 'Only record', 'When not empty, every other site is dropped.', s.allow),
          list('b-private', 'Private only', 'Kept, but only on your private pages.', s.private))));
    }
    const recv = board('Receiver', {},
      row('Receiver', d.receiver === 'running' ? (d.in_app ? 'running in this app' : 'running') : 'stopped',
        'Takes visits from the browser extension on 127.0.0.1:8799, this computer only. Started here, it stops when this app stops.',
        [d.in_app ? btn('Stop', () => post('/api/browse/receiver', { on: false }, () => load('browse')))
          : d.receiver === 'running' ? null : btn('Start', () => post('/api/browse/receiver', { on: true }, () => { toast('Receiver started.'); load('browse'); }))]),
      d.login.supported
        ? row('Start at login', d.login.exists ? 'on' : 'off', 'Adds or removes a login entry (Task Scheduler on Windows, a LaunchAgent on macOS, a user service on Linux), only when you click. It runs:',
          [btn(d.login.exists ? 'Remove' : 'Add', () => job('/api/browse/login', { on: !d.login.exists }))])
        : row('Start at login', 'not on this system', 'Windows only: start the receiver from your login items instead.'),
      d.login.command ? h('div', { class: 'pad' }, h('pre', { text: d.login.command })) : null);
    const pairBtn = btn(d.paired ? 'Pair again' : 'Pair', () => {
      if (d.paired && !window.confirm('Pair again? The key the extension has now stops working.')) return;
      post('/api/browse/pair', {}, (r) => { pairToken = r.token; load('browse'); });
    });
    const extn = board('Extension', {},
      row('Pairing', d.paired ? 'paired' : 'not paired', d.last_contact ? `Last contact ${d.last_contact}.` : 'No contact recorded yet.', [extensionBtn(d.paired), pairBtn]),
      tokenBox(),
      h('div', { class: 'pad' }, h('strong', { text: 'Load it in Comet, Chrome, Edge or Brave' }),
        h('ol', { class: 'how' },
          h('li', {}, 'Open the extensions page: ', h('code', { text: 'chrome://extensions' }), ' (Chrome, Comet), ', h('code', { text: 'edge://extensions' }), ' or ', h('code', { text: 'brave://extensions' }), '.'),
          h('li', { text: 'Turn on Developer mode.' }),
          h('li', {}, 'Click Load unpacked and choose this folder: ', h('code', { text: d.extension }), ' ',
            btn('Open folder', () => post('/api/open', { what: 'extension' }), 'linkbtn')),
          h('li', { text: 'Open the Cortex capture popup, enter port 8799, paste the key and press Save and test. Its top line then says Receiver connected, and its Process now button does the same as the one below.' }))));
    const past = board('Past history', { note: d.history && d.history.imported ? `last import ${d.history.imported}` : 'never imported' },
      ...historyRows(d.history, 'browse'));
    const acts = board('Actions', { note: `${d.queued} waiting · processed ${d.last_processed || 'never'}` },
      row('Process now', null, 'Turns waiting visits and new browser history into your private day pages.', [btn('Process now', () => job('/api/browse/process', {}))]),
      row('Self-test', null, 'Runs the whole import on made-up history in a temp folder; your vault and browsers are not touched.', [btn('Run self-test', () => job('/api/browse/selftest', {}))]),
      row('Private pages', null, 'Your browsing pages are for you only: open them in Obsidian.', [btn('Open private folder in Obsidian', () => post('/api/open', { what: 'private' }))]));
    put('browse', ...parts, ...(d.cloud ? cloudBoard(d.cloud) : []), past, recv, extn, acts);
  }

  /* Dashboard and vault */
  function renderVault(d) {
    if (!d.ok) {
      put('vault', board('Vault', {}, row('No vault yet', d.path || '', 'Set one up first.', [btn('Open Setup', () => show('setup', true))])));
      return;
    }
    put('vault', board('Vault', {},
      row('Folder', d.path, 'Your notes live here as Markdown files.', [btn('Open vault in Obsidian', () => post('/api/open', { what: 'vault' }))]),
      h('div', { class: 'brow' }, h('div', { class: 't' }, h('div', { class: 'line1' }, h('span', { class: 'ttl', text: 'Notes' })),
        h('span', { class: 'sub', text: `Last indexed ${d.last_index || 'not yet'}.` })), h('div', { class: 'acts' }, dm(String(d.notes), 3, `${d.notes} notes`))),
      row('Dashboard', d.dashboard ? `built ${d.dashboard}` : 'not built', 'An offline page of your notes and the 3D brain.',
        [btn('Build dashboard', () => job('/api/vault/dashboard', {})), d.dashboard ? btn('Open dashboard', () => post('/api/open', { what: 'dashboard' })) : null]),
      row('Backups', d.backups ? `${d.backups}, last ${d.last_backup}` : 'none yet', 'A zip of the whole vault in .cortex/backups. Copy it to another drive now and then.',
        [btn('Back up vault', () => job('/api/vault/backup', {}))]),
      row('Health', null, 'Checks Python, the vault folder, git and a few common problems.', [btn('Run doctor', () => job('/api/vault/doctor', {}))])));
  }

  /* Setup */
  const skipped = new Set(), opened = new Set();
  const pick = { model: null, runner: null };
  // the server keeps a skipped Connect (the app then lands on Status); opening it un-skips
  function skip(id, on) {
    const was = skipped.has(id);
    if (on) skipped.add(id); else skipped.delete(id);
    if (id === 'connect' && was !== on) post('/api/setup/skip', { skipped: on }, () => load('setup'));
    else load('setup');
  }
  function step(id, title, done, summary, body, first) {
    const cur = id === first;
    const collapsed = !cur && !opened.has(id);  // one step at a time: the current one, plus any the user opened
    const band = h('div', { class: 'band' }, h('h2', { text: title }),
      h('span', { class: 'band-note' }, done ? h('span', { class: 'done-mark', text: '✓ ' }) : null,
        done ? summary || 'Done' : skipped.has(id) ? 'Skipped' : cur ? dm('NEXT', 2, 'Next') : 'Later'),
      collapsed ? btn(done ? 'Edit' : 'Open', () => { opened.add(id); if (id === 'connect') skip(id, false); else load('setup'); }, 'band-btn',
        { 'aria-label': `${done ? 'Edit' : 'Open'}: ${title}` })
        : !done && id !== 'finish' ? btn('Skip', () => { opened.delete(id); skip(id, true); }, 'band-btn')
          : done ? btn('Hide', () => { opened.delete(id); load('setup'); }, 'band-btn') : null);
    return h('div', { class: 'board step' + (collapsed ? ' collapsed' : '') + (cur ? ' current' : '') }, band, ...body);
  }
  function renderSetup(d) {
    const cn = d.connect, on = cn.filter((t) => t.connected), todo = cn.filter((t) => t.detected && !t.connected);
    const names = (ts) => ts.map((t) => t.label).join(', ');
    const done = { vault: d.vault.done, embed: d.embed.done, connect: on.length > 0,
      history: d.history.done || Object.values(d.imports).some((x) => x.count), llm: d.test.done };
    if (d.connect_skipped) skipped.add('connect'); else skipped.delete('connect');
    const first = d.order.find((k) => !done[k] && !skipped.has(k)) || 'finish';
    const rn = d.runner, md = d.model;
    if (!pick.model) pick.model = md.recommend;
    if (!pick.runner) pick.runner = rn.ollama.found || rn.ollama.serving ? 'ollama' : rn.lmstudio.found ? 'lmstudio' : 'ollama';
    const m = md.options.find((o) => o.id === pick.model) || md.options[0];

    const path = h('input', { type: 'text', id: 'vault-path', value: d.vault.path || d.vault.default, 'aria-label': 'Vault folder', spellcheck: 'false' });
    const sVault = step('vault', 'Vault', d.vault.done, d.vault.path, [
      h('div', { class: 'field' }, h('label', { for: 'vault-path', text: 'The folder for your notes' }),
        h('div', { class: 'inline' }, path, btn('Use this folder', () => job('/api/setup/vault', { path: path.value }), 'btn lit')),
        h('p', { class: 'help', text: 'Cortex adds its own folders and a git history there, and never changes notes you already have. Obsidian can open it as a vault.' }))], first);

    const e = d.embed;
    const sEmbed = step('embed', 'Search model', e.done, 'downloaded', [
      row('Find notes by meaning', e.done ? 'downloaded' : 'not downloaded',
        'Without it, search matches words only. It downloads once, works offline, then indexes your notes.',
        [e.done ? null : btn(`Download search model (${e.size}, from ${e.source})`, () => job('/api/setup/embed', {}), 'btn lit')]),
      row('Index my notes', e.indexed ? `indexed ${e.indexed}` : 'not yet', 'Reads every note once so search by meaning can find it. Safe to stop and run again.',
        [btn('Index my notes', () => job('/api/setup/index', {}), 'btn', { disabled: !(e.done && d.vault.done) })])], first);

    const inst = rn.install;
    const RSTATE = { running: 'running', stopped: 'installed, not running', not_installed: 'not installed' };
    const start = (id) => btn('Start', () => job('/api/setup/start', { runner: id }), 'btn lit');
    const oll = row('Ollama', RSTATE[rn.ollama.state], 'Runs AI models on this computer. Free.',
      rn.ollama.state === 'stopped' ? [start('ollama')] : rn.ollama.found ? null
        : [inst.run ? btn(`Install Ollama (${inst.size}, via ${inst.source})`, () => job('/api/setup/install', {}), 'btn lit') : ext(inst.page, 'Get Ollama')]);
    oll.querySelector('.line1').append(h('span', { class: 'rec', text: 'RECOMMENDED' }));
    const runnerRows = [
      oll,
      !rn.ollama.found ? h('div', { class: 'pad' }, h('span', { class: 'sub', text: inst.run ? 'The button runs:' : 'Run this yourself:' }),
        h('pre', { text: inst.run ? inst.run.join(' ') : inst.show || inst.page })) : null,
      row('LM Studio', RSTATE[rn.lmstudio.state], 'An app with its own model library; Cortex uses its lms command.',
        rn.lmstudio.state === 'stopped' ? [start('lmstudio')] : rn.lmstudio.found ? null : [ext(rn.lmstudio.page, 'I’ll use LM Studio')]),
      h('p', { class: 'pad ink-3 small', text: 'Optional: skip this to run Cortex on rules and the search model only. A model is a download of several GB.' })];

    const runners = [['ollama', 'Ollama', rn.ollama.found || rn.ollama.serving], ['lmstudio', 'LM Studio', rn.lmstudio.found]];
    const opts = md.options.map((o) => {
      const r = h('input', { type: 'radio', name: 'model', value: o.id, checked: o.id === pick.model, id: 'm-' + o.id });
      r.addEventListener('change', () => { pick.model = o.id; load('setup'); });
      const have = o.have.ollama || o.have.lmstudio;
      return h('label', { class: 'brow opt', for: 'm-' + o.id }, r,
        h('div', { class: 't' }, h('div', { class: 'line1' }, h('span', { class: 'ttl', text: o.label }), h('span', { class: 'val', text: o.size }),
          o.id === md.recommend ? h('span', { class: 'rec', text: 'RECOMMENDED' }) : null, have ? h('span', { class: 'done-mark', text: '✓ downloaded' }) : null),
        h('span', { class: 'sub', text: o.tradeoff })));
    });
    const runnerPick = h('div', { class: 'inline' }, h('span', { class: 'lbl', text: 'Runner:' }), runners.map(([id, label, ok]) => {
      const r = h('input', { type: 'radio', name: 'runner', value: id, checked: pick.runner === id, disabled: !ok, id: 'r-' + id });
      r.addEventListener('change', () => { pick.runner = id; load('setup'); });
      return h('label', { class: 'check', for: 'r-' + id }, r, h('span', { text: label + (ok ? '' : ' (not found)') }));
    }));
    const haveIt = pick.runner === 'ollama' ? m.have.ollama : m.have.lmstudio;
    const runnerOk = runners.find((x) => x[0] === pick.runner)[2];
    const src = pick.runner === 'ollama' ? 'ollama.com' : 'LM Studio’s catalogue';
    const sLlm = step('llm', 'Local model (optional)', done.llm, d.test.model, [
      ...runnerRows,
      row('This computer', md.hardware.label, 'The size a model can use well depends on this.'),
      h('div', { class: 'opts' }, opts),
      h('div', { class: 'field' }, runnerPick,
        h('div', { class: 'inline' }, btn(`Set up ${m.label}` + (haveIt ? ' (downloaded)' : ` (${m.size}, from ${src})`),
          () => job('/api/setup/model', { model: m.id, runner: pick.runner }), 'btn lit', { disabled: !runnerOk })),
        h('p', { class: 'help', text: runnerOk ? 'Downloads it here with progress (Cancel stops it), then sends one made-up sentence and turns the local model on when it answers.' : 'Install or start a runner first (above).' }))], first);

    const connectFound = () => {
      const want = Object.fromEntries(todo.map((t) => [t.id, true]));
      post('/api/connect/plan', { want }, (p) => {
        if (!p.rows.length) { toast('Nothing to change.'); return; }
        confirmChanges(p.rows, () => job('/api/connect/apply', { want }));
      });
    };
    const sConnect = step('connect', 'Connect AI apps', done.connect, names(on), [
      row(todo.length ? `Found: ${names(todo)}` : 'No other AI app found', null,
        'This is what lets Claude and your other AI apps know you. Cortex adds itself to each app’s settings; you see the exact files first, and each one is backed up.',
        todo.length ? [btn(`Connect ${names(todo)}`, connectFound, 'btn lit', { disabled: !d.vault.done })] : null),
      row('Choose apps one by one', null, 'Add or remove single apps, and see what each change does.', [btn('Open Connect AI apps', () => show('connect', true))])], first);

    const sHistory = step('history', 'Add your history (optional)', done.history, 'added',
      d.vault.done ? historyBoard(d) : [note('Set up the vault first.')], first);

    const sFinish = step('finish', 'Try it', false, '', [
      h('div', { class: 'pad' }, h('p', { class: 'ink-2', text: on.length ? `Restart ${names(on)} first, so it loads Cortex. Then try these in Claude:` : 'Connect an AI app first (above) and restart it. Then try these in Claude:' }),
        h('ol', { class: 'how' }, d.first_prompts.map((x) => h('li', {}, h('code', { text: x }))))),
      row('Browsing', null, 'Capture new browsing with the extension, privately.', [btn('Open', () => show('browse', true))])], first);
    const steps = { vault: sVault, embed: sEmbed, connect: sConnect, history: sHistory, llm: sLlm };
    put('setup', ...d.order.map((k) => steps[k]), sFinish);
  }

  const RENDER = { setup: renderSetup, ask: renderAsk, status: renderStatus, connect: renderConnect, llm: renderLlm, browse: renderBrowse, vault: renderVault };

  /* ---- header: wordmark, theme, quit, nav ---- */
  $('wordmark').append(dm('CORTEX', 3, 'Cortex'));
  const root = document.documentElement;
  const dayMq = window.matchMedia('(prefers-color-scheme: light)');
  try { const t = localStorage.getItem('cortex-app-theme'); if (t === 'day' || t === 'night') root.dataset.theme = t; } catch (e) { /* storage off */ }
  function themeBtn() {
    const day = root.dataset.theme ? root.dataset.theme === 'day' : dayMq.matches;
    const b = $('theme');
    b.replaceChildren(icon(day ? 'moon' : 'sun'));
    b.setAttribute('aria-label', day ? 'Switch to the night theme' : 'Switch to the day theme');
    b.title = b.getAttribute('aria-label');
  }
  $('theme').addEventListener('click', () => {
    const day = root.dataset.theme ? root.dataset.theme === 'day' : dayMq.matches;
    root.dataset.theme = day ? 'night' : 'day';
    try { localStorage.setItem('cortex-app-theme', root.dataset.theme); } catch (e) { /* storage off */ }
    themeBtn();
  });
  dayMq.addEventListener('change', themeBtn);
  themeBtn();
  $('quit').addEventListener('click', () => confirmDialog('Stop Cortex?', 'Quit',
    [h('p', { class: 'ink-2', text: 'Running jobs are cancelled; your vault is not affected.' })],
    () => post('/api/quit', {}, () => gone('The Cortex app has stopped. You can close this tab.'))));
  $('job-cancel').addEventListener('click', () => post('/api/job/cancel', {}));
  document.querySelectorAll('.side button').forEach((b) => b.addEventListener('click', () => show(b.dataset.s, true)));
  $('section-select').addEventListener('change', (e) => show(e.target.value, true));
  window.addEventListener('popstate', () => { const s = location.hash.slice(1); if (s !== current) show(s, true); });  // Back switches sections

  const want = location.hash.slice(1);
  if (SECTIONS.includes(want)) show(want);
  else api('/api/status').then((d) => show(d.land), () => show('status'));
  idle();
  poll(true);
})();
