"""Keepfold's own browsing store, `<vault>/.keepfold/browse/store.db` (docs/design/v8/PLAN.md
task 6; part of docs/design/v7-review/SPEC-browse-events.md).

Every visit that passed the §2 filter is kept once, keyed by its source and that source's own
key (`native`: a browser's visit id and raw time, a hash of an extension or Google activity
record), so an import reads only what is new and saving a visit twice changes nothing. A row
holds the filter's verdict before the settings' actions (`privacy.judge`): who may see it
(hidden categories, private domains) is decided when the pages are made, from the settings of
that moment. A record a Don't-save category, the deny or allow list or a hard rule drops is
never stored, and `forget.purge` deletes the rows a rule matches (secure_delete overwrites
them on disk). SQLite in WAL mode with a busy timeout; it is Keepfold's own state under
`.keepfold/`, so it is written by SQLite's transactions, not `keepfold.vault`'s temp files.

Tables: meta (among others "settings": the fingerprint of the settings the rows were filtered
under); sources (a browser profile's cursor: newest visit id and raw time read, moved with
each saved chunk, and its History file's size and mtime once read whole); visits; files
(inbox and archive files already read); dirty (days whose pages must be made again; a run
clears each day once its pages and site counts are saved, so a stopped run goes on where it
was); unsummarised (days made while the local model was away); verdicts (the local model's
label per text hash, C1); hub_days (public time per topic hub and day, for the weekly
interest lines); site_pages (one row per site and side: what the site Markdown pages held,
once `migrate` moved them out of the vault: the per-site numbers the brain and Chat read, so
no reader ever scans the visits; `prof` holds what the CSVs and profiles need, so they are
made from rows); site_days (`sites.record`'s counts per day and site key, with a lean copy
for Chat; site-days.json is read in once by `legacy.migrate` through `migrate_days`); judged
(`ingest`'s privacy memo). Indexes: visits by day and by text hash; a first import into an
empty store makes them after its rows (`bulk`).

Not built yet (SPEC-browse-events): the tombstone ledger (a forget deletes rows, it is not a
remembered rule) and the source manager (sources are the browsers found, "extension" and
"takeout/...").
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import zipfile
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit

from keepfold.browse import privacy, sites
from keepfold.browse.privacy import Kept, Settings
from keepfold.events import local_day
from keepfold.vault import Vault, VaultError, sqlite_ro_uri

log = logging.getLogger(__name__)

PATH = ".keepfold/browse/store.db"
EXT = "extension"
CHUNK = 1000  # rows read and saved per transaction: a stop keeps every chunk before it
SEEN_CAP = 50_000  # filter verdicts kept across chunks (`ingest`)
BACKUPS = ".keepfold/backups"

_DDL = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS sources(key TEXT PRIMARY KEY, id_cursor INTEGER,
    time_cursor INTEGER, stat TEXT, read_at TEXT);
CREATE TABLE IF NOT EXISTS visits(
    source TEXT NOT NULL, native TEXT NOT NULL, ts REAL NOT NULL, day TEXT NOT NULL,
    url TEXT NOT NULL, title TEXT NOT NULL, host TEXT NOT NULL, site TEXT NOT NULL,
    category TEXT, maybe INTEGER NOT NULL DEFAULT 0, search TEXT, engine TEXT, video TEXT,
    channel TEXT NOT NULL DEFAULT '', excerpt TEXT NOT NULL DEFAULT '',
    secs REAL NOT NULL DEFAULT 0, tkey TEXT, PRIMARY KEY(source, native));
CREATE TABLE IF NOT EXISTS files(name TEXT PRIMARY KEY, stat TEXT);
CREATE TABLE IF NOT EXISTS dirty(day TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS unsummarised(day TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS verdicts(h TEXT PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hub_days(day TEXT NOT NULL, hub TEXT NOT NULL, secs REAL NOT NULL,
    last REAL NOT NULL, PRIMARY KEY(day, hub));
CREATE TABLE IF NOT EXISTS site_pages(site TEXT NOT NULL, hidden INTEGER NOT NULL,
    category TEXT, visits INTEGER, secs INTEGER, first INTEGER, last INTEGER, days TEXT,
    titles TEXT, searches TEXT, led TEXT, hubs TEXT, related TEXT, description TEXT,
    prof TEXT, PRIMARY KEY(site, hidden));
CREATE INDEX IF NOT EXISTS site_pages_last ON site_pages(last);
CREATE TABLE IF NOT EXISTS site_days(day TEXT NOT NULL, key TEXT NOT NULL, site TEXT NOT NULL,
    lean TEXT NOT NULL, counts TEXT NOT NULL, PRIMARY KEY(day, key));
CREATE INDEX IF NOT EXISTS site_days_site ON site_days(site);
CREATE TABLE IF NOT EXISTS judged(h TEXT PRIMARY KEY, kept TEXT NOT NULL);
"""
_INDEXES = (("visits_day", "visits(day, ts)"), ("visits_tkey", "visits(tkey)"))
_COLS = ("source", "native", "ts", "day", "url", "title", "host", "site", "category", "maybe",
         "search", "engine", "video", "channel", "excerpt", "secs", "tkey")
# a row saved again counts as new only when its time on the page or its title changed (the
# last visit of a read is measured again on the next one)
_UPSERT = (f"INSERT INTO visits({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))}) "
           "ON CONFLICT(source, native) DO UPDATE SET secs = excluded.secs, "
           "title = excluded.title, excerpt = excluded.excerpt, channel = excluded.channel "
           "WHERE visits.secs != excluded.secs OR visits.title != excluded.title "
           "RETURNING day, ts")


