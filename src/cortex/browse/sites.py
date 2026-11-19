"""One page per website and the browsing profile (docs/design/browse-brain.md §3a).

`record` folds a run's filtered items into per-day, per-site counts in
`.cortex/browse/site-days.json` (the browser forgets old history; these counts do not), keyed
`site|category|private-domain`, so who may see a site is decided when the pages are written,
from the settings of that moment. `write` makes, through `cortex.vault` only:
- a visible page per site, `browse/sites/<category>/browse-<site>.md`, from the items of no
  hidden category only; a hidden page, `<private>/sites/<category>/browse-private-<site>.md`
  (`private: true`), from the rest. A site with both gets both pages;
- `browse/sites/browse-sites.md`, `browse/sites.csv` and `browse/browse-profile.md` from the
  visible pages only, and their private twins from every page plus the hidden categories;
- with `browse.visible_to_claude: false` every page is a hidden one.
A visible file never holds a host, title, search term, count or link that came from a hidden
item: led-by searches and co-visited sites are filtered by the same rule. Ids carry the
`browse-` prefix (ids are unique across kinds). Unchanged pages are never rewritten (a content
hash in `.cortex/browse/sites.json`, next to the local LLM's cached output).
`deepen` is the local LLM's long, resumable pass: per site, most visits first, a category for
sites the table does not know, a one-line description, related sites and topic hubs; it can
move a site INTO a sensitive category, never out of one.
"""

from __future__ import annotations

import csv
import functools
import hashlib
import io
import json
import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import cached_property
from urllib.parse import urlsplit

from cortex.browse import lists, privacy
from cortex.browse.privacy import Settings
from cortex.render import esc
from cortex.vault import Vault, VaultError, secret_pattern

log = logging.getLogger(__name__)

DIR = "browse"
HIDDEN = DIR + "/private"  # never read by the index, the dashboard or any chat
CACHE = ".cortex/browse/sites.json"  # the local LLM's output and the page hashes
# a description made with an older prompt is made again on the next deepen
DESC_PROMPT = 2
DAYS = ".cortex/browse/site-days.json"  # per day and site key: the counts pages are made of
VERDICTS = ".cortex/browse/verdicts.json"  # C1: the local model's label per text hash
SITES_DIR, HIDDEN_SITES_DIR = DIR + "/sites", HIDDEN + "/sites"
INDEX_ID, HIDDEN_INDEX_ID = "browse-sites", "browse-private-sites"
PROFILE_ID, HIDDEN_PROFILE_ID = "browse-profile", "browse-private-profile"
CSV_PATH, HIDDEN_CSV = DIR + "/sites.csv", HIDDEN + "/sites.csv"
LED_S = 10 * 60  # a search this soon before a visit to another site "led there"
SESSION_GAP = 30 * 60
CO_KEYS = 30  # site keys per session counted as visited together
TOP_PAGES, TOP_SEARCHES, TOP_RELATED, TOP_SITES, PER_CATEGORY = 15, 10, 8, 25, 200
LLM_TITLES, LLM_CANDIDATES = 20, 40
SAVE_EVERY = 20  # deepen saves its cache this often, so a stop loses little
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
LABELS = (*lists.SITE_CATEGORIES, *lists.CATEGORIES)
_IP = re.compile(r"^[0-9.]+$|:")
# one browse-wide lock: an import, a deepen pass and a forget each load the stores and save
# them, so one running across another would bring back what the other took out
LOCK = ".cortex/browse/lock"
BUSY = "Browsing is being processed; try again in a minute."
WAIT_S = 60.0  # an import or deepen pass waits this long for another to finish


def busy(vault: Vault, wait: float | None = None):
    """The browse-wide lock (`Vault.job_lock`); VaultError("busy", BUSY) after `wait`."""
    return vault.job_lock(LOCK, WAIT_S if wait is None else wait, BUSY)


def held(fn):
    """Run `fn(vault, ...)` under the browse-wide lock."""
    @functools.wraps(fn)
    def wrapper(vault: Vault, *a, **kw):
        with busy(vault):
            return fn(vault, *a, **kw)
    return wrapper


# ---- sites and ids ------------------------------------------------------------------------

def site_of(host: str, path: str = "/") -> str:
    """The website a host belongs to: its registrable domain ("en.m.wikipedia.org" ->
    wikipedia.org), a listed host as itself ("docs.google.com"), the user's own subdomain on a
    multi-tenant host ("ana.github.io"), or "medium.com/@user"."""
    h = host.lower().rstrip(".")
    if _IP.search(h):
        return h
    for p in ("www.", "m.", "mobile."):
        if h.startswith(p) and h.count(".") > 1:
            h = h[len(p):]
    if h == "medium.com":
        seg = path.strip("/").split("/")[0].lower()
        return f"medium.com/{seg}" if re.fullmatch(r"@[a-z0-9._-]{1,60}", seg) else h
    for t in lists.MULTI_TENANT:
        if h.endswith("." + t):
            return h[: -len(t) - 1].rsplit(".", 1)[-1] + "." + t
    if h in lists.SITE_HOSTS or h in lists.WEBMAIL_HOSTS:
        return h
    labels = h.split(".")
    n = 3 if len(labels) > 2 and len(labels[-1]) == 2 and labels[-2] in lists.SECOND_LEVEL \
        else 2
    return ".".join(labels[-n:])


