"""Google Takeout My Activity (Search, YouTube, Chrome) as browse visit records."""

import json
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cortex.browse import privacy, sources, takeout
from cortex.vault import VaultError

EXPORTS = Path(__file__).parent / "fixtures" / "exports"


def test_records_from_my_activity_json():
    rs = takeout.records(EXPORTS / "takeout_MyActivity.json")
    assert [(r.source, r.url, r.title, r.channel) for r in rs] == [
        ("takeout/search", "https://www.google.com/search?q=dijkstra+algorithm",
         "dijkstra algorithm", ""),
        ("takeout/chrome", "https://en.wikipedia.org/wiki/Shortest_path_problem",
         "Shortest path problem", ""),
        ("takeout/youtube", "https://www.youtube.com/watch?v=abcdefghijk",
         "Dijkstra in 10 minutes", "Graph Channel")]  # Maps, no-URL and bad-time rows skipped
    assert all(isinstance(r, sources.Visit) for r in rs)
    assert rs[0].start == datetime(2026, 10, 2, 9, 30, tzinfo=UTC)
    assert all(0 < r.secs <= sources.GAP_CAP for r in rs)
    kept = [privacy.filter_record(r.url, r.title, settings=privacy.Settings(),
                                  channel=r.channel) for r in rs]
    assert all(k is not None for k in kept)  # the browse filter takes them as they are


def test_records_from_a_takeout_zip_skip_gemini(tmp_path):
    z = tmp_path / "takeout.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.write(EXPORTS / "takeout_MyActivity.json", "Takeout/My Activity/Search/MyActivity.json")
        f.write(EXPORTS / "gemini_MyActivity.json",
                "Takeout/My Activity/Gemini Apps/MyActivity.json")
    assert [r.source for r in takeout.records(z)] == [
        "takeout/search", "takeout/chrome", "takeout/youtube"]


@pytest.mark.parametrize("name, body, expect", [
    ("MyActivity.html", "<html></html>", "JSON"),
    ("MyActivity.json", "{broken", "Cannot read"),
    ("MyActivity.json", '{"a": 1}', "list"),
])
def test_bad_input_names_what_was_expected(tmp_path, name, body, expect):
    f = tmp_path / name
    f.write_text(body, "utf-8")
    with pytest.raises(VaultError) as e:
        takeout.records(f)
    assert expect in str(e.value)


def test_zip_without_my_activity(tmp_path):
    z = tmp_path / "t.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Takeout/archive_browser.html", "<html></html>")
    with pytest.raises(VaultError) as e:
        takeout.records(z)
    assert "MyActivity.json" in str(e.value)


HTML = EXPORTS / "takeout_html"  # synthetic, in the real HTML structure
ACT = HTML / "Takeout" / "My Activity"


def _key(rs):
    return [(r.source, r.url, r.title, r.channel, r.start, r.secs) for r in rs]


def test_html_takeout_gives_the_same_records_as_json():
    want = _key(takeout.records(EXPORTS / "takeout_MyActivity.json"))
    assert _key(takeout.records(HTML)) == want  # the folder; Gemini Apps is left out
    one = takeout.records(ACT / "YouTube" / "MyActivity.html")  # one file on its own
    assert [(r.url, r.title, r.channel) for r in one] == [
        ("https://www.youtube.com/watch?v=abcdefghijk", "Dijkstra in 10 minutes",
         "Graph Channel")]


def test_html_row_with_an_unreadable_time_is_skipped_and_counted():
    rows, skipped = takeout.html_rows((ACT / "Search" / "MyActivity.html").read_bytes())
    assert skipped == 1  # "... PM XYZ": an unknown zone
    assert [r["title"] for r in rows] == ["Searched for dijkstra algorithm", "Used Search"]
    assert rows[0]["time"] == "2026-10-02T09:30:00Z" and rows[0]["products"] == ["Search"]


