"""The one shape every `cortex import` source plugs into (v9 item 13).

A `Source` says what it is (id, label, what it reads, its default tier) and yields `Item`s, or a
reason string for each entry it skips. `run` does the rest, the same for every source: the
never-capture filter (`scrub`), secret redaction, the tier, dedupe, the page write through the
vault, the dry run and the report counts (`importers.report_line` prints them).

Two page shapes. `page="item"`: one `resource` page per item, `<prefix>-<title slug>`, deduped by
a sha of the item's text (notes; PDFs, highlights and mail fit here). `page="month"`: one page per
month, `<prefix>-YYYY-MM` (`<prefix>-private-YYYY-MM` when private, so private items never join a
public page), a line per item, deduped by the line (calendars; location, photo dates and Spotify
fit here). Both carry a `Source: <label> · Date: …` line (see importers' docstring). An item
source that sets `updates` dedupes by `key` (a repo's path, a book) and rewrites that page when
a re-import brings a changed text, instead of adding a second page.

Tiers reuse `access.Audience`: `--tier private|normal|skip` is LOCAL (pages `private: true`),
CLOUD (normal pages) or SKIP (nothing imported). The last choice per source is kept in TIERS_FILE.

What Cortex never captures (docs/PRIVACY.md), and where each line is enforced:
- secrets, keys, passwords: `scrub` here for every source (chats.redact), and
  vault.scan_secrets on every write;
- sign-in, payment and bank pages: browse.privacy.drop_reason (browse.lists AUTH/PAY/BANK);
- adult browsing: browse.privacy.Settings.adult defaults to "skip";
- AI replies in chat imports: chat_import keeps the user's own turns only;
- code, diffs, command output from AI chats and coding sessions: chat_import (user turns only;
  a coding session keeps paths and commit subjects only). A source of such text sets
  `drops_code` and `scrub` drops its fenced code; the owner's own notes keep their code;
- image or audio content: an Item is text only; importers' notes source skips non-text files
  and drops embeds;
- other people's quoted text in mail: `scrub` here, for a source that sets `strips_quotes`
  (mail); the owner's own notes and calendar keep what they quote;
- location finer than a city: `scrub` here rounds coordinates to 0.1 degree (about 10 km), for
  a source that sets `coarse_place` (location, photos); notes and calendar keep theirs;
- inferred health, sexuality, religion or politics labels: no code infers labels about the
  person; browse.lists' categories with those names only keep such visits local.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ruamel.yaml.comments import CommentedMap

from cortex import chats
from cortex.access import Audience
from cortex.catalog import slugify
from cortex.vault import Vault, VaultError, _dump_fm, _to_yaml, is_private

TIERS_FILE = ".cortex/import-tiers.json"  # {source id: "local" | "cloud" | "skip"}
WORDS = {"private": Audience.LOCAL, "normal": Audience.CLOUD, "skip": Audience.SKIP}
CODE_LEFT_OUT = "[code left out]"

_HASH = re.compile(r"· sha: ([0-9a-f]{16})$", re.M)
_WIKI = re.compile(r"(!?)\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
# "On Tue, 3 Nov 2026 at 09:30, Asha <her address> wrote:", which mail clients may wrap once
_ATTRIBUTION = re.compile(r"^On [^\n]{1,200}(?:\n[^\n]{0,200})?wrote:[ \t]*\r?$", re.M)
_QUOTED = re.compile(r"^[ \t]*>[^\n]*(?:\n|\Z)", re.M)
_FORWARDED = re.compile(r"^[ \t]*-{2,}[ \t]*(?:Original Message|Forwarded message)[ \t]*-{2,}",
                        re.M | re.I)
# shortcut: only "lat, lon" pairs with 4+ decimals (about 10 m); a source that reads a location
# history (item 14) reduces its own places to a city per day
_COORDS = re.compile(r"(?<![\d.])(-?\d{1,2}\.\d{4,})(\s*,\s*)(-?\d{1,3}\.\d{4,})(?![\d.])")


@dataclass
class Item:
    title: str
    date: str  # YYYY-MM-DD
    text: str  # an item page's body, or a month page's line
    origin: str = ""  # the file path or URL it came from, shown as `From:`
    tags: list[str] = field(default_factory=list)
    private: bool = False  # the item itself asks to be private (a note's frontmatter)
    key: str = ""  # what dedupe hashes when it is not `text` (a note: with its frontmatter)


@dataclass
class Source:
    id: str  # the key its tier is remembered under: "notes", "calendar"
    label: str  # its pages' `Source:` line: "Obsidian", "Calendar"
    reads: str  # "folder" or "file"
    items: Callable[[], Iterable[Item | str]]  # a str is an entry skipped, and why
    default_tier: Audience = Audience.CLOUD
    page: str = "item"  # "item" or "month"
    prefix: str = ""  # page ids; the id when empty
    noun: str = "items"  # a month page's heading and summary: "events"
    skips: tuple[str, ...] = ()  # the skip reasons, in the order the report lists them
    # the never-capture filters past secrets, for text that is not the owner's own writing
    drops_code: bool = False  # AI or tool text: fenced code becomes CODE_LEFT_OUT
    strips_quotes: bool = False  # mail: other people's quoted text is dropped
    coarse_place: bool = False  # location, photos: coordinates rounded to 0.1 degree
    updates: bool = False  # a known `key` rewrites its page when the text changed (repos)


def tier(value: str | Audience) -> Audience:
    """`private`, `normal` or `skip` (the CLI's words), or an Audience value (the app's)."""
    try:
        return WORDS.get(value) or Audience(value)
    except ValueError:
        raise ValueError(f"tier must be one of {', '.join(WORDS)}.") from None


def word(t: Audience) -> str:
    return next(w for w, a in WORDS.items() if a is t)


def remembered(vault: Vault) -> dict[str, Audience]:
    try:
        d = json.loads((vault.root / TIERS_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: Audience(v) for k, v in d.items() if v in set(Audience)} \
        if isinstance(d, dict) else {}


def pick_tier(vault: Vault, src: Source, given: str | None = None,
              ask: Callable[[Source], str] | None = None,
              save: bool = True) -> tuple[Audience, str]:
    """(tier, how it was chosen): `given` (the --tier flag or the app's click), else the last
    choice for this source, else `ask` (a real terminal), else the source's default. A given or
    asked choice is remembered when `save` (not in a dry run)."""
    last = remembered(vault)
    if given is None and src.id in last:
        return last[src.id], "your last choice"
    if given is None and ask is None:
        return src.default_tier, "the default"
    t = tier(given if given is not None else ask(src))
    if save:
        vault.write_text(vault.root / TIERS_FILE, json.dumps(
            {k: v.value for k, v in {**last, src.id: t}.items()}, indent=1, sort_keys=True)
            + "\n")
    return t, "chosen"


def _coarse(m: re.Match) -> str:
    lat, lon = float(m.group(1)), float(m.group(3))
    if abs(lat) > 90 or abs(lon) > 180:
        return m.group(0)
    return f"{lat:.1f}{m.group(2)}{lon:.1f}"


def scrub(text: str, src: Source | None = None) -> str:
    """The never-capture filter every imported text passes: secrets become [redacted], always.
    Per `src`: `drops_code` turns fenced code blocks into CODE_LEFT_OUT, `strips_quotes` drops
    quoted mail (the `>` lines after an "On … wrote:" line, and everything from an "Original
    Message" or "Forwarded message" line on), `coarse_place` rounds coordinates to 0.1 degree."""
    drops_code = bool(src and src.drops_code)
    out, fence = [], ""
    for line in text.splitlines(keepends=True) if drops_code else ():
        if fence:
            s = line.strip()
            if s.startswith(fence) and not s.strip(fence[0]):
                fence = ""
            continue
        if m := _FENCE.match(line):
            fence = m.group(1)
            out.append(CODE_LEFT_OUT + ("\n" if line.endswith("\n") else ""))
            continue
        out.append(line)
    text = "".join(out) if drops_code else text
    if src and src.strips_quotes:
        if m := _ATTRIBUTION.search(text):
            text = text[:m.start()] + _QUOTED.sub("", text[m.end():])
        if m := _FORWARDED.search(text):
            text = text[:m.start()]
    if src and src.coarse_place:
        text = _COORDS.sub(_coarse, text)
    return chats.redact(text)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _free_id(vault: Vault, base: str, taken: set[str]) -> str:
    """`base`, or `base-2`, `-3`… when a page of any kind (ids are unique across kinds) or this
    import already has it."""
    nid, n = base, 2
    while nid in taken or any((vault.root / d / f"{nid}.md").exists()
                              for k in vault.KIND_DIRS for d in vault.dirs(k)):
        nid = f"{base[:78].rstrip('-.')}-{n}"
        n += 1
    taken.add(nid)
    return nid


def _known_hashes(vault: Vault) -> dict[str, Path | None]:
    """sha -> the page that carries it."""
    return {h: p for p in (vault.root / "resources").glob("*.md")
            for h in _HASH.findall(p.read_text("utf-8", errors="replace"))}


def run(vault: Vault, src: Source, tier: Audience = Audience.CLOUD,
        dry_run: bool = False) -> dict:
    """Import `src` at `tier`. The report: `read` (entries, skipped ones too), `imported` (new
    pages for item sources, new lines for month sources), `private`, `duplicate`, `reasons`
    (skip reason -> count), `items` (items read), `pages` (pages they belong on) and `updated`
    (pages rewritten by a source that `updates`)."""
    r = {"read": 0, "imported": 0, "private": 0, "duplicate": 0, "updated": 0,
         "reasons": dict.fromkeys(src.skips, 0), "items": 0, "pages": 0, "tier": tier.value}
    if tier is Audience.SKIP:
        return r
    (_items if src.page == "item" else _months)(vault, src, tier is Audience.LOCAL, dry_run, r)
    return r


def _read(src: Source, r: dict):
    for it in src.items():
        r["read"] += 1
        if isinstance(it, str):
            r["reasons"][it] = r["reasons"].get(it, 0) + 1
        else:
            r["items"] += 1
            yield it


def _items(vault: Vault, src: Source, private: bool, dry_run: bool, r: dict) -> None:
    known, seen, taken, todo = _known_hashes(vault), set(), set(), []
    for it in _read(src, r):
        h = _sha(it.key or it.text)
        old = known.get(h)
        if h in seen or (h in known and not (src.updates and old)):
            r["duplicate"] += 1
            continue
        seen.add(h)
        if old:  # `updates`: compared with its page below; a private page stays private
            todo.append((old.stem, it, h, private or it.private or _was_private(vault, old),
                          old))
            continue
        # the id comes from the filtered title, so a key in a title never reaches a file name
        slug = slugify(scrub(it.title, src)) or "untitled"
        nid = _free_id(vault, f"{src.prefix or src.id}-{slug}"[:81].rstrip("-."), taken)
        todo.append((nid, it, h, private or it.private, None))
    new = [t for t in todo if t[4] is None]
    r["imported"], r["pages"] = len(new), len(todo)
    r["private"] = sum(t[3] for t in new)
    if dry_run and len(new) == len(todo):
        return
    # [[Title]] links between this import's public pages keep working; others become plain text
    ids = {slugify(it.title): nid for nid, it, _, hidden, _ in todo if not hidden}

    def link(m):
        if m.group(1):  # an embedded image or file
            return ""
        to = ids.get(slugify(PurePosixPath(m.group(2)).stem))
        text = m.group(3) or m.group(2)
        return f"[[{to}|{text}]]" if to else text

    for nid, it, h, hidden, old in todo:
        body = scrub(_WIKI.sub(link, it.text), src).strip() \
            .removeprefix(f"# {it.title}\n")
        first = next((s.strip("# ").strip() for s in body.splitlines()
                      if s.strip("# ").strip()), it.title)
        fields = {"title": scrub(it.title, src), "kind": "resource", "resource_type": "other",
                  "status": "reference", "covers": [], "related": [], "date": it.date,
                  "summary": first[:160]}
        if it.tags:
            fields["tags"] = list(it.tags)
        if hidden:
            fields["private"] = True
        origin = f" · From: {scrub(it.origin, src)}" if it.origin else ""
        page = (f"# {fields['title']}\n\nSource: {src.label} · Date: {it.date}{origin} · "
                f"sha: {h}\n\n{body}\n")
        if old is None:
            if not dry_run:
                vault.create_note("resource", nid, fields, page)
            continue
        text = _note_text(nid, fields, page)
        if old.read_text("utf-8", errors="replace") == text:
            r["duplicate"] += 1
            continue
        r["updated"] += 1
        r["private"] += hidden
        if not dry_run:
            vault.update_text(old, lambda _, t=text: t)


def _was_private(vault: Vault, page: Path) -> bool:
    """Whether a page an `updates` import rewrites is private now (set by an earlier private
    import or by hand); a page that does not parse counts as private (fail closed)."""
    try:
        return is_private(vault.read_path(page))
    except VaultError:
        return True


def _note_text(nid: str, fields: dict, body: str) -> str:
    """The text `Vault.create_note` writes, to compare with and rewrite an updated page."""
    data = CommentedMap((k, _to_yaml(v)) for k, v in {"id": nid, **fields}.items())
    return "---\n" + _dump_fm(data, "\n") + "---\n" + body


def _months(vault: Vault, src: Source, private: bool, dry_run: bool, r: dict) -> None:
    months = defaultdict(set)
    for it in _read(src, r):
        months[it.date[:7], private or it.private].add(f"- {scrub(it.text, src)}")
    r["pages"] = len(months)
    for (month, hidden), lines in sorted(months.items()):
        prefix = src.prefix or src.id
        nid = f"{prefix}-private-{month}" if hidden else f"{prefix}-{month}"
        p = vault.path_for("resource", nid)
        have = set(p.read_text("utf-8").splitlines()) if p.is_file() else set()
        add = sorted(lines - have)
        r["imported"] += len(add)
        r["private"] += len(add) if hidden else 0
        if dry_run or not add:
            continue
        heading = src.noun.capitalize()
        if have:
            # shortcut: later imports append, so a month is sorted within each import only
            vault.append_to_section("resource", nid, heading, "\n".join(add))
            continue
        fields = {"title": f"{src.label} {month}", "kind": "resource", "resource_type": "other",
                  "status": "reference", "covers": [], "related": [], "date": f"{month}-01",
                  "summary": f"{src.label} {src.noun} in {month}"}
        if hidden:
            fields["private"] = True
        vault.create_note("resource", nid, fields, f"# {src.label} {month}\n\nSource: "
                          f"{src.label} · Date: {month}-01\n\n## {heading}\n" + "\n".join(add)
                          + "\n")
    r["duplicate"] = r["items"] - r["imported"]