def exists(vault: Vault) -> bool:
    return (vault.root / PATH).is_file()


def connect(vault: Vault, *, memory: bool = False) -> sqlite3.Connection:
    """The store, its tables made when missing. `memory`: an in-memory copy (a dry run writes
    nothing to disk)."""
    p = vault.root / PATH
    if memory:
        con = sqlite3.connect(":memory:")
        if p.is_file():
            with closing(sqlite3.connect(sqlite_ro_uri(p), uri=True)) as src:
                src.backup(con)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(p, timeout=5)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA secure_delete=ON")  # a forgotten row is overwritten, not left in a page
    con.executescript(_DDL)
    if "prof" not in {r[1] for r in con.execute("PRAGMA table_info(site_pages)")}:
        try:  # a store made before the column
            con.execute("ALTER TABLE site_pages ADD COLUMN prof TEXT")
        except sqlite3.OperationalError as e:  # another process added it meanwhile
            if "duplicate column" not in str(e):
                raise
    indexes(con)
    return con


def indexes(con: sqlite3.Connection, make: bool = True) -> None:
    """Make the visits' indexes (when missing), or with `make` False drop them: a first
    import into an empty store saves its rows first and makes them once after (`bulk`)."""
    for name, on in _INDEXES:
        con.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {on}" if make
                    else f"DROP INDEX IF EXISTS {name}")


@contextmanager
def bulk(con: sqlite3.Connection):
    """Around a first import into an empty store: the visits' indexes go while the rows are
    saved and are made once after (a stop or crash in between: `connect` makes them)."""
    empty = con.execute("SELECT 1 FROM visits LIMIT 1").fetchone() is None
    if empty:
        indexes(con, make=False)
    try:
        yield
    finally:
        if empty:
            indexes(con)


def version(vault: Vault) -> tuple:
    """A stamp that changes whenever the store does (its file and WAL's size and mtime): the
    read API's callers cache by it."""
    out = []
    for suffix in ("", "-wal"):
        try:
            st = (vault.root / (PATH + suffix)).stat()
            out += [st.st_size, st.st_mtime_ns]
        except OSError:
            out += [0, 0]
    return tuple(out)


def get_meta(con: sqlite3.Connection, k: str, default=None):
    row = con.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    try:
        return json.loads(row[0]) if row else default
    except ValueError:
        return default


def set_meta(con: sqlite3.Connection, k: str, v) -> None:
    con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, json.dumps(v)))


# ---- saving visits ------------------------------------------------------------------------

def cursor(con: sqlite3.Connection, key: str) -> tuple[int | None, int | None, str | None]:
    """(newest visit id, newest raw time, History file stat) read from source `key` before."""
    row = con.execute("SELECT id_cursor, time_cursor, stat FROM sources WHERE key = ?",
                      (key,)).fetchone()
    return (row[0], row[1], row[2]) if row else (None, None, None)


def set_cursor(con: sqlite3.Connection, key: str, id_: int | None, raw: int | None,
               stat: str | None, now: datetime) -> None:
    old_id, old_raw, _ = cursor(con, key)
    con.execute("INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?, ?)", (
        key, max(x for x in (old_id, id_, -1) if x is not None) if (old_id, id_) != (None, None)
        else None, max(x for x in (old_raw, raw, -1) if x is not None)
        if (old_raw, raw) != (None, None) else None, stat, now.strftime("%Y-%m-%dT%H:%M:%SZ")))


def file_stat(path) -> str | None:
    """A History file's size and mtime, with its -wal's: when they are the same as last time,
    the browser wrote nothing and the file is not even copied."""
    out = []
    for p in (path, path.with_name(path.name + "-wal")):
        try:
            st = p.stat()
            out += [st.st_size, st.st_mtime_ns]
        except OSError:
            out += [None, None]
    return None if out[0] is None else json.dumps(out)


def text_of(k: Kept) -> str:
    """The text the local model is asked about (C1): a search term, or a title that is not
    just the host."""
    return k.search or (k.title if k.title != k.host else "")


_NEUTRAL = Settings()  # no deny, allow or private domains: `judge` before the user's lists


@functools.cache
def _lists_hash() -> str:
    from keepfold.browse import lists

    blob = json.dumps({k: v for k, v in vars(lists).items() if k.isupper()}, sort_keys=True,
                      default=lambda x: sorted(x) if isinstance(x, set | frozenset) else repr(x))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def memo_version() -> str:
    """What the privacy memo's verdicts depend on: the filter's version and its word lists."""
    from keepfold.browse import build

    return f"{build.FILTER_VERSION}-{_lists_hash()}"


def _memo_key(rec: tuple) -> str:
    return hashlib.sha256("\0".join(x or "" for x in rec).encode("utf-8", "replace")
                          ).hexdigest()[:32]


def _settle(k: Kept | None, settings: Settings) -> Kept | None:
    """`judge` under `settings` from `judge` under _NEUTRAL: the deny and allow lists drop by
    host, the private list makes it private."""
    if k is None or privacy.in_domains(k.host, settings.deny) or (
            settings.allow and not privacy.in_domains(k.host, settings.allow)):
        return None
    return replace(k, private=True) if privacy.in_domains(k.host, settings.private) else k


