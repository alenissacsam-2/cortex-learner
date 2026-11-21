"""In-memory SQLite index with FTS5, nodes and edges, rebuilt from a stat-scan per process.
Unchanged files load from `.cortex/index-snapshot.json` instead of being parsed again.

Backs `cortex_find` and `cortex_read` (docs/tools.md). Notes with `private: true`, and catalog
entries under a private domain or area (`planner.hidden`), never enter the index, so neither
tool can return them.
"""

from __future__ import annotations

import datetime as dt
import functools
import html
import itertools
import json
import logging
import re
import sqlite3
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from cortex.levels import GLYPH
from cortex.vault import HEADING_RE, ID_RE, Note, Vault, VaultError, _section, is_private, warm

log = logging.getLogger(__name__)

GLYPHS = GLYPH  # mcp_server imports this name
NOTE_KINDS = ("concept", "area", "domain", "mistake", "problem", "assessment", "application",
              "resource", "note", "project", "decision", "journal", "loop")
STATE_KINDS = ("due", "weak", "ready")
EDGE_KEYS = ("parent", "areas", "requires", "related", "mistakes", "concepts", "tests", "covers",
             "applies", "decisions", "project", "assessments", "application")
SNAPSHOT = ".cortex/index-snapshot.json"  # parsed notes keyed by (mtime_ns, size); JSON only
SNAPSHOT_SCHEMA = 1
SNAPSHOT_MIN = 50  # a refresh that parsed at least this many files saves the snapshot
MAX_HITS = 8
SNIPPET = 120
READ_CAP_CHARS = 1200 * 4  # ~1.2k tokens at chars/4
TRUNCATED = "\n[truncated]"
WINDOW_MIN = 200  # cortex_read(window=): the smallest window, in characters
OUTLINE = 8  # headings a cut read lists of the rest
WIKILINK_RE = re.compile(r"^\[\[([^\]|#^]+)(?:[|#^][^\]]*)?\]\]$")
LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")  # a [[link]] anywhere in text
FENCE_RE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.M | re.S)

SCHEMA = """
CREATE VIRTUAL TABLE fts USING fts5(id UNINDEXED, kind UNINDEXED, title, aliases, headings,
                                    body, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE nodes(id TEXT PRIMARY KEY, kind TEXT, title TEXT, parent TEXT, domain TEXT);
CREATE TABLE edges(src TEXT, dst TEXT, type TEXT);
CREATE INDEX edges_src ON edges(src);
CREATE INDEX edges_dst ON edges(dst);
"""


def _plain(v):
    """ruamel values to JSON-safe Python values."""
    if isinstance(v, Mapping):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        return float(v)
    return str(v)


def _target(raw) -> str | None:
    """'[[x]]', 'x', 'concept:x', '[[x|label]]', '[[x#heading]]' or '[[x^block]]' to the id
    'x'; None if it is not an id. The one link-target parser (audit F25)."""
    s = str(raw).strip()
    m = WIKILINK_RE.match(s)
    if m:
        s = m.group(1).strip()
    s = s.split(":", 1)[1] if ":" in s else s
    return s if ID_RE.fullmatch(s) else None


def link_targets(text: str) -> list[str]:
    """The ids of the [[links]] in `text`, in order, by `_target`'s rules."""
    return [t for m in LINK_RE.finditer(text) if (t := _target(f"[[{m.group(1)}]]"))]


def _edge_targets(value) -> list[str]:
    items = value.keys() if isinstance(value, Mapping) else \
        value if isinstance(value, (list, tuple)) else [value]
    return [t for t in (_target(x) for x in items if x is not None) if t]


STOPWORDS = frozenset(
    "a an and are as at be but by can do does for from how i in is it its me my of on or so "
    "that the this to was what when where which who why will with you your".split())


_SYNTAX = re.compile(r'"([^"]*)"|(?<!\S)-(\w+)|(\w+)')


