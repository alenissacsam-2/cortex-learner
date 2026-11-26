"""The first-run move from Cortex (0.6.1 and older) to Keepfold (docs/INSTALL.md "Coming from
Cortex"; docs/design/v11/RENAME-PLAN.md R3):

- home: ~/.cortex becomes ~/.keepfold, one same-volume os.replace of the folder, only while
  ~/.keepfold is missing (never merged, never deleted);
- vault: <vault>/.cortex/ becomes .keepfold/ the same way, then its ignore lines, the notes'
  `cortex_status`/`cortex_next` keys, the Obsidian views that name them, old-named files in
  .keepfold/ and the vault's git identity follow, with a git commit before and after.

States: `nothing` (no old folder), `done`, `both` (both folders exist: the new one is used and
the old one left as it was), `blocked` (an old Cortex still holds files: nothing changed) and
`error`. A vault move is resumable: `.cortex/migrating` is written before the folder moves (it
moves along) and `.keepfold/migrated` (the version) replaces it once every step is done, so a
crash midway finishes on the next open.

When it runs: `at_start` (the `keepfold` console entry, before anything reads the home),
`launch` (the app and the receiver), `on_open` (every Vault(): its first construction per root
and process), and the `keepfold migrate` command (`everything`). The automatic runs cost one
stat (the home) and two (a vault) when nothing is left to move, and never run from a source
checkout: there `keepfold migrate` does it by hand.

Old names stay only here, in events.py's read-side renames and in `keepfold undo`'s commit
search."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

OLD, NEW = ".cortex", ".keepfold"
MARKER = "migrated"  # in the new folder: the version that finished the move
DOING = "migrating"  # written in the old vault folder before it moves: the move is unfinished
CLOSE = "Close Cortex and the AI apps using it, then open Keepfold again."
TRIES = 6  # os.replace attempts on PermissionError (OneDrive, antivirus): ~3 s in all
BACKOFF = 0.1  # seconds, doubled per attempt; tests set 0
PEER_WAIT = 10.0  # seconds to wait for another Keepfold process that is moving the vault now
KEYS = {"cortex_status": "keepfold_status", "cortex_next": "keepfold_next"}
OLD_IDENTITY, NEW_IDENTITY = ("Cortex", "cortex@localhost"), ("Keepfold", "keepfold@localhost")
RANK = ("nothing", "done", "both", "blocked", "error")  # overall state: the worst one

_SEEN: set[str] = set()  # vault roots checked in this process (on_open's fast path)
_MU = threading.RLock()  # one move at a time in this process
_IN = threading.local()  # .roots: the vaults this thread is moving (our own Vault() skips them)
_dev: bool | None = None


def source_checkout() -> bool:
    """Run from a source checkout (an editable dev install: pyproject.toml beside src/keepfold):
    nothing moves by itself there, so dev runs never touch a real ~/.cortex."""
    global _dev
    if _dev is None:
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        _dev = os.path.isfile(os.path.join(here, "pyproject.toml"))
    return _dev


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("keepfold")
    except PackageNotFoundError:
        return "unknown"


def _fresh(lock: Path) -> bool:
    """`lock` was written in the last LOCK_STALE_S: its process is active."""
    from keepfold.vault import LOCK_STALE_S
    try:
        return time.time() - lock.stat().st_mtime < LOCK_STALE_S
    except OSError:
        return False


def _replace(old: Path, new: Path) -> str:
    """os.replace(old, new), retried with backoff on PermissionError: "moved", "peer" (another
    Keepfold process moved it first) or "blocked" (a file in it stays open)."""
    for attempt in range(TRIES):
        try:
            os.replace(old, new)
            return "moved"
        except PermissionError:
            pass
        except OSError:
            if new.exists() and not old.exists():
                return "peer"
            raise
        if new.exists() and not old.exists():
            return "peer"
        if attempt < TRIES - 1:
            time.sleep(BACKOFF * 2 ** attempt)
    return "blocked"


def _blocked(what: Path) -> dict:
    return {"state": "blocked", "message": f"{what} is in use by an old Cortex. {CLOSE}"}


def _both(old: Path, new: Path) -> dict:
    return {"state": "both", "old": str(old), "new": str(new),
            "message": f"Both {old} and {new} exist. Keepfold uses {new} and left {old} as it "
                       f"was (nothing is merged). If {old} holds what you want, close Keepfold, "
                       f"rename {new}, then open Keepfold again."}


# ---- home ---------------------------------------------------------------------------------


def _old_app(old: Path) -> bool:
    """The old Cortex app runs: its ~/.cortex/app.json names a port that answers its token."""
    import http.client
    import json
    try:
        s = json.loads((old / "app.json").read_text(encoding="utf-8"))
        port, token = int(s["port"]), str(s["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        c.request("GET", "/api/job", headers={"Host": f"127.0.0.1:{port}",
                                              "X-Cortex-Token": token})
        return c.getresponse().status == 200
    except OSError:
        return False
    finally:
        c.close()


def home() -> dict:
    """Move ~/.cortex to ~/.keepfold when only the old one exists. No file inside the old home
    has an old name the code looks up (logs, models, tmp, app.json, config.yaml), so none is
    renamed."""
    h = Path.home()
    old, new = h / OLD, h / NEW
    out = {"old": str(old), "new": str(new)}
    if not old.is_dir():
        return {**out, "state": "done" if (new / MARKER).is_file() else "nothing"}
    if new.exists():
        return {**out, **_both(old, new)}
    # on Windows an open file (the old app's log) also fails the move; this catches the rest
    if _fresh(old / "config.lock") or _old_app(old):
        return {**out, **_blocked(old)}
    got = _replace(old, new)
    if got == "blocked":
        return {**out, **_blocked(old)}
    if got == "moved":
        try:
            (new / MARKER).write_text(_version() + "\n", encoding="utf-8")
        except OSError:
            pass  # only doctor's "done" reads it
    return {**out, "state": "done"}


# ---- vault --------------------------------------------------------------------------------


def _take_old(v, lock: Path, wait: float) -> str | None:
    """The old folder's lock as 0.6.1 takes it (O_EXCL, "pid ts host"), so no Cortex or other
    Keepfold process writes there meanwhile. Unlike Vault._take it never creates the folder (a
    peer may have just moved it). None: the folder is gone, or the lock is still held after
    `wait`."""
    import socket
    deadline = time.monotonic() + wait
    while True:
        token = f"{os.getpid()} {time.time():.3f} {socket.gethostname()}"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileNotFoundError:
            return None
        except (FileExistsError, PermissionError):
            if v._steal_if_stale(lock):
                continue
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)
            continue
        with os.fdopen(fd, "w") as f:
            f.write(token)
        return token


def _old_busy(old: Path) -> bool:
    """A Cortex process is active in the vault's old folder: one of its locks is fresh. While a
    Keepfold process moves it (DOING), its lock is waited for instead (_take_old)."""
    if (old / DOING).exists():
        return False
    return any(_fresh(p) for p in (old / "lock", *old.glob("*.lock"), old / "browse" / "lock"))


def _ignore_text(text: str, final: bool) -> str:
    """Ignore rules with a `.keepfold` line after each `.cortex` one. `final` (the old folder is
    gone): the `.cortex` lines become the `.keepfold` ones, plus one `.cortex/` line, so a copy
    a stray old process makes again is never committable either."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines, out = text.splitlines(), []
    for line in lines:
        if OLD not in line:
            out.append(line)
            continue
        if not final:
            out.append(line)
        new = line.replace(OLD, NEW)
        if new not in lines and new not in out:
            out.append(new)
    if final and f"{OLD}/" not in out:
        out.append(f"{OLD}/")
    return nl.join(out) + nl if out else text


