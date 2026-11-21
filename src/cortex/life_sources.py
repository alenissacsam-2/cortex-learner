"""Sources B, part 1: `cortex import` sources for the owner's own life exports, on
`sources.Source` like more_sources (Sources A). Each keeps only the owner's own words or a
coarse daily summary, never other people's text, addresses, coordinates or files.

- mail: an .mbox (Google Takeout's "Sent", or All mail) into one page per month of the owner's
  sent messages: date, subject, recipients' names (never addresses) and the owner's own text,
  with quoted replies, forwarded parts, signatures and HTML stripped. Keyed by month with
  `updates`, so a re-import of a newer (cumulative) export rewrites a month page in place.
- selfchat: a WhatsApp chat-with-yourself .txt and Telegram Saved Messages (result.json) into
  month pages, a line per own message; media named, never copied. Private by default.
- location: Google Timeline JSON (both shapes) into month pages of one city per day, from the
  visits' postal addresses; no coordinates, streets or network. Private by default.
- photos: a photo folder into month pages of photos per day and album (folder) names; EXIF
  capture date only, never images or GPS.
- spotify: streaming history into a page per year of hours and top artists, tracks and
  podcasts; nothing per play.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

from cortex import importers, sources
from cortex.vault import VaultError

LOCAL = sources.Audience.LOCAL
MAIL_CHARS = 1500  # one message's own text on a month page
MAIL_NAMES = 5  # recipients named per message, then "and N more"


def _s(n: int) -> str:
    return "s" * (n != 1)


# ---- mail ----

MAIL_SKIPS = ("not sent by you", "a draft", "no readable date", "no text of yours",
              "malformed message", "the same message twice", "not an mbox file")
_SIG = re.compile(r"^-- ?$", re.M)  # the "-- " signature line (some clients drop the space)
_OUTLOOK = re.compile(r"^[ \t]*From: [^\n]*\n[ \t]*(?:Sent|Date): ", re.M)  # Outlook's quote
_VOID = {"br", "img", "hr", "meta", "input", "wbr", "link", "area", "col", "source"}
_BLOCK = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "table", "ul", "ol"}
_HIDE = ("gmail_quote", "gmail_signature", "moz-cite-prefix", "moz-signature")


class _Text(HTMLParser):
    """An HTML mail body as text: no script, style, quoted (blockquote, gmail_quote) or
    signature blocks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            if tag == "br" and not self.skip:
                self.out.append("\n")
            return
        cls = dict(attrs).get("class") or ""
        if self.skip or tag in ("script", "style", "head", "blockquote", "title") \
                or any(c in cls for c in _HIDE):
            self.skip += 1
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if self.skip:
            self.skip -= 1
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    p.close()
    return "\n".join(" ".join(ln.split()) for ln in "".join(p.out).splitlines())