def _memo(con: sqlite3.Connection, keys: list[str]) -> dict[str, Kept]:
    """The memoized neutral verdicts of `keys` (a record not memoized is absent). A memo made
    under another `memo_version` is emptied first."""
    v = memo_version()
    if get_meta(con, "judged_version") != v:
        with con:
            con.execute("DELETE FROM judged")
            set_meta(con, "judged_version", v)
    out = {}
    for chunk in _chunks(keys, 500):
        for h, kept in con.execute(f"SELECT h, kept FROM judged WHERE h IN "
                                   f"({', '.join('?' * len(chunk))})", chunk):
            try:
                d = json.loads(kept)
                d.pop("channel", None)
                out[h] = Kept(**d)
            except (ValueError, TypeError):  # judged again
                pass
    return out


def ingest(con: sqlite3.Connection, visits: list, settings: Settings, tz, *,
           quiet_until: float | None = None, seen: dict | None = None) -> Counter:
    """Filter `visits` (sources.Visit with `.source` and `.native`) and save the ones kept,
    CHUNK per transaction. A new or changed row marks its day dirty, except a row no newer
    than `quiet_until` (epoch seconds: a vault imported before the store existed already has
    those days' pages). `seen`: the filter's verdicts by record, kept across calls (the filter
    is pure, so a record is judged once). The store's memo (table judged) keeps `judge`'s
    verdict before the user's domain lists by a hash of the record, for kept records only (a
    record that is not stored leaves no trace), so a record read again in a later run is not
    judged again. Counts: "dropped" (never stored), "new" (rows new or changed)."""
    c: Counter = Counter()
    seen = {} if seen is None else seen
    if len(seen) > SEEN_CAP:  # bounded however long the history: judged again after
        seen.clear()
    keys = {rec: _memo_key(rec) for rec in {(v.url, v.title, v.channel) for v in visits}
            if rec not in seen}
    memo = _memo(con, list(keys.values()))
    add, stale, rows = [], [], []
    for v in visits:
        rec = (v.url, v.title, v.channel)
        if rec not in seen:
            h = keys[rec]
            k = memo.get(h) or privacy.judge(v.url, v.title, settings=_NEUTRAL,
                                             channel=v.channel)
            ks = _settle(k, settings)
            a = privacy.apply(ks, settings=settings)
            if a is None and h in memo:
                stale.append((h,))
            elif a is not None and h not in memo:
                add.append((h, json.dumps({**asdict(k), "channel": v.channel or ""},
                                          ensure_ascii=False)))
            seen[rec] = (ks, a)
        k, a = seen[rec]
        if a is None:
            c["dropped"] += 1
            continue
        text = text_of(k)
        rows.append((v.source, v.native, v.start.timestamp(), local_day(v.start, tz), k.url,
                     k.title, k.host, sites.site_of(k.host, urlsplit(k.url).path), k.category,
                     int(k.maybe), k.search, k.engine, k.video, v.channel or "",
                     "" if a.private else v.excerpt or "", float(v.secs),
                     privacy.text_key(text) if text else None))
    for i in range(0, len(rows), CHUNK):
        with con:
            days = set()
            for r in rows[i:i + CHUNK]:  # one by one: executemany returns no RETURNING rows
                for day, ts in con.execute(_UPSERT, r).fetchall():
                    c["new"] += 1
                    if quiet_until is None or ts > quiet_until:
                        days.add(day)
            mark(con, days)
    if add or stale:  # after the rows: a memo row's record is stored
        with con:
            con.executemany("INSERT OR REPLACE INTO judged VALUES (?, ?)", add)
            con.executemany("DELETE FROM judged WHERE h = ?", stale)
    return c


def native_of(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode("utf-8", "replace")).hexdigest()[:20]


def ingest_files(con: sqlite3.Connection, vault: Vault, settings: Settings, tz, folders,
                 *, quiet_until: float | None = None) -> Counter:
    """The extension's inbox files (and the archive of consumed ones) not read since they last
    changed. A record's key is a hash of its start, URL and title, so the same record in the
    inbox and later in the archive is one row. The extension is gone (0.3.2) and no new files
    arrive; the ones it left still import."""
    from keepfold.browse import build

    c: Counter = Counter()
    for folder in folders:
        for p in sorted((vault.root / folder).glob("inbox-*.jsonl")):
            name = f"{folder}/{p.name}"
            try:
                st = p.stat()
            except OSError:
                continue
            stat = json.dumps([st.st_size, st.st_mtime_ns])
            row = con.execute("SELECT stat FROM files WHERE name = ?", (name,)).fetchone()
            if row and row[0] == stat:
                continue
            visits = []
            for line in p.read_bytes().decode("utf-8", "replace").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict) or not isinstance(r.get("url"), str):
                    continue
                vs = build.inbox_visits([r])
                for v in vs:
                    v.native = native_of(r.get("start"), r["url"], r.get("title") or "")
                visits += vs
            c.update(ingest(con, visits, settings, tz, quiet_until=quiet_until))
            with con:
                con.execute("INSERT OR REPLACE INTO files VALUES (?, ?)", (name, stat))
    return c


# ---- dirty days ---------------------------------------------------------------------------

def mark(con: sqlite3.Connection, days) -> None:
    con.executemany("INSERT OR IGNORE INTO dirty VALUES (?)", [(d,) for d in days])


def dirty(con: sqlite3.Connection) -> set[str]:
    return {d for (d,) in con.execute("SELECT day FROM dirty")}


def clean(con: sqlite3.Connection, days) -> None:
    with con:
        con.executemany("DELETE FROM dirty WHERE day = ?", [(d,) for d in days])


