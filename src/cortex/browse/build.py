"""`cortex browse import` and `status` (docs/design/browse-brain.md §3, §4).

Visits from the browsers (and the extension's `.cortex/browse/inbox-*.jsonl`) pass the §2
filter first; only derived pages are written, through `cortex.vault`:
- `browse/browse-YYYY-MM-DD.md` (kind browse) per day, rebuilt whole from that day's visits:
  no third-party text at all (second privacy review), only the owner's topic hubs as links
  with time and counts, from items with no sensitive category that sit under a hub, and the
  number of other pages;
- `browse/private/browse-private-YYYY-MM-DD.md` (`private: true`) with every kept item of the
  day (titles by topic, searches, videos, sites); it sits in a subfolder, so the index,
  dashboard and chats never even list it; only the LOCAL LLM (localllm.enabled) hears its
  page titles, for its "What I was into" line and a line per browsing session;
- `browse/browse-index.md`: months and days, and the hubs by time; the sites returned to most
  go to `browse/private/browse-private-index.md`;
- per topic hub and week with >= 30 min or >= 3 days: a journal line and a `touch` event.
The public day pages, the public index, journal lines and touches are written only with
`browse.visible_to_claude: true`. By default (false) only the private pages are written (the
hub lines move to their top) and a rebuild removes the public pages of the days it rebuilds.
The extension's inbox files a run consumed are deleted afterwards. Every run then rewrites the
site pages and the profile (`cortex.browse.sites`); when the visibility settings changed since
the last run (`fingerprint`), every day still held is rebuilt and the public day pages that
cannot be are removed.
Ids carry a `browse-` prefix: a bare `YYYY-MM-DD` is the journal page's id, and ids are unique
across kinds. The state file `.cortex/browse/state.json` holds timestamps, sizes and mtimes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, tzinfo

import numpy as np

from cortex import topics
from cortex.browse import forget, lists, privacy, sites, sources
from cortex.browse.privacy import Kept, Settings
from cortex.events import EventLog, live, local_day, void_event
from cortex.render import esc, plain
from cortex.vault import (
    PRIVATE_BROWSE_DIR,
    Vault,
    VaultError,
    _dump_fm,
    _to_yaml,
    secret_pattern,
)

log = logging.getLogger(__name__)

DIR, PRIVATE_DIR = "browse", PRIVATE_BROWSE_DIR
EXPIRING_DAYS = 85  # a browser's oldest day this old is being expired (Chromium keeps 90)
STATE = ".cortex/browse/state.json"
INDEX_ID, PRIVATE_INDEX_ID = "browse-index", "browse-private-index"
# a page goes under its nearest hub only from this cosine: the floor calibrated for short text
# (prime's prompt vector) against the same member centroids; below it, an "Other" cluster
HUB_FLOOR = topics.TOUCH_FLOOR
JOIN = 0.75  # far pages join a cluster at this cosine to its centroid
INTEREST_SECS, INTEREST_DAYS = 30 * 60, 3
SHOWN, SITES, INDEX_SITES, SEARCHES = 20, 10, 30, 50
SUMMARY_TITLES = 15
DAY_TITLES = 60  # page titles a private summary may hear
SESSION_GAP = 30 * 60  # a browsing session ends after this long with no visit
SUMMARY_HEAD = "What I was into: "
EMBED_BATCH = 256
PROGRESS_S = 5.0  # a phase longer than this says how far it got (counts only)
_DUR = re.compile(r"^(?:(\d+) h)?\s*(?:(\d+) min)?$")
_SITE = re.compile(r"^- ([a-z0-9.-]+) · (.+)$")
_DAY_FILE = re.compile(r"^browse-(\d{4}-\d{2}-\d{2})\.md$")
_PDAY_FILE = re.compile(r"^browse-private-(\d{4}-\d{2}-\d{2})\.md$")
# the journal line `signals` writes, so Q12 can take it out again
_JOURNAL_LINE = re.compile(r"^- \d{2}:\d{2} Browsing: spent .+ this week \(\d+ days?\)"
                           r"(?:\r?\n|\Z)", re.M)
_HUB_LINE = re.compile(r"^- \[\[([^\]|]+)\]\] · ([^·]+?)(?: · |$)")


@dataclass
class Item:
    day: str
    start: datetime
    secs: float
    k: Kept
    excerpt: str = ""
    channel: str = ""
    browser: str = ""  # the Visit's source: a browser, "extension" or "takeout/…"


def _dur(secs: float) -> str:
    m = max(1, round(secs / 60))
    h, m = divmod(m, 60)
    return f"{m} min" if not h else f"{h} h {m} min" if m else f"{h} h"


def _secs(text: str) -> float:
    m = _DUR.match(text.strip())
    return (int(m.group(1) or 0) * 60 + int(m.group(2) or 0)) * 60.0 if m else 0.0


def _monday(day: str) -> str:
    d = date.fromisoformat(day)
    return (d - timedelta(days=d.weekday())).isoformat()


# ---- state --------------------------------------------------------------------------------

def load_state(vault: Vault) -> dict:
    try:
        s = json.loads((vault.root / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"sources": {}, "inbox": {}}
    return s if isinstance(s, dict) else {"sources": {}, "inbox": {}}


# ---- the extension's inbox ----------------------------------------------------------------

def _inbox_time(v) -> datetime | None:
    try:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return datetime.fromtimestamp(v / 1000, UTC)
        if isinstance(v, str):
            t = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return t if t.tzinfo else t.replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        pass
    return None


ARCHIVE = ".cortex/browse/inbox-archive"  # consumed inbox files, filtered (review C5)
# kept at least this long (Chrome keeps about this long): a browser not read this run (unticked,
# not chosen, not imported yet) or Takeout may bring back an older day, whose rebuild needs it
ARCHIVE_DAYS = 90


def read_inbox(vault: Vault, folder: str = ".cortex/browse"
               ) -> tuple[list[sources.Visit], dict[str, list[int]]]:
    """The receiver's records as visits (secs = active time), and each file's [size, mtime].
    `folder=ARCHIVE` reads the archive of consumed files instead."""
    out, stats = [], {}
    for p in sorted((vault.root / folder).glob("inbox-*.jsonl")):
        try:
            st = p.stat()
            lines = p.read_bytes().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        stats[p.name] = [st.st_size, st.st_mtime_ns]
        recs = []
        for line in lines:
            try:
                recs.append(json.loads(line))
            except ValueError:
                continue
        out += inbox_visits(recs)
    return out, stats


def inbox_stats(vault: Vault) -> dict[str, list[int]]:
    """read_inbox's [size, mtime] per inbox file, without reading them."""
    out = {}
    for p in sorted((vault.root / ".cortex" / "browse").glob("inbox-*.jsonl")):
        try:
            st = p.stat()
        except OSError:
            continue
        out[p.name] = [st.st_size, st.st_mtime_ns]
    return out