def host_of(site: str) -> str:
    return site.split("/", 1)[0]


def page_id(site: str, hidden: bool) -> str:
    """`browse-<site>` (`browse-private-<site>` for a hidden page): a valid, prefixed id."""
    s = re.sub(r"[^a-z0-9.-]+", "-", site.lower()).strip("-.")
    pid = ("browse-private-" if hidden else "browse-") + s
    if len(pid) > 81:
        pid = pid[:74].rstrip("-.") + "-" + hashlib.sha1(site.encode()).hexdigest()[:6]
    return pid


@functools.lru_cache(maxsize=1 << 16)
def _host_category(host: str) -> str | None:
    """`site_category` from the built-in lists alone (constants, so cached across calls: the
    word check took ~0.4 ms a host, seconds over a heavy user's sites)."""
    sens, _ = privacy.category(host, "/", "")
    if sens:
        return sens
    labels = host.split(".")
    for i in range(len(labels) - 1):  # the most specific listed host first
        if c := lists.SITE_HOSTS.get(".".join(labels[i:])):
            return c
    if labels[-1] == "edu" or "ac" in labels[-2:-1] or "edu" in labels[-2:-1]:
        return "learning"
    return None


def site_category(site: str, llm: dict) -> str:
    """A sensitive category of the host wins; then the built-in table; then the local LLM's
    (validated) label; else "other"."""
    if c := _host_category(host_of(site)):
        return c
    e = llm.get(site)
    c = e.get("category") if isinstance(e, dict) else None
    return c if c in LABELS else "other"


def _key(site: str, cat: str | None, pd: bool) -> str:
    return f"{site}|{cat or ''}|{int(pd)}"


def _split(key: str) -> tuple[str, str, bool]:
    site, cat, pd = key.rsplit("|", 2)
    return site, cat, pd == "1"


# ---- state --------------------------------------------------------------------------------

