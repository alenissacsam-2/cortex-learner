// Pure helpers shared by the service worker and the popup: the deny check that runs before
// anything is read, and the URL scrub. RULES mirrors cortex.browse.lists (the Python filter);
// tests/test_browse_serve.py checks that they agree. The receiver filters again on arrival,
// so this copy only has to be as strict, never the last line of defence.
export const RULES = /*RULES*/{
  "authHosts": ["account.live.com", "account.microsoft.com", "accounts.google.com", "appleid.apple.com", "auth0.com", "id.atlassian.com", "login.live.com", "login.microsoftonline.com", "myaccount.google.com", "okta.com"],
  "authWords": ["2fa", "auth", "authenticate", "authentication", "authn", "authorize", "login", "logon", "logout", "mfa", "oauth", "oauth2", "otp", "passwd", "password", "passwords", "recover", "saml", "sessions", "signin", "signon", "signup", "sso", "verification", "verify"],
  "payHosts": ["billdesk.com", "billing.stripe.com", "ccavenue.com", "checkout.stripe.com", "pay.amazon.com", "pay.amazon.in", "pay.google.com", "payments.google.com", "paypal.com", "paytm.com", "payu.com", "payu.in", "phonepe.com", "razorpay.com", "revolut.com", "wise.com"],
  "payWords": ["billing", "checkout", "pay", "payment", "payments", "paynow", "purchase"],
  "bankHosts": ["americanexpress.com", "aubank.in", "axisbank.com", "bankofamerica.com", "bankofbaroda.in", "barclays.co.uk", "bobibanking.com", "canarabank.com", "capitalone.com", "chase.com", "citi.com", "citibank.com", "federalbank.co.in", "hdfcbank.com", "hsbc.co.in", "hsbc.com", "icicibank.com", "idfcfirstbank.com", "indusind.com", "kotak.com", "netpnb.com", "onlinesbi.sbi", "pnbindia.in", "rblbank.com", "sbi.co.in", "unionbankofindia.co.in", "wellsfargo.com", "yesbank.in"],
  "bankWords": ["bank", "banking", "ebanking", "ibanking", "netbanking"],
  "bankLabel": "bank",
  "bankTitles": ["net banking", "netbanking", "internet banking", "online banking", "mobile banking", "account summary", "account statement", "bank statement", "card statement", "e-statement"],
  "webmailHosts": ["app.fastmail.com", "mail.aol.com", "mail.google.com", "mail.proton.me", "mail.yahoo.com", "mail.yandex.com", "mail.zoho.com", "mail.zoho.in", "outlook.live.com", "outlook.office.com", "outlook.office365.com"],
  "secretKeys": ["access_token", "apikey", "auth", "code", "key", "password", "secret", "session", "sig", "token"],
  "secretParts": ["api_key", "apikey", "passwd", "password", "secret", "session", "signature", "token"]
}/*END*/;

const AUTH_WORDS = new Set(RULES.authWords);
const PAY_WORDS = new Set(RULES.payWords);
const BANK_WORDS = new Set(RULES.bankWords);
const SECRET_KEYS = new Set(RULES.secretKeys);
const SITES = [...RULES.authHosts, ...RULES.payHosts, ...RULES.bankHosts, ...RULES.webmailHosts];

// [host regex (after www./m.), path regex, the one query key kept]; mirrors privacy._engine
const ENGINES = [
  ["google(\\.[a-z]{2,3}){1,2}", "/search", "q"],
  ["bing\\.com", "/search", "q"],
  ["(html\\.)?duckduckgo\\.com", ".*", "q"],
  ["search\\.brave\\.com", "/search.*", "q"],
  ["youtube\\.com", "/results", "search_query"],
  ["perplexity\\.ai", "/search.*", "q"],
  ["amazon(\\.[a-z]{2,3}){1,2}", "/s", "k"],
  ["github\\.com", "/search", "q"],
];
const full = (re, s) => new RegExp(`^(?:${re})$`).test(s);

export function onDomain(host, domains) {
  return domains.some((d) => host === d || host.endsWith("." + d));
}

// Every word of the (decoded) path segments and host labels, like privacy._all_words.
function words(parts) {
  return new Set(parts.flatMap((p) => p.toLowerCase().match(/[a-z0-9]+/g) || []));
}

const decode = (seg) => { try { return decodeURIComponent(seg); } catch { return seg; } };

// Per path segment or host label: its first word, and its first two words joined
// ("sign-in" -> signin), like privacy._heads.
function heads(parts) {
  const out = new Set();
  for (const p of parts) {
    const w = p.toLowerCase().match(/[a-z0-9]+/g);
    if (!w) continue;
    out.add(w[0]);
    if (w.length > 1) out.add(w[0] + w[1]);
  }
  return out;
}

function isLocal(host) {
  if (host === "localhost" || /\.(localhost|local|internal|lan)$/.test(host)) return true;
  const ip = host.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/);
  if (!ip) return !host.includes(".");
  const [a, b] = [Number(ip[1]), Number(ip[2])];
  return a === 0 || a === 10 || a === 127 || a >= 224 || (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) ||
    (a === 100 && b >= 64 && b <= 127);
}

export function secretKey(key) {
  const k = key.toLowerCase();
  return SECRET_KEYS.has(k) || RULES.secretParts.some((p) => k.includes(p));
}