def inbox_visits(records: list) -> list[sources.Visit]:
    """The receiver's records as visits (secs = active time); a malformed one is skipped."""
    out = []
    for r in records:
        if not isinstance(r, dict) or not isinstance(r.get("url"), str):
            continue
        start = _inbox_time(r.get("start"))
        ms = r.get("active_ms")
        if start is None:
            continue
        out.append(sources.Visit(
            start, r["url"], str(r.get("title") or ""),
            ms / 1000 if isinstance(ms, (int, float)) and not isinstance(ms, bool)
            and ms >= 0 else 0.0, source="extension",
            excerpt=str(r.get("excerpt") or "")[:2000],
            channel=str(r.get("channel") or "")[:200]))
    return out


# ---- the local model's verdict (C1) -------------------------------------------------------

VET_LABELS = (*lists.CATEGORIES, "ordinary")


def local_only(runner):
    """`runner` when it is the loopback local model, else None: never cloudllm's CloudRunner
    (a Runner subclass, so `isinstance` would pass it) nor a stand-in. The app's Cancel
    wrapper (`_inner`) is looked through."""
    from cortex import localllm

    inner = getattr(runner, "_inner", runner)
    if type(inner) is not localllm.Runner:
        return None
    try:
        localllm._check_url(inner.base)
    except ValueError:
        return None
    return runner


def vet(vault: Vault, items: list[Item], settings: Settings, runner, budget, *,
        ask: bool = True, save: bool = True, con=None, keep_local: bool | None = None,
        step=None) -> tuple[list[Item], Counter]:
    """C1, before any page is written: an uncategorised record's title or search term goes
    to the local model (`local_only(runner)`; only when `ask`), newest first, in batches
    (`titles.check`), and each batch's labels are saved by text hash in the store (`con`; a
    stop keeps them). A sensitive label makes it that category (skip drops it, a hidden one
    makes it private); "ordinary" clears it. With a local model set up (`keep_local`: default,
    a local runner given) and no verdict (not loaded, budget spent, no answer) a title stays
    local (`Kept.local`); without one, a title is judged by the word lists only. A search
    term is never shown on a visible page without an "ordinary" verdict (`sites._shown`).
    `step(done, total)` per batch. Counts only."""
    from cortex.browse import store, titles

    local = local_only(runner)
    keep = local is not None if keep_local is None else keep_local
    verdicts = sites.load_verdicts(vault)
    c: Counter = Counter()
    need: dict[str, str] = {}
    for it in reversed(items) if local is not None and ask else ():
        k = it.k
        text = store.text_of(k)
        if k.category is None and not k.private and text \
                and (h := privacy.text_key(text)) not in verdicts:
            need.setdefault(h, text)
    if need:
        got, n = titles.check(need, local, list(VET_LABELS), budget, step=step, save=(
            (lambda lab: store.put_verdicts(con, lab)) if save and con is not None else None))
        c["vet_asked"] += n["asked"]
        c["vet_calls"] += n["calls"]
        verdicts.update(got)
        if save and con is None and got:
            sites._save(vault, sites.VERDICTS, {**sites._load(vault, sites.VERDICTS), **got})
    out = []
    for it in items:
        k = it.k
        text = store.text_of(k)
        if k.category is not None or k.private or not text:
            out.append(it)
            continue
        v = verdicts.get(privacy.text_key(text))
        if v in lists.CATEGORIES:
            k = privacy.apply(replace(k, category=v, maybe=True), settings=settings)
            c["vet_sensitive"] += 1
            if k is None:
                c["dropped"] += 1
                continue
        elif v != "ordinary" and keep and not k.search:
            k = replace(k, private=True, local=True)
            c["vet_unsure"] += 1
        it.k = k
        out.append(it)
    return out, c


# ---- topics -------------------------------------------------------------------------------

def hubs(vault: Vault, embed, save: bool = True) -> tuple[list[str], np.ndarray, dict[str, str]]:
    """(hub ids, unit member centroids, hub titles) for hubs with enough members; the root
    topic map is left out (it is not a topic). `save=False` (a dry run) keeps the embedded
    heads out of the vault's vector cache."""
    from cortex import semantic
    from cortex.catalog import Catalog
    from cortex.index import Index

    idx = Index(vault, Catalog.load(vault))
    t = topics.tree(idx)
    dim = len(semantic._embed(embed, ["topic"])[0])
    # only the hub members' heads: the import never pays for embedding every page it wrote
    need = set().union(*topics.members(idx, t).values())
    ids, sums, _ = topics.centroids(idx, t, semantic.vectors(idx, embed, dim, budget=0.0,
                                                             only=need, save=save))
    keep = [n for n, h in enumerate(ids) if h != topics.ROOT]
    sums = sums[keep]
    unit = sums / np.maximum(np.linalg.norm(sums, axis=1, keepdims=True), 1e-9)
    return [ids[n] for n in keep], unit, {ids[n]: t.title.get(ids[n], ids[n]) for n in keep}


def _text(it: Item) -> str:
    return " ".join(f"{it.k.search or it.k.title or it.k.host} {it.excerpt[:500]}".split())


def _embed_all(embed, texts: list[str], say=None) -> np.ndarray:
    from cortex import semantic

    return semantic.embed_all(embed, texts, EMBED_BATCH,
                              say and (lambda s: say(f"topics: {s}")), "page titles")


def _label(titles: list[str], hosts: list[str]) -> str:
    from cortex.topics import _terms

    words = _terms([titles])[0]
    return " ".join(words) or Counter(hosts).most_common(1)[0][0]


def _clusters(vec: np.ndarray, far: list[int], say=None) -> list[list[int]]:
    """Greedy "Other" clusters of rows `far` of `vec`, in order: a row joins the group whose
    member-sum centroid is nearest at cosine >= JOIN, else starts its own. One matvec per row
    over running sums and norms (a Python loop over the groups was O(far x groups))."""
    groups: list[list[int]] = []
    cents = np.zeros((len(far), vec.shape[1]), vec.dtype)
    norms = np.zeros(len(far), vec.dtype)
    last = time.monotonic()
    for k, n in enumerate(far):
        if say and time.monotonic() - last >= PROGRESS_S:
            say(f"topics: grouped {k} of {len(far)} pages")
            last = time.monotonic()
        g = len(groups)
        sims = cents[:g] @ vec[n] / np.where(norms[:g] > 0, norms[:g], 1.0)
        j = int(np.argmax(sims)) if g else -1
        if j >= 0 and sims[j] >= JOIN:
            groups[j].append(n)
            cents[j] += vec[n]
        else:
            j = g
            groups.append([n])
            cents[j] = vec[n]
        norms[j] = np.linalg.norm(cents[j])
    return groups