def own_text(body: str) -> str:
    """The owner's own words in a mail body: everything from the first "On … wrote:",
    forwarded or original-message line, Outlook "From:/Sent:" block or "-- " signature line
    on is cut, `>` lines are dropped and any e-mail address left becomes [address]."""
    body = body.replace("\r\n", "\n")
    cuts = [m.start() for rx in (sources._ATTRIBUTION, sources._FORWARDED, _OUTLOOK, _SIG)
            if (m := rx.search(body))]
    body = body[:min(cuts)] if cuts else body
    body = "\n".join(ln for ln in body.split("\n") if not ln.lstrip().startswith(">"))
    body = importers._EMAIL.sub("[address]", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def _addr(value) -> str:
    from email.utils import parseaddr
    return parseaddr(str(value or ""))[1].lower()


def _labels(head) -> set[str]:
    return {x.strip().lower() for x in str(head.get("X-Gmail-Labels") or "").split(",")}


def _head(box, key):
    """A message's headers only, read up to the blank line (the first pass)."""
    from email import policy
    from email.parser import BytesHeaderParser
    lines = []
    with box.get_file(key) as f:
        for line in iter(f.readline, b""):
            if line in (b"\n", b"\r\n"):
                break
            lines.append(line)
    return BytesHeaderParser(policy=policy.default).parsebytes(b"".join(lines))


def _names(msg) -> str:
    """Recipients' display names (To, Cc), never their addresses."""
    from email.utils import getaddresses
    got = getaddresses([str(v) for k in ("to", "cc") for v in msg.get_all(k, [])])
    names = list(dict.fromkeys(n for name, _ in got
                               if (n := " ".join(importers._EMAIL.sub("", name).split())
                                   .strip("\"' "))))
    more = len(names) - MAIL_NAMES
    return ", ".join(names[:MAIL_NAMES]) + (f" and {more} more" if more > 0 else "")


def _message(raw: bytes) -> tuple[str, str] | str:
    """(day, section) of one sent message, or a skip reason."""
    from email import policy
    from email.parser import BytesParser
    from email.utils import parsedate_to_datetime
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    try:
        day = parsedate_to_datetime(str(msg["Date"] or "")).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return "no readable date"
    # the body only: never an attachment or a forwarded message/rfc822 part
    part = msg.get_body(preferencelist=("plain", "html"))
    body = "" if part is None else part.get_content()
    if part is not None and part.get_content_type() == "text/html":
        body = html_text(body)
    text = own_text(body)
    if not text:
        return "no text of yours"
    subject = " ".join(importers._EMAIL.sub("[address]", str(msg["Subject"] or "")).split())
    to = _names(msg)
    return day, (f"## {day}: {subject or '(no subject)'}\n" + (f"To {to}.\n" if to else "")
                 + f"\n{_clip(text, MAIL_CHARS)}")


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + " …"


def _open_box(path: Path):
    """(the open mbox, {key: its headers or None when malformed}); None when not an mbox."""
    import mailbox
    try:
        box = mailbox.mbox(path, create=False)
        keys = list(box.iterkeys())
    except (OSError, mailbox.Error, ValueError):
        return None
    heads = {}
    for k in keys:
        try:
            heads[k] = _head(box, k)
        except Exception:  # noqa: BLE001 - email raises many types on a malformed message
            heads[k] = None
    return box, heads


def _sent_from(path: Path, heads: dict) -> set[str]:
    """The owner's addresses in one box: the senders of its messages labelled Sent; in a box
    with no Gmail labels (a mail app's export), every sender when the box is a Sent folder."""
    heads = [h for h in heads.values() if h]
    if any(h.get("X-Gmail-Labels") for h in heads):
        return {_addr(h["From"]) for h in heads if "sent" in _labels(h)}
    return {_addr(h["From"]) for h in heads} if re.search(r"\bsent\b", path.stem, re.I) \
        else set()


def _mail_box(box, heads: dict, owners: set[str], months: dict, seen: set):
    """Skip reasons for one open .mbox, filling `months` (month -> [(day, section)]). Only
    mail from `owners` (`_sent_from` across all the boxes) is the owner's."""
    if not heads:
        yield "not an mbox file"
    for k, h in heads.items():
        if h is None:
            yield "malformed message"
            continue
        labels = _labels(h)
        if "draft" in labels or "drafts" in labels:
            yield "a draft"
            continue
        if _addr(h["From"]) not in owners:
            yield "not sent by you"
            continue
        mid = str(h["Message-ID"] or "").strip()
        if mid and mid in seen:
            yield "the same message twice"
            continue
        seen.add(mid)
        try:
            got = _message(box.get_bytes(k))
        except Exception:  # noqa: BLE001 - a broken charset, header or MIME part
            got = "malformed message"
        if isinstance(got, str):
            yield got
        else:
            months[got[0][:7]].append(got)


def mail_source(path: Path) -> sources.Source:
    """An .mbox file, or a folder of them, into one page per month of sent mail; see the
    module docstring. Mailbox needs the file itself, so a .zip is refused (unzip it first)."""
    path = Path(path)
    if path.suffix.lower() == ".zip":
        raise VaultError("invalid_argument", f"Unzip {path.name} first and give the .mbox "
                         "file in it (Takeout: Mail/Sent.mbox or All mail).")
    boxes = [path] if path.is_file() else \
        sorted(p for p in path.rglob("*.mbox") if p.is_file()) if path.is_dir() else []
    if not boxes:
        raise VaultError("invalid_argument", f"Found no .mbox file in {path.name}.")

    def items():
        months, seen = defaultdict(list), set()
        opened = []
        try:
            for p in boxes:
                opened.append(_open_box(p))
                if not opened[-1]:
                    yield "not an mbox file"
            owners = set().union(*(_sent_from(p, o[1]) for p, o in zip(boxes, opened, strict=True)
                                   if o))
            for o in opened:
                if o:
                    yield from _mail_box(*o, owners, months, seen)
        finally:
            for o in opened:
                if o:
                    o[0].close()
        for month, msgs in sorted(months.items()):
            msgs.sort(key=lambda m: m[0])
            n = len(msgs)
            yield sources.Item(f"Sent mail {month}", msgs[-1][0],
                               f"My sent mail in {month}: {n} message{_s(n)}.\n\n"
                               + "\n\n".join(s for _, s in msgs),
                               str(boxes[0] if len(boxes) == 1 else path), key=f"mail {month}")

    return sources.Source("mail", "Sent mail", "file", items, default_tier=LOCAL,
                          prefix="mail", strips_quotes=True, updates=True, skips=MAIL_SKIPS)


# ---- notes to self (WhatsApp, Telegram) ----

CHAT_CHARS = 2000  # one message on a month line
SELF_SKIPS = ("not a chat with yourself", "a system line", "deleted", "forwarded",
              "not yours", "empty", "malformed file", "over 5 MB")
# Android "12/03/2024, 09:15 - Name: text"; iOS "[12/03/24, 9:15:23 AM] Name: text"
_WA = re.compile(r"^[\u200e\u200f]?\[?(\d{1,2})[./-](\d{1,2})[./-](\d{2,4}),?\s+"
                 r"(\d{1,2})[:.](\d{2})(?:[:.]\d{2})?\s?([AaPp]\.?\s?[Mm]\.?)?\]?\s(?:-\s)?(.*)$")
_WA_OMITTED = {"image": "[photo]", "video": "[video]", "audio": "[audio]",
               "sticker": "[sticker]", "gif": "[GIF]", "document": "[file]",
               "contact card": "[contact]"}
_EXT = {"[photo]": ("jpg", "jpeg", "png", "webp", "heic", "gif"),
        "[video]": ("mp4", "mov", "3gp", "mkv"), "[audio]": ("opus", "ogg", "m4a", "mp3",
                                                            "aac", "wav")}
_TG_MEDIA = {"voice_message": "[voice note]", "video_message": "[video]",
             "video_file": "[video]", "audio_file": "[audio]", "sticker": "[sticker]",
             "animation": "[GIF]"}


def _by_ext(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower().strip("> ")
    return next((tag for tag, exts in _EXT.items() if ext in exts), "[file]")


def wa_media(text: str) -> str:
    """A WhatsApp media line as "[photo]", "[video]"…; any other text as it is. The file
    itself is never read or copied."""
    low = text.strip().strip("\u200e").lower()
    if low == "<media omitted>":
        return "[media]"
    if m := re.fullmatch(r"(image|video|audio|sticker|gif|document|contact card) omitted",
                         low):
        return _WA_OMITTED[m.group(1)]
    if low.startswith("<attached:"):
        return _by_ext(low.removesuffix(">"))
    if low.endswith("(file attached)"):
        return _by_ext(low.removesuffix("(file attached)").strip())
    return text


def _one_line(text: str) -> str:
    return _clip(" / ".join(x.strip() for x in text.splitlines() if x.strip()), CHAT_CHARS)


def _hm(h: str, m: str, ampm: str | None) -> str:
    hour, ap = int(h), (ampm or "").lower()
    if ap.startswith("p") and hour < 12:
        hour += 12
    elif ap.startswith("a") and hour == 12:
        hour = 0
    return f"{hour:02d}:{m}"


def _whatsapp(text: str):
    """Items of a WhatsApp chat export with only one person writing in it, or skip reasons."""
    rows, last = [], None
    for line in text.splitlines():
        if m := _WA.match(line):
            last = [m.groups(), m.group(7)]
            rows.append(last)
        elif last is not None:  # a message's next line
            last[1] += "\n" + line
    if not rows:
        yield "malformed file"
        return
    pairs = [(int(g[0]), int(g[1])) for g, _ in rows]
    month_first = not any(a > 12 for a, _ in pairs) and any(b > 12 for _, b in pairs)
    msgs, senders = [], Counter()
    for g, rest in rows:
        who, sep, said = rest.partition(": ")
        if not sep:
            msgs.append("a system line")
            continue
        senders[who.strip("\u200e ")] += 1
        a, b, y = int(g[0]), int(g[1]), int(g[2])
        d, mo = (b, a) if month_first else (a, b)
        try:
            day = date(y + 2000 if y < 100 else y, mo, d).isoformat()
        except ValueError:
            msgs.append("malformed file")
            continue
        msgs.append((day, _hm(g[3], g[4], g[5]), said))
    if len(senders) > 1:  # a chat with others: whose is whose is not known, so none is kept
        yield "not a chat with yourself"
        return
    for x in msgs:
        if isinstance(x, str):
            yield x
            continue
        day, hm, said = x
        said = wa_media(said.strip())
        if said.lower().strip("\u200e ") in ("this message was deleted",
                                             "you deleted this message"):
            yield "deleted"
        elif not said.strip():
            yield "empty"
        else:
            yield sources.Item(day, day, f"{day} {hm} WhatsApp: {_one_line(said)}")


def _tg_text(t) -> str:
    if isinstance(t, list):
        return "".join(x if isinstance(x, str) else str(x.get("text", "")) for x in t
                       if isinstance(x, str | dict))
    return t if isinstance(t, str) else ""


def _telegram(data):
    """Items of Telegram's Saved Messages (a single-chat result.json, or the full export's
    chat of type saved_messages): the owner's own messages; forwarded ones are skipped."""
    chats = [data] if isinstance(data, dict) and "messages" in data else \
        ((data.get("chats") or {}).get("list") or []) if isinstance(data, dict) else []
    saved = [c for c in chats if isinstance(c, dict) and c.get("type") == "saved_messages"]
    if not saved:
        yield "not a chat with yourself"
        return
    for chat in saved:
        msgs = [m for m in chat.get("messages") or [] if isinstance(m, dict)]
        owner = Counter(m.get("from_id") for m in msgs if m.get("type") == "message"
                        and not m.get("forwarded_from")).most_common(1)
        for m in msgs:
            if m.get("type") != "message":
                yield "a system line"
                continue
            if m.get("forwarded_from") or m.get("saved_from"):
                yield "forwarded"
                continue
            if owner and m.get("from_id") != owner[0][0]:
                yield "not yours"
                continue
            when = str(m.get("date") or "")
            if not re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d", when):
                yield "malformed file"
                continue
            tag = "[photo]" if m.get("photo") else _TG_MEDIA.get(m.get("media_type"), "") \
                or ("[file]" if m.get("file") else "")
            said = " ".join(x for x in (tag, _tg_text(m.get("text")).strip()) if x)
            if not said:
                yield "empty"
                continue
            yield sources.Item(when[:10], when[:10],
                               f"{when[:10]} {when[11:16]} Telegram: {_one_line(said)}")


def selfchat_source(path: Path) -> sources.Source:
    """A WhatsApp chat-with-yourself .txt export and Telegram Saved Messages result.json (a
    file, folder or .zip) into one private page per month: the owner's own messages, a line
    each, media as "[photo]" and the like."""
    import json
    path = Path(path)
    found = [f for f in importers._files(path)
             if f[0].lower().endswith(".txt") and _WA.match(
                 f[3](512).decode("utf-8-sig", "replace").lstrip("\ufeff").split("\n", 1)[0])
             or f[0].lower().endswith(".json")]
    if not found:
        raise VaultError("invalid_argument", f"Found no WhatsApp chat (.txt) or Telegram "
                         f"result.json in {path.name}.")

    def items():
        for name, _size, _mtime, read in found:
            try:
                raw = read()
            except VaultError:
                yield "over 5 MB"
                continue
            if name.lower().endswith(".txt"):
                yield from _whatsapp(raw.decode("utf-8-sig", "replace"))
                continue
            try:
                data = json.loads(raw.decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError):
                yield "malformed file"
                continue
            yield from _telegram(data)

    return sources.Source("selfchat", "Notes to self", "file", items, default_tier=LOCAL,
                          page="month", noun="messages", skips=SELF_SKIPS)


# ---- location: a city per day ----

LOC_SKIPS = ("no place name", "not a location file", "raw points (not read)",
             "malformed file", "over 200 MB")
_STREET = re.compile(r"\b(?:road|rd|street|st|avenue|ave|lane|ln|marg|drive|dr|blvd|boulevard|"
                     r"way|highway|hwy|cross|floor|block|sector|plot|house|flat|apt|suite|"
                     r"building|tower|nagar|layout|colony)\b", re.I)


def city(address: str) -> str | None:
    """The city in a one-line postal address ("MG Road, Bengaluru, Karnataka 560001, India"
    -> "Bengaluru"; "Street 1, 10117 Berlin, Germany" -> "Berlin"), or None when the part it
    lands on could be finer than a city (digits or a street word in it)."""
    parts = [p.strip() for p in str(address).replace("\n", ",").split(",") if p.strip()]
    if len(parts) < 2:
        return None
    cand = parts[-2]
    if re.match(r"\d", cand):  # "10117 Berlin": the postcode first
        cand = re.sub(r"^[\d\s-]+", "", cand)
    elif re.search(r"\d", cand) and len(parts) >= 3:  # "Karnataka 560001": the region
        cand = parts[-3]
    elif len(parts) >= 4:  # "street, city, region, country"
        cand = parts[-3]
    cand = re.sub(r"\s+\d[\d\s-]*$", "", cand).strip()  # a postcode after the city
    if not cand or re.search(r"\d", cand) or _STREET.search(cand) or len(cand) > 40:
        return None
    return cand


def _when_day(value) -> str:
    """YYYY-MM-DD from an ISO time (its own local date) or epoch milliseconds (UTC)."""
    s = str(value or "")
    if re.match(r"\d{4}-\d\d-\d\d", s):
        return s[:10]
    if s.isdigit():
        from datetime import UTC, datetime
        try:
            return datetime.fromtimestamp(int(s) / 1000, UTC).date().isoformat()
        except (ValueError, OverflowError, OSError):
            return ""
    return ""


def _visits(data):
    """(day, address or None) per place visit, from either Takeout shape: the monthly
    Semantic Location History (`timelineObjects` placeVisit) or the on-device Timeline
    (`semanticSegments`, or a bare list of segments). Movements are not places: skipped.
    Raises ValueError on any other shape."""
    if isinstance(data, dict) and isinstance(data.get("timelineObjects"), list):
        for o in data["timelineObjects"]:
            v = o.get("placeVisit") if isinstance(o, dict) else None
            if isinstance(v, dict):
                dur, loc = v.get("duration") or {}, v.get("location") or {}
                yield (_when_day(dur.get("startTimestamp") or dur.get("startTimestampMs")),
                       loc.get("address"))
        return
    segs = data.get("semanticSegments") if isinstance(data, dict) else data
    if not isinstance(segs, list):
        raise ValueError("not a location file")
    for s in segs:
        visit = s.get("visit") if isinstance(s, dict) else None
        if isinstance(visit, dict):
            top = visit.get("topCandidate") or {}
            place = top.get("placeLocation")
            yield (_when_day(s.get("startTime")),
                   top.get("address") or (place.get("address") if isinstance(place, dict)
                                          else None))


def location_file(name: str) -> bool:
    """A Takeout location history JSON, by its path (for `importers.detect`)."""
    low = name.lower().replace("\\", "/")
    base = low.rsplit("/", 1)[-1]
    return low.endswith(".json") and (
        "semantic location history/" in low
        or base in ("timeline.json", "location-history.json", "records.json")
        or re.fullmatch(r"\d{4}_(?:january|february|march|april|may|june|july|august|"
                        r"september|october|november|december)\.json", base) is not None)


def location_source(path: Path) -> sources.Source:
    """Google Timeline exports into month pages of `- YYYY-MM-DD: City`, one city per day
    (the most visited that day). Coordinates, street addresses, raw points (Records.json) and
    movements are never read into a page; a day with no address is skipped (no network, no
    reverse geocoding)."""
    import json
    path = Path(path)
    found = [f for f in importers._files(path) if f[0].lower().endswith(".json")]
    if not found:
        raise VaultError("invalid_argument", f"Found no location history .json in "
                         f"{path.name}.")

    def items():
        days = defaultdict(Counter)
        for name, _size, _mtime, read in found:
            if name.lower().rsplit("/", 1)[-1] == "records.json":
                yield "raw points (not read)"
                continue
            try:
                data = json.loads(read().decode("utf-8-sig"))
                visits = list(_visits(data))
            except VaultError:
                yield "over 200 MB"
                continue
            except ValueError as e:
                yield "not a location file" if str(e) == "not a location file" \
                    else "malformed file"
                continue
            except (AttributeError, TypeError):  # a list of something else
                yield "not a location file"
                continue
            for day, address in visits:
                if day:
                    days[day][city(address) if address else None] += 1
        for day, cities in sorted(days.items()):
            cities.pop(None, None)
            if not cities:
                yield "no place name"
                continue
            yield sources.Item(day, day, f"{day}: {cities.most_common(1)[0][0]}")

    return sources.Source("location", "Location", "file", items, default_tier=LOCAL,
                          page="month", noun="places", coarse_place=True, skips=LOC_SKIPS)


# ---- photo dates and albums ----

PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff", ".dng",
                  ".gif")
ALBUMS = 5  # album names per day, then "and N more"


def photo_day(p: Path) -> str:
    """The day a photo was taken: EXIF DateTimeOriginal (else DateTime) when Pillow can read
    it, else the file's modified date. Only those two tags are read: never GPS or any other."""
    try:
        from PIL import Image
    except ImportError:  # Pillow is optional (it comes with pystray on Windows)
        Image = None
    if Image is not None:
        try:
            with Image.open(p) as im:
                exif = im.getexif()
                raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)
            if m := re.match(r"(\d{4}):(\d\d):(\d\d)", str(raw or "")):
                return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except Exception:  # noqa: BLE001 - Pillow raises many types on a damaged image
            pass
    from datetime import datetime
    return datetime.fromtimestamp(p.stat().st_mtime).date().isoformat()


