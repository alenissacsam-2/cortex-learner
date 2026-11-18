"""Read-only checks behind the app's sections, and thin adapters to `cortex.localllm` and the
browse code (both still growing: every call is guarded, so a missing function shows as "not
available yet"). Nothing here writes, and nothing from browsing leaves as text: only counts,
states, dates and the owner's own settings (`safe` filters the receiver's answers)."""

from __future__ import annotations

import http.client
import json
import logging
import os
import plistlib
import re
import shlex
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from cortex.vault import VaultError

log = logging.getLogger(__name__)
RECEIVER_PORT = 8799
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
    return {"path": str(root), "ok": ok, "exists": root.is_dir(),
            "notes": notes(root) if ok else 0,
            "last_index": when(root / ".cortex" / "index-snapshot.json"),
            "dashboard": when(root / ".cortex" / "dashboard.html"),
            "backups": len(list((root / ".cortex" / "backups").glob("*.zip"))) if ok else 0,
            "last_backup": max((when(p) or "" for p in (root / ".cortex" / "backups")
                                .glob("*.zip")), default=None) if ok else None}


def embedding(root: Path | None = None) -> dict:
    from cortex import semantic
    d = semantic.models_dir()
    out = {"downloaded": d.is_dir() and any(d.iterdir()), "dir": str(d)}
    if root is not None:
        out["indexed"] = when(root / semantic.CACHE)
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


def llm() -> dict:
    c = _llm_cfg()
    out = {"enabled": c.get("enabled") is True, "runner": c.get("runner") or "auto",
           "model": c.get("model") or None, "lms": lms_path(), "server_port": None,
           "loaded": [], "ollama": ollama_models(), "available": True, "error": None}
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


def _browse_dir(root: Path) -> Path:
    return root / ".cortex" / "browse"


def receiver_probe(root: Path | None) -> tuple[str, dict]:
    """Is a receiver answering on 127.0.0.1:8799? ("running"|"stopped", its counts)."""
    token = None
    if root is not None:
        try:
            token = (_browse_dir(root) / "token").read_text(encoding="utf-8").strip()
        except OSError:
            pass
    conn = http.client.HTTPConnection("127.0.0.1", RECEIVER_PORT, timeout=0.6)
    try:
        conn.request("GET", "/v1/status", headers={
            "Host": f"127.0.0.1:{RECEIVER_PORT}",
            **({"Authorization": f"Bearer {token}"} if token else {})})
        r = conn.getresponse()
        body = r.read(65536)
    except OSError:
        return "stopped", {}
    finally:
        conn.close()
    try:
        data = json.loads(body) if r.status == 200 else {}
    except ValueError:
        data = {}
    return "running", safe(data)


def browse_settings() -> dict:
    try:
        from cortex.browse import lists, load_settings
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
        from cortex.browse import build, sources
        found = sources.discover()
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
                         **safe(scanned.get(s.key, {}))} for s in found]}


def imports() -> dict:
    """Per chat-export source: conversations imported so far, and when (its folder's time)."""
    from cortex import chats
    out = {}
    for source, (folder, _) in chats.EXPORT_DIRS.items():
        d = chats.export_root() / folder
        n = sum(1 for _ in d.glob("*.jsonl")) if d.is_dir() else 0
        out[source] = {"count": n, "when": when(d) if n else None}
    return out


def login_task() -> dict:
    """The entry that starts the receiver at login: a Windows Task Scheduler entry, else the
    file `login_file()` names."""
    if (f := login_file()) is not None:
        return {"supported": True, "exists": f.is_file()}
    from cortex.app import targets
    ok, _ = targets._run(["schtasks", "/Query", "/TN", LOGIN_TASK], timeout=10)
    return {"supported": True, "exists": ok}


def login_command(root: Path) -> list[str]:
    from cortex import clients
    return ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED", "/TN", LOGIN_TASK,
            "/TR", f'"{clients.cortex_command()}" browse serve --vault "{root}"']


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
        return subprocess.list2cmdline(login_command(root))
    try:
        text = login_text(root)
    except ValueError as e:
        return str(e)
    return "\n".join([f"Writes {f}:", text.rstrip(),
                      *("Then runs: " + shlex.join(c) for c in login_commands(True))])