def fts_query(text: str, any_word: bool = False, syntax: bool = False) -> str:
    """User text to a safe MATCH string: each word a quoted prefix term, ANDed. `any_word` ORs
    the words and drops stopwords instead. '' if none. With `syntax` (agent search, RR-33) a
    "quoted span" is an exact phrase and a leading -word excludes that word; a query with no
    positive term left, or one FTS5 refuses, falls back to the literal form."""
    words = re.findall(r"\w+", text or "")
    literal = " OR ".join(f'"{t}"*' for t in words if t.lower() not in STOPWORDS) if any_word \
        else " ".join(f'"{t}"*' for t in words)
    if not syntax or not any(c in (text or "") for c in '"-'):
        return literal
    pos, neg = [], []
    for phrase, excl, word in _SYNTAX.findall(text):
        if excl:
            neg.append(f'"{excl}"')
        elif word and not (any_word and word.lower() in STOPWORDS):
            pos.append(f'"{word}"*')
        elif toks := re.findall(r"\w+", phrase):
            pos.append(f'"{" ".join(toks)}"')
    if not pos:
        return literal
    q = (" OR " if any_word else " ").join(pos)
    q = f"({q}) NOT ({' OR '.join(neg)})" if neg else q
    return q if _fts_ok(q) else literal


@functools.lru_cache(maxsize=256)
def _fts_ok(match: str) -> bool:
    """Does FTS5 parse `match`? Checked on an empty in-memory table."""
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        db.execute("SELECT * FROM t WHERE t MATCH ?", (match,)).fetchall()
        return True
    except sqlite3.Error:
        return False
    finally:
        db.close()


# Look-alikes of an `untrusted_note` tag: each char's comparison key (NFKD, casefolded, marks,
# format/zero-width chars, controls, spaces, dashes and underscores dropped, homoglyphs folded)
# is matched after every `<`-like char, so `</ untrusted-n<U+200B><Cyrillic o>te` is a tag too.
# Code points, not literals, so no look-alike hides in this source.
_LT = "".join(map(chr, (0x3C, 0xAB, 0x2C2, 0x1438, 0x2039, 0x2329, 0x226E, 0x276E, 0x27E8,
                        0x3008, 0xFE3F, 0xFE64, 0xFF1C)))  # every char keyed "<" (tested)
_LT_RE = re.compile(f"[{re.escape(_LT)}]")
_FOLD = str.maketrans({c: k for k, cs in {  # Cyrillic, Greek, Armenian, small caps, digits
    "<": tuple(map(ord, _LT)), "/": (0x5C, 0x2044, 0x2215, 0x2571, 0x29F8),
    "a": (0x430,), "c": (0x441,), "d": (0x501, 0x1D05), "e": (0x435, 0x3B5, 0x1D07),
    "i": (0x456, 0x3B9), "k": (0x43A, 0x3BA), "m": (0x43C,),
    "n": (0x43F, 0x3B7, 0x3BD, 0x578, 0x274), "o": (0x43E, 0x3BF, 0x585, 0x1D0F, 0x30),
    "p": (0x440, 0x3C1), "r": (0x433, 0x280), "s": (0x455, 0xA731, 0x35),
    "t": (0x442, 0x3C4, 0x1D1B, 0x37), "u": (0x3C5, 0x3BC, 0x57D, 0x1D1C),
    "x": (0x445, 0x3C7), "y": (0x443,)}.items() for c in cs})
_SKIP_CATS = {"Cc", "Cf", "Mn", "Me", "Pc", "Pd", "Zl", "Zp", "Zs"}
_BLANKS = set(map(chr, (0x115F, 0x1160, 0x2800, 0x3164, 0xFFA0)))  # letters that render blank
_TAGS = ("untrustednote", "/untrustednote")


@functools.lru_cache(maxsize=4096)
def _key(ch: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", ch).casefold().translate(_FOLD)
                   if c not in _BLANKS and unicodedata.category(c) not in _SKIP_CATS)