def _load(vault: Vault, rel: str) -> dict:
    try:
        d = json.loads((vault.root / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _save(vault: Vault, rel: str, data: dict) -> None:
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    p = vault.root / rel
    if not (p.is_file() and p.read_text(encoding="utf-8") == text):
        vault.write_text(p, text)


def load_days(vault: Vault) -> tuple[dict, str | None]:
    """The per-day counts, and the name an unreadable file was set aside as (R2-7): a torn
    file is never read as empty, since it holds the days the browsers forgot."""
    p = vault.root / DAYS
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {}, None
    try:
        days = json.loads(text)
    except ValueError:
        days = None
    if isinstance(days, dict):
        return days, None
    bad = f"{p.name}.bad-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    with vault.lock():
        vault.write_text(p.with_name(bad), text)
        vault.remove(p)
    log.warning("browse: an unreadable %s was set aside as %s", p.name, bad)
    return {}, bad


_LEAN: dict = {}  # DAYS path -> ((mtime_ns, size), its lean counts)
_HOSTS: dict[tuple, str] = {}  # a URL's text up to its third "/" -> url_host; ~one per host


def url_host(url: str) -> str:
    """`(urlsplit(url).hostname or "").rstrip(".")`, memoized by the URL's text up to its third
    "/" (scheme and authority: all the hostname depends on), as urlsplit took most of a heavy
    user's answer."""
    k = tuple(url.split("/", 3)[:3])
    if (h := _HOSTS.get(k)) is None:
        h = _HOSTS[k] = (urlsplit(url).hostname or "").rstrip(".")
    return h


def _urls(p) -> list:
    """The URLs of a stored page map; anything malformed is skipped."""
    return [u for u in p if isinstance(u, str)] if isinstance(p, dict | list) else []


def lean_days(vault: Vault) -> dict:
    """{day: {key: {"n", "w", "h"}}}: `load_days` with only the visit and browser counts and
    the page URLs' hosts ("h", in place of the page map "p": all `_sides` reads of it), parsed
    once per version of the file and shared, so callers must not change it. Chat's answers
    read this: a heavy user's file took seconds to parse, its page maps most of it."""
    p = vault.root / DAYS
    try:
        st = p.stat()  # before the read: a change after it misses the cache next time
    except OSError:
        return {}
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _LEAN.get(p)
    if hit is None or hit[0] != stamp:
        days = load_days(vault)[0]
        hit = _LEAN[p] = (stamp, {
            day: {k: {**{f: a[f] for f in ("n", "w") if f in a},
                      **({"h": tuple({url_host(u) for u in _urls(a["p"])})}
                         if a.get("p") else {})}
                  for k, a in keys.items() if isinstance(a, dict)}
            for day, keys in days.items() if isinstance(keys, dict)})
    return hit[1]


def load_verdicts(vault: Vault) -> dict[str, str]:
    """{privacy.text_key(text): label}: the local model's verdicts on titles and search terms
    (a sensitive category or "ordinary"); hashes only, never the text."""
    return {k: v for k, v in _load(vault, VERDICTS).items() if isinstance(v, str)}


def load_cache(vault: Vault) -> dict:
    c = _load(vault, CACHE)
    return {"llm": c.get("llm") if isinstance(c.get("llm"), dict) else {},
            "hashes": c.get("hashes") if isinstance(c.get("hashes"), dict) else {}}


# ---- recording ----------------------------------------------------------------------------

def record(vault: Vault, items: list, where: dict, settings: Settings, tz, start: str,
           held: set[str] | None, days: dict | None = None) -> dict:
    """Replace the counts of every day from `start` on that a source still holds (`held`;
    None: every day) with those of `items`; a day no source holds (forgotten by the browser,
    or from Takeout only) keeps its counts. Drop any key of a category now in
    skip_categories, and save. `days`: the counts already loaded (`load_days`). Returns the
    per-day counts."""
    days = load_days(vault)[0] if days is None else days
    for d in list(days):
        if not isinstance(days[d], dict) or (d >= start and (held is None or d in held)):
            del days[d]
    last: tuple | None = None  # the last search: (time, term, key)
    sessions: dict[str, list[list[str]]] = defaultdict(list)
    end = None
    for it in sorted(items, key=lambda i: (i.start, i.k.url)):
        k = it.k
        site = site_of(k.host, urlsplit(k.url).path)
        # a record kept local for want of a verdict (C1) stays on the hidden side, like a
        # private domain's
        key = _key(site, k.category, privacy.in_domains(k.host, settings.private) or k.local)
        ts = int(it.start.timestamp())
        a = days.setdefault(it.day, {}).setdefault(key, {"n": 0, "s": 0, "a": ts, "b": ts})
        a["n"] += 1
        a["a"], a["b"] = min(a["a"], ts), max(a["b"], ts)
        if it.browser:  # visits per source ("w"); a forget lowers "n" only, so "n" caps these
            w = a.setdefault("w", {})
            w[it.browser] = w.get(it.browser, 0) + 1
        hr = a.setdefault("hr", {})
        h = str(it.start.astimezone(tz).hour)
        hr[h] = hr.get(h, 0) + 1
        if k.search:
            q = a.setdefault("q", {})
            q[k.search] = q.get(k.search, 0) + 1
            last = (it.start, k.search, key)
        else:
            a["s"] += round(it.secs)
            p = a.setdefault("p", {}).setdefault(k.url, ["", 0, 0])
            p[0], p[1], p[2] = k.title or p[0], p[1] + 1, p[2] + round(it.secs)
            if last and _split(last[2])[0] != site and \
                    0 <= (it.start - last[0]).total_seconds() <= LED_S:
                r = a.setdefault("r", {}).setdefault(last[2], {})
                r[last[1]] = r.get(last[1], 0) + 1
                last = None  # one search leads to one site
        w = where.get((k.url, k.private))
        if w and w[0] == "hub":
            t = a.setdefault("t", {})
            t[w[1]] = t.get(w[1], 0) + 1
        if end is None or (it.start - end).total_seconds() >= SESSION_GAP or \
                not sessions[it.day]:
            sessions[it.day].append([])
        sessions[it.day][-1].append(key)
        end = it.start if end is None or it.start > end else end
    for day, ss in sessions.items():
        for s in ss:
            keys = [k for k, _ in Counter(s).most_common(CO_KEYS)]
            for a in keys:
                co = days[day][a].setdefault("co", {})
                for b in keys:
                    if _split(b)[0] != _split(a)[0]:
                        co[b] = co.get(b, 0) + 1
    skip = set(settings.skip_categories)
    for d in days.values():
        for key in [k for k in d if _split(k)[1] in skip]:
            del d[key]
    _save(vault, DAYS, days)
    return days


# ---- the pages ----------------------------------------------------------------------------

@dataclass
class Page:
    site: str
    hidden: bool
    category: str = "other"
    visits: int = 0
    secs: int = 0
    first: int = 0
    last: int = 0
    pages: dict = field(default_factory=dict)  # url -> [title, visits, secs]
    here: Counter = field(default_factory=Counter)  # searches made on this site
    led: Counter = field(default_factory=Counter)  # searches that led here
    hubs: Counter = field(default_factory=Counter)
    co: Counter = field(default_factory=Counter)  # other page ids visited in the same session
    cats: Counter = field(default_factory=Counter)  # item categories, by visits
    hours: Counter = field(default_factory=Counter)
    weekdays: Counter = field(default_factory=Counter)
    months: Counter = field(default_factory=Counter)
    month_secs: Counter = field(default_factory=Counter)
    searches: list = field(default_factory=list)  # [(item category, term, n)] made here
    src: list = field(default_factory=list)  # [(day, key)] its counts came from

    @cached_property
    def id(self) -> str:
        return page_id(self.site, self.hidden)

    def titles(self, n: int) -> list[str]:
        rows = sorted(self.pages.items(), key=lambda x: (-x[1][1], -x[1][2], x[0]))
        return list(dict.fromkeys(t for _, (t, *_) in rows if t))[:n]


def _sides(settings: Settings, llm: dict, days: dict):
    """(side, scat): `side(day, key)` is True when that day's counts of the key are hidden,
    False when visible, None when not shown at all, as `settings` decide it. The domain lists
    are checked against the site and every page URL those counts hold (R2-1): a subdomain made
    private hides the day's whole key and a denied one drops it, since its counts (visits,
    hours) cannot be split by URL."""
    scats: dict[str, str] = {}
    keys: dict[str, bool | None] = {}
    hosts: dict[str, bool | None] = {}
    seen: dict[tuple[str, str], bool | None] = {}
    deep: dict[str, bool] = {}

    def scat(site: str) -> str:
        if site not in scats:
            scats[site] = site_category(site, llm)
        return scats[site]

    def host_side(host: str) -> bool | None:
        if host not in hosts:
            hosts[host] = None if privacy.in_domains(host, settings.deny) or (
                settings.allow and not privacy.in_domains(host, settings.allow)) \
                else privacy.in_domains(host, settings.private)
        return hosts[host]

    def where(key: str) -> bool | None:
        site, cat, pd = _split(key)
        h = host_side(host_of(site))
        # a Don't-save category drops the counts whether the key carries it or only the site's
        # category says so (the local model may file a site under adult after it was stored)
        if h is None or "skip" in (privacy.audience(cat or None, settings),
                                   privacy.audience(scat(site), settings)):
            return None
        return (pd or h or privacy.audience(cat or None, settings) == "local"
                or privacy.audience(scat(site), settings) != "cloud")

    def side(day: str, key: str) -> bool | None:
        if key not in keys:
            ok = isinstance(key, str) and key.count("|") >= 2  # else a malformed key
            keys[key] = where(key) if ok else None
            deep[key] = ok and _deep(settings, key)
        if not deep[key]:
            return keys[key]  # the same every day: no per-day entry to keep
        if (day, key) not in seen:
            h = keys[key]
            a = (days.get(day) or {}).get(key)
            a = a if isinstance(a, dict) else {}
            for host in a["h"] if "h" in a else map(url_host, _urls(a.get("p"))):
                if h is None:
                    break
                u = host_side(host)
                h = None if u is None else h or u
            seen[(day, key)] = h
        return seen[(day, key)]

    return side, scat


def _deep(settings: Settings, key: str) -> bool:
    """Whether `_sides` reads the page URLs' hosts of site key `key`. A page URL's host is its
    site's host or a subdomain of it (`record`), so it can only decide differently from the
    site when a listed domain is a strict subdomain of the site; else they are left unread."""
    host = host_of(_split(key)[0])
    return any(d.endswith("." + host) for d in (*settings.deny, *settings.allow,
                                                *settings.private))


def _shown(settings: Settings, verdicts: dict[str, str]):
    """`shown(text, search)`: may a visible page show this title or search term (C1)? Not when
    the word lists (as they are now) or the local model put it in a category that is not
    cloud; a search term only with the local model's "ordinary" verdict."""
    seen: dict[tuple[str, bool], bool] = {}

    def shown(text: str, search: bool = False) -> bool:
        if (text, search) not in seen:
            cat = privacy.category("", "/", text)[0]
            v = verdicts.get(privacy.text_key(text))
            seen[(text, search)] = ((cat is None or privacy.audience(cat, settings) == "cloud")
                                    and v not in lists.CATEGORIES
                                    and (not search or v == "ordinary"))
        return seen[(text, search)]
    return shown


def pages(vault: Vault, settings: Settings, days: dict | None = None,
          llm: dict | None = None, verdicts: dict | None = None
          ) -> dict[tuple[str, bool], Page]:
    """Every site page as the settings of now decide it: {(site, hidden): Page}. A visible
    page holds only the titles and search terms `_shown` allows."""
    days = load_days(vault)[0] if days is None else days
    llm = load_cache(vault)["llm"] if llm is None else llm
    shown = _shown(settings, load_verdicts(vault) if verdicts is None else verdicts)
    side, scat = _sides(settings, llm, days)
    out: dict[tuple[str, bool], Page] = {}
    for day in sorted(days):
        try:
            d = date.fromisoformat(day)
        except ValueError:
            continue
        for key, a in days[day].items():
            h = side(day, key)
            if h is None:
                continue
            site, cat, _ = _split(key)
            p = out.setdefault((site, h), Page(site, h))
            p.src.append((day, key))
            n, s = int(a.get("n", 0)), int(a.get("s", 0))
            p.visits += n
            p.secs += s
            p.first = min(p.first or a["a"], a["a"])
            p.last = max(p.last, a["b"])
            p.cats[cat] += n
            p.weekdays[d.weekday()] += n
            p.months[day[:7]] += n
            p.month_secs[day[:7]] += s
            for hr, c in (a.get("hr") or {}).items():
                p.hours[int(hr)] += c
            for url, (t, c, x) in (a.get("p") or {}).items():
                if t and not h and not shown(t):
                    continue
                row = p.pages.setdefault(url, ["", 0, 0])
                row[0], row[1], row[2] = t or row[0], row[1] + c, row[2] + x
            for t, c in (a.get("q") or {}).items():
                if not h and not shown(t, True):
                    continue
                p.here[t] += c
                p.searches.append((cat, t, c))
            for skey, terms in (a.get("r") or {}).items():
                # a search shows where its own key may be seen: never a hidden one on a visible page
                if (sh := side(day, skey)) is None or (sh and not h):
                    continue
                for t, c in terms.items():
                    if h or shown(t, True):
                        p.led[t] += c
            for hub, c in (a.get("t") or {}).items():
                p.hubs[hub] += c
            for other, c in (a.get("co") or {}).items():
                if (oh := side(day, other)) is None or (oh and not h):
                    continue
                p.co[page_id(_split(other)[0], oh)] += c
    for p in out.values():
        sc = scat(p.site)
        if p.hidden and sc not in lists.CATEGORIES:
            hidden_cats = Counter({c: n for c, n in p.cats.items() if c})
            sc = hidden_cats.most_common(1)[0][0] if hidden_cats else sc
        p.category = sc
    return out


def _fm(fields: dict, body: str) -> str:
    """Frontmatter for flat str/int/bool fields (ruamel took 5 ms a page), then the body."""
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v) if isinstance(v, int) else json.dumps(str(v), ensure_ascii=False)

    return "---\n" + "".join(f"{k}: {val(v)}\n" for k, v in fields.items()) + "---\n" + body


def _dur(secs: float) -> str:
    m = max(1, round(secs / 60))
    h, m = divmod(m, 60)
    return f"{m} min" if not h else f"{h} h {m} min" if m else f"{h} h"


def _day(ts: int, tz) -> str:
    return datetime.fromtimestamp(ts, UTC).astimezone(tz).strftime("%Y-%m-%d")


def _valid_desc(entry: dict | None, p: Page) -> str | None:
    """The cached description, when every title it was made from is still on this page (a
    title that has since become hidden voids it on a visible page)."""
    if not isinstance(entry, dict) or not isinstance(entry.get("desc"), str):
        return None
    have = {_h(t) for t, *_ in p.pages.values() if t}
    return entry["desc"] if set(entry.get("fed") or []) <= have else None


def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def _page_text(p: Page, ids: dict[str, Page], llm: dict, tz) -> tuple[str, str]:
    """(relative path, text) of one site page."""
    entry = (llm.get(p.site) or {}).get("h" if p.hidden else "v")
    up = HIDDEN_INDEX_ID if p.hidden else INDEX_ID
    fields = {"id": p.id, "title": p.site, "kind": "browse", "host": p.site,
              "category": p.category, "visits": p.visits, "first_seen": _day(p.first, tz),
              "last_seen": _day(p.last, tz)}
    if p.hidden:
        fields["private"] = True
    out = [f"# {p.site}", "", f"Up: [[{up}]]", ""]
    if desc := _valid_desc(entry, p):
        out += [esc(desc, 200), ""]
    out += [f"Category: {p.category} · {p.visits} visit{'s' if p.visits != 1 else ''} · "
            f"{_dur(p.secs)} · first seen {fields['first_seen']} · last seen "
            f"{fields['last_seen']}"]
    hubs = Counter(p.hubs)
    for hub in (entry or {}).get("topics") or []:
        hubs.setdefault(hub, 0)
    if hubs:
        out += ["", "## Topics", *[f"- [[{h}]]" + (f" · {n} visits" if n else "")
                                   for h, n in sorted(hubs.items(), key=lambda x: (-x[1], x[0]))]]
    rows = sorted(p.pages.items(), key=lambda x: (-x[1][1], -x[1][2], x[0]))
    if rows:
        out += ["", "## Top pages"]
        for url, (t, n, s) in rows[:TOP_PAGES]:
            out.append(f"- {esc(t) or esc(urlsplit(url).path, 80) or '/'} · {n} visit"
                       f"{'s' if n != 1 else ''} · {_dur(s)}")
        if len(rows) > TOP_PAGES:
            out.append(f"- and {len(rows) - TOP_PAGES} more")
    for head, c in (("## Searches that led here", p.led), ("## Searches on this site", p.here)):
        if c:
            out += ["", head, *[f"- {esc(t)} · {n}" for t, n in
                                sorted(c.items(), key=lambda x: (-x[1], x[0]))[:TOP_SEARCHES]]]
    rel: list[str] = []
    for s in (entry or {}).get("related") or []:  # the LLM's first, then visited together
        for pid in (page_id(s, False), page_id(s, True)):
            if pid in ids and (p.hidden or not ids[pid].hidden) and pid not in rel:
                rel.append(pid)
    for pid, _ in sorted(p.co.items(), key=lambda x: (-x[1], x[0])):
        if pid in ids and pid != p.id and (p.hidden or not ids[pid].hidden) and pid not in rel:
            rel.append(pid)
    rel = [r for r in rel if r != p.id][:TOP_RELATED]
    if rel:
        out += ["", "## Related sites", *[f"- [[{r}]]" for r in rel]]
    root = HIDDEN_SITES_DIR if p.hidden else SITES_DIR
    return f"{root}/{p.category}/{p.id}.md", _fm(fields, "\n".join(out) + "\n")


def _index_text(shown: list[Page], hidden_index: bool) -> str:
    pid = HIDDEN_INDEX_ID if hidden_index else INDEX_ID
    fields = {"id": pid, "title": "Sites (private)" if hidden_index else "Sites",
              "kind": "browse"}
    if hidden_index:
        fields["private"] = True
    up = HIDDEN_PROFILE_ID if hidden_index else PROFILE_ID
    out = [f"# {fields['title']}", "", f"Up: [[{up}]]"]
    by: dict[str, list[Page]] = defaultdict(list)
    for p in shown:
        by[p.category].append(p)
    for cat in sorted(by, key=lambda c: (-sum(p.visits for p in by[c]), c)):
        ps = sorted(by[cat], key=lambda p: (-p.visits, p.site))
        out += ["", f"## {cat}", *[f"- [[{p.id}]] · {p.visits} visits · {_dur(p.secs)}"
                                   + (" · hidden" if p.hidden and hidden_index else "")
                                   for p in ps[:PER_CATEGORY]]]
        if len(ps) > PER_CATEGORY:
            out.append(f"- and {len(ps) - PER_CATEGORY} more")
    return _fm(fields, "\n".join(out) + "\n")


def _csv(shown: list[Page], tz, hidden_col: bool) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["site", "category", "visits", "minutes", "first_seen", "last_seen", "page",
                *(["hidden"] if hidden_col else [])])
    for p in sorted(shown, key=lambda p: (-p.visits, p.site, p.hidden)):
        w.writerow([p.site, p.category, p.visits, round(p.secs / 60), _day(p.first, tz),
                    _day(p.last, tz), p.id, *([int(p.hidden)] if hidden_col else [])])
    return buf.getvalue()


