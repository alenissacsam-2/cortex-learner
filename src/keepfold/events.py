"""Append-only evidence log: events/YYYY/MM/<session>.jsonl (docs/tools.md "Event").

Server-stamped fields never come from `fields`; reads skip torn or invalid lines.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING

from keepfold.vault import ID_RE, Vault, VaultError, check_day, invalid, scan_secrets

if TYPE_CHECKING:
    from ulid import ULID

log = logging.getLogger(__name__)

SOURCES = {"tutor", "self", "external", "imported", "run", "safety_net"}
KINDS = {"ask", "grade", "log", "taught", "void", "remap", "resume", "close", "open",
         "change"}
MODES = {"home", "learn", "short", "review", "log", "about", "week", "ambient", "capture",
         "reflect",
         "project", "save", "history", "sweep", "mock",
         "tidy",  # tidy: the app's sweep of about-me lines (Keepfold.tidy), never a study mode
         "harvest"}  # harvest: facts from imported chats saved as guesses (fact_harvest)
STUDY_MODES = frozenset({"home", "learn", "short", "review", "log", "reflect",
                         "sweep"})  # not ambient

_ID = ID_RE.pattern[1:-1]
OBJECT_RE = re.compile(
    rf"^(concept|problem|assessment|application|mistake|inbox|area|domain):{_ID}$")
SESSION_RE = re.compile(r"^(s_[0-9A-HJKMNP-TV-Z]{26}|cli|import)$")

SERVER_FIELDS = {"id", "key", "ts", "day", "session", "kind", "mode", "source",
                 "predicted_p", "grader", "schema"}
TEXT_LIMIT = 2000
TEXT_LIMITS = {"question": TEXT_LIMIT, "answer_key": TEXT_LIMIT, "learner_answer": TEXT_LIMIT,
               "ref": 300,
               "summary": 600}
FIELD_ENUMS = {
    "item_type": {"recall", "application", "transfer"},
    "verdict": {"correct", "partial", "wrong"},
    "model_verdict": {"correct", "partial", "wrong"},
}
INT_RANGES = {"confidence": (1, 4), "hints": (0, 3)}
ID_LISTS = {"mistakes", "targets_mistakes"}
CLIENT_FIELDS = (set(TEXT_LIMITS) | set(FIELD_ENUMS) | set(INT_RANGES) | ID_LISTS
                 | {"object", "problem", "also", "payload"})
DAY_ROLLOVER = timedelta(hours=4)


def local_day(ts_utc: datetime, tz: tzinfo | None = None) -> str:
    """Local date of `ts_utc`, rolling over at 04:00. `tz` defaults to the system zone."""
    if ts_utc.tzinfo is None:
        ts_utc = ts_utc.replace(tzinfo=UTC)
    return (ts_utc.astimezone(tz) - DAY_ROLLOVER).date().isoformat()


def parse_ts(ts: str) -> datetime:
    """An event's `ts` (UTC, whole seconds) as an aware datetime."""
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


_BIG_O = re.compile(r"o\(([^)]*)\)")


def norm_answer(s: str) -> str:
    """Lowercase, no backticks or `$`, collapsed whitespace, Big-O spaces removed: the
    server's answer_key match (§15a.11)."""
    s = " ".join(s.lower().replace("`", "").replace("$", "").split())
    return _BIG_O.sub(lambda m: "o(" + re.sub(r"\s+", "", m.group(1)) + ")", s)


def live(events) -> list[dict]:
    """The non-void events whose id no void references."""
    evs = list(events)
    voided = {e.get("ref") for e in evs if e.get("kind") == "void"}
    return [e for e in evs if e.get("kind") != "void" and e.get("id") not in voided]


def _strings(value) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _check_text(name: str, value, limit: int) -> None:
    if not isinstance(value, str):
        raise invalid(f"{name} must be a string.")
    if len(value) > limit:
        raise invalid(f"{name} is {len(value)} characters; keep it to {limit} or fewer.")