def place(items: list[Item], embed, hub_ids: list[str], unit: np.ndarray, say=None
          ) -> dict[tuple[str, bool], tuple]:
    """Section per (scrubbed URL, private), so a private title never shapes a public one:
    ("hub", id) at cosine >= HUB_FLOOR to its nearest hub (searches by their term). Anything
    else is private (`public`) and keyed (url, True): ("other", label) of its cluster, or
    ("site", host) with no embedder or no hubs."""
    first: dict[tuple[str, bool], Item] = {}
    for i in items:
        first.setdefault((i.k.url, i.k.private), i)
    if embed is None or not hub_ids or not first:
        return {(u, True): ("site", i.k.host) for (u, _), i in first.items()}
    keys = list(first)
    texts = [_text(first[k]) for k in keys]
    uniq = list(dict.fromkeys(texts))  # one title on many URLs is embedded once
    at = {t: n for n, t in enumerate(uniq)}
    vec = _embed_all(embed, uniq, say)[[at[t] for t in texts]]
    cos = vec @ unit.T
    best = cos.argmax(axis=1)
    out: dict[tuple[str, bool], tuple] = {}
    far: list[int] = []
    for n, key in enumerate(keys):
        if cos[n, best[n]] >= HUB_FLOOR:
            out[key] = ("hub", hub_ids[best[n]])
        else:
            far.append(n)
    if say:
        say(f"topics: grouping {len(far)} pages under no topic hub")
    groups = _clusters(vec, far, say)
    for g in groups:
        ks = [first[keys[n]].k for n in g]
        label = _label([k.search or k.title for k in ks], [k.host for k in ks])
        for n in g:
            out.setdefault((keys[n][0], True), ("other", label))
    return out


def public(i: Item, where: dict) -> bool:
    """Public is the exception (privacy review H1): no sensitive category, maybe or private
    domain, and placed under an existing topic hub. Everything else goes to the private page."""
    return not i.k.private and (where.get((i.k.url, False)) or ("",))[0] == "hub"


# ---- pages --------------------------------------------------------------------------------

def _hub_rows(items: list[Item], where: dict) -> list[tuple[str, float, int, int, int]]:
    """[(hub id, seconds, pages, videos, searches)] over the public items, longest first.
    Searches add no time."""
    rows: dict[str, list] = {}
    for i in items:
        if not public(i, where):
            continue
        r = rows.setdefault(where[(i.k.url, False)][1], [0.0, set(), set(), set()])
        if i.k.search:
            r[3].add(i.k.search)
        else:
            r[0] += i.secs
            (r[2] if i.k.video else r[1]).add(i.k.video or i.k.url)
    out = [(h, s, len(p), len(v), len(t)) for h, (s, p, v, t) in rows.items()]
    return sorted(out, key=lambda r: (-r[1], r[0]))


def _counts(pages: int, videos: int, searches: int) -> str:
    return ", ".join(f"{n} {w if n == 1 else pl}" for n, w, pl in (
        (pages, "page", "pages"), (videos, "video", "videos"), (searches, "search", "searches"))
        if n)


def _hub_lines(rows: list, others: int) -> list[str]:
    out = [f"- [[{h}]] · " + (f"{_dur(s)} · " if s else "") + _counts(p, v, t)
           for h, s, p, v, t in rows]
    return out + ([f"- {others} other page{'s' if others > 1 else ''}"] if others else [])


def public_body(day: str, rows: list, others: int, summary: str) -> str:
    """The public day page (second privacy review): the owner's own topic hubs as links with
    time and counts, the number of other pages, and nothing a site, a title or a search wrote."""
    out = [f"# Browsing {day}", "", f"Up: [[{INDEX_ID}]]", "", SUMMARY_HEAD + summary, "",
           "## Topics", *_hub_lines(rows, others),
           "", "Details are on the private page in Obsidian."]
    return "\n".join(out) + "\n"


def _sections(items: list[Item], where: dict, titles: dict[str, str]):
    """[(heading, seconds, [(title, host, seconds, visits)])], longest first; a public and a
    private visit of one URL stay apart."""
    pages: dict[tuple[str, bool], list] = {}
    for i in items:
        if i.k.search:
            continue
        p = pages.setdefault((i.k.url, i.k.private), [i.k.title, i.k.host, 0.0, 0])
        p[0] = i.k.title or p[0]
        p[2] += i.secs
        p[3] += 1
    secs: dict[tuple, list] = defaultdict(list)
    for (u, priv), p in pages.items():
        secs[where.get((u, priv)) or where.get((u, True)) or ("site", p[1])].append(p)
    out = []
    for (kind, val), ps in secs.items():
        head = (titles.get(val, val) if kind == "hub" else f"Other: {val}" if kind == "other"
                else val.removeprefix("www."))
        ps.sort(key=lambda p: (-p[2], p[0], p[1]))
        out.append((head, sum(p[2] for p in ps), ps))
    out.sort(key=lambda s: (-s[1], s[0]))
    return out