def days(con: sqlite3.Connection) -> set[str]:
    """Every day the store holds a visit of."""
    return {d for (d,) in con.execute("SELECT DISTINCT day FROM visits")}


def browsing_days(con: sqlite3.Connection) -> set[str]:
    """Days a browser or the extension holds a visit of (Google activity counts on others)."""
    return {d for (d,) in con.execute(
        "SELECT DISTINCT day FROM visits WHERE source NOT LIKE 'takeout%'")}


def partial(con: sqlite3.Connection) -> set[str]:
    """Days the store holds only part of (a browser had expired their first hours before the
    store first read it): their pages, made earlier, keep what they had."""
    return set(get_meta(con, "partial", []) or [])


def mark_unsummarised(con: sqlite3.Connection, days, on: bool) -> None:
    with con:
        if on:
            con.executemany("INSERT OR IGNORE INTO unsummarised VALUES (?)", [(d,) for d in days])
        else:
            con.executemany("DELETE FROM unsummarised WHERE day = ?", [(d,) for d in days])


def unsummarised(con: sqlite3.Connection, limit: int) -> list[str]:
    """Days made without the local model's lines, newest first."""
    return [d for (d,) in con.execute(
        "SELECT day FROM unsummarised ORDER BY day DESC LIMIT ?", (limit,))]


# ---- the pages' items ---------------------------------------------------------------------

def _kept(row, settings: Settings) -> Kept | None:
    """A stored row as the settings of now keep it: None when they drop it."""
    _, ts, day, url, title, host, cat, maybe, search, engine, video, *_ = row
    if privacy.in_domains(host, settings.deny) or (
            settings.allow and not privacy.in_domains(host, settings.allow)):
        return None
    k = Kept(url, title, host, cat, privacy.in_domains(host, settings.private), search, engine,
             video, bool(maybe))
    return privacy.apply(k, settings=settings)


def items(con: sqlite3.Connection, days_: set[str], settings: Settings) -> tuple[list, Counter]:
    """The build's items of `days_` (build.Item), as the settings of now keep them: an
    extension record merges into the History visit of the same scrubbed URL, minute and private
    verdict (its active time and excerpt win); a Google activity row counts only on a day no
    browser or extension visit holds."""
    from keepfold.browse.build import Item

    c: Counter = Counter()
    rows: list = []
    for chunk in _chunks(sorted(days_), 500):
        rows += con.execute(
            "SELECT source, ts, day, url, title, host, category, maybe, search, engine, video, "
            "channel, excerpt, secs FROM visits WHERE day IN "
            f"({', '.join('?' * len(chunk))})", chunk).fetchall()
    rank = {True: 0, False: 1}
    browsing = {r[2] for r in rows if not r[0].startswith("takeout")}
    rows = [r for r in rows if not r[0].startswith("takeout") or r[2] not in browsing]
    rows.sort(key=lambda r: (rank[r[0] != EXT], r[1]))
    out, at = [], {}
    for r in rows:
        k = _kept(r, settings)
        if k is None:
            c["dropped"] += 1
            continue
        start = datetime.fromtimestamp(r[1], UTC)
        key = (k.url, k.private, start.strftime("%Y%m%d%H%M"))
        if r[0] == EXT and key in at:
            hit = at[key]
            hit.secs = r[13] or hit.secs
            hit.excerpt = r[12] or hit.excerpt
            hit.channel = r[11] or hit.channel
            continue
        # which browser ("comet/Default" counts as "comet"), "extension" or "takeout/..."
        src = r[0] if r[0] == EXT or r[0].startswith("takeout") else r[0].split("/")[0]
        it = Item(r[2], start, r[13], k, r[12] if not k.private else "", r[11], src)
        out.append(it)
        at.setdefault(key, it)
    out.sort(key=lambda i: (i.start, i.k.url))
    return out, c


def _chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


# ---- the local model's verdicts (C1) ------------------------------------------------------

def verdicts(vault: Vault) -> dict[str, str]:
    """{text hash: label} the store holds; {} when there is no store."""
    if not exists(vault):
        return {}
    try:
        with closing(connect(vault)) as con:
            return dict(con.execute("SELECT h, label FROM verdicts"))
    except sqlite3.Error as e:
        log.warning("browse: verdicts not read from the store: %s", type(e).__name__)
        return {}


def put_verdicts(con: sqlite3.Connection, got: dict[str, str]) -> None:
    """Save labels and mark the days whose texts they are dirty (a title cleared as ordinary
    may now show on a visible page, a sensitive one moves to its category)."""
    if not got:
        return
    with con:
        con.executemany("INSERT OR REPLACE INTO verdicts VALUES (?, ?)", got.items())
        for chunk in _chunks(list(got), 500):
            con.execute("INSERT OR IGNORE INTO dirty SELECT DISTINCT day FROM visits WHERE tkey "
                        f"IN ({', '.join('?' * len(chunk))})", chunk)


def pending(con: sqlite3.Connection, settings: Settings, have: dict[str, str], limit: int
            ) -> dict[str, str]:
    """Up to `limit` texts the local model has not labelled yet, newest visit first:
    {text hash: text}. Only texts vet would ask about: no category, no private domain."""
    out: dict[str, str] = {}
    for h, search, title, host, _ in con.execute(
            "SELECT tkey, search, title, host, MAX(ts) AS m FROM visits WHERE tkey IS NOT NULL "
            "AND category IS NULL GROUP BY tkey ORDER BY m DESC"):
        if h in have or h in out or privacy.in_domains(host, settings.private):
            continue
        out[h] = search or title
        if len(out) >= limit:
            break
    return out


