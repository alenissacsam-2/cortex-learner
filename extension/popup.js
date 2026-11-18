import { dayKey } from "./lib.js";

const $ = (id) => document.getElementById(id);
const DEFAULTS = { paused: false, deny: [], port: 8799, token: "", queue: [],
  sent: { day: "", n: 0 } };

async function ping(port, token) {
  try {
    const r = await fetch(`http://127.0.0.1:${port}/v1/ping`, {
      headers: { Authorization: `Bearer ${token}` }, cache: "no-store", credentials: "omit",
      redirect: "error", signal: AbortSignal.timeout(3000) });
    if (r.ok) return [true, "Receiver connected"];
    if (r.status === 401) return [false, "Token not accepted: run cortex browse pair again"];
    return [false, `Receiver answered ${r.status}`];
  } catch {
    return [false, "Receiver not running: start it with cortex browse serve"];
  }
}

// The receiver's processing state: "Last processed at …" or why it is unknown.
async function processed(port, token) {
  try {
    const r = await fetch(`http://127.0.0.1:${port}/v1/status`, {
      headers: { Authorization: `Bearer ${token}` }, cache: "no-store", credentials: "omit",
      redirect: "error", signal: AbortSignal.timeout(3000) });
    if (!r.ok) return "";
    const b = await r.json();
    if (b.processing) return "Processing now…";
    const at = b.last_processed ? new Date(b.last_processed) : null;
    const when = at && !Number.isNaN(at.getTime()) ? `Last processed at ${at.toLocaleString()}`
      : "Not processed yet";
    const line = b.queued_records ? `${when}; ${b.queued_records} waiting on the receiver` : when;
    return b.last_summary ? `${line}. ${b.last_summary}` : line;
  } catch {
    return "";
  }
}

function setStatus(ok, text) {
  $("status").textContent = text;
  $("status").className = `status ${ok ? "ok" : "bad"}`;
}

async function render() {
  const s = await chrome.storage.local.get(DEFAULTS);
  $("pause").textContent = s.paused ? "Resume" : "Pause";
  $("pause").setAttribute("aria-pressed", String(s.paused));
  $("sent").textContent = String(s.sent.day === dayKey(Date.now()) ? s.sent.n : 0);
  $("queue").textContent = String(s.queue.length);
  if (document.activeElement !== $("port")) $("port").value = s.port;
  if (document.activeElement !== $("token")) $("token").value = s.token;
  if (!s.token) return setStatus(false, "Not paired: paste the token from cortex browse pair");
  if (s.paused) return setStatus(false, "Paused: nothing is timed, read or sent");
  setStatus(...(await ping(s.port, s.token)));
  $("processed").textContent = await processed(s.port, s.token);
}

$("process").addEventListener("click", async () => {
  const s = await chrome.storage.local.get(DEFAULTS);
  const [ok, msg] = await ping(s.port, s.token);
  if (!ok) {
    $("processed").textContent = `Not sent. ${msg}`;
    return;
  }
  chrome.runtime.sendMessage("process").catch(() => {});
  $("processed").textContent = "Sent: processing starts in a few seconds";
});

$("pause").addEventListener("click", async () => {
  const { paused } = await chrome.storage.local.get({ paused: false });
  await chrome.storage.local.set({ paused: !paused });
  render();
});

$("deny").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  let host = "";
  try {
    const u = new URL(tab.url);
    if (u.protocol === "http:" || u.protocol === "https:") host = u.hostname.replace(/^www\./, "");
  } catch { /* no page */ }
  if (!host) {
    $("denied").textContent = "This tab is not a web page.";
    return;
  }
  const { deny } = await chrome.storage.local.get({ deny: [] });
  if (!deny.includes(host)) await chrome.storage.local.set({ deny: [...deny, host] });
  $("denied").textContent = `Cortex will never read ${host} or its subdomains.`;
});

$("pair").addEventListener("submit", async (e) => {
  e.preventDefault();
  const port = Number($("port").value);
  const token = $("token").value.trim();
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    return setStatus(false, "The port must be a whole number from 1024 to 65535");
  }
  if (!/^[A-Za-z0-9_-]{20,128}$/.test(token)) {
    return setStatus(false, "That does not look like a pairing token");
  }
  await chrome.storage.local.set({ port, token, status: "" }); // a new token may capture again
  setStatus(true, "Saved. Testing the receiver…");
  const [ok, msg] = await ping(port, token);
  // say it was saved either way, so the click visibly did something
  setStatus(ok, `Saved. ${msg}`);
  if (ok) chrome.runtime.sendMessage("flush").catch(() => {});
});

render();
// keep the counts and the processing line live while the popup is open
setInterval(() => { if (!document.querySelector("form :focus")) render(); }, 2000);