def _what(event: dict) -> tuple:
    """What an event records, for telling a retry from a different call with the same key."""
    payload = event.get("payload")
    return (event.get("kind"), event.get("object"),
            payload.get("kind") if isinstance(payload, dict) else None)


def _check_fields(fields: dict) -> None:
    if not isinstance(fields, dict):
        raise invalid("fields must be an object.")
    for name, value in fields.items():
        if name in SERVER_FIELDS:
            raise invalid(f"'{name}' is filled by the server; leave it out.")
        if name not in CLIENT_FIELDS:
            raise invalid(f"Unknown event field '{name}'; allowed: "
                          f"{', '.join(sorted(CLIENT_FIELDS))}.")
        if name in TEXT_LIMITS:
            _check_text(name, value, TEXT_LIMITS[name])
        elif name in FIELD_ENUMS:
            if not isinstance(value, str) or value not in FIELD_ENUMS[name]:
                raise invalid(f"{name} must be one of {', '.join(sorted(FIELD_ENUMS[name]))}.")
        elif name in INT_RANGES:
            lo, hi = INT_RANGES[name]
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise invalid(f"{name} must be a whole number from {lo} to {hi}.")
        elif name in ("object", "problem"):
            if not isinstance(value, str) or not OBJECT_RE.fullmatch(value):
                raise invalid(f"{name} must look like concept:<id>, problem:<id> or inbox:<slug>.")
        elif name == "also":
            if not isinstance(value, list) or not all(
                    isinstance(v, str) and OBJECT_RE.fullmatch(v) for v in value):
                raise invalid("also must be a list like [\"concept:<id>\"].")
        elif name in ID_LISTS:
            if not isinstance(value, list) or not all(
                    isinstance(v, str) and ID_RE.fullmatch(v) for v in value):
                raise invalid(f"{name} must be a list of mistake ids.")
        elif name == "payload":
            if not isinstance(value, dict):
                raise invalid("payload must be an object.")
            try:  # NaN or Infinity would write a line that is not JSON
                json.dumps(value, allow_nan=False)
            except (ValueError, TypeError):
                raise invalid("payload values must be plain JSON (no NaN or Infinity).") \
                    from None
            for s in _strings(value):
                _check_text("A payload value", s, TEXT_LIMIT)


_TAIL = 64  # bytes before the parsed end that must still match for an incremental read


class _Parsed:
    """One event file's parse, in-process only. `events` come from the newline-terminated
    bytes [0, length) (`lines` lines), whose last _TAIL bytes are `tail`; `view` adds a valid
    unterminated last line, if any. `key` is the (st_ino, mtime_ns, size) it was read at."""
    __slots__ = ("key", "events", "length", "lines", "tail", "view")

    def __init__(self):
        self.key, self.events, self.length, self.lines, self.tail = None, [], 0, 0, b""
        self.view = self.events


# The log is append-only, so a file that only grew (same inode, its old tail unchanged) is
# read from where the last parse ended; anything else (undo's os.replace, a truncation, a
# same-size rewrite) is parsed again whole. Bad lines are logged once, when first parsed.
_PARSED: dict[Path, _Parsed] = {}


def _stat_key(st: os.stat_result) -> tuple[int, int, int]:
    return st.st_ino, st.st_mtime_ns, st.st_size


def _renamed(ev: dict) -> dict:
    """Events written before the rename (Cortex 0.6.1 and older) stay as written on disk; every
    fold and filter reads them here under the new names: the app's own client `cortex-app` (a
    grade's grader.client, an open's payload.client) and a keyed change's tool `cortex_*`."""
    for d, k in ((ev.get("grader"), "client"), (ev.get("payload"), "client")):
        if isinstance(d, dict) and d.get(k) == "cortex-app":
            d[k] = "keepfold-app"
    p = ev.get("payload")
    if isinstance(p, dict) and isinstance(t := p.get("tool"), str) and t.startswith("cortex_"):
        p["tool"] = "keepfold_" + t.removeprefix("cortex_")
    return ev