function hostOf(u) {
  return u.hostname.toLowerCase().replace(/\.$/, "");
}

// Why this URL must never be read or sent, or null. `userDeny` is the popup's deny list plus
// the receiver's deny_domains (from /v1/ping).
export function denyReason(url, userDeny = []) {
  let u;
  try {
    u = new URL(url);
  } catch {
    return "bad";
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") return "scheme";
  const host = hostOf(u);
  if (!/^[a-z0-9.-]+$/.test(host)) return "scheme"; // IPv6 literals and odd hosts
  if (u.username || u.password) return "secret";
  if (isLocal(host)) return "local";
  const parts = [...u.pathname.split("/").map(decode), ...host.split(".")];
  const all = words(parts);
  const h = heads(parts); // also joins "sign_in" -> signin
  if ([...all].some((w) => AUTH_WORDS.has(w) || BANK_WORDS.has(w)) ||
      [...h].some((w) => AUTH_WORDS.has(w) || PAY_WORDS.has(w)) ||
      host.split(".").some((l) => l.includes(RULES.bankLabel))) return "sign-in";
  if (onDomain(host, SITES)) return "site";
  if (onDomain(host, userDeny)) return "user";
  const keys = [...u.searchParams.keys()];
  if (u.hash.includes("=")) keys.push(...new URLSearchParams(u.hash.slice(1)).keys());
  if (keys.some(secretKey)) return "secret";
  return null;
}

// {url, kind, term}: the URL without query or fragment, except a known engine's search term
// (kind "search") or a YouTube video id (kind "video").
export function scrub(url) {
  const u = new URL(url);
  const host = hostOf(u);
  const h = host.replace(/^www\./, "").replace(/^m\./, "");
  const base = `${u.protocol}//${host}${u.port ? ":" + u.port : ""}${u.pathname}`;
  for (const [hr, pr, key] of ENGINES) {
    if (full(hr, h) && full(pr, u.pathname)) {
      const term = (u.searchParams.get(key) || "").split(/\s+/).filter(Boolean).join(" ")
        .slice(0, 200);
      if (term) return { url: `${base}?${key}=${encodeURIComponent(term)}`, kind: "search", term };
      return { url: base, kind: "page", term: null };
    }
  }
  let vid = null;
  if (h === "youtube.com" && u.pathname === "/watch") vid = u.searchParams.get("v");
  else if (h === "youtu.be") vid = u.pathname.split("/")[1];
  if (vid && /^[A-Za-z0-9_-]{6,20}$/.test(vid)) {
    const url = h === "youtu.be" ? `${u.protocol}//${host}/${vid}` : `${base}?v=${vid}`;
    return { url, kind: "video", term: null };
  }
  return { url: base, kind: "page", term: null };
}

// A bank page by its title ("Account Summary | SBI Card"), like privacy.judge.
export function bankTitle(title) {
  const t = String(title || "").toLowerCase();
  return RULES.bankTitles.some((p) => t.includes(p));
}

// ---- session boundaries: pure, so tests/test_browse_serve.py runs them in node ----

export const SESSION_IDLE_MS = 15 * 60 * 1000;

// When the owner went away, kept across idle -> locked: chrome.idle says "idle" after 60 s
// without input and "locked" at once; 0 once they are back.
export function awaySince(prev, state, now) {
  if (state === "active") return 0;
  return prev || (state === "idle" ? now - 60000 : now);
}

// A session ends after 15 min away, if anything from it still waits to be processed.
export function sessionEndDue(away, pending, now) {
  return !!pending && away > 0 && now - away >= SESSION_IDLE_MS;
}

// The /v1/batch body: a plain list, or {records, session_end: true} when the session ended.
export function batchBody(records, end) {
  return end ? { records, session_end: true } : records;
}

// A local calendar day "YYYY-MM-DD" for the popup's "sent today" count.
export function dayKey(ms) {
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// ---- receiver identity: the token goes only to a listener that proves it holds it ----

const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");

// Asks GET /v1/hello?n=<fresh nonce> WITHOUT the token and checks the answer is
// HMAC-SHA256(token, nonce). true: the Cortex receiver; "old": a receiver from an older Cortex
// (its /v1/hello asks for the token: a 401 with its own JSON refusal); false: another program
// answers on the port; null: nothing answers. Only true may be given the token.
export async function proveReceiver(port, token) {
  const nonce = hex(crypto.getRandomValues(new Uint8Array(32)));
  let r;
  try {
    r = await fetch(`http://127.0.0.1:${Number(port)}/v1/hello?n=${nonce}`, {
      cache: "no-store", credentials: "omit", redirect: "error",
      signal: AbortSignal.timeout(3000) });
  } catch {
    return null;
  }
  try {
    if (r.status === 401) {
      const b = await r.json();
      const keys = b && typeof b === "object" ? Object.keys(b).sort().join() : "";
      return keys === "error,ok" && b.ok === false ? "old" : false;
    }
    const { proof } = r.ok ? await r.json() : {};
    const enc = new TextEncoder();
    const key = await crypto.subtle.importKey("raw", enc.encode(token),
      { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    return proof === hex(await crypto.subtle.sign("HMAC", key, enc.encode(nonce)));
  } catch {
    return false;
  }
}