def private_body(day: str, items: list[Item], where: dict, titles: dict[str, str],
                 tz: tzinfo | None, hub_lines: list[str] | None = None,
                 summary: str | None = None, sessions: list | None = None) -> str:
    """The private day page: every kept item of the day, sensitive or not, as topics (by hub
    title, with no link), searches, videos and top sites. Text from the browser goes through
    `render.esc`. The one-line summary is the local LLM's `summary` when there is one, else
    rule-made from the headings; `sessions` [(start, end, LLM line or None)] go under
    "## Sessions". `hub_lines` (when no public page is written) go on top under "## Hubs"."""
    sections = _sections(items, where, titles)
    out = [f"# Browsing {day} (private)", "",
           SUMMARY_HEAD + (esc(summary, 200) if summary else ", ".join(
               f"{esc(h, 60) or 'untitled'} ({_dur(s)})" for h, s, _ in sections[:3])
               or "nothing kept"), ""]
    if hub_lines:
        out += ["## Hubs", *hub_lines, ""]
    if sessions:
        out.append("## Sessions")
        for a, b, line in sessions:
            out += ["", f"### {a.astimezone(tz):%H:%M}–{b.astimezone(tz):%H:%M}"]
            if line:
                out.append(esc(line, 200))
        out.append("")
    out.append("## Topics")
    for head, secs, ps in sections:
        out += ["", f"### {esc(head, 80) or 'untitled'} · {_dur(secs)}"]
        for t, host, s, n in ps[:SHOWN]:
            out.append(f"- {esc(t) or host.removeprefix('www.')} · {host.removeprefix('www.')}"
                       f" · {_dur(s)}" + (f" · {n} visits" if n > 1 else ""))
        if len(ps) > SHOWN:
            out.append(f"- and {len(ps) - SHOWN} more")
    searches, seen = [], set()
    for i in items:
        if i.k.search and (i.k.search, i.k.engine) not in seen:
            seen.add((i.k.search, i.k.engine))
            searches.append(f"- {i.start.astimezone(tz):%H:%M} · {esc(i.k.search)} · "
                            f"{i.k.engine}")
    out += ["", "## Searches", *(searches[:SEARCHES] or ["- none"])]
    videos: dict[str, list] = {}
    for i in items:
        if i.k.video:
            v = videos.setdefault(i.k.video, [i.start, i.k.title, 0.0, ""])
            v[1] = i.k.title or v[1]
            v[2] += i.secs
            v[3] = i.channel or v[3]
    out += ["", "## Videos", *([f"- {s.astimezone(tz):%H:%M} · {esc(t) or 'untitled'}"
                                + (f" · {esc(ch, 60)}" if esc(ch, 60) else "") + f" · {_dur(x)}"
                                for s, t, x, ch in videos.values()] or ["- none"])]
    site: Counter = Counter()
    for i in items:
        site[i.k.host.removeprefix("www.")] += i.secs
    top = sorted(site.items(), key=lambda x: (-x[1], x[0]))[:SITES]
    out += ["", "## Top sites", *[f"- {h} · {_dur(s)}" for h, s in top]]
    return "\n".join(out) + "\n"


def _page_text(fields: dict, body: str) -> str:
    from ruamel.yaml.comments import CommentedMap

    fm = CommentedMap((k, _to_yaml(v)) for k, v in fields.items())
    return "---\n" + _dump_fm(fm, "\n") + "---\n" + body


def _old_summary(vault: Vault, path, inputs: str) -> str | None:
    """The local LLM's summary on the page as it stands when it was made from the same hubs,
    so a rebuild does not ask the LLM again (and stays the same)."""
    try:
        n = vault.read_path(path)
    except VaultError:
        return None
    if n.fields.get("browse_inputs") != inputs or n.fields.get("browse_llm") is not True:
        return None
    line = next((ln for ln in n.body.splitlines() if ln.startswith(SUMMARY_HEAD)), None)
    return line[len(SUMMARY_HEAD):] if line else None


def _summary(runner, budget, names: list[str], cap: int = SUMMARY_TITLES, *,
             escape: bool = True) -> str | None:
    """The local LLM's one line from `names`: the owner's own hub names for a public page
    (never a page title); page titles only for a private page."""
    if runner is None or not names or (budget is not None and not budget.ok()):
        return None
    got = runner.summarise(names[:cap], budget=budget)
    return (esc(got, 200) if escape else got) if got else None


def _sessions(items: list[Item]) -> list[list[Item]]:
    """The day's items split where nothing happened for SESSION_GAP (a visit's time counts)."""
    out: list[list[Item]] = []
    end = None
    for i in sorted(items, key=lambda i: i.start):
        if end is None or (i.start - end).total_seconds() >= SESSION_GAP:
            out.append([])
        out[-1].append(i)
        e = i.start + timedelta(seconds=i.secs)
        end = e if end is None or e > end else end
    return out


def _private_summaries(vault: Vault, path, items: list[Item], runner, budget):
    """The local LLM's private lines, from the page titles of the kept items (at most
    DAY_TITLES): (day line, [(session start, end, line)], {inputs hash: line}). Lines already
    on the page for the same titles are reused, so a rebuild does not ask again. Nothing at all
    when no line exists."""
    try:
        old = vault.read_path(path).fields.get("browse_llm")
    except VaultError:
        old = None
    old = old if isinstance(old, dict) else {}
    cache: dict[str, str] = {}

    def line(its: list[Item]) -> str | None:
        names = list(dict.fromkeys(i.k.title for i in its if i.k.title and not i.k.search))
        if not names:
            return None
        names = names[:DAY_TITLES]
        key = hashlib.sha256(json.dumps(names).encode()).hexdigest()[:16]
        got = old.get(key) if isinstance(old.get(key), str) else None
        got = got or _summary(runner, budget, names, DAY_TITLES, escape=False)
        if got:
            cache[key] = got
        return got

    day = line(items)
    sessions = [(s[0].start, max(i.start + timedelta(seconds=i.secs) for i in s), line(s))
                for s in _sessions(items)]
    return (day, sessions, cache) if cache else (None, [], {})


def _put(vault: Vault, path, pid: str, fields: dict, body: str) -> str:
    """Write a page unless it is the same; a line that looks like a secret is left out, and a
    page the vault refuses is skipped ("failed"), never the run."""
    body = "\n".join(ln for ln in body.split("\n") if not secret_pattern(ln))
    text = _page_text(fields, body)
    old = path.read_bytes().decode("utf-8") if path.is_file() else None
    if old == text:
        return "same"
    try:
        vault.write_text(path, text)
    except VaultError as e:
        log.warning("browse: %s not written: %s", pid, e.code)
        return "failed"
    return "written"


def write_day(vault: Vault, day: str, items: list[Item], where: dict, titles: dict[str, str],
              runner=None, budget=None, tz: tzinfo | None = None, *, visible: bool = False
              ) -> dict[str, str]:
    """Rebuild the day's public and private pages from all its items; a page with nothing to
    show is removed. Public: topic hubs, time and counts only, and only when `visible`
    (browse.visible_to_claude); otherwise those lines top the private page and any public page
    goes. Private: every item. Returns
    {"public"/"private": "written"|"same"|"removed"|"none"|"failed"}."""
    res = {}
    rows = _hub_rows(items, where)
    others = len({i.k.url for i in items if not i.k.private and not public(i, where)})
    pid, path = f"browse-{day}", vault.root / DIR / f"browse-{day}.md"
    if not rows or not visible:
        res["public"] = "removed" if vault.remove(path) else "none"
    else:
        inputs = hashlib.sha256(json.dumps([rows, others]).encode()).hexdigest()[:16]
        names = [titles.get(h, h) for h, *_ in rows]
        summary = _old_summary(vault, path, inputs) or _summary(runner, budget, names)
        fields = {"id": pid, "title": f"Browsing {day}", "kind": "browse", "date": day,
                  "browse_inputs": inputs}
        if summary:
            fields["browse_llm"] = True
        fallback = ", ".join(esc(titles.get(h, h), 60) + (f" ({_dur(s)})" if s else "")
                             for h, s, *_ in rows[:3])
        res["public"] = _put(vault, path, pid, fields,
                             public_body(day, rows, others, summary or fallback))
    pid = f"browse-private-{day}"
    path = vault.root / PRIVATE_DIR / f"{pid}.md"
    if not items:
        res["private"] = "removed" if vault.remove(path) else "none"
    else:
        # the private page, never shown to Claude, is the only one whose titles the LLM hears
        summary, sessions, cache = _private_summaries(vault, path, items, runner, budget)
        fields = {"id": pid, "title": f"Browsing {day} (private)", "kind": "browse",
                  "date": day, "private": True}
        if cache:
            fields["browse_llm"] = cache
        res["private"] = _put(vault, path, pid, fields, private_body(
            day, items, where, titles, tz, None if visible else _hub_lines(rows, others),
            summary, sessions))
    return res