def _parse_lines(path: Path, data: bytes, first: int = 0, quiet: bool = False) -> list[dict]:
    out = []
    for n, line in enumerate(data.split(b"\n"), first + 1):
        if not line.strip():
            continue
        try:
            ev = json.loads(line.decode("utf-8"))
            if not (isinstance(ev, dict) and isinstance(ev.get("ts"), str)
                    and isinstance(ev.get("id"), str)):
                raise ValueError("not an event object")
        except ValueError as e:  # JSON and Unicode errors subclass ValueError
            if not quiet:
                log.warning("skipping %s line %d: %s", path, n, e)
            continue
        out.append(_renamed(ev))
    return out


def _parse_file(path: Path, key: tuple[int, int, int], hit: _Parsed | None) -> _Parsed:
    """`path` parsed at `key`: only the bytes past `hit` when the file just grew."""
    with open(path, "rb") as f:
        if hit is not None and hit.key[0] == key[0] and key[2] > hit.key[2]:
            f.seek(hit.length - len(hit.tail))
            data = f.read()
            if data.startswith(hit.tail):
                return _extend(path, key, hit, data, len(hit.tail))
        f.seek(0)
        return _extend(path, key, _Parsed(), f.read(), 0)


def _extend(path: Path, key, p: _Parsed, data: bytes, skip: int) -> _Parsed:
    """Add to `p` the lines of `data[skip:]`, which starts at file offset p.length; `data[:skip]`
    is p.tail. An unterminated last line goes only to `view`, silently: it may be mid-write, and
    it is parsed (and logged if bad) once a newline completes it."""
    end = max(data.rfind(b"\n") + 1, skip)
    if end > skip:
        done = data[skip:end]
        p.events.extend(_parse_lines(path, done, p.lines))
        p.lines += done.count(b"\n")
        p.length += end - skip
        p.tail = data[max(0, end - _TAIL):end]
    rest = data[end:]
    pending = _parse_lines(path, rest, p.lines, quiet=True) if rest.strip() else []
    p.view = p.events + pending if pending else p.events
    p.key = key
    return p


_WARNED: set[Path] = set()  # conflict copies already warned about: once per file


def _merged(files: list[tuple[Path, _Parsed]], session: str | None = None) -> list[dict]:
    """The events of `files`, each id once. A file not named <session>.jsonl is a sync tool's
    conflict copy (Syncthing, OneDrive, Dropbox: `keepfold doctor` names them): it adds only the
    ids the session files lack (the other device's appends) and, when `session` is given, only
    that session's events. An id held with other content keeps the session file's (a copy's,
    between copies, the first by path), with one warning per copy."""
    own = [SESSION_RE.fullmatch(path.stem) is not None for path, _ in files]
    if all(own):  # no copies: nothing to merge
        return [ev for _, p in files for ev in p.view]
    seen: dict[str, dict] = {}
    out = []
    # session files first (a stable sort: path order within each)
    for mine, (path, p) in sorted(zip(own, files, strict=True), key=lambda f: not f[0]):
        for ev in p.view:
            if not mine and session is not None and ev.get("session") != session:
                continue
            first = seen.setdefault(ev["id"], ev)
            if first is ev:
                out.append(ev)
            elif first != ev and not mine and path not in _WARNED:
                _WARNED.add(path)
                log.warning("%s (a sync conflict copy) holds events that differ from another "
                            "file's with the same id; the session file's are kept", path)
    return out


