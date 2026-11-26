"""Google Takeout "My Activity" for Search, YouTube and Chrome as browse visits, the shape
`sources.read` returns, so the browse privacy filter and build can take them. Takeout writes one
`My Activity/<product>/MyActivity.json` per product when Activity records are set to JSON; each
row has header, title, titleUrl, time, products and, for YouTube, subtitles (the channel).
Rows of other products (Gemini Apps, Maps and the rest) are skipped. Takeout's default format
is HTML (`MyActivity.html`); `html_rows` reads it into the same rows, here and for Gemini chats.
A "YouTube and YouTube Music" Takeout holds `history/watch-history.json` and
`search-history.json` (or .html): the same rows and the same HTML cells (seen in open-source
Takeout parsers), read the same way. Their file names stay English in every language.
"""

from __future__ import annotations

import html
import re
import unicodedata
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from keepfold.browse.sources import GAP_CAP, Visit
from keepfold.vault import VaultError

# Takeout writes folder and file names, product names, verbs, labels and dates in the account
# language. Seen in open-source Takeout parsers: de "Meine Aktivitäten"/"Suche" and
# "Gesucht nach:"; ja "マイアクティビティ", "検索" and the を検索しました-style verbs. The other
# "My Activity" names are Google's own UI term in that language.
# shortcut: unconfirmed wording for es, pt, fr, hi, id (verbs, Search's folder and product
# name, the "Products:" label), de (visit verb, suffix verbs) and ja ("サービス:"), verify with a
# real export. A miss leaves the verb in a title or, for a date, skips and counts the row.
PRODUCTS = {"Search": "search", "YouTube": "youtube", "Chrome": "chrome",
            **dict.fromkeys(("Suche", "Búsqueda", "Pesquisa", "Recherche", "खोज", "Penelusuran",
                             "検索"), "search")}
# "My Activity" as the file is named (spaces dropped): en, de, es, pt, fr, hi, id, ja
_ACTIVITY = {"myactivity", "meineaktivitäten", "miactividad", "minhaatividade", "monactivité",
             "मेरीगतिविधि", "aktivitassaya", "マイアクティビティ"}
# YouTube's own history ("YouTube and YouTube Music" Takeout): English in every language
_YOUTUBE = ("watch-history", "search-history")
_PREFIXES = ("Searched for", "Watched", "Visited", "Viewed",
             "Gesucht nach:", "Gesucht nach", "Aufgerufen:", "Besucht:", "Angesehen:",
             "Has buscado", "Has visto", "Has visitado",
             "Pesquisou", "Pesquisou por", "Assistiu a", "Assistiu", "Visitou",
             "Vous avez recherché", "Vous avez regardé", "Vous avez consulté",
             "खोजा गया:", "देखा गया:", "विज़िट किया गया:",
             "Mencari", "Menelusuri", "Menonton", "Mengunjungi")
_SUFFIXES = ("を検索しました", "を視聴しました", "にアクセスしました")
_VERB = re.compile(r"^(?:{})\s+|^「\s*|\s*」?\s*(?:{})$".format(
    "|".join(map(re.escape, sorted(_PREFIXES, key=len, reverse=True))),
    "|".join(map(re.escape, _SUFFIXES))))
# German suffix verbs: stripped from German rows only, as other titles may end in them
_DE_VERB = re.compile(r"\s+(?:angesehen|gesucht)$")
_PRODUCTS_LABEL = {"Products:", "Produkte:", "Productos:", "Produtos:", "Produits:", "उत्पाद:",
                   "Produk:", "サービス:"}
READ_HINT = ("Takeouts in English, Spanish, Portuguese, French, German, Hindi, Indonesian or "
             "Japanese are read (JSON or HTML): other languages' dates and verbs are not "
             "understood.")
HEAD = 256 * 1024  # an HTML My Activity file opens with ~140 KB of CSS before its first row


def activity_file(name: str) -> str:
    """"json" or "html" for a My Activity file in a supported language (a prefix such as
    "takeout_MyActivity.json" allowed) or YouTube's watch-history or search-history, else ""."""
    base = unicodedata.normalize("NFC", name.replace("\\", "/").rsplit("/", 1)[-1].lower())
    stem, _, ext = base.replace(" ", "").rpartition(".")
    return ext if ext in ("json", "html") and any(
        map(stem.endswith, (*_ACTIVITY, *_YOUTUBE))) else ""