def _ignore(v, final: bool) -> None:
    """.gitignore (and .git/info/exclude, if it has them): `.keepfold` lines for `.cortex` ones.
    Called with the old lock held, before the folder moves, so .keepfold/ is ignored from its
    first moment (private browsing data never becomes committable); again at the end."""
    for p in (v.root / ".gitignore", v.root / ".git" / "info" / "exclude"):
        try:
            text = p.read_bytes().decode("utf-8")
        except (OSError, ValueError):
            continue
        if OLD in text and (new := _ignore_text(text, final)) != text:
            v._atomic_write(p, new)  # under the old or the vault lock (callers hold one)


def _key_change(text: str) -> str:
    """`cortex_status`/`cortex_next` front-matter keys renamed in place (line by line, so
    nothing else in the front matter changes); when the new key is there already, the old line
    goes."""
    import re

    from keepfold.vault import _split
    parts = _split(text)
    if parts is None:
        return text
    head, fm, close, body = parts
    for old, new in KEYS.items():
        if re.search(rf"(?m)^{new}\s*:", fm):
            fm = re.sub(rf"(?m)^{old}\s*:.*(?:\r?\n|\Z)", "", fm)
        else:
            fm = re.sub(rf"(?m)^{old}(?=\s*:)", new, fm)
    return head + fm + close + body


