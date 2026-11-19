"""Session lifecycle (SPEC §0 U4, §6, §15a.6-7, §15a.9): implicit sessions, modes, idle close,
end pipeline.

Pure Python; the MCP server and the CLI drive it. Closing runs the end pipeline: `close`
event, tool props of touched concepts, then (unless the session is quiet) journal line, session
note, Today.md and log.md line, then the session's git commit. No close rebuilds a whole-vault
view: `cortex dashboard` writes the dashboard and index.md when the user opens it.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ulid import ULID

from cortex import levels, planner, relink, render
from cortex._proc import NO_WINDOW
from cortex.catalog import Catalog
from cortex.events import MODES, SESSION_RE, EventLog, local_day, parse_ts
from cortex.taint import MARKER as TAINT_MARKER
from cortex.vault import Vault, VaultError, _pid_alive, secret_pattern

log = logging.getLogger(__name__)

# SPEC §0 U4: reads never need or open a session; the first write with none opens "ambient".
# Permission names: resurface, lint, split, void and quiz_result are actions of find, reflect,
# log and grade (mcp_server._action) that keep the rights of the tools they replaced.
READS = frozenset({"find", "read", "context", "lint", "reflect", "chats",
                   "resurface", "recall", "as_me", "prime"})  # as_me (PHASE4 V3) logs access
WRITES = frozenset({"ask", "grade", "quiz_result", "log", "patch", "add", "void", "split"})
_ALL = READS | WRITES | {"start", "end"}
_READ_ONLY = READS | {"start", "end"}
# docs/tools.md "Mode permissions". Review's ban on adding concepts/areas/domains depends on the
# add kind, so the MCP layer checks that; here review allows `add`.
ALLOWED = {
    "home": _ALL, "learn": _ALL, "short": _ALL, "review": _ALL,
    "log": _READ_ONLY | {"log", "patch", "add", "split"},
    "about": _READ_ONLY, "week": _READ_ONLY, "ambient": _ALL,
    "capture": _ALL, "reflect": _ALL, "project": _ALL, "save": _ALL, "history": _ALL,
    "probe": _ALL, "sweep": _ALL, "mock": _ALL,
}
IDLE_CLOSE = timedelta(hours=2)
LIVE_CLOSE = timedelta(hours=24)  # B2: a sweep closes a live server's session only this idle
RESUME_AFTER = timedelta(minutes=15)
EVENT_BUDGET, PATCH_BUDGET = 40, 10
COUNT_KINDS = ("asked", "graded", "logged", "patched", "added")
# Ledger `log` kinds: they record that a chat happened (a topic touch, a missing fact, a resurface,
# an act-as-me read), never learning evidence, a change, a note or a grade. A session whose only
# writes are these closes quietly: its close event and commit, no journal, session note,
# Today.md or log.md line (v6 CONSENSUS item 5, r1-user P1). Every other log kind is loud.
LEDGER_LOG_KINDS = frozenset({"touch", "need", "need_skip", "resurfaced", "access",
                              "chat_learned"})
SUMMARY_MAX = 600
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>!#|~])")


@dataclass
class Session:
    id: str
    mode: str
    topic: str | None
    model: str | None
    started: datetime
    last_activity: datetime
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(COUNT_KINDS, 0))
    ledger: int = 0  # how many of counts["logged"] were LEDGER_LOG_KINDS
    touched: set[str] = field(default_factory=set)
    over_events: bool = False   # write budget flags: set when exceeded, never block
    over_patches: bool = False
    changes: list[dict] = field(default_factory=list)  # {tool, id, kind, op, target} per write
    before: dict = field(default_factory=dict, repr=False)  # levels snapshot at start
    exposed: bool = False  # v5 F3: served external text; its claim writes are tier `exposed`
    mark: int = 0  # vault.writes at start: files written after it are this session's to commit


def _utc(t: datetime) -> datetime:
    return t.astimezone(UTC)  # a naive clock value is local time


def _no_session() -> VaultError:
    return VaultError("no_session", "No open Cortex session to close.")


class SessionManager:
    def __init__(self, vault: Vault, *, client: str | None = None, clock=datetime.now):
        self.vault, self.client, self.clock = vault, client, clock
        self.log = EventLog(vault)  # share this one so idempotency keys stay cached
        self.current: Session | None = None
        # v6 T2: the latest outside read. Exposure follows time, not the session: any session
        # opening within IDLE_CLOSE of it starts exposed, so closing one never launders it.
        # shortcut: in memory only, so a server restart forgets a Cortex-side read (a hook
        # marker still counts); persist it if restarts inside a chat turn out to matter.
        self._exposed_at: datetime | None = None
        self._taint_since = self._now()  # a taint marker older than this belongs to no chat here
        # Imported chat ids a harvest bundle served and the session that served them: the
        # harvest guards (`harvesting`) hold only while that session is current, and a new or
        # closed session clears them. shortcut: process state, so a server restart lifts the
        # guards (harvest verdict, contradiction 3); persist it if that bites.
        self.served: set[str] = set()
        self.served_sid: str | None = None

    def serve(self, ids) -> None:
        """A harvest bundle's chat ids, served in the current session (which must be open)."""
        self.served.update(ids)
        self.served_sid = self.current.id

    @property
    def harvesting(self) -> bool:
        """True while the session that served a harvest bundle is current and not idle-closed
        (the guards run before `require` would close an idle one)."""
        s = self.current
        return (bool(self.served) and s is not None and s.id == self.served_sid
                and self._now() - s.last_activity <= IDLE_CLOSE)

    def _now(self) -> datetime:
        return _utc(self.clock())

    def expose(self, at: datetime | None = None) -> None:
        """v5 F3: external text was served (at `at`, default now). It marks the open session
        and every session that opens within IDLE_CLOSE after it."""
        at = at or self._now()
        self._exposed_at = max(self._exposed_at or at, at)
        if self.current is not None:
            self.current.exposed = True

    def _check_taint(self) -> None:
        """Hook exposure: `cortex taint` (the plugin's PostToolUse hook) touches the marker when
        Claude reads outside text with a non-Cortex tool. A marker newer than this server's
        start and under 2 h old exposes like a Cortex read."""
        if self.current is not None and self.current.exposed:
            return
        try:
            mtime = (self.vault.root / TAINT_MARKER).stat().st_mtime
        except OSError:
            return
        # shortcut: one marker per vault, so a web read in any concurrent Claude Code chat
        # exposes this server's session too (the safe direction); key it by chat if that bites.
        t = datetime.fromtimestamp(mtime, UTC)
        if t > self._taint_since and self._now() - t < IDLE_CLOSE:
            self.expose(t)

    def start(self, mode: str = "learn", topic: str | None = None,
              model: str | None = None) -> Session:
        if mode not in MODES:
            raise VaultError("invalid_argument", f"mode must be one of {', '.join(sorted(MODES))}.")
        if self.current is not None:
            self.close(summary="auto-closed: new session started")
        self.served.clear()  # a new session is never a harvest in progress
        now = self._now()
        before, _ = planner.current_states(self.vault, local_day(now))
        recent = self._exposed_at is not None and now - self._exposed_at < IDLE_CLOSE
        self.current = Session(id=f"s_{ULID.from_datetime(now)}", mode=mode, topic=topic,
                               model=model, started=now, last_activity=now, before=before,
                               mark=self.vault.writes, exposed=mode == "capture" or recent)
        # an event so a sweep can find (and close and commit) a session that wrote no others;
        # the pid lets a sweep close it as soon as this server process has gone
        self.log.append(session=self.current.id, kind="open", source="tutor", mode=mode,
                        fields={"payload": {"pid": os.getpid()}}, now=now)
        return self.current

    def require(self, tool: str, label: str | None = None) -> Session | None:
        """The open session for `tool`. With none (or after an idle close), a read gets None
        and a write opens an ambient session; anything else is `no_session`. `label` names
        the call in the refusal message."""
        s = self.current
        now = self._now()
        if s is not None and now - s.last_activity > IDLE_CLOSE:
            # B15: it ended at its last activity (+1 s: the close sorts after its last event)
            self.close(summary="auto-closed after 2 h idle",
                       ended=s.last_activity + timedelta(seconds=1))
            s = None
        self._check_taint()
        if s is not None and tool in WRITES and any(
                e.get("kind") == "close" for e in self.log.iter_events(session=s.id)):
            # B2: another process closed it (a sweep, `cortex session close`); a write there
            # would miss its commit and `cortex undo`, so it goes to a fresh session
            self._closed()
            new = self.start(s.mode, s.topic, s.model)
            new.exposed = new.exposed or s.exposed  # a reopen never launders exposure
            s = new
        if s is None:
            if tool in READS:
                return None
            if tool in WRITES:
                return self.start("ambient")
            raise _no_session()
        idle = now - s.last_activity
        if idle > RESUME_AFTER:
            self.log.append(session=s.id, kind="resume", source="tutor", mode=s.mode,
                            fields={}, now=now)
        if tool not in ALLOWED[s.mode]:
            raise VaultError("read_only_mode", f"{label or 'cortex_' + tool} is not available "
                             f"in {s.mode} mode; call cortex_end first, then retry.")
        s.last_activity = now
        return s

    def note(self, kind: str, concept: str | None = None, change: dict | None = None, *,
             log_kind: str | None = None) -> str:
        """Count a write; `change` is its record for the end card; `log_kind` is a logged
        write's cortex_log kind. Returns a soft hint the call that first pushes patches/adds
        over budget appends to its `next`, else ""."""
        s = self.current
        if s is None:
            raise _no_session()
        if kind not in (*COUNT_KINDS, "voided", "journal"):
            raise ValueError(f"kind must be one of {COUNT_KINDS}")
        if kind in COUNT_KINDS:
            s.counts[kind] += 1
        if kind == "logged" and log_kind in LEDGER_LOG_KINDS:
            s.ledger += 1
        if concept:
            s.touched.add(concept)
        if change:
            s.changes.append(change)
        if change or kind in ("patched", "added"):
            self._persist(s, kind, concept, change)
        c = s.counts
        events = c["asked"] + c["graded"] + c["logged"]
        crossed_ev = not s.over_events and events > EVENT_BUDGET
        s.over_events = s.over_events or crossed_ev
        writes = c["patched"] + c["added"]
        crossed = not s.over_patches and writes > PATCH_BUDGET
        s.over_patches = s.over_patches or crossed
        wrap = "; consider wrapping up with cortex_end."
        return ((f" This session has made {writes} changes{wrap}" if crossed else "")
                + (f" This session has recorded {events} events{wrap}" if crossed_ev else ""))

    def _persist(self, s: Session, kind: str, concept: str | None, change: dict | None) -> None:
        """A `change` event so a sweep-close rebuilds the counts and Changed list (levels and
        the planner ignore it). It must never fail the write it records. `exposed` (v6 T3) is
        stamped from the session, never from tool input."""
        payload = {"note": kind, **({"concept": concept} if concept else {}),
                   **({"change": change} if change else {}),
                   **({"exposed": True} if s.exposed else {})}
        try:
            self.log.append(session=s.id, kind="change", source="tutor", mode=s.mode,
                            fields={"payload": payload}, now=self._now())
        except VaultError as e:
            log.warning("change event not recorded: %s", e)

    def close(self, summary: str = "", ended: datetime | None = None) -> dict:
        """`ended`: when the session really ended (an idle close: its last activity)."""
        s = self.current
        if s is None:
            raise _no_session()
        return _end(self.vault, self.log, sid=s.id, mode=s.mode, started=s.started,
                    ended=ended or self._now(), now=self._now(), counts=s.counts,
                    touched=s.touched, before=s.before,
                    summary=summary, changes=s.changes, mark=s.mark, ledger=s.ledger,
                    on_closed=self._closed)

    def _closed(self) -> None:
        self.current = None
        self.served.clear()


