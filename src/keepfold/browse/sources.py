"""The browsers' own history files (docs/design/browse-brain.md §1): Chromium (Chrome, Edge,
Brave, Comet, every profile) and Firefox. Each file is copied to a temp folder (it is locked
while the browser runs), opened read-only and deleted again; a browser file is never written.
`scan` is the counts-only dry run behind `keepfold browse scan`.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

from keepfold.browse import privacy
from keepfold.events import local_day

CHROMIUM = {"chrome": ("Google", "Chrome"), "edge": ("Microsoft", "Edge"),
            "brave": ("BraveSoftware", "Brave-Browser"),
            "comet": ("Perplexity", "Comet")}  # Perplexity's Chromium browser
# macOS (~/Library/Application Support) and Linux (~/.config): profiles sit right in the folder
MAC_CHROMIUM = {"chrome": ("Google", "Chrome"), "edge": ("Microsoft Edge",),
                "brave": ("BraveSoftware", "Brave-Browser")}
LINUX_CHROMIUM = {"chrome": ("google-chrome",), "edge": ("microsoft-edge",),
                  "brave": ("BraveSoftware", "Brave-Browser")}
BROWSERS = (*CHROMIUM, "firefox")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
CHROME_OFFSET_US = 11_644_473_600_000_000  # 1601-01-01 to 1970-01-01
SUBFRAME = {3, 4}  # Chromium core transitions AUTO_SUBFRAME, MANUAL_SUBFRAME: not a page
FF_SKIP = {4, 8}  # Firefox TRANSITION_EMBED, TRANSITION_FRAMED_LINK
GAP_CAP = 600.0  # seconds: a visit lasts until the next one, at most this long
TEMP_PREFIX, STALE_SECS = "keepfold-browse-", 3600
CHROMIUM_SQL = """
SELECT v.visit_time, v.visit_duration, v.transition, u.url, u.title,
       (SELECT k.term FROM keyword_search_terms k WHERE k.url_id = u.id LIMIT 1), v.rowid
FROM visits v JOIN urls u ON u.id = v.url {where} ORDER BY v.visit_time"""
FIREFOX_SQL = """
SELECT v.visit_date, v.visit_type, p.url, p.title, v.rowid
FROM moz_historyvisits v JOIN moz_places p ON p.id = v.place_id {where} ORDER BY v.visit_date"""
# rows past the cursor: a new visit id, or a time in the last GAP_CAP before the newest time
# read (re-read, so the last visit's time on the page is measured to the visit after it)
_SINCE = {"chromium": "WHERE v.rowid > ? OR v.visit_time >= ?",
          "firefox": "WHERE v.rowid > ? OR v.visit_date >= ?"}


class CopyFailed(OSError):
    """The History file could not be copied (the browser holds it locked, or no permission)."""


def why(e: BaseException) -> str:
    """A source's read error as a short, text-free reason for the owner."""
    if isinstance(e, CopyFailed):
        return ("could not copy its history file (the browser may hold it locked: close the "
                "browser and run again)")
    if isinstance(e, sqlite3.DatabaseError):
        return "its history file is not a readable database"
    return f"not read ({type(e).__name__})"


@dataclass(frozen=True)
class Source:
    browser: str
    profile: str
    path: Path

    @property
    def key(self) -> str:
        return f"{self.browser}/{self.profile}"


@dataclass
class Visit:
    start: datetime  # aware, UTC
    url: str  # raw: filtered before anything keeps it
    title: str
    secs: float  # estimated time on the page
    term: str | None = None  # Chromium's keyword_search_terms
    source: str = ""
    excerpt: str = ""  # from the extension's inbox only
    channel: str = ""  # a video's channel, from the extension's inbox only
    # "<raw time>:<URL hash>": the visit's key in keepfold.browse.store, the same however the
    # browser numbers its rows (a rebuilt History file, a reused id)
    native: str = ""


def _profiles(browser: str, folder: Path, name: str) -> list[Source]:
    return [Source(browser, d.name, d / name) for d in sorted(folder.iterdir())
            if (d / name).is_file()] if folder.is_dir() else []


def _discover_unix(home: Path, mac: bool) -> list[Source]:
    """macOS and Linux (review C14): the same browsers under ~/Library/Application Support or
    ~/.config, and Firefox under its own profile folder."""
    base = home / "Library" / "Application Support" if mac else home / ".config"
    out = []
    for browser, parts in (MAC_CHROMIUM if mac else LINUX_CHROMIUM).items():
        out += _profiles(browser, base.joinpath(*parts), "History")
    ff = base / "Firefox" / "Profiles" if mac else home / ".mozilla" / "firefox"
    return out + _profiles("firefox", ff, "places.sqlite")