# ---- topic hubs per day (the weekly interest lines) ---------------------------------------

def put_hub_days(con: sqlite3.Connection, days_: set[str], rows: dict) -> None:
    """Replace the public time per hub of `days_`: rows {(day, hub): (secs, last ts)}."""
    with con:
        con.executemany("DELETE FROM hub_days WHERE day = ?", [(d,) for d in days_])
        con.executemany("INSERT INTO hub_days VALUES (?, ?, ?, ?)",
                        [(d, h, s, t) for (d, h), (s, t) in rows.items()])


def hub_weeks(con: sqlite3.Connection, mondays: set[str]) -> dict:
    """{(hub, monday): (secs, days, last day, last ts)} over the weeks of `mondays`."""
    out = {}
    for m in mondays:
        end = (date.fromisoformat(m) + timedelta(days=6)).isoformat()
        for hub, secs, n, last in con.execute(
                "SELECT hub, SUM(secs), COUNT(DISTINCT day), MAX(last) FROM hub_days "
                "WHERE day BETWEEN ? AND ? GROUP BY hub", (m, end)):
            day = con.execute("SELECT day FROM hub_days WHERE hub = ? AND last = ? AND day "
                              "BETWEEN ? AND ?", (hub, last, m, end)).fetchone()[0]
            out[(hub, m)] = (secs, n, day, last)
    return out


# ---- site pages (once migrated) -----------------------------------------------------------

def migrated(vault: Vault) -> bool:
    """True once `migrate` moved the site pages out of the vault: from then on the site pages
    live in the store only (`sites.write`)."""
    if not exists(vault):
        return False
    try:
        with closing(connect(vault)) as con:
            return bool(get_meta(con, "pages_migrated"))
    except sqlite3.Error:
        return False


def put_pages(con: sqlite3.Connection, allp: dict, llm: dict, only: set[str] | None = None
              ) -> None:
    """Replace the site page rows with `allp` (sites.pages: {(site, hidden): Page}): all of
    them, or with `only` the rows of those sites (a live read: the sites its days hold). A row
    also keeps what the CSVs and profiles read of its page (`prof`, `pages_for_lists`)."""
    ids = {} if only is None else {  # page id -> (site, hidden) of the rows that stay
        sites.page_id(s, bool(h)): (s, bool(h))
        for s, h in con.execute("SELECT site, hidden FROM site_pages") if s not in only}
    ids.update({p.id: (p.site, p.hidden) for p in allp.values()})
    rows = []
    for p in allp.values():
        entry = (llm.get(p.site) or {}).get("h" if p.hidden else "v")
        rel: list[str] = []
        for s in (entry or {}).get("related") or []:  # the LLM's first, then visited together
            for pid in (sites.page_id(s, False), sites.page_id(s, True)):
                if pid in ids and (p.hidden or not ids[pid][1]) and ids[pid][0] not in rel:
                    rel.append(ids[pid][0])
        for pid, _ in sorted(p.co.items(), key=lambda x: (-x[1], x[0])):
            q = ids.get(pid)
            if q and q[0] != p.site and (p.hidden or not q[1]) and q[0] not in rel:
                rel.append(q[0])
        hubs = Counter(p.hubs)
        for hub in (entry or {}).get("topics") or []:
            hubs.setdefault(hub, 0)
        pages = sorted(p.pages.values(), key=lambda x: (-x[1], -x[2], x[0]))
        prof = {"hours": p.hours, "weekdays": p.weekdays, "months": p.months,
                "msecs": p.month_secs}
        if p.hidden:  # only the private profile lists searches, by category
            terms = Counter()
            for cat, t, n in p.searches:
                terms[(cat, t)] += n
            prof["searches"] = sorted([c, t, n] for (c, t), n in terms.items())
        rows.append((p.site, int(p.hidden), p.category, p.visits, p.secs, p.first, p.last,
                     json.dumps(sorted({d for d, _ in p.src})),
                     json.dumps([t for t, *_ in pages if t][:sites.TOP_PAGES]),
                     json.dumps(sorted(p.here.items(), key=lambda x: (-x[1], x[0]))),
                     json.dumps(sorted(p.led.items(), key=lambda x: (-x[1], x[0]))),
                     json.dumps(sorted(hubs.items(), key=lambda x: (-x[1], x[0]))),
                     json.dumps(rel[:sites.TOP_RELATED]),
                     sites._valid_desc(entry, p) or "", json.dumps(prof)))
    with con:
        if only is None:
            con.execute("DELETE FROM site_pages")
        else:
            con.executemany("DELETE FROM site_pages WHERE site = ?", [(s,) for s in only])
        con.executemany(f"INSERT INTO site_pages VALUES ({', '.join('?' * 15)})", rows)


def unprofiled(con: sqlite3.Connection) -> bool:
    """A site page row saved before rows kept `prof`: the next write makes every row again."""
    return con.execute("SELECT 1 FROM site_pages WHERE prof IS NULL LIMIT 1").fetchone() is not None


