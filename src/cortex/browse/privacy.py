"""The browse privacy filter (docs/design/browse-brain.md §2), as pure functions.

It runs first, before anything is kept: a record it drops is never stored anywhere. Order:
hard drop (scheme, local hosts, sign-in, payment and bank pages, secret-carrying URLs), URL
scrubbing (no query or fragment but a search term or a YouTube `v=`), the user's lists, then
the sensitive categories (`browse.skip_categories` drops; one not in `browse.cloud_categories`
stays on the private pages; by default adult is skipped and every other category kept private).
`filter_record` is the whole filter.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import unicodedata
from dataclasses import dataclass, replace
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from cortex.browse import lists
from cortex.vault import secret_pattern

HOST_RE = re.compile(r"^[a-z0-9.-]+$")
_WORD = re.compile(r"[a-z0-9]+")
_UWORD = re.compile(r"[^\W_]+")  # any script, casefolded first; "_" splits like "-"
_HYPHENED = re.compile(r"[^\W_]+(?:-[^\W_]+)+")
_NOTIFY = re.compile(r"^\(\d+\+?\)\s*")  # "(3) Title": an unread-count prefix
TITLE_MAX, TERM_MAX = 300, 200
ADULT = ("skip", "private")
SENSITIVE = ("private", "skip")


@dataclass(frozen=True)
class Settings:
    """`browse:` in ~/.cortex/config.yaml. adult: skip|private (code default skip, spec §0);
    sensitive: private|skip; deny/allow/private: domain lists (a domain covers subdomains);
    visible_to_claude: write the public pages, journal lines and touches (code default False:
    only private pages, so nothing from browsing reaches Claude)."""
    adult: str = "skip"
    sensitive: str = "private"
    deny: tuple[str, ...] = ()
    allow: tuple[str, ...] = ()
    private: tuple[str, ...] = ()
    visible_to_claude: bool = False
    browsers: tuple[str, ...] = ()  # `browsers:` which browsers to read; empty = every one found
    # categories (lists.CATEGORIES) kept only under browse/private/, never shown to Claude
    hidden_from_claude: tuple[str, ...] = lists.CATEGORIES
    # never saved at all; None: from the legacy adult/sensitive "skip" (adult by default)
    skip_categories: tuple[str, ...] | None = None
    private_mcp: bool = False  # allow `cortex mcp --private` (local models only)
    cloud_processing: bool = False  # a cloud model may process cloud-audience browsing
    skip_profiles: tuple[str, ...] = ()  # source keys unticked in the app: no automatic import

    def __post_init__(self):
        if self.skip_categories is None:
            object.__setattr__(self, "skip_categories", tuple(
                c for c in lists.CATEGORIES
                if (self.adult if c == "adult" else self.sensitive) == "skip"))


def load_settings(config: dict | None = None) -> Settings:
    """Settings from a config dict (default: ~/.cortex/config.yaml); bad values fall back."""
    if config is None:
        from cortex.taint import config as read
        config = read()
    b = config.get("browse") if isinstance(config, dict) else None
    b = b if isinstance(b, dict) else {}

    def domains(key):
        v = b.get(key) or []
        v = [v] if isinstance(v, str) else v if isinstance(v, list) else []
        v = {str(d).strip().lower().lstrip(".") for d in v if str(d).strip()}
        return tuple(sorted({_ascii_host(d) or d for d in v}))  # hosts are stored as punycode

    def cats(key, default):
        v = b.get(key)
        v = [v] if isinstance(v, str) else v if isinstance(v, list) else None
        return default if v is None else tuple(c for c in lists.CATEGORIES if c in {
            str(x).strip().lower() for x in v})

    # C9: the cloud allowlist is stored, so a category added later is local; a config from
    # before it (hidden_from_claude) counts every category it could not know of as hidden
    cloud = cats("cloud_categories", None)
    legacy = cats("hidden_from_claude", None)
    hidden = (tuple(c for c in lists.CATEGORIES if c not in cloud) if cloud is not None
              else lists.CATEGORIES if legacy is None
              else tuple(c for c in lists.CATEGORIES
                         if c in legacy or c not in lists.CATEGORIES_V1))
    return Settings(adult=b.get("adult") if b.get("adult") in ADULT else "skip",
                    hidden_from_claude=hidden,
                    skip_categories=cats("skip_categories", None),
                    private_mcp=b.get("private_mcp") is True,
                    cloud_processing=b.get("cloud_processing") is True,
                    sensitive=b.get("sensitive") if b.get("sensitive") in SENSITIVE
                    else "private",
                    deny=domains("deny_domains"), allow=domains("allow_domains"),
                    private=domains("private_domains"),
                    visible_to_claude=b.get("visible_to_claude") is True,
                    skip_profiles=tuple(sorted({x for x in b["skip_profiles"]
                                                if isinstance(x, str)}))
                    if isinstance(b.get("skip_profiles"), list) else (),
                    browsers=tuple(sorted({str(x).strip().lower() for x in (
                        [b["browsers"]] if isinstance(b.get("browsers"), str)
                        else b.get("browsers") if isinstance(b.get("browsers"), list) else [])
                        if str(x).strip()})))


@dataclass(frozen=True)
class Kept:
    """A record that passed the filter. `url` is scrubbed; `title` is still untrusted text
    (escape it when rendering); `category` is one of lists.CATEGORIES or None;
    `maybe` marks a category from a soft word only; it is private all the same."""
    url: str
    title: str
    host: str
    category: str | None = None
    private: bool = False
    search: str | None = None  # the kept search term
    engine: str | None = None
    video: str | None = None  # a YouTube video id
    maybe: bool = False
    # C1: kept local because its text has no local-model verdict yet (a local model is set up)
    local: bool = False


def in_domains(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _local(host: str) -> bool:
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return "." not in host  # an intranet single-label host
    return not ip.is_global  # private, loopback, link-local, CGNAT 100.64/10, reserved...


def _heads(parts) -> set[str]:
    """Per part (a path segment or host label): its first word, and its first two joined
    ("sign-in" -> signin), so "/how-oauth-works" is not a sign-in page but "/oauth/x" is."""
    out = set()
    for p in parts:
        w = _WORD.findall(p.lower())
        if w:
            out.add(w[0])
            if len(w) > 1:
                out.add(w[0] + w[1])
    return out


def _all_words(parts) -> set[str]:
    return {w for p in parts for w in _WORD.findall(p.lower())}


def _secret_query(query: str) -> bool:
    for k, _ in parse_qsl(query, keep_blank_values=True):
        k = k.lower()
        if k in lists.SECRET_KEYS or any(p in k for p in lists.SECRET_PARTS):
            return True
    return False


def _ascii_host(host: str) -> str | None:
    """An IDN host as punycode ("bücher.de" -> "xn--bcher-kva.de"), or None when it has no
    valid IDNA form."""
    if host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def drop_reason(url: str) -> str | None:
    """§2.1: why `url` is dropped outright, or None."""
    try:
        u = urlsplit(url.strip())
        host = _ascii_host((u.hostname or "").rstrip("."))
        _ = u.port  # raises on a garbage port
    except ValueError:
        return "scheme"
    if u.scheme.lower() not in ("http", "https"):
        return "scheme"
    if not host or not HOST_RE.match(host):
        return "scheme"
    if u.username or u.password:
        return "secret"
    if _local(host):
        return "local"
    parts = [unquote(p) for p in u.path.split("/")] + host.split(".")
    words, heads = _all_words(parts), _heads(parts)  # heads also join "sign_in" -> signin
    if in_domains(host, lists.AUTH_HOSTS) or (words | heads) & lists.AUTH_WORDS:
        return "sign-in"
    if (in_domains(host, lists.PAY_HOSTS) or in_domains(host, lists.BANK_HOSTS)
            or heads & lists.PAY_WORDS or words & lists.BANK_WORDS
            or any(lists.BANK_LABEL in label for label in host.split("."))):
        return "payment"
    if _secret_query(u.query) or _secret_query(u.fragment):
        return "secret"
    return None


def _engine(host: str, path: str) -> tuple[str, str] | None:
    """(engine, query key) of a known search page, or None."""
    h = host.removeprefix("www.").removeprefix("m.")
    if re.fullmatch(r"google(\.[a-z]{2,3}){1,2}", h) and path == "/search":
        return "google", "q"
    if h == "bing.com" and path == "/search":
        return "bing", "q"
    if h in ("duckduckgo.com", "html.duckduckgo.com"):
        return "duckduckgo", "q"
    if h == "search.brave.com" and path.startswith("/search"):
        return "brave", "q"
    if h == "youtube.com" and path == "/results":
        return "youtube", "search_query"
    if h == "perplexity.ai" and path.startswith("/search"):
        return "perplexity", "q"
    if re.fullmatch(r"amazon(\.[a-z]{2,3}){1,2}", h) and path == "/s":
        return "amazon", "k"
    if h == "github.com" and path == "/search":
        return "github", "q"
    return None


def scrub(url: str) -> tuple[str, str | None, str | None, str | None]:
    """§2.2: (url without query or fragment, search term, engine, YouTube id). Only a known
    engine's search term and a YouTube watch `v=` survive, as the only query parameter."""
    u = urlsplit(url.strip())
    host = (u.hostname or "").rstrip(".")
    host = _ascii_host(host) or host
    path = u.path or "/"
    base = f"{u.scheme.lower()}://{host}{quote(path, safe='/%:@-._~!$&()*+,;=')}"
    q = dict(parse_qsl(u.query))
    if eng := _engine(host, path):
        term = " ".join(q.get(eng[1], "").split())[:TERM_MAX]
        if term:
            return f"{base}?{eng[1]}={quote(term)}", term, eng[0], None
        return base, None, None, None
    h = host.removeprefix("www.").removeprefix("m.")
    vid = None
    if h == "youtube.com" and path == "/watch":
        vid = q.get("v")
    elif h == "youtu.be":
        vid = path.strip("/").split("/")[0]
    if vid and re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid):
        return f"https://www.youtube.com/watch?v={vid}", None, None, vid
    return base, None, None, None


