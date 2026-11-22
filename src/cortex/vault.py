"""Vault parsing and safe writes: lock, atomic replace, ID and path checks, secret scan.

This is the only module that touches vault files (SPEC §4, CLAUDE.md).
"""

from __future__ import annotations

import copy
import io
import json
import logging
import os
import re
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

log = logging.getLogger(__name__)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,80}$")
# The private browse folder, vault-relative: kept from Claude, git and the index.
PRIVATE_BROWSE_DIR = "browse/private"
# C5: imported AI chat pages (resources), vault-relative; git-ignored, read like resources/
CHAT_ARCHIVE = "archive/chats"

# SPEC §15a.2: fields marked * per kind. TOOL_PROPS are written only by set_tool_props.
SET_FIELD_WHITELIST = {
    "problem": {"status", "solved_on"},
    "assessment": {"date", "outcome", "score", "summary"},
    "application": {"status"},
    "track": {"current_day"},
    "resource": {"status", "summary", "url"},
    "note": {"summary"},
    "project": {"status", "summary"},
    "decision": {"status", "summary", "review_on"},
    "loop": {"status", "due", "summary"},
}
TOOL_PROPS = {"cortex_status", "cortex_next"}
ENUMS = {
    ("problem", "status"): {"todo", "attempted", "solved"},
    # taken: the test is done and the result is not out yet; only pending counts as coming up
    ("assessment", "outcome"): {"pending", "taken", "pass", "fail", "unknown"},
    ("application", "status"): {"applied", "oa", "interview", "offer", "rejected", "withdrawn"},
    # phase4-wiki.md §2, §2a
    ("resource", "resource_type"): {"book", "paper", "course", "video", "repo", "article", "other"},
    ("resource", "status"): {"queued", "reading", "done", "reference"},
    ("note", "note_type"): {"fleeting", "literature", "permanent", "structure"},
    ("project", "status"): {"idea", "active", "paused", "shipped", "archived"},
    ("decision", "status"): {"proposed", "accepted", "superseded", "retired"},
    ("loop", "status"): {"open", "done", "dropped"},
    ("loop", "loop_type"): {"commitment", "question", "idea"},
}

SECRET_RES = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"api[_-]?key\s*[:=]\s*\S{12,}", re.IGNORECASE),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),  # Stripe
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),  # Google API key
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),  # GitLab
    re.compile(r"\bhf_[A-Za-z]{30,}"),  # Hugging Face
    re.compile(r"\bnpm_[A-Za-z0-9]{36}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWT
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@"),  # credentials in a URL
    re.compile(r"\bpassword\s*[:=]\s*\S{6,}", re.IGNORECASE),
    # a 64-hex value soon after a private-key word (pasted wallet keys); bare tx hashes pass
    re.compile(r"priv(?:ate)?[_ -]?key[^\n]{0,40}?(?<![0-9a-f])(?:0x)?[0-9a-f]{64}(?![0-9a-f])",
               re.IGNORECASE),
]

# path -> ((mtime_ns, size), fields, body): in-process only, so re-reads skip the YAML parse.
# shortcut: a same-size rewrite within one mtime tick is missed; add st_ino if that bites.
_PARSED: dict[Path, tuple[tuple[int, int], CommentedMap, str]] = {}

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
UNCHECKED_RE = re.compile(r"^\s*[-*] \[ \] ")
CHECKED_RE = re.compile(r"^\s*[-*] \[[xX]\] ")

LOCK_STALE_S = 120
REPLACE_TRIES = 8
# lock tokens this process holds now: a lock file with this pid and another token is one it
# failed to delete (Windows refuses while another process reads it), so never a live holder
_HELD: set[str] = set()


