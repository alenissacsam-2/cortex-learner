"""`cortex taint`: the plugin's PostToolUse hook records that Claude read outside text with a
tool that is not Cortex (web, browser, mail, other MCP servers). The MCP server treats a fresh
marker as exposure (session.SessionManager._check_taint), so claim tiers see it.

This module is also the `cortex` console entry: `cortex taint` runs here without importing
cortex.cli (typer and every core module), since the hook fires on each matched tool call.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

MARKER = ".cortex/taint"
# Cortex's own servers, the Claude desktop app's one-word `ccd_*` servers (not ccd_session_mgmt:
# it returns other chats' text), widgets, schedules and the savvy-progress bar, whose tools
# return no outside text (review r4 M4).
# Desktop Commander, Windows-MCP, browsers and connectors still count. plugin/hooks/hooks.json's
# matcher skips the same names (tests/test_taint.py checks they agree); this is the backstop.
EXCLUDED = re.compile(
    r"^mcp__(cortex|plugin_cortex_cortex|ccd_[^_]*|visualize|scheduled-tasks|"
    r"savvy-progress)__")


def _config_path() -> Path:
    return Path.home() / ".cortex" / "config.yaml"


# a config pinned for one thread's work (cortex.chat_eval): config() returns it there instead of
# the file, and write_config refuses, so a made-up run never reads or changes the owner's settings
_PINNED: ContextVar[dict | None] = ContextVar("cortex_pinned_config", default=None)


@contextmanager
def pinned(data: dict):
    """Within the block, in this thread (and context) only, config() is a copy of `data`."""
    token = _PINNED.set(data)
    try:
        yield
    finally:
        _PINNED.reset(token)


def config() -> dict:
    """~/.cortex/config.yaml as a dict ({} when missing or broken); `pinned`'s dict within it."""
    if (p := _PINNED.get()) is not None:
        import copy
        return copy.deepcopy(p)
    from ruamel.yaml import YAML, YAMLError  # cortex.vault loads it anyway
    try:
        data = YAML(typ="safe").load(_config_path().read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def write_config(data: dict | Callable[[dict], dict]) -> Path:
    """Set `data`'s keys in ~/.cortex/config.yaml, keeping its other keys. `data` may be a
    function: it gets the config as read under the lock and returns the keys to set, so a
    read-merge-write of a sub-dict (`browse:`, `app:`) cannot lose a concurrent writer's change.
    Refuses to overwrite a config that exists, is not empty and does not parse to a dict (a merge
    with {} would wipe `vault:` and `exclude:`). Under ~/.cortex/config.lock: mkstemp, then
    os.replace with retry; an unchanged config is not rewritten."""
    import copy
    import tempfile

    from ruamel.yaml import YAML, YAMLError

    from cortex.vault import LOCK_STALE_S, REPLACE_TRIES, VaultError

    if _PINNED.get() is not None:
        raise VaultError("invalid_argument", "Settings cannot change during a pinned run.")
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name("config.lock")
    deadline = time.monotonic() + 5
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except (FileExistsError, PermissionError) as e:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_S:  # its writer died
                    lock.unlink()
                    continue
            except OSError:
                pass  # gone, or never creatable (R2-5): wait out the deadline, never spin
            if time.monotonic() >= deadline:
                why = ("is held by another Cortex process" if lock.exists()
                       else f"cannot be created ({e.strerror or e})")
                raise VaultError("vault_locked", f"{lock} {why}; try again in a moment.") from None
            time.sleep(0.05)
    try:
        try:
            old = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            old = {}
        except (OSError, ValueError, YAMLError) as e:
            raise VaultError("invalid_argument", f"{path} cannot be read ({e}); fix it (UTF-8 "
                             "YAML with key: value lines) or delete it, then try again.") from e
        if not isinstance(old, dict):
            raise VaultError("invalid_argument", f"{path} is not a key: value mapping; fix it "
                             "or delete it, then try again.")
        # a copy: a function that edits a sub-dict in place must not make `new == old`
        new = {**old, **(data(copy.deepcopy(old)) if callable(data) else data)}
        if new == old:
            return path
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                YAML(typ="safe").dump(new, f)
            for attempt in range(REPLACE_TRIES):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == REPLACE_TRIES - 1:
                        raise
                    time.sleep(0.05 * 2**attempt)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    finally:
        lock.unlink(missing_ok=True)
    return path


def _vault_root() -> Path | None:
    if env := os.environ.get("CORTEX_VAULT"):
        return Path(env)
    v = config().get("vault")
    return Path(v) if v else None


def exclude_list(cfg: dict | None = None) -> list[str]:
    ex = (config() if cfg is None else cfg).get("exclude") or []
    return [str(e) for e in ([ex] if isinstance(ex, str) else ex) if e]


def excluded(*dirs) -> str | None:
    """The `exclude:` folder of ~/.cortex/config.yaml that holds one of `dirs` (resolved, by
    whole path components; case-insensitive on Windows), or None."""
    folders = [Path(e).expanduser().resolve() for e in exclude_list()]
    for d in dirs:
        if d:
            p = Path(d).resolve()
            for f in folders:
                if p == f or f in p.parents:
                    return str(f)
    return None


def session_dirs(cwd=None) -> tuple:
    """The folders a Claude Code session runs in: `cwd` (the hook payload's, else the process's)
    and CLAUDE_PROJECT_DIR when Claude Code sets it."""
    return cwd or os.getcwd(), os.environ.get("CLAUDE_PROJECT_DIR")


def run(raw: str) -> bool:
    """Write the marker for one hook payload; True if written. Never raises: a hook must not
    fail the tool call it follows."""
    try:
        hook = json.loads(raw)
        tool = hook.get("tool_name")
        if not isinstance(tool, str) or not tool or EXCLUDED.match(tool):
            return False
        root = _vault_root()
        if root is None or not root.is_dir():
            return False
        from cortex.vault import Vault
        sid = hook.get("session_id")
        Vault(root).write_text(root / MARKER, json.dumps(
            {"ts": time.time(), "tool_name": tool[:200],
             "session_id": sid[:200] if isinstance(sid, str) else None}) + "\n")
        return True
    except Exception:  # noqa: BLE001  (garbage stdin, a busy vault lock, a secret-scan refusal)
        return False


def read_stdin() -> str:
    try:
        return sys.stdin.buffer.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001  (no stdin, or a text-only stream)
        return ""


def main() -> None:
    """The `cortex` console script."""
    if sys.argv[1:2] == ["taint"]:
        run(read_stdin())
        return  # exit 0, nothing on stdout
    if sys.argv[1:2] == ["prime-hook"]:  # fires on every prompt: skip the CLI import too
        from cortex import prime_hook
        if out := prime_hook.run(read_stdin()):
            sys.stdout.write(out)
        if prime_hook.pending is not None and prime_hook.pending.is_alive():
            sys.stdout.flush()  # a prime still running past its budget must not hold the exit
            os._exit(0)
        return
    from cortex.cli import app
    app()
