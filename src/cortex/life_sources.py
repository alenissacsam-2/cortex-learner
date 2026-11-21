"""Sources B, part 1: `cortex import` sources for the owner's own life exports, on
`sources.Source` like more_sources (Sources A). Each keeps only the owner's own words or a
coarse daily summary, never other people's text, addresses, coordinates or files.

- mail: an .mbox (Google Takeout's "Sent", or All mail) into one page per month of the owner's
  sent messages: date, subject, recipients' names (never addresses) and the owner's own text,
  with quoted replies, forwarded parts, signatures and HTML stripped. Keyed by month with
  `updates`, so a re-import of a newer (cumulative) export rewrites a month page in place.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
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


def _mail_box(path: Path, months: dict, seen: set):
    """Skip reasons for one .mbox, filling `months` (month -> [(day, section)]). The owner is
    who sent the messages in a Sent label, else the most common From address."""
    import mailbox
    try:
        box = mailbox.mbox(path, create=False)
        keys = list(box.iterkeys())
    except (OSError, mailbox.Error, ValueError):
        yield "not an mbox file"
        return
    try:
        heads = {}
        for k in keys:
            try:
                heads[k] = _head(box, k)
            except Exception:  # noqa: BLE001 - email raises many types on a malformed message
                heads[k] = None
        sent = Counter(_addr(h["From"]) for h in heads.values() if h and "sent" in _labels(h))
        every = Counter(_addr(h["From"]) for h in heads.values() if h)
        owners = set(sent) or {a for a, _ in every.most_common(1)}
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
    finally:
        box.close()


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
        for box in boxes:
            yield from _mail_box(box, months, seen)
        for month, msgs in sorted(months.items()):
            msgs.sort(key=lambda m: m[0])
            n = len(msgs)
            yield sources.Item(f"Sent mail {month}", msgs[-1][0],
                               f"My sent mail in {month}: {n} message{_s(n)}.\n\n"
                               + "\n\n".join(s for _, s in msgs),
                               str(boxes[0] if len(boxes) == 1 else path), key=f"mail {month}")

    return sources.Source("mail", "Sent mail", "file", items, default_tier=LOCAL,
                          prefix="mail", strips_quotes=True, updates=True, skips=MAIL_SKIPS)


SOURCE_OF = {"mail": mail_source}