def pages_for_lists(con: sqlite3.Connection) -> list:
    """Every site page row as a sites.Page holding what the CSVs and profiles read (no page
    map, titles or co-visits), so they are made without the per-day counts."""
    out = []
    for site, hidden, cat, visits, secs, first, last, hubs, prof in con.execute(
            "SELECT site, hidden, category, visits, secs, first, last, hubs, prof "
            "FROM site_pages"):
        pr = json.loads(prof or "{}")
        out.append(sites.Page(
            site, bool(hidden), cat, visits, secs, first, last,
            hubs=Counter({h: n for h, n in json.loads(hubs or "[]") if n > 0}),
            hours=Counter({int(k): n for k, n in pr.get("hours", {}).items()}),
            weekdays=Counter({int(k): n for k, n in pr.get("weekdays", {}).items()}),
            months=Counter(pr.get("months", {})), month_secs=Counter(pr.get("msecs", {})),
            searches=[tuple(x) for x in pr.get("searches", [])]))
    return out


# ---- per-day site counts (sites.load_days) ------------------------------------------------

def _replace_days(con: sqlite3.Connection, days_, rows: list) -> None:
    con.executemany("DELETE FROM site_days WHERE day = ?", [(d,) for d in days_])
    con.executemany("INSERT OR REPLACE INTO site_days VALUES (?, ?, ?, ?, ?)", rows)
    set_meta(con, "days_gen", int(get_meta(con, "days_gen", 0) or 0) + 1)


def put_days(con: sqlite3.Connection, days_, rows: list) -> None:
    """Replace the site counts of the days `days_` with `rows` [(day, key, site, lean,
    counts)] in one transaction, and move the "days_gen" stamp."""
    with con:
        _replace_days(con, days_, rows)


def migrate_days(con: sqlite3.Connection, days_, rows: list) -> bool:
    """`put_days` of site-days.json's counts, once per store: False when another process read
    them in first (checked again under the write lock, so its newer rows are never undone)."""
    con.execute("BEGIN IMMEDIATE")
    try:
        if get_meta(con, "days_migrated"):
            con.rollback()
            return False
        _replace_days(con, days_, rows)
        set_meta(con, "days_migrated", True)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return True


def day_rows(con: sqlite3.Connection, col: str = "counts", days_=None, sites_=None) -> list:
    """(day, key, `col`) of the stored site counts: all, of the days `days_` or of the sites
    `sites_`."""
    q = f"SELECT day, key, {col} FROM site_days"  # col: "counts" or "lean", never input
    if days_ is None and sites_ is None:
        return con.execute(q).fetchall()
    by, vals = ("day", days_) if days_ is not None else ("site", sites_)
    out: list = []
    for chunk in _chunks(sorted(vals), 500):
        out += con.execute(f"{q} WHERE {by} IN ({', '.join('?' * len(chunk))})",
                           chunk).fetchall()
    return out


_SITES_CACHE: dict = {}


def site_rows(vault: Vault, since: str | None = None, include_private: bool = False
              ) -> list[dict]:
    """The read API for the brain and Chat: one record per site, most visits first.
    `include_private=False` (anything Claude or a cloud model may read): the visible side
    only, with the titles and search terms its page showed (`sites._shown`: a search term only
    with the local model's "ordinary" verdict); a site with no visible side is left out.
    `include_private=True` (the owner's own screens, the local model): both sides as one
    record, `hidden` true when any part is. Each record: site, category, audience ("cloud" or
    "local"), hidden, visits, secs, first and last (epoch seconds), days (visit days, oldest
    first), searches [[term, n]] made on the site, led [[term, n]] searches that led there,
    titles (most visited first), hubs [[hub id, visits]], related (other sites the same
    reader may see), description. `since` (YYYY-MM-DD): sites visited on or after that day.
    Built from the site pages `sites.write` saves (after `migrate`); [] before."""
    if not exists(vault):
        return []
    key = (str(vault.root), version(vault), since)
    # one entry per side, swapped whole: the app reads both while unlocked, from two threads
    if (hit := _SITES_CACHE.get(include_private)) is not None and hit[0] == key:
        return hit[1]
    with closing(connect(vault)) as con:  # the JSON columns as one array: one parse a row
        rows = con.execute("SELECT site, hidden, category, visits, secs, first, last, '[' || "
                           "days || ',' || titles || ',' || searches || ',' || led || ',' || "
                           "hubs || ',' || related || ']', description FROM site_pages"
                           + ("" if include_private else " WHERE hidden = 0")).fetchall()
    by: dict[str, dict] = {}
    for site, hidden, cat, visits, secs, first, last, lists, desc in rows:
        days_, titles, here, led, hubs, rel = json.loads(lists)
        if since and (not days_ or days_[-1] < since):
            continue
        r = by.get(site)
        if r is None:
            by[site] = {"site": site, "category": cat, "hidden": bool(hidden),
                        "audience": "local" if hidden else "cloud", "visits": visits,
                        "secs": secs, "first": first, "last": last, "days": days_,
                        "titles": titles, "searches": here, "led": led, "hubs": hubs,
                        "related": rel, "description": desc or ""}
            continue
        # both sides of one site (include_private): one record, hidden when any part is
        r["hidden"] = r["hidden"] or bool(hidden)
        r["audience"] = "local" if r["hidden"] else "cloud"
        if hidden:
            r["category"] = cat
        r["visits"] += visits
        r["secs"] += secs
        r["first"] = min(r["first"] or first, first)
        r["last"] = max(r["last"], last)
        r["days"] = sorted(set(r["days"]) | set(days_))
        r["titles"] = list(dict.fromkeys(r["titles"] + titles))
        for f, extra in (("searches", here), ("led", led), ("hubs", hubs)):
            c = Counter(dict(map(tuple, r[f])))
            c.update(dict(map(tuple, extra)))
            r[f] = [list(x) for x in sorted(c.items(), key=lambda x: (-x[1], x[0]))]
        r["related"] = list(dict.fromkeys(r["related"] + rel))
        r["description"] = r["description"] or desc or ""
    out = sorted(by.values(), key=lambda r: (-r["visits"], r["site"]))
    for r in out:
        r["related"] = [s for s in r["related"] if s != r["site"]][:sites.TOP_RELATED]
    _SITES_CACHE[include_private] = (key, out)
    return out