def photos_source(path: Path) -> sources.Source:
    """A photo folder into month pages of `- YYYY-MM-DD: N photos (albums)`, the album being
    each photo's folder name. Images are never stored or copied; no thumbnails, no GPS."""
    root = Path(path)
    if not root.is_dir():
        raise VaultError("invalid_argument", f"{root.name} is not a folder of photos.")

    def items():
        days = defaultdict(Counter)
        for p in importers._walk(root, lambda rel: any(
                x.startswith(".") for x in rel.split("/")), []):
            if not p.is_file() or p.name.startswith("."):
                continue
            if p.suffix.lower() not in PHOTO_SUFFIXES:
                yield "not a photo"
                continue
            try:
                day = photo_day(p)
            except (OSError, ValueError, OverflowError):
                yield "unreadable"
                continue
            days[day][p.parent.name if p.parent != root else root.name] += 1
        for day, albums in sorted(days.items()):
            n, names = sum(albums.values()), sorted(albums)
            more = len(names) - ALBUMS
            yield sources.Item(day, day, f"{day}: {n} photo{_s(n)} ("
                               + ", ".join(names[:ALBUMS])
                               + (f" and {more} more" if more > 0 else "") + ")")

    return sources.Source("photos", "Photos", "folder", items, page="month", noun="days",
                          coarse_place=True, skips=("not a photo", "unreadable"))