def _keys(v) -> tuple[int, int]:
    """Every note with an old key rewritten through Vault._mutate (lock, temp file, os.replace,
    and an owner's edit meanwhile is reread). (rewritten, skipped: unreadable front matter)."""
    from keepfold.vault import Vault, VaultError
    done = skipped = 0
    for kind in Vault.KIND_DIRS:
        if kind == "browse":  # generated site pages carry no tool keys; browse/ is never read
            continue
        for p in v.note_paths(kind):
            try:
                if b"cortex_" in p.read_bytes() and v._mutate(p, _key_change):
                    done += 1
            except (OSError, ValueError, VaultError):
                skipped += 1  # the page keeps its old key; nothing is lost
    return done, skipped


def _views(v) -> None:
    """Keepfold's Obsidian views (views/*.base), graph colours and property types name the keys
    too."""
    def change(text: str) -> str:
        for old, new in KEYS.items():
            text = text.replace(old, new)
        return text

    from keepfold.vault import VaultError
    for p in [*(v.root / "views").glob("*.base"), v.root / ".obsidian" / "graph.json",
              v.root / ".obsidian" / "types.json"]:
        try:
            if p.is_file() and b"cortex_" in p.read_bytes():
                v._mutate(p, change)
        except (OSError, ValueError, VaultError):
            pass  # a view the owner broke stays as it is


def _old_names(v) -> None:
    """Files in .keepfold/ whose names or contents held the old names: backups `cortex-*.zip`
    (keepfold.backup and forget look for `keepfold-*.zip`) and the X-ray ring, a trimmed cache
    whose rows the X-ray tab filters by tool name (`cortex_find`) and app (`cortex-app`). The
    index, vector stores and other caches have no old names; changed pages are reindexed."""
    new = v.root / NEW
    for p in sorted((new / "backups").glob("cortex-*.zip")):
        dest = p.with_name("keepfold-" + p.name.removeprefix("cortex-"))
        if not dest.exists():
            os.replace(p, dest)  # one rename in .keepfold/, under the vault lock
    ring = new / "xray.jsonl"
    try:
        text = ring.read_bytes().decode("utf-8")
    except (OSError, ValueError):
        return
    fixed = text.replace('"tool":"cortex_', '"tool":"keepfold_').replace(
        '"client":"cortex-app"', '"client":"keepfold-app"')
    if fixed != text:
        v.write_text(ring, fixed)


def _identity(root: Path) -> None:
    """The vault repo's own identity: Cortex <cortex@localhost> becomes Keepfold's."""
    from keepfold import session
    if not (root / ".git").is_dir() or session.git_exe() is None:
        return
    if session._git(root, "config", "--local", "user.email").stdout.strip() != OLD_IDENTITY[1]:
        return
    if session._git(root, "config", "--local", "user.name").stdout.strip() == OLD_IDENTITY[0]:
        session._git(root, "config", "--local", "user.name", NEW_IDENTITY[0])
    session._git(root, "config", "--local", "user.email", NEW_IDENTITY[1])


def _finish(v) -> dict:
    """Every step after the folder move, under the vault lock; each is idempotent, so a rerun
    after a crash finishes the work. Returns counts."""
    from keepfold import session
    new = v.root / NEW
    with v.lock(PEER_WAIT):  # another Keepfold process may be finishing it now
        if not (new / DOING).is_file() and (new / MARKER).is_file():
            return {}  # another process finished it meanwhile
        _ignore(v, final=not (v.root / OLD).exists())
        _identity(v.root)
        # what is there first gets its own commit, so the key rewrite can be undone
        session._commit(v, "keepfold: before the move from Cortex")
        _old_names(v)
        mark = v.writes
        notes, skipped = _keys(v)
        _views(v)
        session._commit(v, "keepfold: moved from Cortex", v.wrote_since(mark))
        v.write_text(new / MARKER, _version() + "\n")
        if (new / DOING).exists():
            v.remove(new / DOING)
    return {"notes": notes, "skipped": skipped}


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def vault(root: Path) -> dict:
    """Move one vault's .cortex/ to .keepfold/ and finish the steps after it."""
    from keepfold.vault import Vault, VaultError
    root = Path(root).resolve()
    old, new = root / OLD, root / NEW
    out = {"path": str(root)}
    if _same(root, Path.home()):  # ~/.cortex there is the settings folder: home() moves it
        return {**out, "state": "nothing"}
    roots = getattr(_IN, "roots", None) or set()
    _IN.roots = roots
    with _MU:
        roots.add(str(root))  # our own Vault(root) below must not start this again
        try:
            return {**out, **_vault(Vault(root), old, new)}
        except Exception as e:  # noqa: BLE001  (reported in plain words; nothing is lost)
            if isinstance(e, VaultError) and e.code == "vault_locked":
                return {**out, "state": "blocked", "message": f"{root} is busy: another "
                        f"program holds its files. {CLOSE}"}
            return {**out, "state": "error",
                    "message": f"Moving {old} failed ({type(e).__name__}); nothing was "
                               "deleted. Run `keepfold migrate` to try again."}
        finally:
            roots.discard(str(root))


