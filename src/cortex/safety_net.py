"""The chat safety net (`cortex safety-net`, the app after each chat import): record what a chat
model forgot to log, from the chat pages already in the vault (chat_import).

A pass looks at chat pages (semantic.is_chat with `**Me:**` turns; coding summaries hold no
teaching and are left out) that are new or changed since the last pass. STATE (JSON under
`.cortex/`, a git-ignored cache) keeps per resource page its (mtime_ns, size), the body hash
(chat_import._hash) and what was done, so a pass with nothing new only stats files.

A chat is skipped when:
- it is private (`private: true`, or its category is not "cloud" in the browse privacy
  settings, as chat_import judges it); private chats never touch or create public pages;
- its last turn is older than RECENT_DAYS (a first pass must not mark old chats taught today);
- the chat model already recorded learning for it: any live `ask`, `grade` or `taught` event,
  or a `touch`, `taught` or `self_report` log, not written by this module or an import, whose
  `ts` falls between the chat's first turn less BEFORE and its last turn plus AFTER (turn times
  are the page's `### YYYY-MM-DD HH:MM` lines); a page with no turn times uses its local day.
  Live MCP logging carries no chat id, so the time span is the robust link; a learning event
  from another chat in the same span also skips it (the safe side);
- one of its names matches a hidden concept (planner.hidden): the chat is about a private
  subject, so nothing public is written from it.

Layer 1 (code only, always): concept titles and aliases (catalog) and topic hub titles and
aliases (the index) found as whole words in the user's turns, plus the hub `topics.attach`
picks for the chat's vector (FLOOR) when an embedder is already in memory. The new ids become one
`touch` log (no object: levels ignore it), each page at most once per chat.

Layer 2 (only with a runner whose model is already loaded; the app also needs the PC idle,
browse.titles.may_run): the model names at most PER_CHAT concepts the chat explained (JSON,
strict parse; junk, generic words and secrets refused, and a word of the name must occur in
the chat). Each resolves to an existing concept (catalog id, title, alias or slug; then an
embedding cosine >= MATCH_COS to a concept page; then the catalog's near-duplicate "did you
mean") or is created like cortex_add (Catalog.add) under the area most of the chat's matched
concepts share, else under DOMAIN > AREA, and linked (`related`) to the hub `topics.attach`
picks for its name. Each gets one `taught` event per chat.

Every event this writes has source SOURCE (server-stamped, never model output), session
`import` and payload {"auto": SOURCE, "chat": <page id>}. Caps: MAX_CHATS chats and MAX_EVENTS
events per pass. A pass that wrote something adds one journal line (the home feed). `undo`
voids the events (all, or one chat's); `cortex void <event id>` voids one. Concepts it created
stay; merge or delete them by hand (`cortex concept merge`).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cortex import chat_import, localllm, planner, topics
from cortex.catalog import Catalog, slugify
from cortex.events import EventLog, live, local_day, parse_ts, void_event
from cortex.index import STOPWORDS, Index
from cortex.vault import Vault, VaultError, is_private, secret_pattern

log = logging.getLogger(__name__)

STATE, LOCK = ".cortex/safety-net.json", ".cortex/safety-net.lock"
BUSY = "A safety-net pass is already running."
SOURCE = "safety_net"
MAX_CHATS, MAX_EVENTS, PER_CHAT = 10, 15, 3  # per pass, per pass, model concepts per chat
TOUCHES = 5  # ids in one touch event, as cortex_log's TOUCH_MAX
RECENT_DAYS = 7
BEFORE, AFTER = timedelta(minutes=2), timedelta(minutes=30)  # around the chat's turn times
# bge-small cosine of a short concept name to a concept page's head: near-synonyms score
# 0.85-0.95, related but different concepts 0.70-0.82
MATCH_COS = 0.85
EMBED_CHARS, MODEL_CHARS = 1500, 3000
NAME_CHARS, NAME_WORDS, MIN_NAME = 60, 6, 4
DOMAIN, AREA = "From chats", "Unsorted"  # where a new concept goes when the chat has no area
LEARNING = ("ask", "grade", "taught")  # event kinds
LEARNING_LOGS = ("touch", "taught", "self_report")  # log payload kinds
GENERIC = frozenset(
    "concept concepts topic topics idea ideas programming code coding example examples question "
    "questions answer answers help thing things data function functions learning study studying "
    "chat explanation overview basics basic introduction intro general misc other none unknown "
    "test tests problem problems solution solutions method methods approach way ways".split())
_AT = re.compile(r"^### (\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*$", re.M)
_BAD = re.compile(r"[\[\]{}<>|\\/`#*\n]|https?:|www\.", re.I)
SYSTEM = ("You read the user's side of a study chat with an AI tutor. Name the specific "
          f"concepts the tutor explained: at most {PER_CHAT}, each 1 to 5 words, as a textbook "
          'would title it (for example "Binary search" or "Two pointers"). Never generic words '
          'such as "programming" or "help". If nothing was taught, give an empty list. Reply '
          'with JSON only: {"concepts": ["..."]}')


@dataclass
class Chat:
    id: str
    title: str
    turns: list[str]
    start: datetime | None  # UTC, from the turn headings
    end: datetime | None
    day: str

    @property
    def text(self) -> str:
        return "\n".join([self.title, *self.turns])

    def last_day(self) -> str:
        return local_day(self.end) if self.end else self.day


def chat_of(note) -> Chat | None:
    """The chat on an ordinary chat page (the user's turns), else None."""
    body = note.body
    if note.kind != "resource" or not topics.page_source(body):
        return None
    turns = [t.strip() for t in chat_import._ME.findall(body) if t.strip()]
    if not turns:
        return None  # a coding summary, or no turns
    times = []
    for s in _AT.findall(body):
        try:
            times.append(datetime.strptime(s, "%Y-%m-%d %H:%M").astimezone().astimezone(UTC))
        except ValueError:
            continue
    m = chat_import._DATE.search(body)
    day = m[1] if m else str(note.fields.get("date") or "")[:10]
    if not day and times:
        day = local_day(min(times))
    title = str(note.fields.get("title") or note.id)
    return Chat(note.id, title, turns, min(times, default=None), max(times, default=None), day)


def recorded(chat: Chat, learning: list[dict]) -> bool:
    """Did a chat model record learning during `chat` (the module docstring's rule)?"""
    for e in learning:
        if chat.start is not None:
            try:
                t = parse_ts(e["ts"])
            except (KeyError, TypeError, ValueError):
                continue
            if chat.start - BEFORE <= t <= chat.end + AFTER:
                return True
        elif e.get("day") == chat.day:
            return True
    return False


def _learning(events) -> list[dict]:
    out = []
    for e in live(events):
        if e.get("source") in (SOURCE, "imported"):
            continue
        p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        if e.get("kind") in LEARNING or (e.get("kind") == "log" and p.get("kind") in LEARNING_LOGS):
            out.append(e)
    return out


def _usable(name: str) -> bool:
    words = name.casefold().split()
    return len(name) >= MIN_NAME and not all(w in STOPWORDS or w in GENERIC for w in words)


def _pattern(names) -> re.Pattern | None:
    alts = sorted({r"\s+".join(map(re.escape, n.split())) for n in names if _usable(n)},
                  key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)", re.I) if alts else None


def clean_name(raw, text: str) -> str | None:
    """A model's concept name, tidied, or None when it is junk: too short or long, generic,
    markup or a link, a secret, or none of its longer words occurs in the chat `text`."""
    if not isinstance(raw, str):
        return None
    name = " ".join(raw.split()).strip(" .,;:-'\"")
    if not name or len(name) > NAME_CHARS or len(name.split()) > NAME_WORDS or _BAD.search(raw) \
            or not re.search(r"[^\W\d_]", name) or not _usable(name) or secret_pattern(name):
        return None
    low = text.casefold()
    words = [w for w in re.findall(r"\w+", name.casefold()) if len(w) >= 4 and w not in GENERIC]
    return name if any(re.search(rf"(?<!\w){re.escape(w)}", low) for w in words) else None


def ask_model(runner, chat: Chat) -> list[str]:
    """At most PER_CHAT clean concept names from the runner; [] on any malformed reply."""
    raw = runner.chat(SYSTEM, chat.text[:MODEL_CHARS], max_tokens=200)
    obj = localllm._first_json(raw) if isinstance(raw, str) else None
    items = obj.get("concepts") if isinstance(obj, dict) else None
    if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
        return []
    out: list[str] = []
    for x in items:
        n = clean_name(x, chat.text)
        if n and n.casefold() not in {o.casefold() for o in out}:
            out.append(n)
    return out[:PER_CHAT]


# ---- the pass ---------------------------------------------------------------------------------

def _load(vault: Vault) -> dict:
    try:
        d = json.loads((vault.root / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _todo(vault: Vault, ledger: dict, model: bool) -> list:
    """(path, sig) of resource pages new or changed since the ledger, plus (with a model)
    chats layer 2 has not seen yet. Stats only."""
    out = []
    for p in vault.note_paths("resource"):
        try:
            st = p.stat()
        except OSError:
            continue
        sig = [st.st_mtime_ns, st.st_size]
        rec = ledger.get(p.stem)
        if not isinstance(rec, dict) or rec.get("sig") != sig or (model and rec.get("l2") is False):
            out.append((p, sig))
    return out


class _Pass:
    def __init__(self, vault: Vault, runner, embed, dry_run: bool, now: datetime):
        self.vault, self.runner, self.embed, self.dry, self.now = vault, runner, embed, dry_run, now
        self.evlog = EventLog(vault)
        self.catalog = Catalog.load(vault)
        self.index = Index(vault, self.catalog)
        self.hide = planner.hidden(vault, self.catalog)
        self.learning = _learning(self.evlog.iter_events())
        self.ctx = chat_import.Ctx(vault)
        self.counts: Counter = Counter()
        self.new_ids: list[str] = []
        self.taught_ids: list[str] = []
        self.events = 0
        self._names()
        self._vectors()

    def _names(self) -> None:
        cat, notes = self.catalog.entries, self.index._notes
        self.tree = topics.tree(self.index)
        self.by_name: dict[str, str] = {}  # casefolded name -> page id
        hidden: list[str] = []
        for e in cat.values():
            if e.kind != "concept":
                continue
            names = [e.title, *e.aliases]
            if e.id in self.hide:
                hidden += names
            elif e.id in notes:
                for n in names:
                    self.by_name.setdefault(" ".join(n.casefold().split()), e.id)
        for h, title in self.tree.title.items():
            if h == topics.ROOT or h not in notes:
                continue
            al = notes[h]["fields"].get("aliases")
            for n in [title, *(al if isinstance(al, list) else [al] if al else [])]:
                self.by_name.setdefault(" ".join(str(n).casefold().split()), h)
        self.public = _pattern(self.by_name)
        self.hidden = _pattern(hidden)

    def _vectors(self) -> None:
        self.cents = self.heads = None
        if self.embed is None:
            return
        from cortex import semantic
        stack = topics.head_stack(self.index, self.embed)
        self.cents = topics.centroids(self.index, self.tree, stack)
        hv = topics.heads(stack)
        ids = [c for c, e in self.catalog.entries.items()
               if e.kind == "concept" and c in hv and c not in self.hide]
        self.heads = (ids, [hv[c] for c in ids]) if ids else None
        self._embed = lambda t: semantic._embed(self.embed, [t])[0]

    # ---- one chat ----

    def skip(self, note, chat: Chat) -> str | None:
        if is_private(note) or self.ctx.audience(chat.text) != "cloud":
            return "private"
        if chat.last_day() < (self.now - timedelta(days=RECENT_DAYS)).date().isoformat():
            return "old"
        if self.hidden is not None and self.hidden.search(chat.text):
            return "hidden"
        if recorded(chat, self.learning):
            return "recorded"
        return None

    def layer1(self, chat: Chat) -> list[str]:
        ids = list(dict.fromkeys(self.by_name[" ".join(m[0].casefold().split())]
                                 for m in (self.public.finditer(chat.text) if self.public else ())))
        if self.cents is not None:
            hub = topics.attach(self._embed(chat.text[:EMBED_CHARS]), self.tree, self.cents).hub
            if hub and hub not in ids:
                ids.append(hub)
        return ids

    def _append(self, kind: str, fields: dict, key: str) -> None:
        if not self.dry:
            self.evlog.append(session="import", kind=kind, source=SOURCE, fields=fields, key=key,
                              now=self.now)
        self.events += 1

    def touch(self, chat: Chat, h: str, ids: list[str], rec: dict) -> None:
        new = [i for i in ids if i not in rec["touched"]][:TOUCHES]
        if not new:
            return
        self._append("log", {"payload": {"kind": "touch", "ids": new, "auto": SOURCE,
                                         "chat": chat.id}}, f"safety-net:{chat.id}:{h}:touch")
        rec["touched"] += new
        self.counts["touches"] += len(new)

    def resolve(self, name: str) -> tuple[str, str | None]:
        """("match", id), ("new", None) or ("drop", None) for a model's concept name."""
        try:
            cid = self.catalog.resolve(name)  # no skip: a hidden hit is dropped, never copied
            return ("drop", None) if cid in self.hide else ("match", cid)
        except VaultError as e:
            if e.code != "unknown_concept":
                raise
        if self.heads is not None:
            import numpy as np
            ids, vs = self.heads
            cos = np.asarray(vs) @ self._embed(name)
            k = int(np.argmax(cos))
            if float(cos[k]) >= MATCH_COS:
                return "match", ids[k]
        return "new", None

    def area(self, matched: list[str]) -> str:
        """The area most of the chat's matched concepts share, else DOMAIN > AREA (made once)."""
        areas = Counter(e.parent for c in matched if (e := self.catalog.entries.get(c))
                        and e.kind == "concept" and e.parent and e.parent not in self.hide)
        if areas:
            return areas.most_common(1)[0][0]
        cat = self.catalog
        did = slugify(DOMAIN)
        if getattr(cat.entries.get(did), "kind", None) != "domain":
            did = cat.add("domain", DOMAIN, force=True)
        aid = f"{did}.{slugify(AREA)}"
        if aid not in cat.entries:
            aid = cat.add("area", AREA, did, force=True)
        return aid

    def create(self, name: str, matched: list[str]) -> tuple[str | None, bool]:
        """(concept id, created) for a name no concept matched; (None, False) when refused."""
        try:
            cid = self.catalog.add("concept", name, self.area(matched))
        except VaultError as e:
            dups = e.did_you_mean or []
            if e.code == "duplicate_suggested":  # cortex_add's "did you mean": the first one
                ok = dups and not any(d in self.hide for d in dups)  # never near a hidden one
                return (dups[0], False) if ok else (None, False)
            if e.code in ("invalid_argument", "invalid_id", "secret_detected"):
                return None, False
            raise
        if self.cents is not None and (hub := topics.attach(
                self._embed(name), self.tree, self.cents).hub):
            self.vault._edit_fields("concept", cid, {"related": [f"[[{hub}]]"]})
        return cid, True

    def layer2(self, chat: Chat, matched: list[str], rec: dict) -> None:
        for name in ask_model(self.runner, chat):
            what, cid = self.resolve(name)
            if what == "drop":
                continue
            made = False
            if what == "new":
                if self.dry:
                    self.counts["new"] += 1
                    self.new_ids.append(slugify(name))
                    continue
                cid, made = self.create(name, matched)
                if cid is None:
                    continue
            if cid in rec["taught"]:
                continue
            if made:
                self.counts["new"] += 1
                self.new_ids.append(cid)
                self.index.refresh()
            matched.append(cid)
            self._append("taught", {"object": f"concept:{cid}", "payload": {
                "kind": "taught", "auto": SOURCE, "chat": chat.id}},
                f"safety-net:{chat.id}:taught:{cid}")
            rec["taught"].append(cid)
            self.counts["taught"] += 1
            self.taught_ids.append(cid)

    def one(self, note, chat: Chat, h: str, rec: dict) -> dict:
        if why := self.skip(note, chat):
            self.counts[f"skipped_{why}"] += 1
            return {**rec, "what": why, "l2": None}
        rec = {"touched": [], "taught": [], **rec, "what": "done"}
        self.counts["chats"] += 1
        ids = self.layer1(chat)
        self.touch(chat, h, ids, rec)
        if self.runner is not None:
            self.layer2(chat, [i for i in ids if i in self.catalog.entries], rec)
        rec["l2"] = self.runner is not None
        return rec

    def line(self) -> str:
        n = self.counts
        s = (f"Safety net: from {n['chats']} chat{'s' * (n['chats'] != 1)}, {n['new']} new "
             f"concept{'s' * (n['new'] != 1)}, {n['taught']} taught, {n['touches']} "
             f"touch{'es' * (n['touches'] != 1)}")
        if self.new_ids:
            s += " (new: " + ", ".join(f"[[{i}]]" for i in self.new_ids) + ")"
        rest = [i for i in self.taught_ids if i not in self.new_ids]
        if rest:
            s += " (taught: " + ", ".join(f"[[{i}]]" for i in rest) + ")"
        return s


def run(vault: Vault, runner=None, embed=None, *, dry_run: bool = False,
        now: datetime | None = None) -> dict:
    """One pass. `runner`: a local runner whose model is already loaded, or None (layer 2 is
    skipped); `embed`: an embedder already in memory, or None. Returns counts (chats, touches,
    taught, new, skipped_<why>), `new_ids`, `taught_ids` and `line` when anything was recorded.
    VaultError("busy") while another pass runs."""
    now = now or datetime.now(UTC)
    ledger = _load(vault)
    todo = _todo(vault, ledger, runner is not None)
    if not todo:
        return {}
    lock = nullcontext() if dry_run else vault.job_lock(LOCK, 0, BUSY)
    with lock:
        p, skipped, work = None, Counter(), []
        for path, sig in todo:
            try:
                note = vault.read("resource", path.stem)
            except VaultError:
                continue
            h = chat_import._hash(note.body)
            rec = ledger.get(path.stem) if isinstance(ledger.get(path.stem), dict) else {}
            if rec.get("hash") == h and not (runner is not None and rec.get("l2") is False):
                ledger[path.stem] = {**rec, "sig": sig}  # only frontmatter or Topics: changed
            elif (chat := chat_of(note)) is None:  # most resources: no index or catalog needed
                skipped["skipped_not_chat"] += 1
                ledger[path.stem] = {"what": "not_chat", "sig": sig, "hash": h}
            else:
                work.append((note, chat, sig, h, rec if rec.get("hash") == h else
                                {k: rec[k] for k in ("touched", "taught") if k in rec}))
        work.sort(key=lambda x: (x[1].last_day(), x[0].id), reverse=True)  # newest first
        try:
            for note, chat, sig, h, rec in work:
                p = p or _Pass(vault, runner, embed, dry_run, now)
                if p.skip(note, chat) is None and (
                        p.counts["chats"] >= MAX_CHATS or p.events + 1 + PER_CHAT > MAX_EVENTS):
                    p.counts["left"] += 1  # next pass
                    continue
                ledger[note.id] = {**p.one(note, chat, h, rec), "sig": sig, "hash": h}
        finally:
            if not dry_run:
                vault.write_text(vault.root / STATE, json.dumps(ledger, sort_keys=True))
        out: dict = dict(skipped + (p.counts if p else Counter()))
        if p is not None and p.events:
            out.update(new_ids=p.new_ids, taught_ids=p.taught_ids, line=p.line())
            if not dry_run:
                at = now.astimezone()
                vault.append_journal(local_day(now), at.strftime("%H:%M"), p.line())
        return out


def pending(vault: Vault) -> bool:
    """Is there a page for a pass to look at (stats only)? The app asks before it probes
    for a loaded model."""
    return bool(_todo(vault, _load(vault), True))

def undo(vault: Vault, chat: str | None = None, now: datetime | None = None) -> int:
    """Void every live safety-net event (only `chat`'s with a page id); returns how many. The
    ledger keeps those chats done, so the next pass does not record them again."""
    evlog = EventLog(vault)
    n = 0
    for e in live(evlog.iter_events()):
        p = e.get("payload")
        if e.get("source") == SOURCE and isinstance(p, dict) and p.get("auto") == SOURCE \
                and (chat is None or p.get("chat") == chat):
            void_event(evlog, session="cli", event_id=e["id"], reason="safety net undo",
                       source="external", now=now)
            n += 1
    return n


def background_tools():
    """(runner, embed) for the app: the runner only when the PC is idle and the model is
    already loaded (browse.titles.may_run), the embedder only when already in memory."""
    from cortex import semantic
    from cortex.browse import titles
    return titles.may_run()[0], semantic._embedder


def cli_tools():
    """(runner, embed) for `cortex safety-net`: an already-loaded model (no idle wait: the
    owner ran it) and the downloaded embedder, if any; nothing is downloaded or loaded."""
    from cortex import semantic
    try:
        runner = localllm.find_loaded()
    except ValueError:
        runner = None
    return runner, semantic.load_embedder()