# ---- Spotify, a page per year ----

TOP, TOP_PODCASTS = 20, 10
SPOTIFY_SKIPS = ("not a listening history file", "covered by the extended history",
                 "malformed file", "over 200 MB")


def spotify_file(name: str) -> str | None:
    """"extended" (Streaming_History_Audio_*.json, endsong_*.json), "account"
    (StreamingHistory*.json, music and podcasts) or None."""
    base = name.lower().replace("\\", "/").rsplit("/", 1)[-1]
    if not base.endswith(".json"):
        return None
    if base.startswith(("streaming_history_audio_", "endsong_")):
        return "extended"
    return "account" if base.startswith("streaminghistory") else None


def _hours(ms: float) -> str:
    return f"{ms / 3.6e6:,.1f}"


def _plays(rows: list, kind: str, years: dict) -> None:
    """Add one file's rows to `years` (year -> {total, music, podcasts, artists, tracks,
    shows, last}); nothing per play is kept."""
    for r in rows:
        if not isinstance(r, dict):
            continue
        extended = kind == "extended"
        when = str(r.get("ts" if extended else "endTime") or "")
        ms = r.get("ms_played" if extended else "msPlayed")
        if not re.match(r"\d{4}-\d\d-\d\d", when) or not isinstance(ms, int | float) or ms <= 0:
            continue
        y = years.setdefault(when[:4], {"total": 0, "music": 0, "podcasts": 0,
                                        "artists": Counter(), "tracks": Counter(),
                                        "shows": Counter(), "last": ""})
        y["total"] += ms
        y["last"] = max(y["last"], when[:10])
        track = r.get("master_metadata_track_name" if extended else "trackName")
        artist = r.get("master_metadata_album_artist_name" if extended else "artistName")
        show = r.get("episode_show_name" if extended else "podcastName")
        if track and artist:
            y["music"] += ms
            y["artists"][artist] += ms
            y["tracks"][track, artist] += ms
        elif show:
            y["podcasts"] += ms
            y["shows"][show] += ms