def _vault(v, old: Path, new: Path) -> dict:
    from keepfold.vault import Vault
    if old.is_dir():
        if new.exists():
            if (new / MARKER).is_file():
                return _both(old, new)
            if _old_busy(old):
                return _blocked(old)
            return {**_both(old, new), **_finish(v)}  # keys, ignore lines, identity still apply
        if _old_busy(old):
            return _blocked(old)
        token = _take_old(v, old / "lock", PEER_WAIT)
        if token is None and old.is_dir():
            return _blocked(old)
        if token is not None:
            got = "blocked"
            try:
                v._atomic_write(old / DOING, _version() + "\n")
                _ignore(v, final=False)  # before .keepfold/ exists: it is ignored from the start
                got = _replace(old, new)
            finally:
                for p in (old / "lock", new / "lock"):  # wherever our lock now is
                    Vault._drop(p, token)
                if got != "moved" and old.is_dir():
                    (old / DOING).unlink(missing_ok=True)
            if got == "blocked":
                return _blocked(old)
    if (new / DOING).is_file():
        return {"state": "done", **_finish(v)}
    return {"state": "done" if (new / MARKER).is_file() else "nothing"}


def on_open(root: Path) -> str | None:
    """Vault()'s check, on its first construction per root in this process: before anything
    reads or writes .keepfold/, so an empty one never blocks the move. Returns why the vault
    cannot open yet (blocked, error), else None. Two stats when nothing is left to move."""
    key = str(root)
    if key in _SEEN or key in (getattr(_IN, "roots", None) or ()):
        return None
    if source_checkout() or (not os.path.isdir(os.path.join(key, OLD))
                             and not os.path.isfile(os.path.join(key, NEW, DOING))):
        _SEEN.add(key)
        return None
    r = vault(root)
    if r["state"] in ("blocked", "error"):
        return r["message"]
    _SEEN.add(key)
    return None


# ---- entry points ------------------------------------------------------------------------


APPS = "apps-migrated"  # in ~/.keepfold: the AI apps were repointed (the app's start skips it)


def _apps() -> dict:
    """The AI-app connections made under the old names, repointed
    (keepfold.app.targets.migrate_old_entries): {state, rows, message}."""
    try:
        from keepfold.app import targets
        rows = targets.migrate_old_entries()
    except Exception as e:  # noqa: BLE001  (an app part that fails must not stop the move)
        return {"state": "error", "rows": [], "message": f"Repointing the AI apps failed "
                f"({type(e).__name__}); run `keepfold doctor` to see what is left."}
    errors = [r for r in rows if r["action"] == "error"]
    left = [r for r in rows if r["action"] == "left"]
    message = " ".join(f"{r['app']}: {r['note']}" for r in errors) or (
        "Some AI-app entries were not Keepfold's own and were left as they were; "
        "`keepfold doctor` lists them." if left else "")
    state = "error" if errors else "done" if any(r["action"] == "moved" for r in rows) \
        else "nothing"
    return {"state": state, "rows": rows, "message": message}


def _overall(*states: str) -> str:
    return max(states or ("nothing",), key=RANK.index)


def _summary(state: str, parts: list[dict]) -> str:
    """One plain line for the installer's window: the first part in the overall state."""
    for p in parts:
        if p.get("state") == state and p.get("message"):
            return p["message"]
    return {"done": "Your Cortex settings, notes and AI-app connections now use Keepfold.",
            "nothing": "Nothing from Cortex was left to move."}.get(state, "")