# ---- moving the site pages out of the vault -----------------------------------------------

def _site_page_files(vault: Vault) -> list:
    """The site Markdown pages `sites.write` made (and the site indexes beside them)."""
    out = []
    for d in (sites.SITES_DIR, sites.HIDDEN_SITES_DIR):
        out += [p for p in sorted((vault.root / d).rglob("*.md")) if sites._ours(p)]
    return out


def _page_site(path) -> tuple[str, bool] | None:
    """(host, private) from a site page's frontmatter; None for an index page."""
    head = path.read_bytes()[:2000].decode("utf-8", "replace")
    m = re.search(r'^host: "?([^"\n]+)"?$', head, re.M)
    if not m:
        return None
    return m.group(1), bool(re.search(r"^private: true$", head, re.M))


MIGRATE_STEPS = ("saving the site rows", "zipping the site pages", "removing the site pages",
                 "making the profiles")


@sites.held
def migrate(vault: Vault, *, settings: Settings, dry_run: bool = False, step=None,
            today: str | None = None) -> dict:
    """The once-only move of the site Markdown pages into the store (the explicit step the app
    runs first): the site rows are made from the counts (`sites.write` into the store), every
    page on disk must then be in the store (else nothing moves, `missing` says how many),
    the pages go into `.keepfold/backups/browse-pages-<date>.zip` (checked: as many entries as
    pages), and only then are they removed from the vault. Safe to run again; `dry_run`
    reports what it would do and changes nothing. `step(n, done, total)` as each of
    MIGRATE_STEPS goes (n from 1). Under the browse-wide lock. Counts only."""
    from keepfold import taint
    taint.check_config("the browsing import")  # `settings` may be the defaults read in its place
    step = step or (lambda n, done, total: None)
    step(1, 0, 1)
    files = _site_page_files(vault)
    pages = [(p, _page_site(p)) for p in files]
    report = {"pages": sum(1 for _, s in pages if s), "files": len(files), "missing": 0,
              "zip": None, "removed": 0, "already": False}
    from keepfold.browse import legacy  # the counts from before the store first (F3)

    moved = {} if dry_run else legacy.migrate(vault)
    if moved.get(sites.DAYS, "migrated") != "migrated":  # torn or not read in: pages stay
        report["error"] = "site counts unreadable"
        return report
    with closing(connect(vault, memory=dry_run)) as con:
        if get_meta(con, "pages_migrated"):
            report["already"] = True
            if not files or dry_run:
                return report
        days, _ = sites.load_days(vault)
        llm = sites.load_cache(vault)["llm"]
        put_pages(con, sites.pages(vault, settings, days, llm), llm)
        held = {(s, bool(h)) for s, h in con.execute("SELECT site, hidden FROM site_pages")}
    report["sites"] = len(held)
    report["missing"] = sum(1 for _, s in pages if s and s not in held)
    if dry_run or report["missing"]:
        return report
    step(2, 0, 1)
    if files:
        report["zip"] = _zip(vault, files, today or date.today().isoformat())
        last = time.monotonic()
        for n, p in enumerate(files):
            report["removed"] += vault.remove(p)
            if time.monotonic() - last >= 0.5 or n + 1 == len(files):
                step(3, n + 1, len(files))
                last = time.monotonic()
        for d in (sites.SITES_DIR, sites.HIDDEN_SITES_DIR):
            _prune_dirs(vault.root / d)
    with closing(connect(vault)) as con, con:
        set_meta(con, "pages_migrated", today or date.today().isoformat())
    # the site rows, the profile and CSVs as the store keeps them; the pages' vectors go
    step(4, 0, 1)
    sites.write(vault, settings)
    from keepfold.browse import forget

    forget.prune_vectors(vault)
    step(4, 1, 1)
    return report


def _zip(vault: Vault, files: list, today: str) -> str:
    """Zip `files` (paths kept relative to the vault) into a new browse-pages zip; raise
    VaultError when the zip does not hold every one."""
    dest_dir = vault.root / BACKUPS
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"browse-pages-{today}.zip"
    n = 2
    while dest.exists():
        dest = dest_dir / f"browse-pages-{today}-{n}.zip"
        n += 1
    tmp = dest.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for p in files:
                z.write(p, p.relative_to(vault.root).as_posix())
        with zipfile.ZipFile(tmp) as z:
            if len(z.namelist()) != len(files) or z.testzip() is not None:
                raise VaultError("backup_failed", "The site pages' zip is incomplete; nothing "
                                 "was removed.")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest.relative_to(vault.root).as_posix()