def _tokens(s: str) -> list[str]:
    """Words of any script: letters, digits and combining marks (Devanagari vowel signs)."""
    return "".join(c if c.isalnum() or unicodedata.category(c)[0] == "M" else " "
                   for c in s).split()


def _leet(t: str) -> str:
    return t.translate(lists.LEET) if any(c.isalpha() for c in t) or len(t) == 1 else t


def _joined(tokens: list[str]) -> list[str]:
    """Runs of single characters joined ("p o r n", "p.o.r.n" -> porn), leetspeak mapped."""
    out, run = [], []
    for t in [*tokens, "  "]:
        if len(t) == 1:
            run.append(t)
            continue
        if len(run) > 1:
            out.append("".join(run).translate(lists.LEET))
        run = []
    return out


def _norm(*texts: str) -> tuple[set[str], str]:
    """(words, the words joined by spaces) for matching: NFKC, casefold, zero-width and soft
    hyphens dropped, Cyrillic/Greek look-alikes and leetspeak mapped, single letters joined.
    The words before the look-alike and leetspeak mapping stay in the set too."""
    s = unicodedata.normalize("NFKC", " ".join(t for t in texts if t)).casefold()
    s = re.sub(r"\bstd\s*::", " ", s.translate(lists.CONFUSABLES))  # C++, not health
    raw = _tokens(s)
    mapped = [_leet(t) for t in _tokens(re.sub(r"[@$]", lambda m: "a" if m[0] == "@" else "s",
                                               s))]
    words = set(raw) | set(mapped) | set(_joined(raw)) | set(_HYPHENED.findall(s))
    return words, " " + " ".join(mapped) + " "


