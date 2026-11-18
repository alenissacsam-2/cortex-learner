"""Append-only evidence log: events/YYYY/MM/<session>.jsonl (docs/tools.md "Event").

Server-stamped fields never come from `fields`; reads skip torn or invalid lines.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING

from cortex.vault import ID_RE, Vault, VaultError, invalid, scan_secrets

if TYPE_CHECKING:
    from ulid import ULID

log = logging.getLogger(__name__)

SOURCES = {"tutor", "self", "external", "imported", "run"}
KINDS = {"ask", "grade", "log", "taught", "void", "remap", "resume", "close", "open",
         "change"}
MODES = {"home", "learn", "short", "review", "log", "about", "week", "ambient", "capture",
         "reflect",
         "project", "save", "history", "probe", "sweep", "mock"}
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
            if value not in FIELD_ENUMS[name]:
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
            for s in _strings(value):
                _check_text("A payload value", s, TEXT_LIMIT)


# path -> ((mtime_ns, size), events): in-process only. The log is append-only, so a re-read
# parses just the file being written. Bad lines are logged once per file version.
# shortcut: a same-size rewrite within one mtime tick is missed; add st_ino if that bites.
_PARSED: dict[Path, tuple[tuple[int, int], list[dict]]] = {}


def _parse_lines(path: Path, data: bytes) -> list[dict]:
    out = []
    for n, line in enumerate(data.split(b"\n"), 1):
        if not line.strip():
            continue
        try:
            ev = json.loads(line.decode("utf-8"))
            if not (isinstance(ev, dict) and isinstance(ev.get("ts"), str)
                    and isinstance(ev.get("id"), str)):
                raise ValueError("not an event object")
        except ValueError as e:  # JSON and Unicode errors subclass ValueError
            log.warning("skipping %s line %d: %s", path, n, e)
            continue
        out.append(ev)
    return out


class EventLog:
    def __init__(self, vault: Vault):
        self.vault = vault
        self._last_id: ULID | None = None

    # ---- reading ------------------------------------------------------------

    def _dir(self):
        return self.vault.root / "events"

    def iter_events(self, session: str | None = None) -> Iterator[dict]:
        """All events (or one session's), sorted by (ts, id). Bad lines are logged and skipped."""
        if session is not None and not SESSION_RE.fullmatch(session):
            raise invalid("session must be s_<ULID>, cli or import.")
        pattern = f"*/*/{session}.jsonl" if session else "*/*/*.jsonl"
        # shortcut: shallow copies guard top-level edits only; nested dicts are shared and
        # no caller mutates them. Deep-copy here if one ever must.
        out = [dict(ev) for ev in self._cached(pattern)]
        out.sort(key=lambda e: (e["ts"], e["id"]))
        return iter(out)

    def _cached(self, pattern: str) -> list[dict]:
        """The parse-cached events of the files matching `pattern`, unsorted and shared with
        the cache: callers must not mutate them."""
        out = []
        for path in sorted(self._dir().glob(pattern)):
            try:
                st = path.stat()  # before the read: a change after it misses the cache next time
                key = (st.st_mtime_ns, st.st_size)
                hit = _PARSED.get(path)
                if hit is None or hit[0] != key:
                    hit = _PARSED[path] = (key, _parse_lines(path, path.read_bytes()))
            except OSError as e:
                log.warning("skipping %s: %s", path, e)
                continue
            out.extend(hit[1])
        return out

    def _key_map(self) -> dict[str, dict]:
        """key -> its first event (by ts, id), shared with the cache. Rebuilt on each call from the
        cache: another process's keys and a file reverted by cortex undo must both be seen."""
        keys: dict[str, dict] = {}
        for ev in self._cached("*/*/*.jsonl"):
            k = ev.get("key")
            if isinstance(k, str) and (k not in keys or (ev["ts"], ev["id"]) < (
                    keys[k]["ts"], keys[k]["id"])):
                keys[k] = ev
        return keys

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
                return dict(keys[key])  # a copy: the cached event is shared
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
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(path, "ab+") as f:
            size = f.seek(0, os.SEEK_END)
            if size:
                f.seek(size - 1)
                if f.read(1) != b"\n":
                    # never truncate: the line may be a whole event; a torn one is skipped on read
                    log.warning("closing an unterminated last line in %s", path)
                    f.write(b"\n")
            f.write(line.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        self.vault._track(path)


def log_assessment(log: EventLog, *, session: str, source: str, assessment: str, outcome: str,
                   score=None, company: str | None = None, kind: str | None = None,
                   date: str | None = None, felt_weak: list[str] | None = None,
                   key: str | None = None, **stamp) -> dict:
    """assessment_result (§15a.5): create the note if missing, set outcome/score, log the event.

    `stamp` (mode, grader, now) passes through to EventLog.append.
    """
    vault = log.vault
    try:
        vault.read("assessment", assessment)
    except VaultError as e:
        if e.code != "unknown_id":
            raise
        fields = {k: v for k, v in (("company", company), ("kind", kind), ("date", date))
                  if v is not None}
        title = " ".join(filter(None, (company, (kind or "").upper()))) or assessment
        vault.create_note("assessment", assessment, {**fields, "outcome": outcome},
                          f"# {title}\n\n## Notes\n")
    vault.set_field("assessment", assessment, "outcome", outcome)
    if score is not None:
        vault.set_field("assessment", assessment, "score", score)
    payload = {"kind": "assessment_result", "assessment": assessment, "outcome": outcome}
    for name, value in (("score", score), ("company", company), ("assessment_kind", kind),
                        ("date", date), ("felt_weak", felt_weak)):
        if value not in (None, "", []):
            payload[name] = value
    return log.append(session=session, kind="log", source=source, key=key,
                      fields={"object": f"assessment:{assessment}", "payload": payload}, **stamp)


def void_event(log: EventLog, *, session: str, event_id: str, reason: str, source: str,
               **stamp) -> tuple[dict, dict]:
    """Append a `void` for `event_id` (must exist, not be a void, not be voided already).
    Returns (void event, target event); `stamp` (mode, grader, now) goes to EventLog.append."""
    if not isinstance(reason, str) or not reason.strip():
        raise invalid("reason is required.")
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
    kind, unless the note no longer holds the voided value (a later hand edit is kept)."""
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
    if note.fields.get(field) != pl.get(field):
        return
    latest = None
    for e in live(log.iter_events()):
        p = e.get("payload")
        if isinstance(p, dict) and p.get("kind") == pl["kind"] and p.get(idkey) == nid:
            latest = e
    value = latest["payload"][field] if latest else default
    values = {field: value}
    if nkind == "problem":
        values["solved_on"] = latest["day"] if value == "solved" else None
    log.vault._edit_fields(nkind, nid, values)