def everything(roots: list[Path] | None = None, apps: bool = True) -> dict:
    """`keepfold migrate` and the app's start: the home, then each vault (default: the
    configured one), then (with `apps`) the AI apps. `exit`: 0 done or nothing to do, 2
    blocked, 1 error; `message`: one plain line."""
    h = home()
    out = {"home": h, "vaults": [], "apps": None}
    if h["state"] not in ("blocked", "error"):
        if roots is None:
            from keepfold import taint
            roots = [r] if (r := taint._vault_root()) else []
        for r in roots:
            if Path(r).is_dir():
                out["vaults"].append(vault(Path(r)))
            else:
                out["vaults"].append({"path": str(r), "state": "nothing",
                                      "message": f"{r} does not exist."})
        if apps:
            out["apps"] = _apps()
            if out["apps"]["state"] != "error" and (Path.home() / NEW).is_dir():
                try:
                    (Path.home() / NEW / APPS).write_text(_version() + "\n", encoding="utf-8")
                except OSError:
                    pass  # the app's start repoints again next time; a second run finds nothing
    parts = [h, *out["vaults"], *([out["apps"]] if out["apps"] else [])]
    state = _overall(*(p["state"] for p in parts))
    return {"state": state, "exit": {"error": 1, "blocked": 2}.get(state, 0),
            "message": _summary(state, parts), **out}


def at_start(argv: list[str]) -> int | None:
    """The `keepfold` console entry, before anything reads the home: one stat when there is no
    ~/.cortex. Returns an exit code to stop with (an old Cortex holds the home), else None. The
    hooks never print and stop quietly; `migrate` does the move itself and `doctor` reports it;
    a source checkout only prints a hint."""
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "migrate" or not os.path.isdir(os.path.join(Path.home(), OLD)):
        return None
    hook = cmd in ("taint", "prime-hook")
    if source_checkout():
        if not hook and not os.path.exists(os.path.join(Path.home(), NEW)):
            print("keepfold: Cortex settings found in ~/.cortex; run `keepfold migrate` to move "
                  "them to ~/.keepfold.", file=sys.stderr)
        return None
    r = home()
    if r["state"] not in ("blocked", "error") or cmd in ("doctor", "", "--help", "--version"):
        return None
    if not hook:
        print(f"keepfold: {r['message']}", file=sys.stderr)
    return 0 if hook else 2


def launch() -> str | None:
    """The app's and the receiver's start: the home, the configured vault and the AI apps. The
    message to show when something blocks or fails, else None."""
    if source_checkout():
        return None
    new = Path.home() / NEW
    # the AI apps once, after a move from Cortex (the installer's `migrate` usually did it)
    r = everything(apps=(new / MARKER).is_file() and not (new / APPS).is_file())
    for part in (r["home"], *r["vaults"]):  # an AI app that failed is doctor's, not a stop
        if part["state"] in ("blocked", "error"):
            return part["message"]
    return None


# ---- doctor and Status ----------------------------------------------------------------------


def status(root: Path | None) -> tuple[str, str, str]:
    """(state ok|warn, value, what to do) for doctor's and the Status page's "Moved from Cortex"
    row, read-only: it never moves anything."""
    h = Path.home()
    parts = [("Settings", h / OLD, h / NEW, h / NEW / MARKER, None)]
    if root is not None and not _same(root, h):
        parts.append(("Vault", root / OLD, root / NEW, root / NEW / MARKER, root / NEW / DOING))
    states, notes = [], []
    for name, old, new, marker, doing in parts:
        if doing is not None and doing.is_file():
            states.append("pending")
            notes.append(f"{name}: the move from {old} did not finish. {CLOSE}")
        elif old.is_dir() and new.exists():
            states.append("both")
            notes.append(f"{name}: {_both(old, new)['message']}")
        elif old.is_dir():
            states.append("pending")
            notes.append(f"{name}: {old} is not moved yet. {CLOSE}" + (
                "" if not source_checkout() else " (From a source checkout: `keepfold migrate`.)"))
        else:
            states.append("done" if marker.is_file() else "nothing")
    if "pending" in states:
        return "warn", "not moved yet", " ".join(notes)
    if "both" in states:
        return "warn", "both folders exist", " ".join(notes)
    if "done" in states:
        return "ok", "done", ""
    return "ok", "nothing to move", ""


def old_env() -> list[str]:
    """CORTEX_* variables still set: Keepfold reads only KEEPFOLD_* (no fallback)."""
    return sorted(k for k in os.environ if k.upper().startswith("CORTEX_"))
