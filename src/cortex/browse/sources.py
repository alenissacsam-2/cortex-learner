"""The browsers' own history files (docs/design/browse-brain.md §1): Chromium (Chrome, Edge,
Brave, Comet, every profile) and Firefox. Each file is copied to a temp folder (it is locked
while the browser runs), opened read-only and deleted again; a browser file is never written.
`scan` is the counts-only dry run behind `cortex browse scan`.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

from cortex.browse import privacy
from cortex.events import local_day

CHROMIUM = {"chrome": ("Google", "Chrome"), "edge": ("Microsoft", "Edge"),
            "brave": ("BraveSoftware", "Brave-Browser"),
            "comet": ("Perplexity", "Comet")}  # Perplexity's Chromium browser
BROWSERS = (*CHROMIUM, "firefox")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
CHROME_OFFSET_US = 11_644_473_600_000_000  # 1601-01-01 to 1970-01-01
SUBFRAME = {3, 4}  # Chromium core transitions AUTO_SUBFRAME, MANUAL_SUBFRAME: not a page
FF_SKIP = {4, 8}  # Firefox TRANSITION_EMBED, TRANSITION_FRAMED_LINK
GAP_CAP = 600.0  # seconds: a visit lasts until the next one, at most this long
TEMP_PREFIX, STALE_SECS = "cortex-browse-", 3600
CHROMIUM_SQL = """
SELECT v.visit_time, v.visit_duration, v.transition, u.url, u.title,
       (SELECT k.term FROM keyword_search_terms k WHERE k.url_id = u.id LIMIT 1)
FROM visits v JOIN urls u ON u.id = v.url ORDER BY v.visit_time"""
FIREFOX_SQL = """
SELECT v.visit_date, v.visit_type, p.url, p.title
FROM moz_historyvisits v JOIN moz_places p ON p.id = v.place_id ORDER BY v.visit_date"""


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


def discover(local: Path | None = None, roaming: Path | None = None) -> list[Source]:
    """Every Chromium profile folder holding a `History` file under `local` (default
    %LOCALAPPDATA%) and every Firefox profile with `places.sqlite` under `roaming` (default
    %APPDATA%)."""
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
    """The sources `browse.browsers` allows (every one found when it is empty)."""
    found = discover() if found is None else found
    want = getattr(settings, "browsers", ())
    return [s for s in found if not want or s.browser in want]


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
    sweep()
    with _copy(src.path) as con:
        if src.browser == "firefox":
            rows = [(t, 0, u, ti, None) for t, ty, u, ti in con.execute(FIREFOX_SQL)
                    if ty not in FF_SKIP]
        else:
            rows = [(t, d, u, ti, k) for t, d, tr, u, ti, k in con.execute(CHROMIUM_SQL)
                    if not isinstance(tr, int) or tr & 0xFF not in SUBFRAME]
    late = datetime.now(UTC) + timedelta(days=1)
    rows = [r for r in rows if _sane(src.browser, r[0], late)]
    rows = [(t, d if isinstance(d, int) else 0, *rest) for t, d, *rest in rows]
    out = []
    for n, (raw, dur, url, title, term) in enumerate(rows):
        gap = (rows[n + 1][0] - raw) / 1e6 if n + 1 < len(rows) else GAP_CAP
        secs = min(max(gap, 0.0), GAP_CAP)
        if dur and dur > 0:
            secs = min(secs, dur / 1e6)
        out.append(Visit(to_time(src.browser, raw), url or "", title or "", secs,
                         term or None, src.key))
    return out, (rows[-1][0] if rows else None)


def scan(sources: list[Source], settings: privacy.Settings,
         tz: tzinfo | None = None) -> list[dict]:
    """Per source: visits, searches, first and last day, and how many the filter would drop,
    keep private or keep (a "maybe" counts as its category; "keep" can still turn private on
    import when a page sits under no topic hub). Counts only, never text."""
    rows = []
    for s in sources:
        try:
            visits, _ = read(s)
        except Exception as e:  # one bad source never stops the others
            rows.append({"source": s.key, "error": type(e).__name__, "why": why(e)})
            continue
        c: Counter = Counter()
        for v in visits:
            k = privacy.judge(v.url, v.title, settings=settings)
            out = privacy.apply(k, settings=settings)
            c["dropped" if out is None else "private" if out.private else "kept"] += 1
            c["searches"] += bool(v.term or (k and k.search))
            c["maybe"] += bool(k and k.maybe)
        days = [local_day(v.start, tz) for v in visits[:1] + visits[-1:]]
        rows.append({"source": s.key, "visits": len(visits), "searches": c["searches"],
                     "first": days[0] if days else None, "last": days[-1] if days else None,
                     "dropped": c["dropped"], "private": c["private"], "kept": c["kept"],
                     "maybe": c["maybe"]})
    return rows