class EventLog:
    def __init__(self, vault: Vault):
        self.vault = vault
        self._last_id: ULID | None = None
        self._keys: dict[str, dict] = {}
        self._sig: dict[Path, tuple] | None = None  # the file keys self._keys was built from

    # ---- reading ------------------------------------------------------------

    def _dir(self):
        return self.vault.root / "events"

    def iter_events(self, session: str | None = None) -> Iterator[dict]:
        """All events (or one session's), sorted by (ts, id). Bad lines are logged and skipped."""
        if session is not None and not SESSION_RE.fullmatch(session):
            raise invalid("session must be s_<ULID>, cli or import.")
        # `<session>*`: its sync conflict copies too (`_merged` keeps only this session's events)
        pattern = f"*/*/{session}*.jsonl" if session else "*/*/*.jsonl"
        # shortcut: shallow copies guard top-level edits only; nested dicts are shared and
        # no caller mutates them. Deep-copy here if one ever must.
        out = [dict(ev) for ev in self._cached(pattern, session)]
        out.sort(key=lambda e: (e["ts"], e["id"]))
        return iter(out)

    def signature(self) -> tuple:
        """Every event file with its (ino, mtime_ns, size): while it is equal, so is
        iter_events(). Statting the files reads (and caches) any that changed."""
        return tuple((path, p.key) for path, p in self._files("*/*/*.jsonl"))

    def _files(self, pattern: str) -> list[tuple[Path, _Parsed]]:
        """The up-to-date parses of the files matching `pattern`."""
        out = []
        for path in sorted(self._dir().glob(pattern)):
            try:
                key = _stat_key(path.stat())  # before the read: a later change shows next time
                hit = _PARSED.get(path)
                if hit is None or hit.key != key:
                    hit = _PARSED[path] = _parse_file(path, key, hit)
            except OSError as e:
                log.warning("skipping %s: %s", path, e)
                continue
            out.append((path, hit))
        return out

    def _cached(self, pattern: str, session: str | None = None) -> list[dict]:
        """The parse-cached events of the files matching `pattern`, each id once (`_merged`),
        unsorted and shared with the cache: callers must not mutate them."""
        return _merged(self._files(pattern), session)

    def _key_map(self) -> dict[str, dict]:
        """key -> its first event (by ts, id), shared with the cache. Rebuilt whenever any file
        changed other than by this instance's own append (which updates it in place): another
        process's keys and a file reverted by keepfold undo must both be seen."""
        files = self._files("*/*/*.jsonl")
        sig = {path: p.key for path, p in files}
        if sig != self._sig:
            self._keys = {}
            for ev in _merged(files):
                self._add_key(ev)
            self._sig = sig
        return self._keys

    def _add_key(self, ev: dict) -> None:
        k, keys = ev.get("key"), self._keys
        if isinstance(k, str) and (k not in keys or (ev["ts"], ev["id"]) < (
                keys[k]["ts"], keys[k]["id"])):
            keys[k] = ev

    def has_key(self, key) -> bool:
        return key in self._key_map()

    # ---- writing ------------------------------------------------------------

    def _new_id(self, now: datetime) -> str:
        from ulid import ULID  # lazy: its import costs ~0.3 s, and readers (prime-hook) never mint

        new = ULID.from_datetime(now)
        if self._last_id is not None and int(new) <= int(self._last_id):
            new = ULID.from_int(int(self._last_id) + 1)  # keep ids monotonic in this process
        self._last_id = new
        return str(new)

    def append(self, *, session: str, kind: str, source: str, fields: dict,
               mode: str | None = None, key: str | None = None,
               predicted_p: float | None = None, grader: dict | None = None,
               now: datetime | None = None) -> dict:
        if not isinstance(session, str) or not SESSION_RE.fullmatch(session):
            raise invalid("session must be s_<ULID>, cli or import.")
        if kind not in KINDS:
            raise invalid(f"kind must be one of {', '.join(sorted(KINDS))}.")
        if source not in SOURCES:
            raise invalid(f"source must be one of {', '.join(sorted(SOURCES))}.")
        if mode is not None and mode not in MODES:
            raise invalid(f"mode must be one of {', '.join(sorted(MODES))}.")
        if key is not None:
            _check_text("key", key, 300)
        if predicted_p is not None and (isinstance(predicted_p, bool) or not isinstance(
                predicted_p, (int, float)) or not 0 <= predicted_p <= 1):
            raise invalid("predicted_p must be a number from 0 to 1.")
        if grader is not None:
            if not isinstance(grader, dict) or not set(grader) <= {"client", "model"}:
                raise invalid("grader may hold only client and model.")
            for name, value in grader.items():
                if value is not None:
                    _check_text(f"grader.{name}", value, 300)
        _check_fields(fields)
        for s in _strings([fields, grader, key]):
            scan_secrets(s)

        now = now or datetime.now(UTC)
        now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
        with self.vault.lock():
            keys = self._key_map()
            if key is not None and key in keys:
                old = keys[key]
                if _what(old) != _what({"kind": kind, **fields}):  # a retry repeats the call
                    raise invalid("This key is already used by a different call; use a new "
                                  "key (or none).")
                return dict(old)  # a copy: the cached event is shared
            eid = self._new_id(now)
            event = {"id": eid, "key": eid if key is None else key,
                     "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "day": local_day(now),
                     "session": session, "kind": kind}
            if mode is not None:
                event["mode"] = mode
            event["source"] = source
            event.update(fields)
            if predicted_p is not None:
                event["predicted_p"] = float(predicted_p)
            if grader is not None:
                event["grader"] = grader
            event["schema"] = 1
            self._write(event)
        return event

    def _write(self, event: dict) -> None:
        year, month = event["day"][:4], event["day"][5:7]
        path = self._dir() / year / month / f"{event['session']}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        try:
            before = _stat_key(path.stat())
        except FileNotFoundError:
            before = None
        with open(path, "ab+") as f:
            size = f.seek(0, os.SEEK_END)
            if size:
                f.seek(size - 1)
                if f.read(1) != b"\n":
                    # never truncate: the line may be a whole event; a torn one is skipped on read
                    log.warning("closing an unterminated last line in %s", path)
                    f.write(b"\n")
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        self.vault._track(path)
        self._remember(path, before, size, line)

    def _remember(self, path: Path, before, size: int, line: bytes) -> None:
        """Fold our own append into the parse cache, and the key map, so the next call reads
        nothing back. Only when the cache held the whole file just before the write; otherwise
        the next read catches up from the file."""
        hit = _PARSED.get(path)
        if before is None and size == 0:
            hit = _Parsed()
        elif hit is None or hit.key != before or hit.length != size:
            return
        try:
            after = _stat_key(path.stat())
        except OSError:
            _PARSED.pop(path, None)
            return
        ev = json.loads(line)  # a copy of the returned event, exactly as a re-read gives it
        hit.events.append(ev)
        hit.lines += 1
        hit.length += len(line)
        hit.tail = (hit.tail + line)[-_TAIL:]
        hit.key = after
        _PARSED[path] = hit
        if self._sig is not None and self._sig.get(path) == before:
            self._sig[path] = after
            self._add_key(ev)