def _profile_text(shown: list[Page], hidden_profile: bool, settings: Settings) -> str:
    pid = HIDDEN_PROFILE_ID if hidden_profile else PROFILE_ID
    fields = {"id": pid, "title": "Browsing profile" + (" (private)" if hidden_profile else ""),
              "kind": "browse"}
    if hidden_profile:
        fields["private"] = True
    hours, weekdays, months, msecs, hubs = Counter(), Counter(), Counter(), Counter(), Counter()
    msites: dict[str, set] = defaultdict(set)
    cats: dict[str, list] = defaultdict(lambda: [0, 0, 0])
    for p in shown:
        hours.update(p.hours)
        weekdays.update(p.weekdays)
        months.update(p.months)
        msecs.update(p.month_secs)
        hubs.update(p.hubs)
        for mo in p.months:
            msites[mo].add(p.id)
        c = cats[p.category]
        c[0], c[1], c[2] = c[0] + p.visits, c[1] + p.secs, c[2] + 1
    total = sum(p.visits for p in shown)
    out = [f"# {fields['title']}", "",
           f"Up: [[{HIDDEN_INDEX_ID if hidden_profile else INDEX_ID}]]", "",
           f"{total} visits to {len(shown)} sites."]
    out += ["", "## When I browse", "", "### By hour",
            *[f"- {h:02d}:00 · {hours[h]} visits" for h in range(24) if hours[h]],
            "", "### By weekday", *[f"- {WEEKDAYS[d]} · {weekdays[d]} visits"
                                    for d in range(7) if weekdays[d]]]
    out += ["", "## Top categories", *[
        f"- {c} · {v} visits · {_dur(s)} · {n} site{'s' if n != 1 else ''}"
        for c, (v, s, n) in sorted(cats.items(), key=lambda x: (-x[1][0], x[0]))]]
    out += ["", "## Top sites", *[f"- [[{p.id}]] · {p.category} · {p.visits} visits · "
                                  f"{_dur(p.secs)}" for p in
                                  sorted(shown, key=lambda p: (-p.visits, p.site))[:TOP_SITES]]]
    if hubs:
        out += ["", "## Interests", *[f"- [[{h}]] · {n} visits"
                                      for h, n in sorted(hubs.items(),
                                                         key=lambda x: (-x[1], x[0]))[:20]]]
    out += ["", "## Months", *[f"- {mo} · {months[mo]} visits · {_dur(msecs[mo])} · "
                               f"{len(msites[mo])} sites" for mo in sorted(months, reverse=True)]]
    if hidden_profile:
        hide = [c for c in lists.CATEGORIES if c in settings.hidden_from_claude]
        rows = []
        for c in hide:
            ps = [p for p in shown if p.hidden and p.category == c]
            terms = Counter()
            for p in shown:
                if p.hidden:
                    for cat, t, n in p.searches:
                        if cat == c:
                            terms[t] += n
            if not ps and not terms:
                continue
            rows += ["", f"### {c}", f"{sum(p.visits for p in ps)} visits · "
                     f"{_dur(sum(p.secs for p in ps))} · {len(ps)} sites", "", "Top sites:",
                     *([f"- [[{p.id}]] · {p.visits} visits · {_dur(p.secs)}" for p in
                        sorted(ps, key=lambda p: (-p.visits, p.site))[:TOP_SITES]] or ["- none"]),
                     "", "Top searches:",
                     *([f"- {esc(t)} · {n}" for t, n in
                        sorted(terms.items(), key=lambda x: (-x[1], x[0]))[:TOP_SITES]]
                       or ["- none"])]
        if rows:
            out += ["", "## Hidden categories", *rows]
    return _fm(fields, "\n".join(out) + "\n")