def _words(*texts: str) -> set[str]:
    return _norm(*texts)[0]


def category(host: str, path: str, title: str, term: str | None = None, channel: str = ""
             ) -> tuple[str | None, bool]:
    """§2.4: (category or None, maybe). A built-in domain, an adult TLD, a host label starting
    with an adult brand ("xvideos2.com"), a STRONG word or a word starting with a STRONG_STEMS
    stem is sure; any other listed word, phrase or stem in the title, search term, channel or
    URL words (normalised, see `_norm`) is a maybe."""
    for c in lists.CATEGORIES:
        if in_domains(host, lists.CATEGORY_HOSTS[c]):
            return c, False
    try:  # an IDN host is matched in its own script, not as punycode
        host = host.encode("ascii").decode("idna") if "xn--" in host else host
    except UnicodeError:
        pass
    labels = host.split(".")
    if labels[-1] in lists.ADULT_TLDS or any(
            label.startswith(lists.ADULT_BRANDS) for label in labels):
        return "adult", False
    words, text = _norm(title, term or "", channel, host.replace(".", " "),
                        unquote(path).replace("/", " "))
    if words & lists.STRONG or any(w.startswith(lists.STRONG_STEMS) for w in words):
        return "adult", False
    for c in lists.CATEGORIES:
        if words & lists.CATEGORY_WORDS[c]:
            return c, True
    for c in lists.CATEGORIES:
        if any(f" {p} " in text for p in lists.PHRASES.get(c, ())):
            return c, True
    for c in lists.STEM_ORDER:
        if any(w.startswith(lists.STEMS[c]) for w in words):
            return c, True
    return None, False


