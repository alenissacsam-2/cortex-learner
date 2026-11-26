"""`keepfold browse selftest`: the whole browse import, end to end, on synthetic data only.

It builds a throwaway vault and synthetic Chrome, Comet (Perplexity/Comet/User Data) and
Firefox history files in a temp folder, runs scan and import against them and checks what the
owner cares about. It never opens the real vault, ~/.keepfold/config.yaml or a real browser
profile (default Settings, explicit paths), so the owner can see it work without exposing data.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from keepfold.browse import build, privacy, sources
from keepfold.vault import PRIVATE_BROWSE_DIR, Vault

DROPPED = ("SIGNINMARKER", "BANKMARKER", "SECRETMARKER")
SENSITIVE = ("HEALTHMARKER", "ADULTMARKER")


def _chromium(path: Path, rows: list[tuple[datetime, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
        "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER, "
        "visit_duration INTEGER, transition INTEGER);"
        "CREATE TABLE keyword_search_terms(keyword_id INTEGER, url_id INTEGER, term TEXT, "
        "normalized_term TEXT);")
    for t, url, title in rows:
        uid = con.execute("INSERT INTO urls(url, title) VALUES (?, ?)", (url, title)).lastrowid
        raw = int((t - sources.EPOCH).total_seconds() * 1e6) + sources.CHROME_OFFSET_US
        con.execute("INSERT INTO visits(url, visit_time, visit_duration, transition) "
                    "VALUES (?, ?, 0, 805306368)", (uid, raw))
    con.commit()
    con.close()


def _firefox(path: Path, rows: list[tuple[datetime, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                      "CREATE TABLE moz_historyvisits(id INTEGER PRIMARY KEY, place_id INTEGER,"
                      " visit_date INTEGER, visit_type INTEGER);")
    for t, url, title in rows:
        pid = con.execute("INSERT INTO moz_places(url, title) VALUES (?, ?)",
                          (url, title)).lastrowid
        con.execute("INSERT INTO moz_historyvisits(place_id, visit_date, visit_type) "
                    "VALUES (?, ?, 1)", (pid, int(t.timestamp() * 1e6)))
    con.commit()
    con.close()


def _machine(local: Path, roaming: Path, now: datetime) -> None:
    t = (now - timedelta(days=3)).replace(hour=9, minute=0, second=0, microsecond=0)

    def at(mins: int, day: int = 0) -> datetime:
        return t + timedelta(days=day, minutes=mins)

    _chromium(local / "Google" / "Chrome" / "User Data" / "Default" / "History", [
        (at(0), "https://example.org/chess/openings", "Chess openings for beginners"),
        (at(10), "https://www.google.com/search?q=chess+endgames", "chess endgames - Google"),
        (at(20), "https://accounts.example.com/signin", "Sign in SIGNINMARKER"),
        (at(30), "https://www.hdfcbank.com/personal", "Net Banking BANKMARKER"),
        (at(40), "https://example.com/cb?token=SECRETMARKER", "Callback SECRETMARKER"),
        (at(50), "https://health.example.org/flu", "Flu symptoms HEALTHMARKER"),
        (at(60), "https://www.pornhub.com/view_video.php?viewkey=zz", "ADULTMARKER video"),
        (at(0, 1), "https://example.org/guitar/chords", "Guitar chords"),
    ])
    _chromium(local / "Perplexity" / "Comet" / "User Data" / "Default" / "History",
              [(at(90), "https://example.org/notes/linear-algebra", "Linear algebra notes")])
    _firefox(roaming / "Mozilla" / "Firefox" / "Profiles" / "ab12.default-release"
             / "places.sqlite",
             [(at(120), "https://example.org/python/typing", "Python typing guide")])


def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file()}


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(echo: Callable[[str], None] = print) -> bool:
    """Run every check, echo one PASS/FAIL line each and a total; True when all pass."""
    results: list[tuple[str, bool]] = []

    def check(name: str, fn: Callable[[], bool]) -> None:
        try:
            ok = bool(fn())
        except Exception as e:  # a crash is a failed check, not a traceback
            ok, name = False, f"{name} ({type(e).__name__})"
        results.append((name, ok))
        echo(f"{'PASS' if ok else 'FAIL'}  {name}")

    echo("browse selftest: synthetic history in a temp folder; your vault and browsers are "
         "not touched")
    old_tmp = tempfile.tempdir
    with tempfile.TemporaryDirectory(prefix="keepfold-selftest-") as tmp:
        base = Path(tmp)
        local, roaming, root, scratch = (base / n for n in ("local", "roaming", "vault",
                                                            "scratch"))
        root.mkdir()
        scratch.mkdir()
        now = datetime.now(UTC)
        _machine(local, roaming, now)
        srcs = sources.discover(local, roaming)
        before = {s.key: _digest(s.path) for s in srcs}
        # the owner's default mode (private pages only); adult kept private, not skipped, so
        # the check can see where it went
        settings = replace(privacy.Settings(adult="private"), visible_to_claude=False)
        vault = Vault(root)
        tempfile.tempdir = str(scratch)  # the History copies land here, so we can see them go
        try:
            check("finds Chrome, Comet and Firefox history",
                  lambda: [s.browser for s in srcs] == ["chrome", "comet", "firefox"])
            scan = sources.scan(srcs, settings)
            check("scan reads every source (counts only)",
                  lambda: all("error" not in r for r in scan)
                  and sum(r["visits"] for r in scan) == 10)
            check("scan: the privacy filter would drop the sign-in, bank and secret rows",
                  lambda: sum(r["dropped"] for r in scan) == 3)
            first = build.run(vault, srcs, settings, now=now)
            check("import reads every source", lambda: first.get("sources") == 3
                  and not first.get("sources_failed"))
            pages = {k: v for k, v in _files(root).items() if k.startswith("browse/")}
            check("pages written only under browse/private",
                  lambda: pages and all(k.startswith(PRIVATE_BROWSE_DIR + "/") for k in pages))
            every = b"".join(_files(root).values())
            check("sign-in, bank and secret rows are nowhere in the vault",
                  lambda: not any(m.encode() in every for m in DROPPED))
            check("sensitive rows (health, adult) are on private pages only",
                  lambda: all(m.encode() in b"".join(pages.values()) for m in SENSITIVE)
                  and all(privacy.filter_record(u, t, settings=settings).private for u, t in (
                      ("https://health.example.org/flu", "Flu symptoms"),
                      ("https://www.pornhub.com/x", "video")))
                  and all(b"private: true" in v for k, v in pages.items()
                          if k.endswith(".md")))
            snap = _files(root / "browse")
            since = min((r["first"] for r in scan if r.get("first")), default=None)
            again = build.run(vault, srcs, settings, now=now, since=since)
            check("a second import (full rebuild) gives the same pages",
                  lambda: _files(root / "browse") == snap and again.get("days_from"))
            whole = _files(root)
            dry = build.run(vault, srcs, settings, now=now, since=since, dry_run=True)
            check("--dry-run writes nothing",
                  lambda: _files(root) == whole and dry.get("private_would") == 2)
            check("temp copies of the history files are deleted",
                  lambda: not any(scratch.iterdir()))
            check("the browser history files are unchanged",
                  lambda: {s.key: _digest(s.path) for s in srcs} == before)
        except Exception as e:  # a step outside a check crashed: say so, never a traceback
            check(f"selftest stopped early ({type(e).__name__})", lambda: False)
        finally:
            tempfile.tempdir = old_tmp
    bad = sum(not ok for _, ok in results)
    echo(f"PASS: all {len(results)} checks passed" if not bad
         else f"FAIL: {bad} of {len(results)} checks failed")
    return not bad