_OURS = re.compile(r'\A---\nid: "?(browse-[a-z0-9.-]+)"?\n(?:[^\n]*\n)*?kind: "?browse"?\n')


def _ours(path) -> bool:
    """A file this module made (R2-2): a CSV with its header row, or a `browse-*.md` whose
    frontmatter starts with its own id and says kind browse. An owner's own note under
    browse/sites/ is never one."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(1000)
    except OSError:
        return False
    if path.suffix == ".csv":
        return head.startswith("site,category,visits,minutes,first_seen,last_seen,page")
    m = _OURS.match(head)
    return bool(m) and m.group(1) == path.stem


def on_disk(vault: Vault) -> list:
    """Every file this module makes that is on disk now."""
    out = [p for d in (SITES_DIR, HIDDEN_SITES_DIR) for p in (vault.root / d).rglob("*.md")]
    return out + [p for rel in (CSV_PATH, HIDDEN_CSV, f"{DIR}/{PROFILE_ID}.md",
                                f"{HIDDEN}/{HIDDEN_PROFILE_ID}.md")
                  if (p := vault.root / rel).is_file()]


WRITE_CHUNK = 200  # about 1 s of pages per lock hold
PROGRESS_S = 5.0  # a longer write says how far it got (counts only)


def write(vault: Vault, settings: Settings, tz=None, *, say=None, days: dict | None = None,
          bad: str | None = None) -> dict:
    """Write every site page, the indexes, CSVs and profiles that changed, and remove the ones
    no longer made (a site that moved category or side), never a file this module did not
    make. `bad`: the name an unreadable site-days.json was set aside as this run
    (`load_days`); then nothing is removed. Counts only."""
    say = say or (lambda s: None)
    if days is None:
        days, bad = load_days(vault)
    cache = load_cache(vault)
    say("sites: making the site pages")
    allp = pages(vault, settings, days, cache["llm"])
    vis = [p for p in allp.values() if not p.hidden]
    files: dict[str, str] = {}
    ids = {p.id: p for p in allp.values()}
    for p in allp.values():
        rel, text = _page_text(p, ids, cache["llm"], tz)
        files[rel] = text
    every = list(allp.values())
    if vis:
        files[f"{SITES_DIR}/{INDEX_ID}.md"] = _index_text(vis, False)
        files[CSV_PATH] = _csv(vis, tz, False)
        files[f"{DIR}/{PROFILE_ID}.md"] = _profile_text(vis, False, settings)
    if every:
        files[f"{HIDDEN_SITES_DIR}/{HIDDEN_INDEX_ID}.md"] = _index_text(every, True)
        files[HIDDEN_CSV] = _csv(every, tz, True)
        files[f"{HIDDEN}/{HIDDEN_PROFILE_ID}.md"] = _profile_text(every, True, settings)
    old: dict = cache["hashes"]
    new: dict[str, str] = {}
    c: Counter = Counter()
    items = sorted(files.items())
    say(f"sites: checking {len(items)} files")
    last = time.monotonic()
    # a short lock per chunk, never one for the whole run (R2-3: 20k pages held it 114 s,
    # so every other writer timed out); each file is still written atomically
    for i in range(0, len(items), WRITE_CHUNK):
        with vault.lock():
            for rel, text in items[i:i + WRITE_CHUNK]:
                h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
                path = vault.root / rel
                if old.get(rel) == h and path.is_file():
                    new[rel] = h
                    c["same"] += 1
                    continue
                if secret_pattern(text):  # a line that looks like a secret is left out
                    text = "\n".join(ln for ln in text.split("\n") if not secret_pattern(ln))
                try:
                    vault.write_text(path, text)
                except VaultError as e:
                    log.warning("browse: a site page was not written: %s", e.code)
                    c["failed"] += 1
                    continue
                new[rel] = h
                c["written"] += 1
        if time.monotonic() - last >= PROGRESS_S:
            say(f"sites: {min(i + WRITE_CHUNK, len(items))} of {len(items)} files checked, "
                f"{c['written']} written")
            last = time.monotonic()
    with vault.lock():
        # reconcile with the disk, not the hashes: a page this run did not make (a site now
        # hidden, visibility turned off, a lost cache) goes, if this module made it; with the
        # counts set aside (a torn file), nothing goes this run
        for p in [] if bad else on_disk(vault):
            if p.relative_to(vault.root).as_posix() in files:
                continue
            if _ours(p):
                c["removed"] += vault.remove(p)
            else:
                c["not_ours"] += 1
    _save(vault, CACHE, {"llm": cache["llm"], "hashes": new})
    r = {"sites_visible": len(vis), "sites_hidden": len(every) - len(vis),
         "sites_written": c["written"], "sites_same": c["same"], "sites_removed": c["removed"],
         "sites_failed": c["failed"], "sites_not_ours": c["not_ours"],
         "sites_days_bad": int(bool(bad))}
    if bad:
        say(f"sites: the site counts file was unreadable and is set aside as {bad}; no site "
            "page was removed this run")
    say(f"sites: {r['sites_visible']} visible, {r['sites_hidden']} hidden; written "
        f"{r['sites_written']}, unchanged {r['sites_same']}, removed {r['sites_removed']}"
        + (f"; {r['sites_not_ours']} files not made by Cortex left alone"
           if r["sites_not_ours"] else ""))
    return r


# ---- the local LLM's pass -----------------------------------------------------------------

def _hubs(vault: Vault) -> dict[str, str]:
    """{hub title (lowercased): hub id} of the owner's topic hubs."""
    from cortex import topics
    from cortex.catalog import Catalog
    from cortex.index import Index

    try:
        t = topics.tree(Index(vault, Catalog.load(vault)))
    except Exception as e:  # a vault the index cannot read: no topic links, the rest goes on
        log.warning("browse: topic hubs not read: %s", type(e).__name__)
        return {}
    return {str(t.title.get(h, h)).strip().lower(): h for h in t.parent if h != topics.ROOT}


