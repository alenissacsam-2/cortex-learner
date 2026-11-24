"""Read-only checks behind the app's sections, and thin adapters to `cortex.localllm` and the
browse code (both still growing: every call is guarded, so a missing function shows as "not
available yet"). Nothing here writes, and nothing from browsing leaves as text: only counts,
states, dates and the owner's own settings (`safe` filters the browse answers)."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cortex.vault import VaultError, in_temp_folder

TEMP_VAULT = ("This vault is in a temporary folder. Windows can empty it at any time. "
              "Use Change folder to pick your real vault.")

log = logging.getLogger(__name__)
LOGIN_TASK = "Cortex browse receiver"
LOGIN_ID = "cortex-browse-receiver"  # LaunchAgent label, unit and .desktop name
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)?")


def config() -> dict:
    from cortex import taint
    return taint.config()


def safe(d) -> dict:
    """Only numbers, booleans and date strings: what a browse answer may show the page."""
    if not isinstance(d, dict):
        return {}
    return {str(k): v for k, v in d.items()
            if isinstance(v, (bool, int, float)) or (isinstance(v, str) and _DATE.fullmatch(v))}


# The Status board's slow probes (HTTP to Ollama and LM Studio, `lms`: seconds together on
# Windows, where a refused loopback connect waits) are kept for KEEP_S: the first
# read waits, later ones answer at once and refresh a stale answer in the background. `forget`
# drops them all (any action, and Check again), so the next read waits for fresh answers.
KEEP_S = 30.0
_kept: dict = {}  # key -> [monotonic time, answer, refreshing]
_kept_lock = threading.Lock()


def kept(key, fn):
    """`fn()`'s answer kept under `key` (see KEEP_S)."""
    with _kept_lock:
        hit = _kept.get(key)
        if hit is not None:
            if time.monotonic() - hit[0] > KEEP_S and not hit[2]:
                hit[2] = True
                threading.Thread(target=_refresh, args=(key, fn, hit), daemon=True).start()
            return hit[1]
    v = fn()
    with _kept_lock:
        _kept[key] = [time.monotonic(), v, False]
    return v


def _refresh(key, fn, hit: list) -> None:
    try:
        v = fn()
    except Exception:  # noqa: BLE001  (the probes catch their own; a bug keeps the old answer)
        log.exception("status probe refresh failed")
        hit[2] = False
        return
    with _kept_lock:
        if _kept.get(key) is hit:  # not forgotten meanwhile
            _kept[key] = [time.monotonic(), v, False]


def forget() -> None:
    with _kept_lock:
        _kept.clear()


def when(p: Path) -> str | None:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return None


# ---- vault ----------------------------------------------------------------------------------


def notes(root: Path) -> int:
    """Markdown pages in the vault, outside dot folders and browse/ (counted, never read)."""
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        top = Path(dirpath) == root
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and not (top and d == "browse")]
        n += sum(f.endswith(".md") for f in filenames)
    return n


def vault(root: Path | None) -> dict:
    if root is None:
        return {"path": None, "ok": False}
    ok = (root / ".cortex").is_dir()
    return {"path": str(root), "ok": ok, "exists": root.is_dir(), "temp": in_temp_folder(root),
            "notes": notes(root) if ok else 0,
            "last_index": indexed(root),
            "backups": len(list((root / ".cortex" / "backups").glob("*.zip"))) if ok else 0,
            "last_backup": max((when(p) or "" for p in (root / ".cortex" / "backups")
                                .glob("*.zip")), default=None) if ok else None}


def indexed(root: Path) -> str | None:
    """When the notes were last indexed: the vector cache "Index my notes" writes. The one
    answer Setup, Status and Dashboard and vault all show."""
    from cortex import semantic
    p = root / semantic.CACHE  # a save lands in its -wal first
    return max(filter(None, (when(p), when(Path(f"{p}-wal")) if p.is_file() else None)),
               default=None)


