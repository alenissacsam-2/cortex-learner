"""The browse brain, phase 1 (docs/design/browse-brain.md): the §2 privacy filter (a positive
and a negative case per rule), discovery and read-only reading of synthetic Chromium and
Firefox History files built here (never real history), `cortex browse scan` (counts only), and
the import: day pages, private pages, the index, interest signals, "New interest?" lines,
idempotency, the extension inbox merge, the local LLM hooks and the dashboard lobe."""

import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from conftest import git, note
from typer.testing import CliRunner

from cortex import backup, dashboard, events, semantic
from cortex.browse import Settings, build, filter_record, load_settings, privacy, sources
from cortex.catalog import Catalog
from cortex.cli import app
from cortex.events import EventLog
from cortex.index import Index
from cortex.vault import Vault, secret_pattern

S = Settings()


def kept(url, title="A page", settings=S):
    return filter_record(url, title, settings=settings)


# ---- §2.1 hard drop ---------------------------------------------------------------------

@pytest.mark.parametrize("url", ["chrome://settings", "edge://history", "about:blank",
                                 "file:///C:/x.txt", "data:text/html,hi", "blob:https://a.b/x",
                                 "chrome-extension://abc/popup.html", "ftp://example.com/x"])
def test_non_web_schemes_drop(url):
    assert kept(url) is None


@pytest.mark.parametrize("url", ["http://localhost:8000/x", "http://127.0.0.1/", "http://[::1]/",
                                 "http://192.168.1.5/admin", "http://10.0.0.2/", "http://intranet/",
                                 "http://printer.local/", "http://dev.localhost/",
                                 "http://100.101.102.103:8080/admin", "http://198.18.0.1/"])
def test_local_hosts_drop(url):  # L1: CGNAT 100.64/10 and other non-global ranges too
    assert kept(url) is None


def test_public_hosts_and_ips_stay():
    assert kept("https://example.com/x") is not None
    assert kept("http://8.8.8.8/") is not None


@pytest.mark.parametrize("url", ["https://accounts.google.com/v3/signin/identifier",
                                 "https://example.com/login", "https://example.com/users/sign_in",
                                 "https://auth.example.com/", "https://example.com/oauth/authorize",
                                 "https://example.com/account/password-reset",
                                 "https://example.com/2fa", "https://example.com/verify/email",
                                 "https://sso.example.com/start"])
def test_sign_in_pages_drop(url):
    assert privacy.drop_reason(url) == "sign-in"


@pytest.mark.parametrize("url", ["https://example.com/author/jane",
                                 "https://authors.example.com/"])
def test_words_that_only_contain_sign_in_words_stay(url):
    assert privacy.drop_reason(url) is None


@pytest.mark.parametrize("url", ["https://shop.example.com/checkout/step1",
                                 "https://www.hdfcbank.com/personal", "https://www.paypal.com/x",
                                 "https://example.com/billing", "https://payments.example.com/"])
def test_payment_and_bank_pages_drop(url):
    assert privacy.drop_reason(url) == "payment"


@pytest.mark.parametrize("url", [
    "https://example.com/wp-login.php", "https://shop.example.com/customer_login",
    "https://example.com/forgot-password", "https://example.com/sessions/new",
    "https://example.com/%6Cogin",
    "https://example.com/reset-password/9f8e7d6c5b4a39281706f5e4d3c2b1a0",
    "https://example.com/blog/how-oauth-works"])
def test_m3_every_decoded_path_word_is_checked(url):
    """M3: any word of a decoded path segment, not just its first one."""
    assert privacy.drop_reason(url) == "sign-in"


@pytest.mark.parametrize("url,title", [
    ("https://www.sbicard.com/en/personal/account-summary.page", "Account Summary | SBI Card"),
    ("https://www.sc.com/in/digital-banking/", "Online Banking | Standard Chartered India"),
    ("https://www.sc.com/in/digital-banking/", "x"),
    ("https://netbanking.example.in/", "x"), ("https://www.mybank.example/", "x"),
    ("https://e.com/x", "Net Banking - Login"), ("https://e.com/x", "Your bank statement")])
def test_m2_bank_hosts_paths_and_titles_drop(url, title):
    assert kept(url, title) is None


def test_an_article_about_payments_stays():
    assert privacy.drop_reason("https://example.com/blog/how-payments-work") is None


def test_webmail_keeps_the_site_name_only():
    k = kept("https://mail.google.com/mail/u/0/#inbox/FMfcgzXYZ", "Re: secret plans - Gmail")
    assert (k.url, k.title, k.host) == ("https://mail.google.com/", "mail.google.com",
                                        "mail.google.com")
    other = kept("https://example.com/news", "Re: secret plans")
    assert other.title == "Re: secret plans"


@pytest.mark.parametrize("url", ["https://example.com/cb?token=abc", "https://e.com/x?code=1",
                                 "https://e.com/x?KEY=1", "https://e.com/x?sessionid=9",
                                 "https://e.com/x?sig=2", "https://e.com/x?apikey=3",
                                 "https://e.com/x?refresh_token=4", "https://e.com/#access_token=5",
                                 "https://e.com/x?client_secret=6", "https://user:pw@e.com/"])
def test_urls_carrying_secrets_drop(url):
    assert privacy.drop_reason(url) == "secret"


def test_harmless_query_keys_stay_but_are_scrubbed():
    assert privacy.drop_reason("https://e.com/x?page=2&authuser=0") is None
    assert kept("https://e.com/x?page=2#top").url == "https://e.com/x"


def test_a_title_or_url_matching_a_vault_secret_pattern_drops():
    assert kept("https://e.com/x", "my key sk-" + "a" * 30) is None
    assert kept("https://e.com/x", "a normal title") is not None


@pytest.mark.parametrize("q", ["api_key%3Dabcdefghijklmnop", "password%3A+hunter2222",
                               "my+token+eyJhbGciOiJI%2EeyJzdWIiOiIx%2Eabcdefghij"])
def test_m1_a_decoded_search_term_holding_a_secret_drops(q):
    """M1: the reviewer's terms; the check runs on the decoded term, after the scrub."""
    assert kept(f"https://www.google.com/search?q={q}", "x") is None
    assert kept("https://e.com/x", "password: hunter2222") is None


# ---- §2.2 scrubbing -----------------------------------------------------------------------

@pytest.mark.parametrize("url,engine,term", [
    ("https://www.google.com/search?q=binary+search&oq=b&sourceid=chrome", "google",
     "binary search"),
    ("https://www.google.co.in/search?q=dbms", "google", "dbms"),
    ("https://www.bing.com/search?q=os+paging&form=QBLH", "bing", "os paging"),
    ("https://duckduckgo.com/?q=heap+sort&ia=web", "duckduckgo", "heap sort"),
    ("https://search.brave.com/search?q=tries", "brave", "tries"),
    ("https://www.youtube.com/results?search_query=dp+tutorial&sp=x", "youtube", "dp tutorial"),
    ("https://www.perplexity.ai/search?q=what+is+raft", "perplexity", "what is raft"),
    ("https://www.amazon.in/s?k=usb+hub&ref=nb", "amazon", "usb hub"),
    ("https://github.com/search?q=fastmcp&type=code", "github", "fastmcp"),
])
def test_search_terms_survive_on_known_engines(url, engine, term):
    k = kept(url, "results")
    assert (k.engine, k.search) == (engine, term)
    assert k.url.count("?") == 1 and "&" not in k.url


def test_unknown_sites_lose_their_query_even_when_named_q():
    k = kept("https://example.com/search?q=private+thing", "x")
    assert k.url == "https://example.com/search" and k.search is None


def test_youtube_keeps_only_the_video_id():
    k = kept("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1&t=30s",
             "(3) A talk - YouTube")
    assert k.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert (k.video, k.title) == ("dQw4w9WgXcQ", "A talk")
    assert kept("https://youtu.be/dQw4w9WgXcQ?si=abc", "x").video == "dQw4w9WgXcQ"
    assert kept("https://www.youtube.com/feed/history?x=1", "x").video is None


# ---- §2.3 user lists ----------------------------------------------------------------------

def test_deny_allow_and_private_domains():
    s = Settings(deny=("news.com",), private=("diary.net",))
    assert kept("https://www.news.com/a", settings=s) is None
    assert kept("https://news.org/a", settings=s) is not None
    assert kept("https://my.diary.net/a", settings=s).private
    assert not kept("https://diary.org/a", settings=s).private
    allow = Settings(allow=("wikipedia.org",))
    assert kept("https://en.wikipedia.org/wiki/X", settings=allow) is not None
    assert kept("https://example.com/", settings=allow) is None


def test_settings_from_config_and_defaults():
    assert load_settings({}) == Settings(adult="skip", sensitive="private")
    s = load_settings({"browse": {"adult": "private", "sensitive": "skip",
                                  "deny_domains": ["A.com", ".b.org"], "private_domains": "c.net",
                                  "allow_domains": None}})
    assert (s.adult, s.sensitive, s.deny, s.private, s.allow) == (
        "private", "skip", ("a.com", "b.org"), ("c.net",), ())
    assert load_settings({"browse": {"adult": "show", "sensitive": "public"}}) == Settings()
    assert not load_settings({}).visible_to_claude
    assert load_settings({"browse": {"visible_to_claude": True}}).visible_to_claude
    assert not load_settings({"browse": {"visible_to_claude": "yes"}}).visible_to_claude


def test_unicode_domain_in_lists_matches_the_punycode_host():
    """G1: a list entry typed in Unicode must match the host the filter stores as punycode."""
    s = load_settings({"browse": {"private_domains": ["bücher.de"], "deny_domains": ["яндекс.рф"],
                                  "visible_to_claude": True}})
    assert s.private == ("xn--bcher-kva.de",)
    assert filter_record("https://bücher.de/katalog", "Katalog", settings=s).private
    assert filter_record("https://яндекс.рф/search", "x", settings=s) is None
    bad = "ü" * 70 + ".de"  # no valid IDNA form (label too long): kept as typed
    assert load_settings({"browse": {"allow_domains": [bad]}}).allow == (bad,)


# ---- §2.4 sensitive categories --------------------------------------------------------------

def test_adult_follows_browse_adult_skip_by_default():
    assert kept("https://www.pornhub.com/view?viewkey=1", "x") is None
    k = kept("https://www.pornhub.com/view", "x", Settings(adult="private"))
    assert (k.category, k.private, k.maybe) == ("adult", True, False)
    assert kept("https://example.xxx/", "x") is None
    assert kept("https://forum.example.com/r/nsfw/1", "a thread") is None  # a STRONG word


