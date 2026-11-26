"""Vault lint (docs/design/phase4-wiki.md §3): candidate problems for the model to fix.

Read-only; returns at most MAX_FINDINGS findings `{kind, id, detail}`, most severe kinds first
(SEVERITY), noisy kinds capped (KIND_CAP), plus per-kind totals before the cut. Private notes are
never reported (they still count as link sources and targets).
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from collections.abc import Mapping

from keepfold.catalog import slugify
from keepfold.events import DAY_ROLLOVER
from keepfold.index import EDGE_KEYS, STOPWORDS, _edge_targets
from keepfold.people import PREFIX
from keepfold.relink import TOKEN_RE, _tgt
from keepfold.render import plain
from keepfold.vault import Vault, is_private

MAX_FINDINGS = 30
# Order before the cut: broken links first, unlinked mentions (the noisiest) last.
SEVERITY = ("id_clash", "sync_conflict", "missing_link", "expired_claim", "merged_related",
            "orphan", "no_summary", "weak_summary", "links_no_reason", "resource_no_covers",
            "stale_project", "long_note", "old_fleeting", "unlinked_mention")
KIND_CAP = {"unlinked_mention": 10, "weak_summary": 10}
# RR-26: a summary is what search and prime show; one that only restates the title, or is a
# few words, adds no words to be found by
SUMMARY_MIN_CHARS, SUMMARY_TITLE_OVERLAP = 30, 0.7
# `keepfold lint --findability`: words of a page's second chunk that must find it in the top N
PROBE_WORDS, FINDABLE_TOP = 20, 3
LONG_NOTE_WORDS = 350
STALE_PROJECT_DAYS = 30
FLEETING_DAYS = 7
# profile pages and generated session notes have no links; browse pages hold browser titles and
# rollup pages generated counts, not the user's text to link (no mention findings)
NOT_ORPHANS = ("me", "session", "browse", "rollup")
_ANY_LINK_RE = re.compile(r"\[\[[^\]]*\]\]")
_FLEETING_RE = re.compile(r"^- \[ \] (.+?) \(fleeting, (\d{4}-\d\d-\d\d)\)\s*$")
_BARE_LINK_RE = re.compile(r"^[-*+]\s*\[\[[^\]]*\]\]\s*$")  # a ## Links bullet with no reason
# an imported page (a chat, YouTube, NotebookLM) says `Source: <app>` near its top; it is filed by
# topic, not by concept, and its text is not the user's to link: no covers or mention findings
_IMPORTED_RE = re.compile(r"^(?:Timeline: .*?· )?Source: [^\n·]+", re.M)


def _out_edges(fields: Mapping, body: str) -> tuple[set[str], set[str]]:
    """(valid-id targets from frontmatter edges, raw wikilink targets from the body)."""
    fm = {t for k in EDGE_KEYS if k in fields for t in _edge_targets(fields[k])}
    # [[people/x]] (the people hub's form) is the person note x
    # TOKEN_RE skips fenced blocks and inline code, as Obsidian does
    raw = (_tgt(m["t"]) for m in TOKEN_RE.finditer(body) if m["t"] is not None)
    return fm, {t.removeprefix(PREFIX) for t in raw if t}


def lint_report(vault: Vault, now: dt.datetime | None = None,
                withheld: set[str] = frozenset()) -> tuple[list[dict], dict]:
    """(findings sorted by SEVERITY, capped per KIND_CAP and at MAX_FINDINGS;
    {kind: total found} before any cut). `withheld`: bare ids of me/ pages whose claim text
    the caller may not see (mcp_server.Keepfold._withheld)."""
    now = now or dt.datetime.now()
    notes = list(vault.iter_notes())
    kinds_of: dict[str, list[str]] = {}
    for n in notes:
        kinds_of.setdefault(n.id, []).append(n.kind)
    merged = {n.id for n in notes if n.kind == "concept" and n.fields.get("merged_into")}
    out: list[dict] = []

    def add(kind: str, id: str, detail: str) -> None:
        out.append({"kind": kind, "id": id, "detail": detail})

    for id, kinds in sorted(kinds_of.items()):
        if len(kinds) > 1 and not _is_private_id(notes, id):
            add("id_clash", id, f"used by {', '.join(kinds)}")

    from keepfold import sync_copies

    for copy, orig in sync_copies.find_visible(vault)[0]:  # never a private page's
        add("sync_conflict", plain(copy), f"a sync conflict copy of {plain(orig)}: open both, "
            "keep what you want in the original, delete the copy")

    edges, inbound = {}, Counter()
    for n in notes:
        fm, body = _out_edges(n.fields, n.body)
        edges[(n.kind, n.id)] = (fm, body)
        for t in (fm | body) & kinds_of.keys() - {n.id}:
            inbound[t] += 1

    mention = _mention_re(notes, merged)
    for n in notes:
        if is_private(n):
            continue
        fm, body = edges[(n.kind, n.id)]
        for t in sorted(fm | body):
            if t not in kinds_of and not t.startswith("raw/"):
                add("missing_link", n.id, f"links to [[{plain(t, 60)}]], which does not exist")
        if not ((fm | body) & kinds_of.keys() - {n.id}) and not inbound[n.id] \
                and n.kind not in NOT_ORPHANS:
            add("orphan", n.id, "no links in or out")
        imported = n.kind == "resource" and bool(_IMPORTED_RE.search(n.body[:600]))
        if n.kind == "resource" and not imported \
                and not _edge_targets(n.fields.get("covers") or []):
            add("resource_no_covers", n.id, "covers lists no concept")
        if n.kind == "project" and n.fields.get("status") == "active":
            age = (now - dt.datetime.fromtimestamp(n.path.stat().st_mtime)).days
            if age > STALE_PROJECT_DAYS:
                add("stale_project", n.id, f"active, not updated for {age} days")
        if n.kind == "note" and n.fields.get("note_type") == "permanent":
            words = len(n.body.split())
            if words > LONG_NOTE_WORDS:
                add("long_note", n.id, f"{words} words; split it into atomic notes")
            if not str(n.fields.get("summary") or "").strip():
                add("no_summary", n.id, "permanent note has no summary")
            links = _links_section(n.body)
            if not links:
                add("links_no_reason", n.id, "## Links is empty")
            elif any(_BARE_LINK_RE.match(ln) for ln in links):
                add("links_no_reason", n.id, "a ## Links bullet has no reason")
        if n.kind not in NOT_ORPHANS and (why := weak_summary(n.fields)):  # me/ labels
            add("weak_summary", n.id, why)
        for t in sorted(_edge_targets(n.fields.get("related") or [])):
            if t in merged:
                add("merged_related", n.id, f"related points at [[{t}]], which was merged")
        if mention and n.kind not in NOT_ORPHANS and not imported:
            found = {m.lower(): m for m in mention[0].findall(_ANY_LINK_RE.sub(" ", n.body))}
            for low, text in sorted(found.items()):
                cid = mention[1].get(low)  # None if case folding differs from lower()
                if cid and cid != n.id and cid not in fm | body:
                    add("unlinked_mention", n.id, f"mentions '{plain(text, 60)}' without "
                        f"[[{cid}]]")

    from keepfold import claims  # claims -> reflect -> lint: a top-level import is circular

    today = (now - DAY_ROLLOVER).date().isoformat()  # `now` is naive local time
    for r in claims.rows(vault):  # agenda 4: served no more, but never moved automatically
        if r["status"] == "live" and (until := claims.expired(r["text"], today)) \
                and r["page"] not in withheld and not _is_private_id(notes, r["page"]):
            add("expired_claim", f"me/{r['page']}", f"'{plain(r['text'], 60)}' is past its "
                f"(until {until}); retire it: keepfold_patch(op='retire', target='^{r['id']}')")
    for text, since in old_fleeting(vault, now):
        add("old_fleeting", f"inbox:{slugify(text)}", f"fleeting since {since}; "
            "process it into a note or drop it")
    totals = Counter(f["kind"] for f in out)
    rank = {k: i for i, k in enumerate(SEVERITY)}
    out.sort(key=lambda f: rank.get(f["kind"], len(SEVERITY)))  # stable: vault order within
    shown, kept = Counter(), []
    for f in out:
        if shown[f["kind"]] < KIND_CAP.get(f["kind"], MAX_FINDINGS):
            shown[f["kind"]] += 1
            kept.append(f)
    return kept[:MAX_FINDINGS], dict(totals)


def _words(text) -> set[str]:
    return {w for w in re.findall(r"\w+", str(text or "").lower()) if w not in STOPWORDS}


def weak_summary(fields: Mapping) -> str:
    """Why a set `summary` helps findability little ('' when it is fine): under
    SUMMARY_MIN_CHARS, or its words and the title's overlap by more than SUMMARY_TITLE_OVERLAP
    (Jaccard, stopwords out), so it says little the title does not."""
    summary = " ".join(str(fields.get("summary") or "").split())
    if not summary:
        return ""
    if len(summary) < SUMMARY_MIN_CHARS:
        return f"summary is under {SUMMARY_MIN_CHARS} characters"
    t, w = _words(fields.get("title")), _words(summary)
    if t and len(t & w) / len(t | w) > SUMMARY_TITLE_OVERLAP:
        return "summary mostly repeats the title"
    return ""


def findability(vault: Vault, embed=None) -> list[dict]:
    """RR-26, report only: `{kind: not_findable, id, detail}` per page (not private or hidden;
    an imported chat ranks as any page here) with a second chunk whose first PROBE_WORDS
    words, as a recall query, do not put it in the top FINDABLE_TOP. No reranker and no graph
    hops; keyword only when `embed` is None. Never writes a page."""
    from keepfold import semantic
    from keepfold.catalog import Catalog
    from keepfold.index import Index

    idx = Index(vault, Catalog.load(vault))
    out = []
    for id, n in sorted(idx._notes.items()):
        chunks = semantic._chunks(n)
        if len(chunks) < 2:
            continue
        probe = " ".join(chunks[1][1].split("\n", 1)[-1].split()[:PROBE_WORDS])  # no header
        hits = semantic.recall(idx, probe, embed, FINDABLE_TOP, hops=0, chats=True)
        if id not in [h["id"] for h in hits[:FINDABLE_TOP]]:
            out.append({"kind": "not_findable", "id": id, "detail": f"'{plain(probe, 60)}' "
                        f"from its second chunk does not find it in the top {FINDABLE_TOP}"})
    return out


def not_shown(findings: list[dict], totals: Mapping[str, int]) -> str:
    """'140 more unlinked_mention, 2 more orphan' for what the cut hid; '' when nothing."""
    shown = Counter(f["kind"] for f in findings)
    return ", ".join(f"{totals[k] - shown[k]} more {k}" for k in SEVERITY
                     if totals.get(k, 0) > shown[k])


def _links_section(body: str) -> list[str]:
    """Non-blank lines under '## Links' up to the next heading; [] when there is none."""
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().lower() == "## links":
            rest = lines[i + 1:]
            end = next((j for j, x in enumerate(rest) if x.startswith("#")), len(rest))
            return [x.strip() for x in rest[:end] if x.strip()]
    return []


def _mention_re(notes, merged: set[str]):
    """(one case-insensitive whole-word regex over every public concept title of 2+ words,
    lowercased title -> id), or None when there are no such titles."""
    titles = {}
    for n in notes:
        title = " ".join(str(n.fields.get("title") or "").split())
        if (n.kind == "concept" and len(title.split()) >= 2 and not is_private(n)
                and n.id not in merged):
            titles.setdefault(title.lower(), n.id)
    if not titles:
        return None
    alt = "|".join(map(re.escape, sorted(titles, key=len, reverse=True)))  # longest first
    return re.compile(rf"(?<!\w)(?:{alt})(?!\w)", re.IGNORECASE), titles


def old_fleeting(vault: Vault, now: dt.datetime) -> list[tuple[str, str]]:
    """(text, date) of inbox.md fleeting lines older than FLEETING_DAYS."""
    inbox = vault.root / "inbox.md"
    if not inbox.is_file():
        return []
    ms = (_FLEETING_RE.match(ln) for ln in inbox.read_bytes().decode("utf-8").splitlines())
    return [(m[1], m[2]) for m in ms
            if m and (now.date() - dt.date.fromisoformat(m[2])).days > FLEETING_DAYS]


def _is_private_id(notes, id: str) -> bool:
    return any(n.id == id and is_private(n) for n in notes)
