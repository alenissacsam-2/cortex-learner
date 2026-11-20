"""The app's live home (v8): the dashboard blob plus `feed`, `next_up` and `exams`, kept
between requests by one HomeCache per app server and sent to the page as deltas.

`version(vault)` stamps what the home reads, from directory listings only (no file is opened):
- events/<year>/<month>/*.jsonl: each file's name, size and mtime;
- each kind folder's *.md: each file's name, size and mtime; under browse/ its subfolders too,
  except browse/private/ (never read) and browse/sites/<category>/ (whole-vault walks skip the
  site pages);
- catalog.yaml, inbox.md, .cortex/brain-layout.json, .cortex/vectors.db and its -wal, and
  .cortex/recall-log.jsonl: size and mtime;
- the browsing store, .cortex/browse/store.db and its -wal: size and mtime (the brain's stem of
  websites; `store.version`);
- the Cortex day (04:00 local), so Today rolls over.
Left out: the time of day (an exam forecast moves with it and is refreshed with any other
change), the search index snapshot (a parse shortcut with the same content) and the shipped
help text. A file rewritten within one mtime tick at the same size is seen at its next change.
The stamp is a hash of these listings, valid within one process (the app server's).
"""

from __future__ import annotations

import base64
import logging
import os
import re
import sqlite3
import threading
from collections import OrderedDict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import numpy as np

from cortex import brainmap, dashboard, planner, recall_log, semantic, topics
from cortex.access import Audience, audience_of_note, audience_of_study, may_see
from cortex.browse import store
from cortex.catalog import CATALOG
from cortex.events import DAY_ROLLOVER, STUDY_MODES, live, local_day
from cortex.render import plain, term
from cortex.vault import ID_RE, PRIVATE_BROWSE_DIR, Vault, VaultError, is_private

log = logging.getLogger(__name__)

FILES = {"catalog": CATALOG, "inbox": "inbox.md", "layout": brainmap.LAYOUT,
         "vectors": semantic.CACHE, "vectors_wal": semantic.CACHE + "-wal",  # saves land here
         "recall": recall_log.FILE}
WEEK_DAYS = 7  # Today plus the six days before it
FEED_CAP = 200  # items in each of Today and This week
PER_GROUP = 20  # items of one kind for one subject on one day (an import adds hundreds)
NEXT_SUBJECTS, NEXT_ITEMS = 8, 5
KEEP = 5  # homes kept per Private setting, for deltas
# the brain stem's websites: the STEM_TOP most visited, then up to STEM_RECENT more visited in
# the last STEM_DAYS days (the rest stay in the browsing store; the legend says how many)
STEM_TOP, STEM_RECENT, STEM_DAYS = 1500, 1000, 30
SITE_LIST = 50  # searches of each kind a site's details carry
_JOURNAL = re.compile(r"^- (\d\d:\d\d|—) (.+)$")  # as vault.append_journal writes them
_NOT_ADDED = {"journal", "browse", "session", "rollup"}  # their own items say more than "added"


# ---- the version stamp ---------------------------------------------------------------------

def _md(folder: str) -> list:
    """(name, mtime_ns, size) of the *.md entries directly in `folder`, as glob("*.md") finds
    them, in directory order. DirEntry.stat comes with the listing on Windows: no call per file."""
    try:
        with os.scandir(folder) as it:
            return [(e.name, (st := e.stat()).st_mtime_ns, st.st_size) for e in it
                    if e.name.endswith(".md")]
    except OSError:
        return []


def _kind_md(root: str, kind: str) -> list:
    """(vault-relative path, mtime_ns, size) of a kind's *.md pages in all its folders
    (Vault.dirs: an archived chat page in archive/chats/ too)."""
    return [(os.path.join(*d.split("/"), n), m, s) for d in Vault.dirs(kind)
            for n, m, s in _md(os.path.join(root, *d.split("/")))]