# ---- end pipeline -------------------------------------------------------------------

def _esc(text) -> str:
    """One-line note-derived text with Markdown links, images and HTML neutralised."""
    return _MD_SPECIAL.sub(r"\\\1", " ".join(str(text).split()))


def _state_word(state: str | None) -> str:
    state = state or "not_started"
    return f"{levels.GLYPH[state]} {levels.LABEL[state]}"


def _minutes(started: datetime, ended: datetime) -> int:
    return max(0, round((ended - started).total_seconds() / 60))


def _session_md(sid, mode, started, ended, counts, rows, unchecked, summary,
                changes=()) -> str:
    mins = _minutes(started, ended)
    lines = [f"# Session {sid}", "",
             f"- Mode: {mode}",
             f"- Started: {started.astimezone():%Y-%m-%d %H:%M}",
             f"- Duration: {mins} min",
             "- Counts: " + ", ".join(f"{k} {counts.get(k, 0)}" for k in COUNT_KINDS),
             "", "## Concepts"]
    lines += [f"- {_esc(r['title'])} ({r['id']}): {_state_word(r['before'])} → "
              f"{_state_word(r['after'])}" for r in rows] or ["- none"]
    lines += ["", "## Taught, not yet checked"]
    lines += [f"- {_esc(u['title'])} ({u['id']})" for u in unchecked] or ["- none"]
    if changes:
        lines += ["", "## Changed", *render.changed_lines(changes)]
    lines += ["", "## Summary", _esc(summary) if summary else "(none)", ""]
    return "\n".join(lines)