def embedding(root: Path | None = None) -> dict:
    from cortex import doctor, semantic
    d = semantic.models_dir()
    out = {"downloaded": doctor.search_model_ready(), "dir": str(d)}
    if root is not None:
        out["indexed"] = indexed(root)
    return out


# ---- local model (adapter to cortex.localllm) -----------------------------------------------


def _llm_cfg() -> dict:
    c = config().get("localllm")
    return c if isinstance(c, dict) else {}


def ollama_models() -> list[str] | None:
    """Ollama's local model names (cloud ones left out), or None when it does not answer."""
    try:
        from cortex import localllm
        data = localllm._request("http://127.0.0.1:11434/api/tags", None, 0.5)
        names = [r.get("name") for r in data.get("models", []) if isinstance(r, dict)]
        return [n for n in names if isinstance(n, str) and not localllm._cloud(n)]
    except (OSError, ValueError):  # not running (refused, timeout) or not JSON
        return None
    except Exception:  # noqa: BLE001  (a bug: logged, still "not available")
        log.exception("Ollama check failed")
        return None


def lms_path() -> str | None:
    try:
        from cortex import localllm
        exe = localllm._lms_exe(_llm_cfg())
        return str(exe) if exe else None
    except OSError:
        return None
    except Exception:  # noqa: BLE001
        log.exception("lms lookup failed")
        return None


def _lms_state() -> dict:
    """LM Studio's server port, loaded models and localllm's error, read-only."""
    out = {"server_port": None, "loaded": [], "available": True, "error": None}
    try:
        from cortex import localllm
        got = localllm.status(lms=True)  # read-only; "error": a cloud model or non-local url
        st, out["error"] = got["lmstudio"], got["error"]
        if st:
            out["server_port"] = st.get("port")
            out["loaded"] = [str(r.get("identifier") or r.get("modelKey"))
                             for r in st.get("loaded") or []]
    except ImportError:
        out["available"] = False
    except (OSError, ValueError):  # lms missing a command, a slow LM Studio
        pass
    except Exception:  # noqa: BLE001
        log.exception("LM Studio state check failed")
    return out


def llm(quick: bool = False) -> dict:
    """The local model's settings and both runners' state; `quick`: the probes as `kept`."""
    c = _llm_cfg()
    key = json.dumps(c, sort_keys=True, default=str)  # a settings change asks again

    def probe(name, fn):
        return kept((name, key), fn) if quick else fn()
    return {"enabled": c.get("enabled") is True, "runner": c.get("runner") or "auto",
            "model": c.get("model") or None,
            "background_model": c.get("background_model") or None,
            "lms": probe("lms", lms_path), "ollama": probe("ollama", ollama_models),
            **probe("lms_state", _lms_state)}


def lms_models() -> list[dict]:
    """`lms ls`: the LLMs on this machine (never ones on another device)."""
    try:
        from cortex import localllm
        exe = localllm._lms_exe(_llm_cfg())
        rows = localllm._rows(localllm._lms(exe, "ls", "--json")) if exe else []
    except OSError:
        return []
    except Exception:  # noqa: BLE001
        log.exception("lms ls failed")
        return []
    return [{"key": str(r.get("modelKey")), "name": str(r.get("displayName") or r.get("modelKey")),
             "size": r.get("sizeBytes") if isinstance(r.get("sizeBytes"), int) else None,
             "params": str(r.get("paramsString") or "")}
            for r in rows if r.get("modelKey") and r.get("type", "llm") == "llm"
            and not r.get("deviceIdentifier")]


# ---- browsing (adapter to cortex.browse and cortex.browse_serve): counts and states only ----