def browse(root: Path | None, in_app: dict | None) -> dict:
    """The Browsing section: settings, receiver, pairing, queue and last processing."""
    out = {"settings": browse_settings(), "paired": False, "receiver": "stopped",
           "in_app": in_app is not None, "queued": 0, "last_contact": None,
           "last_processed": None, "processing": False, "login": login_task()}
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
    d = _browse_dir(root)
    out["paired"] = (d / "token").is_file()
    inbox = sorted(d.glob("inbox-*.jsonl"))
    for p in inbox:  # newlines counted, the lines never parsed
        try:
            out["queued"] += p.read_bytes().count(b"\n")
        except OSError:
            pass
    out["last_contact"] = max((when(p) or "" for p in inbox), default=None) or None
    try:
        from cortex.browse import build
        from cortex.vault import Vault
        out["last_processed"] = safe({"t": build.load_state(Vault(root)).get("last_run")}
                                     ).get("t")
    except (OSError, ValueError, VaultError):
        pass
    except Exception:  # noqa: BLE001
        log.exception("browse state read failed")
    if in_app is not None:
        out["receiver"] = "running"
        live = safe(in_app)
    else:
        out["receiver"], live = receiver_probe(root)
    out["processing"] = bool(live.get("processing"))
    out["queued"] = live.get("queued_records", out["queued"])
    out["last_processed"] = live.get("last_processed") or out["last_processed"]
    out["last_contact"] = live.get("last_contact") or out["last_contact"]
    if out["login"].get("supported"):
        out["login"]["command"] = login_shown(root)
    return out


# ---- the Status board ---------------------------------------------------------------------


def _go(section: str, label: str) -> dict:
    return {"go": section, "label": label}


def rows(root: Path | None, in_app: dict | None) -> list[dict]:
    """One row per check: {id, label, state: ok|warn|off, value, detail, action}."""
    v, out = vault(root), []
    if not v["path"]:
        out.append({"id": "vault", "label": "Vault", "state": "off", "value": "not set",
                    "detail": "Cortex keeps your notes in a folder you choose.",
                    "action": _go("setup", "Set up")})
    else:
        out.append({"id": "vault", "label": "Vault", "state": "ok" if v["ok"] else "warn",
                    "value": v["path"], "count": v["notes"],
                    "detail": (f"{v['notes']} notes; last indexed {v['last_index'] or 'not yet'}"
                               if v["ok"] else "This folder is not a Cortex vault yet."),
                    "action": None if v["ok"] else _go("setup", "Set up")})
    e = embedding(root if v["ok"] else None)
    out.append({"id": "embed", "label": "Search model", "state": "ok" if e["downloaded"] else "off",
                "value": "downloaded" if e["downloaded"] else "not downloaded",
                "detail": "Finds notes by meaning, not just matching words." if e["downloaded"]
                else "Without it, search matches words only.",
                "action": None if e["downloaded"] else _go("setup", "Download")})
    from cortex.app import targets
    on = [t["label"] for t in targets.describe(None) if t["connected"]]
    out.append({"id": "apps", "label": "AI apps", "state": "ok" if on else "off",
                "value": ", ".join(on) or "none connected",
                "detail": "They read and record through Cortex." if on else
                "Without one, nothing reads or records anything.",
                "action": _go("connect", "Open" if on else "Connect")})
    m = llm()
    loaded = [x for x in m["loaded"] if m["model"] and x.lower() == str(m["model"]).lower()]
    runner = ("LM Studio" if m["lms"] else "") or ("Ollama" if m["ollama"] is not None else "")
    out.append({"id": "llm", "label": "Local model",
                "state": "off" if not m["enabled"] else "ok" if (loaded or m["ollama"]) else "warn",
                "value": f"{runner or 'no runner'}, {m['model'] or 'no model chosen'}",
                "detail": ("On: sorts your browsing on this computer. "
                           + ("Loaded now." if loaded else "Loads when needed."))
                if m["enabled"] else "Off. Optional: a small AI that runs on this computer.",
                "action": _go("llm", "Open")})
    b = browse(root if v["ok"] else None, in_app)
    out.append({"id": "receiver", "label": "Browse receiver",
                "state": "ok" if b["receiver"] == "running" else "off", "value": b["receiver"],
                "detail": "Takes visits from the browser extension, on this computer only.",
                "action": _go("browse", "Open")})
    out.append({"id": "extension", "label": "Extension", "state": "ok" if b["paired"] else "off",
                "value": "paired" if b["paired"] else "not paired",
                "detail": f"Last contact {b['last_contact']}." if b["last_contact"]
                else "No contact recorded yet.", "action": _go("browse", "Open")})
    out.append({"id": "processing", "label": "Browse processing",
                "state": "ok" if b["last_processed"] else "off",
                "value": b["last_processed"] or "never", "count": b["queued"],
                "detail": f"{b['queued']} visits waiting to be processed.",
                "action": _go("browse", "Open")})
    out.append({"id": "dashboard", "label": "Dashboard",
                "state": "ok" if v.get("dashboard") else "off",
                "value": f"built {v['dashboard']}" if v.get("dashboard") else "not built",
                "detail": "Your notes as a page you can browse, offline.",
                "action": _go("vault", "Open")})
    return out