def discover(local: Path | None = None, roaming: Path | None = None, *,
             home: Path | None = None, platform: str | None = None) -> list[Source]:
    """Every Chromium profile folder holding a `History` file under `local` (default
    %LOCALAPPDATA%) and every Firefox profile with `places.sqlite` under `roaming` (default
    %APPDATA%). On macOS and Linux (`platform`, default sys.platform), with neither folder
    given, the browsers' folders under `home` (default ~) instead."""
    platform = platform or sys.platform
    if local is None and roaming is None and not platform.startswith("win"):
        return _discover_unix(home or Path.home(), platform == "darwin")
    if local is None and os.environ.get("LOCALAPPDATA"):
        local = Path(os.environ["LOCALAPPDATA"])
    if roaming is None and os.environ.get("APPDATA"):
        roaming = Path(os.environ["APPDATA"])
    out = []
    for browser, parts in CHROMIUM.items():
        ud = local.joinpath(*parts, "User Data") if local else None
        if ud and ud.is_dir():
            out += [Source(browser, d.name, d / "History") for d in sorted(ud.iterdir())
                    if (d / "History").is_file()]
    ff = roaming / "Mozilla" / "Firefox" / "Profiles" if roaming else None
    if ff and ff.is_dir():
        out += [Source("firefox", d.name, d / "places.sqlite") for d in sorted(ff.iterdir())
                if (d / "places.sqlite").is_file()]
    return out


def chosen(settings, found: list[Source] | None = None) -> list[Source]:
    """The sources `browse.browsers` allows (every one found when it is empty), minus the
    profiles unticked in the app (`browse.skip_profiles`)."""
    found = discover() if found is None else found
    want = getattr(settings, "browsers", ())
    skip = getattr(settings, "skip_profiles", ())
    return [s for s in found if (not want or s.browser in want) and not (skip and s.key in skip)]


def pick(sources: list[Source], browser: str | None = None,
         profile: str | None = None) -> list[Source]:
    return [s for s in sources if (browser is None or s.browser == browser.lower())
            and (profile is None or s.profile.lower() == profile.lower())]


def to_time(browser: str, raw: int) -> datetime:
    """A browser timestamp (Chromium: µs since 1601; Firefox: µs since 1970) as UTC."""
    return EPOCH + timedelta(microseconds=raw - (0 if browser == "firefox" else
                                                 CHROME_OFFSET_US))


def _rmtree(d: Path) -> None:
    """Delete a temp copy, retrying while Windows still holds a handle on it."""
    for n in range(5):
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            return
        time.sleep(0.1 * (n + 1))


def sweep(now: float | None = None) -> None:
    """Delete temp copies a crashed or locked-out earlier run left (older than STALE_SECS)."""
    now = time.time() if now is None else now
    for d in Path(tempfile.gettempdir()).glob(TEMP_PREFIX + "*"):
        try:
            if d.is_dir() and now - d.stat().st_mtime > STALE_SECS:
                _rmtree(d)
        except OSError:
            pass


@contextmanager
def _copy(path: Path) -> Iterator[sqlite3.Connection]:
    """A read-only connection to a temp copy of `path` (and its -wal), deleted afterwards."""
    d = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    try:
        dst = d / path.name
        try:
            shutil.copyfile(path, dst)
            wal = path.with_name(path.name + "-wal")
            if wal.is_file():
                shutil.copyfile(wal, dst.with_name(dst.name + "-wal"))
        except OSError as e:
            raise CopyFailed(str(e)) from e
        con = sqlite3.connect(f"{dst.as_uri()}?mode=ro", uri=True)
        try:
            yield con
        finally:
            con.close()
    finally:
        _rmtree(d)


def _sane(browser: str, raw, late: datetime) -> bool:
    """A raw visit time that is an integer and lands between 1970 and tomorrow."""
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        return False
    try:
        return EPOCH <= to_time(browser, raw) <= late
    except OverflowError:
        return False


def read(src: Source) -> tuple[list[Visit], int | None]:
    """Every page visit of `src`, oldest first, and the newest raw timestamp (the cursor).
    A visit lasts until the next one (at most GAP_CAP), or its recorded duration if shorter.
    A row whose time is not an integer, negative or out of range is skipped (second review
    L1), so one bad row never aborts the source or moves the cursor into the future."""
    visits, top, _ = read_since(src)
    return visits, top


def read_since(src: Source, after_id: int | None = None, after_time: int | None = None
               ) -> tuple[list[Visit], int | None, int | None]:
    """The visits of `src` past a cursor, all at once (`Since` reads them in chunks): the
    visits, oldest first, then the newest raw time and visit id read (the next cursor)."""
    with Since(src, after_id, after_time) as rd:
        out = [v for chunk in rd.chunks() for v in chunk]
    return out, rd.top_time, rd.top_id