def browse_settings() -> dict:
    try:
        from cortex.browse import lists, load_settings
        from cortex.browse_serve import auto_import_hours
        s = load_settings()
    except (ImportError, OSError, ValueError):  # no browse code, an unreadable config
        return {"available": False}
    except Exception:  # noqa: BLE001
        log.exception("browse settings failed")
        return {"available": False}
    from cortex.browse.privacy import audience
    return {"available": True, "visible_to_claude": s.visible_to_claude, "adult": s.adult,
            "sensitive": s.sensitive, "deny": list(s.deny), "allow": list(s.allow),
            "private": list(s.private), "categories": list(lists.CATEGORIES),
            "hidden_from_claude": list(s.hidden_from_claude),
            "skip_categories": list(s.skip_categories), "private_mcp": s.private_mcp,
            "auto_import_hours": auto_import_hours(),
            # per category as stored; with visible_to_claude off every kept one is local anyway
            "audience": {c: audience(c, replace(s, visible_to_claude=True))
                         for c in lists.CATEGORIES}}


def cloud(root: Path | None) -> dict:
    """The cloud-model settings (never the key: only whether its variable is set) and the last
    runs from <vault>/.cortex/cloud-log.jsonl, counts only."""
    from cortex import cloudllm
    c = config().get("cloudllm")
    c = c if isinstance(c, dict) else {}
    b = config().get("browse")
    env = c.get("api_key_env") if isinstance(c.get("api_key_env"), str) else None
    runs = []
    try:
        lines = (root / cloudllm.LOG).read_text(encoding="utf-8").splitlines()[-10:] \
            if root is not None else []
    except OSError:
        lines = []
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            runs.append({**safe({k: r.get(k) for k in ("ts", "items", "chars", "skipped")}),
                         **{k: str(r.get(k, ""))[:100] for k in ("provider", "model")}})
    return {"cloud_processing": isinstance(b, dict) and b.get("cloud_processing") is True,
            "provider": c.get("provider"), "model": c.get("model"),
            "base_url": c.get("base_url"), "api_key_env": env,
            "key_set": bool(env and os.environ.get(env, "").strip()),
            "presets": list(cloudllm.PRESETS), "runs": runs}


def history(root: Path | None, scanned: dict) -> dict:
    """The browser profiles on this computer (found by file, never read here), with the counts
    of the last scan: the "Import my past history" step."""
    try:
        from cortex.browse import build, load_settings, sources
        found = sources.discover()
        skip = load_settings().skip_profiles
    except (ImportError, OSError):
        return {"available": False, "sources": [], "imported": None}
    except Exception:  # noqa: BLE001
        log.exception("browser profile scan failed")
        return {"available": False, "sources": [], "imported": None}
    imported = None
    if root is not None:
        try:
            from cortex.vault import Vault
            imported = safe({"t": build.load_state(Vault(root)).get("last_run")}).get("t")
        except (OSError, ValueError, VaultError):  # no vault or no state yet
            pass
        except Exception:  # noqa: BLE001
            log.exception("browse state read failed")
    return {"available": True, "imported": imported, "scanned": bool(scanned),
            "sources": [{"key": s.key, "browser": s.browser, "profile": s.profile,
                         "skip": s.key in skip, **safe(scanned.get(s.key, {}))}
                        for s in found]}


def imports(root: Path | None = None) -> dict:
    """Per guided-import source: what is imported so far, and when. Chats: conversations in
    their folder (its time); notes: the resource pages the notes import names; calendar: the
    event lines on its month pages (the newest page's time); docs, repos and highlights:
    their pages."""
    from cortex import chats
    out = {}
    for source, (folder, _) in chats.EXPORT_DIRS.items():
        d = chats.export_root() / folder
        n = sum(1 for _ in d.glob("*.jsonl")) if d.is_dir() else 0
        out[source] = {"count": n, "when": when(d) if n else None}
    if root is not None:
        from cortex import importers
        res = root / "resources"
        notes = [p for k in importers.LABELS for p in res.glob(f"{k}-*.md")]
        cal = list(res.glob("calendar-*.md"))
        events = 0
        for p in cal:
            with contextlib.suppress(OSError):
                events += sum(x.startswith("- ") for x in p.read_text("utf-8").splitlines())
        more = {k: [p for g in globs for p in res.glob(g)] for k, globs in (
            ("docs", ("docs-*.md",)), ("repos", ("repo-*.md",)),
            ("highlights", ("highlights-*.md", "pocket-*.md")), ("mail", ("mail-*.md",)),
            ("selfchat", ("selfchat-*.md",)), ("location", ("location-*.md",)),
            ("photos", ("photos-*.md",)), ("spotify", ("listening-*.md",)),
            ("voice-memos", ("voice-*.md",)))}
        for source, pages, n in (("notes", notes, len(notes)), ("calendar", cal, events),
                                 *((k, ps, len(ps)) for k, ps in more.items())):
            out[source] = {"count": n,
                           "when": max(when(p) or "" for p in pages) if n else None}
    return out