def _tag_follows(text: str, j: int) -> bool:
    """Do the chars from `j` on spell `/?untrustednote` once compared by `_key`? Stops at the
    first char that cannot continue it, so the scans from each `<` never overlap: linear."""
    s = ""
    while j < len(text):
        s += _key(text[j])
        j += 1
        if s.startswith(_TAGS):
            return True
        if not any(t.startswith(s) for t in _TAGS):
            return False
    return False


def _neutralise(text: str) -> str:
    """Break every look-alike of an opening or closing `untrusted_note` tag: its `<` becomes
    `&lt;`, the rest of the text is left as it is."""
    out, last = [], 0
    for m in _LT_RE.finditer(text):
        if _tag_follows(text, m.end()):
            out += [text[last:m.start()], "&lt;"]
            last = m.end()
    return "".join(out) + text[last:]


def _wrap(id: str, text: str) -> str:
    text = _neutralise(text)
    return f'<untrusted_note id="{html.escape(id, quote=True)}">\n{text}\n</untrusted_note>'


def _preview(body: str) -> str:
    return " ".join(ln for ln in body.splitlines() if not HEADING_RE.match(ln))


def _offset(body: str, at) -> int:
    """RR-12: where `at` starts in a note body: an offset (an int or digits, moved back to
    its line start) or a heading's text (with or without #s, any case)."""
    if isinstance(at, int) or str(at).strip().isdigit():
        off = max(0, min(len(body), int(at)))
        return body.rfind("\n", 0, off) + 1
    want = str(at).lstrip("#").strip().casefold()
    pos = 0
    for line in body.splitlines(keepends=True):
        m = HEADING_RE.match(line.rstrip("\r\n"))
        if m and m.group(2).casefold() == want:
            return pos
        pos += len(line)
    raise VaultError("invalid_argument", f"Heading '{str(at)[:80]}' is not in the note; pass "
                     "an existing heading or an offset.")


def _rest(body: str, cut: int) -> str:
    """RR-12: the end of a read cut at `cut`: how much is left and the rest's headings."""
    heads = [ln.strip() for ln in body[cut:].splitlines() if HEADING_RE.match(ln)]
    more = len(body.rstrip()) - cut
    return (f"\n[truncated: {more:,} more characters"
            + ("; the rest's headings:]\n" + "\n".join(heads[:OUTLINE])
               + (f"\n(+{len(heads) - OUTLINE} more headings)" if len(heads) > OUTLINE else "")
               if heads else "]"))


def _one_line(text: str, n: int = SNIPPET) -> str:
    s = " ".join(text.split())
    return s if len(s) <= n else s[: n - 1] + "…"