def test_a_soft_adult_word_is_a_maybe_and_a_plain_page_is_not():
    k = privacy.judge("https://e.com/a", "Sexy cars of 2026", settings=S)
    assert (k.category, k.maybe) == ("adult", True)
    assert privacy.judge("https://e.com/a", "Fast cars of 2026", settings=S).category is None


@pytest.mark.parametrize("url,title,cat", [
    ("https://www.webmd.com/x", "x", "health"), ("https://e.com/a", "Flu symptoms", "health"),
    ("https://kite.zerodha.com/dashboard", "x", "money"),
    ("https://e.com/a", "Open a demat account", "money"),
    ("https://www.dream11.com/", "x", "betting"), ("https://e.com/a", "Best casino", "betting"),
    ("https://tinder.com/app", "x", "dating"), ("https://e.com/a", "Dating tips", "dating"),
])
def test_sensitive_categories_go_private_by_default(url, title, cat):
    k = kept(url, title)
    assert (k.category, k.private) == (cat, True)
    assert kept(url, title, Settings(sensitive="skip")) is None


@pytest.mark.parametrize("title", ["C++ std::vector reference", "Problem statement: two sum",
                                   "Python __slots__ explained", "Loan prediction dataset"])
def test_student_words_are_not_sensitive(title):
    k = kept("https://e.com/a", title)
    assert (k.category, k.private) == (None, False)


@pytest.mark.parametrize("url,title,cat", [
    ("https://www.google.com/search?q=xvideos", "t", "adult"),
    ("https://www.google.com/search?q=naked+women", "t", "adult"),
    ("https://www.google.com/search?q=herpes+treatment", "t", "health"),
    ("https://www.google.com/search?q=am+i+gay+quiz", "t", "sexuality"),
    ("https://www.google.com/search?q=depression+medication+side+effects", "t", "health"),
    ("https://www.google.com/search?q=teen+patti+real+money", "t", "betting"),
    ("https://www.amazon.in/s?k=vibrator", "t", "adult"),
    ("https://www.youtube.com/results?search_query=hot+girls+bikini", "t", "adult"),
    ("https://www.xvideos2.com/video123/x", "Hot stepmom gets surprised - XVIDEOS.COM", "adult"),
    ("https://xhamster.desi/videos/x", "Desi bhabhi video", "adult"),
    ("https://www.youtube.com/watch?v=abcdefgh", "Nude beach vlog", "adult"),
    ("https://www.youtube.com/watch?v=abcdefgh", "lesbian couple story", "sexuality"),
    ("https://www.youtube.com/watch?v=abcdefgh", "Escort services Delhi", "adult"),
    ("https://www.youtube.com/watch?v=abcdefgh", "OnlyFans leak", "adult"),
    ("https://www.youtube.com/watch?v=abcdefgh", "boobs compilation", "adult"),
    ("https://e.com/a", "BOOBS Compilation", "adult"),  # casefolded
    ("https://e.com/a", "Ｈｅｒｐｅｓ treatment", "health"),  # fullwidth: Unicode words
])
def test_h1_the_reviewers_sensitive_examples_get_a_category(url, title, cat):
    k = kept(url, title, ADULT_PRIVATE)
    assert (k.category, k.private) == (cat, True)


def test_h1_unicode_words_and_brand_prefixes():
    assert privacy._words("सेक्स वीडियो")  # not ASCII-only any more
    assert privacy.category("xhamster.desi", "/", "x") == ("adult", False)
    assert privacy.category("www.xvideos2.com", "/", "x") == ("adult", False)
    assert privacy.category("milford.gov", "/", "x") == (None, False)  # a brand, not a word


# ---- synthetic History files --------------------------------------------------------------

T0 = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)  # a Monday, mid-morning in UTC
LINK, NEG_LINK, SUBFRAME = 0x30000000, -0x60000000, 3