@pytest.mark.parametrize("text, want", [
    ("Nov 17, 2026, 10:42:03 PM IST", datetime(2026, 11, 17, 17, 12, 3, tzinfo=UTC)),
    ("Nov 7, 2026, 9:02:03 AM PST", datetime(2026, 11, 7, 17, 2, 3, tzinfo=UTC)),
    ("17 Nov 2026, 22:42:03 GMT+05:30", datetime(2026, 11, 17, 17, 12, 3, tzinfo=UTC)),
    ("November 17, 2026, 10:42:03 PM UTC", datetime(2026, 11, 17, 22, 42, 3, tzinfo=UTC)),
    ("Nov 17, 2026, 10:42:03 PM XYZ", None),
    ("17.11.2026, 22:42:03 MEZ", datetime(2026, 11, 17, 21, 42, 3, tzinfo=UTC)),  # de
    ("17 nov 2026, 10:42:03 p. m. CET", datetime(2026, 11, 17, 21, 42, 3, tzinfo=UTC)),  # es
    ("17 de nov. de 2026, 19:42:03 BRT", datetime(2026, 11, 17, 22, 42, 3, tzinfo=UTC)),  # pt
    ("17 nov. 2026, 22:42:03 UTC+1", datetime(2026, 11, 17, 21, 42, 3, tzinfo=UTC)),  # fr
    ("17 नव॰ 2026, 10:42:03 pm IST", datetime(2026, 11, 17, 17, 12, 3, tzinfo=UTC)),  # hi
    ("17 Nov 2026, 22.42.03 WIB", datetime(2026, 11, 17, 15, 42, 3, tzinfo=UTC)),  # id
    ("2026/11/17 22:42:03 JST", datetime(2026, 11, 17, 13, 42, 3, tzinfo=UTC)),  # ja
    ("17 févr. 2026, 08:00:00 UTC", datetime(2026, 2, 17, 8, 0, 0, tzinfo=UTC)),
    ("17 нояб. 2026 г., 22:42:03 MSK", None),  # Russian: not supported, so skipped
])
def test_html_times(text, want):
    assert takeout.when(text) == want


def test_records_count_html_rows_skipped_for_an_unreadable_time():
    assert takeout.records(ACT / "Search" / "MyActivity.html").skipped == 1
    assert takeout.records(HTML).skipped == 1  # the folder: only Search has one
    assert takeout.records(EXPORTS / "takeout_MyActivity.json").skipped == 0


# Synthetic translations of the English fixtures: (My Activity folder, file stem, Search's
# product name, "Products:" label, (English, local) text swaps, the HTML date format)
_YT = '<a href="https://www.youtube.com/watch?v=abcdefghijk">Dijkstra in 10 minutes</a>'
LANGS = {
    "es": ("Mi actividad", "MiActividad", "Búsqueda", "Productos:",
           [("Searched for", "Has buscado"), ("Watched", "Has visto"),
            ("Visited", "Has visitado")], "2 oct 2026, {H}:{M}:{S} {tz}"),
    "de": ("Meine Aktivitäten", "MeineAktivitäten", "Suche", "Produkte:",
           [("Searched for", "Gesucht nach:"), (f"Watched&nbsp;{_YT}", f"{_YT} angesehen"),
            ('"Watched Dijkstra in 10 minutes"', '"Dijkstra in 10 minutes angesehen"'),
            ("Visited", "Aufgerufen:")], "02.10.2026, {H}:{M}:{S} {tz}"),
    "hi": ("मेरी गतिविधि", "मेरी गतिविधि", "खोज", "उत्पाद:",
           [("Searched for", "खोजा गया:"), ("Watched", "देखा गया:"),
            ("Visited", "विज़िट किया गया:")], "2 अक्तू॰ 2026, {h}:{M}:{S} {ap} {tz}"),
    "pt": ("Minha atividade", "MinhaAtividade", "Pesquisa", "Produtos:",
           [("Searched for", "Pesquisou"), ("Watched", "Assistiu a"), ("Visited", "Visitou")],
           "2 de out. de 2026, {H}:{M}:{S} {tz}"),
    "ja": ("マイ アクティビティ", "マイアクティビティ", "検索", "サービス:",
           [("Searched for&nbsp;<a href=\"https://www.google.com/search?q=dijkstra+algorithm\">"
             "dijkstra algorithm</a>", "<a href=\"https://www.google.com/search?q=dijkstra+"
             "algorithm\">「dijkstra algorithm」</a>を検索しました"),
            ('"Searched for dijkstra algorithm"', '"「dijkstra algorithm」を検索しました"'),
            (f"Watched&nbsp;{_YT}", f"{_YT} を視聴しました"),
            ('"Watched Dijkstra in 10 minutes"', '"Dijkstra in 10 minutes を視聴しました"'),
            ("Visited&nbsp;", ""), ("Shortest path problem</a>",
                                    "Shortest path problem</a> にアクセスしました"),
            ('"Visited Shortest path problem"', '"Shortest path problem にアクセスしました"')],
           "2026/10/02 {H}:{M}:{S} {tz}"),
}
_EN_TIME = re.compile(r"(?:Oct 2, 2026|2 Oct 2026), (\d+):(\d\d):(\d\d)(?: | )?([AP]M)? (\S+)")