def login_task() -> dict:
    """The entry that starts the receiver at login: a value under HKCU's Run key on Windows,
    else the file `login_file()` names."""
    if (f := login_file()) is not None:
        return {"supported": True, "exists": f.is_file()}
    return {"supported": True, "exists": run_get() is not None}


# Windows: HKCU ...\Run starts the no-console `cortex-receiver` at login. It replaced a Task
# Scheduler entry (LOGIN_TASK) that opened a console and stopped on battery; `actions.login`
# and the uninstaller delete that old task. The desktop app's Start with Windows is another value
# under the same key (desktop.AUTOSTART).
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def run_get(name: str = LOGIN_TASK) -> str | None:  # tests replace run_get/set/delete (conftest)
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def run_set(value: str, name: str = LOGIN_TASK) -> None:
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)


def run_delete(name: str = LOGIN_TASK) -> None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except FileNotFoundError:
        pass


def receiver_exe() -> str:
    """`cortex-receiver.exe` (no console) beside `cortex.exe`; the console `cortex` when it is
    missing (shortcut: a dev venv without the gui script; reinstall the tool to get it)."""
    from cortex import clients
    cortex = Path(clients.cortex_command())
    gui = cortex.with_name("cortex-receiver" + cortex.suffix)
    return str(gui) if gui.is_file() else str(cortex)


def login_is_for(root: Path) -> bool:
    """Whether the entry that starts the receiver at login is the one this Cortex made for
    `root` (the same receiver and vault): only then may a vault move re-point it. Another
    install's entry, or one a test copy of Cortex did not make, is left alone."""
    if (f := login_file()) is not None:
        try:
            return f.read_text(encoding="utf-8") == login_text(root)
        except OSError:
            return False
    return run_get() == login_command(root)


def login_command(root: Path) -> str:
    """The Run value: the receiver for `root`, as `cortex browse serve --vault <root>` runs."""
    exe = receiver_exe()
    serve = "" if Path(exe).stem == "cortex-receiver" else " browse serve"
    return f'"{exe}"{serve} --vault "{root}"'


def _systemd() -> bool:  # tests replace this; sd_booted()'s own test
    return Path("/run/systemd/system").is_dir()


def login_file() -> Path | None:
    """Start at login outside Windows (None there): a macOS LaunchAgent, a systemd user unit,
    or an XDG autostart entry when there is no systemd. Untested on a real Mac."""
    if sys.platform == "win32":
        return None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{LOGIN_ID}.plist"
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return (xdg / "systemd" / "user" / f"{LOGIN_ID}.service" if _systemd()
            else xdg / "autostart" / f"{LOGIN_ID}.desktop")


