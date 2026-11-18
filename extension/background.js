// Service worker: times the page on screen (tab focus, window focus, idle), reads a short
// excerpt after 20 s via content.js, queues one record per visit in chrome.storage.local and
// flushes the queue every minute to the Cortex receiver on 127.0.0.1. Nothing goes anywhere
// else. A session ends when the last window closes, after 15 min idle or locked, or (for a run
// that ended without it) at the next browser start: the queue then goes with session_end, and
// the receiver processes it. It never logs URLs or titles. Unpaired, or after the receiver
// refused the token, it times, reads and queues nothing; a queued visit older than 24 h is
// dropped unsent.
import { awaySince, bankTitle, batchBody, dayKey, denyReason, scrub, sessionEndDue }
  from "./lib.js";

const READ_AFTER_MS = 20000;
const QUEUE_CAP = 2000;
const BATCH_MAX = 500;
const BATCH_BYTES = 200 * 1024; // under the receiver's 256 KB body cap
const MAX_BACKOFF_MIN = 60;
const QUEUE_MAX_AGE_MS = 24 * 3600 * 1000;
const DEFAULTS = { paused: false, deny: [], port: 8799, token: "", status: "",
  serverDeny: [] };

// The visit on screen: {tabId, url, title, start, activeMs, since, excerpt, channel, read}.
// Mirrored in chrome.storage.session (memory only) because the worker can be stopped.
let cur = null;
const ready = chrome.storage.session.get("cur").then((r) => { cur = r.cur || null; });

// Every handler runs one at a time, so storage reads and writes never interleave.
let chain = Promise.resolve();
function serial(fn) {
  chain = chain.then(() => ready).then(fn).catch(() => console.warn("cortex: a step failed"));
  return chain;
}

const settings = () => chrome.storage.local.get(DEFAULTS);
const save = () => chrome.storage.session.set({ cur });
// Why nothing may be timed, read or queued for `url` now (paused, unpaired, refused, denied).
const blocked = (s, url) => s.paused || !s.token || s.status === "unauthorized" ||
  !!denyReason(url, [...s.deny, ...s.serverDeny]);
const fresh = (queue) => queue.filter((r) => Date.now() - Number(r.start) < QUEUE_MAX_AGE_MS);

async function onScreen() {
  const [state, win] = await Promise.all([
    chrome.idle.queryState(60), chrome.windows.getLastFocused().catch(() => null)]);
  return state === "active" && !!win && win.focused;
}

function stopClock(now = Date.now()) {
  if (cur && cur.since) {
    cur.activeMs += now - cur.since;
    cur.since = null;
  }
}

function activeSoFar() {
  return cur ? cur.activeMs + (cur.since ? Date.now() - cur.since : 0) : 0;
}

function scheduleRead() {
  if (!cur || cur.read || !cur.since) return;
  setTimeout(() => serial(readPage), Math.max(0, READ_AFTER_MS - activeSoFar()) + 100);
}

async function readPage() {
  if (!cur || cur.read || activeSoFar() < READ_AFTER_MS) return;
  const s = await settings();
  if (blocked(s, cur.url)) return;
  const tab = await chrome.tabs.get(cur.tabId).catch(() => null);
  if (!tab || tab.url !== cur.url || tab.incognito) return;
  cur.read = true;
  await save();
  const [res] = await chrome.scripting.executeScript({
    target: { tabId: cur.tabId }, files: ["content.js"] }).catch(() => []);
  const out = res && res.result;
  if (!cur || !out || out.href !== cur.url) return; // the page changed meanwhile
  cur.excerpt = String(out.excerpt || "").slice(0, 2000);
  cur.channel = String(out.channel || "").slice(0, 200);
  if (out.title) cur.title = String(out.title);
  await save();
}