def _browse(root: str) -> list:
    """browse/'s *.md files at any depth (paths relative to it), except under browse/private/
    and the site pages in browse/sites/<category>/."""
    base = os.path.join(root, Vault.KIND_DIRS["browse"])
    private = os.path.normcase(os.path.join(root, *PRIVATE_BROWSE_DIR.split("/")))
    sites = os.path.normcase(os.path.join(base, "sites"))
    out, todo = [], [""]
    while todo:
        rel = todo.pop()
        folder = os.path.join(base, rel)
        try:
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            continue
        in_sites = os.path.normcase(folder) == sites  # its subfolders hold the site pages
        for e in entries:
            if e.is_dir():
                if not in_sites and os.path.normcase(e.path) != private:
                    todo.append(os.path.join(rel, e.name))
            elif e.name.endswith(".md"):
                st = e.stat()
                out.append((os.path.join(rel, e.name), st.st_mtime_ns, st.st_size))
    return out


def _events(root: str) -> list:
    """(year/month/name, mtime_ns, size) of events/*/*/*.jsonl, as EventLog globs them."""
    out = []
    ev = os.path.join(root, "events")
    for y in _names(ev, dirs=True):
        for m in _names(os.path.join(ev, y), dirs=True):
            try:
                with os.scandir(os.path.join(ev, y, m)) as it:
                    for e in it:
                        if e.name.endswith(".jsonl") and not e.name.startswith("."):
                            st = e.stat()
                            out.append((f"{y}/{m}/{e.name}", st.st_mtime_ns, st.st_size))
            except OSError:
                continue
    return out


def _names(folder: str, dirs: bool) -> list[str]:
    try:
        with os.scandir(folder) as it:
            return [e.name for e in it if not e.name.startswith(".") and e.is_dir() == dirs]
    except OSError:
        return []