def _prune_dirs(root) -> None:
    """Remove empty folders under `root` (and `root` when it ends up empty)."""
    if not root.is_dir():
        return
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        try:
            d.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def forget_zips(vault: Vault, rule) -> Counter:
    """Take the site pages a forget rule matches out of the browse-pages zips too: a page of a
    site the rule names, or holding a title or search it matches, goes whole."""
    c: Counter = Counter()
    llm = rule.llm or {}
    for zp in sorted((vault.root / BACKUPS).glob("browse-pages-*.zip")):
        try:
            with zipfile.ZipFile(zp) as z:
                entries = [(i, z.read(i)) for i in z.infolist()]
        except (OSError, zipfile.BadZipFile):
            continue
        keep = []
        for info, data in entries:
            text = data.decode("utf-8", "replace")
            m = re.search(r'^host: "?([^"\n]+)"?$', text, re.M)
            cat = re.search(r'^category: "?([^"\n]+)"?$', text, re.M)
            goes = bool(m) and rule.site(m.group(1), cat.group(1) if cat else "", llm)
            if m and not goes:
                host = sites.host_of(m.group(1))
                goes = any(rule.text(host, ln[2:].rsplit(" · ", 1)[0])
                           for ln in text.splitlines() if ln.startswith("- "))
            if goes:
                c["zip_pages"] += 1
            else:
                keep.append((info, data))
        if len(keep) == len(entries):
            continue
        tmp = zp.with_suffix(".zip.tmp")
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for info, data in keep:
                    z.writestr(info, data)
            os.replace(tmp, zp)
        finally:
            tmp.unlink(missing_ok=True)
    return c


def forget_rows(vault: Vault, rule, gone: dict, text_sig) -> Counter:
    """Delete the visits a forget rule matches (forget.purge): a host it names or the
    settings' lists drop, a title, search term or site in one of its categories. The days they
    sat on are made again on the next import; `gone[day]` gets each removed text's
    `text_sig`, so the day pages lose those lines now. A row the settings now keep private
    loses its excerpt."""
    c: Counter = Counter()
    if not exists(vault):
        return c
    llm = rule.llm or {}
    with closing(connect(vault)) as con:
        drop, blank, days_ = [], [], set()
        for (rid, day, url, title, host, site, cat, search, channel,
             excerpt) in con.execute("SELECT rowid, day, url, title, host, site, category, "
                                     "search, channel, excerpt FROM visits"):
            goes, k = _goes(rule, llm, url, title, host, site, cat, search, channel)
            if k is not None and k.private and excerpt:
                blank.append(rid)
            if goes:
                drop.append(rid)
                days_.add(day)
                for t in (title, search):
                    if t:
                        gone[day].add(text_sig(t))
        memo = []
        for h, kept in con.execute("SELECT h, kept FROM judged"):
            try:
                d = json.loads(kept)
                url, host = d["url"], d["host"]
                goes = _goes(rule, llm, url, d["title"], host,
                             sites.site_of(host, urlsplit(url).path), d.get("category"),
                             d.get("search"), d.get("channel") or "")[0]
            except (ValueError, KeyError, TypeError):  # unreadable: it goes too
                goes = True
            if goes:
                memo.append((h,))
        with con:
            con.executemany("DELETE FROM visits WHERE rowid = ?", [(r,) for r in drop])
            con.executemany("UPDATE visits SET excerpt = '' WHERE rowid = ?",
                            [(r,) for r in blank])
            con.executemany("DELETE FROM judged WHERE h = ?", memo)
            mark(con, days_)
            sites_gone = [(s, h) for s, h, cat in con.execute(
                "SELECT site, hidden, category FROM site_pages") if rule.site(s, cat or "", llm)]
            con.executemany("DELETE FROM site_pages WHERE site = ? AND hidden = ?", sites_gone)
            c["store_sites"] = len(sites_gone)
        if drop or blank or memo or sites_gone:  # the old page images leave the WAL too
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c["store_visits"] = len(drop)
        c["store_excerpts"] = len(blank)
        c["store_memo"] = len(memo)
    return +c


def _goes(rule, llm: dict, url: str, title: str, host: str, site: str, cat, search,
          channel: str) -> tuple[bool, Kept | None]:
    """`forget_rows`' test of one stored record: (it goes, the record as the rule's settings
    keep it when it stays)."""
    if (rule.host(host) or rule.site(site, cat or "", llm)
            or rule.text(host, title, search or "", channel, path=urlsplit(url).path)):
        return True, None
    if rule.settings is None:
        return False, None
    k = privacy.filter_record(url, title, settings=rule.settings, channel=channel)  # as
    return k is None, k  # Rule.record judges an inbox line


def by_day(con: sqlite3.Connection, days_: set[str] | None = None) -> dict[str, int]:
    """{day: visits} the store holds (of `days_` only, when given)."""
    if days_ is None:
        return dict(con.execute("SELECT day, COUNT(*) FROM visits GROUP BY day"))
    out: dict[str, int] = {}
    for chunk in _chunks(sorted(days_), 500):
        out.update(con.execute("SELECT day, COUNT(*) FROM visits WHERE day IN "
                               f"({', '.join('?' * len(chunk))}) GROUP BY day", chunk))
    return out


_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def day_ok(day) -> bool:
    return isinstance(day, str) and bool(_DAY.fullmatch(day))


def first_fill_partial(days_: dict[str, list], site_days: set[str], expiring: str) -> list:
    """The days a first read leaves partial for a vault imported before the store: up to the
    newest of the sources' oldest days (`days_`: per source, its days) that is old enough to be
    expiring; only days the counts already hold (they have pages)."""
    oldest = [min(ds) for ds in days_.values() if ds and min(ds) <= expiring]
    if not oldest:
        return []
    until = max(oldest)
    return sorted(d for d in site_days if day_ok(d) and d <= until)
