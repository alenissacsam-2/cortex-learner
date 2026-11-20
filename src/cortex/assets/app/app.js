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
  history.replaceState(null, '', '/manage' + location.hash);
  const $ = (id) => document.getElementById(id);
  const SVGNS = 'http://www.w3.org/2000/svg';
  const SECTIONS = ['setup', 'chat', 'status', 'review', 'xray', 'connect', 'llm', 'browse', 'vault'];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  /* ---- DOM helpers ---- */
  function h(tag, props, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = String(v);
      else if (k === 'onclick' && tag === 'button') e.addEventListener('click', pressable(e, v));
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
  // a click whose handler returns a promise (post, job, api, a copy) marks its button busy
  // until it settles: aria-busy and the dots in app.css; a second click is ignored. Never
  // disabled: that would drop keyboard focus to the page
  function pressable(b, fn) {
    return (ev) => {
      if (b.getAttribute('aria-busy') === 'true') return undefined;
      const r = fn(ev);
      if (!r || typeof r.then !== 'function') return r;
      b.setAttribute('aria-busy', 'true');
      const done = () => b.removeAttribute('aria-busy');
      r.then(done, done);
      return r;
    };
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
    check: ['M5 12.5l4.5 4.5L19 7.5'],
    x: ['M6 6l12 12', 'M18 6L6 18'],
    open: ['M14 4h6v6', 'M20 4l-9 9', 'M18 14v5H5V6h5'],
  };
  function icon(name) {
    const s = svgEl('svg', { class: 'ico', viewBox: '0 0 24 24', 'aria-hidden': 'true' });
    for (const d of ICONS[name]) s.append(svgEl('path', { d }));
    return s;
  }

  /* ---- talking to the app ---- */
  let dead = false;
  // how this user reopens the app: the server says (the Start menu for an installer install)
  const REOPEN = $('gone').getAttribute('data-reopen') || 'Run cortex app again to reopen it.';
  function gone(text) {
    dead = true;
    const g = $('gone');
    if (text) g.textContent = text;
    g.hidden = false;
    for (const id of ['top', 'jobline', 'shell']) $(id).inert = true;  // only the banner stays live
    $('confirm').close();
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
    if (r.status === 401) gone('This page lost its key. ' + REOPEN);
    if (r.status === 409) poll(true);  // busy: show the job this page did not start (an automatic import)
    if (!r.ok) throw new Error(data.error || `The app answered ${r.status}.`);
    return data;
  }
  /* toast: one message at a time; #toast-msg is the live region (role alert for an error).
     The card stays in the page, faded out, so the region is always there to announce. */
  let toastTimer = null, toastEnd = 0, toastLeft = 0, toastOn = false, toastHeld = false;
  function toastHide() {
    clearTimeout(toastTimer);
    toastTimer = null;
    toastOn = false;
    $('toast').classList.remove('on');
    $('toast-x').hidden = true;
    setTimeout(() => { if (!toastOn) $('toast-msg').textContent = ''; }, 120);  // after the fade out
  }
  function toastRun(ms) {
    clearTimeout(toastTimer);
    toastEnd = Date.now() + ms;
    toastTimer = setTimeout(toastHide, ms);
  }
  function toast(msg, long, kind) {  // kind: 'error', 'ok' or none (info)
    const m = $('toast-msg');
    m.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    m.textContent = msg;
    $('toast').className = 'toast on' + (kind ? ' ' + kind : '');
    $('toast-x').hidden = false;
    toastOn = true;
    toastLeft = long || kind === 'error' ? 6000 : Math.min(4000, 2500 + 25 * String(msg).length);
    if (!toastHeld) toastRun(toastLeft);
  }
  toast.error = (msg) => toast(msg, true, 'error');
  toast.ok = (msg) => toast(msg, false, 'ok');
  // a screen reader hears a short confirmation (Saved, Copied) through the same live region
  function announce(msg) {
    if (toastOn) return;  // a visible toast is being read already
    const m = $('toast-msg');
    m.setAttribute('role', 'status');
    m.textContent = msg;
    setTimeout(() => { if (!toastOn && m.textContent === msg) m.textContent = ''; }, 1500);
  }
  function toastHold() {
    toastHeld = true;
    if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; toastLeft = Math.max(1000, toastEnd - Date.now()); }
  }
  function toastRelease() {
    toastHeld = false;
    if (toastOn) toastRun(toastLeft);
  }
  async function post(path, body, done) {
    try {
      const r = await api(path, body || {});
      if (done) done(r);
      return r;
    } catch (e) {
      toast.error(e.message);
      return null;
    }
  }
  // a check and a word beside a control for 1.5 s (a setting saved on change), also announced
  function savedMark(ctrl) {
    const mark = h('span', { class: 'saved', 'aria-hidden': 'true' }, icon('check'), 'Saved');
    if (ctrl.parentNode && ctrl.parentNode.tagName === 'LABEL') ctrl.parentNode.append(mark);
    else ctrl.after(mark);
    announce('Saved');
    setTimeout(() => mark.remove(), 1500);
  }
  // Copy: the label turns to a check and Copied for 1.5 s; a failure says so in a toast, or
  // runs `onFail` (e.g. selects the text so Ctrl+C copies it)
  function copyBtn(text, failMsg, cls, withIcon, onFail) {
    const face = () => (withIcon ? [icon('copy'), 'Copy'] : ['Copy']);
    let timer;
    const b = btn('', () => Promise.resolve().then(() => navigator.clipboard.writeText(text())).then(() => {
      clearTimeout(timer);
      b.replaceChildren(icon('check'), 'Copied');
      announce('Copied');
      timer = setTimeout(() => b.replaceChildren(...face()), 1500);
    }, () => (onFail ? onFail() : toast.error(failMsg))), cls);
    b.replaceChildren(...face());
    return b;
  }

  /* ---- boards and rows ---- */
  function board(title, opts, ...kids) {
    const o = opts || {};
    const band = h('div', { class: 'band' }, h('h2', { text: title }),
      o.note != null ? h('span', { class: 'band-note' }, o.note) : null,
      o.btn ? btn(o.btn[0], o.btn[1], 'band-btn', o.btn[2]) : null);
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
  let polling = false, pollAgain = false, hideTimer;
  async function job(path, body) {
    const r = await post(path, body);
    if (r && r.disk) {  // too little free space: nothing started; the owner may continue anyway
      confirmDialog('Not enough free space', 'Continue anyway', [h('p', { class: 'ink-2', text: r.disk })],
        () => job(path, { ...body, override: true }));
      return null;
    }
    if (r) poll();
    return r;
  }
  function showJob(j) {
    clearTimeout(hideTimer);
    const r = j.result || {};
    const word = j.error ? 'failed' : r.cancelled ? 'cancelled' : r.need === 'embed' ? 'stopped: needs the search model' : r.error === 'no_answer' ? 'no answer' : r.error ? 'stopped: needs setup' : r.ok === false ? 'failed' : 'done';
    // running: the moving bar under the line; done: a brief success tint; failed: the error tint
    $('jobline').classList.remove('idle', 'running', 'ok', 'bad', 'ended', 'determinate');
    $('jobline').classList.add(j.running ? 'running' : word === 'done' ? 'ok' : word === 'failed' ? 'bad' : 'ended');
    $('job-name').textContent = j.running ? j.name : `${j.name}: ${word}`;
    // a stepped job: "Step 3 of 5 · checking titles · 42% · about 2 min left", a filling bar
    const p = j.running ? j.progress : null;
    if (p) {
      $('jobline').classList.add('determinate');
      $('jobline').style.setProperty('--pct', `${p.overall}%`);
    }
    $('job-line').textContent = p ? stepText(p) : j.running ? j.line : (j.error || '');
    $('job-time').textContent = `${Math.round(j.seconds)} s`;
    $('job-cancel').hidden = !(j.running && j.cancellable);
  }
  // initial: only a job still running (on open, the periodic look, a busy refusal); a job this
  // page started while a look is under way is followed once that look ends, never dropped
  async function poll(initial) {
    if (polling) { if (!initial) pollAgain = true; return; }
    polling = true;
    try {
      for (;;) {
        if (pollAgain) { pollAgain = false; initial = false; }
        const v = await api('/api/job');
        const j = v.job;
        if (!j || (initial && !j.running)) break;  // on open: only a job still running
        initial = false;
        showJob(j);
        if (!j.running) { finish(j); break; }
        await sleep(600);
      }
    } catch (e) { /* the app stopped: gone() said so */ } finally {
      polling = false;
      if (pollAgain && !dead) { pollAgain = false; poll(); }
    }
  }
  function stepText(p) {
    const s = p.eta_s;
    const left = s == null ? '' : s < 60 ? ' · less than a minute left'
      : s < 3600 ? ` · about ${Math.round(s / 60)} min left` : ` · about ${Math.round(s / 3600)} h left`;
    return `Step ${p.step} of ${p.steps} · ${p.label} · ${p.pct}%${left}`;
  }
  function idle() {
    $('jobline').classList.remove('running', 'ok', 'bad', 'ended', 'determinate');
    $('jobline').classList.add('idle');
    $('job-name').textContent = 'Nothing running';
    $('job-line').textContent = 'Long actions show their progress here, one at a time.';
    $('job-time').textContent = '';
    $('job-cancel').hidden = true;
  }
  const jobFailed = (j) => { const r = j.result || {}; return !!j.error || (r.ok === false && !r.cancelled && !r.need); };
  function finish(j) {
    if (jobFailed(j)) toast.error(j.error || `${j.name}: failed.`);
    else if (j.name === 'Start at login') toast.ok('The receiver now starts at login.');  // server.login's job name
    if (guide && guide.wait && j.section === 'setup') {  // a guided import's own job: shown in its panel
      const w = guide.wait;
      guide.wait = null;
      if (w === 'preview' && j.error) Object.assign(guide, { error: j.error, upload: null });
      else if (w === 'preview') guide.preview = j.result;
      else Object.assign(guide, { done: j, upload: j.result && j.result.need ? guide.upload : null });
    } else if (j.section === 'update') {  // shown in the Updates board
      upd = j;
      if (j.result && j.result.launched) gone('Cortex is closing to install the update. It reopens by itself when it is done; you can close this tab.');
    }
    else {
      // a command that stopped with an error (ok: false) failed too
      outputs[j.section] = { title: j.name, nodes: resultNodes(j), error: jobFailed(j) };
      if (desktopChanged(j)) toast(RESTART_DESKTOP, true);
    }
    hideTimer = setTimeout(idle, 8000);
    if (current) load(current);
  }
  const RESTART_DESKTOP = 'Claude Desktop loads Cortex only when it starts, and closing its window does not stop it: right-click the Claude icon near the clock (the tray; on a Mac, the menu bar), choose Quit, then open Claude again.';
  const desktopChanged = (j) => j.section === 'connect' && !!j.result && (j.result.rows || []).some((x) => x.id === 'claude-desktop' && x.change !== 'error');
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
      h('thead', {}, h('tr', {}, h('th', { text: 'What' }), h('th', { class: 'n', text: 'Count' }))),
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
    if (desktopChanged(j)) out.push(row('Next: restart Claude Desktop', null, RESTART_DESKTOP, null));
    if (r.kind === 'scan') out.push(r.rows.length ? scanTable(r.rows) : note('No browser history found on this computer.'));
    if (r.counts) out.push(Object.keys(r.counts).length ? countsTable(r.counts) : note('Nothing to count.'));
    if (r.answer) out.push(row('Answer', `${r.answer} in ${r.seconds} s`, r.runner, null));
    if (r.admin) out.push(row('Install Git for everyone on this computer', null, r.admin,
      [btn('Install Git for all users (Windows will ask for admin permission)', () => job('/api/setup/git', { scope: 'machine', from: j.section }), 'btn lit'), ext(r.license, 'License')]));
    if (r.need === 'embed') out.push(row('Search model needed', null, r.lines[0], [btn('Download the search model', () => job('/api/setup/embed', {}), 'btn lit')]));
    else if (r.lines && r.lines.length) out.push(h('pre', { class: 'out', text: r.lines.join('\n') }));
    return out;
  }
  function outputBoard(s) {
    const o = outputs[s];
    return o ? board(`Last action: ${o.title}`, { note: o.error ? 'failed' : 'finished' }, ...o.nodes) : null;
  }

  /* ---- sections ---- */
  let current = null, landing = false;  // landing: Status opened in place of the app's own choice
  function show(s, focus) {
    if (!SECTIONS.includes(s)) s = 'status';
    if (focus) landing = false;  // the user chose: Status's answer does not move them
    current = s;
    try { sessionStorage.setItem('cortex.section', s); } catch (e) { /* storage off */ }
    for (const x of SECTIONS) $('s-' + x).hidden = x !== s;
    document.querySelectorAll('.side button').forEach((b) => b.setAttribute('aria-current', b.dataset.s === s ? 'page' : 'false'));
    $('section-select').value = s;
    if (location.hash !== '#' + s) history[focus ? 'pushState' : 'replaceState'](null, '', '#' + s);  // a click is a Back step
    load(s);
    if (focus) {  // focus on the heading (screen readers say where they are); the section lands below the sticky header (scroll-margin-top in app.css)
      ($('s-' + s).querySelector('h1') || $('s-' + s)).focus({ preventScroll: true });
      $('s-' + s).scrollIntoView({ block: 'start' });
    }
  }
  async function load(s, fresh) {
    const box = $(s);
    if (!box.firstChild) box.append(note('Loading…'));
    else {  // the old boards stay up, dimmed, until the new answer replaces them
      box.classList.add('refreshing');
      box.setAttribute('aria-busy', 'true');
    }
    try {
      const d = await api('/api/' + s + (fresh ? '?fresh=1' : ''));  // fresh: Status asks every probe again
      if (current === s) RENDER[s](d);
    } catch (e) {
      if (!dead) fail(s, e.message);
    } finally {
      box.classList.remove('refreshing');
      box.setAttribute('aria-busy', 'false');
    }
  }
  // a redraw keeps keyboard focus that was inside the section: on the same control in the new
  // boards (by id, a radio by name and value, else by tag and name), else on the section's heading
  const focusKey = (e) => (e.id ? '#' + e.id : e.getAttribute('name') ? `${e.getAttribute('name')}=${e.value}`
    : e.getAttribute('aria-label') || e.textContent);
  function put(s, ...nodes) {
    const box = $(s), a = document.activeElement;
    const inside = !!a && a !== box && box.contains(a);
    const like = (k) => [...box.querySelectorAll(a.tagName)].filter((e) => focusKey(e) === k);
    const k = inside ? focusKey(a) : null, n = inside ? like(k).indexOf(a) : 0;  // the nth of same-named controls
    box.replaceChildren(...nodes.filter(Boolean), outputBoard(s) || '');
    if (!inside) return;
    const found = like(k);
    (found[n] || found[0] || $('s-' + s).querySelector('h1') || $('s-' + s)).focus({ preventScroll: true });
  }

  function renderStatus(d) {
    if (landing) {  // the first answer of a page opened with no section: the app's choice (Setup until it is done)
      landing = false;
      if (d.land !== 'status' && SECTIONS.includes(d.land)) { show(d.land); return; }
    }
    const rows = d.rows.map((r) => {
      // at: a time the row names where "{ago}" stands, in words, the exact time in a title
      const fill = (t) => (r.at ? String(t).replace('{ago}', daysAgo(r.at, true)) : t);
      const title = r.at ? new Date(r.at).toLocaleString() : r.value;
      return h('div', { class: 'brow srow' }, stateCell(r.state),
        h('div', { class: 't' },
          h('div', { class: 'line1' }, h('span', { class: 'ttl', text: r.label }), h('span', { class: 'val', text: fill(r.value), title })),
          h('span', { class: 'sub', text: fill(r.detail) }),
          r.login_hint ? h('span', { class: 'sub', text: LOGIN_HINT }) : null),
        h('div', { class: 'acts' }, r.count != null ? dm(String(r.count), 2, String(r.count)) : null,
          r.import && r.import.length ? btn('Import new history now', () => job('/api/browse/import', { sources: r.import, from: 'browse' }), 'btn lit') : null,
          r.login_hint ? loginBtn() : null,
          r.install ? btn(r.install.label, () => job('/api/setup/git', { from: 'status' }), 'btn lit') : null,
          r.install ? ext(r.install.license, 'License') : null,
          r.page ? ext(r.page, 'Get Git') : null,
          r.action ? btn(r.action.label, () => show(r.action.go, true)) : null));
    });
    const t = new Date(), hm = `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`;
    const wn = d.whats_new || {};
    put('status', wn.show && wn.show.length ? newsBoard(wn.show, true) : null,
      board('This computer', { note: ['checked ', dm(hm, 2, hm)], btn: ['Check again', () => load('status', true)] },
        ...(rows.length ? rows : [note('Nothing to check yet. Check again in a moment.')])),
      d.first_run ? null : chatImportBoard(d.chat_import !== false),
      d.update ? updatesBoard(d.update, wn.current) : null);
  }
  /* app.chat_import: past AI chats saved as chat pages every 5 minutes, no model (on by default) */
  function chatImportBoard(on) {
    const cb = h('input', { type: 'checkbox', id: 'chat-import', checked: on });
    cb.addEventListener('change', () => post('/api/app/chat-import', { enabled: cb.checked }, () => savedMark(cb)));
    return board('Past AI chats', {},
      h('div', { class: 'field' }, h('label', { class: 'check', for: 'chat-import' }, cb, h('span', { text: 'Save new AI chats to your notes automatically' })),
        h('p', { class: 'help', text: 'Every 5 minutes, Claude Code chats and the chats you imported become chat pages: your own messages only (a coding session as a short summary of files and commits, never code), with keys and passwords removed. No AI model is used. Chats in a category you set to skip are never saved.' })));
  }

  /* What's new: shown once after an update (Got it records it), and any time from Updates */
  function newsBoard(entries, pending) {
    const hide = () => { b.remove(); ($('s-status').querySelector('h1') || $('s-status')).focus(); };  // focus stays on the page
    const b = board(`What’s new in Cortex ${entries[0].version}`, {
      id: 'whats-new',
      btn: pending ? ['Got it', () => post('/api/whats_new/seen', {}, () => { hide(); toast.ok('Read it again any time: Updates, What’s new.'); })]
        : ['Close', hide],
    }, ...entries.map((e) => h('div', { class: 'pad md' },
      entries.length > 1 ? h('h3', { text: `Cortex ${e.version}` }) : null,
      h('ul', {}, e.items.map((t) => h('li', { text: t }))),
      e.do && e.do.length ? [h('p', {}, h('b', { text: 'What to do' })), h('ol', { class: 'how' }, e.do.map((t) => h('li', { text: t })))] : null)));
    b.querySelector('h2').setAttribute('tabindex', '-1');
    return b;
  }
  function openNews(cur) {
    let b = $('whats-new');
    if (!b) {
      b = newsBoard([cur], false);
      $('status').replaceChildren(b, ...$('status').childNodes);
    }
    b.querySelector('h2').focus();
  }

  /* Updates: the app checks GitHub every 6 hours and prepares a newer release (u.ready); it is
     installed only on a click */
  let upd = null;  // the last check or update job
  function updatesBoard(u, cur) {
    const r = upd && upd.result;
    const cmd = (c) => h('div', { class: 'pad' }, h('div', { class: 'token' }, h('code', { text: c }),
      copyBtn(() => c, 'Copy failed; select the command and copy it.')));
    const out = [row(`Cortex ${u.current}`, null, null, [cur ? btn('What’s new', () => openNews(cur)) : null,
      btn('Check for updates', () => { upd = null; return job('/api/update/check', {}); })])];
    if (upd && upd.error) out.push(row('That did not work', null, upd.error, null));
    else if (r && r.cancelled) out.push(row('Cancelled', null, 'Nothing was installed; update again any time.', null));
    else if (r && !r.newer) out.push(row('You have the latest version', r.latest, null, null));
    else if (r) {
      const mb = r.size ? ` (about ${Math.max(1, Math.round(r.size / 2 ** 20))} MB)` : '';  // unknown when the API was busy
      const now = () => confirmDialog('Update Cortex?', 'Update now',
        [h('p', { class: 'ink-2', text: `Cortex will download the update${mb}, close, and reopen when it's done. AI apps connected to Cortex may need a restart.` })],
        () => job('/api/update/apply', {}));
      out.push(row(`Cortex ${r.latest} is available`, null, r.command ? 'Run this in a terminal to update:' : null,
        [ext(r.notes_url, 'Release notes'), r.mode === 'installer' ? btn('Update now', now, 'btn lit') : null]));
      if (r.command) out.push(cmd(r.command));
    } else if (u.ready) {  // found and prepared by the app's own check
      out.push(row(`Cortex ${u.ready.version} is ready`, null, u.ready.command ? 'Run this in a terminal to update:'
        : 'Downloaded and checked. Cortex closes, installs it and reopens.',
      [ext(u.ready.notes_url, 'Release notes'), u.ready.command ? null : btn('Restart to update', () => job('/api/update/apply', {}), 'btn lit')]));
      if (u.ready.command) out.push(cmd(u.ready.command));
    }
    return board('Updates', { note: 'checked every 6 hours, installed only when you click' }, ...out);
  }

  /* Chat: a model on this computer answers from your notes, saved chats and browsing; never a
     cloud model. The answer streams as NDJSON from POST /api/chat and is rendered by a tiny
     safe renderer (textContent only: paragraphs, lists, bold, code, [n] citations). */
  const MISSING = {  // [title, value, why, button]
    embed: ['Search model', 'not downloaded', 'Finds the notes and pages that answer your question. A one-time download.', 'Set up the search model'],
    llm: ['Local model', 'not running', 'Writes the answer on this computer, never in the cloud. Start Ollama or LM Studio, or set one up (a download of a few GB).', 'Set up a local model'],
  };
  const EXAMPLES = ['What did I read about binary search?', 'Which sites did I visit yesterday?', 'What mistakes do I keep making?'];
  // answered from Cortex's own data, with no search model and no local model (chat.site_list, chat_study)
  const DATA_EXAMPLES = ['Which sites did I visit yesterday?', 'What should I study today?', 'What is due for review?', 'Where am I weak?'];
  const KIND = { note: 'note', chat: 'chat', browse: 'browsing' };
  const chatS = { d: null, id: null, msgs: [], draft: '', busy: null, setOpen: false, focus: false, follow: true, pill: null };
  const last = (e) => e.childNodes[e.childNodes.length - 1];

  function inline(text, cite) {  // **bold**, `code` and [n] citations; everything else is text
    const out = [];
    for (const part of String(text).split(/(\*\*[^*\n]+\*\*|`[^`\n]+`|\[\d+(?:\s*,\s*\d+)*\])/)) {
      if (!part) continue;
      if (part.startsWith('**') && part.length > 4) out.push(h('strong', { text: part.slice(2, -2) }));
      else if (part.startsWith('`') && part.length > 2) out.push(h('code', { text: part.slice(1, -1) }));
      else if (/^\[\d/.test(part)) {
        for (const n of part.slice(1, -1).split(',').map((x) => x.trim())) {
          out.push(h('button', { class: 'cite', type: 'button', text: n, 'aria-label': `Source ${n}`, onclick: () => cite(n) }));
        }
      } else out.push(part);
    }
    return out;
  }
  function md(text, cite) {  // paragraphs, "- " and "1." lists; never HTML, links or images
    const nodes = [];
    let cur = null, kind = '';  // the open paragraph or list and its kind: p, ul or ol
    for (const l of String(text).split('\n')) {
      const k = !l.trim() ? '' : /^\s*[-*•]\s+/.test(l) ? 'ul' : /^\s*\d+[.)]\s+/.test(l) ? 'ol' : 'p';
      if (k !== kind || !k) { cur = k ? h(k) : null; kind = k; if (cur) nodes.push(cur); }
      if (k === 'p') { if (cur.childNodes.length) cur.append(h('br')); cur.append(...inline(l, cite)); }
      else if (k) cur.append(h('li', {}, inline(l.replace(/^\s*(?:[-*•]|\d+[.)])\s+/, ''), cite)));
    }
    return nodes;
  }
  // hours: under a day in hours, then "n days ago" at any age (the caller titles the exact time)
  function daysAgo(ts, hours) {
    const d = new Date(ts);
    if (isNaN(d)) return '';
    const hr = Math.floor((Date.now() - d) / 36e5);
    if (hours && hr < 24) return hr < 1 ? 'less than an hour ago' : `${hr} hour${hr === 1 ? '' : 's'} ago`;
    const n = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(d).setHours(0, 0, 0, 0)) / 864e5);
    return n <= 0 ? 'today' : n === 1 ? 'yesterday' : n < 7 || hours ? `${n} days ago` : d.toISOString().slice(0, 10);
  }
  // each pill opens its passage (`open`); the pill of the open one is pressed
  function sourceRow(sources, searched, open, shown) {
    const pill = (s, label) => h('button', { class: 'chip', type: 'button', 'data-n': s.n, 'aria-pressed': String(shown === s),
      title: 'Show the passage', onclick: () => open(s) },
    h('span', { class: 'ct', text: label }), h('span', { class: 'tag', text: KIND[s.kind] || s.kind }));
    if ((!sources || !sources.length) && searched && searched.length)  // the model cited nothing
      return h('div', { class: 'chat-src' }, h('span', { class: 'ink-3 small', text: 'Searched, not cited' }),
        searched.map((s) => pill(s, s.title || s.path)));
    if (!sources || !sources.length) return null;
    return h('div', { class: 'chat-src' }, h('span', { class: 'ink-3 small', text: 'Sources' }),
      sources.map((s) => pill(s, `${s.n} · ${s.title || s.path}`)));
  }
  // the facts Cortex computed (chat_facts), above the model's words: exactly as counted
  const FACT_NOUN = { notes: 'notes', chats: 'saved AI chats', journal: 'journal lines', browsing: 'visits', study: 'study events' };
  const FACT_SRC = { notes: 'notes', chats: 'chats', journal: 'journal', browsing: 'browsing', study: 'study' };
  function factsBox(f) {
    const n = (x) => Number(x).toLocaleString();
    const period = f.period ? (f.period[0] === f.period[1] ? f.period[0] : `${f.period[0]} to ${f.period[1]}`) : 'all time';
    const big = (num, label) => [h('span', { class: 'fx-n', text: n(num) }), h('span', { text: label })];
    let head;
    if (f.kind === 'count') head = big(f.total, f.unit === 'days' ? 'days' : f.focus ? FACT_NOUN[f.focus] : 'mentions');
    else if (f.kind === 'timeline') head = big(f.total, `items across ${n(f.rows.length + (f.more || 0))} ${f.unit}s`);
    else if (f.kind === 'about') head = big(f.total, 'mentions');
    else head = f.item ? [h('span', { class: 'fx-n', text: f.item.day }), h('span', { text: `${f.kind} time: ${f.item.title} (${FACT_SRC[f.item.source] || f.item.source})` })]
      : big(0, 'matches');
    const extra = f.kind === 'about' && f.first ? h('div', { class: 'fx-line', text: `First ${f.first.day}, last ${f.last.day}` + (f.page ? ` · page: ${f.page.title}` : '') }) : '';
    const cov = f.coverage || {};
    return h('div', { class: 'facts', role: 'group', 'aria-label': 'Facts computed by Cortex' },
      h('div', { class: 'fx-top' }, h('span', { class: 'fx-ttl', text: 'Computed from your data' }), h('span', { class: 'ink-3', text: period + (f.query ? ` · “${f.query}”` : '') })),
      h('div', { class: 'fx-head' }, ...head), extra,
      Object.keys(f.sources || {}).length ? h('div', { class: 'fx-src' }, Object.entries(f.sources).map(([s, c]) =>
        h('span', { class: 'fx-chip' }, h('span', { class: 'tag', text: FACT_SRC[s] || s }), h('span', { text: n(c) })))) : '',
      cov.read != null ? h('div', { class: 'fx-cov', text: `Counted all ${n(cov.matched)} matches in ${n(cov.read)} items read`
        + (cov.off && cov.off.length ? ` · switched off: ${cov.off.join(', ')}` : '') }) : '');
  }
  // the exact passage a source stands for (GET /api/chat/passage, under the switches of now)
  function passageView(s, p, close) {
    const kids = p.error ? [h('p', { class: 'ink-3 small', text: p.error })]
      : !p.data ? [h('p', { class: 'thinking', text: 'Opening the passage…' })]
        : [h('div', { class: 'pv-where' }, h('span', { class: 'tag', text: KIND[p.data.kind] || p.data.kind }),
          h('strong', { text: p.data.title || s.path }), p.data.heading ? h('span', { class: 'ink-3', text: `› ${p.data.heading}` }) : ''),
        h('div', { class: 'pv-text', text: p.data.text || '(this part of the page is empty now)' })];
    return h('div', { class: 'passage', role: 'region', 'aria-label': `Passage ${s.n || ''}`.trim() }, ...kids,
      h('div', { class: 'msg-acts' }, btn('Open the page in Obsidian', () => post('/api/open', { what: 'page', path: s.path }), 'btn quiet'),
        btn('Close', close, 'btn quiet')));
  }
  function botMsg(m) {  // {text, sources, searched, model, stopped, facts, coverage}; returns the element and its updater
    const body = h('div', { class: 'md' }), el = h('div', { class: 'msg bot' }), who = h('div', { class: 'who' });
    let live = false, shown = null, p = null;  // the source whose passage is open, and its fetch
    const open = async (s) => {
      if (shown === s) { shown = p = null; draw(live); return; }
      shown = s; p = {};
      draw(live);
      const mine = p;
      try {
        mine.data = await api(`/api/chat/passage?path=${encodeURIComponent(s.path)}&at=${s.at || 0}&len=${s.len || 0}`);
      } catch (e) { mine.error = e.message; }
      if (p === mine) draw(live);
    };
    const cite = (n) => {
      const s = (m.sources || []).find((x) => String(x.n) === String(n));
      if (s) open(s);
    };
    const draw = (streaming) => {
      live = streaming;
      who.textContent = 'Cortex' + (m.model === 'Cortex' ? ' · from your data' : m.model ? ` · ${m.model}` : '') + (!streaming && m.seconds ? ` · ${Math.round(m.seconds)} s` : '');
      if (streaming && !m.text) {  // each stage with a live count, so a slow one never looks frozen
        const secs = Math.floor((Date.now() - m.t0) / 1000);
        body.replaceChildren(h('span', { class: 'thinking', text: `${m.thinking ? 'Thinking' : m.step || 'Sending your question'}… ${secs} s` }));
      } else if (m.thinking && !m.text) body.replaceChildren(h('span', { class: 'thinking', text: 'thinking…' }));
      else body.replaceChildren(...md(m.text, cite));
      if (streaming) {
        let at = last(body) || body;
        if (at.tagName === 'UL' || at.tagName === 'OL') at = last(at);
        if (!at || at.tagName === 'SPAN') at = body;
        at.append(h('span', { class: 'caret', 'aria-hidden': 'true' }));
      }
      // a finished answer gets Copy: its text without the [n] markers; the sources stay out
      const copy = !streaming && m.text ? h('div', { class: 'msg-acts' }, copyBtn(() => m.text.replace(/\s*\[\d+(?:\s*,\s*\d+)*\]/g, ''),
        'Copy failed; select the answer and copy it.', 'btn quiet', true)) : '';
      const cov = m.coverage;  // what the model read of what the search found
      el.replaceChildren(who, m.facts ? factsBox(m.facts) : '', body, copy, sourceRow(m.sources, m.searched, open, shown) || '',
        shown ? passageView(shown, p, () => open(shown)) : '',
        cov && !streaming ? h('p', { class: 'ink-3 small cov', text: `Read ${cov.read} of ${cov.matched} match${cov.matched === 1 ? '' : 'es'}` }) : '',
        m.stopped ? h('p', { class: 'ink-3 small', text: 'Stopped before the end.' }) : '');
    };
    draw(false);
    return { el, draw };
  }
  function userMsg(m) {
    return h('div', { class: 'msg you' }, h('div', { class: 'who', text: 'You' }), h('div', { class: 'md' }, ...md(m.text, () => {})));
  }
  function missingRows(keys, why) {
    return h('div', { class: 'board' }, note(why || 'Chat needs two things on this computer. Still missing:'),
      keys.map((k) => {
        const [title, value, help, label] = MISSING[k];
        return row(title, value, help, [btn(label, () => { opened.add(k); show('setup', true); }, 'btn lit')]);
      }));
  }

  async function openChat(id) {
    if (chatS.busy) return;
    if (!id) Object.assign(chatS, { id: null, msgs: [] });
    else {
      try {
        const r = await api('/api/chat/get?id=' + encodeURIComponent(id));
        Object.assign(chatS, { id, msgs: r.messages });
      } catch (e) { toast.error(e.message); return; }
    }
    chatS.focus = true;
    renderChat(chatS.d);
  }
  async function reloadChats() {
    try { chatS.d.chats = (await api('/api/chat/list')).chats; } catch (e) { /* the page says so */ }
  }
  function saveSetting(body, ctrl) {  // on change: a Saved mark beside the control
    return post('/api/chat/settings', body, (s) => { chatS.d.settings = s; savedMark(ctrl); });
  }
  function settingsPanel(s) {
    const length = h('select', { id: 'chat-length' }, ['short', 'normal', 'long'].map((v) => h('option', { value: v, text: v[0].toUpperCase() + v.slice(1) })));
    length.value = s.length;
    length.addEventListener('change', () => saveSetting({ length: length.value }, length));
    const temp = h('input', { type: 'range', id: 'chat-temp', min: '0', max: '1', step: '0.1', value: String(s.temperature) });
    temp.addEventListener('change', () => saveSetting({ temperature: Number(temp.value) }, temp));
    const k = h('select', { id: 'chat-k' }, [4, 5, 6, 7, 8, 9, 10, 11, 12].map((n) => h('option', { value: String(n), text: String(n) })));
    k.value = String(s.passages);
    k.addEventListener('change', () => saveSetting({ passages: Number(k.value) }, k));
    const srcNames = { notes: 'Notes and mistakes', chats: 'Saved AI chats', browsing: 'Browsing', private: 'Private notes and browsing' };
    const srcs = Object.entries(srcNames).map(([key, label]) => {
      const cb = h('input', { type: 'checkbox', id: 'chat-src-' + key, checked: s.sources[key] });
      cb.addEventListener('change', () => saveSetting({ sources: { [key]: cb.checked } }, cb));
      return h('label', { class: 'check', for: 'chat-src-' + key }, cb, h('span', { text: label }));
    });
    const keep = h('input', { type: 'checkbox', id: 'chat-keep', checked: s.history });
    keep.addEventListener('change', () => saveSetting({ history: keep.checked }, keep));
    const wipe = () => confirmDialog('Delete all chats?', 'Delete',
      [h('p', { class: 'ink-2', text: 'Every saved chat is deleted from this computer. This cannot be undone.' })],
      () => post('/api/chat/delete', { all: true }, (r) => {
        toast.ok(`Deleted ${r.deleted} chat${r.deleted === 1 ? '' : 's'}.`);
        Object.assign(chatS, { id: null, msgs: [] });
        load('chat');
      }));
    const det = h('details', { class: 'chat-set', open: chatS.setOpen },
      h('summary', { text: 'Chat settings' }),
      h('div', { class: 'set-panel' },
        h('div', { class: 'field' }, h('label', { for: 'chat-length', text: 'Answer length' }), length),
        h('div', { class: 'field' }, h('label', { for: 'chat-temp', text: 'Creativity' }), temp,
          h('p', { class: 'help', text: 'Left: sticks closely to your pages. Right: freer wording.' })),
        h('div', { class: 'field' }, h('label', { for: 'chat-k', text: 'Pages read per answer' }), k),
        h('div', { class: 'field' }, h('span', { class: 'lbl', text: 'Search in' }), ...srcs),
        h('div', { class: 'field' }, h('label', { class: 'check', for: 'chat-keep' }, keep, h('span', { text: 'Keep chat history' })),
          h('p', { class: 'help', text: 'Off: new chats are forgotten when the app stops; nothing is written.' })),
        h('div', { class: 'field' }, btn('Delete all chats', wipe))));
    det.addEventListener('toggle', () => { chatS.setOpen = det.open; });
    return det;
  }

  function drawSide() {  // the past chats, wide (a list) and narrow (a select); redrawn in place
    const side = $('chat-side'), pick = $('chat-pick');
    if (!side || !pick) return;
    const chats = chatS.d.chats || [];
    const list = h('ul', { class: 'chat-list', 'aria-label': 'Past chats' }, chats.map((c) => h('li', { class: 'chat-item' + (c.id === chatS.id ? ' on' : '') },
      h('button', { class: 'chat-open', type: 'button', 'aria-current': c.id === chatS.id ? 'true' : null, onclick: () => openChat(c.id) },
        h('span', { class: 't', text: c.title }), h('span', { class: 'd', text: daysAgo(c.updated) })),
      btn('Delete', () => post('/api/chat/delete', { id: c.id }, () => {
        if (chatS.id === c.id) Object.assign(chatS, { id: null, msgs: [] });
        load('chat');
      }), 'chat-del', { 'aria-label': `Delete chat: ${c.title}` }))));
    side.replaceChildren(btn('New chat', () => openChat(null), 'btn lit chat-new'),
      chats.length ? list : h('p', { class: 'ink-3 small', text: 'No chats yet. Ask something to start one.' }));
    pick.replaceChildren(h('option', { value: '', text: 'New chat' }),
      ...chats.map((c) => h('option', { value: c.id, text: `${c.title} · ${daysAgo(c.updated)}` })));
    pick.value = chatS.id || '';
  }

  function renderChat(d) {
    chatS.d = d;
    if (chatS.busy) return;  // never rebuild under a streaming answer
    if (!d.vault) {
      $('chat').replaceChildren(board('Chat', {}, row('No vault yet', null, 'Set one up first.', [btn('Open Setup', () => show('setup', true))])));
      return;
    }
    const pick = h('select', { class: 'chat-pick', id: 'chat-pick', 'aria-label': 'Chat' });
    pick.addEventListener('change', () => openChat(pick.value || null));
    const side = h('div', { class: 'chat-side', id: 'chat-side' });

    const log = h('div', { class: 'chat-log', id: 'chat-log', role: 'log', 'aria-live': 'polite', 'aria-label': 'Conversation' });
    for (const m of chatS.msgs) log.append(m.role === 'user' ? userMsg(m) : botMsg(m).el);
    const missing = d.missing || [];
    if (!chatS.msgs.length) {  // the examples fill the message box
      log.append(h('div', { class: 'chat-empty' }, h('p', { text: 'Ask about anything you\'ve saved or browsed.' }),
        missing.length ? h('p', { text: 'Until then, Chat can still answer questions about your sites and your studying, like these:' }) : null,
        h('div', { class: 'inline' }, (missing.length ? DATA_EXAMPLES : EXAMPLES).map((x) => btn(x, () => { chatS.draft = x; chatS.focus = true; renderChat(chatS.d); })))));
    }
    // something missing: what to set up, above the message box (site and study questions still work)
    const need = missing.length ? missingRows(missing, d.llm_error ? `The local model is not used: ${d.llm_error}` : null) : null;
    let foot;
    {
      const ta = h('textarea', { id: 'chat-input', rows: '1', value: chatS.draft, placeholder: 'Message Cortex', 'aria-label': 'Message', spellcheck: 'true' });
      const grow = () => { ta.rows = String(Math.min(6, Math.max(1, ta.value.split('\n').length))); };
      grow();
      ta.addEventListener('input', () => { chatS.draft = ta.value; grow(); });
      const go = h('button', { class: 'btn lit chat-send', type: 'button', id: 'chat-send', text: 'Send' });
      // a Stop within 400 ms of sending is a double click on Send, not a Stop (Esc still stops)
      go.addEventListener('click', () => (!chatS.busy ? send(ta, go, log) : Date.now() - chatS.busy.t0 > 400 ? chatS.busy.ctrl.abort() : null));
      ta.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!chatS.busy) send(ta, go, log); }
      });
      // shown while an answer streams and the reader has scrolled up: jumps to the end
      chatS.pill = h('button', { class: 'btn new-text', type: 'button', hidden: true, onclick: () => { chatS.follow = true; chatS.pill.hidden = true; toEnd(); } }, '↓ New text');
      foot = h('div', { class: 'composer' }, chatS.pill, h('div', { class: 'crow' }, ta, go),
        h('p', { class: 'help', text: missing.length ? 'Runs on this computer. Nothing leaves it.' : `Runs on this computer with ${d.model || 'your local model'}. Nothing leaves it.` }));
    }
    const main = h('div', { class: 'chat-main' }, h('div', { class: 'chat-bar' }, pick, settingsPanel(d.settings)), need, log, foot);
    $('chat').replaceChildren(h('div', { class: 'chat' }, side, main));
    drawSide();
    if (chatS.focus && $('chat-input')) $('chat-input').focus();
    chatS.focus = false;
  }

  // the page follows a streaming answer only while the reader is at the bottom; scrolled up,
  // it stays put and the New text pill offers the jump
  const toEnd = () => window.scrollTo(0, document.documentElement.scrollHeight);
  const atEnd = () => window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 48;
  function followEnd() {
    if (chatS.follow) toEnd();
    else if (chatS.pill) chatS.pill.hidden = false;
  }
  window.addEventListener('scroll', () => {
    const end = atEnd();
    if (chatS.busy) chatS.follow = end;
    if (end && chatS.pill) chatS.pill.hidden = true;
  }, { passive: true });
  // Esc stops a streaming answer, like Stop
  $('chat').addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && chatS.busy) { e.preventDefault(); chatS.busy.ctrl.abort(); }
  });

  async function send(ta, go, log) {
    const text = ta.value.trim();
    if (!text) { toast('Type a message first.'); return; }
    const ctrl = new AbortController();
    chatS.busy = { ctrl, t0: Date.now() };
    const empty = log.querySelector('.chat-empty');
    if (empty) log.replaceChildren();  // the examples go once a chat starts
    chatS.msgs.push({ role: 'user', text });
    log.append(userMsg({ text }));
    const m = { role: 'assistant', text: '', sources: [], model: chatS.d.model, t0: Date.now() };
    const bot = botMsg(m);
    bot.draw(true);
    const tick = setInterval(() => { if (!m.text) bot.draw(true); }, 1000);
    log.append(bot.el);
    log.setAttribute('aria-busy', 'true');  // a screen reader hears the answer once, when it is done
    chatS.follow = true;
    if (chatS.pill) chatS.pill.hidden = true;
    toEnd();
    ta.value = chatS.draft = '';
    ta.rows = '1';
    ta.focus();
    go.textContent = 'Stop';
    go.setAttribute('aria-label', 'Stop the answer');
    let failed = null;
    try {
      const r = await fetch('/api/chat', {
        method: 'POST', headers: { 'X-Cortex-Token': T, 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: chatS.id, text }), signal: ctrl.signal,
      });
      if (!r.ok) {
        let data = {};
        try { data = await r.json(); } catch (e) { /* no body */ }
        if (r.status === 401) gone('This page lost its key. ' + REOPEN);
        throw new Error(data.error || `The app answered ${r.status}.`);
      }
      const reader = r.body.getReader(), dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, i).trim();
          buf = buf.slice(i + 1);
          if (!line) continue;
          const ev = JSON.parse(line);
          if (ev.t === 'wait') continue;  // the server's keepalive while the model is quiet
          if (ev.id) chatS.id = ev.id;
          if (ev.t === 'start') m.model = ev.model;
          else if (ev.t === 'step') m.step = ev.s;
          else if (ev.t === 'done') m.seconds = ev.seconds;
          else if (ev.t === 'think') m.thinking = true;
          else if (ev.t === 'tok') m.text += ev.s;
          else if (ev.t === 'text') m.text = ev.s;
          else if (ev.t === 'facts') m.facts = ev.facts;
          else if (ev.t === 'src') { m.sources = ev.sources; m.searched = ev.searched || []; m.coverage = ev.coverage; }
          else if (ev.t === 'err') failed = ev;
          if (ev.t !== 'err') { bot.draw(ev.t !== 'done'); followEnd(); }
        }
      }
    } catch (e) {
      if (e.name === 'AbortError') m.stopped = true;
      else failed = { code: 'app', message: e.name === 'TypeError' ? 'The app is not answering.' : e.message };
    }
    clearInterval(tick);
    chatS.busy = null;
    log.setAttribute('aria-busy', 'false');
    go.textContent = 'Send';
    go.setAttribute('aria-label', 'Send');
    if (failed) {
      if (failed.code === 'not_set_up' || failed.code === 'no_local_model') {
        bot.el.replaceChildren(missingRows([failed.code === 'not_set_up' ? 'embed' : 'llm'], failed.message));
      } else bot.el.replaceChildren(h('div', { class: 'board' }, row('No answer', null, failed.message, null)));
      if (['app', 'not_set_up', 'no_local_model'].includes(failed.code)) {  // the question was not kept
        chatS.msgs.pop();
        ta.value = chatS.draft = text;
      }
      ta.focus({ preventScroll: true });
      return;
    }
    if (m.text || m.stopped) chatS.msgs.push(m);
    bot.draw(false);
    followEnd();
    ta.focus({ preventScroll: true });
    await reloadChats();
    drawSide();
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
        t.files.length ? [h('p', { text: 'Files:' }), h('ul', { class: 'files' }, t.files.map((f) => h('li', { class: 'mono', text: f })))] : null,
        t.snippet ? [h('p', { text: t.commands ? 'Commands Cortex runs:' : 'The entry Cortex adds:' }), h('pre', { text: t.snippet })]
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
      return post('/api/connect/plan', { want }, (p) => {
        if (!p.rows.length) { toast('Nothing to change.'); return; }
        confirmChanges(p.rows, () => job('/api/connect/apply', { want }));
      });
    };
    const skill = board('Claude Desktop skill', {},
      row('cortex-skill.zip', d.skill_zip_found ? d.skill_zip : null, 'Skills cannot be installed by file. In Claude Desktop, open Settings → Capabilities. First turn on Code execution and file creation on that page, then upload this zip under Skills; /cortex then works in Chat.',
        d.skill_zip_found ? [btn('Open folder', () => post('/api/open', { what: 'skill_zip' }), null, { 'aria-label': 'Open folder: Claude Desktop skill' }),
          copyBtn(() => d.skill_zip, 'Copy failed; select the path and copy it.')] : [ext(d.skill_url, 'Download cortex-skill.zip')]));
    put('connect', board('AI apps', { note: `${found} found · ${on} connected`, btn: ['Apply…', apply] }, ...rows,
      h('div', { class: 'foot', text: d.vault ? 'Apply shows the exact files first. Backups are named <file>.cortex-<date>.bak, next to each file.' : 'Set up a vault first (Setup); each entry points at it.' })), skill,
      d.packs ? packsBoard(d.packs) : null, d.profiles ? profilesBoard(d.profiles, d.scopes) : null);
  }
  // plan v9 item 8: context packs for web chats with no MCP; Copy, else the text is selected
  const PACK_NAMES = { general: 'General', coding: 'Coding', writing: 'Writing' };
  function packsBoard(packs) {
    return board('Context packs', { note: 'for ChatGPT, Gemini and other web chats' }, ...packs.map((p) => {
      const name = PACK_NAMES[p.id] || p.id;
      if (!p.text) return row(name, null, 'Nothing to pack yet: add lines to me/core.md or me/preferences.md.');
      const area = h('textarea', { class: 'mono', readonly: true, rows: 8, value: p.text, 'aria-label': `${name} pack text` });
      const what = h('details', { class: 'what' }, h('summary', { text: 'Show the text' }), area);
      const select = () => { what.open = true; area.focus(); area.select(); toast('Copy is blocked here; the text is selected, press Ctrl+C.'); };
      return h('div', { class: 'brow' },
        h('div', { class: 't' },
          h('div', { class: 'line1' }, h('span', { class: 'ttl', text: name }), h('span', { class: 'val', text: `${p.text.length.toLocaleString()} characters` })),
          h('span', { class: 'sub', text: 'Your pinned core, then your ' + (p.id === 'general' ? 'preferences' : name.toLowerCase() + ' preferences') + '. Paste it at the start of a chat. Private pages are never in it.' }),
          what),
        h('div', { class: 'acts' }, copyBtn(() => p.text, '', null, true, select)));
    }));
  }
  // plan v9 item 8: which scopes each AI app gets over MCP; saved at once, from this page only
  function profilesBoard(profiles, scopes) {
    const data = { profiles: profiles.profiles.map((p) => ({ ...p, scopes: [...p.scopes] })), unknown: [...profiles.unknown] };
    const checks = (list, label, key) => h('div', { class: 'sub' }, scopes.map((s) => {
      const cb = h('input', { type: 'checkbox', id: `pf-${key}-${s}`, checked: list.includes(s), 'aria-label': `${label}: ${s}` });
      cb.addEventListener('change', () => {
        const i = list.indexOf(s);
        if (cb.checked && i < 0) list.push(s);
        if (!cb.checked && i >= 0) list.splice(i, 1);
        post('/api/profiles', data, () => savedMark(cb));
      });
      return h('label', { class: 'check' }, cb, h('span', { text: s }));
    }));
    const prow = (title, sub, list, key) => h('div', { class: 'brow' },
      h('div', { class: 't' }, h('div', { class: 'line1' }, h('span', { class: 'ttl', text: title })),
        h('span', { class: 'sub', text: sub }), checks(list, title, key)));
    return board('Agent profiles', { note: 'what each AI app may read' },
      ...data.profiles.map((p, i) => prow(`Apps named “${p.match}”`, 'The first name that matches the app wins.', p.scopes, i)),
      prow('Any other app', 'Apps no name above matches.', data.unknown, 'other'),
      h('div', { class: 'foot', text: 'all: everything, as before. core: your pinned lines. Each other scope: that section of your preferences. personal: your profile, goals, journal, people and past chats. Private pages are never shared.' }));
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
  // a runner's state in the same words on this page and in Setup (wizard.runner_state)
  const RSTATE = { running: 'running', stopped: 'installed, not running', not_installed: 'not installed' };
  function renderLlm(d) {
    if (!d.available) { put('llm', board('Local model', {}, note('Not available in this version yet.'))); return; }
    const runner = board('Runner', {},
      row('Ollama', d.ollama === null ? RSTATE[d.ollama_state] : `${d.ollama.length} models`, 'Recommended. Cortex uses it whenever it answers on this computer; set it up under Setup → Local model.'),
      row('LM Studio', d.lms ? 'found' : 'not found', d.lms || 'The alternative runner, with its own model library.',
        d.lms ? null : [ext('https://lmstudio.ai/download', 'Get LM Studio')]),
      row('LM Studio server', d.server_port ? `on, port ${d.server_port}` : 'off', 'Starts by itself when a model loads.'),
      row('Loaded now', d.loaded.length ? d.loaded.join(', ') : 'nothing', 'LM Studio unloads a model after 10 idle minutes.'));
    // every chat model already installed, in either runner (embedding models left out by the server)
    const ollama = d.ollama || [];
    const choices = [...ollama.map((n) => ({ model: n, runner: 'ollama' })), ...d.models.map((m) => ({ model: m.key, runner: 'lmstudio' }))];
    const opt = (i, text) => h('option', { value: String(i), text });
    const sel = h('select', { id: 'llm-model', 'aria-label': 'Model' },
      h('option', { value: '', text: choices.length ? 'Choose a model' : 'No models yet' }),
      ollama.length ? h('optgroup', { label: 'Ollama' }, ollama.map((n, i) => opt(i, n))) : null,
      d.models.length ? h('optgroup', { label: 'LM Studio' }, d.models.map((m, i) => opt(ollama.length + i, [m.name, m.params, gb(m.size)].filter(Boolean).join(' · ')))) : null);
    let at = choices.findIndex((c) => c.model === d.model && c.runner === d.runner);
    if (at < 0) at = choices.findIndex((c) => c.model === d.model);
    sel.value = at < 0 ? '' : String(at);
    const chosen = () => (sel.value === '' ? null : choices[Number(sel.value)] || null);
    const loadBtn = btn('Load', () => job('/api/llm/load', { model: chosen().model }));
    const save = () => { const c = chosen(); return post('/api/llm/settings', { model: c.model, runner: c.runner }, () => { toast.ok('Saved.'); load('llm'); }); };
    const saveBtn = btn('Save choice', save), testBtn = btn('Test', () => job('/api/llm/test', {}));
    const sync = () => {  // nothing chosen: nothing to save, load or test
      const c = chosen();
      saveBtn.disabled = testBtn.disabled = !c;
      loadBtn.disabled = !c || c.runner !== 'lmstudio';
    };
    sel.addEventListener('change', sync);
    sync();
    // an optional second model for imports and site descriptions, which run while you are away
    const bgSel = h('select', { id: 'llm-bg', 'aria-label': 'Model for background work' },
      h('option', { value: '', text: 'Same as the model above' }),
      choices.map((c) => h('option', { value: c.model, text: c.model, selected: c.model === d.background_model })));
    if (d.background_model && !choices.some((c) => c.model === d.background_model)) bgSel.append(h('option', { value: d.background_model, text: `${d.background_model} (not found now)`, selected: true }));
    const bgSave = btn('Save', () => post('/api/llm/settings', { background_model: bgSel.value }, () => { toast.ok('Saved.'); load('llm'); }),
      null, { 'aria-label': 'Save: Model for background work' });
    const enabled = h('input', { type: 'checkbox', id: 'llm-on', checked: d.enabled });
    enabled.addEventListener('change', () => post('/api/llm/settings', { enabled: enabled.checked }, () => { toast.ok(enabled.checked ? 'Local model on.' : 'Local model off.'); load('llm'); }));
    const model = board('Model', { note: d.model ? `chosen: ${d.model}` : 'none chosen' },
      d.error ? note(`Not used: ${d.error}`) : null,
      choices.length ? null : note('No models installed yet. Download an Ollama model below, or set one up under Setup → Local model.'),
      h('div', { class: 'field' }, h('label', { for: 'llm-model', text: 'Model (installed in Ollama or LM Studio)' }),
        h('div', { class: 'inline' }, sel, saveBtn, loadBtn, testBtn),
        h('p', { class: 'help' }, 'Load is for LM Studio models: it starts LM Studio and loads the model. Ollama loads its models by itself. Test sends one made-up sentence and shows the answer and time. ',
          ext('https://lmstudio.ai/models', 'Get models'), ' in LM Studio, or download an Ollama model below.')),
      h('div', { class: 'field' }, h('label', { class: 'check' }, enabled, h('span', { text: 'Use the local model' })),
        h('p', { class: 'help', text: 'On: Cortex may start LM Studio and load the chosen model, and the browse import asks it for short summaries of your private pages. Off: the browse import uses rules and the search model only; Chat still uses a model that is already running.' })),
      h('div', { class: 'field' }, h('label', { for: 'llm-bg', text: 'Model for background work (optional)' }),
        h('div', { class: 'inline' }, bgSel, bgSave),
        h('p', { class: 'help', text: 'Browsing imports and site descriptions run while you are away, so a larger, slower model can do them better (for example a 9B model while Chat uses a 2B one). If it does not answer, the model above is used. Two models can sit in memory at once, so with 8 GB of graphics memory or less, leave this as the same model.' })));
    // download another Ollama model by name; the list above reloads when the job ends
    const name = h('input', { type: 'text', id: 'llm-pull', placeholder: 'qwen3.5:4b', spellcheck: 'false', autocomplete: 'off' });
    const pull = () => {
      const v = name.value.trim();
      if (!v) { toast('Type a model name first.'); return null; }
      return post('/api/llm/size', { name: v }, (r) => confirmDialog(`Download ${r.name}?`, 'Download',
        [h('p', { class: 'ink-2', text: `${r.size}, from ${r.source}.` })],
        () => job('/api/llm/pull', { name: r.name })));
    };
    const rec = d.recommend, hw = d.hardware;
    const download = board('Download an Ollama model', {},
      hw && rec ? row('This computer', `${hw.label} → ${rec.label} suits it`, null,
        [btn(`Use ${rec.ollama} (${rec.size})`, () => { name.value = rec.ollama; name.focus(); })]) : null,
      h('div', { class: 'field' }, h('label', { for: 'llm-pull', text: 'Model name' }),
        h('div', { class: 'inline' }, name, btn('Download', pull)),
        h('p', { class: 'help' }, 'A name from ', ext('https://ollama.com/library', 'ollama.com/library'), ', like qwen3.5:4b. Ollama must be running. Cloud models are refused.')));
    put('llm', runner, model, download);
  }

  /* Past history: one list for the Setup step and the Browsing section */
  const BROWSERS = { chrome: 'Chrome', edge: 'Edge', brave: 'Brave', comet: 'Comet', firefox: 'Firefox' };
  const untick = new Set(), seeded = new Set();
  function seedUntick(hist) {  // browse.skip_profiles starts unticked; the user's clicks win after
    for (const s of hist.sources) if (!seeded.has(s.key)) { seeded.add(s.key); if (s.skip) untick.add(s.key); }
  }
  function importTicked(hist, from) {  // only new visits: the import skips the days it has
    seedUntick(hist);
    const all = hist.sources.map((s) => s.key), keys = all.filter((k) => !untick.has(k));
    if (!keys.length) { toast('Tick at least one browser.'); return; }
    return job('/api/browse/import', { sources: keys, skip: all.filter((k) => untick.has(k)), from });
  }
  /* The Browsing section's last import and browse.auto_import_hours (0 = off) */
  // shown while automatic import is on and the receiver does not start at login (Status, Browsing)
  const LOGIN_HINT = 'New history comes in only while Cortex runs. Start the receiver at login to keep it going in the background.';
  const loginBtn = () => btn('Start at login', () => job('/api/browse/login', { on: true }));
  function autoImportRows(hist, s, login) {
    if (!hist || !hist.available || !hist.sources.length) return [];
    const at = hist.imported ? new Date(hist.imported) : null, ok = at && !isNaN(at);
    const hours = h('input', { type: 'number', id: 'b-auto', min: 0, max: 168, step: 1, value: String(s.auto_import_hours ?? 6) });
    hours.addEventListener('change', () => {
      const v = hours.value.trim() === '' ? NaN : Number(hours.value);  // empty is not 0: it never turns import off
      if (!(v >= 0 && v <= 168)) { toast('Enter hours from 0 to 168; 0 turns it off.'); return null; }
      return post('/api/browse/auto', { hours: v }, () => (v ? savedMark(hours) : toast.ok('Saved: automatic import is off.')));
    });
    return [
      h('div', { class: 'brow' }, h('div', { class: 't' }, h('div', { class: 'line1' }, h('span', { class: 'ttl', text: 'Last import' }),
        h('span', { class: 'val', text: ok ? daysAgo(at, true) : 'never', title: ok ? at.toLocaleString() : null }))),
        h('div', { class: 'acts' }, btn('Import new history now', () => importTicked(hist, 'browse')))),
      h('div', { class: 'field' }, h('label', { for: 'b-auto', text: 'Import new history automatically' }),
        h('div', { class: 'inline' }, h('span', { text: 'every' }), hours, h('span', { text: 'hours while Cortex runs (0 = off)' })),
        h('p', { class: 'help', text: 'Browsers keep about 90 days of history; days not imported by then are gone.' })),
      s.auto_import_hours && login && login.supported && !login.exists
        ? h('div', { class: 'brow' }, h('div', { class: 't' }, h('span', { class: 'sub', text: LOGIN_HINT })), h('div', { class: 'acts' }, loginBtn())) : null].filter(Boolean);
  }
  function historyRows(hist, from) {
    if (!hist || !hist.available) return [note('Not available in this version yet.')];
    const intro = h('p', { class: 'pad ink-2', text: 'Cortex can read the history your browsers already keep, on this computer only, and turn it into private day pages. The first time can take a while. If the local model is on, it writes the day summaries; when its time budget runs out, later days get simple rule-made lines.' });
    if (!hist.sources.length) return [intro, note('No browser history found on this computer (looked for Comet, Chrome, Edge, Brave and Firefox).')];
    seedUntick(hist);
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
    return [intro, ...rows, h('p', { class: 'help', text: 'Unticked profiles are also left out of the automatic imports.' }), h('div', { class: 'foot' }, h('div', { class: 'inline' },
      btn('Scan (counts only)', () => job('/api/browse/scan', { from })),
      btn('Import my past history', () => importTicked(hist, from), 'btn lit'),
      h('span', { class: 'sub', text: hist.imported ? `Last import ${hist.imported}.` : 'Never imported.' })))];
  }

  /* Add your history: one card per source; each export source opens a three-step guide */
  const CARDS = ['claude', 'chatgpt', 'gemini', 'perplexity', 'google-activity', 'notes', 'calendar', 'docs', 'repos', 'highlights', 'browser'];
  const CARD = {
    claude: 'Your claude.ai chats.', chatgpt: 'Your ChatGPT chats.', gemini: 'Your Gemini prompts, from Google Takeout.',
    perplexity: 'Perplexity threads, exported as Markdown.',
    'google-activity': 'Searches, YouTube and Chrome activity, from Google Takeout.',
    notes: 'An Obsidian vault, a Notion export or a folder of .md and .txt notes.',
    calendar: 'Your events, from an .ics calendar file.',
    docs: 'A folder of PDFs, Word documents and text files.',
    repos: 'Your own git projects: READMEs, languages and your commit messages, never the code.',
    highlights: 'Kindle clippings, a Readwise export or a Pocket list.',
    browser: ['Browser history', 'The history your browsers keep on this computer.'],
  };
  const MAX_UPLOAD = 4 * 2 ** 30;
  let guide = null;  // {source, step, upload, path, busy, wait, preview, tier, done, error}
  const guideSrc = () => ({ source: guide.source, ...(guide.upload ? { upload: guide.upload } : { path: guide.path }) });
  async function guided(path, wait, extra) {
    Object.assign(guide, { error: null, wait });
    try { await api(path, { ...guideSrc(), ...extra }); poll(); } catch (e) { Object.assign(guide, { wait: null, error: e.message }); }
    load('setup');
  }
  function check() {
    Object.assign(guide, { step: 3, preview: null, done: null });
    return guided('/api/import/preview', 'preview');
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
    const g = d.guides[id], s = guide.step;
    const back = (to) => btn('Back', () => { Object.assign(guide, { step: to, error: null }); load('setup'); });
    const steps = h('div', { class: 'gsteps' }, ['Get your export', 'Add it', 'Check and import'].map((t, i) =>
      h('span', { class: i + 1 === s ? 'on' : i + 1 < s ? 'done' : null, 'aria-current': i + 1 === s ? 'step' : null, text: `${i + 1}. ${t}` })));
    const err = guide.error ? row('That did not work', null, guide.error, null, 'gerr') : null;
    if (s === 1) {
      return [steps, h('ol', { class: 'how' }, g.steps.map((t) => h('li', { text: t }))),
        h('p', { class: 'help' }, g.link ? [ext(g.link, `Open ${new URL(g.link).hostname}`), ' · '] : null, `Typical wait: ${g.wait}.`),
        h('p', { class: 'help', text: `The file to pick: ${g.pick}.` }),
        h('div', { class: 'inline' }, btn('I have the file', () => { guide.step = 2; load('setup'); }, 'btn lit'))];
    }
    if (s === 2) {
      const file = h('input', { type: 'file', id: 'imp-file', accept: '.zip,.json,.html,.md,.txt,.ics,.csv,.pdf,.docx', disabled: !!guide.busy });
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
        return check();  // busy until the check is posted: a second click is ignored
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
        // C2: notes and calendars ask who may read the pages before Import (last choice preselected)
        const tier = p.tier ? h('select', { id: 'imp-tier', 'aria-label': `${g.name}: who may read it` },
          Object.entries(AUDIENCE).map(([v, t]) => h('option', { value: v, text: t }))) : null;
        if (tier) {
          tier.value = guide.tier || p.tier;
          tier.addEventListener('change', () => { guide.tier = tier.value; });
          out.push(row('Who may read these pages', null, null, [tier]));
        }
        out.push(h('div', { class: 'inline' }, btn('Import', () => guided('/api/import/run', 'import', tier ? { tier: tier.value } : {}), 'btn lit', { disabled: !!guide.wait || !p.found }),
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
        : id === 'browser' || id === 'google-activity' ? (hi.imported ? `browsing last imported ${hi.imported.slice(0, 10)}` : 'not yet') : '';
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
    }, () => { toast.ok('Saved.'); load('browse'); });
    const name = c.api_key_env || 'NAME';
    const runs = c.runs.length ? h('div', { class: 'table-scroll' }, h('table', {},
      h('thead', {}, h('tr', {}, ['When', 'Provider', 'Model'].map((t) => h('th', { text: t })),
        ['Requests', 'Characters', 'Skipped for a secret'].map((t) => h('th', { class: 'n', text: t })))),
      h('tbody', {}, c.runs.map((r) => h('tr', {}, h('td', { text: (r.ts || '').replace('T', ' ').replace('Z', ' UTC') }),
        h('td', { text: r.provider }), h('td', { text: r.model }),
        ['items', 'chars', 'skipped'].map((k) => h('td', { class: 'n', text: String(r[k] ?? 0) })))))))
      : note('No cloud runs yet.');
    return [board('Cloud model', { btn: ['Save', save, { 'aria-label': 'Save: Cloud model' }], note: c.cloud_processing ? 'on' : 'off' },
      h('div', { class: 'field' }, h('label', { class: 'check' }, on, h('span', { text: 'Let a cloud model sort and summarise' })),
        h('p', { class: 'help', text: 'Off (the default). On: when Cortex processes browsing, pages of categories set to Cloud AI apps can read (with that switch on) go to this cloud model for short descriptions. Local AI only pages never leave this computer and never fall back to the cloud. A request that looks like it holds a key or token is not sent, and each run sends at most 500 requests unless cloudllm.max_items in the config says otherwise.' })),
      h('div', { class: 'field' }, h('label', { for: 'c-provider', text: 'Provider' }), prov),
      baseField,
      h('div', { class: 'field' }, h('label', { for: 'c-model', text: 'Model' }), model),
      h('div', { class: 'field' }, h('label', { for: 'c-env', text: 'Name of the environment variable that holds your API key' }), env,
        h('p', { class: 'help', text: c.api_key_env ? `${c.api_key_env} is ${c.key_set ? 'set' : 'not set'} for this app.` : 'No variable named yet.' }),
        h('p', { class: 'help', text: `Cortex never asks for the key itself and never stores it. Set the variable yourself as below, then click Quit (top right). ${REOPEN}` }),
        h('ol', { class: 'how' },
          h('li', {}, 'Windows: in PowerShell run ', h('code', { text: `setx ${name} "your-key"` }), ', then open a new terminal.'),
          h('li', {}, 'macOS: add ', h('code', { text: `export ${name}="your-key"` }), ' to ~/.zshrc, then open a new terminal.')))),
    board('Recent cloud runs', { note: 'counts only' }, runs)];
  }
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
      }, () => { toast.ok('Saved.'); load('browse'); });
      parts.push(board('Privacy', { btn: ['Save', save, { 'aria-label': 'Save: Privacy' }] },
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
      d.login.supported
        ? row('Start at login', d.login.exists ? 'on' : 'off', 'The receiver imports new browser history in the background, with no window. This adds or removes its login entry (a Run entry on Windows, a LaunchAgent on macOS, a user service on Linux), only when you click. It runs:',
          [btn(d.login.exists ? 'Remove' : 'Add', () => job('/api/browse/login', { on: !d.login.exists }))])
        : row('Start at login', 'not on this system', 'Windows only: start the receiver from your login items instead.'),
      d.login.command ? h('div', { class: 'pad' }, h('pre', { text: d.login.command })) : null);
    const past = board('Past history', { note: d.history && d.history.imported ? `last import ${d.history.imported}` : 'never imported' },
      ...autoImportRows(d.history, s, d.login), ...historyRows(d.history, 'browse'));
    const acts = board('Actions', { note: `processed ${d.last_processed || 'never'}` },
      row('Process now', null, 'Turns new browser history into your private day pages.', [btn('Process now', () => job('/api/browse/process', {}))]),
      row('Self-test', null, 'Runs the whole import on made-up history in a temp folder; your vault and browsers are not touched.', [btn('Run self-test', () => job('/api/browse/selftest', {}))]),
      row('Private pages', null, 'Your browsing pages are for you only: open them in Obsidian.', [btn('Open private folder in Obsidian', () => post('/api/open', { what: 'private' }, noObsidian))]));
    put('browse', ...parts, ...(d.cloud ? cloudBoard(d.cloud) : []), past, recv, acts);
  }

  /* Dashboard and vault */
  // Export snapshot: the desktop app asks where to save it (then a job writes it); the browser downloads it
  async function exportSnapshot() {
    const r = await post('/api/vault/export', {});
    if (!r || r.cancelled) return;
    if (r.job) { poll(); return; }
    try {
      const res = await fetch(r.download, { headers: { 'X-Cortex-Token': T } });
      if (!res.ok) throw new Error(`The app answered ${res.status}.`);
      const name = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') || '');
      const a = h('a', { href: URL.createObjectURL(await res.blob()), download: name ? name[1] : 'cortex-snapshot.html' });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 60000);
      toast.ok('Snapshot saved to your downloads.');
    } catch (e) { toast.error(e.message); }
  }
  const noObsidian = (r) => { if (r.obsidian === false) toast('Obsidian is not installed, so it opened with this computer’s own app.', true); };
  function renderVault(d) {
    if (!d.ok) {
      put('vault', board('Vault', {}, row('No vault yet', d.path || '', 'Set one up first.', [btn('Open Setup', () => show('setup', true))])));
      return;
    }
    put('vault', board('Vault', {},
      row('Folder', d.path, 'Your notes live here as Markdown files.', [btn('Open vault in Obsidian', () => post('/api/open', { what: 'vault' }, noObsidian)),
        btn('Open folder', () => post('/api/open', { what: 'folder' }), null, { 'aria-label': 'Open folder: Vault' })]),
      h('div', { class: 'brow' }, h('div', { class: 't' }, h('div', { class: 'line1' }, h('span', { class: 'ttl', text: 'Notes' })),
        h('span', { class: 'sub', text: `Last indexed ${d.last_index || 'not yet'}.` })), h('div', { class: 'acts' }, dm(String(d.notes), 3, `${d.notes} notes`))),
      row('Snapshot', null, 'One page of your notes and the 3D brain that opens without Cortex. Private items are never in it.',
        [btn('Export snapshot', exportSnapshot)]),
      row('Backups', d.backups ? `${d.backups}, last ${d.last_backup}` : 'none yet', 'A zip of the whole vault in .cortex/backups. Copy it to another drive now and then.',
        [btn('Back up vault', () => job('/api/vault/backup', {}))]),
      row('Health', null, 'Checks Python, the vault folder, git and a few common problems.', [btn('Run doctor', () => job('/api/vault/doctor', {}))])),
      gitBoard(d.git || { enabled: true, private_browsing: false }));
  }
  function gitBoard(g) {
    const on = h('input', { type: 'checkbox', id: 'g-on', checked: g.enabled });
    const priv = h('input', { type: 'checkbox', id: 'g-private', checked: g.private_browsing, disabled: !g.enabled });
    on.addEventListener('change', () => { priv.disabled = !on.checked; });
    const save = () => post('/api/vault/git', { enabled: on.checked, private_browsing: priv.checked }, () => { toast.ok('Saved.'); load('vault'); });
    return board('History', { btn: ['Save', save, { 'aria-label': 'Save: History' }] },
      h('div', { class: 'field' }, h('label', { class: 'check' }, on, h('span', { text: 'Keep the vault’s history in git (lets you undo sessions; uses more disk as the vault grows)' })),
        h('p', { class: 'help', text: 'On (the default): each session is saved as a git commit you can undo. Off: no new history is kept and undo is off; history already there is left as it is.' })),
      h('div', { class: 'field' }, h('label', { class: 'check' }, priv, h('span', { text: 'Include private browsing pages in git history' })),
        h('p', { class: 'help', text: 'Off (the default): private browsing pages stay out of git. On: they are saved in the history with everything else, so undo covers them and deleting one later leaves a copy in old commits.' })));
  }

  /* Review (plans 5a, 5b): what waits for the user's own click. One quiet count in the nav, no
     popups. Every text is the server's shown text, set with textContent. */
  function badge(n) {
    const k = Number(n) > 0 ? Number(n) : 0, b = $('review-badge'), o = $('review-option');
    b.textContent = k ? String(k) : '';
    b.hidden = !k;
    b.setAttribute('aria-label', `${k} waiting`);
    if (o) o.textContent = k ? `Review (${k})` : 'Review';
  }
  const PAGE = { 'teaching-contract': 'Teaching rule', preferences: 'Preferences', personality: 'Personality',
    goals: 'Goals', profile: 'Profile', voice: 'Voice', portrait: 'Portrait', core: 'Core' };
  function renderReview(d) {
    badge(d.count);
    const act = (path, body, said) => post(path, body, (r) => { renderReview(r); toast.ok(said); });
    const where = (x) => (PAGE[x.page] || x.page) + (x.section ? ` · ${x.section}` : '');
    const list = (rows, empty) => (rows.length ? rows : [note(empty)]);
    put('review',
      board('Waiting for your approval', { note: String(d.pending.length) }, ...list(d.pending.map((p) =>
        row(p.text, where(p), p.since ? `Suggested ${p.since}` : null, [
          btn('Approve', () => act('/api/review/approve', { key: p.key }, 'Approved.'), 'btn lit', { 'aria-label': `Approve: ${p.text}` }),
          btn('Dismiss', () => act('/api/review/dismiss', { key: p.key }, 'Dismissed. It will not be suggested again.'), null, { 'aria-label': `Dismiss: ${p.text}` })])),
      'Nothing is waiting.')),
      board('Past their end date', { note: String(d.expired.length) }, ...list(d.expired.map((x) =>
        row(x.text, where(x), `Ended ${x.until}`, [
          btn('Retire', () => act('/api/review/retire', { id: x.id }, 'Retired.'), null, { 'aria-label': `Retire: ${x.text}` })])),
      'No line is past its end date.')),
      board('Still true?', { note: String(d.stale.length) }, ...list(d.stale.map((x) =>
        row(x.text, where(x), `Last confirmed ${x.since}`, [
          btn('Still true', () => act('/api/review/still', { id: x.id }, 'Kept.'), 'btn lit', { 'aria-label': `Still true: ${x.text}` }),
          btn('Retire', () => act('/api/review/retire', { id: x.id }, 'Retired.'), null, { 'aria-label': `Retire: ${x.text}` })])),
      'Nothing to check this month.')),
      board('Lines that may conflict', { note: String((d.conflicts || []).length) }, ...list((d.conflicts || []).map((c) =>
        row(c.left.text, where(c.left), `or: ${c.right.text} (${where(c.right)})`, [
          btn('Keep both', () => act('/api/review/conflict', { key: c.key, keep: 'both' }, 'Kept both.'), null, { 'aria-label': `Keep both: ${c.left.text} and ${c.right.text}` }),
          btn('Keep left', () => act('/api/review/conflict', { key: c.key, keep: 'left' }, 'Kept one; the other is retired.'), null, { 'aria-label': `Keep only: ${c.left.text}` }),
          btn('Keep right', () => act('/api/review/conflict', { key: c.key, keep: 'right' }, 'Kept one; the other is retired.'), null, { 'aria-label': `Keep only: ${c.right.text}` })])),
      'No lines seem to conflict.')),
      board('Corrections you repeat', { note: String((d.rules || []).length) }, ...list((d.rules || []).map((x) =>
        row(`You told ${x.chats} chats: ${x.text}`, 'Core', null, [
          btn('Approve', () => act('/api/review/rule/approve', { key: x.key }, 'Added to your core lines.'), 'btn lit', { 'aria-label': `Approve: ${x.text}` }),
          btn('Dismiss', () => act('/api/review/rule/dismiss', { key: x.key }, 'Dismissed. It will not be suggested again.'), null, { 'aria-label': `Dismiss: ${x.text}` })])),
      'No repeated corrections found.')));
  }

  /* Agent X-ray (v9 item 6): what cortex_prime and cortex_context served each AI app. Ids come
     back resolved to titles by the server; a private page is only "a private page". Pin: prime
     always serves it; Mute: prime and context never do. Every text is set with textContent. */
  const BLOCK = { core: 'Core lines', profile: 'Profile', preferences: 'Lines about you', contract: 'Teaching contract',
    related: 'Related pages', mistakes: 'Open mistakes', hub: 'Topic', topic: 'Topic concepts', claims: 'Lines about you' };
  const xrayOpen = new Set();  // the calls left open survive a redraw after a click
  function renderXray(d) {
    const act = (path, body, said) => post(path, body, (r) => { renderXray(r); toast.ok(said); });
    const ref = (x) => (x.id ? { id: x.id } : { token: x.token });
    const pin = (x) => btn(x.pinned ? 'Unpin' : 'Pin', () => act('/api/xray/pin', { ...ref(x), on: !x.pinned },
      x.pinned ? 'Unpinned.' : 'Pinned. Cortex now serves it in every first call.'), null, { 'aria-label': `${x.pinned ? 'Unpin' : 'Pin'}: ${x.title}` });
    const mute = (x) => btn(x.muted ? 'Unmute' : 'Mute', () => act('/api/xray/mute', { ...ref(x), on: !x.muted },
      x.muted ? 'Unmuted.' : 'Muted. Cortex will not serve it again.'), null, { 'aria-label': `${x.muted ? 'Unmute' : 'Mute'}: ${x.title}` });
    const item = (x, block, extra) => row(x.title, BLOCK[block] || block, x.where || null,
      x.id && x.kind !== 'me' ? [pin(x), mute(x)] : [], extra);
    const call = (c, client) => {
      const key = `${client}|${c.ts}|${c.tool}`, when = c.ts ? daysAgo(c.ts, true) : '';
      const n = (bs) => bs.reduce((k, b) => k + b.items.length, 0);
      const det = h('details', { class: 'pad xray-call', open: xrayOpen.has(key) },
        h('summary', { text: `${c.tool} · ${when} · ${c.chars} characters · ${n(c.served)} served${c.trimmed.length ? `, ${n(c.trimmed)} cut for space` : ''}` }),
        ...c.served.flatMap((b) => b.items.map((x) => item(x, b.block))),
        ...c.trimmed.flatMap((b) => b.items.map((x) => item({ ...x, where: 'cut for space' }, b.block, 'cut'))),
        c.served.length || c.trimmed.length ? null : note('Nothing was served.'));
      det.addEventListener('toggle', () => { if (det.open) xrayOpen.add(key); else xrayOpen.delete(key); });
      return det;
    };
    const flags = (list, which) => list.map((x) => row(x.title, x.kind === 'claim' ? 'Line about you' : 'Page', x.where || null,
      [which === 'pin' ? pin(x) : mute(x)]));
    put('xray',
      board('Pinned and muted', { note: String(d.pinned.length + d.muted.length) },
        ...(d.pinned.length || d.muted.length ? [...flags(d.pinned, 'pin'), ...flags(d.muted, 'mute')]
          : [note('Nothing pinned or muted. Pin or mute a page or line from a call below.')])),
      ...(d.clients.length ? d.clients.map((g) => board(g.client, { note: `${g.total} call${g.total === 1 ? '' : 's'}` },
        ...g.calls.map((c) => call(c, g.client))))
        : [board('No calls yet', {}, note('When an AI app asks Cortex about you, each call shows here.'))]));
  }

  /* Setup */
  const skipped = new Set(), opened = new Set();
  const pick = { model: null, runner: null };
  // the server keeps a skipped Connect (the app then lands on Status); opening it un-skips
  function skip(id, on) {
    const was = skipped.has(id);
    if (on) skipped.add(id); else skipped.delete(id);
    if (id === 'connect' && was !== on) return post('/api/setup/skip', { skipped: on }, () => load('setup'));
    return load('setup');
  }
  function step(id, title, done, summary, body, first) {
    const cur = id === first;
    const collapsed = !cur && !opened.has(id);  // one step at a time: the current one, plus any the user opened
    const band = h('div', { class: 'band' }, h('h2', { text: title }),
      h('span', { class: 'band-note' }, done ? h('span', { class: 'done-mark', text: '✓ ' }) : null,
        done ? summary || 'Done' : skipped.has(id) ? 'Skipped' : cur ? dm('NEXT', 2, 'Next') : 'Later'),
      collapsed ? btn(done ? 'Edit' : 'Open', () => { opened.add(id); return id === 'connect' ? skip(id, false) : load('setup'); }, 'band-btn',
        { 'aria-label': `${done ? 'Edit' : 'Open'}: ${title}` })
        : !done && id !== 'finish' && !skipped.has(id) ? btn('Skip', () => { opened.delete(id); return skip(id, true); }, 'band-btn', { 'aria-label': `Skip: ${title}` })
          : done || skipped.has(id) ? btn('Hide', () => { opened.delete(id); load('setup'); }, 'band-btn', { 'aria-label': `Hide: ${title}` }) : null);
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

    // moved: the config names a folder that is gone; ask, never prefill (that would make a new, empty vault)
    const moved = d.vault.moved, gi = d.git;
    const path = h('input', { type: 'text', id: 'vault-path', value: moved ? '' : d.vault.path || d.vault.default, placeholder: moved ? 'The folder’s new location' : null, 'aria-label': 'Vault folder', spellcheck: 'false' });
    const sVault = step('vault', 'Vault', d.vault.done, d.vault.path, [
      moved ? row('Did you move your vault?', null, `Cortex used ${d.vault.path}, which is not there any more. Paste the folder’s new location below, or a new folder to start an empty vault.`, null) : null,
      h('div', { class: 'field' }, h('label', { for: 'vault-path', text: 'The folder for your notes' }),
        h('div', { class: 'inline' }, path, btn('Use this folder', () => job('/api/setup/vault', { path: path.value }), 'btn lit')),
        h('p', { class: 'help', text: 'Cortex adds its own folders and a git history there, and never changes notes you already have. Obsidian can open it as a vault.' })),
      gi && !gi.found && gi.enabled !== false ? row('Git (history and undo)', 'not installed', 'Without git, Cortex keeps no history of your vault, so undo does nothing. Free.',
        gi.install.run ? [btn(`Install Git (${gi.install.size}, via ${gi.install.source})`, () => job('/api/setup/git', {})), ext(gi.install.license, 'License')] : [ext(gi.install.page, 'Get Git')]) : null], first);

    const e = d.embed;
    const sEmbed = step('embed', 'Search model', e.done, 'downloaded', [
      row('Find notes by meaning', e.done ? 'downloaded' : 'not downloaded',
        'Without it, search matches words only. It downloads once, works offline, then indexes your notes.',
        [e.done ? null : btn(`Download search model (${e.size}, from ${e.source})`, () => job('/api/setup/embed', {}), 'btn lit')]),
      row('Index my notes', e.indexed ? `indexed ${e.indexed}` : 'not yet', 'Reads every note once so search by meaning can find it. Safe to stop and run again.',
        [btn('Index my notes', () => job('/api/setup/index', {}), 'btn', { disabled: !(e.done && d.vault.done) })])], first);

    const inst = rn.install;
    const start = (id, name) => btn('Start', () => job('/api/setup/start', { runner: id }), 'btn lit', { 'aria-label': `Start: ${name}` });
    const oll = row('Ollama', RSTATE[rn.ollama.state], 'Runs AI models on this computer. Free.',
      rn.ollama.state === 'stopped' ? [start('ollama', 'Ollama')] : rn.ollama.found ? null
        : inst.run ? [btn(`Install Ollama (${inst.size}, via ${inst.source})`, () => job('/api/setup/install', {}), 'btn lit'), inst.license ? ext(inst.license, 'License') : null] : [ext(inst.page, 'Get Ollama')]);
    oll.querySelector('.line1').append(h('span', { class: 'rec', text: 'RECOMMENDED' }));
    const runnerRows = [
      oll,
      !rn.ollama.found ? h('div', { class: 'pad' }, h('span', { class: 'sub', text: inst.run ? 'The button runs:' : 'Run this yourself:' }),
        h('pre', { text: inst.run ? inst.run.join(' ') : inst.show || inst.page })) : null,
      row('LM Studio', RSTATE[rn.lmstudio.state], 'An app with its own model library; Cortex uses its lms command.',
        rn.lmstudio.state === 'stopped' ? [start('lmstudio', 'LM Studio')] : rn.lmstudio.found ? null : [ext(rn.lmstudio.page, 'I’ll use LM Studio')]),
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
      return post('/api/connect/plan', { want }, (p) => {
        if (!p.rows.length) { toast('Nothing to change.'); return; }
        confirmChanges(p.rows, () => job('/api/connect/apply', { want }));
      });
    };
    const sConnect = step('connect', 'Connect AI apps', done.connect, names(on), [
      row(todo.length ? `Found: ${names(todo)}` : 'No other AI app found', null,
        'This is what lets Claude and your other AI apps know you. Cortex adds itself to each app’s settings; you see the exact files first, and each one is backed up.',
        todo.length ? [btn(`Connect ${names(todo)}`, connectFound, 'btn lit', { disabled: !d.vault.done })]
          : on.length ? null : [ext('https://claude.ai/download', 'Get Claude Desktop')]),
      row('Choose apps one by one', null, 'Add or remove single apps, and see what each change does.', [btn('Open Connect AI apps', () => show('connect', true))])], first);

    const sHistory = step('history', 'Add your history (optional)', done.history, 'added',
      d.vault.done ? historyBoard(d) : [note('Set up the vault first.')], first);

    const sFinish = step('finish', 'Try it', false, '', [
      h('div', { class: 'pad' }, h('p', { class: 'ink-2', text: on.length ? `Restart ${names(on)} first, so it loads Cortex.${on.some((t) => t.id === 'claude-desktop') ? ' ' + RESTART_DESKTOP : ''} Then try these in Claude:` : 'Connect an AI app first (above) and restart it. Then try these in Claude:' }),
        h('ol', { class: 'how' }, d.first_prompts.map((x) => h('li', {}, h('code', { text: x })))),
        h('p', { class: 'help', text: 'This step never gets a check mark: Cortex cannot see your chats in Claude. When Claude answers one of these using Cortex, you are set.' })),
      row('Chat', null, 'Or ask about your notes here, with a model on this computer.', [btn('Open Chat', () => show('chat', true))]),
      row('Browsing', null, 'Bring in your browser history, privately.', [btn('Open', () => show('browse', true))])], first);
    const steps = { vault: sVault, embed: sEmbed, connect: sConnect, history: sHistory, llm: sLlm };
    put('setup', ...d.order.map((k) => steps[k]), sFinish);
  }

  const RENDER = { setup: renderSetup, chat: renderChat, status: renderStatus, review: renderReview, xray: renderXray, connect: renderConnect, llm: renderLlm, browse: renderBrowse, vault: renderVault };

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
  $('job-cancel').addEventListener('click', pressable($('job-cancel'), () => post('/api/job/cancel', {})));
  $('toast-x').append(icon('x'));
  $('toast-x').addEventListener('click', toastHide);
  for (const [t, fn] of [['mouseenter', toastHold], ['mouseleave', toastRelease], ['focusin', toastHold], ['focusout', toastRelease]]) $('toast').addEventListener(t, fn);
  document.querySelectorAll('.side button').forEach((b) => b.addEventListener('click', () => show(b.dataset.s, true)));
  $('section-select').addEventListener('change', (e) => (e.target.value === 'home' ? location.assign('/') : show(e.target.value, true)));
  window.addEventListener('popstate', () => { const s = location.hash.slice(1); if (s !== current) show(s, true); });  // Back switches sections

  // a section at once: the link's, else the last one this tab used, else Status (its first
  // answer may move to the app's choice); each shows Loading… until its answer comes
  const want = location.hash.slice(1);
  let lastSection = null;
  try { lastSection = sessionStorage.getItem('cortex.section'); } catch (e) { /* storage off */ }
  if (SECTIONS.includes(want)) show(want);
  else if (SECTIONS.includes(lastSection)) show(lastSection);
  else { landing = true; show('status'); }
  idle();
  poll(true);
  if (current !== 'review') api('/api/review').then((d) => badge(d.count), () => {});  // the nav's count; no vault yet: none
  // the app starts the automatic history import by itself: the job line shows it within 15 s
  setInterval(() => { if (!dead && !document.hidden) poll(true); }, 15000);
})();