def _days(folder, pattern: re.Pattern) -> list[tuple[str, str]]:
    """[(day, text)] of the day pages in `folder` whose file name matches `pattern`."""
    out = []
    for p in sorted(folder.glob("*.md")):
        if m := pattern.match(p.name):
            out.append((m.group(1), p.read_bytes().decode("utf-8", "replace")))
    return out


def _section_lines(text: str, heading: str) -> list[str]:
    inside, out = False, []
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line == heading
        elif inside:
            out.append(line)
    return out


def _put_index(vault: Vault, path, fields: dict, lines: list[str]) -> bool:
    text = _page_text(fields, "\n".join(lines) + "\n")
    if path.is_file() and path.read_bytes().decode("utf-8") == text:
        return False
    vault.write_text(path, text)
    return True


def _days_and_hubs(pages: list[tuple[str, str]], heading: str, prefix: str) -> list[str]:
    """Index lines: months and days (newest first, linked by `prefix`), and the hubs by time
    summed from each page's hub lines under `heading`."""
    hub = defaultdict(lambda: [0, 0.0])
    for _, text in pages:
        for line in _section_lines(text, heading):
            if m := _HUB_LINE.match(line):
                hub[m.group(1)][0] += 1
                hub[m.group(1)][1] += _secs(m.group(2))
    days = sorted((d for d, _ in pages), reverse=True)
    out = ["## Days"]
    for month in sorted({d[:7] for d in days}, reverse=True):
        out += ["", f"### {month}", *[f"- [[{prefix}{d}]]" for d in days
                                      if d.startswith(month)]]
    top = sorted(hub.items(), key=lambda x: (-x[1][1], -x[1][0], x[0]))[:INDEX_SITES]
    return out + ["", "## Topics by time",
                  *[f"- [[{h}]] · {n} day{'s' if n > 1 else ''} · {_dur(s)}"
                    for h, (n, s) in top]]


def write_index(vault: Vault, *, visible: bool = False) -> bool:
    """`browse/browse-index.md` from the public day pages on disk: months and days, newest
    first, and the topic hubs by time (no site, no title). The sites returned to most go to
    the private index, `browse/private/browse-private-index.md`. Not `visible`: no public
    index (an old one goes) and the private index also gets the days and hubs. True if either
    changed."""
    pub, priv = _days(vault.root / DIR, _DAY_FILE), _days(vault.root / PRIVATE_DIR, _PDAY_FILE)
    changed = False
    path = vault.root / DIR / f"{INDEX_ID}.md"
    if not pub or not visible:
        changed |= vault.remove(path)
    else:
        changed |= _put_index(vault, path, {"id": INDEX_ID, "title": "Browsing",
                                            "kind": "browse"},
                              ["# Browsing", "", *_days_and_hubs(pub, "## Topics", "browse-")])
    path = vault.root / PRIVATE_DIR / f"{PRIVATE_INDEX_ID}.md"
    if not priv:
        return vault.remove(path) or changed
    sites = defaultdict(lambda: [0, 0.0])
    for _, text in priv:
        for line in _section_lines(text, "## Top sites"):
            if s := _SITE.match(line):
                sites[s.group(1)][0] += 1
                sites[s.group(1)][1] += _secs(s.group(2))
    top = sorted(sites.items(), key=lambda x: (-x[1][0], -x[1][1], x[0]))[:INDEX_SITES]
    out = ["# Browsing (private)", ""]
    if not visible:
        out += [*_days_and_hubs(priv, "## Hubs", "browse-private-"), ""]
    out += ["## Sites I return to",
            *[f"- {h} · {n} day{'s' if n > 1 else ''} · {_dur(s)}" for h, (n, s) in top]]
    return _put_index(vault, path, {"id": PRIVATE_INDEX_ID, "title": "Browsing (private)",
                                    "kind": "browse", "private": True}, out) or changed


# ---- signals ------------------------------------------------------------------------------

def signals(vault: Vault, items: list[Item], where: dict, titles: dict[str, str],
            tz: tzinfo | None = None, con=None, days: set[str] | None = None) -> int:
    """§3 interest: per topic hub and week, >= 30 min or >= 3 days of public browsing earns one
    journal line and one `touch` event (keyed, so once). With the store (`con`), `items` are
    those of `days` only: their time per hub and day is saved, and the weeks are summed from
    every day saved. Returns how many were new."""
    weeks: dict[tuple[str, str], list] = {}
    for i in items:
        if i.k.search or not public(i, where):
            continue
        row = weeks.setdefault((where[(i.k.url, False)][1], _monday(i.day)), [0.0, set(), i])
        row[0] += i.secs
        row[1].add(i.day)
        if i.start > row[2].start:
            row[2] = i
    if con is not None:
        from cortex.browse import store

        per: dict[tuple[str, str], list] = {}
        for i in items:
            if not i.k.search and public(i, where):
                r = per.setdefault((i.day, where[(i.k.url, False)][1]), [0.0, 0.0])
                r[0] += i.secs
                r[1] = max(r[1], i.start.timestamp())
        store.put_hub_days(con, days if days is not None else {i.day for i in items}, per)
        weeks = {(hub, week): [secs, n, (day, datetime.fromtimestamp(t, UTC))]
                 for (hub, week), (secs, n, day, t) in store.hub_weeks(
                     con, {w for _, w in weeks} | {_monday(d) for d in days or ()}).items()}
    else:
        weeks = {k: [s, len(ds), (i.day, i.start)] for k, (s, ds, i) in weeks.items()}
    evlog, new = EventLog(vault), 0
    for (hub, week), (secs, n, (day, at)) in sorted(weeks.items()):
        key = f"browse:touch:{hub}:{week}"
        if (secs < INTEREST_SECS and n < INTEREST_DAYS) or evlog.has_key(key):
            continue
        vault.append_journal(day, f"{at.astimezone(tz):%H:%M}",
                             f"Browsing: spent {_dur(secs)} on {plain(titles.get(hub, hub), 80)}"
                             f" this week ({n} day{'s' if n > 1 else ''})")
        evlog.append(session="cli", kind="log", source="imported", key=key,
                     fields={"payload": {"kind": "touch", "ids": [hub]}}, now=at)
        new += 1
    return new