def _translate(text: str, lang: str) -> str:
    _, _, search, label, swaps, fmt = LANGS[lang]
    for en, local in swaps:
        text = text.replace(en, local)
    text = text.replace(">Search<br>", f">{search}<br>").replace(
        "&emsp;Search<br>", f"&emsp;{search}<br>").replace('"Search"', f'"{search}"')

    def date(m):
        h = int(m[1]) % 12 + (12 if m[4] == "PM" else 0) if m[4] else int(m[1])
        return fmt.format(H=f"{h:02d}", h=h % 12 or 12, ap="pm" if h >= 12 else "am",
                          M=m[2], S=m[3], tz=m[5])
    return _EN_TIME.sub(date, text.replace("<b>Products:</b>", f"<b>{label}</b>"))


@pytest.mark.parametrize("lang", list(LANGS))
def test_a_takeout_in_another_language_gives_the_same_records(tmp_path, lang):
    folder, stem, search, *_ = LANGS[lang]
    want = _key(takeout.records(HTML))
    z = tmp_path / f"takeout-{lang}.zip"
    with zipfile.ZipFile(z, "w") as f:
        for product in ("Search", "YouTube", "Chrome"):
            page = (ACT / product / "MyActivity.html").read_text("utf-8")
            local = search if product == "Search" else product
            f.writestr(f"Takeout/{folder}/{local}/{stem}.html", _translate(page, lang))
    got = takeout.records(z)
    assert _key(got) == want and got.skipped == 1  # the unknown "XYZ" zone, as in English
    j = tmp_path / f"{stem}.json"  # the JSON export of the same rows
    j.write_text(_translate((EXPORTS / "takeout_MyActivity.json").read_text("utf-8"), lang),
                 "utf-8")
    assert _key(takeout.records(j)) == _key(takeout.records(EXPORTS / "takeout_MyActivity.json"))


def test_an_unknown_language_skips_its_rows_and_counts_them(tmp_path):
    page = (ACT / "Search" / "MyActivity.html").read_text("utf-8")
    ru = re.sub(r"Oct 2, 2026, [^<]*", "2 окт. 2026 г., 15:00:00 MSK", page)
    rows, skipped = takeout.html_rows(ru.encode("utf-8"))
    assert rows == [] and skipped == 3


def _cell(title, href, time, label="Products:", product="Search"):
    return (f'<div class="outer-cell"><div class="header-cell"><p class="mdl-typography--title">'
            f'{product}<br></p></div><div class="content-cell">{title} <a href="{href}">x</a>'
            f'<br>{time}<br></div><div class="content-cell mdl-typography--caption">'
            f'<b>{label}</b><br>&emsp;{product}<br></div></div>')