def _year_text(year: str, y: dict) -> str:
    head = (f"My Spotify listening in {year}: {_hours(y['total'])} hours (music "
            f"{_hours(y['music'])}, podcasts {_hours(y['podcasts'])}).")
    out = [head]
    for title, rows in (
            (f"Top {TOP} artists", [f"{a}: {_hours(ms)} hours"
                                    for a, ms in y["artists"].most_common(TOP)]),
            (f"Top {TOP} tracks", [f"\"{t}\" by {a}: {_hours(ms)} hours"
                                   for (t, a), ms in y["tracks"].most_common(TOP)]),
            ("Top podcasts", [f"{s}: {_hours(ms)} hours"
                              for s, ms in y["shows"].most_common(TOP_PODCASTS)])):
        if rows:
            out.append(f"## {title}\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(rows, 1)))
    return "\n\n".join(out)


def spotify_source(path: Path) -> sources.Source:
    """A Spotify data export (the account data's StreamingHistory*.json or the extended
    Streaming_History_Audio_*.json; the extended one wins when both are there) into one page
    per year: total hours, the top artists and tracks by time, the top podcasts. Keyed by
    year with `updates`, so a newer export rewrites the year in place."""
    import json
    path = Path(path)
    found = [(f, k) for f in importers._files(path) if (k := spotify_file(f[0]))]
    if not found:
        raise VaultError("invalid_argument", f"Found no Spotify streaming history "
                         f"(StreamingHistory*.json or Streaming_History_Audio_*.json) in "
                         f"{path.name}.")
    extended = any(k == "extended" for _, k in found)

    def items():
        years = {}
        for (_name, _size, _mtime, read), kind in found:
            if extended and kind == "account":
                yield "covered by the extended history"
                continue
            try:
                rows = json.loads(read().decode("utf-8-sig"))
            except VaultError:
                yield "over 200 MB"
                continue
            except ValueError:
                yield "malformed file"
                continue
            if not isinstance(rows, list):
                yield "not a listening history file"
                continue
            _plays(rows, kind, years)
        for year, y in sorted(years.items()):
            yield sources.Item(f"Spotify {year}", y["last"], _year_text(year, y),
                               key=f"spotify {year}")

    return sources.Source("spotify", "Spotify", "file", items, prefix="listening",
                          updates=True, skips=SPOTIFY_SKIPS)


SOURCE_OF = {"mail": mail_source, "selfchat": selfchat_source, "location": location_source,
             "photos": photos_source, "spotify": spotify_source}