def login_text(root: Path) -> str:
    """`login_file()`'s content: it runs `cortex browse serve --vault <root>` at login."""
    from cortex import clients
    args = [clients.cortex_command(), "browse", "serve", "--vault", str(root)]
    if any(re.search(r"[\x00-\x1f\x7f]", a) for a in args):  # a newline would add a directive
        raise ValueError("Start at login is not offered for a vault path holding a line break "
                         "or another control character; move the vault to a plainer folder.")
    kind = login_file().suffix
    if kind == ".plist":  # plistlib escapes &, < and > as XML
        return plistlib.dumps({"Label": LOGIN_ID, "ProgramArguments": args,
                               "RunAtLoad": True}).decode("utf-8")
    if kind == ".service":  # systemd quoting: \ and " escaped, % and $ doubled (expansions)
        cmd = " ".join('"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
                       .replace("$", "$$") + '"' for a in args)
        return (f"[Unit]\nDescription={LOGIN_TASK}\n\n[Service]\nExecStart={cmd}\n\n"
                "[Install]\nWantedBy=default.target\n")
    # Desktop Entry spec: \ " ` $ escaped inside quotes, then every \ doubled, % doubled
    cmd = " ".join('"' + re.sub(r'([\\"`$])', r"\\\1", a).replace("\\", "\\\\")
                   .replace("%", "%%") + '"' for a in args)
    return f"[Desktop Entry]\nType=Application\nName={LOGIN_TASK}\nExec={cmd}\nNoDisplay=true\n"


def login_commands(on: bool) -> list[list[str]]:
    """What runs after `login_file()` is written (on) or before it is deleted (off)."""
    f = login_file()
    if f.suffix == ".plist":
        return [["launchctl", "load" if on else "unload", "-w", str(f)]]
    if f.suffix == ".service":
        return ([["systemctl", "--user", "daemon-reload"]] if on else []) + [
            ["systemctl", "--user", "enable" if on else "disable", f.name]]
    return []


def login_shown(root: Path) -> str:
    """The exact command, or the file and its commands, the page shows before the click."""
    if (f := login_file()) is None:
        return f'Adds "{LOGIN_TASK}" under HKCU\\{RUN_KEY}:\n{login_command(root)}'
    try:
        text = login_text(root)
    except ValueError as e:
        return str(e)
    return "\n".join([f"Writes {f}:", text.rstrip(),
                      *("Then runs: " + shlex.join(c) for c in login_commands(True))])


def browse(root: Path | None) -> dict:
    """The Browsing section: settings, the last import and Start at login (the receiver that
    imports in the background)."""
    out = {"settings": browse_settings(), "last_processed": None, "login": login_task()}
    try:
        from cortex import browse_serve  # noqa: F401
    except ImportError:
        out["available"] = False
        return out
    except Exception:  # noqa: BLE001
        log.exception("browse receiver import failed")
        out["available"] = False
        return out
    out["available"] = True
    if root is None:
        return out
    try:
        from cortex.browse import build
        from cortex.vault import Vault
        out["last_processed"] = safe({"t": build.load_state(Vault(root)).get("last_run")}
                                     ).get("t")
    except (OSError, ValueError, VaultError):
        pass
    except Exception:  # noqa: BLE001
        log.exception("browse state read failed")
    if out["login"].get("supported"):
        out["login"]["command"] = login_shown(root)
    return out


# ---- the Status board ---------------------------------------------------------------------


def _go(section: str, label: str) -> dict:
    return {"go": section, "label": label}