def test_a_title_that_looks_like_a_time_keeps_its_row():
    """R5 F1: the first line `_TIME` matches is a title here; the real time line follows."""
    page = ("<html><body>"
            + _cell("Searched for release notes 2024 v1.12.03", "https://www.google.com/search?q=a",
                    "Nov 17, 2026, 10:42:03 PM IST")
            + _cell("Searched for cats", "https://www.google.com/search?q=b",
                    "Nov 17, 2026, 10:43:03 PM IST") + "</body></html>").encode()
    rows, skipped = takeout.html_rows(page)
    assert skipped == 0 and len(rows) == 2
    assert rows[0]["title"] == "Searched for release notes 2024 v1.12.03 x"
    assert rows[0]["time"] == "2026-11-17T17:12:03Z"


def test_german_suffix_verbs_are_stripped_only_from_german_rows(tmp_path):
    """R5 F6: "gesucht" ends a real title in an English export; a German one (by its file or
    folder name, or its "Produkte:" label) drops the verb."""
    row = {"header": "YouTube", "title": "Watched Zeugen gesucht", "products": ["YouTube"],
           "titleUrl": "https://www.youtube.com/watch?v=abcdefghijk",
           "time": "2026-10-02T09:30:00Z"}
    en = tmp_path / "MyActivity.json"
    en.write_text(json.dumps([row]), "utf-8")
    assert [r.title for r in takeout.records(en)] == ["Zeugen gesucht"]
    de = tmp_path / "MeineAktivitäten.json"
    de.write_text(json.dumps([{**row, "title": "Zeugen angesehen"}]), "utf-8")
    assert [r.title for r in takeout.records(de)] == ["Zeugen"]
    yt = "https://www.youtube.com/watch?v=abcdefghijk"
    f = tmp_path / "MyActivity.html"
    for label, title, want in (("Products:", "Watched", "Zeugen gesucht"),
                               ("Produkte:", "", "Zeugen")):
        page = _cell(title, yt, "Nov 17, 2026, 10:42:03 PM IST", label, "YouTube")
        f.write_text(page.replace(">x</a>", ">Zeugen gesucht</a>" if title
                                  else ">Zeugen</a> angesehen"), "utf-8")
        assert [r.title for r in takeout.records(f)] == [want]


@pytest.mark.parametrize("text, want", [
    ("05.11.2026, 22:42:03 MEZ", datetime(2026, 11, 5, 21, 42, 3, tzinfo=UTC)),  # day-first
    ("17/11/2026, 22:42:03 UTC", datetime(2026, 11, 17, 22, 42, 3, tzinfo=UTC)),
    ("11/17/2026, 10:42:03 PM EST", None),  # month-first: no month 17
    ("11/05/2026, 10:42:03 PM EST", None),  # 11 May or 5 Nov: skipped, never guessed
])
def test_a_numeric_date_is_day_first_and_never_guessed(text, want):
    assert takeout.when(text) == want


# ---- YouTube's own history ("YouTube and YouTube Music" Takeout), invented rows -------------

YT = "Takeout/YouTube and YouTube Music/history/"
WATCH = [{"header": "YouTube", "title": "Watched Heaps in 7 minutes",
          "titleUrl": "https://www.youtube.com/watch?v=zzzzzzzzzz1",
          "subtitles": [{"name": "Tree Channel", "url": "https://www.youtube.com/channel/UC9"}],
          "time": "2026-10-03T08:00:00.123Z", "products": ["YouTube"],
          "activityControls": ["YouTube watch history"]}]
SEARCH = [{"header": "YouTube", "title": "Searched for binary heap",
           "titleUrl": "https://www.youtube.com/results?search_query=binary+heap",
           "time": "2026-10-03T07:55:00.000Z", "products": ["YouTube"],
           "activityControls": ["YouTube search history"]},
          {"header": "YouTube", "title": "Searched for hot girls bikini",
           "titleUrl": "https://www.youtube.com/results?search_query=hot+girls+bikini",
           "time": "2026-10-03T07:50:00.000Z", "products": ["YouTube"]}]