# ---- the run ------------------------------------------------------------------------------

PHASES = ("reading browsers", "saving new visits", "checking titles", "writing day pages",
          "updating search index")
RESUMMARISE = 7  # days made while the local model was away, redone per run once it is here


def _local_set_up() -> bool:
    """A local model is set up (`localllm.enabled`), loaded or not: then a title with no
    verdict yet stays local (C1), whether or not this run may ask the model."""
    from cortex import localllm

    try:
        return localllm.enabled()
    except Exception:  # an unreadable config: the safe side
        return True


@sites.held
def run(vault: Vault, srcs: list[sources.Source], settings: Settings, *, embed=None,
        runner=None, budget=None, since: str | None = None, now: datetime | None = None,
        tz: tzinfo | None = None, dry_run: bool = False, progress=None, say=None,
        takeout: list[sources.Visit] | None = None, step=None, ask: bool | None = None,
        summaries: bool = True, keep_local: bool | None = None) -> dict:
    """One import into the store (`cortex.browse.store`), then the pages of the days it
    changed. Counts only.
    1 reading browsers: each source from its cursor on (`sources.read_since`); a History file
    unchanged since the last run is not even copied. 2 saving new visits: through the §2
    filter into the store, a chunk per transaction, with the source's cursor in the last one;
    the extension's inbox files (and the archive of consumed ones) not read since they
    changed; `takeout` (Google activity, cortex.browse.takeout) too, which counts only on days
    no browser or inbox visit holds. 3 checking titles: the local model's verdicts (C1, `vet`)
    on the changed days' texts, in batches, when `ask` (default: visible to Claude and not a
    dry run). 4 writing day pages: every dirty day (new or changed rows, a forget, a verdict;
    all days on a settings change, from `since` on with it), newest first; each day is clean
    once its pages are written, so a stop goes on there next time. 5 updating search index:
    the browse index, the site pages (`cortex.browse.sites`, only when the counts or settings
    changed), the state.
    `runner` (the local LLM, laptop only) writes the public pages' one-line summary from hub
    names and the private pages' day and session summaries from their kept titles; with
    `summaries` False (the app's import job: the model's time goes to the titles) or no
    runner, a day made with a local model set up goes on a list and is redone, RESUMMARISE
    days a run, newest first, by a later run that has the model. `keep_local`: a local model
    is set up (default: a local runner or `localllm.enabled`). Without
    settings.visible_to_claude only the private pages are written: no public page or index,
    journal line or touch.
    A source that fails is skipped and keeps its cursor. `dry_run` writes nothing (an
    in-memory copy of the store) and counts the day pages it would write.
    `progress(key, visits, new, error)` per source; `step(phase, done, total)` (phase 1-5,
    PHASES) as each part goes. `srcs` are read as given: callers that did not pick browsers
    explicitly pass `sources.chosen(settings)` (S2). Runs under the browse-wide lock
    (`sites.busy`), so a forget never lands in the middle of it."""
    from cortex.browse import store

    now = now or datetime.now(UTC)
    vis = settings.visible_to_claude
    tell = say or (lambda s: None)  # counts-only progress, so a long run is never silent
    phase = step or (lambda n, done, total: None)
    state = load_state(vault)
    report: Counter = Counter()
    fp = fingerprint(settings)
    # who may see what changed (visibility, hidden or skipped categories, the domain lists,
    # the filter's own rules): everything stored is filtered again (C2) and every day redone
    reset = state.get("settings") != fp
    fresh = not store.exists(vault)
    if reset and not dry_run:
        tell("settings changed: filtering what is stored again")
        report.update({f"refiltered_{k}": n for k, n in forget.purge(
            vault, forget.Rule.of(settings), write=False).items()})
    if not dry_run:
        forget.sync_private(vault)
    keep_local = local_only(runner) is not None or _local_set_up() if keep_local is None \
        else keep_local
    ask = vis and not dry_run if ask is None else ask and not dry_run
    con = store.connect(vault, memory=dry_run)
    try:
        cursors = _read_and_save(vault, con, srcs, settings, tz, now, reset, fresh, state,
                                 takeout, report, tell, phase, progress)
        part = store.partial(con)
        held = store.days(con) - part
        if reset and not vis and not dry_run and (n := _void_signals(vault)):
            report["browse_voided"] += n
        if not dry_run and (gone := _drop_public(vault, None if not vis else held if reset
                                                 else "")):
            report["public_removed"] += gone
        with con:
            if reset:
                store.mark(con, held)
            if since:
                store.mark(con, {d for d in held if d >= since})
            if summaries and local_only(runner) is not None and not dry_run:
                store.mark(con, store.unsummarised(con, RESUMMARISE))
        todo = store.dirty(con) - part
        purged = reset
        days = bad = None
        if todo:
            got = _render(vault, con, todo, settings, embed=embed, runner=runner, budget=budget,
                          tz=tz, dry_run=dry_run, ask=ask, keep_local=keep_local,
                          summaries=summaries, report=report, tell=tell, phase=phase)
            if dry_run:
                return {**report, "days_from": _monday(min(todo)), "days": got,
                        "visible_to_claude": vis, "dry_run": True}
            days, bad, skipped = got
            purged |= skipped
        elif dry_run:
            return {**report, "visible_to_claude": vis, "dry_run": True}
        phase(5, 0, 1)
        if todo or report["public_removed"] or reset:
            report["index_changed"] = int(write_index(vault, visible=vis))
        stats = _archive_consumed(vault, inbox_stats(vault), settings)
        if not report["sources_failed"]:  # the store holds what the archive held
            report["archive_pruned"] = _prune_archive(
                vault, (now - timedelta(days=ARCHIVE_DAYS)).date().isoformat())
        if todo or purged or state.get("sites_stamp") != _sites_stamp(vault):
            tell("sites: making the site pages")
            if days is None:
                days, bad = sites.load_days(vault)
            # every time the counts change, so a settings change reaches the site pages
            report.update(sites.write(vault, settings, tz, say=say, days=days, bad=bad))
        if purged:
            report["refiltered_vectors"] = forget.prune_vectors(vault)["vectors"]
        vault.write_text(vault.root / STATE, json.dumps({
            "pending": None, "last_run": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "llm": runner.describe() if runner is not None else None,
            "embedder": embed is not None,
            "sources": {**(state.get("sources") or {}), **cursors},
            "settings": fp, "inbox": stats, "sites_stamp": _sites_stamp(vault)},
            indent=1, sort_keys=True))
        phase(5, 1, 1)
        return {**report, "visible_to_claude": vis}
    finally:
        con.close()