def wanted(name: str) -> bool:
    """A file `records` reads: any JSON one, and an HTML one alone, in a Search, YouTube or
    Chrome folder or YouTube's own history (its folder is named in the account language), so a
    full Takeout's big Ads or Play Store pages are not parsed."""
    kind = activity_file(name)
    parts = unicodedata.normalize("NFC", name.replace("\\", "/").lower()).split("/")
    return kind == "json" or kind == "html" and (
        len(parts) == 1 or parts[-2] in {p.lower() for p in PRODUCTS}
        or parts[-1].rpartition(".")[0].endswith(_YOUTUBE))


class Rows(list):
    """The visits `records` returns; `skipped` counts HTML rows whose time could not be read."""
    skipped = 0


def records(path: Path) -> Rows:
    """Every Search, YouTube and Chrome row with a URL and a readable time in a Takeout export
    (the .zip, its folder or one MyActivity, watch-history or search-history .json or .html),
    oldest first. A visit lasts until the next one, at most GAP_CAP, as in `sources.read`. A
    row read twice (My Activity's YouTube file and watch-history both hold a watch) is kept
    once, by its time to the second and URL: an HTML time has no fraction of a second."""
    path = Path(path)
    from keepfold.importers import _files, json_value  # the one size-capped reader
    files = _files(path)
    if path.suffix.lower() == ".zip" or path.is_dir():
        files = [f for f in files if wanted(f[0])]
    if not files:
        raise VaultError("invalid_argument", f"Found no My Activity file in {path.name}; expected "
                         "My Activity/<product>/MyActivity.json or .html, or YouTube's "
                         "history/watch-history or search-history (.json or .html).")
    out = Rows()
    for name, *_, read in files:  # one file's rows at a time
        # German by the file or folder name; an HTML row may also say so by its label
        de = "meineaktivitäten" in unicodedata.normalize("NFC", name.lower().replace(" ", ""))
        try:
            html_file = name.lower().endswith((".html", ".htm"))
            data, skipped = html_rows(read(), name) if html_file else (json_value(read), 0)
            out.skipped += skipped
            if not isinstance(data, list | Iterator):
                raise VaultError("invalid_argument",
                                 f"{name} should hold a list of activity rows.")
            out += [v for e in data if (v := _visit(e, de))]  # C11: a big file streams here
        except (OSError, ValueError, zipfile.BadZipFile) as e:
            raise VaultError("invalid_argument", f"Cannot read {name}: {e}") from None
    out.sort(key=lambda v: v.start)
    seen: set = set()
    out[:] = [v for v in out if (k := (v.start.replace(microsecond=0), v.url)) not in seen
              and not seen.add(k)]
    for v, nxt in zip(out, [*out[1:], None], strict=True):
        v.secs = min(max((nxt.start - v.start).total_seconds(), 0.0), GAP_CAP) if nxt \
            else GAP_CAP
    return out