def rows(root: Path | None) -> list[dict]:
    """One row per check: {id, label, state: ok|warn|off, value, detail, action}."""
    v, out = vault(root), []
    if not v["path"]:
        out.append({"id": "vault", "label": "Vault", "state": "off", "value": "not set",
                    "detail": "Cortex keeps your notes in a folder you choose.",
                    "action": _go("setup", "Set up")})
    else:
        hot = v["ok"] and v["temp"]
        out.append({"id": "vault", "label": "Vault",
                    "state": "ok" if v["ok"] and not hot else "warn",
                    "value": v["path"], "count": v["notes"],
                    "detail": (TEMP_VAULT if hot else
                               f"{v['notes']} notes; last indexed {v['last_index'] or 'not yet'}"
                               if v["ok"] else "This folder is not a Cortex vault yet."),
                    "action": _go("vault", "Change folder") if hot else
                    None if v["ok"] else _go("setup", "Set up")})
    if v["ok"] and (row := archive_row(root)):
        out.append(row)
    out.append(git_row())
    e = embedding(root if v["ok"] else None)
    out.append({"id": "embed", "label": "Search model", "state": "ok" if e["downloaded"] else "off",
                "value": "downloaded" if e["downloaded"] else "not downloaded",
                "detail": "Finds notes by meaning, not just matching words." if e["downloaded"]
                else "Without it, search matches words only.",
                "action": None if e["downloaded"] else _go("setup", "Download")})
    from cortex.app import targets
    on = [t["label"] for t in kept(("targets", None), lambda: targets.describe(None))
          if t["connected"]]
    out.append({"id": "apps", "label": "AI apps", "state": "ok" if on else "off",
                "value": ", ".join(on) or "none connected",
                "detail": "They read and record through Cortex." if on else
                "Without one, nothing reads or records anything.",
                "action": _go("connect", "Open" if on else "Connect")})
    if stale := targets.ClaudeDesktop().other_copy():  # uncached: Apply fixes it at once
        out[-1].update(state="warn", value="Claude Desktop runs a different Cortex copy",
                       detail=f"{'; '.join(map(str, stale))} starts another cortex than the "
                              "installed app, so its updates do not reach Claude Desktop. "
                              "Apply under Connect points it at the installed one (backup "
                              "beside the file); then quit Claude Desktop from the tray and "
                              "open it again.",
                       action=_go("connect", "Fix"))
    if row := claude_code_row():
        out.append(row)
    out.append(llm_row())
    b = browse(root if v["ok"] else None)
    out.append(history_row(root if v["ok"] else None, b))
    return out


def cortex_version(exe: str) -> str | None:  # tests replace this: never a real exe
    """`<exe> --version`'s "cortex X.Y.Z", else None (unknown)."""
    from cortex._proc import NO_WINDOW
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5,
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"\bcortex (\d[\w.+-]*)", r.stdout or "")
    return m.group(1) if m else None


def claude_code_row() -> dict | None:
    """Claude Code starts the first `cortex` on PATH (its plugin's MCP entry and hooks): a warn
    row when that is another copy than the installed Cortex, of another version; Fix puts the
    installed bin folder first on the user PATH, unless the other copy's folder is on the
    system PATH, which Windows reads first."""
    from cortex import clients, update
    from cortex.app import winpath
    exe, found = clients.installed_cortex(), shutil.which("cortex")
    if not exe or not found or Path(found).resolve() == exe.resolve():
        return None
    other = str(Path(found).resolve())
    have, app_v = kept(("cortex_version", other), lambda: cortex_version(other)), update.current()
    if have == app_v:
        return None
    try:
        older = bool(have) and update.newer(update.parse_version(app_v), update.parse_version(have))
    except update.UpdateError:  # a dev or odd version: say "different", not "older"
        older = False
    row = {"id": "claude-code", "label": "Claude Code", "state": "warn", "action": None,
           "value": (f"Claude Code runs Cortex {have}, the app is {app_v}" if have else
                     f"Claude Code runs an unknown Cortex version, the app is {app_v}")}
    start = (f"Claude Code's hooks and tools start the first cortex on your PATH ({found}), "
             f"{'an older' if older else 'a different'} copy.")
    folder = str(Path(found).parent)
    if winpath.in_system(folder):
        return {**row, "detail": f"{start} Its folder ({folder}) is on the system PATH, which "
                                 "comes first, so Cortex cannot fix this. Remove that folder "
                                 "from the system PATH, or update that copy; then restart Claude."}
    return {**row, "detail": f"{start} Fix puts the app's copy first; then restart Claude.",
            "fix": {"post": "/api/connect/path", "label": "Fix"}}


def archive_row(root: Path) -> dict | None:
    """R3-10: after the automatic chat archive move, one line: what moved and how to undo it
    (chat_archive.last_move, from the marker the move writes); None before or after an undo."""
    from cortex import chat_archive
    from cortex.vault import Vault
    m = chat_archive.last_move(Vault(root))
    if m is None:
        return None
    n = m["count"]
    return {"id": "archive", "label": "Chat archive", "state": "ok", "action": None,
            "value": f"{n} chat page{'s' * (n != 1)} moved to {m['folder']} on {m['day']}",
            "detail": f"Out of git, still searchable. To undo: run `{m['undo']}` in a terminal."}