def check_assessment_result(score, day) -> None:
    if score is not None and (isinstance(score, bool) or not isinstance(score, (str, int, float))
                              or (isinstance(score, float) and not math.isfinite(score))):
        raise invalid("score must be a string or a number.")
    if day is not None:
        check_day("date", day)


def log_assessment(log: EventLog, *, session: str, source: str, assessment: str, outcome: str,
                   score=None, company: str | None = None, kind: str | None = None,
                   date: str | None = None, felt_weak: list[str] | None = None,
                   key: str | None = None, **stamp) -> dict:
    """assessment_result (§15a.5): create the note if missing, set outcome/score, log the event.

    `stamp` (mode, grader, now) passes through to EventLog.append.
    """
    check_assessment_result(score, date)  # B10: before the note exists
    if company is not None and not isinstance(company, str):
        raise invalid("company must be text.")
    payload = {"kind": "assessment_result", "assessment": assessment, "outcome": outcome}
    for name, value in (("score", score), ("company", company), ("assessment_kind", kind),
                        ("date", date), ("felt_weak", felt_weak)):
        if value not in (None, "", []):
            payload[name] = value
    fields = {"object": f"assessment:{assessment}", "payload": payload}
    _check_fields(fields)  # too long or odd values are refused before anything is written
    vault = log.vault
    try:
        vault.read("assessment", assessment)
    except VaultError as e:
        if e.code != "unknown_id":
            raise
        props = {k: v for k, v in (("company", company), ("kind", kind), ("date", date))
                 if v is not None}
        title = " ".join(filter(None, (company, (kind or "").upper()))) or assessment
        vault.create_note("assessment", assessment, {**props, "outcome": outcome},
                          f"# {title}\n\n## Notes\n")
    vault.set_field("assessment", assessment, "outcome", outcome)
    if score is not None:
        vault.set_field("assessment", assessment, "score", score)
    return log.append(session=session, kind="log", source=source, key=key, fields=fields,
                      **stamp)