def _visit(e, de: bool = False) -> Visit | None:
    if not isinstance(e, dict):
        return None
    products = e.get("products") if isinstance(e.get("products"), list) else []
    kind = next((PRODUCTS[p] for p in products if isinstance(p, str) and p in PRODUCTS), None)
    url = e.get("titleUrl")
    if kind is None or not isinstance(url, str) or not url.startswith("http"):
        return None
    try:
        start = datetime.fromisoformat(str(e.get("time")).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None
    u = urlsplit(url)
    if u.netloc.endswith("google.com") and u.path == "/url":  # Google's redirect to the page
        url = parse_qs(u.query).get("q", [url])[0]
    subs = e.get("subtitles")
    channel = subs[0].get("name") if kind == "youtube" and isinstance(subs, list) and subs \
        and isinstance(subs[0], dict) else ""
    title = _VERB.sub("", str(e.get("title") or "")).strip()
    if de or e.get("de") is True or "Suche" in products:
        title = _DE_VERB.sub("", title)
    return Visit(start, url, title, 0.0,
                 source=f"takeout/{kind}", channel=channel if isinstance(channel, str) else "")


# Takeout's HTML: one div.outer-cell per row, holding a header-cell (the product), a
# content-cell (verb + link text, <br>, the YouTube channel link or the rest of a Gemini prompt,
# <br>, the local time, then a Gemini answer) and a caption cell ("Products:" then the name).
_TIME = re.compile(r"\d{4}.{0,12}?\d{1,2}[:.]\d{2}[:.]\d{2}")
# shortcut: common abbreviations (IST is India's, MEZ/MESZ German, WIB Indonesian). Unknown ones
# skip the row; add if seen.
_TZ = {"UTC": 0, "GMT": 0, "IST": 330, "PST": -480, "PDT": -420, "MST": -420, "MDT": -360,
       "CST": -360, "CDT": -300, "EST": -300, "EDT": -240, "AKST": -540, "AKDT": -480,
       "HST": -600, "BST": 60, "CET": 60, "CEST": 120, "EET": 120, "EEST": 180, "MSK": 180,
       "PKT": 300, "NPT": 345, "BDT": 360, "ICT": 420, "WIB": 420, "SGT": 480, "HKT": 480,
       "PHT": 480, "AWST": 480, "JST": 540, "KST": 540, "ACST": 570, "AEST": 600,
       "AEDT": 660, "NZST": 720, "NZDT": 780, "MEZ": 60, "MESZ": 120, "WEZ": 0, "WESZ": 60,
       "BRT": -180, "ART": -180, "WITA": 480, "WIT": 540}
# month names and abbreviations: en, es, pt, fr, de, id, hi (ja dates are numeric). A word
# matches whole, else by its first 4 or 3 letters ("noviembre" -> "nov", "juil." -> "juil").
_MONTHS = {unicodedata.normalize("NFC", k): n for n, ks in enumerate((
    "jan ene janv जन जनवरी", "feb fev fév févr फ़र फ़रवरी", "mar mär mars मार्च",
    "apr abr avr अप्रैल", "may mai mei मई", "jun juin जून", "jul juil जुल जुलाई",
    "aug ago aoû agu agt अग अगस्त", "sep set sept सित सितंबर",
    "oct okt out अक्तू अक्तूबर अक्टू अक्टूबर", "nov नव नवंबर", "dec dic dez déc des दिस दिसंबर"),
    1) for k in ks.split()}


def _month(word: str) -> int | None:
    w = word.lower()
    return _MONTHS.get(w) or _MONTHS.get(w[:4]) or _MONTHS.get(w[:3])


def when(text: str) -> datetime | None:
    """A Takeout HTML time in a supported language ("Nov 17, 2026, 10:42:03 PM IST",
    "17.11.2026, 22:42:03 MEZ", "17 नव॰ 2026, 10:42:03 pm IST", "2026/11/17 22:42:03 JST")
    in UTC, or None."""
    body, _, tz = unicodedata.normalize("NFC", " ".join(text.split())).rpartition(" ")
    m = re.fullmatch(r"(?:GMT|UTC)([+\-−])(\d{1,2}):?(\d{2})?", tz)
    mins = (-1 if m[1] != "+" else 1) * (int(m[2]) * 60 + int(m[3] or 0)) if m else _TZ.get(tz)
    t = re.search(r"(?<![\d.:])(\d{1,2})[:.](\d{2})[:.](\d{2})(?![\d.:])", body)
    if mins is None or t is None:
        return None
    date, ampm, hour = body[:t.start()], re.sub(r"[\s.]", "", body[t.end():]).lower(), int(t[1])
    if ampm in ("am", "pm") or "午前" in date or "午後" in date:
        hour = hour % 12 + (12 if ampm == "pm" or "午後" in date else 0)
    elif ampm:
        return None
    words = [w.strip("॰") for w in re.split(r"[\s,/.年月日-]+", date) if w.strip("॰")]
    nums = [int(w) for w in words if w.isdigit()]
    months = [k for w in words if not w.isdigit() and (k := _month(w))]
    if months and len(nums) == 2:  # "Nov 17, 2026", "17 de nov. de 2026"
        (y, d), mo = (nums if nums[0] > 31 else nums[::-1]), months[0]
    elif not months and len(nums) == 3:  # "17.11.2026", "2026/11/17"
        # assumed day-first (no supported locale writes month-first); "11/05/2026" could be
        # either, so a slashed date with both numbers <= 12 is skipped and counted, not guessed
        if "/" in date and nums[0] <= 12 and nums[1] <= 12 and nums[2] > 31:
            return None
        y, mo, d = nums if nums[0] > 31 else nums[::-1]
    else:
        return None
    try:
        return datetime(y, mo, d, hour, int(t[2]), int(t[3]),
                        tzinfo=timezone(timedelta(minutes=mins))).astimezone(UTC)
    except ValueError:
        return None


class _Cells(HTMLParser):
    """Each outer cell as [header, content lines, product, "Products:" label]; a line is
    [(text, href or None)]."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells, self.divs, self.mode, self.href = [], [], None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div":
            cls = a.get("class") or ""
            self.divs.append(cls)
            if "outer-cell" in cls:
                self.cells.append(["", [[]], "", ""])
                self.mode = None
            elif self.cells and "header-cell" in cls:
                self.mode = "header"
            elif self.cells and "caption" in cls:
                self.mode = "caption"
            elif self.cells and "content-cell" in cls and "text-right" not in cls:
                self.mode = "content"
        elif tag == "br" and self.mode == "content":
            self.cells[-1][1].append([])
        elif tag == "a":
            self.href = a.get("href")

    def handle_endtag(self, tag):
        if tag == "a":
            self.href = None
        elif tag == "div" and self.divs and "cell" in self.divs.pop():
            self.mode = None

    def handle_data(self, data):
        if not self.cells or not self.mode:
            return
        cell = self.cells[-1]
        if self.mode == "header" and not cell[0]:
            cell[0] = data.strip()
        elif self.mode == "content":
            cell[1][-1].append((data, self.href))
        elif self.mode == "caption" and data.strip():
            if cell[2] == "?":  # the text right after <b>Products:</b>
                cell[2] = data.strip()
            elif data.strip() in _PRODUCTS_LABEL:
                cell[2], cell[3] = "?", data.strip()


def html_rows(data: bytes, name: str = "MyActivity.html") -> tuple[list[dict], int]:
    """An HTML My Activity file as the JSON export's rows (header, title, titleUrl,
    subtitles, time, products, safeHtmlItem for a Gemini answer), and how many rows were
    skipped because their time could not be read."""
    p = _Cells()
    p.feed(data.decode("utf-8", errors="replace"))
    p.close()
    if not p.cells:
        raise VaultError("invalid_argument", f"{name} is HTML that Keepfold cannot read as Takeout "
                         f"My Activity. {READ_HINT}")
    rows, skipped = [], 0
    for header, lines, product, label in p.cells:
        text = [" ".join("".join(t for t, _ in line).split()) for line in lines]
        # the first line that reads as a time: a title such as "notes 2024 v1.12.03" may match
        i, t = next(((i, w) for i, s in enumerate(text) if _TIME.search(s) and (w := when(s))),
                    (None, None))
        if t is None:
            skipped += 1
            continue
        # after line 0, a line holding a link is a subtitle (YouTube's channel); the rest of the
        # lines before the time continue the title (a multi-line Gemini prompt)
        linked = [k > 0 and any(h for _, h in line) for k, line in enumerate(lines[:i])]
        row = {"header": header,
               "title": "\n".join(s for s, ln in zip(text[:i], linked, strict=True)
                                  if s and not ln),
               "time": t.isoformat().replace("+00:00", "Z"), "products": [product or header]}
        if label == "Produkte:":
            row["de"] = True
        url = next((h for _, h in lines[0] if h), None)
        if url:
            row["titleUrl"] = url
        subs = [{"name": text[k], "url": next(h for _, h in lines[k] if h)}
                for k, ln in enumerate(linked) if ln]
        if subs:
            row["subtitles"] = subs
        answer = " ".join(" ".join(t for t, _ in line) for line in lines[i + 1:]).strip()
        if answer:  # _gemini strips tags and unescapes, as for the JSON's answer html
            row["safeHtmlItem"] = [{"html": html.escape(answer)}]
        rows.append(row)
    return rows, skipped