def git_row() -> dict:
    """Git keeps the vault's history (undo). Missing: the same Install Git as Setup's."""
    from cortex import session
    from cortex.app import wizard
    row = {"id": "git", "label": "Git", "action": None}
    if not session.git_choice()[0]:
        return {**row, "state": "off", "value": "off in settings",
                "detail": "Your choice: Cortex keeps no history of the vault, so undo is off.",
                "action": _go("vault", "Open")}
    if session.git_exe():
        return {**row, "state": "ok", "value": "installed",
                "detail": "Keeps your vault's history, so undo works."}
    plan = wizard.git_plan()
    return {**row, "state": "off", "value": "not installed",
            "detail": "Without git, Cortex keeps no history of your vault, so undo does "
                      "nothing. Free.",
            "install": {"label": f"Install Git ({plan['size']}, via {plan['source']})",
                        "license": plan["license"]} if plan["run"] else None,
            "page": None if plan["run"] else plan["page"]}


def llm_row() -> dict:
    """The configured runner by name, "ok" only when that runner holds the chosen model
    (runner auto: whichever does)."""
    from cortex import localllm
    m = llm(quick=True)
    want = str(m["model"] or "")
    loaded = [x for x in m["loaded"] if want and x.lower() == want.lower()]
    in_ollama = bool(want and localllm._local_name(m["ollama"] or [], want))
    if m["runner"] == "ollama":
        name, ready, up = "Ollama", in_ollama, m["ollama"] is not None
    elif m["runner"] == "lmstudio":
        name, up = "LM Studio", bool(m["lms"])  # LM Studio starts its server when needed
        ready = bool(m["lms"] and want and (loaded or any(
            want.lower() in (r["key"].lower(), r["name"].lower())
            for r in kept(("lms_models", m["lms"]), lms_models))))
    else:  # auto: the runner holding the model, else whichever one is running
        ready = bool(in_ollama or loaded)
        name = ("Ollama" if in_ollama else "LM Studio" if loaded
                else "Ollama" if m["ollama"] is not None
                else "LM Studio" if m.get("server_port") else "")
        up = bool(name)
    if not m["enabled"]:
        detail = "Off. Optional: a small AI that runs on this computer."
    elif ready:
        detail = ("On: sorts your browsing on this computer. "
                  + ("Loaded now." if loaded else "Loads when needed."))
    elif not want:
        detail = "On, but no model is chosen. Open Local model to choose one."
    elif not name:
        detail = "On, but neither Ollama nor LM Studio is running. Open Local model to start one."
    elif not up:
        detail = (f"On, but {name} is not running. Open Local model to start it."
                  if name == "Ollama" else
                  "On, but LM Studio is not on this computer. Open Local model to set it up.")
    else:
        detail = (f"On, but {name} does not have {want}. "
                  "Open Local model to get it or choose another model.")
    return {"id": "llm", "label": "Local model",
            "state": "off" if not m["enabled"] else "ok" if ready else "warn",
            "value": f"{name or 'none running'}, {want or 'no model chosen'}",
            "detail": detail, "action": _go("llm", "Open")}


