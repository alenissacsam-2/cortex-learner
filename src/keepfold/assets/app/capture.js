/* Quick capture's box (the desktop app's hotkey): one line, Enter saves it through
   POST /api/capture, Esc or the box losing focus hides it (the text stays for the next open).
   Text goes in with textContent only; the key arrives once in the URL, as on the app page. */
(() => {
  'use strict';
  const T = new URLSearchParams(location.search).get('t') || '';
  history.replaceState(null, '', '/capture');
  const input = document.getElementById('cap'), hint = document.getElementById('cap-hint');
  const SHOW_MS = 1200;  // a saved line's result, then the box hides and clears
  const TAG = /(?<![\w:/])\/\/(pref|fact|forget):/i;  // tags.TAG's start
  const WHAT = {
    pref: 'Enter saves it as your preference (//pref)',
    fact: 'Enter saves it about you (//fact)',
    forget: 'Enter forgets it about you (//forget)',
  };
  let busy = false, shown = null;  // shown: a result on the hint line until the box hides

  function theme() {  // one key with the app page and Home
    let t = null;
    try { t = localStorage.getItem('keepfold-theme') || localStorage.getItem('keepfold-app-theme'); } catch (e) { /* storage off */ }
    if (t === 'day' || t === 'night') document.documentElement.dataset.theme = t;
    else delete document.documentElement.dataset.theme;
  }
  // what Enter will do with the line as typed now (the server's own three kinds)
  function what(text) {
    const m = TAG.exec(text);
    if (m) return WHAT[m[1].toLowerCase()];
    if (/^https?:\/\/\S+$/i.test(text.trim())) return 'Enter saves this link';
    return 'Enter adds it to today’s journal';
  }
  function say(text, kind) {
    hint.textContent = text;
    hint.className = 'cap-hint' + (kind ? ' ' + kind : '');
  }
  function update() {
    if (shown === null) say(`${what(input.value)} · Esc closes`);
  }
  async function send(path, body) {
    const r = await fetch(path, {
      method: 'POST', headers: { 'X-Keepfold-Token': T, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    let data = {};
    try { data = await r.json(); } catch (e) { /* no body */ }
    if (r.status === 401) throw new Error('This box lost its key. Quit Keepfold and start it again.');
    if (!r.ok) throw new Error(data.error || `The app answered ${r.status}.`);
    return data;
  }
  function hide() { send('/api/capture/hide', {}).catch(() => { /* the app is gone */ }); }

  async function save() {
    if (busy || !input.value.trim()) return;
    busy = true;
    try {
      const r = await send('/api/capture', { text: input.value });
      shown = r.said;
      say(r.said, 'ok');
      setTimeout(() => {
        input.value = '';
        shown = null;
        update();
        hide();
      }, SHOW_MS);
    } catch (e) {  // a refusal stays, in the error colour; the text stays in the input
      shown = e.message;
      say(e.message, 'bad');
    } finally {
      busy = false;
    }
  }

  input.addEventListener('input', () => { if (!busy) { shown = null; update(); } });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); save(); }
    if (e.key === 'Escape') { e.preventDefault(); hide(); }
  });
  window.addEventListener('blur', hide);
  const opened = () => {
    if (document.hidden) return;
    theme();
    if (!busy && hint.className.includes('bad')) shown = null;  // a new open starts fresh
    update();
    input.focus();
  };
  window.addEventListener('focus', opened);
  document.addEventListener('visibilitychange', opened);
  window.addEventListener('storage', theme);
  theme();
  update();
  input.focus();
})();