class Index:
    def __init__(self, vault: Vault, catalog, states: Mapping[str, Any] | None = None,
                 withhold=None, save: bool = True):
        self.vault = vault
        self.save = save  # False (the app's dry run): never write the snapshot
        # () -> ids kept out like hidden ones (so out of the snapshot too): a non-owner
        # server's me/ pages not shared with work (mcp_server.Cortex._withheld)
        self.withhold = withhold
        self.catalog = catalog
        self.states: Mapping[str, Any] = states or {}
        self.day: str | None = None  # local day `states` are as of, for find(kind="due")
        # the server builds the index in a start-up thread, then uses it on the loop thread only
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.executescript(SCHEMA)
        self._stats: dict = {}  # path -> (mtime_ns, size)
        self._ids: dict = {}  # path -> id it was indexed under
        self._notes: dict[str, dict] = {}  # id -> {kind, title, fields, body}
        self._rowid: dict[str, int] = {}  # id -> fts rowid: a delete by the UNINDEXED id scans
        self._private: set[str] = set()
        self._flagged: set[str] = set()  # domains, areas, concepts with their own private: true
        self._hidden: set[str] = set()  # planner.hidden as of the last refresh
        self.dirty = False  # the snapshot on disk is behind this index
        self._snap = self._load_snapshot()  # relpath -> entry; read by the first refresh only
        self.refresh()

    # ---- building ---------------------------------------------------------

    def _scan(self) -> dict:
        out = {}
        for kind in self.vault.KIND_DIRS:
            for path in self.vault.note_paths(kind):
                if ID_RE.fullmatch(path.stem):
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    out[path] = (kind, (st.st_mtime_ns, st.st_size))
        return out

    def refresh(self) -> int:
        """Re-index files whose (mtime_ns, size) changed, and drop deleted ones. Pages under a
        private domain or area count as private (planner.hidden, the views' rule)."""
        self._parsed = 0
        n = self._refresh()
        if self._snap:  # entries for files that are gone
            self.dirty = True
        self._snap = {}
        if self._parsed >= SNAPSHOT_MIN:
            self.save_snapshot()
        return n

    def _refresh(self) -> int:
        from cortex import planner  # lazy: planner imports catalog, which imports this module

        seen = self._scan()
        n = 0
        fresh = [p for p, (_, st) in seen.items()
                 if self._stats.get(p) != st and self._rel(p) not in self._snap]
        if len(fresh) > 32:  # a build, not a one-file refresh: open them side by side first
            warm(fresh)
        with self.db:
            for path in [p for p in self._stats if p not in seen]:
                self._drop(self._ids.pop(path, None))
                del self._stats[path]
                self.dirty = True
                n += 1
            for path, (kind, stat) in seen.items():
                if self._stats.get(path) == stat:
                    continue
                self._drop(self._ids.pop(path, None))
                self._stats[path] = stat
                n += 1
                note = self._from_snapshot(path, kind, stat)
                if note is None:
                    self._parsed += 1
                    self.dirty = True
                    try:
                        note = self._parse(path, kind)
                    except Exception as e:  # YAML, fence, encoding or containment errors
                        log.warning("not indexing %s: %s", path, e)
                        continue
                self._ids[path] = note.id
                self._add(note)
        # From the flags just indexed, not a second vault walk: that would cost ~3x per refresh.
        hidden = planner.hidden(self.vault, self.catalog, self._flagged) | (
            self.withhold() if self.withhold else set())
        if hidden != self._hidden:  # privacy inherited or lifted: re-add those pages once
            changed = hidden ^ self._hidden
            self._hidden = hidden
            for path, id in self._ids.items():
                if id in changed and id not in self._flagged:  # flagged stay private anyway
                    self._stats.pop(path, None)
            n += self._refresh()
        return n

    # ---- snapshot -----------------------------------------------------------

    @property
    def snapshot_path(self) -> Path:
        return self.vault.root / SNAPSHOT

    @staticmethod
    def _rel(path) -> str:
        # _scan paths are always <root>/<kind folder>/<id>.md; pathlib.relative_to cost ~1 s
        return f"{path.parent.name}/{path.name}"

    def _load_snapshot(self) -> dict:
        p = self.snapshot_path
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_bytes())  # JSON, never pickle: loading runs no vault code
            if data.get("schema") != SNAPSHOT_SCHEMA or not isinstance(data.get("files"), dict):
                raise ValueError(f"schema {data.get('schema')!r}, want {SNAPSHOT_SCHEMA}")
            return data["files"]
        except (OSError, ValueError, AttributeError) as e:
            log.warning("index snapshot ignored, rebuilding it: %s", e)
            self.dirty = True
            return {}

    def _from_snapshot(self, path, kind: str, stat) -> Note | None:
        """The note as the snapshot holds it when its (mtime_ns, size) still match; else None.
        A private entry has no fields or body: it only marks the id private (and flagged)."""
        e = self._snap.pop(self._rel(path), None)
        if not (isinstance(e, list) and len(e) in (5, 6) and e[:2] == list(stat)
                and e[2] == kind and e[3] == path.stem and isinstance(e[4], dict)):
            return None
        if len(e) == 5:
            return Note(path, kind, path.stem, {"private": True}, "") \
                if e[4].get("private") is True else None
        return Note(path, kind, path.stem, e[4], e[5]) if isinstance(e[5], str) else None

    def _snapshot_data(self) -> dict:
        kinds = {d: k for k, d in self.vault.KIND_DIRS.items()}
        files = {}
        for path, id in self._ids.items():
            stat, n = self._stats.get(path), self._notes.get(id)
            # a browse site page sits in browse/sites/<category>/: its kind is the parsed one
            kind = kinds.get(path.parent.name) or (n["kind"] if n else None)
            if stat is None:
                continue
            if n is not None and n["kind"] == kind:
                files[self._rel(path)] = [*stat, kind, id, n["fields"], n["body"]]
            # Own private: true only. Pages private by inheritance (planner.hidden) are left
            # out and reparsed, so lifting the parent's flag brings them back in full.
            elif id in self._private and (id not in self._hidden or id in self._flagged):
                files[self._rel(path)] = [*stat, kind, id,
                                          {"private": True, "flagged": id in self._flagged}]
        return {"schema": SNAPSHOT_SCHEMA, "files": files}

    def save_snapshot(self) -> None:
        """Write the snapshot under the vault lock; a failure only logs (the next build redoes
        the parse)."""
        if not self.save:
            return
        try:
            with self.vault.lock():  # the lock creates .cortex/
                self.vault.write_text(self.snapshot_path,
                                      json.dumps(self._snapshot_data(), ensure_ascii=False))
            self.dirty = False
        except (VaultError, OSError) as e:
            log.warning("index snapshot not saved: %s", e)

    def _parse(self, path, kind: str) -> Note:
        # The vault's cached parse: the planner and levels then reuse these notes for free.
        return self.vault._parse(self.vault._contained(path), kind)

    def _drop(self, id: str | None) -> None:
        if id is None:
            return
        self._private.discard(id)
        self._flagged.discard(id)
        self._notes.pop(id, None)
        if (rowid := self._rowid.pop(id, None)) is not None:
            self.db.execute("DELETE FROM fts WHERE rowid = ?", (rowid,))
        self.db.execute("DELETE FROM nodes WHERE id = ?", (id,))
        self.db.execute("DELETE FROM edges WHERE src = ?", (id,))

    def _add(self, note) -> None:
        # Invariant: ids are unique across kinds (Vault.create_note refuses a clash and
        # `cortex doctor` lists hand-made ones), so keying nodes by id alone is safe.
        if note.id in self._notes or note.id in self._private:  # a fresh build drops nothing
            self._drop(note.id)
        if is_private(note) and note.kind in ("domain", "area", "concept"):
            self._flagged.add(note.id)
        if is_private(note) or note.id in self._hidden:
            self._private.add(note.id)
            return
        f = note.fields
        headings = [m.group(2) for line in note.body.splitlines()
                    if (m := HEADING_RE.match(line))]
        title = str(f.get("title") or (headings[0] if headings else note.id))
        aliases = f.get("aliases") or []
        aliases = [str(a) for a in (aliases if isinstance(aliases, list) else [aliases])]
        entry = getattr(self.catalog, "entries", {}).get(note.id)
        parent = (_edge_targets(f["parent"]) or [None])[0] if f.get("parent") else \
            (entry.parent if entry else None)
        domain = str(f.get("domain") or (entry.domain if entry else "") or "")
        self._notes[note.id] = {"kind": note.kind, "title": title, "fields": _plain(f),
                                "body": note.body}
        self._rowid[note.id] = self.db.execute(
            "INSERT INTO fts VALUES (?, ?, ?, ?, ?, ?)",
            (note.id, note.kind, title, " ".join(aliases), " ".join(headings),
             note.body)).lastrowid
        self.db.execute("INSERT INTO nodes VALUES (?, ?, ?, ?, ?)",
                        (note.id, note.kind, title, parent, domain))
        edges = {(t, key) for key in EDGE_KEYS if f.get(key) is not None
                 for t in _edge_targets(f[key])}
        # Body [[links]] are `mentions` edges for every kind (RETR-6). Journals keep their old
        # rule exactly; other kinds skip code fences and targets a frontmatter edge already has.
        journal = note.kind == "journal"
        body = note.body if journal else FENCE_RE.sub("", note.body)
        typed = set() if journal else {t for t, _ in edges}
        edges |= {(t, "mentions") for t in link_targets(body) if t not in typed}
        self.db.executemany("INSERT INTO edges VALUES (?, ?, ?)",
                            [(note.id, t, key) for t, key in sorted(edges)])

    # ---- states -----------------------------------------------------------

    def _state(self, id: str) -> str:
        return getattr(self.states.get(id), "state", None) or "not_started"

    def _hit(self, id: str, kind: str, title: str, snippet: str) -> dict:
        state = self._state(id) if kind == "concept" else None
        summary = self._notes[id]["fields"].get("summary")
        return {"id": id, "kind": kind, "title": title,
                **({"summary": _one_line(str(summary))} if summary else {}),
                "state": state, "glyph": GLYPH.get(state, ""), "snippet": _one_line(snippet)}

    def _state_ids(self, kind: str) -> list[str]:
        concepts = [r[0] for r in self.db.execute(
            "SELECT id FROM nodes WHERE kind = 'concept' ORDER BY id")]
        from cortex import planner  # lazy: planner imports catalog, which imports this module
        if kind == "due":
            from cortex.events import local_day
            day = self.day or local_day(dt.datetime.now(dt.UTC))
            due = [c for c in concepts if planner.is_due(self.states.get(c), day)]
            nd = {c: getattr(self.states.get(c), "next_due", None) for c in due}
            return sorted(due, key=lambda c: (nd[c] is None, str(nd[c] or "")))
        if kind == "weak":
            return [c for c in concepts if self._state(c) == "learning"
                    or getattr(self.states.get(c), "open_mistakes", None)]

        def requires(c):
            return SimpleNamespace(requires=[r[0] for r in self.db.execute(
                "SELECT dst FROM edges WHERE src = ? AND type = 'requires'", (c,))])

        return [c for c in concepts
                if planner.is_ready(self._state(c), requires(c), self.states)]

    # ---- find -------------------------------------------------------------

    def find(self, query: str = "", kind: str | None = None, limit: int = MAX_HITS,
             any_word: bool = False, keep=None,
             syntax: bool = False) -> list[dict]:
        """FTS hits, or a state list; `keep(id)` (a date or source filter) is applied before
        the limit."""
        if kind is not None and kind not in NOTE_KINDS + STATE_KINDS:
            raise VaultError("invalid_argument", f"kind must be one of "
                             f"{', '.join(NOTE_KINDS + STATE_KINDS)}.")
        try:
            limit = max(1, min(MAX_HITS, int(limit)))
        except (TypeError, ValueError):
            limit = MAX_HITS
        match = fts_query(query, any_word, syntax)
        if kind in STATE_KINDS:
            ids = self._state_ids(kind)
            if match:
                ranked = {r[0]: r[1] for r in self.db.execute(
                    "SELECT id, snippet(fts, 5, '', '', '…', 16) FROM fts WHERE fts MATCH ? "
                    "AND kind = 'concept'", (match,))}
                ids = [i for i in ids if i in ranked]
            ids = [i for i in ids if keep is None or keep(i)]
            hits = []
            for i in ids[:limit]:
                n = self._notes[i]
                hits.append(self._hit(i, n["kind"], n["title"],
                                      ranked[i] if match else _preview(n["body"])))
            return hits
        if not match:
            return []
        sql = ("SELECT id, kind, title, snippet(fts, 5, '', '', '…', 16) FROM fts "
               "WHERE fts MATCH ?" + (" AND kind = ?" if kind else "") +
               " ORDER BY bm25(fts, 0, 0, 10, 8, 3, 1)")
        if keep is None:
            sql += " LIMIT ?"
        args = (match, kind) if kind else (match,)
        rows = self.db.execute(sql, (*args, limit) if keep is None else args)
        return [self._hit(*row) for row in itertools.islice(
            (r for r in rows if keep is None or keep(r[0])), limit)]

    def problems(self, query: str = "", limit: int = MAX_HITS, syntax: bool = False
                 ) -> list[dict]:
        """Problems, todo first, then attempted, then solved. A query keeps the problems whose
        own text matches or that link a concept (`concepts:`) whose title or id matches."""
        ids = [i for i, n in self._notes.items() if n["kind"] == "problem"]
        if match := fts_query(query, syntax=syntax):
            hit = {r[0] for r in self.db.execute(
                "SELECT id FROM fts WHERE fts MATCH ? AND kind = 'problem'", (match,))}
            hit |= {r[0] for r in self.db.execute(  # concepts by title, not body
                "SELECT id FROM fts WHERE fts MATCH ? AND kind = 'concept'",
                (f"{{title aliases}} : ({match})",))}
            words = [w.lower() for w in re.findall(r"\w+", query)]
            ids = [i for i in ids if i in hit or any(
                t in hit or all(w in t for w in words) for (t,) in self.db.execute(
                    "SELECT dst FROM edges WHERE src = ? AND type = 'concepts'", (i,)))]
        rank = {"todo": 0, "attempted": 1, "solved": 2}
        ids.sort(key=lambda i: (rank.get(self._notes[i]["fields"].get("status"), 1), i))
        return [{**self._hit(i, "problem", self._notes[i]["title"], ""),
                 "status": self._notes[i]["fields"].get("status")} for i in ids[:limit]]

    def title_matches(self, title: str, kind: str, limit: int = 3) -> list[str]:
        """Ids of `kind` whose title or aliases hold every word of `title` (prefix, any case);
        bodies are not searched, so a note that merely mentions the words is no match."""
        match = fts_query(title)
        if not match:
            return []
        return [r[0] for r in self.db.execute(
            "SELECT id FROM fts WHERE fts MATCH ? AND kind = ? "
            "ORDER BY bm25(fts, 0, 0, 10, 8, 3, 1) LIMIT ?",
            (f"{{title aliases}} : ({match})", kind, limit))]

    def related(self, query: str, limit: int = 5) -> list[dict]:
        """FTS hits on `query`, then one graph hop around the top three; any kind, no private."""
        hits = self.find(query, None, MAX_HITS)
        seen = {h["id"] for h in hits}
        for h in hits[:3]:
            for (i,) in self.db.execute(
                    "SELECT dst FROM edges WHERE src = ?1 UNION SELECT src FROM edges "
                    "WHERE dst = ?1", (h["id"],)):
                if i not in seen and i in self._notes:
                    seen.add(i)
                    n = self._notes[i]
                    hits.append(self._hit(i, n["kind"], n["title"], ""))
        return hits[:limit]

    # ---- read -------------------------------------------------------------

    def _resolve(self, id: str) -> str:
        if id in self._notes:
            return id
        kinds = ("domain", "area", "concept")
        try:
            r = self.catalog.resolve(id, kinds)
            if r in self._notes:
                return r
        except VaultError:
            pass
        raise VaultError("unknown_id", f"No note '{id}'; check the id with find.",
                         [s for s in self.catalog.suggest(id, kinds) if s in self._notes])

    def _ids_where(self, sql: str, *args) -> list[str]:
        return [r[0] for r in self.db.execute(sql, args) if r[0] not in self._private]

    def linked_mistakes(self, id: str) -> list[str]:
        """Ids the note's `mistakes:` names, then mistake notes whose `concepts:` name it."""
        mistakes = self._ids_where("SELECT dst FROM edges WHERE src = ? AND type = ?",
                                   id, "mistakes")
        return mistakes + [m for m in self._ids_where(
            "SELECT e.src FROM edges e JOIN nodes n ON n.id = e.src "
            "WHERE e.dst = ? AND e.type = ? AND n.kind = ?", id, "concepts", "mistake")
            if m not in mistakes]

    def read(self, id: str, sections: list[str] | None = None, at: int | str | None = None,
             window: int | None = None) -> dict:
        """A note within READ_CAP_CHARS. `at` (an offset in the body, or a heading) and
        `window` (characters) read a part of it (RR-12). A read cut short ends with the rest's
        headings and gives `next_at`, the offset to read on from (not for `sections`)."""
        id = self._resolve(str(id))
        n = self._notes[id]
        body = n["body"]
        end = len(body.rstrip())
        lo = None  # where the shown text starts in the body; None for sections
        if sections:
            if at is not None or window is not None:
                raise VaultError("invalid_argument", "Pass sections, or at/window, not both.")
            lines = body.splitlines(keepends=True)
            parts = []
            for h in sections:
                start, stop = _section(lines, h)
                parts.append("".join(lines[start - 1 : stop]).rstrip())
            text = "\n\n".join(parts)
        else:
            lo = _offset(body, at) if at is not None else len(body) - len(body.lstrip())
            hi = end
            if window is not None:
                try:
                    size = max(WINDOW_MIN, min(READ_CAP_CHARS, int(window)))
                except (TypeError, ValueError):
                    raise VaultError("invalid_argument", "window must be a number of "
                                     "characters.") from None
                if lo + size < end:  # end at a line end when one is in the window's second half
                    nl = body.rfind("\n", lo + size // 2, lo + size)
                    hi = nl + 1 if nl > 0 else lo + size  # the newline too: next_at starts a line
            text = body[lo:hi]
        # shown first, so the cap never cuts it; a later window of the page goes without
        head = f"Summary: {n['fields']['summary']}\n\n" if n["fields"].get("summary") \
            and at is None else ""
        out_of = "SELECT dst FROM edges WHERE src = ? AND type = ?"
        into = "SELECT e.src FROM edges e JOIN nodes n ON n.id = e.src " \
               "WHERE e.dst = ? AND e.type = ? AND n.kind = ?"
        parent = self.db.execute("SELECT parent FROM nodes WHERE id = ?", (id,)).fetchone()[0]
        mistakes = self.linked_mistakes(id)
        st = self.states.get(id)
        next_due = getattr(st, "next_due", None)
        result = {
            "id": id, "kind": n["kind"], "title": n["title"],
            "state": self._state(id) if n["kind"] == "concept" else None,
            "fields": {k: v for k, v in n["fields"].items() if not k.startswith("cortex_")},
            "text": "",
            "neighbours": {
                "parent": parent if parent not in self._private else None,
                "requires": self._ids_where(out_of, id, "requires"),
                "required_by": self._ids_where(into, id, "requires", "concept"),
                "mistakes": mistakes,
                "problems": self._ids_where(into, id, "concepts", "problem"),
            },
            "open_mistakes": [str(m) for m in getattr(st, "open_mistakes", None) or []],
            "next_refresh": _plain(next_due) if next_due is not None else None,
        }
        def show(shown: str, cut: bool) -> int:
            """Set the text and `next_at`; returns the JSON size over the cap."""
            nxt = None if lo is None or not (cut or lo + len(shown) < end) else lo + len(shown)
            rest = _rest(body, nxt) if nxt is not None else TRUNCATED if cut else ""
            result["text"] = _wrap(id, head + shown.rstrip() + rest)
            result.pop("next_at", None)
            if nxt is not None:
                result["next_at"] = nxt
            return len(json.dumps(result, ensure_ascii=False)) - READ_CAP_CHARS

        over = show(text, False)
        while over > 0 and text:
            # JSON escapes make the size non-linear in the cut, so trim until it fits, at a
            # line end when one is in the kept half (the next read starts on a whole line);
            # halved while far over, since escapes make `over` count more than the text holds
            text = text[: len(text) // 2 if over > len(text) // 2
                        else max(0, len(text) - over - len(TRUNCATED))]
            nl = text.rfind("\n")
            text = text[:nl + 1] if nl > len(text) // 2 else text
            over = show(text, True)
        return result