def history_row(root: Path | None, b: dict, now: datetime | None = None) -> dict:
    """Is the automatic import keeping up? Warns when the last import is older than twice
    `browse.auto_import_hours` (browsers keep ~90 days, so missed days are lost). `at` is the
    import time the page shows in words where "{ago}" stands; `import` the sources imported
    before (the automatic import's); `login_hint` that only a running Cortex imports."""
    row = {"id": "history", "label": "Browsing history", "action": _go("browse", "Open")}
    state: dict = {}
    if root is not None and b.get("available"):
        try:
            from cortex.browse import build
            from cortex.vault import Vault
            state = build.load_state(Vault(root))
        except (OSError, ValueError, VaultError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("browse state read failed")
    last = state.get("last_run")
    try:
        t = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):  # never imported (the automatic import waits for a first)
        return {**row, "state": "off", "value": "never imported",
                "detail": "Import your past history once under Browsing; after that Cortex "
                          "keeps it up to date."}
    hours = (b.get("settings") or {}).get("auto_import_hours") or 0
    row |= {"at": last, "value": "last import {ago}"}
    if not hours:
        return {**row, "state": "off",
                "detail": "Automatic import is off: new visits come in only when you import "
                          "them. Turn it on under Browsing."}
    login = b.get("login") or {}
    row["login_hint"] = bool(login.get("supported") and not login.get("exists"))
    if (now or datetime.now(UTC)) - t > timedelta(hours=2 * hours):
        from cortex import browse_serve  # the automatic import's sources: never an unticked one
        from cortex.vault import Vault
        return {**row, "state": "warn",
                "detail": "Last import {ago}: newer visits are not in Cortex yet.",
                "import": [s.key for s in browse_serve.auto_sources(Vault(root))]}
    return {**row, "state": "ok", "detail": f"New visits come in every {hours:g} hours."}


# ---- "Something not working?": the text a user pastes into a bug report ---------------------

_REDACT = ((re.compile(r"(?i)\b[a-z]:[\\/][^\s'\"<>|]*"), "<path>"),  # a drive path
           (re.compile(r"(?<![\w.])/(?:Users|home|root|var|tmp|opt|etc)/[^\s'\"<>|]*"), "<path>"),
           (re.compile(r"(?i)([?&](?:token|key|api_?key|access_token|auth|secret|password)=)"
                       r"[^\s&'\"]*"), r"\1<redacted>"),
           (re.compile(r"\b(?:sk|pk|ghp|gho|ghs|xox[bap])[-_][A-Za-z0-9_-]{8,}"), "<key>"))


def redact(text, root: Path | str | None = None) -> str:
    """`text` with the vault path, the home folder and any other path as `<path>`, the user name
    as `<user>`, a URL's token= value and an API key removed."""
    import getpass

    s = str(text)
    for p in sorted({str(x) for x in (root, Path.home()) if x}, key=len, reverse=True):
        s = s.replace(p, "<path>").replace(p.replace("\\", "/"), "<path>")
    for rx, sub in _REDACT:
        s = rx.sub(sub, s)
    names = {Path.home().name}
    with contextlib.suppress(Exception):
        names.add(getpass.getuser())
    for n in names:
        if n:
            s = re.sub(rf"(?i)(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])", "<user>", s)
    return s


def report(root: Path | None) -> str:
    """The plain-text block for a bug report: versions, each Status row's name and state, which
    optional parts are on. No value, path, note or model name; every line passes `redact`.
    (Status shows no last-error line, so none is included.)"""
    import platform

    from cortex import doctor, update

    rs = rows(root)
    by = {r["id"]: r for r in rs}
    state = {"ok": "Ready", "off": "Off"}
    llm, embed, hist = by.get("llm", {}), by.get("embed", {}), by.get("history", {})
    runner = (llm.get("value") or "").split(",")[0].strip()
    lines = [f"Cortex {update.current()} ({'CortexSetup.exe' if doctor.installer() else 'uv'})",
             f"{platform.system()} {platform.release()} (build {platform.version()}), "
             f"Python {platform.python_version()}", "", "Status:"]
    lines += [f"- {r['label']}: {state.get(r['state'], 'Needs attention')}" for r in rs]
    lines += ["", "Optional parts:",
              f"- Search model: {'on' if embed.get('state') == 'ok' else 'off'}",
              f"- Local model: {(runner or 'on') if llm.get('state', 'off') != 'off' else 'off'}",
              f"- Browsing import: {'yes' if hist.get('state', 'off') != 'off' else 'no'}"]
    return redact("\n".join(lines), root)