def chrome_raw(t):
    return int((t - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds() * 1e6) \
        + sources.CHROME_OFFSET_US


def chromium(path, rows, wal_rows=()):
    """A Chromium History file; `wal_rows` stay in the -wal (the writer is returned open)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
        "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER, "
        "visit_duration INTEGER, transition INTEGER);"
        "CREATE TABLE keyword_search_terms(keyword_id INTEGER, url_id INTEGER, term TEXT, "
        "normalized_term TEXT);")

    def add(rows):
        for t, url, title, *rest in rows:
            dur, tr, term = [*rest, 0, LINK, None][:3] if rest else (0, LINK, None)
            uid = con.execute("INSERT INTO urls(url, title) VALUES (?, ?)", (url, title)).lastrowid
            con.execute("INSERT INTO visits(url, visit_time, visit_duration, transition) "
                        "VALUES (?, ?, ?, ?)", (uid, chrome_raw(t), int(dur * 1e6), tr))
            if term:
                con.execute("INSERT INTO keyword_search_terms VALUES (1, ?, ?, ?)",
                            (uid, term, term))
    add(rows)
    con.commit()
    if not wal_rows:
        con.close()
        return None
    con.execute("PRAGMA journal_mode=WAL")
    add(wal_rows)
    con.commit()
    return con


def firefox(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                      "CREATE TABLE moz_historyvisits(id INTEGER PRIMARY KEY, place_id INTEGER, "
                      "visit_date INTEGER, visit_type INTEGER);")
    for t, url, title, vtype in rows:
        pid = con.execute("INSERT INTO moz_places(url, title) VALUES (?, ?)",
                          (url, title)).lastrowid
        con.execute("INSERT INTO moz_historyvisits(place_id, visit_date, visit_type) "
                    "VALUES (?, ?, ?)", (pid, int(t.timestamp() * 1e6), vtype))
    con.commit()
    con.close()


def m(minutes, day=0):
    return T0 + timedelta(days=day, minutes=minutes)


CHROME_ROWS = [
    (m(0), "https://lichess.org/study/abc", "Chess openings for beginners"),
    (m(10), "https://lichess.org/study/def", "Chess middlegame plans"),
    (m(20), "https://www.chess.com/lessons/1", "Chess tactics lesson"),
    (m(30), "https://lichess.org/study/abc", "Chess openings for beginners"),
    (m(40), "https://ads.example.com/frame", "an ad", 0, SUBFRAME),
    (m(45), "https://www.google.com/search?q=chess+openings&oq=c", "chess openings - Google",
     0, LINK, "chess openings"),
    (m(50), "https://www.youtube.com/watch?v=abcdefghijk&t=3", "Chess endgame - YouTube"),
    (m(60), "https://www.pornhub.com/view_video.php?viewkey=zz", "ADULTMARKER video"),
    (m(70), "https://accounts.google.com/signin", "SIGNINMARKER"),
    (m(80), "https://example.com/cb?token=TOKENMARKER", "Callback DROPMARKER"),
    (m(90), "https://health.example.org/flu", "Flu symptoms HEALTHMARKER"),
    (m(180), "https://food.example.com/pasta", "Pasta recipe cooking", 300, NEG_LINK),
    (m(300), "https://music.example.com/g1", "Guitar chords"),
    (m(310), "https://music.example.com/g2", "Guitar strumming"),
    (m(320), "https://music.example.com/g3", "Guitar scales"),
    (m(300, 1), "https://music.example.com/g1", "Guitar chords"),
    (m(310, 1), "https://music.example.com/g4", "Guitar songs"),
]


@pytest.fixture
def browsers(tmp_path):
    local, roaming = tmp_path / "local", tmp_path / "roaming"
    ud = local / "Google" / "Chrome" / "User Data"
    chromium(ud / "Default" / "History", CHROME_ROWS)
    chromium(ud / "Profile 1" / "History",
             [(m(0, 1), "https://chess.com/puzzles", "Chess puzzles")])
    (ud / "System Profile").mkdir()  # no History file: not a source
    chromium(local / "Microsoft" / "Edge" / "User Data" / "Default" / "History",
             [(m(120), "https://lichess.org/learn", "Chess basics")])
    firefox(roaming / "Mozilla" / "Firefox" / "Profiles" / "ab12.default-release" / "places.sqlite",
            [(m(130), "https://lichess.org/practice", "Chess practice", 1),
             (m(131), "https://embed.example.com/x", "embedded", 4)])
    return local, roaming


def test_discover_finds_comet_profiles(tmp_path):
    ud = tmp_path / "local" / "Perplexity" / "Comet" / "User Data"
    chromium(ud / "Default" / "History", [(m(0), "https://chess.com/learn", "Chess lessons")])
    assert [s.key for s in sources.discover(tmp_path / "local", None)] == ["comet/Default"]


def test_browsers_setting_limits_the_sources(browsers):
    found = sources.discover(*browsers)
    only = load_settings({"browse": {"browsers": ["Edge"]}})
    assert only.browsers == ("edge",)
    assert [s.key for s in sources.chosen(only, found)] == ["edge/Default"]
    assert sources.chosen(load_settings({}), found) == found  # unset: every browser found
    assert load_settings({"browse": {"browsers": "comet"}}).browsers == ("comet",)


def test_discover_finds_every_profile_and_firefox(browsers):
    keys = [s.key for s in sources.discover(*browsers)]
    assert keys == ["chrome/Default", "chrome/Profile 1", "edge/Default",
                    "firefox/ab12.default-release"]
    assert sources.discover(browsers[0].parent / "nothing", None) == []
    picked = sources.pick(sources.discover(*browsers), "chrome", "profile 1")
    assert [s.key for s in picked] == ["chrome/Profile 1"]


def _digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns


def test_read_converts_times_skips_frames_and_never_writes(browsers, tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    src = sources.discover(*browsers)[0]
    before = _digest(src.path)
    visits, top = sources.read(src)
    assert _digest(src.path) == before and list((tmp_path / "tmp").iterdir()) == []
    assert top == chrome_raw(m(310, 1))
    assert visits[0].start == m(0) and visits[0].title == "Chess openings for beginners"
    assert "an ad" not in [v.title for v in visits]  # a subframe is not a page
    assert visits[0].secs == 600  # until the next visit, at most GAP_CAP
    pasta = next(v for v in visits if "Pasta" in v.title)
    assert pasta.secs == 300  # its recorded duration is shorter than the gap
    assert next(v for v in visits if v.term).term == "chess openings"
    ff = sources.read(sources.discover(*browsers)[-1])[0]
    assert [v.title for v in ff] == ["Chess practice"] and ff[0].start == m(130)


def test_read_sees_rows_still_in_the_wal(tmp_path):
    path = tmp_path / "Default" / "History"
    con = chromium(path, [(m(0), "https://a.com/", "A")], wal_rows=[(m(5), "https://b.com/", "B")])
    try:
        assert path.with_name("History-wal").is_file()
        visits, _ = sources.read(sources.Source("chrome", "Default", path))
        assert [v.title for v in visits] == ["A", "B"]
    finally:
        con.close()


def test_scan_counts_only(browsers):
    rows = sources.scan(sources.discover(*browsers), Settings(adult="private"), tz=UTC)
    d = rows[0]
    assert d["source"] == "chrome/Default" and d["visits"] == 16 and d["searches"] == 1
    assert (d["first"], d["last"]) == ("2026-11-09", "2026-11-10")
    assert (d["dropped"], d["private"], d["kept"]) == (2, 2, 12)
    assert all(not isinstance(v, str) or "MARKER" not in v for r in rows for v in r.values())


def test_scan_reports_an_unreadable_file(tmp_path):
    bad = tmp_path / "Default" / "History"
    bad.parent.mkdir()
    bad.write_bytes(b"not a database")
    assert sources.scan([sources.Source("chrome", "Default", bad)], S) == [
        {"source": "chrome/Default", "error": "DatabaseError",
         "why": "its history file is not a readable database"}]


# ---- the import ---------------------------------------------------------------------------

AXES = {"chess": 0, "cooking": 1, "recipe": 1, "pasta": 1, "guitar": 2}


class Embed:
    def __call__(self, texts):
        out = np.zeros((len(texts), 4), dtype=np.float32)
        for k, t in enumerate(texts):
            for w in t.lower().split():
                if w in AXES:
                    out[k, AXES[w]] += 1
        return out


def hubbed(r):
    for hub, word in (("chess", "chess"), ("cooking", "cooking recipe")):
        note(r, f"notes/{hub}.md", f'title: "{hub.title()} hub"\nkind: note\n'
             'note_type: structure\ntags: ["topic"]\nrelated: []\n', f"# {hub.title()}\n")
        for k in range(3):
            note(r, f"resources/{hub}-{k}.md", f'title: "{hub} page {k}"\nkind: resource\n'
                 f'related: ["[[{hub}]]"]\n', f"# {hub} page\n\n{word} {word}\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "hubs")
    return r


@pytest.fixture
def vault(root):
    return hubbed(root)


ADULT_PRIVATE = Settings(adult="private")


def run_import(r, browsers, settings=ADULT_PRIVATE, visible=True, **kw):
    """The tests below check the public pages, so browse.visible_to_claude is on unless
    `visible=False` (the code default)."""
    kw.setdefault("embed", Embed())
    settings = replace(settings, visible_to_claude=visible)
    return build.run(Vault(r), sources.discover(*browsers), settings, tz=UTC,
                     now=m(0, 2), **kw)


def visible_files(r):
    """Every file Claude may see under browse/: all but browse/private/."""
    return [p for p in (r / "browse").rglob("*")
            if p.is_file() and not p.is_relative_to(r / "browse" / "private")]


def files(r):
    return {p.relative_to(r).as_posix(): p.read_bytes() for p in r.rglob("*")
            if p.is_file() and ".git" not in p.parts}


def test_every_long_phase_says_how_far_it_got_in_counts_only(vault, browsers, monkeypatch):
    """The owner saw minutes of silence: each phase reports (every PROGRESS_S, here every
    step), and no line carries a title, URL or search term."""
    from cortex.browse import sites, vectors

    monkeypatch.setattr(build, "PROGRESS_S", 0)
    monkeypatch.setattr(sites, "PROGRESS_S", 0)
    monkeypatch.setattr(semantic, "PROGRESS_S", 0)
    monkeypatch.setattr(semantic, "_embedder", Embed())
    lines: list[str] = []
    run_import(vault, browsers, say=lines.append)
    vectors.update(Vault(vault), progress=lines.append)
    text = "\n".join(lines)
    for phase in ("reading its history", "filtering: ", "topics: embedded ", "topics: grouped ",
                  "day pages: ", "sites: counting", "sites: making", "files checked",
                  "private search: ", "embedded "):
        assert phase in text, phase
    for row in CHROME_ROWS:
        assert row[2].lower() not in text.lower() and "lichess" not in text


def test_import_writes_day_private_and_index_pages(vault, browsers):
    rep = run_import(vault, browsers)
    assert rep["days_from"] == "2026-11-09" and rep["public_written"] == 2
    day = (vault / "browse" / "browse-2026-11-09.md").read_text(encoding="utf-8")
    assert 'kind: "browse"' in day and "Up: [[browse-index]]" in day
    assert ("## Topics\n- [[chess]] · 1 h 10 min · 5 pages, 1 video, 1 search\n"
            "- [[cooking]] · 5 min · 1 page\n- 3 other pages\n") in day
    assert "What I was into: Chess hub (1 h 10 min), Cooking hub (5 min)" in day
    assert "Details are on the private page in Obsidian." in day
    public_only(day, {"Chess hub", "Cooking hub"})
    for marker in ("ADULTMARKER", "HEALTHMARKER", "SIGNINMARKER", "DROPMARKER", "TOKENMARKER",
                   "an ad", "?q=", "http", "lichess", "Chess openings", "chess openings",
                   "Chess endgame", "google", "youtube", "Guitar", "Pasta"):
        assert marker not in day, marker  # second review: no third-party text, ever
    priv = (vault / "browse" / "private" / "browse-private-2026-11-09.md").read_text(
        encoding="utf-8")
    assert "private: true" in priv and "ADULTMARKER" in priv and "HEALTHMARKER" in priv
    assert "### Chess hub · " in priv
    assert "- Chess openings for beginners · lichess.org · 20 min · 2 visits" in priv
    assert "## Searches\n- 09:45 · chess openings · google" in priv
    assert "## Videos\n- 09:50 · Chess endgame · " in priv
    assert "## Top sites\n- lichess.org · " in priv
    assert "[[" not in priv  # a private page links to no hub (no backlink on the hub)
    assert "Guitar chords" in priv
    idx = (vault / "browse" / "browse-index.md").read_text(encoding="utf-8")
    assert "### 2026-11\n- [[browse-2026-11-10]]\n- [[browse-2026-11-09]]" in idx
    assert "## Topics by time\n- [[chess]] · 2 days · 1 h 20 min\n" in idx
    for marker in ("lichess", "pornhub", "Sites", "chess.com"):
        assert marker not in idx, marker
    pidx = (vault / "browse" / "private" / "browse-private-index.md").read_text(
        encoding="utf-8")
    assert "private: true" in pidx and "## Sites I return to" in pidx
    assert "- lichess.org · 1 day · " in pidx and "- pornhub.com · 1 day" in pidx
    every = b"".join(files(vault).values())
    for marker in (b"SIGNINMARKER", b"DROPMARKER", b"TOKENMARKER", b"an ad"):
        assert marker not in every, marker  # dropped means stored nowhere
    state = json.loads((vault / build.STATE).read_text(encoding="utf-8"))
    assert set(state["sources"]["chrome/Default"]) == {"cursor", "ran", "size", "mtime_ns"}
    assert "MARKER" not in json.dumps(state) and "lichess" not in json.dumps(state)


def test_import_twice_gives_the_same_pages(vault, browsers):
    run_import(vault, browsers)
    first = files(vault)
    assert run_import(vault, browsers).get("days_from") is None  # nothing new
    rep = run_import(vault, browsers, since="2026-11-09")
    assert rep["public_same"] == 2 and rep.get("public_written", 0) == 0
    after = files(vault)  # .cortex holds the state and caches (the new pages get vectors)
    assert {k: v for k, v in after.items() if not k.startswith(".cortex/")} == \
        {k: v for k, v in first.items() if not k.startswith(".cortex/")}


def test_new_visits_rebuild_their_week_only(vault, browsers):
    run_import(vault, browsers)
    path = browsers[0] / "Google" / "Chrome" / "User Data" / "Profile 1" / "History"
    con = sqlite3.connect(path)
    uid = con.execute("INSERT INTO urls(url, title) VALUES ('https://lichess.org/new', "
                      "'Chess news')").lastrowid
    con.execute("INSERT INTO visits(url, visit_time, visit_duration, transition) VALUES "
                "(?, ?, 0, ?)", (uid, chrome_raw(m(0, 8)), LINK))
    con.commit()
    con.close()
    rep = run_import(vault, browsers)
    assert rep["days_from"] == "2026-11-16" and rep["public_written"] == 1
    assert "[[chess]] · " in (vault / "browse" / "browse-2026-11-17.md").read_text(
        encoding="utf-8")
    assert "Chess news" in (vault / "browse" / "private" / "browse-private-2026-11-17.md"
                            ).read_text(encoding="utf-8")


def test_interest_signal_once_per_hub_and_week(vault, browsers):
    assert run_import(vault, browsers)["signals"] == 1
    journal = (vault / "journal" / "2026-11-10.md").read_text(encoding="utf-8")
    assert "Browsing: spent 1 h 20 min on Chess hub this week (2 days)" in journal
    ev = [e for e in EventLog(Vault(vault)).iter_events()
          if (e.get("payload") or {}).get("kind") == "touch"]
    assert len(ev) == 1 and ev[0]["payload"]["ids"] == ["chess"]
    assert (ev[0]["source"], ev[0]["session"], ev[0]["day"]) == ("imported", "cli", "2026-11-10")
    assert ev[0]["key"] == "browse:touch:chess:2026-11-09"
    run_import(vault, browsers, since="2026-11-09")
    assert journal == (vault / "journal" / "2026-11-10.md").read_text(encoding="utf-8")
    assert len([e for e in EventLog(Vault(vault)).iter_events()
                if (e.get("payload") or {}).get("kind") == "touch"]) == 1


def test_default_hides_browsing_from_claude(vault, browsers):
    """browse.visible_to_claude off (the code default): only private pages and the state file
    change; no public page or index, journal line, touch event or inbox line."""
    before = files(vault)
    rep = run_import(vault, browsers, visible=False)
    after = files(vault)
    changed = {p for p in after.keys() | before.keys() if before.get(p) != after.get(p)}
    changed.discard(".cortex/vectors-v3.npz")  # the notes' embedding cache, from the hubs only
    changed.discard(".gitignore")  # C3: the run adds the private-pages ignore line once
    assert changed and all(p.startswith(("browse/private/", ".cortex/browse/"))
                           for p in changed), changed
    assert rep["visible_to_claude"] is False and rep["signals"] == 0
    assert rep["sites_visible"] == 0 and rep["sites_hidden"] > 0
    assert "public_written" not in rep and not any((vault / "browse").glob("*.md"))
    priv = (vault / "browse" / "private" / "browse-private-2026-11-09.md").read_text(
        encoding="utf-8")
    assert ("## Hubs\n- [[chess]] · 1 h 10 min · 5 pages, 1 video, 1 search\n"
            "- [[cooking]] · 5 min · 1 page\n- 3 other pages\n") in priv
    idx = (vault / "browse" / "private" / "browse-private-index.md").read_text(encoding="utf-8")
    assert "- [[browse-private-2026-11-09]]" in idx and "## Topics by time\n- [[chess]] · " in idx
    assert "## Sites I return to" in idx


def test_switching_visibility_off_removes_the_public_pages(vault, browsers):
    run_import(vault, browsers)
    assert (vault / "browse" / "browse-2026-11-09.md").is_file()
    assert (vault / "browse" / "browse-index.md").is_file()
    rep = run_import(vault, browsers, visible=False, since="2026-11-09")
    assert rep["public_removed"] == 2 and not any((vault / "browse").glob("*.md"))
    assert (vault / "browse" / "private" / "browse-private-2026-11-09.md").is_file()


def test_a_far_cluster_goes_private_and_writes_no_inbox_line(vault, browsers):
    """H1: pages under no topic hub are private, so no "New interest?" label carries them."""
    rep = run_import(vault, browsers)
    assert "new_interests" not in rep
    assert not (vault / "inbox.md").exists() or "New interest?" not in (
        vault / "inbox.md").read_text(encoding="utf-8")
    priv = (vault / "browse" / "private" / "browse-private-2026-11-09.md").read_text(
        encoding="utf-8")
    assert "### Other: guitar" in priv


def test_hub_floor_places_and_everything_else_is_private(tmp_path):
    """A page is public only at HUB_FLOOR to its nearest hub; under it, it is private and
    clustered by itself ("Other"), keyed (url, True)."""
    def item(title, n, private=False):
        return build.Item(f"2026-11-0{n % 2 + 1}", m(n), 60,
                          privacy.Kept(f"https://e.com/{n}", title, "e.com", private=private))

    items = [item("chess", 0), *[item("chess guitar", n) for n in (1, 2)],
             item("guitar", 3), item("chess", 4, private=True)]
    where = build.place(items, Embed(), ["chess", "cooking"], np.eye(4, dtype=np.float32)[:2])
    assert where[("https://e.com/0", False)] == ("hub", "chess")
    assert where[("https://e.com/1", True)][0] == "other"  # cosine 0.707 < HUB_FLOOR
    assert ("https://e.com/1", False) not in where
    assert [build.public(i, where) for i in items] == [True, False, False, False, False]
    assert build.place(items, None, [], None)[("https://e.com/0", True)] == ("site", "e.com")


def test_other_clusters_match_the_one_by_one_greedy_join():
    """The vectorised "Other" clustering joins exactly as the per-group loop it replaced."""
    rng = np.random.default_rng(3)
    raw = np.repeat(rng.standard_normal((6, 8)), 50, axis=0) + 0.4 * rng.standard_normal((300, 8))
    vec = (raw / np.linalg.norm(raw, axis=1, keepdims=True)).astype(np.float32)
    far = list(rng.permutation(300))
    groups, cents = [], []
    for n in far:  # the old loop
        sims = [float(c @ vec[n]) / (np.linalg.norm(c) or 1.0) for c in cents]
        j = int(np.argmax(sims)) if sims else -1
        if j >= 0 and sims[j] >= build.JOIN:
            groups[j].append(n)
            cents[j] = cents[j] + vec[n]
        else:
            groups.append([n])
            cents.append(vec[n].copy())
    assert 6 <= len(groups) < 300
    assert build._clusters(vec, far) == groups


def test_without_an_embedder_everything_is_private(vault, browsers):
    """H1: with no embedder nothing is under a hub, so nothing is public."""
    rep = run_import(vault, browsers, embed=None)
    assert not (vault / "browse" / "browse-2026-11-09.md").exists()
    priv = (vault / "browse" / "private" / "browse-private-2026-11-09.md").read_text(
        encoding="utf-8")
    assert "### lichess.org · " in priv and "Topic: [[" not in priv
    assert rep["signals"] == 0 and rep.get("kept", 0) == 0


# ---- privacy review regressions (C1, H1, H2, M1, M5, L2, L3, L4) ----------------------------

class FarEmbed:
    """"python" sits on the hub's axis; every other text is far from it."""
    def __call__(self, texts):
        out = np.zeros((len(texts), 3), np.float32)
        for k, t in enumerate(texts):
            out[k, 0 if "python" in t.lower() else 2] = 1
        return out


HUB = (["python"], np.array([[1, 0, 0]], np.float32), {"python": "Python hub"})


def place_and_write(tmp_path, items, day="2026-11-10"):
    v = Vault(tmp_path)
    where = build.place(items, FarEmbed(), HUB[0], HUB[1])
    build.write_day(v, day, items, where, HUB[2], tz=UTC, visible=True)
    pub = tmp_path / "browse" / f"browse-{day}.md"
    priv = tmp_path / "browse" / "private" / f"browse-private-{day}.md"
    return v, where, (pub.read_text(encoding="utf-8") if pub.exists() else ""), (
        priv.read_text(encoding="utf-8") if priv.exists() else "")


def it(url, title, h, secs=300, settings=ADULT_PRIVATE):
    k = privacy.filter_record(url, title, settings=settings)
    return build.Item("2026-11-10", datetime(2026, 11, 10, h, tzinfo=UTC), secs, k)


_PUBLIC = re.compile(
    r"(|# Browsing \d{4}-\d{2}-\d{2}|Up: \[\[browse-index\]\]|## Topics"
    r"|Details are on the private page in Obsidian\.|- \d+ other pages?"
    r"|- \[\[[a-z0-9-]+\]\] · (?:\d+ h(?: \d+ min)? · |\d+ min · )?"
    r"\d+ (?:pages?|videos?|search(?:es)?)(?:, \d+ (?:pages?|videos?|search(?:es)?))*)")


def public_only(text, hub_titles):
    """Second review: a public day page holds hub links, times, counts and the hub names in
    its summary line, and nothing else."""
    body = text.split("---\n", 2)[2] if text.startswith("---\n") else text
    for line in body.splitlines():
        if line.startswith(build.SUMMARY_HEAD):
            rest = line[len(build.SUMMARY_HEAD):]
            for part in rest.split(", "):
                assert re.sub(r" \(\d+ (?:h|min)(?: \d+ min)?\)$", "", part) in hub_titles, line
        else:
            assert _PUBLIC.fullmatch(line), line


def test_c1_a_private_title_never_heads_a_public_page_on_a_shared_url(tmp_path):
    """C1: the reviewer's two forum threads share one scrubbed URL; one is private."""
    items = [it("https://forum.example.com/viewtopic.php?t=1", "HIV symptoms forum thread", 9,
                secs=2000),
             it("https://forum.example.com/viewtopic.php?t=2", "python help thread", 10,
                secs=2000),
             it("https://forum.example.com/viewtopic.php?t=1", "HIV symptoms forum thread", 11,
                secs=2000)]
    assert [i.k.private for i in items] == [True, False, True]
    assert items[0].k.url == items[1].k.url
    v, where, pub, priv = place_and_write(tmp_path, items)
    assert "- [[python]] · 33 min · 1 page" in pub and "python help thread" in priv
    assert "hiv" not in pub.lower() and "symptoms" not in pub.lower() and "Other" not in pub
    assert "forum" not in pub and "HIV symptoms forum thread" in priv
    assert build.signals(v, items, where, HUB[2], UTC) == 1
    journal = (tmp_path / "journal" / "2026-11-10.md").read_text(encoding="utf-8")
    assert "10:00" in journal and "11:00" not in journal and "hiv" not in journal.lower()
    ev = [e for e in EventLog(v).iter_events() if (e.get("payload") or {}).get("kind")]
    assert len(ev) == 1 and ev[0]["ts"].startswith("2026-11-10T10:00")


PROBE = [  # the reviewer's probe.py inputs that survive the filter
    ("https://www.google.com/search?q=naked+women", "t"),
    ("https://www.google.com/search?q=herpes+treatment", "t"),
    ("https://www.google.com/search?q=am+i+gay+quiz", "t"),
    ("https://www.google.com/search?q=depression+medication+side+effects", "t"),
    ("https://www.google.com/search?q=how+to+get+a+personal+loan+with+bad+credit", "t"),
    ("https://www.google.com/search?q=teen+patti+real+money", "t"),
    ("https://www.amazon.in/s?k=vibrator", "t"),
    ("https://www.youtube.com/results?search_query=hot+girls+bikini", "t"),
    ("https://www.google.com/search?q=xvideos", "t"),
    ("https://www.xvideos2.com/video123/x", "Hot stepmom gets surprised - XVIDEOS.COM"),
    ("https://xhamster.desi/videos/x", "Desi bhabhi video"),
    ("https://www.google.com/url?q=https://pornhub.com/view_video.php", "t"),
    ("https://www.youtube.com/watch?v=abcdefgh", "सेक्स वीडियो"),
    ("https://www.youtube.com/watch?v=abcdefgh", "Nude beach vlog"),
    ("https://www.youtube.com/watch?v=abcdefgh", "lesbian couple story"),
    ("https://www.youtube.com/watch?v=abcdefgh", "Escort services Delhi"),
    ("https://www.youtube.com/watch?v=abcdefgh", "OnlyFans leak"),
    ("https://www.youtube.com/watch?v=abcdefgh", "boobs compilation"),
    ("https://news.ycombinator.com/item?id=1", "t"),
]
DROPPED = ["https://example.com/wp-login.php", "https://shop.example.com/customer_login",
           "https://example.com/forgot-password", "https://example.com/sessions/new",
           "https://example.com/%6Cogin",
           "https://example.com/reset-password/9f8e7d6c5b4a39281706f5e4d3c2b1a0",
           "https://www.google.com/search?q=api_key%3Dabcdefghijklmnop",
           "http://100.101.102.103:8080/admin",
           "https://www.sbicard.com/en/personal/account-summary.page",
           "https://www.sc.com/in/digital-banking/"]


def test_h1_none_of_the_reviewers_list_lands_on_a_public_page(tmp_path):
    """H1: sensitive or not, a search or page under no topic hub is private; the hard drops
    of the same list are gone entirely."""
    titles = {"https://www.sbicard.com/en/personal/account-summary.page":
              "Account Summary | SBI Card"}
    for u in DROPPED:
        assert kept(u, titles.get(u, "t"), ADULT_PRIVATE) is None, u
    items = [it(u, t, 9 + n % 10) for n, (u, t) in enumerate(PROBE)]
    items.append(it("https://www.google.com/search?q=python+decorators", "t", 20))
    _, _, pub, priv = place_and_write(tmp_path, items)
    assert "- [[python]] · 1 search\n" in pub  # a search under a hub counts, its term is private
    assert "python decorators" in priv and "decorators" not in pub
    public_only(pub, {"Python hub"})
    for u, t in PROBE:
        k = kept(u, t, ADULT_PRIVATE)
        for text in filter(None, (k.search, k.title if k.title != "t" else None)):
            assert text not in pub, text
    assert "naked women" in priv and "OnlyFans" not in pub
    for word in ("xvideos", "xhamster", "pornhub", "ycombinator", "amazon", "youtube"):
        assert word not in pub, word


class OtherRunner:
    """A local LLM that calls everything "other" and summarises with a fixed line."""
    def __init__(self):
        self.asked = []

    def describe(self):
        return "fake qwen3:4b at 127.0.0.1:11434"

    def classify(self, text, labels, *, timeout=20, budget=None):
        self.asked.append(labels)
        return ("other", 0.99)

    def summarise(self, lines, *, max_chars=200, timeout=20, budget=None):
        return "Mostly chess study."


def test_h2_the_local_llm_never_clears_a_category(vault, browsers):
    """H2: a runner answering "other" for a maybe leaves it private."""
    r = OtherRunner()
    run_import(vault, browsers, runner=r)
    day = (vault / "browse" / "browse-2026-11-09.md").read_text(encoding="utf-8")
    priv = (vault / "browse" / "private" / "browse-private-2026-11-09.md").read_text(
        encoding="utf-8")
    assert "HEALTHMARKER" not in day and "HEALTHMARKER" in priv
    assert privacy.judge("https://e.com/a", "Flu symptoms", settings=S).maybe


def test_m1_write_day_drops_a_secret_line_and_never_aborts(tmp_path, monkeypatch):
    """M1: a line that looks like a secret is left out; a page the vault refuses is skipped
    and reported, and the run still saves its state."""
    def raw(title, h):
        return build.Item("2026-11-10", datetime(2026, 11, 10, h, tzinfo=UTC), 300,
                          privacy.Kept(f"https://e.com/{h}", title, "e.com"))

    items = [raw("python password: hunter2222", 9), raw("python tips", 10),
             raw("python AKIAABCDEFGHIJKLMNOP", 11)]
    _, _, pub, priv = place_and_write(tmp_path, items)
    assert "python tips" in priv and "hunter2222" not in priv and "AKIA" not in priv
    assert "- [[python]] · 15 min · 3 pages" in pub
    assert not secret_pattern(pub) and not secret_pattern(priv)
    v = Vault(tmp_path)

    def refuse(path, text):
        raise build.VaultError("secret_detected", "no")
    monkeypatch.setattr(v, "write_text", refuse)
    where = build.place(items, FarEmbed(), HUB[0], HUB[1])
    res = build.write_day(v, "2026-11-11", items, where, HUB[2], tz=UTC, visible=True)
    assert res["public"] == "failed"


def test_m5_consumed_inbox_files_are_deleted_and_backups_skip_them(vault, browsers):
    d = vault / ".cortex" / "browse"
    d.mkdir(parents=True)
    row = {"url": "https://lichess.org/extonly", "title": "Chess from the extension",
           "start": m(200).isoformat(), "active_ms": 60_000, "excerpt": "", "kind": "page"}
    (d / "inbox-2026-11-09.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (d / "token").write_text("t" * 40, encoding="utf-8")
    run_import(vault, browsers)
    assert "Chess from the extension" in (
        vault / "browse" / "private" / "browse-private-2026-11-09.md").read_text(encoding="utf-8")
    assert not (d / "inbox-2026-11-09.jsonl").exists()
    assert json.loads((vault / build.STATE).read_text(encoding="utf-8"))["inbox"] == {}
    (d / "inbox-2026-11-01.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    run_import(vault, browsers, since="2026-11-09")
    # v7 C16: a file read whole leaves the inbox whatever its date (archived, or pruned when
    # it is older than the browsers' history; test_browse_v7)
    assert not (d / "inbox-2026-11-01.jsonl").exists()
    with zipfile.ZipFile(backup.backup(vault, vault.parent / "bk")) as z:
        names = z.namelist()
    assert "browse/browse-2026-11-09.md" in names
    assert not [n for n in names if n.startswith(".cortex/browse/")]


def test_l2_an_extension_record_merges_only_with_the_same_verdict(vault, browsers):
    """L2: same URL and minute, but the extension saw a private title: no merge."""
    d = vault / ".cortex" / "browse"
    d.mkdir(parents=True)
    row = {"url": "https://lichess.org/study/abc", "title": "HIV symptoms",
           "start": int(m(0).timestamp() * 1000) + 20_000, "active_ms": 3_000_000,
           "excerpt": "", "kind": "page"}
    (d / "inbox-2026-11-09.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    run_import(vault, browsers)
    day = (vault / "browse" / "browse-2026-11-09.md").read_text(encoding="utf-8")
    priv = (vault / "browse" / "private" / "browse-private-2026-11-09.md").read_text(
        encoding="utf-8")
    assert "- Chess openings for beginners · lichess.org · 20 min · 2 visits" in priv
    assert "HIV symptoms" in priv and "HIV" not in day
    assert "- [[chess]] · 1 h 10 min · 5 pages" in day  # the private record adds no public time


def test_l3_a_video_channel_is_judged(tmp_path):
    vid = "https://www.youtube.com/watch?v=abcdefghijk"
    assert kept(vid, "A vlog").private is False
    assert privacy.filter_record(vid, "A vlog", settings=S, channel="Herpes Support").private
    assert privacy.filter_record(vid, "A vlog", settings=S,
                                 channel="api_key=abcdefghijklmnop") is None


def test_l4_stale_temp_copies_are_swept_and_fresh_ones_kept(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    old, new = tmp_path / "cortex-browse-old", tmp_path / "cortex-browse-new"
    for d in (old, new):
        d.mkdir()
        (d / "History").write_bytes(b"x")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    sources.sweep()
    assert not old.exists() and new.exists()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_m3_m4_the_extension_mirror_drops_the_same_urls(tmp_path):
    """M3 in the extension's lib.js: the same decoded-word sign-in and bank rules."""
    lib = Path(__file__).parents[1] / "extension" / "lib.js"
    shutil.copy(lib, tmp_path / "lib.mjs")
    urls = DROPPED[:6] + ["https://www.sc.com/in/digital-banking/",
                          "http://100.101.102.103:8080/admin"]
    (tmp_path / "t.mjs").write_text(
        'import { denyReason, bankTitle } from "./lib.mjs";\n'
        f"const urls = {json.dumps(urls)};\n"
        'console.log(JSON.stringify([urls.map((u) => denyReason(u)), '
        'denyReason("https://example.com/blog/how-payments-work"), '
        'bankTitle("Account Summary | SBI Card")]));\n', encoding="utf-8")
    out = subprocess.run(["node", str(tmp_path / "t.mjs")], capture_output=True, text=True,
                         timeout=30)
    reasons, article, bank = json.loads(out.stdout)
    assert all(reasons) and article is None and bank is True


def test_adult_skip_writes_no_adult_item_anywhere(vault, browsers):
    run_import(vault, browsers, settings=Settings())
    assert b"ADULTMARKER" not in b"".join(files(vault).values())
    priv = (vault / "browse" / "private" / "browse-private-2026-11-09.md").read_text(
        encoding="utf-8")
    assert "HEALTHMARKER" in priv  # sensitive still goes private by default


def test_private_pages_stay_out_of_find_recall_and_dashboard(vault, browsers):
    run_import(vault, browsers)
    v = Vault(vault)
    idx = Index(v, Catalog.load(v))
    assert idx.find("ADULTMARKER") == [] and idx.find("HEALTHMARKER") == []
    # a site of no hidden category has a visible page; the day pages name no site
    found = {h["id"] for h in idx.find("lichess")}
    assert "browse-lichess.org" in found and not any("private" in i for i in found)
    assert not any(i.startswith("browse-2026") for i in found)
    hits = semantic.recall(idx, "ADULTMARKER HEALTHMARKER", Embed(), limit=20)
    assert all("private" not in h["id"] for h in hits)
    data = dashboard.build_data(v, now=m(0, 2))
    blob = json.dumps(data)
    assert "ADULTMARKER" not in blob and "HEALTHMARKER" not in blob
    pages = {p["id"]: p for p in data["pages"]}
    assert pages["browse-2026-11-09"]["cluster"] == "browsing"
    assert pages["browse-index"]["cluster"] == "browsing"
    shown = sum(not n.fields.get("private") for n in v.iter_notes("browse"))
    assert shown > 3 and {"id": "browsing", "title": "Browsing", "kind": "group",
                          "count": shown} in data["clusters"]
    assert not any(p.startswith("browse-private") for p in pages)


def test_extension_inbox_merges_by_url_and_minute(vault, browsers):
    d = vault / ".cortex" / "browse"
    d.mkdir(parents=True)
    rows = [{"url": "https://lichess.org/study/abc", "title": "Chess openings for beginners",
             "start": int(m(0).timestamp() * 1000) + 20_000, "active_ms": 180_000,
             "excerpt": "chess opening theory", "kind": "page"},
            {"url": "https://lichess.org/extonly", "title": "Chess from the extension",
             "start": m(200).isoformat().replace("+00:00", "Z"), "active_ms": 60_000,
             "excerpt": "", "kind": "page"},
            {"url": "https://www.youtube.com/watch?v=abcdefghijk", "title": "Chess endgame",
             "start": m(50).isoformat(), "active_ms": 120_000, "excerpt": "", "kind": "video",
             "channel": "Chess [Channel](x)"},
            "not a record", {"url": 3}]
    (d / "inbox-2026-11-09.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n{torn", encoding="utf-8")
    run_import(vault, browsers)
    day = (vault / "browse" / "private" / "browse-private-2026-11-09.md").read_text(
        encoding="utf-8")
    assert "- Chess openings for beginners · lichess.org · 13 min · 2 visits" in day
    assert "- Chess from the extension · lichess.org · 1 min" in day
    assert "- 09:50 · Chess endgame · Chess Channel · 2 min" in day  # link text only


class FakeRunner:
    kind = "ollama"  # a real Runner always has one; localllm.status reads it

    def __init__(self, summary="Mostly chess study."):
        self.summary = summary
        self.seen = []

    def describe(self):
        return "fake qwen3:4b at 127.0.0.1:11434"

    def summarise(self, lines, *, max_chars=200, timeout=20, budget=None):
        self.seen.append(list(lines))
        return self.summary


def test_local_llm_writes_the_summary_once(vault, browsers):
    r = FakeRunner()
    run_import(vault, browsers, runner=r)
    # second review: for a public page the runner hears the owner's hub names only, never a
    # title or a site; page titles go to it only for the private pages (see the test below)
    hub_calls = [s for s in r.seen if set(s) <= {"Chess hub", "Cooking hub"}]
    assert hub_calls == [["Chess hub", "Cooking hub"], ["Chess hub"]]
    day = (vault / "browse" / "browse-2026-11-09.md").read_text(encoding="utf-8")
    assert "What I was into: Mostly chess study." in day
    assert "HEALTHMARKER" not in day
    run_import(vault, browsers, runner=FakeRunner("Something else."), since="2026-11-09")
    assert day == (vault / "browse" / "browse-2026-11-09.md").read_text(encoding="utf-8")
    state = json.loads((vault / build.STATE).read_text(encoding="utf-8"))
    assert state["llm"] == "fake qwen3:4b at 127.0.0.1:11434"


def test_private_page_gets_title_summaries_per_day_and_session(vault, browsers):
    page = vault / "browse" / "private" / "browse-private-2026-11-09.md"
    run_import(vault, browsers, visible=False)
    plain_text = page.read_text(encoding="utf-8")
    assert "## Sessions" not in plain_text and "browse_llm" not in plain_text  # no runner
    r = FakeRunner("Chess <b>and</b> [guitar](https://x.com).")
    run_import(vault, browsers, runner=r, visible=False, since="2026-11-09")
    text = page.read_text(encoding="utf-8")
    assert r.seen and all(len(s) <= build.DAY_TITLES for s in r.seen)
    assert any("Chess openings for beginners" in s for s in r.seen)  # titles, never a URL
    assert not any("://" in t or "lichess.org" in t for s in r.seen for t in s)
    head = next(ln for ln in text.splitlines() if ln.startswith(build.SUMMARY_HEAD))
    assert "Chess" in head and "<b>" not in head and "](http" not in head
    # visits with gaps under 30 min are one session: 09:00-10:31, the pasta one, the guitar one
    sessions = text.split("## Sessions", 1)[1].split("## Topics", 1)[0]
    assert sessions.count("\n### ") == 3 and "### 09:00–" in sessions
    assert sessions.count("Chess and guitar.") == 3 and "<b>" not in sessions  # escaped
    # a rebuild reuses the lines on the page and asks nothing
    again = FakeRunner("Other.")
    run_import(vault, browsers, runner=again, visible=False, since="2026-11-09")
    assert page.read_text(encoding="utf-8") == text and again.seen == []
    assert not (vault / "browse" / "browse-2026-11-09.md").exists()


def test_private_summaries_respect_the_budget(vault, browsers):
    from cortex.localllm import Budget

    r = FakeRunner()
    run_import(vault, browsers, runner=r, visible=False, budget=Budget(0))
    assert r.seen == []
    text = (vault / "browse" / "private" / "browse-private-2026-11-09.md").read_text(
        encoding="utf-8")
    assert "## Sessions" not in text


def test_a_runner_found_later_replaces_the_rule_summary(vault, browsers):
    page = vault / "browse" / "browse-2026-11-10.md"
    run_import(vault, browsers)
    assert "Mostly chess study." not in page.read_text(encoding="utf-8")
    run_import(vault, browsers, runner=FakeRunner(), since="2026-11-09")
    assert "What I was into: Mostly chess study." in page.read_text(encoding="utf-8")


def test_import_opens_no_network_connection(vault, browsers, monkeypatch):
    def refuse(self, address):
        raise AssertionError(f"network use: {address}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: refuse(None, a))
    assert run_import(vault, browsers, runner=FakeRunner())["public_written"] == 2


# ---- second privacy review (17 Nov): no third-party text on public pages ------------------

@pytest.mark.parametrize("text,cat", [
    ("p0rn", "adult"), ("pr0n", "adult"), ("p o r n", "adult"), ("p.o.r.n", "adult"),
    ("p​orn", "adult"), ("p­orn", "adult"), ("pоrn", "adult"),  # Cyrillic o
    ("ｐｏｒｎ", "adult"), ("pornography", "adult"), ("porns", "adult"),
    ("masturbation", "adult"), ("nofap", "adult"), ("only fans", "adult"),
    ("S3x drive", "adult"), ("सेक्स video", "adult"),
    ("sexual health clinic", "health"), ("STD test", "health"), ("chlamydia", "health"),
    ("condom", "health"), ("pregnant", "health"), ("anxiety", "health"), ("ADHD", "health"),
    ("OCD", "health"), ("bipolar", "health"), ("cancer", "health"),
    ("Anxiety and attention span in students", "health"),
    ("self harm", "selfharm"), ("self-harm", "selfharm"), ("kill myself", "selfharm"),
    ("suicidal thoughts", "selfharm"), ("weed", "drugs"), ("cocaine", "drugs"),
    ("satta matka", "betting"), ("Grindr", "dating"), ("g@y", "sexuality"),
    ("sexuality", "sexuality"),
])
def test_r2_category_words_are_normalised_and_stemmed(text, cat):
    assert privacy.category("example.org", "/x", text)[0] == cat


@pytest.mark.parametrize("title", ["C++ std::vector reference", "Kernel panic on boot",
                                   "Process scheduling algorithms round robin",
                                   "Web3 and 5G explained"])
def test_r2_normalising_leaves_student_pages_alone(title):
    assert privacy.category("example.org", "/x", title) == (None, False)


def test_r2_drugs_and_selfharm_follow_browse_sensitive():
    for title in ("How to quit weed", "I want to kill myself"):
        assert kept("https://e.com/a", title).private
        assert kept("https://e.com/a", title, Settings(sensitive="skip")) is None


class HubEmbed:
    """The worst case for the hub test: every text sits right on the hub."""
    def __call__(self, texts):
        out = np.zeros((len(texts), 3), np.float32)
        out[:, 0] = 1
        return out


R2_TITLES = [  # the second reviewer's p1/p2 probes: sensitive by meaning, hub-like by words
    "Panic attacks before every exam, how do I cope? : r/GetStudying",
    "Got diagnosed with ADHD at 21, my exam scores make sense now : r/ADHD",
    "How long after unprotected sex can you test for chlamydia",
    "Is it normal to feel anxious and lonely in hostel", "Ozempic weight loss before and after",
    "Steroid cycle for beginners: testosterone dosage", "How to quit weed and vaping for good",
    "Coming out to my parents as bi", "Signs your boyfriend is cheating",
    "How to get a fake medical certificate for college attendance",
    "I failed my semester and my parents don't know", "Antidepressants and gym gains",
    "Sexual health clinic near me", "Erectile dysfunction at 22 causes",
    "Body dysmorphia gym bro reddit", "Interview anxiety medicine propranolol",
    "Layoffs at my company, resume tips", "p0rn addiction and motivation to study",
    "How to stop nofap relapse and focus on placements",
    "Anxiety and attention span in students", "Habit formation to quit masturbation",
    "Testosterone levels and motivation", "Creatine vs anabolic steroids for muscle gain",
    "Masturbation and memory: cognitive effects", "Cognitive biases in people with OCD",
    "Memory loss after alcohol blackout", "ADHD medication Adderall and focus",
    "Pr0n habit and dopamine motivation", "S3x drive and protein intake",
    "Resume gap due to mental illness interview",
    "Memory and forgetting curve after a concussion",
]


def test_r2_no_probe_title_reaches_a_public_page_even_under_a_hub(tmp_path, monkeypatch):
    """H1/H2 moot: even when every title sits on a hub, the public page holds the hub, its
    time and counts, and the sensitive items add nothing to them."""
    monkeypatch.setattr(build, "SHOWN", 100)
    items = [it(f"https://example.org/{n}", t, 9, secs=60) for n, t in enumerate(R2_TITLES)]
    v = Vault(tmp_path)
    where = build.place(items, HubEmbed(), HUB[0], HUB[1])
    build.write_day(v, "2026-11-10", items, where, HUB[2], tz=UTC, visible=True)
    pub = (tmp_path / "browse" / "browse-2026-11-10.md").read_text(encoding="utf-8")
    priv = (tmp_path / "browse" / "private" / "browse-private-2026-11-10.md").read_text(
        encoding="utf-8")
    public_only(pub, {"Python hub"})
    plain = [i for i in items if not i.k.private]
    assert f"- [[python]] · {len(plain)} min · {len(plain)} pages\n" in pub
    for i in items:
        assert i.k.title in priv
        for w in re.findall(r"[A-Za-z]{4,}", i.k.title):
            assert w.lower() not in pub.lower() or w.lower() in ("python", "pages", "other"), w
    sensitive = {i.k.title for i in items if i.k.private}
    assert "Anxiety and attention span in students" in sensitive
    assert "Cognitive biases in people with OCD" in sensitive


def test_r2_a_same_url_visit_never_shows_an_unchecked_title(tmp_path, vault):
    """p3: one scrubbed URL, two titles; the public page shows neither."""
    local, roaming = tmp_path / "local", tmp_path / "roaming"
    chromium(local / "Google" / "Chrome" / "User Data" / "Default" / "History", [
        (m(0), "https://www.reddit.com/search/?q=chess+openings", "chess openings - Reddit"),
        (m(5), "https://www.reddit.com/search/?q=chlamydia+symptoms",
         "chlamydia after a one night stand - Reddit Search"),
        (m(10), "https://www.youtube.com/feed/subscriptions", "chess - YouTube"),
        (m(15), "https://www.youtube.com/feed/subscriptions", "(1) my ex is stalking me - YouTube"),
    ])
    run_import(vault, (local, roaming))
    for p in (vault / "browse").glob("browse-2*.md"):  # the day pages: no third-party text
        text = p.read_text(encoding="utf-8")
        for w in ("reddit", "youtube", "chlamydia", "stalking", "openings", "subscriptions"):
            assert w not in text.lower(), (p.name, w)
    for p in visible_files(vault):  # the site pages show what is not hidden, never health
        assert b"chlamydia" not in p.read_bytes().lower(), p
    priv = (vault / "browse" / "private" / "browse-private-2026-11-09.md").read_text(
        encoding="utf-8")
    assert "my ex is stalking me" in priv


def test_r2_m1_a_since_rebuild_removes_pages_whose_items_are_now_dropped(tmp_path, vault):
    """M1: after a new deny_domains entry, `--since` rewrites or removes the day's pages."""
    local, roaming = tmp_path / "local", tmp_path / "roaming"
    chromium(local / "Google" / "Chrome" / "User Data" / "Default" / "History",
             [(m(0), "https://chess-forum.example/t/1", "chess openings forum"),
              (m(0, 1), "https://lichess.org/a", "chess puzzles")])
    pub = vault / "browse" / "browse-2026-11-09.md"
    priv = vault / "browse" / "private" / "browse-private-2026-11-09.md"

    def go(**s):
        return run_import(vault, (local, roaming), settings=Settings(adult="private", **s),
                          since="2026-11-09")

    go()
    assert pub.is_file() and priv.is_file()
    go(private=("chess-forum.example",))
    assert not pub.is_file() and "chess openings forum" in priv.read_text(encoding="utf-8")
    rep = go(deny=("chess-forum.example",))
    assert not pub.is_file() and not priv.is_file() and rep["private_removed"] == 1
    assert (vault / "browse" / "browse-2026-11-10.md").is_file()  # other days stay
    idx = (vault / "browse" / "browse-index.md").read_text(encoding="utf-8")
    assert "browse-2026-11-09" not in idx


def test_r2_m1_a_day_the_browser_forgot_keeps_its_page(tmp_path, vault):
    local, roaming = tmp_path / "local", tmp_path / "roaming"
    hist = local / "Google" / "Chrome" / "User Data" / "Default" / "History"
    chromium(hist, [(m(0), "https://lichess.org/a", "chess openings"),
                    (m(0, 1), "https://lichess.org/b", "chess puzzles")])
    run_import(vault, (local, roaming))
    hist.unlink()
    chromium(hist, [(m(0, 1), "https://lichess.org/b", "chess puzzles")])  # day 1 expired
    run_import(vault, (local, roaming), since="2026-11-09")
    assert (vault / "browse" / "browse-2026-11-09.md").is_file()


def test_r2_l1_rows_with_bad_times_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    for name, raw in [("huge", 2**62), ("text", "abc"), ("neg", -2**62), ("future",
                      chrome_raw(datetime(2999, 1, 1, tzinfo=UTC)))]:
        p = tmp_path / name / "History"
        chromium(p, [(m(0), "https://a.org/", "A")])
        con = sqlite3.connect(p)
        con.execute("INSERT INTO urls(url, title) VALUES ('https://b.org/', 'B')")
        con.execute("INSERT INTO visits(url, visit_time, visit_duration, transition) "
                    "VALUES (2, ?, 'x', 0)", (raw,))
        con.commit()
        con.close()
        vs, top = sources.read(sources.Source("chrome", name, p))
        assert [v.title for v in vs] == ["A"] and top == chrome_raw(m(0)), name
    assert list((tmp_path / "tmp").iterdir()) == []


# ---- the CLI ------------------------------------------------------------------------------

def cli(*args):
    return CliRunner().invoke(app, list(args))


@pytest.fixture
def machine(browsers, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(browsers[0]))
    monkeypatch.setenv("APPDATA", str(browsers[1]))
    return browsers


def test_cli_scan_prints_counts_and_no_text(machine):
    out = cli("browse", "scan")
    assert out.exit_code == 0, out.output
    assert "settings: not saved adult; hidden from Claude adult, health" in out.output
    assert "chrome/Default: 16 visits, 1 searches" in out.output
    for bad in ("MARKER", "lichess", "http", "Chess"):
        assert bad not in out.output
    assert cli("browse", "scan", "--browser", "opera").exit_code == 1


def test_cli_import_status_and_llm_status(vault, machine, monkeypatch):
    from cortex import localllm

    def no_probe(config=None):
        raise AssertionError("import probed a runner without localllm.enabled")
    monkeypatch.setattr(localllm, "find", no_probe)  # H3: not enabled, so never asked
    out = cli("browse", "import", "--vault", str(vault))
    assert out.exit_code == 0, out.output
    assert "days written: 2 private pages" in out.output and "MARKER" not in out.output
    assert "visible to Claude: no — only private pages written" in out.output
    # no embedder, so no hub: everything is private (H1)
    assert (vault / "browse" / "private" / "browse-private-2026-11-09.md").is_file()
    assert "cortex: cli browse import" in git(vault, "log", "-1", "--format=%s")
    st = cli("browse", "status", "--vault", str(vault)).output
    assert "chrome/Default: up to 2026-11-1" in st and "local LLM: none" in st
    assert cli("browse", "import", "--since", "11/10", "--vault", str(vault)).exit_code == 1
    monkeypatch.setattr(localllm, "find", lambda config=None, **_: None)
    out = cli("llm", "status")
    assert "ollama pull qwen3.5:4b" in out.output
    monkeypatch.setattr(localllm, "find", lambda config=None, **_: FakeRunner())
    monkeypatch.setattr(FakeRunner, "ping", lambda *a, **k: 0.25, raising=False)
    out = cli("llm", "status").output
    assert "found: fake qwen3:4b at 127.0.0.1:11434" in out
    assert "latency (1 token): 0.25 s" in out
    assert "localllm: {enabled: true}" in out


def test_h3_cli_import_uses_the_runner_only_when_enabled(vault, machine, monkeypatch, home):
    from cortex import localllm

    (home / ".cortex").mkdir(exist_ok=True)
    (home / ".cortex" / "config.yaml").write_text("localllm:\n  enabled: true\n",
                                                   encoding="utf-8")
    used = []
    monkeypatch.setattr(localllm, "find", lambda config=None, **_: used.append(1) or FakeRunner())
    assert cli("browse", "import", "--vault", str(vault)).exit_code == 0
    assert used == [1]
    out = cli("llm", "status").output
    assert "enabled: true" not in out


def test_cli_pair_prints_a_token(vault):
    pytest.importorskip("cortex.browse_serve")
    out = cli("browse", "pair", "--vault", str(vault))
    assert out.exit_code == 0 and len(out.output.strip()) >= 20


# ---- self-checks and a sturdier import (owner can't run it blind) -------------------------

def test_selftest_passes_on_synthetic_data_only(machine, tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    before = {p: _digest(p) for p in tmp_path.rglob("*") if p.is_file()}
    out = cli("browse", "selftest")
    assert out.exit_code == 0, out.output
    assert out.output.count("PASS  ") == 11 and "FAIL" not in out.output
    assert "PASS: all 11 checks passed" in out.output
    # the fixture's "real" browsers were never read or changed, and the temp folder is gone
    assert {p: _digest(p) for p in tmp_path.rglob("*") if p.is_file()} == before
    assert list((tmp_path / "tmp").iterdir()) == []


def test_selftest_fails_loudly(monkeypatch):
    def broken(src):
        raise sqlite3.DatabaseError("x")
    monkeypatch.setattr(sources, "read", broken)
    out = cli("browse", "selftest")
    assert out.exit_code == 1
    assert "FAIL  scan reads every source" in out.output and "checks failed" in out.output


def test_cli_scan_says_comet_range_and_next_step(machine):
    out = cli("browse", "scan").output
    assert "Comet found: no" in out and "date range: 2026-11-09 to 2026-11-10" in out
    assert "next: `cortex browse selftest`" in out
    ud = machine[0] / "Perplexity" / "Comet" / "User Data" / "Default" / "History"
    chromium(ud, [(m(0), "https://chess.com/learn", "Chess lessons")])
    assert "Comet found: yes" in cli("browse", "scan").output


def test_cli_scan_and_import_with_no_browsers(vault, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "none"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "none"))
    assert "no browser history found" in cli("browse", "scan").output
    out = cli("browse", "import", "--vault", str(vault))
    assert out.exit_code == 0 and "no browser history found" in out.output


def test_cli_import_without_a_vault():
    out = cli("browse", "import")
    assert out.exit_code == 1 and "No vault set" in out.output


def test_cli_import_progress_summary_and_model_hint(vault, machine):
    out = cli("browse", "import", "--vault", str(vault))
    assert out.exit_code == 0, out.output
    assert "chrome/Default: 16 visits read, 16 new" in out.output
    assert "firefox/ab12.default-release: 1 visits read, 1 new" in out.output
    assert "embedding model not downloaded" in out.output
    assert "To fix: cortex model download" in out.output
    assert "dropped by the privacy filter 3" in out.output  # adult: skip by default
    assert "local model used: no; time taken" in out.output
    for bad in ("MARKER", "lichess", "http", "Chess"):
        assert bad not in out.output


def test_cli_import_dry_run_writes_nothing(vault, machine):
    before = files(vault)
    out = cli("browse", "import", "--dry-run", "--vault", str(vault))
    assert out.exit_code == 0, out.output
    assert "dry run, nothing written: would write 2 private and 0 public day pages" in out.output
    assert files(vault) == before
    assert not (vault / build.STATE).exists()


def test_a_locked_source_is_reported_skipped_and_redone_next_time(vault, machine, monkeypatch,
                                                                  tmp_path):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    real = shutil.copyfile

    def locked(src, dst, *a, **k):
        if "Edge" in str(src):
            raise PermissionError(13, "in use")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(sources.shutil, "copyfile", locked)
    out = cli("browse", "import", "--vault", str(vault))
    assert out.exit_code == 0, out.output
    assert "edge/Default: not read: could not copy its history file" in out.output
    assert "chrome/Default: 16 visits read" in out.output and "1 source(s) not read" in out.output
    state = build.load_state(Vault(vault))
    assert "edge/Default" not in state["sources"] and "chrome/Default" in state["sources"]
    assert state["pending"] == "2026-11-09"
    assert list((tmp_path / "tmp").iterdir()) == []
    assert "rebuilds from 2026-11-09" in cli("browse", "status", "--vault", str(vault)).output
    monkeypatch.setattr(sources.shutil, "copyfile", real)  # the browser closed
    rep = build.run(Vault(vault), sources.discover(*machine), S, tz=UTC)
    assert rep["days_from"] == "2026-11-09" and build.load_state(Vault(vault))["pending"] is None
    assert "edge/Default" in build.load_state(Vault(vault))["sources"]


def test_any_error_in_one_source_never_stops_the_others(vault, browsers):
    real = sources.read

    def odd(src):
        if src.browser == "edge":
            raise ValueError("odd file")
        return real(src)
    seen = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sources, "read", odd)
        rep = run_import(vault, browsers, progress=lambda *a: seen.append(a))
    assert rep["sources"] == 3 and rep["sources_failed"] == 1
    assert [a[0] for a in seen if a[3]] == ["edge/Default"]


def test_ctrl_c_leaves_whole_pages_no_state_and_no_temp_copy(vault, machine, monkeypatch,
                                                             tmp_path):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    real, calls = build.write_day, []

    def interrupted(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(*a, **k)
    monkeypatch.setattr(build, "write_day", interrupted)
    out = cli("browse", "import", "--vault", str(vault))
    assert out.exit_code == 130 and "run the import again" in out.output
    assert not (vault / build.STATE).exists()
    assert list((tmp_path / "tmp").iterdir()) == []
    assert not [p for p in vault.rglob("*.tmp")]
    page = vault / "browse" / "private" / "browse-private-2026-11-09.md"
    assert page.read_text(encoding="utf-8").startswith("---")


def test_cli_status_shows_mode_and_last_read(vault, machine):
    assert "visible to Claude: no" in cli("browse", "status", "--vault", str(vault)).output
    cli("browse", "import", "--vault", str(vault))
    st = cli("browse", "status", "--vault", str(vault)).output
    assert re.search(r"chrome/Default: up to 2026-11-1\d \d\d:\d\d, last read 20\d\d-", st)


def test_q12_switching_visibility_off_voids_the_journal_lines_and_touches(vault, browsers):
    """Q12: once browsing is hidden from Claude, its past journal lines and touches go too."""
    assert run_import(vault, browsers)["signals"] == 1
    rep = run_import(vault, browsers, visible=False)
    assert rep["browse_voided"] == 1
    live_touch = [e for e in events.live(EventLog(Vault(vault)).iter_events())
                  if (e.get("key") or "").startswith("browse:touch:")]
    assert live_touch == []
    journal = (vault / "journal" / "2026-11-10.md").read_text(encoding="utf-8")
    assert "Browsing: spent" not in journal and "## Log" in journal
    assert run_import(vault, browsers, visible=False).get("browse_voided", 0) == 0


# ---- Google Takeout activity (cortex browse import --takeout) -------------------------------

def takeout_dir(tmp_path, rows):
    """A Takeout folder: My Activity/<product>/MyActivity.json, one per product."""
    by: dict[str, list] = {}
    for when, product, title, url in rows:
        by.setdefault(product, []).append({"header": product, "title": title, "titleUrl": url,
                                           "time": when.isoformat(), "products": [product]})
    root = tmp_path / "Takeout"
    for product, items in by.items():
        p = root / "My Activity" / product / "MyActivity.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(items), encoding="utf-8")
    return root


TAKEOUT_ROWS = [
    (m(0, -5), "Search", "Searched for chess openings",
     "https://www.google.com/search?q=chess+openings"),
    (m(5, -5), "YouTube", "Watched Chess endgame", "https://www.youtube.com/watch?v=abcdefghijk"),
    (m(10, -5), "Chrome", "Visited Sign in SIGNINTAKEOUT", "https://accounts.google.com/signin"),
    (m(15, -5), "Search", "Searched for porn adulttakeout",
     "https://www.google.com/search?q=porn+adulttakeout"),
    (m(20, -5), "Chrome", "Visited ADULTTAKEOUT clip",
     "https://www.pornhub.com/view_video.php?viewkey=zz"),
]


def test_takeout_rows_go_through_the_privacy_filter(vault, tmp_path):
    from cortex.browse import takeout

    rows = takeout.records(takeout_dir(tmp_path, TAKEOUT_ROWS))
    s = Settings(adult="private", hidden_from_claude=("adult",), visible_to_claude=True)
    r = build.run(Vault(vault), [], s, embed=Embed(), tz=UTC, now=m(0, 2), takeout=rows)
    assert r["takeout"] == 5 and r["dropped"] == 1  # the sign-in page
    for p in visible_files(vault):
        low = p.read_text(encoding="utf-8").lower()
        assert "adulttakeout" not in low and "signintakeout" not in low, p
    hid = [p.read_text(encoding="utf-8").lower()
           for p in (vault / "browse" / "private").rglob("*.md")]
    assert any("adulttakeout" in t for t in hid)  # kept, but only on the hidden side
    assert not any("signintakeout" in t for t in hid)
    # by default adult is never saved at all
    r2 = build.run(Vault(vault), [], replace(s, adult="skip", skip_categories=None,
                                             hidden_from_claude=()),
                   embed=Embed(), tz=UTC, now=m(0, 2), takeout=rows)
    assert r2["dropped"] == 3


def test_cli_import_takeout_keeps_only_what_the_browsers_do_not_hold(vault, machine,
                                                                      tmp_path):
    folder = takeout_dir(tmp_path, [
        (m(0, -5), "Search", "Searched for chess openings",
         "https://www.google.com/search?q=chess+openings"),
        (m(45), "Search", "Searched for chess openings",  # the browser holds this one
         "https://www.google.com/search?q=chess+openings")])
    out = cli("browse", "import", "--takeout", str(folder), "--vault", str(vault))
    assert out.exit_code == 0, out.output
    assert "takeout: 2 activity rows read" in out.output
    assert "takeout: 1 rows used from days your browsers do not hold" in out.output
    assert "visits from 2026-11-02" in out.output  # the Monday before the takeout day
    bad = cli("browse", "import", "--takeout", str(tmp_path / "nothing"), "--vault", str(vault))
    assert bad.exit_code == 1


# ---- board 2 review fixes (docs/design/v6/board2) -------------------------------------------

def test_r2_8_takeout_is_cut_only_on_days_a_browser_holds(vault, tmp_path):
    from cortex.browse import takeout

    local = tmp_path / "local"
    chromium(local / "Google" / "Chrome" / "User Data" / "Default" / "History",
             [(m(0), "https://lichess.org/study/abc", "Chess openings")])
    # a second browser that still holds one old visit
    chromium(local / "Microsoft" / "Edge" / "User Data" / "Default" / "History",
             [(m(0, -30), "https://lichess.org/learn", "Chess basics")])
    rows = takeout.records(takeout_dir(tmp_path, [
        (m(0, -10), "Search", "Searched for chess gapday",
         "https://www.google.com/search?q=chess+gapday"),
        (m(5), "Search", "Searched for chess heldday",
         "https://www.google.com/search?q=chess+heldday")]))
    srcs = sources.discover(local, None)
    s = Settings(visible_to_claude=True)
    r = build.run(Vault(vault), srcs, s, embed=Embed(), tz=UTC, now=m(0, 2), takeout=rows)
    assert r["takeout"] == 1  # the day Chrome holds is cut, the day no browser holds is not
    gap = vault / "browse" / "private" / f"browse-private-{m(0, -10).date()}.md"
    assert "gapday" in gap.read_text(encoding="utf-8")
    assert not any("heldday" in p.read_text(encoding="utf-8")
                   for p in (vault / "browse").rglob("*.md"))
    # a later run without it, with new rules (every held day redone), keeps the Takeout-only day
    build.run(Vault(vault), srcs, replace(s, deny=("example.com",)), embed=Embed(), tz=UTC,
              now=m(0, 3))
    assert "gapday" in gap.read_text(encoding="utf-8")
    from cortex.browse import sites
    assert str(m(0, -10).date()) in json.loads((vault / sites.DAYS).read_text(encoding="utf-8"))


def test_s2_a_takeout_import_reads_only_the_chosen_browsers(vault, browsers, tmp_path,
                                                          monkeypatch):
    """S2: without an explicit browser, the CLI and the app read only `browse.browsers`.
    G7: an explicit --browser wins, with --takeout too: build.run reads what it is given."""
    from types import SimpleNamespace

    from cortex import cli
    from cortex.app import actions
    from cortex.browse import takeout

    tk = takeout_dir(tmp_path, TAKEOUT_ROWS[:1])
    rows = takeout.records(tk)
    srcs = sources.discover(*browsers)
    assert {s.browser for s in srcs} >= {"chrome", "edge", "firefox"}
    monkeypatch.setattr(sources, "discover", lambda *a, **k: srcs)
    monkeypatch.setattr("cortex.taint.config", lambda: {"browse": {"browsers": ["firefox"]}})
    assert {s.browser for s in cli._browse_sources(None, None)} == {"firefox"}
    assert {s.browser for s in cli._browse_sources("edge", None)} == {"edge"}
    got = []
    monkeypatch.setattr(actions, "history_import",
                        lambda job, root, keys, takeout=None: got.append(keys) or {})
    actions.import_run(SimpleNamespace(say=lambda m: None), "google-activity", tk, vault)
    assert got == [{"firefox/ab12.default-release"}]
    s = Settings(browsers=("firefox",))
    edge = [x for x in srcs if x.browser == "edge"]
    r = build.run(Vault(vault), edge, s, embed=Embed(), tz=UTC, now=m(0, 2), takeout=rows)
    state = json.loads((vault / build.STATE).read_text(encoding="utf-8"))
    assert r["sources"] == 1 and list(state["sources"]) == ["edge/Default"]


def test_an_explicit_profile_is_read_even_when_unticked(tmp_path, monkeypatch):
    """`cortex browse import --profile X` is the user's choice: X is read even when unticked
    (browse.skip_profiles), but only from the browsers `browse.browsers` allows, unless
    --browser names one."""
    from cortex import cli

    found = [sources.Source("chrome", p, tmp_path / p) for p in ("Default", "Work")]
    monkeypatch.setattr(sources, "discover", lambda *a, **k: found)
    cfg = {"browse": {"browsers": ["chrome"], "skip_profiles": ["chrome/Work"]}}
    monkeypatch.setattr("cortex.taint.config", lambda: cfg)
    assert [s.key for s in cli._browse_sources(None, None)] == ["chrome/Default"]
    assert [s.key for s in cli._browse_sources(None, "work")] == ["chrome/Work"]
    cfg["browse"]["browsers"] = ["firefox"]
    assert cli._browse_sources(None, "work") == []  # browse.browsers still holds
    assert [s.key for s in cli._browse_sources("chrome", "work")] == ["chrome/Work"]


@pytest.mark.parametrize("url,host", [("https://bücher.de/katalog", "xn--bcher-kva.de"),
                                      ("https://МОСКВА.рф/news", "xn--80adxhks.xn--p1ai")])
def test_idn_hosts_are_kept_as_punycode_and_still_filtered(url, host):
    assert privacy.drop_reason(url) is None
    k = kept(url)
    assert k is not None and k.host == host and k.url.startswith(f"https://{host}/")
    assert kept(url, settings=Settings(deny=(host,))) is None
    # the category rules still read the host in its own script
    for adult in ("https://xvideosü.com/x", "https://सेक्स.com/x"):
        assert privacy.judge(adult, "t", settings=S).category == "adult"
    assert privacy.drop_reason("https://" + "ü" * 70 + ".de/") == "scheme"  # not encodable