// Close the current visit and queue its record (unless paused or denied by now).
async function finish() {
  if (!cur) return;
  stopClock();
  const v = cur;
  cur = null;
  await save();
  const s = await settings();
  if (blocked(s, v.url) || bankTitle(v.title)) return;
  const sc = scrub(v.url);
  const title = sc.kind === "search" ? sc.term : v.title.replace(/\s+-\s+YouTube$/, "");
  const rec = { url: sc.url, title: title.slice(0, 500), start: v.start,
    active_ms: Math.round(v.activeMs), excerpt: sc.kind === "search" ? "" : v.excerpt,
    kind: sc.kind };
  if (sc.kind === "video" && v.channel) rec.channel = v.channel;
  const { queue = [] } = await chrome.storage.local.get("queue");
  const kept = fresh(queue);
  kept.push(rec);
  // on disk at once (oldest dropped first): the worker can stop when the last window closes
  await chrome.storage.local.set({ queue: kept.slice(-QUEUE_CAP), pendingEnd: true });
}

// The tab now on screen: continue its visit if it is the same page, else start a new one.
async function show(tab) {
  if (cur && tab && tab.id === cur.tabId && tab.url === cur.url) {
    if (!cur.since && (await onScreen())) cur.since = Date.now();
    await save();
    scheduleRead();
    return;
  }
  await finish();
  if (!tab || tab.incognito || !tab.url) return;
  const s = await settings();
  if (blocked(s, tab.url)) return;
  const now = Date.now();
  cur = { tabId: tab.id, url: tab.url, title: tab.title || "", start: now, activeMs: 0,
    since: (await onScreen()) ? now : null, excerpt: "", channel: "", read: false };
  await save();
  scheduleRead();
}

async function focusedTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab || null;
}

chrome.tabs.onActivated.addListener(({ tabId }) =>
  serial(async () => show(await chrome.tabs.get(tabId))));

chrome.tabs.onUpdated.addListener((tabId, info, tab) => serial(async () => {
  if (cur && cur.tabId === tabId && info.title && tab.url === cur.url) {
    cur.title = info.title;
    await save();
  }
  // navigated away: the visit is over even when its window is not focused
  if (cur && cur.tabId === tabId && info.url && info.url !== cur.url) await finish();
  if (!info.url || !tab.active) return;
  const win = await chrome.windows.getLastFocused().catch(() => null);
  if (win && tab.windowId === win.id) await show(tab);
}));

chrome.tabs.onRemoved.addListener((tabId) => serial(async () => {
  if (cur && cur.tabId === tabId) await finish();
}));

chrome.windows.onFocusChanged.addListener((winId) => serial(async () => {
  if (winId === chrome.windows.WINDOW_ID_NONE) {
    stopClock();
    await save();
    return;
  }
  const [tab] = await chrome.tabs.query({ active: true, windowId: winId });
  await show(tab || null);
}));

chrome.idle.setDetectionInterval(60);
chrome.idle.onStateChanged.addListener((state) => serial(async () => {
  const { away } = await chrome.storage.local.get({ away: 0 });
  await chrome.storage.local.set({ away: awaySince(away, state, Date.now()) });
  if (state === "active") {
    await show(await focusedTab());
  } else {
    stopClock();
    await save();
  }
}));

// Pause, a new deny entry, a refused or removed token drop the visit on screen, unsent.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !(changes.paused || changes.deny || changes.serverDeny ||
    changes.token || changes.status)) return;
  serial(async () => {
    const s = await settings();
    if (cur && blocked(s, cur.url)) {
      cur = null;
      await save();
    }
  });
});

// ---- flushing to the receiver ----

function takeBatch(queue) {
  const enc = new TextEncoder();
  let n = 0;
  let size = 2;
  while (n < queue.length && n < BATCH_MAX) {
    const len = enc.encode(JSON.stringify(queue[n])).length + 1;
    if (n && size + len > BATCH_BYTES) break;
    size += len;
    n += 1;
  }
  return n;
}

// The receiver's deny_domains (~/.cortex/config.yaml), so they are never read here either.
// The private list stays with the receiver, which clears a private record's text itself. A refused token stops all capture until the popup saves a new one.
async function syncLists() {
  const s = await settings();
  if (!s.token) return;
  try {
    const r = await fetch(`http://127.0.0.1:${Number(s.port)}/v1/ping`, {
      cache: "no-store", credentials: "omit", redirect: "error",
      headers: { Authorization: `Bearer ${s.token}` }, signal: AbortSignal.timeout(3000) });
    if (r.status === 401) {
      await chrome.storage.local.set({ status: "unauthorized" });
      return;
    }
    if (!r.ok) return;
    const b = await r.json();
    const list = (v) => (Array.isArray(v) ? v.filter((d) => typeof d === "string") : []);
    await chrome.storage.local.set({ serverDeny: list(b.deny_domains) });
    await chrome.storage.local.remove("serverPrivate"); // stored by older versions
  } catch { /* receiver down: keep the last lists */ }
}