class Since:
    """The visits of `src` past a cursor (`after_id`, `after_time`: the newest visit id and
    raw time read before; None reads all), from a temp copy of its History file while open:
    `total` rows to go through, `chunks(n)` yields them as Visits, oldest first, with their
    `native` key, n at a time (memory stays bounded however long the history), then
    `top_time` and `top_id` hold the next cursor (None when nothing was read). Visits of the
    last GAP_CAP before `after_time` are read again, so their time on the page is measured to
    the next visit. Only the rows past the cursor are fetched (the History file's indexes on
    the visit id and time), so a run with nothing new reads next to nothing."""

    def __init__(self, src: Source, after_id: int | None = None,
                 after_time: int | None = None):
        self.src, self.total = src, 0
        self.top_time: int | None = None
        self.top_id: int | None = None
        since = after_id is not None and after_time is not None
        self._where = _SINCE["firefox" if src.browser == "firefox" else "chromium"] \
            if since else ""
        self._args = (after_id, after_time - int(GAP_CAP * 1e6)) if since else ()

    def __enter__(self) -> Since:
        sweep()
        self._copy = _copy(self.src.path)
        self._con = self._copy.__enter__()
        try:
            table = "moz_historyvisits" if self.src.browser == "firefox" else "visits"
            self.total = self._con.execute(
                f"SELECT COUNT(*) FROM {table} v {self._where}", self._args).fetchone()[0]
        except BaseException:
            self._copy.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc) -> None:
        self._copy.__exit__(*exc)

    def chunks(self, n: int = 1000) -> Iterator[list[Visit]]:
        ff = self.src.browser == "firefox"
        sql = (FIREFOX_SQL if ff else CHROMIUM_SQL).format(where=self._where)
        late = datetime.now(UTC) + timedelta(days=1)
        out: list[Visit] = []
        last = None  # the row before: its time on the page is measured to this one
        for r in self._con.execute(sql, self._args):
            if ff:
                raw, ty, url, title, rid = r
                dur, term, keep = 0, None, ty not in FF_SKIP
            else:
                raw, dur, tr, url, title, term, rid = r
                keep = not isinstance(tr, int) or tr & 0xFF not in SUBFRAME
            if isinstance(rid, int):  # a frame moves the cursor too
                self.top_id = rid if self.top_id is None else max(self.top_id, rid)
            if not keep or not _sane(self.src.browser, raw, late):
                continue
            if last is not None:
                out.append(self._visit(last, raw))
                if len(out) >= n:
                    yield out
                    out = []
            last = (raw, dur if isinstance(dur, int) else 0, url, title, term)
            self.top_time = raw if self.top_time is None else max(self.top_time, raw)
        if last is not None:
            out.append(self._visit(last, None))
        if out:
            yield out

    def _visit(self, row: tuple, after: int | None) -> Visit:
        raw, dur, url, title, term = row
        secs = min(max((after - raw) / 1e6, 0.0), GAP_CAP) if after is not None else GAP_CAP
        if dur and dur > 0:
            secs = min(secs, dur / 1e6)
        return Visit(to_time(self.src.browser, raw), url or "", title or "", secs,
                     term or None, self.src.key,
                     native=f"{raw}:{hashlib.sha1((url or '').encode()).hexdigest()[:16]}")


def scan(sources: list[Source], settings: privacy.Settings,
         tz: tzinfo | None = None) -> list[dict]:
    """Per source: visits, searches, first and last day, and how many the filter would drop,
    keep private or keep (a "maybe" counts as its category; "keep" can still turn private on
    import when a page sits under no topic hub). Counts only, never text."""
    rows = []
    for s in sources:
        c: Counter = Counter()
        first = last = None
        try:
            with Since(s) as rd:
                for chunk in rd.chunks():
                    for v in chunk:
                        k = privacy.judge(v.url, v.title, settings=settings)
                        out = privacy.apply(k, settings=settings)
                        c["dropped" if out is None else "private" if out.private else "kept"] += 1
                        c["searches"] += bool(v.term or (k and k.search))
                        c["maybe"] += bool(k and k.maybe)
                    first = first or local_day(chunk[0].start, tz)
                    last = local_day(chunk[-1].start, tz)
        except Exception as e:  # one bad source never stops the others
            rows.append({"source": s.key, "error": type(e).__name__, "why": why(e)})
            continue
        rows.append({"source": s.key, "visits": c["dropped"] + c["private"] + c["kept"],
                     "searches": c["searches"], "first": first, "last": last,
                     "dropped": c["dropped"], "private": c["private"], "kept": c["kept"],
                     "maybe": c["maybe"]})
    return rows