def _sites_stamp(vault: Vault) -> list:
    """The site counts and the local model's site cache as they stand (size, mtime): the
    site pages are made again only when one changed."""
    out = []
    for rel in (sites.DAYS, sites.CACHE):
        try:
            st = (vault.root / rel).stat()
            out += [st.st_size, st.st_mtime_ns]
        except OSError:
            out += [0, 0]
    return out


def _state_row(cursor: int | None, ran: str, stat: str | None) -> dict:
    """A source's row in the state (`status`, the app's auto import): its cursor, when it was
    read, its History file's size and mtime."""
    row: dict = {"cursor": cursor, "ran": ran}
    if stat:
        row["size"], row["mtime_ns"] = json.loads(stat)[:2]
    return row


def _read_and_save(vault: Vault, con, srcs, settings: Settings, tz, now: datetime,
                   reset: bool, fresh: bool, state: dict, takeout, report: Counter, tell,
                   phase, progress) -> dict:
    """Phases 1 and 2 of `run`: the new visits of every source, the inbox files and `takeout`
    into the store. The state's cursor rows of the sources read (for `status`)."""
    from cortex.browse import store

    got, cursors = [], {}
    ran = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for n, s in enumerate(srcs):
        phase(1, n, len(srcs))
        id_cur, t_cur, stat_old = store.cursor(con, s.key)
        stat = store.file_stat(s.path)
        if not reset and id_cur is not None and stat is not None and stat == stat_old:
            report["sources"] += 1  # the browser wrote nothing since: not even copied
            cursors[s.key] = _state_row(t_cur, ran, stat)
            if progress:
                progress(s.key, 0, 0, None)
            continue
        tell(f"{s.key}: reading its history")
        try:
            # a settings change reads everything again: a visit dropped before may pass now
            vs, top_t, top_id = sources.read_since(s, None if reset else id_cur,
                                                   None if reset else t_cur)
        except Exception as e:  # one bad source never stops the others
            log.warning("browse: %s not read: %s", s.key, type(e).__name__)
            report["sources_failed"] += 1
            if progress:
                progress(s.key, 0, 0, e)
            continue
        report["sources"] += 1
        got.append((s, vs, top_t, top_id, stat))
    phase(1, len(srcs), len(srcs))
    legacy = state.get("sources") or {}
    total, done, fills = sum(len(g[1]) for g in got), 0, {}
    for s, vs, top_t, top_id, stat in got:
        old = (legacy.get(s.key) or {}).get("cursor")
        # a vault imported before the store existed: the days up to the old cursor have pages
        quiet = (sources.to_time(s.browser, old).timestamp()
                 if fresh and isinstance(old, int) else None)
        tell(f"{s.key}: saving {len(vs)} visits")
        c = store.ingest(con, vs, settings, tz, quiet_until=quiet,
                         step=lambda d, t, base=done: phase(2, base + d, total),
                         before_commit=lambda s=s, a=top_id, b=top_t, st=stat: store.set_cursor(
                             con, s.key, a, b, st, now))
        done += len(vs)
        report["dropped"] += c["dropped"]
        report["visits_new"] += c["new"]
        if progress:
            progress(s.key, len(vs), c["new"], None)
        cursors[s.key] = _state_row(store.cursor(con, s.key)[1], ran, stat)
        if quiet is not None and vs:
            fills[s.key] = {local_day(v.start, tz) for v in vs}
    if fresh and state.get("last_run"):  # the archive's days were imported before
        store.ingest_files(con, vault, settings, tz, [ARCHIVE], quiet_until=float("inf"))
    c = store.ingest_files(con, vault, settings, tz, [".cortex/browse", ARCHIVE])
    report["dropped"] += c["dropped"]
    if fills:  # the first read of a vault imported before: its oldest days are partial
        expiring = local_day(now - timedelta(days=EXPIRING_DAYS), tz)
        part = store.first_fill_partial(fills, set(sites._load(vault, sites.DAYS)), expiring)
        if part:
            with con:
                store.set_meta(con, "partial", sorted(store.partial(con) | set(part)))
    if takeout:
        for v in takeout:
            v.native = store.native_of(v.start.isoformat(), v.url, v.title, v.source)
        c = store.ingest(con, takeout, settings, tz)
        have = store.browsing_days(con)
        report["takeout"] = sum(1 for v in takeout if local_day(v.start, tz) not in have)
        report["dropped"] += c["dropped"]
    phase(2, total, total)
    return cursors


def _render(vault: Vault, con, todo: set[str], settings: Settings, *, embed, runner, budget,
            tz, dry_run: bool, ask: bool, keep_local: bool, summaries: bool, report: Counter,
            tell, phase):
    """Phases 3 and 4 of `run` for the days `todo`: (site counts, the name a torn counts file
    went to, whether a new skip verdict purged anything); in a dry run, the number of days."""
    from cortex.browse import store

    vis = settings.visible_to_claude
    tell(f"filtering: the visits of {len(todo)} days")
    items, c = store.items(con, todo, settings)
    report.update(c)
    phase(3, 0, 1)
    if local_only(runner) is not None and ask:
        tell(f"local model: checking the titles and searches of {len(items)} visits")
    had = sites.load_verdicts(vault)
    items, c = vet(vault, items, settings, runner, budget, ask=ask, save=not dry_run, con=con,
                   keep_local=keep_local, step=lambda d, t: phase(3, d, t))
    report.update(c)
    # a text the model now files under a Don't-save category may sit on a day this run does
    # not make (kept unvetted before): every store loses it now
    skip = frozenset(v for h, v in sites.load_verdicts(vault).items()
                     if had.get(h) != v and privacy.audience(v, settings) == "skip")
    if skip and not dry_run:
        report.update({f"refiltered_{k}": n for k, n in forget.purge(
            vault, forget.Rule(cats=skip), write=False).items()})
    phase(3, 1, 1)
    phase(4, 0, len(todo))
    if embed is not None:
        tell("topics: reading the topic hubs")
    hub_ids, unit, titles = (hubs(vault, embed, save=not dry_run) if embed is not None
                             else ([], None, {}))
    tell(f"topics: placing {len(items)} visits under {len(hub_ids)} topic hubs")
    where = place(items, embed, hub_ids, unit, tell)
    for i in items:
        report["kept" if public(i, where) else "private"] += 1
    by_day: dict[str, list[Item]] = {d: [] for d in todo}
    for i in items:
        by_day.setdefault(i.day, []).append(i)
    if dry_run:
        for its in by_day.values():
            report["private_would"] += bool(its)
            report["public_would"] += bool(vis and _hub_rows(its, where))
            report["empty_would"] += not its
        return len(by_day)
    tell(f"sites: counting {len(items)} visits by site")
    days, bad = sites.load_days(vault)
    days = sites.record(vault, items, where, settings, tz, "", set(by_day), days)
    report["signals"] = signals(vault, items, where, titles, tz, con, set(by_day)) if vis else 0
    report["days_from"] = _monday(min(todo))  # type: ignore[assignment]
    asks = runner if summaries else None
    last = time.monotonic()
    for n, day in enumerate(sorted(by_day, reverse=True)):  # the newest first
        for side, what in write_day(vault, day, by_day[day], where, titles, asks, budget,
                                    tz, visible=vis).items():
            report[f"{side}_{what}"] += 1
        store.clean(con, [day])
        if keep_local and by_day[day]:  # its local-model lines are made once the model is here
            store.mark_unsummarised(con, [day], local_only(asks) is None
                                    or (budget is not None and not budget.ok()))
        phase(4, n + 1, len(by_day))
        if time.monotonic() - last >= PROGRESS_S:
            tell(f"day pages: {n + 1} of {len(by_day)} days")
            last = time.monotonic()
    return days, bad, bool(skip)