// Send the queue. With `end` (or a session end still owed from a failed try) the last batch,
// empty if need be, carries session_end: true.
async function flush(end = false) {
  const s = await settings();
  const st = await chrome.storage.local.get({ queue: [], backoff: 0, nextTry: 0,
    sent: { day: "", n: 0 }, endOwed: false });
  end = end || st.endOwed;
  if (end && !st.endOwed) await chrome.storage.local.set({ endOwed: true });
  const live = fresh(st.queue);
  if (live.length !== st.queue.length) await chrome.storage.local.set({ queue: live });
  if (!s.token || s.status === "unauthorized" || !(live.length || end) ||
    Date.now() < st.nextTry) return;
  let { sent } = st;
  let queue = live;
  let status = "ok";
  let ended = false;
  try {
    do {
      const n = takeBatch(queue);
      const last = n === queue.length;
      const r = await fetch(`http://127.0.0.1:${Number(s.port)}/v1/batch`, {
        method: "POST", cache: "no-store", credentials: "omit", redirect: "error",
        headers: { Authorization: `Bearer ${s.token}`, "Content-Type": "application/json" },
        body: JSON.stringify(batchBody(queue.slice(0, n), end && last)),
        signal: AbortSignal.timeout(10000) });
      if (r.status === 401) { status = "unauthorized"; break; } // capture stops; queue expires
      if (r.status >= 500) throw new Error("receiver error");
      queue = queue.slice(n); // a 400 or 413 batch would fail forever, so it is dropped too
      if (!r.ok) { status = "rejected"; continue; }
      ended = end && last;
      const day = dayKey(Date.now());
      sent = sent.day === day ? sent : { day, n: 0 };
      sent.n += n;
    } while (queue.length);
    await chrome.storage.local.set({ queue, sent, backoff: 0, nextTry: 0, status,
      ...(ended ? { endOwed: false, pendingEnd: false } : {}) });
  } catch {
    const backoff = Math.min(Math.max(1, st.backoff * 2), MAX_BACKOFF_MIN); // minutes
    await chrome.storage.local.set({ queue, sent, backoff, status: "down",
      nextTry: Date.now() + backoff * 60000 });
  }
}

// The session ended: close the visit on screen and send everything as an ended session.
async function endSession() {
  await finish();
  await flush(true);
}

async function checkAway() {
  const { away, pendingEnd } = await chrome.storage.local.get({ away: 0, pendingEnd: false });
  if (sessionEndDue(away, pendingEnd || !!cur, Date.now())) await endSession();
}

// The last window closed: best effort, as the worker may be stopped any moment now.
chrome.windows.onRemoved.addListener(() => serial(async () => {
  const left = await chrome.windows.getAll().catch(() => []);
  if (!left.length) await endSession();
}));

// The last run ended without its session end (Comet killed, the worker stopped first).
chrome.runtime.onStartup.addListener(() => serial(async () => {
  const st = await chrome.storage.local.get({ queue: [], pendingEnd: false, endOwed: false });
  if (!(st.pendingEnd || st.endOwed || st.queue.length)) return;
  await chrome.storage.local.set({ backoff: 0, nextTry: 0, away: 0 });
  await flush(true);
}));

chrome.alarms.get("tick").then((a) => a || chrome.alarms.create("tick", { periodInMinutes: 1 }));
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name !== "tick") return;
  serial(syncLists);
  serial(readPage);
  serial(checkAway);
  serial(() => flush());
});

// The popup's "Save and test" asks for a flush now, past any backoff; "Process now" also
// ends the session, so the receiver processes what it has.
chrome.runtime.onMessage.addListener((msg) => {
  if (msg === "flush" || msg === "process") {
    serial(async () => {
      await chrome.storage.local.set({ backoff: 0, nextTry: 0 });
      await syncLists();
      await flush(msg === "process");
    });
  }
});