GIT_TIMEOUT_S = 60  # a hung git (pinentry, a hook, antivirus) must not hold the vault lock


def _run_git(cmd: list[str], input: str | None = None) -> subprocess.CompletedProcess:
    """git with UTF-8 I/O; a timeout comes back as a failed run (returncode 1), with a warning."""
    try:
        return subprocess.run(cmd, input=input, capture_output=True, encoding="utf-8",
                              timeout=GIT_TIMEOUT_S, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        log.warning("vault git timed out after %d s: %s", GIT_TIMEOUT_S, " ".join(cmd[1:]))
        return subprocess.CompletedProcess(cmd, 1, "", f"git timed out after {GIT_TIMEOUT_S} s")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return _run_git(["git", "-C", str(root), *args])


def git_busy(root: Path) -> str | None:
    """Why the vault repo must not take a commit now (an unfinished revert/merge/cherry-pick
    or unmerged files), else None."""
    heads = ("REVERT_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD")
    paths = _git(root, "rev-parse", *(a for h in heads for a in ("--git-path", h))).stdout
    for head, p in zip(heads, paths.splitlines(), strict=False):
        if (root / p).exists():
            return f"{head} exists"
    unmerged = unmerged_paths(root)
    return f"unmerged files: {', '.join(unmerged)}" if unmerged else None


def unmerged_paths(root: Path) -> list[str]:
    out = _git(root, "diff", "--name-only", "-z", "--diff-filter=U").stdout
    return sorted(set(filter(None, out.split("\0"))))


def _gitz(root, *args: str, input: str | None = None) -> subprocess.CompletedProcess:
    """git with UTF-8 I/O and literal pathspecs: file names are data, never globs."""
    return _run_git(["git", "--literal-pathspecs", "-C", str(root), *args], input=input)


def _status_paths(out: str) -> list[str]:
    """Repo-relative paths in `git status --porcelain -z` output, both sides of a rename."""
    parts, paths, i = out.split("\0"), [], 0
    while i < len(parts):
        entry, i = parts[i], i + 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if ("R" in entry[:2] or "C" in entry[:2]) and i < len(parts):
            paths.append(parts[i])  # the rename's source
            i += 1
    return paths


def _rel(vault: Vault, paths) -> set[str]:
    return {Path(p).relative_to(vault.root).as_posix() for p in paths
            if Path(p).is_relative_to(vault.root)}


def _commit_paths(top: str, paths: list[str], message: str) -> bool:
    """Stage and commit exactly `paths` (a pathspec commit leaves anything else in the index)."""
    spec = "\0".join(paths)
    for args in (("add", "-A"), ("commit", "-q", "-m", message)):
        r = _gitz(top, *args, "--pathspec-from-file=-", "--pathspec-file-nul", input=spec)
        if r.returncode:
            log.warning("vault git %s failed: %s", args[0], r.stderr.strip())
            return False
    return True


SCAN_MAX_BYTES = 1 << 20  # r1-trust T7: larger files (exports, media) are not scanned


def _withhold_secrets(top: str, paths: list[str], withheld: list[str]) -> list[str]:
    """v6 T7: drop from `paths` (repo-relative) each file that looks like it holds a key, so it
    stays uncommitted; only the files about to be committed are read, never the whole vault.
    Each drop is logged and added to `withheld` as "<path>: <pattern>", never the secret."""
    keep = []
    for rel in paths:
        p = Path(top) / rel
        try:
            hit = p.is_file() and p.stat().st_size <= SCAN_MAX_BYTES and secret_pattern(
                p.read_bytes().decode("utf-8", errors="ignore"))
        except OSError:
            hit = None
        if hit:
            withheld.append(f"{rel}: {hit}")
            log.warning("vault commit left %s uncommitted: it matches secret pattern %s; "
                        "remove the key and close again", rel, hit)
        else:
            keep.append(rel)
    return keep


def _commit(vault: Vault, message: str, mine=None, *, before: str | None = None,
            exclude=(), withheld: list[str] | None = None) -> str | None:
    """Commit in the vault repo. With `mine` (vault file paths), only those of them that
    changed go in, and every other change, less `exclude`, first gets its own commit `before`
    (with no `before` it stays uncommitted); with no `mine`, every change goes in. A changed
    file that matches a secret pattern stays uncommitted and is named in `withheld`. Returns
    the `message` commit, or None if not a repo, none of ours changed, a revert/merge is in
    progress, or git failed."""
    withheld = [] if withheld is None else withheld
    with vault.lock():
        top = _gitz(vault.root, "rev-parse", "--show-toplevel")
        if top.returncode:
            return None
        top = top.stdout.strip()
        from cortex.browse.forget import PRIVATE_IGNORE

        status = _gitz(top, "status", "--porcelain", "-z", "--untracked-files=all")
        pre = _gitz(vault.root, "rev-parse", "--show-prefix").stdout.strip()
        # C3: a private browse page still in the index (staged, or tracked before the ignore
        # line) is never committed; `untrack_private` takes it out of the index
        changed = [c for c in _status_paths(status.stdout)
                   if not c.startswith(pre + PRIVATE_IGNORE)]
        if status.returncode or not changed:
            return None
        if busy := git_busy(vault.root):
            log.warning("vault commit skipped (%s); finish or abort it in git first", busy)
            return None
        ours, other = changed, []
        if mine is not None:
            keys = {pre + r for r in _rel(vault, mine)}
            skip = {pre + r for r in _rel(vault, exclude)}
            ours = [c for c in changed if c in keys]
            other = [c for c in changed if c not in keys and c not in skip] if before else []
    # B12: the lock covered only the snapshot and the path lists; the scan, add and commit run
    # on those exact paths outside it, so other writers never wait on git. A file written
    # meanwhile goes in at its newer, whole state (writes are atomic replaces, and git reads
    # an appended event file up to the size it stat'ed), the same as a write made just before
    # the snapshot; ownership is by path, so `exclude` still holds. A revert or merge begun
    # meanwhile makes git refuse the pathspec commit, and a second commit's index.lock fails
    # the add or commit: either way a logged warning and None.
    ours = _withhold_secrets(top, ours, withheld)
    other = _withhold_secrets(top, other, withheld)
    if other and not _commit_paths(top, other, before):
        return None
    if not ours or not _commit_paths(top, ours, message):
        return None
    # HEAD may already be another process's later commit: the newest with our message
    shas = _gitz(top, "log", "-z", "-n", "20", "--format=%H %B").stdout.split("\0")
    return next((h for h, _, b in (s.partition(" ") for s in shas) if b.strip() == message),
                None)


_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _owned(vault: Vault, events) -> dict[str, set[Path]]:
    """Files each session's events show it wrote: its event files and the notes its `change`
    records name (what a sweep can know of a session another process ran)."""
    out: dict[str, set[Path]] = {}
    for e in events:
        sid, day = e.get("session"), e.get("day")
        if not (isinstance(sid, str) and sid.startswith("s_") and SESSION_RE.fullmatch(sid)
                and isinstance(day, str) and _DAY_RE.fullmatch(day)):
            continue
        paths = out.setdefault(sid, set())
        paths.add(vault.root / "events" / day[:4] / day[5:7] / f"{sid}.jsonl")
        p = e.get("payload") if e.get("kind") == "change" else None
        ch = p.get("change") if isinstance(p, dict) else None
        if isinstance(ch, dict):
            try:
                paths.add(vault.path_for(ch.get("kind"), ch.get("id")))
            except (VaultError, TypeError):
                pass
    return out


def exposed_pages(events) -> set[str]:
    """v6 T3: ids of pages whose latest `change` event was written in an exposed session.
    Events only, never file mtime: `_end` writes `cortex_status` on concept pages and logs no
    change, so an mtime rule would clear every touched concept on close. `events` sorted."""
    latest: dict[str, bool] = {}
    for e in events:
        p = e.get("payload") if e.get("kind") == "change" else None
        ch = p.get("change") if isinstance(p, dict) else None
        if isinstance(ch, dict) and isinstance(ch.get("id"), str):
            latest[ch["id"]] = p.get("exposed") is True
    return {i for i, exposed in latest.items() if exposed}


def _commit_session(vault: Vault, evlog: EventLog, sid: str, mode: str, mark: int,
                    withheld: list[str] | None = None) -> str | None:
    """Commit what this session wrote: the files this process wrote since `mark` plus what its
    events show. Other changes (hand edits, CLI writes, other processes) go in a commit of
    their own first, so `cortex undo <sid>` never touches them; files other still-open
    sessions own are left for those sessions' own commits."""
    events = list(evlog.iter_events())
    owned = _owned(vault, events)
    closed = {e.get("session") for e in events if e.get("kind") == "close"}
    others = set().union(*(ps for s, ps in owned.items() if s != sid and s not in closed))
    return _commit(vault, f"cortex: session {sid} {mode}",
                   owned.get(sid, set()) | vault.wrote_since(mark),
                   before=f"cortex: other changes before session {sid}", exclude=others,
                   withheld=withheld)


def _end(vault: Vault, evlog: EventLog, *, sid: str, mode: str, started: datetime,
         ended: datetime, counts: dict, touched: set[str], before: dict, summary: str,
         changes=(), on_closed=None, mark: int = 0, ledger: int = 0,
         now: datetime | None = None) -> dict:
    """`mark`: vault.writes when the session started in this process; files written since
    then belong to it. `ledger`: how many of counts["logged"] were LEDGER_LOG_KINDS; a session
    with nothing else is quiet. `ended` dates the close event, duration and journal line;
    `now` (default `ended`) the states, tool props and Today.md written at the close (B15)."""
    now = now or ended
    fields = {"summary": summary[:SUMMARY_MAX]} if summary else {}
    with vault.lock():  # SEC-6: a sweep in another process may have closed it meanwhile
        if any(e.get("kind") == "close" for e in evlog.iter_events(session=sid)):
            if on_closed:
                on_closed()
            return {"session": sid, "mode": mode, "counts": dict(counts), "moved": [],
                    "unchecked": [], "today": [], "minutes": _minutes(started, ended),
                    "changes": list(changes), "commit": None, "already_closed": True}
        evlog.append(session=sid, kind="close", source="tutor", mode=mode, fields=fields,
                     now=ended)
    if on_closed:
        on_closed()  # the session is closed from here on, whatever fails below
    catalog = Catalog.load(vault)
    after, _ = planner.current_states(vault, local_day(now), catalog)

    def title(c):
        e = catalog.entries.get(c)
        return e.title if e else c

    rows = []
    unchecked = [{"id": c, "title": title(c)} for c in planner.fresh_unchecked(
        {c: after[c] for c in touched if c in after}, local_day(now))]
    for c in sorted(touched):
        b, a = before.get(c), after.get(c)
        rows.append({"id": c, "title": title(c), "before": b.state if b else "not_started",
                     "after": a.state if a else "not_started"})
        try:
            vault.set_tool_props("concept", c, a.state if a else "not_started",
                                 a.next_due if a else None)
        except VaultError as e:
            if e.code not in ("unknown_id", "invalid_id"):
                raise
    moved = [r for r in rows if r["before"] != r["after"]]
    today = planner.today(vault, catalog, after, now=now)
    quiet = not changes and counts.get("logged", 0) == ledger and not any(
        counts.get(k) for k in COUNT_KINDS if k != "logged")
    if not quiet:
        _record(vault, sid=sid, mode=mode, started=started, ended=ended, counts=counts,
                rows=rows, unchecked=unchecked, summary=summary, changes=changes, today=today,
                as_of=local_day(now))
    withheld: list[str] = []
    commit = _commit_session(vault, evlog, sid, mode, mark, withheld)
    return {"session": sid, "mode": mode, "counts": dict(counts), "moved": moved,
            "unchecked": unchecked, "today": today, "minutes": _minutes(started, ended),
            "changes": list(changes), "commit": commit, "not_committed": withheld}

# The journal line a session close writes. AUTO_JOURNAL matches it so the portrait gate
# (reflect.portrait) can tell Cortex's own lines from the learner's; keep the two in sync.
SESSION_LINE = "session {mode}: {changes} changes, {graded} graded"
AUTO_JOURNAL = re.compile(r"session \w+: \d+ changes, \d+ graded")


def _record(vault: Vault, *, sid, mode, started, ended, counts, rows, unchecked, summary,
            changes, today, as_of) -> None:
    """What a non-quiet close writes besides its event and commit: journal line, session
    note, Today.md (one planner pass, ~10 ms at 5k pages) and the log.md line."""
    try:
        vault.append_journal(local_day(ended), f"{ended.astimezone():%H:%M}",
                             SESSION_LINE.format(mode=mode, changes=len(changes),
                                                 graded=counts.get("graded", 0)))
    except Exception:  # the journal is a view of the day; it must never break close
        log.exception("journal line failed")
    name = f"{local_day(started)}-{mode}-{sid[-8:].lower()}"
    text = _session_md(sid, mode, started, ended, counts, rows, unchecked, summary, changes)
    day = next((d for d in (local_day(started), local_day(ended))
                if vault.path_for("journal", d).is_file()), None)
    vault.write_text(vault.path_for("session", name),
                     relink.with_up(text, day) if day else text)  # Obsidian: session -> day
    vault.write_text(vault.root / "Today.md", render.today_md(
        today=today, sprint_day=planner.sprint_day(vault),
        assessments=planner.assessments(vault), as_of=as_of,
        cues=planner.last_questions(vault)))
    try:  # log.md is a view too; it must never break close
        vault.append_log(local_day(ended), "session", f"{mode}, {len(changes)} changes")
    except Exception:
        log.exception("log.md line failed")


def sweep(vault: Vault, clock=datetime.now, *, force: bool = False) -> list[str]:
    """Close orphan sessions (events but no `close`) idle > 2 h or whose server process has
    exited (the pid in its `open` event), or all of them with `force`. A session whose server
    is still alive is left open until it idles out."""
    evlog = EventLog(vault)
    events = list(evlog.iter_events())
    by_session: dict[str, list[dict]] = {}
    for ev in events:
        sid = ev.get("session")
        if isinstance(sid, str) and sid.startswith("s_") and SESSION_RE.fullmatch(sid):
            by_session.setdefault(sid, []).append(ev)
    now = _utc(clock())
    closed = []
    for sid, evs in by_session.items():
        if any(e.get("kind") == "close" for e in evs):
            continue
        pid = next((e["payload"].get("pid") for e in evs if e.get("kind") == "open"
                    and isinstance(e.get("payload"), dict)), None)
        alive = type(pid) is int and _pid_alive(pid)
        gone = type(pid) is int and not alive
        last = parse_ts(evs[-1]["ts"])
        # B2: a live server's session may be reading for hours without an event; it closes
        # itself when idle, so a sweep waits a day for it (pid reuse aside)
        if not force and not gone and now - last <= (LIVE_CLOSE if alive else IDLE_CLOSE):
            continue
        kinds = [e.get("kind") for e in evs]
        counts = dict.fromkeys(COUNT_KINDS, 0)
        counts.update(asked=kinds.count("ask"), graded=kinds.count("grade"),
                      logged=kinds.count("log") + kinds.count("taught"))
        ledger = sum(e.get("kind") == "log" and isinstance(e.get("payload"), dict)
                     and e["payload"].get("kind") in LEDGER_LOG_KINDS for e in evs)
        touched = {o.split(":", 1)[1] for e in evs
                   for o in [e.get("object"), *(e.get("also") or [])]
                   if isinstance(o, str) and o.startswith("concept:")}
        changes = []
        for e in evs:  # what SessionManager.note recorded, as the in-memory close sees it
            p = e.get("payload") if e.get("kind") == "change" else None
            if not isinstance(p, dict):
                continue
            if p.get("note") in ("patched", "added"):
                counts[p["note"]] += 1
            if isinstance(p.get("concept"), str):
                touched.add(p["concept"])
            if isinstance(p.get("change"), dict):
                changes.append(p["change"])
        mode = next((e["mode"] for e in evs if e.get("mode") in MODES), "learn")
        before, _ = planner.current_states(
            vault, local_day(now), events=[e for e in events if e.get("session") != sid])
        if any(e.get("kind") == "close" for e in evlog.iter_events(session=sid)):
            continue  # another process closed it since the scan; _end re-checks under the lock
        # B15: an idle or orphaned session ended at its last event (+1 s, so the close sorts
        # after it); `force` closes it now
        res = _end(vault, evlog, sid=sid, mode=mode, started=parse_ts(evs[0]["ts"]),
                   ended=now if force else min(now, last + timedelta(seconds=1)), now=now,
                   counts=counts, touched=touched, before=before, summary="auto-closed",
                   changes=changes, mark=vault.writes, ledger=ledger)  # another process ran it
        if not res.get("already_closed"):
            closed.append(sid)
    return closed
