"""Synthetic inputs shared by the integration and E2E tests (docs/design/v6/TEST-PLAN.md §3):
a Chromium History file, a fake %LOCALAPPDATA% of browser profiles, a ChatGPT export zip, and
the fake Ollama. Never a real profile or export."""

import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

from e2e.fake_ollama import FakeOllama

from keepfold.browse import sources

fake_ollama = FakeOllama  # `with fake_ollama() as f:`; f.url, f.requests, f.chats()
EXPORTS = Path(__file__).parents[1] / "fixtures" / "exports"
LINK = 0x30000000  # Chromium's core transition LINK with the chain-start/end bits


def chrome_history(path: Path, rows) -> Path:
    """A Chromium History file at `path`. Each row is (when: aware datetime, url, title) with an
    optional search term; visit times are Chrome-epoch microseconds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
        "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER, "
        "visit_duration INTEGER, transition INTEGER);"
        "CREATE TABLE keyword_search_terms(keyword_id INTEGER, url_id INTEGER, term TEXT, "
        "normalized_term TEXT);")
    for t, url, title, *term in rows:
        uid = con.execute("INSERT INTO urls(url, title) VALUES (?, ?)", (url, title)).lastrowid
        raw = int((t - sources.EPOCH).total_seconds() * 1e6) + sources.CHROME_OFFSET_US
        con.execute("INSERT INTO visits(url, visit_time, visit_duration, transition) "
                    "VALUES (?, ?, 0, ?)", (uid, raw, LINK))
        if term:
            con.execute("INSERT INTO keyword_search_terms VALUES (1, ?, ?, ?)",
                        (uid, term[0], term[0].lower()))
    con.commit()
    con.close()
    return path


def fake_profile_root(tmp: Path, browsers: dict[str, list[tuple[datetime, str, str]]],
                      profile: str = "Default") -> Path:
    """`tmp` as %LOCALAPPDATA% holding one `profile` per Chromium browser (chrome, edge, brave,
    comet) with `rows` as its History. Returns `tmp`."""
    for browser, rows in browsers.items():
        chrome_history(tmp.joinpath(*sources.CHROMIUM[browser], "User Data", profile,
                                    "History"), rows)
    return tmp


def chatgpt_zip(tmp: Path) -> Path:
    """A ChatGPT export zip (conversations.json from tests/fixtures/exports)."""
    p = tmp / "chatgpt-export.zip"
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w") as z:
        z.write(EXPORTS / "chatgpt_conversations.json", "conversations.json")
    return p