class VaultError(Exception):
    """A vault failure with a tool error code from docs/tools.md."""

    def __init__(self, code: str, message: str, did_you_mean: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.did_you_mean = did_you_mean


def invalid(msg: str) -> VaultError:
    return VaultError("invalid_argument", msg)


def check_day(name: str, value) -> None:
    """B10: YYYY-MM-DD and a real day, or the exam clock silently skips the note."""
    try:
        ok = bool(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
                  and date.fromisoformat(value))
    except ValueError:
        ok = False
    if not ok:
        raise invalid(f"{name} must be a date like 2026-11-30.")


@dataclass
class Note:
    path: Path
    kind: str
    id: str
    fields: CommentedMap
    body: str


TRUTHY = {"true", "yes", "y", "on", "1"}


def truthy(v) -> bool:
    """A frontmatter flag set in any truthy form a person might type: true, yes, y, on, 1
    (any case, quoted or not)."""
    return v is True or str(v).strip().casefold() in TRUTHY


def is_private(note: Note) -> bool:
    # v5 PHASE4 V4: people pages are private unless their frontmatter says `private: false`
    p = note.fields.get("private")
    return truthy(p) or (note.kind == "person" and p is not False)


def secret_pattern(text: str) -> str | None:
    """The first SECRET_RES pattern `text` matches (the regex, never the match), or None."""
    return next((rx.pattern for rx in SECRET_RES if rx.search(text)), None)


def scan_secrets(text: str) -> None:
    if secret_pattern(text):
        raise VaultError("secret_detected", "The text looks like it holds a key or token; "
                         "remove it and try again.")


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 1_000_000  # never wrap
    return y


def _split(text: str) -> tuple[str, str, str, str] | None:
    """Split into (open fence, frontmatter, close fence, body), or None without frontmatter."""
    m = re.match(r"\ufeff?---[ \t]*(\r?\n)", text)  # an optional BOM stays in the open fence
    if not m:
        return None
    close = re.compile(r"^---[ \t]*(\r?\n|\Z)", re.M).search(text, m.end())
    if not close:
        raise ValueError("frontmatter fence is not closed")
    return text[: m.end()], text[m.end() : close.start()], close.group(0), text[close.end() :]


def _newline(text: str) -> str:
    return "\r\n" if text.split("\n", 1)[0].endswith("\r") else "\n"


_FLAT = re.compile(r'([A-Za-z_][\w-]*): ("(?:[^"\\]|\\.)*"|-?(?:0|[1-9][0-9]*)|true|false)')


def _flat_fm(fm: str) -> CommentedMap | None:
    """Flat `key: "json string" | int | true | false` lines (generated pages, sites._fm) read
    without ruamel, which took ~5 ms a page (R2-10: minutes for 20k site pages), to the values
    and types ruamel gives them; None for anything else."""
    out = CommentedMap()
    for line in fm.splitlines():
        m = _FLAT.fullmatch(line)
        if not m or m[1] in out:
            return None
        v = m[2]
        if v[0] == '"':
            try:
                out[m[1]] = DoubleQuotedScalarString(json.loads(v))
            except ValueError:  # an escape JSON lacks, such as \x41: ruamel reads it
                return None
        else:
            out[m[1]] = v == "true" if v in ("true", "false") else int(v)
    return out


def _load_fm(fm: str) -> CommentedMap:
    if (flat := _flat_fm(fm)) is not None:
        return flat
    data = _yaml().load(fm) if fm.strip() else CommentedMap()
    if data is None:
        return CommentedMap()
    if not isinstance(data, CommentedMap):
        raise ValueError("frontmatter is not a mapping")
    return data


def _dump_fm(data: CommentedMap, nl: str) -> str:
    buf = io.StringIO()
    _yaml().dump(data, buf)
    out = buf.getvalue()
    return out.replace("\n", nl) if nl != "\n" else out


def _to_yaml(value):
    """Plain Python values to the vault format: quoted strings, flow-style lists and maps."""
    if isinstance(value, str):
        return DoubleQuotedScalarString(value)
    if isinstance(value, (list, tuple)):
        seq = CommentedSeq(_to_yaml(v) for v in value)
        seq.fa.set_flow_style()
        return seq
    if isinstance(value, dict):
        m = CommentedMap((_to_yaml(k), _to_yaml(v)) for k, v in value.items())
        m.fa.set_flow_style()
        return m
    return value


def _section(lines: list[str], heading: str) -> tuple[int, int]:
    """Return [start, end) line indexes of the section under `heading` (text without #s)."""
    want = heading.lstrip("#").strip()
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line.rstrip("\r\n"))
        if m and m.group(2) == want:
            level = len(m.group(1))
            for j in range(i + 1, len(lines)):
                n = HEADING_RE.match(lines[j].rstrip("\r\n"))
                if n and len(n.group(1)) <= level:
                    return i + 1, j
            return i + 1, len(lines)
    raise VaultError("invalid_argument", f"Heading '{want}' is not in the note; "
                     "use an existing heading.")


def _touch(path: Path) -> None:
    try:
        path.read_bytes()
    except OSError:
        pass