def text_key(text: str) -> str:
    """The key a local-model verdict on `text` is cached under: a hash, never the text."""
    return hashlib.sha256(" ".join(text.casefold().split()).encode("utf-8")).hexdigest()[:16]


def clean_title(title: str) -> str:
    t = " ".join(str(title or "").split())
    return _NOTIFY.sub("", t)[:TITLE_MAX]


def judge(url: str, title: str, *, settings: Settings, channel: str = "") -> Kept | None:
    """Steps 1-3 and the category of step 4, before the category's skip/private action
    (`apply`). None: dropped by a hard rule, a secret, a bank title or the user's lists.
    `channel` (a video's, from the old extension's inbox) counts for the secret and category
    checks."""
    if drop_reason(url):
        return None
    title = clean_title(title)
    if any(p in title.casefold() for p in lists.BANK_TITLES):
        return None
    clean, term, engine, vid = scrub(url)
    # checked decoded, after the scrub: "q=api_key%3D..." hides from a check on the raw URL
    if any(secret_pattern(t) for t in (url, unquote(url), title, term or "", channel)):
        return None
    u = urlsplit(clean)
    host = u.hostname or ""
    if in_domains(host, settings.deny) or (settings.allow
                                           and not in_domains(host, settings.allow)):
        return None
    if in_domains(host, lists.WEBMAIL_HOSTS):  # the site name only, never the subject
        return Kept(f"https://{host}/", host, host, private=in_domains(host, settings.private))
    if vid:
        title = re.sub(r"\s+-\s+YouTube$", "", title)
    cat, maybe = category(host, u.path, title, term, channel)
    return Kept(clean, title, host, cat, in_domains(host, settings.private), term, engine,
                vid, maybe)


def audience(category: str | None, settings: Settings) -> str:
    """Who may read and process a category (docs/design/data-access.md §1): "skip" (never
    stored), "local" (local models only) or "cloud". A "maybe" counts as its category; None
    (uncategorised) follows visible_to_claude."""
    if category in settings.skip_categories:
        return "skip"
    if category in settings.hidden_from_claude or not settings.visible_to_claude:
        return "local"
    return "cloud"


def apply(k: Kept | None, *, settings: Settings) -> Kept | None:
    """Step 4's action (a "maybe" counts as its category): a category in
    settings.skip_categories drops the record; one in settings.hidden_from_claude keeps it for
    the private pages only; any other stays as it is (a private_domains host stays private)."""
    if k is None or k.category is None:
        return k
    if audience(k.category, settings) == "skip":
        return None
    return replace(k, private=True) if k.category in settings.hidden_from_claude else k


def filter_record(url: str, title: str, *, settings: Settings, channel: str = ""
                  ) -> Kept | None:
    """The whole §2 filter for one record (a "maybe" category counts as the category): the
    scrubbed record, or None when it must not be stored at all."""
    return apply(judge(url, title, settings=settings, channel=channel), settings=settings)