# bump when the filter's rules change, so stored browsing is re-filtered and every day the
# browsers still hold is rebuilt once (3: per-browser counts and Don't-save by the site's
# category, 0.2.3)
FILTER_VERSION = 3


def fingerprint(settings: Settings) -> str:
    """A hash of the settings (and FILTER_VERSION) that decide what a public page may hold."""
    return hashlib.sha256(json.dumps([
        FILTER_VERSION, settings.visible_to_claude, sorted(settings.hidden_from_claude),
        sorted(settings.skip_categories), settings.private, settings.deny, settings.allow
    ]).encode()).hexdigest()[:16]


def _void_signals(vault: Vault) -> int:
    """Q12: browsing went hidden from Claude, so void every live browse touch and take the
    browse lines out of the journal pages. How many touches were voided."""
    evlog = EventLog(vault)
    ids = [e["id"] for e in live(evlog.iter_events())
           if str(e.get("key") or "").startswith("browse:touch:")]
    for i in ids:
        void_event(evlog, session="cli", event_id=i, reason="browsing hidden from Claude",
                   source="imported")
    for p in vault.note_paths("journal"):
        if _JOURNAL_LINE.search(p.read_bytes().decode("utf-8", "replace")):
            vault._mutate(p, lambda text: _JOURNAL_LINE.sub("", text))
    return len(ids)


def _drop_public(vault: Vault, held: set[str] | str | None) -> int:
    """Remove the public day pages of the days no source holds (None: every one; "": none),
    the ones a run cannot rebuild after the visibility rules changed. How many went."""
    if held == "":
        return 0
    gone = 0
    for p in sorted((vault.root / DIR).glob("browse-*.md")):
        if (m := _DAY_FILE.match(p.name)) and (held is None or m.group(1) not in held):
            gone += vault.remove(p)
    return gone


def _archive_consumed(vault: Vault, stats: dict[str, list[int]], settings: Settings
                      ) -> dict[str, list[int]]:
    """Move each inbox file this run read whole and that is unchanged since, whatever its
    date (C16), into ARCHIVE (C5), so a later rebuild of its day still has the visits only
    the extension saw. Its records are filtered again on the way (the settings of now): a
    dropped one goes, a private one keeps no excerpt. The stats of the files that stay."""
    left = dict(stats)
    with vault.lock():  # the receiver appends under the same lock
        for name, st in stats.items():
            p = vault.root / ".cortex" / "browse" / name
            try:
                same = [p.stat().st_size, p.stat().st_mtime_ns] == st
                lines = p.read_bytes().decode("utf-8", "replace").splitlines()
            except OSError:
                continue
            if not same:
                continue
            keep = []
            for line in lines:
                try:
                    r = json.loads(line)
                    k = privacy.filter_record(str(r["url"]), str(r.get("title") or ""),
                                              settings=settings,
                                              channel=str(r.get("channel") or ""))
                except (ValueError, TypeError, KeyError):
                    continue
                if k is None:
                    continue
                if k.private and r.get("excerpt"):
                    r["excerpt"] = ""
                keep.append(json.dumps(r, ensure_ascii=False))
            if keep:
                a = vault.root / ARCHIVE / name
                old = a.read_bytes().decode("utf-8", "replace") if a.is_file() else ""
                vault.write_text(a, old + "".join(f"{ln}\n" for ln in keep))
            vault.remove(p)
            del left[name]
    return left


def _prune_archive(vault: Vault, horizon: str) -> int:
    """Delete the archived inbox files of days before `horizon` (the oldest day the browsers'
    History holds, or ARCHIVE_DAYS ago when that is older; a day of zone slack). How many
    went."""
    cut = (date.fromisoformat(horizon) - timedelta(days=1)).isoformat()
    gone = 0
    for p in sorted((vault.root / ARCHIVE).glob("inbox-*.jsonl")):
        m = re.search(r"\d{4}-\d{2}-\d{2}", p.name)
        if m and m.group(0) < cut:
            gone += vault.remove(p)
    return gone


def status(vault: Vault, tz: tzinfo | None = None, *, visible: bool = False) -> list[str]:
    """`cortex browse status` lines: the mode, last run, last run and cursor per source, the
    local LLM used."""
    s = load_state(vault)
    mode = ("visible to Claude: yes (public day pages, index and interest lines)" if visible
            else "visible to Claude: no (private pages only; browse.visible_to_claude)")
    if not s.get("last_run"):
        return [mode, "never imported; run `cortex browse selftest`, `cortex browse scan`, "
                "then `cortex browse import`"]
    out = [mode, f"last run: {s['last_run']}",
           f"local LLM: {s.get('llm') or 'none (rules and embeddings only)'}",
           f"embeddings: {'yes' if s.get('embedder') else 'no (sections by site)'}"]
    if s.get("pending"):
        out.append(f"a source failed last time; the next import rebuilds from {s['pending']}")
    for key, row in sorted((s.get("sources") or {}).items()):
        row = row if isinstance(row, dict) else {}
        cur = row.get("cursor")
        when = (sources.to_time(key.split("/", 1)[0], cur).astimezone(tz)
                .strftime("%Y-%m-%d %H:%M") if isinstance(cur, int) else "none")
        out.append(f"{key}: up to {when}, last read {row.get('ran') or 'unknown'}")
    out.append(f"extension inbox files: {len(s.get('inbox') or {})}")
    return out