def warm(paths: list[Path]) -> None:
    """Read files once in parallel before a sequential parse: the first open of a newly
    written file waits for the antivirus scan (~9 ms each here, R2-10), and scans overlap."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(16) as ex:
        list(ex.map(_touch, paths))


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if os.name == "nt":
        # os.kill(pid, 0) would terminate the process on Windows; query it instead.
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # access denied: it exists
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_started(pid: int) -> float | None:
    """When process `pid` started (epoch seconds), or None when that cannot be read."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return None
            t = [wintypes.FILETIME() for _ in range(4)]
            ok = k32.GetProcessTimes(wintypes.HANDLE(h), *map(ctypes.byref, t))
            k32.CloseHandle(wintypes.HANDLE(h))
            # creation time: 100 ns ticks since 1601
            return ((t[0].dwHighDateTime << 32 | t[0].dwLowDateTime) / 1e7 - 11644473600
                    if ok else None)
        # field 22 of /proc/<pid>/stat: clock ticks since boot; /proc/stat btime: the boot
        ticks = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
        boot = next(float(line.split()[1]) for line in
                    Path("/proc/stat").read_text().splitlines() if line.startswith("btime "))
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration, AttributeError):
        return None


class Vault:
    KIND_DIRS = {
        "concept": "concepts", "area": "areas", "domain": "domains", "mistake": "mistakes",
        "problem": "problems", "assessment": "assessments", "application": "applications",
        "track": "tracks", "session": "sessions", "me": "me", "resource": "resources",
        "note": "notes", "project": "projects", "decision": "decisions",
        "journal": "journal", "loop": "loops", "person": "people",
        "browse": "browse",  # browse-brain.md §3: generated day pages; browse/private/ is not read
        "rollup": "rollups",  # cortex.rollups: generated week, month and year pages
    }
    # more folders a kind's pages may sit in, read like its own; a new page goes to the main
    # folder unless create_note names one of these (cortex.chat_archive)
    EXTRA_DIRS = {"resource": (CHAT_ARCHIVE,)}

    @classmethod
    def dirs(cls, kind: str) -> tuple[str, ...]:
        """A kind's folders, vault-relative: its own, then EXTRA_DIRS."""
        return (cls.KIND_DIRS[kind], *cls.EXTRA_DIRS.get(kind, ()))

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._tlock = threading.RLock()
        self._depth = 0
        self._jobs: set[tuple[str, int]] = set()  # (job lock, thread) held, see job_lock
        # path -> write number of its last write or removal, so a session close or a CLI
        # command commits only the files this process wrote (see wrote_since)
        self.written: dict[Path, int] = {}
        self.writes = 0

    def _track(self, path: Path) -> None:
        self.writes += 1
        self.written[Path(path)] = self.writes

    def wrote_since(self, mark: int) -> set[Path]:
        """Files written or removed through this Vault after `self.writes` was `mark`."""
        return {p for p, n in list(self.written.items()) if n > mark}

    # ---- paths ------------------------------------------------------------

    def path_for(self, kind: str, id: str) -> Path:
        if kind not in self.KIND_DIRS:
            raise VaultError("invalid_argument", f"Unknown kind '{kind}'; use one of "
                             f"{', '.join(self.KIND_DIRS)}.")
        if not isinstance(id, str) or not ID_RE.fullmatch(id):
            raise VaultError("invalid_id", "IDs must match ^[a-z0-9][a-z0-9.-]{0,80}$.")
        path = self._contained(self.root / self.KIND_DIRS[kind] / f"{id}.md")
        if not path.is_file():  # an extra folder's page; else the main folder's path
            return next((p for d in self.EXTRA_DIRS.get(kind, ())
                         if (p := self._contained(self.root / d / f"{id}.md")).is_file()), path)
        return path

    def _contained(self, path: Path) -> Path:
        p = Path(path)
        p = (p if p.is_absolute() else self.root / p).resolve()
        if not p.is_relative_to(self.root):
            raise VaultError("invalid_id", "The path must stay inside the vault.")
        return p

    # ---- reading ----------------------------------------------------------

    def _parse(self, path: Path, kind: str) -> Note:
        st = path.stat()  # before the read: a change after it misses the cache next time
        key = (st.st_mtime_ns, st.st_size)
        hit = _PARSED.get(path)
        if hit is None or hit[0] != key:
            text = path.read_bytes().decode("utf-8")
            parts = _split(text)
            fields = CommentedMap() if parts is None else _load_fm(parts[1])
            hit = _PARSED[path] = (key, fields, text if parts is None else parts[3])
        # shortcut: a shallow copy guards top-level edits only; nested lists are shared, and
        # no caller mutates them. Deep-copy here if one ever must.
        return Note(path, kind, path.stem, copy.copy(hit[1]), hit[2])

    def read(self, kind: str, id: str) -> Note:
        path = self.path_for(kind, id)
        if not path.is_file():
            raise VaultError("unknown_id", f"No {kind} note '{id}'; check the id with find.")
        try:
            return self._parse(path, kind)
        except Exception as e:  # YAML, fence or encoding errors
            raise VaultError("invalid_argument", f"{path.name} cannot be parsed: {e}") from e

    def read_path(self, path: Path) -> Note:
        """Read a vault file outside the kind folders, such as BRIEF.md, Today.md or inbox.md."""
        p = self._contained(path)
        if not p.is_file():
            raise VaultError("unknown_id", f"{p.name} does not exist in the vault.")
        kinds = {d: k for k, d in self.KIND_DIRS.items()}
        extra = {d: k for k, ds in self.EXTRA_DIRS.items() for d in ds}
        try:
            return self._parse(p, "file" if p.parent == self.root else
                               extra.get(p.parent.relative_to(self.root).as_posix())
                               or kinds.get(p.parent.name, "file"))
        except Exception as e:
            raise VaultError("invalid_argument", f"{p.name} cannot be parsed: {e}") from e

    def note_paths(self, kind: str) -> list[Path]:
        """The .md files of a kind folder. browse/ is read recursively (the site pages sit in
        browse/sites/<category>/) except its private/ subfolder, which is never read."""
        folder = self.root / self.KIND_DIRS[kind]
        if kind != "browse":
            return [p for d in self.dirs(kind) if (f := self.root / d).is_dir()
                    for p in f.glob("*.md")]
        if not folder.is_dir():
            return []
        hidden = self.root / PRIVATE_BROWSE_DIR
        return [p for p in folder.rglob("*.md") if not p.is_relative_to(hidden)]

    def iter_notes(self, kind: str | None = None) -> Iterator[Note]:
        kinds = [kind] if kind else list(self.KIND_DIRS)
        sites = self.root / self.KIND_DIRS["browse"] / "sites"
        for k in kinds:
            if k not in self.KIND_DIRS:
                raise VaultError("invalid_argument", f"Unknown kind '{k}'.")
            for path in sorted(self.note_paths(k)):
                # browse site pages (browse/sites/<category>/, up to tens of thousands) are
                # for find and recall, which read note_paths; whole-vault walks skip them (R2-10)
                if path.parent.parent == sites:
                    continue
                if not ID_RE.fullmatch(path.stem):
                    log.warning("skipping %s: file name is not a valid id", path)
                    continue
                try:
                    yield self._parse(path, k)
                except Exception as e:
                    log.warning("skipping %s: %s", path, e)

    # ---- lock ---------------------------------------------------------------

    @contextmanager
    def lock(self, timeout: float = 5.0):
        """Exclusive vault lock: .cortex/lock (O_EXCL) holding "pid ts host". Reentrant per
        Vault."""
        if not self._tlock.acquire(timeout=timeout):
            raise VaultError("vault_locked", "The vault is busy; try again in a moment.")
        try:
            if self._depth == 0:
                self._acquire_file(timeout)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    self._release_file()
        finally:
            self._tlock.release()

    @contextmanager
    def job_lock(self, rel: str, timeout: float, busy: str):
        """A lock file `rel` for a long job (a browse import), apart from the vault lock so
        short writes go on meanwhile: the same "pid ts host" file and stale rules (a crashed
        holder never leaves it stuck), reentrant per Vault and thread. VaultError("busy",
        `busy`) when another holder keeps it past `timeout`."""
        key = (rel, threading.get_ident())
        if key in self._jobs:
            yield
            return
        path = self.root / rel
        try:
            token = self._take(path, timeout)
        except VaultError:
            raise VaultError("busy", busy) from None
        self._jobs.add(key)
        try:
            yield
        finally:
            self._jobs.discard(key)
            self._drop(path, token)

    @property
    def _lock_path(self) -> Path:
        return self.root / ".cortex" / "lock"

    def _acquire_file(self, timeout: float) -> None:
        self._token = self._take(self._lock_path, timeout)

    def _take(self, path: Path, timeout: float) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        while True:
            token = f"{os.getpid()} {time.time():.3f} {socket.gethostname()}"
            _HELD.add(token)  # before the file shows it, so no reader takes it for a leftover
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileExistsError, PermissionError):
                _HELD.discard(token)
                if self._steal_if_stale(path):
                    continue
                if time.monotonic() >= deadline:
                    raise VaultError("vault_locked", "Another Cortex process holds the vault "
                                     "lock; try again in a moment.") from None
                time.sleep(0.05)
                continue
            with os.fdopen(fd, "w") as f:
                f.write(token)
            return token

    def _steal_if_stale(self, path: Path) -> bool:
        try:
            content = path.read_text()
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False  # vanished or being written; retry the create
        # a holder this host can see is stale when its pid is dead, or when the process with
        # that pid started after the lock was written (G4: a pid reused after a reboot); its
        # start time unknown, a lock under 10 x LOCK_STALE_S stays (R2-3: a long sites.write).
        # No readable pid, or another host's: the file's age decides
        try:
            pid_s, ts_s, *host = content.split(maxsplit=2)
            pid, ts = int(pid_s), float(ts_s)
            age = time.time() - ts
            local = not host or host[0] == socket.gethostname()
        except ValueError:
            local = False
        if local and pid == os.getpid():
            if content in _HELD:
                return False  # another Vault object or thread of this process holds it
        elif local and _pid_alive(pid):
            started = _pid_started(pid)
            if (started <= ts + 5 if started is not None  # 5 s of clock slack
                    else age <= 10 * LOCK_STALE_S):
                return False
        elif not local and age <= LOCK_STALE_S:
            return False
        # shortcut: a second stealer could race between this read and unlink; the window is
        # tiny and only opens after a crash. Upgrade to rename-then-verify if it ever bites.
        try:
            if path.read_text() == content:
                path.unlink()
                log.warning("stole a stale vault lock (%s)", content)
        except OSError:
            pass
        return True

    def _release_file(self) -> None:
        self._drop(self._lock_path, self._token)

    @staticmethod
    def _drop(path: Path, token: str) -> None:
        _HELD.discard(token)  # a file left behind is now a leftover this process may steal
        for attempt in range(REPLACE_TRIES):
            try:
                if path.read_text() == token:
                    path.unlink()
                return
            except FileNotFoundError:
                return
            except OSError:  # Windows: another process is reading the file; it lets go soon
                time.sleep(0.01 * (attempt + 1))

    # ---- writing --------------------------------------------------------------

    def _atomic_write(self, path: Path, text: str | bytes,
                      expect: os.stat_result | None = None) -> bool:
        """Temp file + os.replace with retry. Returns False if the file changed since `expect`."""
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(text if isinstance(text, bytes) else text.encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            if expect is not None:
                st = path.stat()
                if (st.st_mtime_ns, st.st_size) != (expect.st_mtime_ns, expect.st_size):
                    os.unlink(tmp)
                    return False
            for attempt in range(REPLACE_TRIES):
                try:
                    os.replace(tmp, path)
                    self._track(path)
                    return True
                except PermissionError:
                    if attempt == REPLACE_TRIES - 1:
                        raise VaultError("vault_locked", f"{path.name} is open in another "
                                         "program; try again.") from None
                    time.sleep(0.05 * 2**attempt)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return True  # unreachable

    def _mutate(self, path: Path, change: Callable[[str], str]) -> bool:
        """Lock, read, apply `change`, write; reread and reapply if the file changed meanwhile."""
        with self.lock():
            for _ in range(5):
                try:
                    st = path.stat()
                    text = path.read_bytes().decode("utf-8")
                except FileNotFoundError:
                    raise VaultError("unknown_id", f"{path.name} does not exist; check the "
                                     "id with find.") from None
                new = change(text)
                if new == text:
                    return False
                if self._atomic_write(path, new, expect=st):
                    return True
                log.info("%s changed during the edit; rereading", path)
            raise VaultError("vault_locked", f"{path.name} keeps changing; try again.")

    def _edit_fields(self, kind: str, id: str, values: dict) -> None:
        def change(text: str) -> str:
            parts = _split(text)
            if parts is None:
                raise VaultError("invalid_argument", f"{id} has no frontmatter.")
            head, fm, close, body = parts
            data = _load_fm(fm)
            for k, v in values.items():
                if v is None:
                    data.pop(k, None)
                else:
                    data[k] = _to_yaml(v)
            return head + _dump_fm(data, _newline(text)) + close + body

        self._mutate(self.path_for(kind, id), change)

    def set_field(self, kind: str, id: str, field: str, value) -> None:
        if field in TOOL_PROPS or field == "merged_into":
            raise VaultError("invalid_argument", f"'{field}' is computed by Cortex and cannot "
                             "be set by hand.")
        if field not in SET_FIELD_WHITELIST.get(kind, set()):
            allowed = sorted(SET_FIELD_WHITELIST.get(kind, set()))
            raise VaultError("invalid_argument", f"'{field}' cannot be set on a {kind}; "
                             f"settable fields: {', '.join(allowed)}.")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise VaultError("invalid_argument", "The value must be a string or a number.")
        allowed_values = ENUMS.get((kind, field))
        if allowed_values and value not in allowed_values:
            raise VaultError("invalid_argument", f"{field} must be one of "
                             f"{', '.join(sorted(allowed_values))}.")
        if (kind, field) == ("assessment", "date"):
            check_day(field, value)
        scan_secrets(str(value))
        self._edit_fields(kind, id, {field: value})

    def set_tool_props(self, kind: str, id: str, status: str, next_date: str | None) -> None:
        """Write cortex_status and cortex_next; a None next_date removes cortex_next."""
        self._edit_fields(kind, id, {"cortex_status": status, "cortex_next": next_date})

    def append_to_section(self, kind: str, id: str, heading: str, text: str) -> None:
        scan_secrets(text)

        def change(full: str) -> str:
            parts = _split(full)
            prefix = "".join(parts[:3]) if parts else ""
            body = parts[3] if parts else full
            nl = _newline(full)
            lines = body.splitlines(keepends=True)
            start, end = _section(lines, heading)
            at = start
            for j in range(end - 1, start - 1, -1):
                if lines[j].strip():
                    at = j + 1
                    break
            if at > 0 and not lines[at - 1].endswith("\n"):
                lines[at - 1] += nl
            new = text.replace("\r\n", "\n").rstrip("\n").replace("\n", nl) + nl
            if at < len(lines) and HEADING_RE.match(lines[at].rstrip("\r\n")):
                new += nl  # keep a blank line before the next heading
            lines.insert(at, new)
            return prefix + "".join(lines)

        self._mutate(self.path_for(kind, id), change)

    def check_item(self, kind: str, id: str, section: str, item_text: str) -> bool:
        """Tick the one unchecked item in `section` containing `item_text`.

        Returns False if it was already ticked; raises invalid_argument if none or several match.
        """
        changed = False

        def change(full: str) -> str:
            nonlocal changed
            parts = _split(full)
            prefix = "".join(parts[:3]) if parts else ""
            lines = (parts[3] if parts else full).splitlines(keepends=True)
            start, end = _section(lines, section)
            hits = [i for i in range(start, end)
                    if UNCHECKED_RE.match(lines[i]) and item_text in lines[i]]
            if len(hits) > 1:
                raise VaultError("invalid_argument", f"{len(hits)} items in '{section}' match; "
                                 "give more of the item text.")
            if not hits:
                if any(CHECKED_RE.match(lines[i]) and item_text in lines[i]
                       for i in range(start, end)):
                    changed = False
                    return full
                raise VaultError("invalid_argument", f"No unchecked item in '{section}' "
                                 "matches; check the item text.")
            lines[hits[0]] = lines[hits[0]].replace("[ ]", "[x]", 1)
            changed = True
            return prefix + "".join(lines)

        self._mutate(self.path_for(kind, id), change)
        return changed

    def create_note(self, kind: str, id: str, fields: dict, body: str,
                    folder: str | None = None) -> Path:
        """A new page in its kind's folder, or in `folder`, one of Vault.dirs(kind)."""
        path = self.path_for(kind, id)
        if folder is not None:
            if folder not in self.dirs(kind):
                raise VaultError("invalid_argument", f"A {kind} page cannot go in {folder}/.")
            if not path.exists():
                path = self._contained(self.root / folder / f"{id}.md")
        if fields.get("id", id) != id:
            raise VaultError("invalid_argument", "fields.id must match the note id.")
        data = CommentedMap((k, _to_yaml(v)) for k, v in {"id": id, **fields}.items())
        if body and not body.endswith("\n"):
            body += "\n"
        text = "---\n" + _dump_fm(data, "\n") + "---\n" + body
        scan_secrets(text)
        with self.lock():
            if path.exists():
                raise VaultError("invalid_argument", f"{kind} '{id}' already exists; patch it "
                                 "instead.")
            # ids are unique across kinds: the index keys nodes by id alone
            other = next((k for k in self.KIND_DIRS for d in self.dirs(k)
                          if (self.root / d / f"{id}.md").is_file()), None)
            if other:
                title = fields.get("title") or id
                raise VaultError("invalid_id", f"The id '{id}' is already the {other} "
                                 f"'{id}', and ids are unique across kinds (force does not "
                                 f"override this); use that note or choose another title, "
                                 f"such as '{title} {kind}' (id {id}-{kind}).", [id])
            path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(path, text)
        return path

    def append_journal(self, day: str, hhmm: str, text: str) -> None:
        """Add `- HH:MM text` under ## Log of the journal page for `day`, creating it if missing."""
        text = " ".join(text.split())
        scan_secrets(text)
        from cortex.relink import journal_up  # lazy: relink imports this module

        with self.lock():
            if not self.path_for("journal", day).is_file():
                up = journal_up(self, day)  # Obsidian's graph: day -> month -> topic map
                self.create_note("journal", day, {"title": f"Journal {day}", "kind": "journal",
                                                  "date": day},
                                 (f"Up: [[{up}]]\n\n" if up else "") + "## Log\n")
            self.append_to_section("journal", day, "Log", f"- {hhmm} {text}")

    def append_log(self, day: str, type: str, title: str) -> None:
        """Add `## [day] type | title` to log.md; earlier lines are never touched."""
        line = f"## [{day}] {type} | {' '.join(str(title).split())}\n"
        path = self.root / "log.md"
        with self.lock():
            old = path.read_bytes().decode("utf-8") if path.is_file() else "# Log\n\n"
            self.write_text(path, old + line)

    def remove(self, path: Path) -> bool:
        """Delete one vault file under the lock, retrying a Windows sharing error. False if it
        was not there."""
        p = self._contained(path)
        with self.lock():
            _PARSED.pop(p, None)
            for attempt in range(REPLACE_TRIES):
                try:
                    p.unlink()
                    self._track(p)
                    return True
                except FileNotFoundError:
                    return False
                except PermissionError:
                    if attempt == REPLACE_TRIES - 1:
                        raise VaultError("vault_locked", f"{path.name} is open in another "
                                         "program; try again.") from None
                    time.sleep(0.05 * 2**attempt)
        return False  # unreachable

    def move(self, src: Path, dst: Path) -> None:
        """Move one vault file under the lock: `dst` written atomically with `src`'s bytes and
        mtime, then `src` removed. A crash between leaves the same bytes in both, and a rerun
        finishes; VaultError when `dst` already holds something else."""
        src, dst = self._contained(src), self._contained(dst)
        with self.lock():
            st, data = src.stat(), src.read_bytes()
            if dst.exists() and dst.read_bytes() != data:
                raise VaultError("invalid_argument", f"{dst.name} is already in "
                                 f"{dst.parent.relative_to(self.root).as_posix()}/.")
            dst.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(dst, data)
            os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))  # pages keep their dates
            self.remove(src)

    def update_text(self, path: Path, change: Callable[[str], str], default: str = "") -> bool:
        """Read-modify-write a page the owner may edit at the same time (B13): `change` (which
        may run more than once) gets the text, or `default` if the file is missing, and an
        outside edit made meanwhile is reread instead of overwritten. False: nothing changed."""
        p = self._contained(path)

        def scanned(text: str) -> str:
            new = change(text)
            if new != text:
                scan_secrets(new)
            return new

        with self.lock():
            if p.is_file():
                return self._mutate(p, scanned)
            new = scanned(default)
            if new == default:
                return False
            p.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(p, new)
            return True

    def write_text(self, path: Path, text: str) -> Path:
        """Atomic write for generated files (Today.md, session notes, dashboard). Files under
        `.cortex/` are git-ignored local caches and views, never committed, so they skip the
        secret scan (scanning an 18.8 MB dashboard took ~10 s at 5k pages)."""
        p = self._contained(path)
        if not p.is_relative_to(self.root / ".cortex"):
            scan_secrets(text)
        with self.lock():
            p.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(p, text)
        return p