WATCH_HTML = (
    '<html><body><div class="mdl-grid"><div class="outer-cell mdl-cell"><div class="mdl-grid">'
    '<div class="header-cell mdl-cell"><p class="mdl-typography--title">YouTube<br></p></div>'
    '<div class="content-cell mdl-cell mdl-typography--body-1">Watched&nbsp;<a href="'
    'https://www.youtube.com/watch?v=zzzzzzzzzz1">Heaps in 7 minutes</a><br><a href="'
    'https://www.youtube.com/channel/UC9">Tree Channel</a><br>Oct 3, 2026, 8:00:00 AM UTC<br>'
    '</div><div class="content-cell mdl-cell mdl-typography--text-right"></div>'
    '<div class="content-cell mdl-cell mdl-typography--caption"><b>Products:</b><br>&emsp;'
    'YouTube<br></div></div></div></div></body></html>')
HEAP = ("takeout/youtube", "https://www.youtube.com/watch?v=zzzzzzzzzz1", "Heaps in 7 minutes",
        "Tree Channel")


def _yt(r):
    return (r.source, r.url, r.title, r.channel)


@pytest.mark.parametrize("name, body", [("watch-history.json", json.dumps(WATCH)),
                                        ("watch-history.html", WATCH_HTML)])
def test_youtube_watch_history_alone(tmp_path, name, body):
    f = tmp_path / name
    f.write_text(body, "utf-8")
    rs = takeout.records(f)
    assert [_yt(r) for r in rs] == [HEAP]
    assert rs[0].start.replace(microsecond=0) == datetime(2026, 10, 3, 8, tzinfo=UTC)


def test_youtube_only_takeout_zip_and_folder(tmp_path):
    members = {YT + "watch-history.json": json.dumps(WATCH),
               YT + "search-history.json": json.dumps(SEARCH)}
    z = tmp_path / "yt.zip"
    with zipfile.ZipFile(z, "w") as f:
        for n, b in members.items():
            f.writestr(n, b)
    for n, b in members.items():
        (tmp_path / "dir" / n).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "dir" / n).write_text(b, "utf-8")
    for src in (z, tmp_path / "dir"):
        assert [r.title for r in takeout.records(src)] == [
            "hot girls bikini", "binary heap", "Heaps in 7 minutes"]
    assert takeout.wanted(YT + "watch-history.html")  # its folder is named per language
    assert takeout.wanted("Takeout/YouTube und YouTube Music/Verlauf/search-history.html")


def test_a_watch_in_my_activity_and_watch_history_counts_once(tmp_path):
    # My Activity as HTML (Takeout's default, times to the second) beside watch-history.json
    z = tmp_path / "full.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Takeout/My Activity/YouTube/MyActivity.html", WATCH_HTML)
        f.writestr(YT + "watch-history.json", json.dumps(WATCH))
        f.writestr("Takeout/My Activity/YouTube/MyActivity.json", json.dumps(WATCH))
    rs = takeout.records(z)
    assert [_yt(r) for r in rs] == [HEAP] and rs[0].secs == sources.GAP_CAP


def test_youtube_history_goes_through_the_privacy_filter(tmp_path):
    f = tmp_path / "search-history.json"
    f.write_text(json.dumps(SEARCH), "utf-8")
    adult, plain = takeout.records(f)
    assert privacy.filter_record(adult.url, adult.title,
                                 settings=privacy.Settings(adult="skip")) is None  # the default
    k = privacy.filter_record(adult.url, adult.title, settings=privacy.Settings(adult="private"))
    assert (k.category, k.private) == ("adult", True)
    assert privacy.filter_record(plain.url, plain.title, settings=privacy.Settings()).private \
        is False