def _clean(text) -> str | None:
    if not isinstance(text, str):
        return None
    s = re.sub(r"https?://\S+|www\.\S+|<[^>]*>|[*_`#>\[\]|]", "", text)
    s = " ".join(s.split())[:200].strip()
    return s if s and not secret_pattern(s) else None


@held
def deepen(vault: Vault, runner, budget, settings: Settings, *, tz=None, say=None,
           pick=None, fresh=None) -> dict:
    """For each site page, most visits first, until `budget` runs out: ask the local LLM once
    for a category (kept only for a site the table and the sensitive rules do not place), a
    one-line description, related sites (from the candidates it was shown) and topic hubs.
    A visible page's call hears only that page's titles and visible candidate sites. With
    `pick(audience)` (browse_serve.runner_for) the runner is chosen per page: a hidden page is
    "local", a visible one "cloud" while the settings of that moment (`fresh()`, default
    `settings`) still show all of it, else "local"; a page with no runner is left for next
    time. A call to a runner other than `runner` (the cloud) hears only the topic hubs the
    local index linked that site to, and only candidate sites still visible. The answers are
    saved to `.cortex/browse/sites.json` as they come, so a stopped pass resumes where it
    was; then the pages are rewritten. Counts only."""
    say = say or (lambda s: None)
    cache = load_cache(vault)
    llm = cache["llm"]
    days, bad = load_days(vault)
    allp = pages(vault, settings, days, llm)
    hubs = _hubs(vault)
    names = {h: t for t, h in hubs.items()}
    now: list = [None, None, {}]  # the settings last judged with, their side(), page verdicts

    def shown(q: Page) -> bool:
        """Every count of visible page `q` is still visible under the settings of now."""
        s = fresh() if fresh else settings
        if s != now[0]:
            now[:] = [s, _sides(s, llm, days)[0], {}]
        if q.id not in now[2]:
            now[2][q.id] = all(now[1](d, k) is False for d, k in q.src)
        return now[2][q.id]
    order = sorted(allp.values(), key=lambda p: (-p.visits, p.site, p.hidden))
    todo = [p for p in order if p.titles(1) and (
        _valid_desc(e := (llm.get(p.site) or {}).get("h" if p.hidden else "v"), p) is None
        or e.get("pv") != DESC_PROMPT)]
    say(f"deepen: {len(order)} site pages, {len(todo)} to describe")
    c: Counter = Counter()
    for n, p in enumerate(todo):
        if budget is not None and not budget.ok():
            c["left"] = len(todo) - n
            break
        # S3: who may see the page is asked again before every call, as runner_for does
        r = pick("local" if p.hidden or not shown(p) else "cloud") if pick else runner
        if r is None:
            c["no_runner"] += 1
            continue
        c["asked"] += 1
        cloud = r is not runner
        titles = p.titles(LLM_TITLES)
        pool = [q for q in order if q.site != p.site and (p.hidden or not q.hidden)]
        near = sorted(pool, key=lambda q: (-p.co.get(q.id, 0), q.category != p.category,
                                           -q.visits, q.site))
        cands = list(dict.fromkeys(q.site for q in near if not cloud or shown(q))
                     )[:LLM_CANDIDATES]
        # S6: the cloud hears only the hubs this site is linked to, never the owner's list
        topics = sorted(names[h] for h in p.hubs if h in names) if cloud \
            else sorted(hubs)[:60]
        got = r.site(p.site, titles, list(LABELS), cands, topics, budget=budget)
        entry = llm.setdefault(p.site, {})
        if not isinstance(got, dict):
            c["failed"] += 1
            continue
        cat = got.get("category")
        cat = cat.strip().lower() if isinstance(cat, str) else None
        # the LLM may place a site the rules do not; it never moves one out of a sensitive
        # category (site_category checks the host first), and only names a known category
        if cat in LABELS and site_category(p.site, {}) == "other":
            entry["category"] = cat
        rel = got.get("related") if isinstance(got.get("related"), list) else []
        # a small model pads the list: keep a site only if it shares the category or a hub
        like = {q.site for q in pool if q.category == p.category or set(q.hubs) & set(p.hubs)}
        by = {s.lower(): s for s in cands if s in like}
        tops = got.get("topics") if isinstance(got.get("topics"), list) else []
        entry["h" if p.hidden else "v"] = {
            "desc": _clean(got.get("description")) or "",
            "fed": [_h(t) for t in titles],
            "pv": DESC_PROMPT,
            "related": list(dict.fromkeys(by[x.strip().lower()] for x in rel
                                          if isinstance(x, str) and x.strip().lower() in by)
                            )[:3],
            "topics": list(dict.fromkeys(hubs[x.strip().lower()] for x in tops
                                         if isinstance(x, str) and x.strip().lower() in hubs))[:3]}
        c["described"] += 1
        if c["asked"] % SAVE_EVERY == 0:
            _save(vault, CACHE, {"llm": llm, "hashes": cache["hashes"]})
            say(f"deepen: {c['asked']} of {len(todo)} asked, {c['described']} described")
    _save(vault, CACHE, {"llm": llm, "hashes": cache["hashes"]})
    say(f"deepen: {c['described']} described, {c['failed']} unanswered, "
        f"{c.get('left', 0)} left for next time")
    r = write(vault, fresh() if fresh else settings, tz, say=say, days=days, bad=bad)
    return {"asked": c["asked"], "described": c["described"], "failed": c["failed"],
            "left": c.get("left", 0), "no_runner": c["no_runner"], **r}


def update_vectors(vault: Vault, say=None) -> dict | None:
    """The private browse vectors (cortex.browse.vectors), brought up to date; a failure is
    reported by type, never raised (the pages are already written)."""
    say = say or (lambda s: None)
    try:
        from cortex.browse import vectors

        return vectors.update(vault, progress=say)
    except Exception as e:
        say("private browse search not updated: "
            + (str(e) if isinstance(e, VaultError) else type(e).__name__))
        return None
