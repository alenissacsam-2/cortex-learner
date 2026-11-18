// Injected on demand by background.js (never declared in the manifest), only after the page
// has been on screen for 20 s and only when the site is not denied and capture is not paused.
// It never reads form fields: input, textarea, select, [contenteditable] and everything inside
// a form are removed before any text is taken. The last expression is the result.
(() => {
  if (location.protocol !== "http:" && location.protocol !== "https:") return null;
  const result = { href: location.href, title: document.title, excerpt: "", channel: "" };
  // a page with a password field is a sign-in page whatever its URL says: read nothing
  if (document.querySelector("input[type=password]")) return result;

  const host = location.hostname.replace(/^(www|m)\./, "");
  if (host === "youtube.com" && location.pathname === "/watch") {
    const t = document.querySelector("h1.ytd-watch-metadata, h1.title");
    const c = document.querySelector(
      "ytd-watch-metadata #owner #channel-name a, ytd-video-owner-renderer #channel-name a");
    if (t) result.title = t.textContent.trim();
    if (c) result.channel = c.textContent.trim().slice(0, 200);
    return result; // a video's title and channel only, no comments or description
  }

  const PRIVATE = "form, [contenteditable]";
  const SKIP = "nav, footer, aside, header, form, input, textarea, select, option, button, " +
    "[contenteditable], script, style, noscript, template, svg, canvas, iframe, object, " +
    "[hidden], [aria-hidden=true]";

  function pick() {
    for (const sel of ["article", "main", "[role=main]"]) {
      const el = document.querySelector(sel);
      if (el && !el.closest(PRIVATE)) return el;
    }
    // the element holding the most paragraph text
    const sums = new Map();
    for (const p of document.querySelectorAll("p")) {
      if (p.closest(PRIVATE)) continue;
      const parent = p.parentElement;
      if (parent) sums.set(parent, (sums.get(parent) || 0) + p.textContent.length);
    }
    let best = null;
    let most = 0;
    for (const [el, n] of sums) if (n > most) [best, most] = [el, n];
    return best;
  }

  const root = pick();
  if (!root) return result;
  const copy = root.cloneNode(true); // a detached copy; the live page is never changed
  copy.querySelectorAll(SKIP).forEach((el) => el.remove());
  result.excerpt = (copy.textContent || "").replace(/\s+/g, " ").trim().slice(0, 2000);
  return result;
})();