def void_event(log: EventLog, *, session: str, event_id: str, reason: str, source: str,
               **stamp) -> tuple[dict, dict]:
    """Append a `void` for `event_id` (must exist, not be a void, not be voided already).
    Returns (void event, target event); `stamp` (mode, grader, now) goes to EventLog.append."""
    if not isinstance(reason, str) or not reason.strip():
        raise invalid("reason is required.")
    with log.vault.lock():  # B11: two voids of one event must not both pass the check
        events = list(log.iter_events())
        target = next((e for e in events if e["id"] == event_id), None)
        if target is None:
            raise VaultError("unknown_id", f"No event '{event_id}' in the log.")
        if target.get("kind") == "void":
            raise invalid("A void cannot be voided.")
        if any(e.get("kind") == "void" and e.get("ref") == event_id for e in events):
            raise invalid(f"Event '{event_id}' is already voided.")
        ev = log.append(session=session, kind="void", source=source, **stamp,
                        fields={"ref": event_id, "payload": {"reason": reason.strip()}})
        _restore_note(log, target)
    return ev, target


# payload kind -> (note kind, payload id key, note field, default when no live event is left)
_RESTORE = {"problem_status": ("problem", "problem", "status", "todo"),
            "assessment_result": ("assessment", "assessment", "outcome", "pending")}


def _restore_note(log: EventLog, target: dict) -> None:
    """After a void, reset the field the voided event set, from the latest live event of the same
    kind, unless the note no longer holds the voided value (a later hand edit is kept). An
    assessment's score goes the same way: the latest live score, or no score at all."""
    pl = target.get("payload")
    spec = _RESTORE.get(pl.get("kind")) if isinstance(pl, dict) else None
    if spec is None:
        return
    nkind, idkey, field, default = spec
    nid = pl.get(idkey)
    try:
        note = log.vault.read(nkind, nid)
    except VaultError:
        return
    mine = [e for e in live(log.iter_events()) if isinstance(e.get("payload"), dict)
            and e["payload"].get("kind") == pl["kind"] and e["payload"].get(idkey) == nid]
    values = {}
    if note.fields.get(field) == pl.get(field):
        latest = mine[-1] if mine else None
        values[field] = latest["payload"][field] if latest else default
        if nkind == "problem":
            values["solved_on"] = latest["day"] if values[field] == "solved" else None
    if "score" in pl and note.fields.get("score") == pl["score"]:
        # log_assessment writes a score only when given, so the note holds the last one given
        values["score"] = next((e["payload"]["score"] for e in reversed(mine)
                                if "score" in e["payload"]), None)
    if values:
        log.vault._edit_fields(nkind, nid, values)