def _stat(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _scan(root: Path, day: str) -> dict:
    """The listings `version` hashes: {"events", "notes": {kind: files}, FILES..., "day"}, in
    directory order (stable while a folder is unchanged)."""
    r = str(root)
    notes = {kind: tuple(_browse(r) if kind == "browse" else _kind_md(r, kind))
             for kind in Vault.KIND_DIRS}
    db = os.path.join(r, *store.PATH.split("/"))
    return {"events": tuple(_events(r)), "notes": notes,
            **{k: _stat(os.path.join(r, *f.split("/"))) for k, f in FILES.items()},
            "browse": (_stat(db), _stat(db + "-wal")), "day": day}


def _hash(scan: dict) -> str:
    key = tuple((k, tuple(v.items()) if isinstance(v, dict) else v) for k, v in scan.items())
    return format(hash(key) & 0xFFFF_FFFF_FFFF_FFFF, "016x")


def version(vault: Vault, now: datetime | None = None) -> str:
    """A cheap stamp of everything the home reads (see the module doc)."""
    return _hash(_scan(vault.root, local_day(now or datetime.now(UTC))))


# ---- the cache -----------------------------------------------------------------------------

class _Served:
    """The vault for one build: `iter_notes` serves the cache's notes; the rest is the vault's."""

    def __init__(self, vault: Vault, notes: dict[str, list]):
        self._vault, self._served, self.root = vault, notes, vault.root

    def __getattr__(self, name):
        return getattr(self._vault, name)

    def iter_notes(self, kind: str | None = None):
        if kind is not None and kind not in self._served:
            raise VaultError("invalid_argument", f"Unknown kind '{kind}'.")
        return iter(self._served[kind] if kind else
                    [n for k in Vault.KIND_DIRS for n in self._served[k]])


class HomeCache:
    """The home for one vault, rebuilt only in the parts whose files changed: unchanged notes
    keep their parsed objects (and so their page rows), the brain is laid out again only when
    the notes, vectors or layout changed and never relaid by itself, and the feed alone follows
    the recall log. Keeps the last KEEP homes per Private setting for `delta`. Thread-safe: one
    build at a time."""

    def __init__(self, vault: Vault, clock=None):
        self.vault = vault
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        # kind -> (its listing, {relpath: ((mtime_ns, size), Note)}, its notes in order)
        self._notes: dict[str, tuple] = {}
        self._memo: dict = {}  # dashboard.build_data's
        self._scan: dict | None = None
        self._data: dict | None = None
        self._items: list[dict] = []  # the feed, every audience
        self._homes = {False: OrderedDict(), True: OrderedDict()}
        # the stem's placement, in memory only (no file holds a site name): {site: (first,
        # share of the axis)}; and per Private setting (stamp, page brain, brain with the stem)
        self._placed: dict = {}
        self._stems: dict = {}

    def version(self) -> str:
        return version(self.vault, self._clock())

    def get(self, private: bool = False) -> dict:
        """The whole home: build_data's keys, `feed`, `next_up`, `exams` and its version `v`."""
        with self._lock:
            return self._current(private)

    def delta(self, since: str | None, private: bool = False) -> dict:
        """What changed since version `since`: {"v", "full": false, "sections": {name: value}}
        with "pages" ({"upsert": rows, "removed": ids}) and "brain" ({"blob", "added",
        "removed", "changed"}) only when they changed. An unknown `since` gets
        {"full": true, ...the whole home}."""
        with self._lock:
            new = self._current(private)
            old = self._homes[private].get(since) if since else None
            if old is None:
                return {**new, "full": True}
            out = {"v": new["v"], "full": False, "sections": {
                k: v for k, v in new.items() if k not in ("v", "pages", "brain")
                and (k not in old or (old[k] is not v and old[k] != v))}}
            if old["pages"] is not new["pages"]:
                was = {p["id"]: p for p in old["pages"]}
                ids = {p["id"] for p in new["pages"]}
                up = [p for p in new["pages"] if (q := was.get(p["id"])) is not p and q != p]
                gone = [i for i in was if i not in ids]
                if up or gone or [p["id"] for p in old["pages"]] != [p["id"] for p in new["pages"]]:
                    out["pages"] = {"upsert": up, "removed": gone,
                                    "order": [p["id"] for p in new["pages"]]}
            if old["brain"] is not new["brain"]:
                out["brain"] = {"blob": new["brain"], **_brain_diff(old["brain"], new["brain"])}
            return out

    def site(self, site: str, private: bool = False) -> dict | None:
        """One website's details for the brain's side panel, from the browsing store (never a
        vault page): None when the store has no such site for this Private setting, so a
        private site is unknown while the switch is locked. Its topic hubs only as pages the
        home shows."""
        try:
            rows = store.site_rows(self.vault, include_private=private)
            r = next((x for x in rows if x["site"] == site), None)
            if r is None:
                return None
            open_ = not private or any(x["site"] == site for x in store.site_rows(self.vault))
        except sqlite3.Error as e:
            log.warning("browsing store not read: %s", type(e).__name__)
            return None
        with self._lock:
            titles = {p["id"]: p["title"] for p in (self._data or {}).get("pages", [])}

        def text(x):
            return " ".join(term(x).split())[:300]
        return {"site": r["site"], "category": r["category"] or "other", "private": not open_,
                "visits": r["visits"], "secs": r["secs"], "days": r["days"],
                "titles": [text(t) for t in r["titles"]],
                "searches": [[text(t), n] for t, n in r["searches"][:SITE_LIST]],
                "led": [[text(t), n] for t, n in r["led"][:SITE_LIST]],
                "hubs": [{"id": h, "title": titles[h], "visits": n} for h, n in r["hubs"]
                         if h in titles],
                "related": list(r["related"]), "description": text(r["description"])}

    # ---- building ----

    def _stem(self, scan: dict, private: bool) -> dict:
        """The page brain with the browsing store's websites as its stem (`brainmap.with_stem`)
        for one Private setting: locked, only the visible side of the store is read
        (`include_private=False`); unlocked, every site, those with no visible side marked
        private. The same object while nothing it shows changed; the page brain itself when
        the store holds no site."""
        brain = self._data["brain"]
        key = (scan["browse"], scan["day"])
        hit = self._stems.get(private)
        if hit is not None and hit[0] == key and hit[1] is brain:
            return hit[2]
        try:
            pub = store.site_rows(self.vault)
            rows = store.site_rows(self.vault, include_private=True) if private else pub
        except sqlite3.Error as e:  # the home goes on without the stem
            log.warning("browsing store not read: %s", type(e).__name__)
            pub = rows = []
        out = brain
        if rows:
            cut = (date.fromisoformat(scan["day"]) - timedelta(days=STEM_DAYS)).isoformat()
            shown = rows[:STEM_TOP] + sorted(
                (r for r in rows[STEM_TOP:] if r["days"] and r["days"][-1] >= cut),
                key=lambda r: (r["days"][-1], r["site"]), reverse=True)[:STEM_RECENT]
            brainmap.stem_order(pub, self._placed)  # the visible sites first: never placed by
            brainmap.stem_order(shown, self._placed)  # where private ones are
            open_ = {r["site"] for r in pub}
            sites = sorted(({**r, "first_day": (r["days"] or [""])[0],
                             "last_day": (r["days"] or [""])[-1], "private": r["site"] not in open_}
                            for r in shown), key=lambda r: (self._placed[r["site"]][1], r["site"]))
            out = brainmap.with_stem(brain, sites, self._placed, len(rows))
            if hit is not None and hit[1] is brain and hit[2] == out:
                out = hit[2]  # only the store's file moved: no brain change to send
        self._stems[private] = (key, brain, out)
        return out

    def _current(self, private: bool) -> dict:
        now = self._clock()
        scan = _scan(self.vault.root, local_day(now))
        v = _hash(scan)
        homes = self._homes[private]
        if v in homes:
            homes.move_to_end(v)
            return homes[v]
        if scan != self._scan:
            self._build(scan, now)
            # the build may have written the layout: stamp what it wrote, so the next poll
            # does not see a change of its own making
            wrote = self._memo.get("brain", {}).get("layout")
            if wrote and wrote[0] == _stat(str(self.vault.root / brainmap.LAYOUT)) \
                    and wrote[0] != scan["layout"]:
                scan = {**scan, "layout": wrote[0]}
                v = _hash(scan)
            self._scan = scan
        home = {**self._data, "brain": self._stem(scan, private),
                "feed": _split(self._items, scan["day"], private), "v": v}
        homes[v] = home
        while len(homes) > KEEP:
            homes.popitem(last=False)
        return home

    def _build(self, scan: dict, now: datetime) -> None:
        old = self._scan or {}
        learn = ("events", "notes", "catalog", "inbox", "day")
        served = _Served(self.vault, self._load(scan["notes"]))
        if self._data is None or any(scan[k] != old.get(k) for k in learn):
            data = dashboard.build_data(served, now=now, auto_relayout=False, memo=self._memo)
            parts = self._memo["parts"]
            self._data = {**data, "next_up": _next_up(data, parts, scan["day"]),
                          "exams": [a for a in data["assessments"] if a["date"]]}
        elif any(scan[k] != old.get(k) for k in ("vectors", "vectors_wal", "layout")):
            self._data = {**self._data, "brain": brainmap.build(
                served, self._data["pages"], self._data["clusters"], auto_relayout=False,
                memo=self._memo.setdefault("brain", {}))}
        if any(scan[k] != old.get(k) for k in (*learn, "recall")):
            self._items = _feed(self.vault, self._data, self._memo["parts"], scan["day"])

    def _load(self, listing: dict) -> dict[str, list]:
        """Each kind's notes in iter_notes order; a file whose (mtime, size) is unchanged keeps
        its Note object, and an unchanged folder its list. Many to parse (the first build):
        through the search index snapshot."""
        miss = 0
        for kind, files in listing.items():
            had = self._notes.get(kind)
            if had is None or had[0] != files:
                miss += sum(1 for f in files if (got := (had or (0, {}))[1].get(f[0])) is None
                            or got[0] != f[1:])
        if miss > 50:
            with dashboard._snapshot_parses(self.vault):
                return self._parse(listing)
        return self._parse(listing)

    def _parse(self, listing: dict) -> dict[str, list]:
        root, keep = self.vault.root, {}
        for kind, files in listing.items():
            had = self._notes.get(kind)
            if had is not None and had[0] == files:
                keep[kind] = had
                continue
            # _browse paths are browse/-relative, _kind_md ones vault-relative
            d = Vault.KIND_DIRS[kind] if kind == "browse" else ""
            ns, old, by = [], (had or (0, {}))[1], {}
            for rel, mtime, size in sorted(files, key=lambda f: os.path.normcase(f[0]).split(
                    os.sep)):  # iter_notes' order
                got = old.get(rel)
                if got is None or got[0] != (mtime, size):
                    if not ID_RE.fullmatch(os.path.basename(rel)[:-3]):
                        continue
                    try:
                        n = self.vault._parse(root / d / rel, kind)
                    except Exception as e:  # noqa: BLE001  as iter_notes: skip, never fail
                        log.warning("skipping %s: %s", rel, e)
                        continue
                    n.fields = dict(n.fields)  # only read here; a dict reads ~8x faster
                    got = ((mtime, size), n)
                by[rel] = got
                ns.append(got[1])
            keep[kind] = (files, by, ns)
        self._notes = keep
        return {kind: k[2] for kind, k in keep.items()}


# ---- feed, next up -------------------------------------------------------------------------

def _split(items: list[dict], day: str, private: bool) -> dict:
    """Today and This week for one Private setting: grouped by subject (the group of the
    newest item first), newest first in a group, FEED_CAP each."""
    seen = [i for i in items if may_see("app_home", i["_aud"], i["_access"],
                                        private_flag=private)]
    out = {}
    for name, keep in (("today", lambda d: d == day), ("week", lambda d: d < day)):
        got = [i for i in seen if keep(i["day"])][:FEED_CAP]
        rank: dict = {}
        for i in got:
            rank.setdefault((i["subject"] or {}).get("id"), len(rank))
        got.sort(key=lambda i: rank[(i["subject"] or {}).get("id")])  # stable
        out[name] = [{k: v for k, v in i.items() if not k.startswith("_")} for i in got]
    return out


class _Lookup:
    """Titles, subjects (brain lobes) and audiences of page and catalog ids."""

    def __init__(self, data: dict, parts: dict):
        self.catalog, self.hidden = parts["catalog"], parts["hidden"]
        self.notes = {}
        for ns in parts["notes"].values():
            for n in ns:
                self.notes.setdefault(n.id, n)
        self.lobe = {p["id"]: p["cluster"] for p in data["pages"]}
        self.ltitle = {c["id"]: c["title"] for c in data["clusters"]}

    def title(self, i: str) -> str:
        if (e := self.catalog.entries.get(i)) is not None:
            return plain(e.title) or i
        n = self.notes.get(i)
        return dashboard._title(n) if n is not None else i

    def subject(self, i: str | None) -> dict | None:
        lobe = self.lobe.get(i) if i else None
        if lobe is None and i and (e := self.catalog.entries.get(i)) is not None and e.domain:
            lobe = f"domain:{e.domain}"
        if lobe is None:
            return None
        d = self.catalog.entries.get(lobe.removeprefix("domain:"))
        return {"id": lobe, "title": self.ltitle.get(lobe)
                or (plain(d.title) if d is not None and lobe.startswith("domain:") else lobe)}

    def audience(self, i: str | None) -> Audience:
        if not i:
            return Audience.CLOUD
        n = self.notes.get(i)
        if i in self.catalog.entries:
            return audience_of_study([i], self.hidden, note_private=n is not None
                                     and is_private(n))
        return audience_of_note(n, self.hidden) if n is not None else Audience.CLOUD


def _ts(day: str, hhmm: str | None) -> str:
    """A local Cortex-day time as UTC 'YYYY-MM-DDTHH:MM:SSZ'; no time: the day's start."""
    d = date.fromisoformat(day)
    if hhmm:
        t = time(int(hhmm[:2]), int(hhmm[3:5]))
        at = datetime.combine(d + timedelta(days=t < time(4)), t)  # 01:30 ends the day before
    else:
        at = datetime.combine(d, time()) + DAY_ROLLOVER
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _obj(e: dict) -> str | None:
    o = e.get("object")
    return o.split(":", 1)[1] if isinstance(o, str) and ":" in o else None


def _feed(vault: Vault, data: dict, parts: dict, day: str) -> list[dict]:
    """Every feed item of the last WEEK_DAYS days, newest first, each with its audience."""
    look = _Lookup(data, parts)
    start = (date.fromisoformat(day) - timedelta(days=WEEK_DAYS - 1)).isoformat()
    evs = live(parts["events"])
    items: list[dict] = []

    def add(ts, d, kind, i, title, detail="", access="note", aud=None, timed=True):
        items.append({"ts": ts, "day": d, "timed": timed, "kind": kind, "subject": look.subject(i),
                      "title": plain(title, 200), "detail": plain(detail, 300), "id": i,
                      "_aud": aud or look.audience(i), "_access": access})

    graded: dict[str, str] = {}  # object id -> its last grade's ts
    answered = set()
    for e in evs:
        if e.get("kind") == "grade" and (o := _obj(e)):
            graded[o] = max(graded.get(o, ""), e["ts"])
            answered.add(e.get("ref"))
    quiz: dict[tuple, dict] = {}
    sessions: dict[str, dict] = {}
    added = set()
    for e in evs:
        d, kind, o = str(e.get("day", "")), e.get("kind"), _obj(e)
        if d < start:
            continue
        p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        pk = p.get("kind") if kind == "log" else None
        if kind == "grade" or pk == "self_report":
            o = o or p.get("concept")
            verdict = e.get("verdict") if kind == "grade" else p.get("verdict")
            if o and verdict in ("correct", "partial", "wrong"):
                q = quiz.setdefault((d, o), {"ts": "", "correct": 0, "partial": 0, "wrong": 0})
                q["ts"] = max(q["ts"], e["ts"])
                q[verdict] += 1
        elif kind == "ask" and o and e.get("id") not in answered:
            add(e["ts"], d, "ask", o, look.title(o), str(e.get("question") or ""), "study")
        elif kind == "taught" and o and graded.get(o, "") < e["ts"]:
            add(e["ts"], d, "taught", o, look.title(o),
                str(p.get("summary") or "Taught, not yet tested"), "study")
        elif kind in ("open", "close") and e.get("session"):
            sessions.setdefault(e["session"], {})[kind] = e
        elif kind == "change" and p.get("note") == "added" and isinstance(p.get("change"), dict):
            c = p["change"]
            i = str(c.get("id") or "")
            if i and i in look.notes and i not in added:
                added.add(i)
                src = topics.page_source(look.notes[i].body).lower()
                add(e["ts"], d, "chat" if src in dashboard.CHATS else "added", i, look.title(i),
                    str(c.get("kind") or ""), "chat" if src in dashboard.CHATS else "note")
        elif pk == "problem_status" and (i := str(p.get("problem") or o or "")):
            add(e["ts"], d, "problem", i, look.title(i), str(p.get("status") or ""), "study")
        elif pk == "assessment_result" and (i := str(p.get("assessment") or o or "")):
            add(e["ts"], d, "result", i, look.title(i),
                " ".join(str(p[k]) for k in ("outcome", "score") if p.get(k) is not None),
                "study")
        elif pk == "chat_saved" and p.get("auto"):  # the automatic chat import: each page as
            for i in p.get("pages") or []:  # an added one, on the day it came in, once
                if isinstance(i, str) and i in look.notes and i not in added:
                    added.add(i)
                    add(e["ts"], d, "chat", i, look.title(i), str(p.get("source") or ""), "chat")
        elif pk in ("chat_saved", "chat_learned") and not p.get("auto"):
            pages = [x for x in p.get("pages") or [] if isinstance(x, str)]
            i = pages[0] if pages else None
            add(e["ts"], d, "chat", i, str(p.get("title") or "Chat"), str(p.get("source") or ""),
                "chat", Audience.LOCAL if any(look.audience(x) is not Audience.CLOUD
                                              for x in pages) else Audience.CLOUD)
    for (d, o), q in quiz.items():
        n = q["correct"] + q["partial"] + q["wrong"]
        add(q["ts"], d, "quiz", o, look.title(o),
            f"{q['correct']} of {n} right" + (f", {q['partial']} partial" if q["partial"] else ""),
            "study")
    for s in sessions.values():
        e = s.get("close") or s["open"]
        mode = e.get("mode") or (s.get("open") or {}).get("mode")
        add(e["ts"], str(e["day"]), "session", None,
            "Study session" if mode in STUDY_MODES else "Chat session",
            str((s.get("close") or {}).get("summary") or ""), "study", Audience.CLOUD)
    try:
        rows = recall_log.rows(vault)
    except (OSError, UnicodeDecodeError) as e:  # usage stats: the feed goes on without them
        log.warning("recall log not read: %s", e)
        rows = []
    for row in rows:
        q = row.get("query") if isinstance(row, dict) else None
        if not isinstance(q, str) or not isinstance(row.get("ts"), str):
            continue
        try:
            d = local_day(datetime.fromisoformat(row["ts"].replace("Z", "+00:00")))
        except ValueError:
            continue
        if d < start:
            continue
        ids = [x for x in row.get("ids") or [] if isinstance(x, str)]
        add(row["ts"], d, "research", ids[0] if ids else None, q, f"{len(ids)} pages found",
            "note", Audience.LOCAL if any(look.audience(x) is not Audience.CLOUD for x in ids)
            else Audience.CLOUD)
    for n in parts["notes"]["journal"]:
        if not start <= n.id <= day:
            continue
        for ln in n.body.splitlines():
            if m := _JOURNAL.match(ln):
                hhmm = m.group(1) if m.group(1) != "—" else None
                add(_ts(n.id, hhmm), n.id, "journal", n.id, m.group(2), "", "note")
    for i, n in look.notes.items():  # pages dated this week that no tool event added
        if n.kind in _NOT_ADDED or i in added:
            continue
        d = topics.page_date(i, n.fields, n.body)
        if start <= d <= day:
            src = topics.page_source(n.body).lower()
            add(_ts(d, None), d, "chat" if src in dashboard.CHATS else "added", i,
                look.title(i), n.kind, "chat" if src in dashboard.CHATS else "note", timed=False)
    items.sort(key=lambda i: i["ts"], reverse=True)
    count: dict[tuple, int] = {}
    out = []
    for i in items:
        k = (i["day"], i["kind"], (i["subject"] or {}).get("id"), i["_aud"])
        count[k] = count.get(k, 0) + 1
        if count[k] <= PER_GROUP:
            out.append(i)
    return out


def _next_up(data: dict, parts: dict, day: str) -> list[dict]:
    """Subjects by their latest activity, each with its next steps: the plan's items, then
    the due reviews the plan had no room for; no dates. Private subjects are left out."""
    look = _Lookup(data, parts)
    states, hidden = parts["states"], parts["hidden"]
    steps = []
    for i in data["today"]:
        why = "Due for review" if i["kind"] == "refresh" and i["why"].startswith("due ") \
            else i["why"]
        steps.append({**i, "why": why})
    planned = {i["id"] for i in steps}
    for c in sorted(states):
        if c not in planned and c not in hidden and c in look.catalog.entries \
                and look.audience(c) is Audience.CLOUD and planner.is_due(states[c], day):
            steps.append({"kind": "review", "id": c, "title": look.title(c),
                          "state": states[c].state, "why": "Due for review"})
    last: dict = {}
    for e in parts["events"]:
        p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        c = p.get("change") if e.get("kind") == "change" else None
        i = _obj(e) or (str(c.get("id")) if isinstance(c, dict) and c.get("id") else None)
        if i and (s := look.subject(i)):
            last[s["id"]] = max(last.get(s["id"], ""), e.get("ts", ""))
    groups: dict = {}
    for s in steps:
        subj = look.subject(s["id"])
        g = groups.setdefault((subj or {}).get("id"), {"subject": subj, "items": []})
        if len(g["items"]) < NEXT_ITEMS:
            g["items"].append(s)
    order = sorted(groups.values(), key=lambda g: last.get((g["subject"] or {}).get("id"), ""),
                   reverse=True)  # stable: subjects never touched keep the plan's order
    return order[:NEXT_SUBJECTS]


def _brain_diff(old: dict, new: dict) -> dict:
    """Node ids added, removed, and kept but moved or restyled (place, lobe, depth, embedded,
    weight) between two brain blobs."""
    def cols(b):
        n = b["nodes"]
        lobes = [x["id"] for x in b["lobes"]]
        arr = {k: np.frombuffer(base64.b64decode(n[k]), t).reshape(-1, w)
               for k, t, w in (("xyz", "<f4", 3), ("depth", "<f4", 1), ("embedded", "u1", 1),
                               ("weight", "<f4", 1))}
        lobe = np.frombuffer(base64.b64decode(n["lobe"]), "<u2")  # 65535: a stem site, no lobe
        return ({i: k for k, i in enumerate(n["ids"])}, arr,
                [lobes[k] if k < len(lobes) else None for k in lobe.tolist()])
    (oi, oa, ol), (ni, na, nl) = cols(old), cols(new)
    both = [i for i in ni if i in oi]
    a = np.array([oi[i] for i in both], np.int64)
    b = np.array([ni[i] for i in both], np.int64)
    moved = np.zeros(len(both), bool)
    for k in oa:
        if len(both):
            moved |= (oa[k][a] != na[k][b]).any(1)
    return {"added": [i for i in ni if i not in oi], "removed": [i for i in oi if i not in ni],
            "changed": [i for k, i in enumerate(both) if moved[k] or ol[a[k]] != nl[b[k]]]}
