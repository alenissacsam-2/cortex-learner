"""`cortex app`: a stdlib HTTP server on 127.0.0.1 and one page (docs/design/cortex-app.md).

Security, because the app changes other apps' config files:
- a per-launch random token on every request: the page, its CSS and JS take it as `?t=` (the
  page then cleans the URL); every API call sends it as the `X-Cortex-Token` header;
- the Host must be the app's own loopback address (DNS rebinding), and any Origin its own;
- GET never changes anything; changes are POST with `Content-Type: application/json`;
- CSP `default-src 'self'`, no inline script; the page builds its DOM without innerHTML;
- it stops after 30 minutes idle, or on Quit.
Long actions run as background jobs, one at a time, with a progress line (`Jobs`).
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, unquote, urlsplit

from cortex import update
from cortex.app import actions, news, status, targets, wizard

HOST = "127.0.0.1"
IDLE_S = 30 * 60
MAX_BODY = 64 * 1024
MAX_UPLOAD = 4 * 2**30
UPLOAD = "/api/import/upload"  # the one raw (not JSON) POST: an export file, streamed to disk
UPLOAD_EXT = (".zip", ".json", ".html", ".md", ".txt", ".ics")
CLOUD_FIELDS = ("cloud_processing", "provider", "model", "base_url", "api_key_env")
IMPORTS = ("claude", "chatgpt", "gemini", "perplexity", "google-activity", "notes", "calendar")
ASSETS = Path(__file__).resolve().parents[1] / "assets" / "app"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8")}
CSP = ("default-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")
EXPIRED = (b"<!doctype html><meta charset=utf-8><title>Cortex</title><p>This link has no valid "
           b"key. Open the link that <code>cortex app</code> printed, or run it again.</p>")
EXPIRED_INSTALLED = (b"<!doctype html><meta charset=utf-8><title>Cortex</title><p>This link "
                     b"has no valid key. Open Cortex from the Start menu again.</p>")


def reopen() -> str:
    """How this user reopens the app (A12): the Start menu for an installer install."""
    return ("Open Cortex from the Start menu to reopen it." if update.mode() == "installer"
            else "Run cortex app again to reopen it.")
_open = webbrowser.open  # tests replace this


def _checkout() -> Path:
    from cortex import cli
    return cli.CHECKOUT


def extension_dir() -> Path:
    """The browser extension: shipped in the package (an installed wheel), else the source
    checkout's extension/ (an editable install)."""
    from importlib.resources import files
    p = Path(str(files("cortex") / "assets" / "extension"))
    return p if p.is_dir() else _checkout() / "extension"


def skill_url() -> str:
    """The Chat skill attached to the latest release (installer/build.py makes it)."""
    from cortex import cli
    return f"{cli.RELEASES}/latest/download/cortex-skill.zip"


def _open_path(p: Path) -> None:
    if sys.platform == "win32" and _open is webbrowser.open:
        os.startfile(p)  # noqa: S606  (a folder or file Cortex chose, never the page's)
    else:
        _open(p.as_uri())


def _obsidian() -> bool:  # tests replace this
    """An obsidian:// handler is registered (Windows); elsewhere assume Obsidian is there."""
    if sys.platform != "win32":
        return True
    import winreg
    for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Classes\obsidian"),
                      (winreg.HKEY_CLASSES_ROOT, "obsidian")):
        try:
            winreg.CloseKey(winreg.OpenKey(hive, key))
            return True
        except OSError:
            pass
    return False


def _open_note(p: Path) -> dict:
    """A vault page or folder in Obsidian; without Obsidian, with this computer's default app
    (Explorer for a folder)."""
    if _obsidian():
        _open("obsidian://open?path=" + quote(str(p), safe=""))
        return {"ok": True, "obsidian": True}
    _open_path(p)
    return {"ok": True, "obsidian": False}


# ---- jobs -------------------------------------------------------------------------------------


KEEPALIVE_S = 3  # seconds between {"t": "wait"} lines while a chat answer is quiet

class Busy(Exception):
    pass


class Job:
    def __init__(self, name: str, section: str, cancellable: bool):
        self.name, self.section, self.cancellable = name, section, cancellable
        self.lines: list[str] = []
        self.result = self.error = None
        self.running, self.started, self.ended = True, time.time(), None
        self.cancelled = threading.Event()
        self.proc = None

    def say(self, line) -> None:
        self.lines.append(str(line)[:400])
        del self.lines[:-300]

    def cancel(self) -> None:
        self.cancelled.set()
        if self.proc is not None:
            try:
                self.proc.terminate()
            except OSError:
                pass

    def view(self) -> dict:
        return {"name": self.name, "section": self.section, "running": self.running,
                "cancellable": self.cancellable, "line": self.lines[-1] if self.lines else "",
                "seconds": round((self.ended or time.time()) - self.started, 1),
                "result": self.result, "error": self.error}


class Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.cur: Job | None = None
        self.seq = 0

    def start(self, name: str, section: str, fn, *, cancellable: bool = False,
              quiet_errors: bool = False) -> dict:
        """Run `fn(job)` on a thread; Busy while another job runs. `quiet_errors` reports an
        unexpected error by type only (browse jobs: a message could quote a page)."""
        with self.lock:
            if self.cur is not None and self.cur.running:
                raise Busy(f"Wait: {self.cur.name} is still running.")
            job = self.cur = Job(name, section, cancellable)
            self.seq += 1

        def body():
            try:
                job.result = fn(job)
            except Exception as e:  # noqa: BLE001  (reported on the page, never a traceback)
                from cortex.vault import VaultError  # its messages are written for users
                job.error = (type(e).__name__ if quiet_errors
                             and not isinstance(e, ValueError | VaultError)
                             else str(e)[:500] or type(e).__name__)
                if not quiet_errors and not isinstance(e, ValueError | VaultError):  # refusals
                    traceback.print_exc(limit=3)
            finally:
                job.running, job.ended = False, time.time()

        threading.Thread(target=body, daemon=True, name=f"cortex-app-{name}").start()
        return {"job": name, "seq": self.seq}

    def view(self) -> dict:
        return {"job": self.cur.view() if self.cur else None, "seq": self.seq}

    def running(self) -> bool:
        return self.cur is not None and self.cur.running


# ---- the app ---------------------------------------------------------------------------------


def _connect_skipped() -> bool:
    app = status.config().get("app")
    return isinstance(app, dict) and app.get("connect_skipped") is True


def _bool(body: dict, key: str) -> bool:
    v = body.get(key)
    if not isinstance(v, bool):
        raise ValueError(f"{key} must be true or false")
    return v


def _domains(v, key: str) -> list[str]:
    if isinstance(v, str):
        v = v.replace(",", "\n").split()
    if not isinstance(v, list) or len(v) > 1000:
        raise ValueError(f"{key} must be a list of domains")
    out = []
    for d in v:
        d = str(d).strip().lower().lstrip(".")
        if d and not (len(d) <= 253 and all(c.isalnum() or c in ".-" for c in d)):
            raise ValueError(f"{d!r} is not a domain such as example.com")
        if d:
            out.append(d)
    return sorted(set(out))


class App:
    def __init__(self, root: Path | None):
        self.root = Path(root).resolve() if root else None
        self.token = secrets.token_urlsafe(32)
        self.port = 0
        self.jobs = Jobs()
        self.receiver = actions.Receiver()
        self.scanned: dict = {}  # source key -> the last scan's counts
        self.uploads: dict[str, Path] = {}  # upload id -> its file under ~/.cortex/tmp
        self.chatting = threading.Lock()  # one chat answer at a time, beside any job
        self.last = time.monotonic()
        self.stop = threading.Event()
        self.get = {"/api/status": self.status, "/api/connect": self.connect,
                    "/api/llm": self.llm, "/api/browse": self.browse, "/api/vault": self.vault,
                    "/api/setup": self.setup, "/api/chat": self.chat_state,
                    "/api/chat/list": self.chat_list, "/api/chat/get": self.chat_get,
                    "/api/chat/settings": self.chat_settings_get,
                    "/api/update": self.update_state, "/api/job": self.jobs.view}
        self.post = {
            "/api/connect/plan": self.connect_plan, "/api/connect/apply": self.connect_apply,
            "/api/llm/settings": self.llm_settings, "/api/llm/load": self.llm_load,
            "/api/llm/test": self.llm_test, "/api/llm/pull": self.llm_pull,
            "/api/browse/settings": self.browse_settings, "/api/browse/cloud": self.cloud,
            "/api/browse/auto": self.auto_settings,
            "/api/browse/receiver": self.receive, "/api/chat": None,  # do_POST streams it
            "/api/chat/delete": self.chat_delete, "/api/chat/settings": self.chat_settings,
            "/api/browse/login": self.login, "/api/browse/pair": self.pair,
            "/api/browse/process": self.process, "/api/browse/selftest": self.selftest,
            "/api/browse/scan": self.scan, "/api/browse/import": self.history_import,
            "/api/vault/dashboard": self.dashboard, "/api/vault/backup": self.backup,
            "/api/vault/doctor": self.doctor, "/api/vault/git": self.git_settings,
            "/api/open": self.open,
            "/api/setup/vault": self.setup_vault, "/api/setup/embed": self.setup_embed,
            "/api/setup/index": self.setup_index, "/api/setup/install": self.setup_install,
            "/api/setup/git": self.setup_git,
            "/api/setup/pull": self.setup_pull, "/api/setup/test": self.setup_test,
            "/api/setup/start": self.setup_start, "/api/setup/model": self.setup_model,
            "/api/browse/setup": self.extension_setup, UPLOAD: None,  # do_POST streams it
            "/api/import/preview": self.import_preview, "/api/import/run": self.import_run,
            "/api/job/cancel": self.cancel, "/api/quit": self.quit,
            "/api/setup/skip": self.setup_skip, "/api/update/check": self.update_check,
            "/api/update/apply": self.update_apply, "/api/whats_new/seen": self.news_seen}

    def touch(self) -> None:
        self.last = time.monotonic()

    def need_root(self) -> Path:
        if self.root is None or not (self.root / ".cortex").is_dir():
            raise ValueError("Set up a vault first (Setup, step 1).")
        return self.root

    def entry(self) -> dict | None:
        if self.root is None or not (self.root / ".cortex").is_dir():
            return None
        from cortex import clients
        return clients.server_entry(clients.cortex_command(), self.root)

    # ---- GET (never changes anything) ----
    def status(self) -> dict:
        v = status.vault(self.root)
        ready = v["ok"] and (_connect_skipped() or any(
            t["connected"] for t in targets.describe(self.entry())))
        return {"rows": status.rows(self.root, self.receiver.status()),
                "first_run": not v["ok"], "vault": v["path"], "update": self.update_state(),
                "whats_new": news.card(),
                "land": "status" if ready else "setup"}  # Setup until its key steps are done

    def connect(self) -> dict:
        return {"targets": targets.describe(self.entry()), "vault": self.entry() is not None,
                "skill_zip": str(_checkout() / "dist" / "cortex-skill.zip"),
                "skill_zip_found": (_checkout() / "dist" / "cortex-skill.zip").is_file(),
                "skill_url": skill_url()}

    def llm(self) -> dict:
        """The Local model page: both runners' chat models (embedding models left out by the
        chat's own name rule) and the model sized to this computer."""
        from cortex import localllm
        d, hw = status.llm(), wizard.hardware()

        def chats(names):
            return [n for n in names if not localllm._NOT_CHAT.search(n)]
        return {**d, "ollama": None if d["ollama"] is None else chats(d["ollama"]),
                "ollama_state": wizard._ollama_state(d["ollama"]),  # the words Setup uses
                "models": [m for m in status.lms_models() if chats([m["key"]])],
                "hardware": hw, "recommend": wizard.model(wizard.recommend(hw["gb"], hw["kind"]))}

    def browse(self) -> dict:
        b = status.browse(self.root if self.entry() else None, self.receiver.status())
        return {**b, "extension": str(extension_dir()),
                "history": status.history(self.root if self.entry() else None, self.scanned),
                "cloud": status.cloud(self.root if self.entry() else None)}

    def vault(self) -> dict:
        from cortex.session import git_choice
        g = status.config().get("git")
        # the saved private choice, shown (greyed out) even while git is off
        private = isinstance(g, dict) and g.get("private_browsing") is True
        return {**status.vault(self.root), "embed": status.embedding(),
                "git": {"enabled": git_choice()[0], "private_browsing": private}}

    def chat_state(self) -> dict:
        """What the Chat page needs to open: the missing pieces (search model, local model),
        the model's name, the settings and the past chats."""
        from cortex import chat, localllm
        if self.entry() is None:
            return {"vault": False}
        st = localllm.status()  # read-only: a GET never starts LM Studio or loads a model
        runner, why = st["runner"], st["error"]
        missing = [k for k, gone in (("embed", not status.embedding()["downloaded"]),
                                     ("llm", runner is None)) if gone]
        return {"vault": True, "missing": missing, "llm_error": why,
                "model": runner.model if runner else None, "settings": chat.get_settings(),
                "chats": chat.list(self._vault())}

    def _vault(self):
        from cortex.vault import Vault
        return Vault(self.need_root())

    def chat_list(self) -> dict:
        from cortex import chat
        return {"chats": chat.list(self._vault())}

    def chat_get(self, query: dict) -> dict:
        from cortex import chat
        cid = (query.get("id") or [None])[0]
        if cid is None:
            return {"id": None, "messages": []}
        return {"id": cid, "messages": chat.load(self._vault(), cid)}  # load checks the id

    def chat_settings_get(self) -> dict:
        from cortex import chat
        return chat.get_settings()

    def setup(self) -> dict:
        from cortex import chats, cli, importers
        token = self.root / ".cortex" / "browse" / "token" if self.entry() else None
        return {**wizard.state(self.root, self.scanned),
                "connect": [{k: t[k] for k in ("id", "label", "detected", "connected")}
                            for t in targets.describe(self.entry())],
                "first_prompts": list(cli.FIRST_PROMPTS), "connect_skipped": _connect_skipped(),
                "guides": {**chats.GUIDES, **importers.GUIDES},
                "imports": status.imports(self.root if self.entry() else None),
                "extension": {"paired": bool(token and token.is_file()),
                              "folder": str(extension_dir())}}

    # ---- POST ----
    def _want(self, body: dict) -> dict:
        want = body.get("want")
        if not isinstance(want, dict) or not all(
                isinstance(v, bool) or (k == "lmstudio" and v == "private")
                for k, v in want.items()):
            raise ValueError("want must map app ids to true or false")
        return want

    def connect_plan(self, body: dict) -> dict:
        return {"rows": targets.plan_all(self._want(body), self.entry())}

    def connect_apply(self, body: dict) -> dict:
        want, base = self._want(body), self.entry()
        return self.jobs.start("Connecting AI apps", "connect",
                               lambda job: {"ok": True,
                                            "rows": targets.apply_all(want, base, job.say)})

    def llm_settings(self, body: dict) -> dict:
        from cortex import taint

        def merge(cfg: dict) -> dict:  # under the config lock
            old = cfg.get("localllm")
            new = dict(old) if isinstance(old, dict) else {}
            if "enabled" in body:
                new["enabled"] = _bool(body, "enabled")
            if "model" in body:
                m = body["model"]
                if not isinstance(m, str) or not m:  # never write `model: null`
                    raise ValueError("Choose a model first.")
                if len(m) > 200 or not all(c.isalnum() or c in "._:@/-" for c in m):
                    raise ValueError("not a model name")
                r = body.get("runner", "lmstudio")  # no runner: an LM Studio choice, as before
                if r not in wizard.RUNNERS:
                    raise ValueError("runner must be ollama or lmstudio")
                new["model"], new["runner"] = m, r
            if "background_model" in body:  # "" or null: the main model does background work too
                b = body["background_model"] or ""
                if not isinstance(b, str) or len(b) > 200 or not all(
                        c.isalnum() or c in "._:@/-" for c in b):
                    raise ValueError("not a model name")
                if b:
                    new["background_model"] = b
                else:
                    new.pop("background_model", None)
            return {"localllm": new}
        taint.write_config(merge)
        return {"ok": True}

    def llm_load(self, body: dict) -> dict:
        m = body.get("model")
        if not isinstance(m, str) or not m:
            raise ValueError("choose a model first")
        return self.jobs.start(f"Loading {m}", "llm", lambda job: actions.llm_load(job, m))

    def llm_pull(self, body: dict) -> dict:
        name = wizard.ollama_name(body.get("name"))
        return self.jobs.start(f"Downloading {name}", "llm",
                               lambda job: wizard.pull_ollama(job, name), cancellable=True)

    def llm_test(self, body: dict) -> dict:
        # a test may start LM Studio and load the chosen model, as `enabled: true` would
        return self.jobs.start("Testing the local model", "llm",
                               lambda job: actions.llm_test(job, actions.llm_config(enabled=True)))

    def browse_settings(self, body: dict) -> dict:
        """Save `browse:`, keeping its other keys and the rest of the config. The page sends
        per-category lists; the legacy adult/sensitive pair is then written to match them."""
        from cortex import taint
        from cortex.browse import lists
        for key, ok in (("adult", ("skip", "private")), ("sensitive", ("private", "skip"))):
            if key in body and body[key] not in ok:
                raise ValueError(f"{key} must be {' or '.join(ok)}")
        if "audience" in body:  # one three-way choice per category, as the two lists
            aud = body["audience"]
            if not isinstance(aud, dict) or set(aud) != set(lists.CATEGORIES) or not all(
                    v in ("skip", "local", "cloud") for v in aud.values()):
                raise ValueError("audience must give every category skip, local or cloud")
            body = {**body, "skip_categories": [c for c, v in aud.items() if v == "skip"],
                    "hidden_from_claude": [c for c, v in aud.items() if v == "local"]}

        def merge(cfg: dict) -> dict:  # under the config lock
            old = cfg.get("browse")
            new = {**(old if isinstance(old, dict) else {}),
                   **{k: body[k] for k in ("adult", "sensitive") if k in body},
                   "visible_to_claude": _bool(body, "visible_to_claude"),
                   "deny_domains": _domains(body.get("deny", []), "deny"),
                   "allow_domains": _domains(body.get("allow", []), "allow"),
                   "private_domains": _domains(body.get("private", []), "private")}
            for key in ("hidden_from_claude", "skip_categories"):
                if key in body:
                    v = body[key]
                    if not isinstance(v, list) or not all(c in lists.CATEGORIES for c in v):
                        raise ValueError(f"{key} must list categories from "
                                         f"{', '.join(lists.CATEGORIES)}")
                    new[key] = [c for c in lists.CATEGORIES if c in v]
            if "skip_categories" in body:
                skip = new["skip_categories"]
                new["adult"] = "skip" if "adult" in skip else "private"
                new["sensitive"] = "skip" if all(c in skip for c in lists.CATEGORIES
                                                 if c != "adult") else "private"
            if "private_mcp" in body:
                new["private_mcp"] = _bool(body, "private_mcp")
            if "hidden_from_claude" in body:  # C9: stored as the cloud allowlist; new stay local
                hidden = new.pop("hidden_from_claude")
                new["cloud_categories"] = [c for c in lists.CATEGORIES if c not in hidden]
            return {"browse": new}
        taint.write_config(merge)
        return {"ok": True}

    def cloud(self, body: dict) -> dict:
        """Save the cloud model: `browse.cloud_processing` and `cloudllm` (other keys kept).
        Never a key: only the name of the variable that holds it. Empty fields are removed."""
        from cortex import cloudllm, taint
        from cortex.vault import secret_pattern
        if extra := set(body) - set(CLOUD_FIELDS):
            raise ValueError(f"unknown field {sorted(extra)[0]!r}: the key itself is never "
                             "sent here, only the name of the variable that holds it")
        on = _bool(body, "cloud_processing")
        got = {k: body.get(k) or None for k in CLOUD_FIELDS[1:]}
        if not all(v is None or (isinstance(v, str) and len(v) <= 300) for v in got.values()):
            raise ValueError("provider, model, base_url and api_key_env must be text")
        if got["provider"] is not None and got["provider"] not in (*cloudllm.PRESETS, "custom"):
            raise ValueError(f"provider must be one of {', '.join(cloudllm.PRESETS)} or custom")
        m = got["model"]
        if m is not None and not all(c.isalnum() or c in "._:@/-" for c in m):
            raise ValueError("not a model name")
        if got["provider"] != "custom":
            got["base_url"] = None  # a preset's base is its own
        elif got["base_url"] is not None:
            cloudllm._check(got["base_url"])  # https with a host, or ValueError
        env = got["api_key_env"]
        if env is not None:
            cloudllm.check_env_name(env)
            if secret_pattern(env):
                raise ValueError("api_key_env is the NAME of an environment variable, such as "
                                 "OPENAI_API_KEY; never paste the key here")
        need = {"provider": "a provider", "model": "a model", "api_key_env": "the variable name"}
        if got["provider"] == "custom":
            need["base_url"] = "the Base URL"
        if on and (gap := [w for k, w in need.items() if got[k] is None]):
            raise ValueError(f"To turn the cloud model on, fill in {' and '.join(gap)} first.")

        def merge(old: dict) -> dict:  # under the config lock
            c = old.get("cloudllm") if isinstance(old.get("cloudllm"), dict) else {}
            c = {k: v for k, v in c.items() if k not in CLOUD_FIELDS}
            b = old.get("browse") if isinstance(old.get("browse"), dict) else {}
            return {"cloudllm": {**c, **{k: v for k, v in got.items() if v is not None}},
                    "browse": {**b, "cloud_processing": on}}
        taint.write_config(merge)
        return {"ok": True}

    def auto_settings(self, body: dict) -> dict:
        """Save `browse.auto_import_hours` (0-168; 0 = off), the other browse keys kept."""
        from cortex import taint
        h = body.get("hours")
        if not (isinstance(h, (int, float)) and not isinstance(h, bool) and 0 <= h <= 168):
            raise ValueError("hours must be a number from 0 (off) to 168")
        taint.write_config(lambda cfg: {"browse": {
            **(cfg["browse"] if isinstance(cfg.get("browse"), dict) else {}),
            "auto_import_hours": int(h) if h == int(h) else h}})
        return {"ok": True}

    def auto_import(self) -> dict | None:
        """Start the ordinary history import (only the browsers and profiles imported before,
        incremental) when
        `browse_serve.auto_import_due` says so and no job runs; None when it did not. The
        in-app receiver's Processor does the same check, so the app leaves it to that."""
        from cortex import browse_serve
        from cortex.vault import Vault
        if not self.entry() or self.receiver.srv is not None or self.jobs.running():
            return None
        if not status.embedding()["downloaded"]:  # the import job would stop at once, every
            return None                            # 10 minutes: wait for the search model
        root = self.root
        if not browse_serve.auto_import_due(Vault(root)):
            return None
        keys = {s.key for s in browse_serve.auto_sources(Vault(root))}  # never an unticked one
        if not keys:
            return None
        try:
            return self.jobs.start("Importing new history", "browse",
                                   lambda job: actions.history_import(job, root, keys),
                                   cancellable=True, quiet_errors=True)
        except Busy:  # a job started meanwhile: the next check tries again
            return None

    def chat_begin(self, body: dict) -> tuple:
        """Check a chat turn and take the one chat slot: (vault, id or None, text). Busy while
        another answer streams; ValueError on a bad body."""
        from cortex import chat
        cid, text = body.get("id"), body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > chat.MAX_TEXT:
            raise ValueError(f"Type a message (up to {chat.MAX_TEXT:,} characters).")
        vault = self._vault()
        if cid is not None:
            chat._path(vault, cid)  # a ULID or ValueError: never a path
        if not self.chatting.acquire(blocking=False):
            raise Busy("An answer is still being written; wait for it or press Stop.")
        return vault, cid, text

    def chat_delete(self, body: dict) -> dict:
        from cortex import chat
        if self.chatting.locked():  # the answer's final save would bring the chat back
            raise Busy("An answer is still being written; press Stop first, then delete.")
        if body.get("all") is True:
            return {"deleted": chat.delete_all(self._vault())}
        return {"deleted": int(chat.delete(self._vault(), body.get("id")))}

    def chat_settings(self, body: dict) -> dict:
        from cortex import chat
        return chat.save_settings(body)

    def receive(self, body: dict) -> dict:
        if _bool(body, "on"):
            self.receiver.start(self.need_root())
        else:
            self.receiver.stop()
        return {"ok": True}

    def login(self, body: dict) -> dict:
        on, root = _bool(body, "on"), self.need_root()
        return self.jobs.start("Start at login" if on else "Removing start at login", "browse",
                               lambda job: actions.login(job, root, on))

    def pair(self, body: dict) -> dict:
        return {"token": actions.pair(self.need_root())}

    def extension_setup(self, body: dict) -> dict:
        """Pair, start the receiver (unless one answers already) and open the extension folder.
        The page shows the key with Copy; Start at login stays its own click."""
        root = self.need_root()
        token = actions.pair(root)
        state = "running" if self.receiver.srv is not None else status.receiver_probe(root)[0]
        if state == "stopped":
            self.receiver.start(root)
            state = "running"
        folder = extension_dir()
        if folder.is_dir():
            _open_path(folder)
        return {"token": token, "folder": str(folder), "opened": folder.is_dir(),
                "receiver": state}

    def process(self, body: dict) -> dict:
        root = self.need_root()
        return self.jobs.start("Processing browsing", "browse",
                               lambda job: actions.browse_process(job, root), quiet_errors=True)

    def selftest(self, body: dict) -> dict:
        return self.jobs.start("Browse self-test", "browse", actions.browse_selftest,
                               quiet_errors=True)

    def _section(self, body: dict) -> str:
        return "setup" if body.get("from") == "setup" else "browse"

    def scan(self, body: dict) -> dict:
        def run(job):
            out = actions.browse_scan(job)
            self.scanned = {r["source"]: r for r in out["rows"]}
            return out
        return self.jobs.start("Counting browser history", self._section(body), run,
                               quiet_errors=True)

    def history_import(self, body: dict) -> dict:
        root, keys = self.need_root(), body.get("sources")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
            raise ValueError("Tick at least one browser.")
        if "skip" in body:  # only the tick list sends it; Status's button must not clear it
            skip = body["skip"]
            if not isinstance(skip, list) or not all(isinstance(k, str) for k in skip):
                raise ValueError("skip must list profile keys")
            self._skip_profiles(skip, keys)
        return self.jobs.start("Importing past history", self._section(body),
                               lambda job: actions.history_import(job, root, set(keys)),
                               cancellable=True, quiet_errors=True)

    @staticmethod
    def _skip_profiles(skip: list, ticked: list) -> None:
        """Keep `browse.skip_profiles` as the user's last ticks: the unticked keys added, the
        ticked ones removed; the automatic imports (`sources.chosen`) leave those out."""
        from cortex import taint

        def merge(cfg: dict) -> dict:  # under the config lock; unchanged: no write
            old = cfg.get("browse")
            old = old if isinstance(old, dict) else {}
            had = old.get("skip_profiles")
            had = [k for k in had if isinstance(k, str)] if isinstance(had, list) else []
            new = sorted((set(had) | set(skip)) - set(ticked))
            return {"browse": {**old, "skip_profiles": new}} if new != sorted(had) else {}
        taint.write_config(merge)

    def dashboard(self, body: dict) -> dict:
        root = self.need_root()
        return self.jobs.start("Building the dashboard", "vault",
                               lambda job: actions.dashboard(job, root))

    def backup(self, body: dict) -> dict:
        root = self.need_root()
        return self.jobs.start("Backing up the vault", "vault",
                               lambda job: actions.backup(job, root))

    def git_settings(self, body: dict) -> dict:
        """Save `git:` (enabled, private_browsing), keeping its other keys, then apply the
        private-browsing choice to the vault's .gitignore (and index) now, not at the next
        browse import."""
        from cortex import taint
        from cortex.browse import forget
        new = {"enabled": _bool(body, "enabled"),
               "private_browsing": _bool(body, "private_browsing")}
        taint.write_config(lambda cfg: {"git": {
            **(cfg["git"] if isinstance(cfg.get("git"), dict) else {}), **new}})
        if self.entry() is not None:
            forget.sync_private(self._vault())
        return {"ok": True}

    def doctor(self, body: dict) -> dict:
        root = self.root
        return self.jobs.start("Running the doctor", "vault",
                               lambda job: actions.doctor(job, root))

    def open(self, body: dict) -> dict:
        what = body.get("what")
        if what == "page":  # a source of an answer: a Markdown page inside the vault
            root, rel = self.need_root(), body.get("path")
            p = (root / rel).resolve() if isinstance(rel, str) and rel else None
            if p is None or Path(rel).is_absolute() or not p.is_relative_to(root) \
                    or p.suffix != ".md" or not p.is_file() \
                    or p.relative_to(root).parts[0].startswith("."):
                raise ValueError("not a page in this vault")
            return _open_note(p)
        if what in ("skill_zip", "extension"):
            p = _checkout() / "dist" if what == "skill_zip" else extension_dir()
            if not p.is_dir():
                raise ValueError(f"{p} is not there; build it first.")
            _open_path(p)
            return {"ok": True}
        root = self.need_root()
        if what == "dashboard":
            p = root / ".cortex" / "dashboard.html"
            if not p.is_file():
                raise ValueError("Build the dashboard first.")
            _open(p.as_uri())
        elif what in ("vault", "private"):
            p = root / "browse" / "private" / "browse-private-index.md" if what == "private" \
                else root
            if what == "private" and not p.is_file():
                p = p.parent
                if not p.is_dir():
                    raise ValueError("There are no private browse pages yet.")
            return _open_note(p)
        elif what == "folder":  # the plain folder, in Explorer or Finder
            _open_path(root)
        else:
            raise ValueError("unknown thing to open")
        return {"ok": True}

    def setup_skip(self, body: dict) -> dict:
        """Skip the Connect step (Status becomes the landing page), or un-skip it."""
        from cortex import taint
        skip = _bool(body, "skipped")

        def merge(cfg: dict) -> dict:  # under the config lock
            app = cfg.get("app")
            app = {k: v for k, v in app.items() if k != "connect_skipped"} \
                if isinstance(app, dict) else {}
            return {"app": {**app, "connect_skipped": True} if skip else app}
        taint.write_config(merge)
        return {"ok": True}

    def setup_vault(self, body: dict) -> dict:
        path = body.get("path")

        def run(job):
            root, out = wizard.make_vault(job, path)
            self.root = root
            return out
        return self.jobs.start("Setting up the vault", "setup", run)

    def setup_embed(self, body: dict) -> dict:
        root = self.entry() and self.root

        def run(job):  # a successful download goes straight on to indexing the notes
            out = wizard.embed(job, root)
            if not out["ok"] or not root or job.cancelled.is_set():
                return out
            job.say("downloaded; now indexing your notes")
            idx = wizard.index(job, root)
            return {**idx, "lines": [*out["lines"][-5:], *idx["lines"]]}
        return self.jobs.start("Downloading the search model", "setup", run, cancellable=True)

    def setup_index(self, body: dict) -> dict:
        root = self.need_root()
        return self.jobs.start("Indexing your notes", "setup",
                               lambda job: wizard.index(job, root), cancellable=True)

    def setup_install(self, body: dict) -> dict:
        return self.jobs.start("Installing Ollama", "setup", wizard.install_ollama,
                               cancellable=True)

    def setup_git(self, body: dict) -> dict:
        return self.jobs.start("Installing Git", "setup", wizard.install_git, cancellable=True)

    def _model_runner(self, body: dict) -> tuple[str, str]:
        m, r = body.get("model"), body.get("runner")
        wizard.model(m if isinstance(m, str) else "")
        if r not in ("ollama", "lmstudio"):
            raise ValueError("runner must be ollama or lmstudio")
        return m, r

    def setup_pull(self, body: dict) -> dict:
        m, r = self._model_runner(body)
        return self.jobs.start(f"Downloading {wizard.model(m)['label']}", "setup",
                               lambda job: wizard.pull(job, m, r), cancellable=True)

    def setup_test(self, body: dict) -> dict:
        m, r = self._model_runner(body)
        return self.jobs.start(f"Testing {wizard.model(m)['label']}", "setup",
                               lambda job: wizard.test_and_save(job, m, r))

    def setup_start(self, body: dict) -> dict:
        r = body.get("runner")
        if r not in ("ollama", "lmstudio"):
            raise ValueError("runner must be ollama or lmstudio")
        name = "Ollama" if r == "ollama" else "LM Studio"
        return self.jobs.start(f"Starting {name}", "setup", lambda job: wizard.start(r))

    def setup_model(self, body: dict) -> dict:
        """One click: download the model, then test it and turn it on."""
        m, r = self._model_runner(body)

        def run(job):
            out = wizard.pull(job, m, r)
            return wizard.test_and_save(job, m, r) if out["ok"] else out
        return self.jobs.start(f"Setting up {wizard.model(m)['label']}", "setup", run,
                               cancellable=True)

    # ---- guided imports: an upload (or a pasted path), a dry-run preview, then the import ----
    def _drop(self, uid: str | None) -> None:
        if uid and (p := self.uploads.pop(uid, None)) is not None:
            p.unlink(missing_ok=True)

    def _import(self, body: dict, title: str, fn, keep: bool) -> dict:
        """Start `fn(job, source, path, root)` as a job on a checked source and file. The
        upload is deleted when the job fails, and after it unless `keep` (a preview)."""
        source, uid, raw = body.get("source"), body.get("upload"), body.get("path")
        if source not in IMPORTS:
            raise ValueError(f"source must be one of {', '.join(IMPORTS)}")
        if uid is not None:
            p = self.uploads.get(uid) if isinstance(uid, str) else None
            if p is None or not p.is_file():
                raise ValueError("That file is gone; add it again.")
        else:
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("Add the file, or paste its full path.")
            p = Path(raw.strip().strip("\"'")).expanduser()
            if not p.is_absolute():
                raise ValueError("Paste the full path, such as "
                                 "C:\\Users\\you\\Downloads\\export.zip.")
            p = p.resolve()
            if not p.exists():  # a folder is fine: Perplexity .md files, an unzipped Takeout
                raise ValueError(f"Nothing is at {p}.")
        # notes, calendars and Google activity go into the vault; chats go beside it
        root = self.need_root() if source in ("notes", "calendar") or (
            source == "google-activity" and not keep) else self.root

        def run(job):
            out = None
            try:
                out = fn(job, source, p, root)
                return out
            finally:  # kept after a preview, or to retry once the search model is in
                if out is None or not (keep or out.get("need")):
                    self._drop(uid)
        return self.jobs.start(title, "setup", run, cancellable=source == "google-activity",
                               quiet_errors=source == "google-activity")

    def import_preview(self, body: dict) -> dict:
        return self._import(body, "Checking the file", actions.import_preview, keep=True)

    def import_run(self, body: dict) -> dict:
        return self._import(body, "Importing", actions.import_run, keep=False)

    def cancel(self, body: dict) -> dict:
        if self.jobs.cur is not None and self.jobs.cur.running and self.jobs.cur.cancellable:
            self.jobs.cur.cancel()
        return {"ok": True}

    # ---- updates: only on a click, never in the background ----
    def update_state(self) -> dict:
        return {"current": update.current(), "mode": update.mode()}

    def _check(self, job) -> tuple[dict, dict]:
        job.say("asking GitHub for the latest release")
        info, cur = update.latest(), update.current()
        out = {"current": cur, "latest": ".".join(map(str, info["version"])),
               "newer": update.newer(info["version"], update.parse_version(cur)),
               "notes_url": info["notes_url"], "mode": update.mode(),
               "size": info["exe_size"]}
        if out["mode"] == "dev":
            out["command"] = update.command(info["tag"])
        actions.sweep_updates(update.dest_dir(info) if out["newer"] else None)
        return info, out

    def news_seen(self, body: dict) -> dict:
        """Got it on the What's new card: this version's news is not shown again."""
        news.record()
        return {"ok": True}

    def update_check(self, body: dict) -> dict:
        return self.jobs.start("Checking for updates", "update", lambda job: self._check(job)[1])

    def update_apply(self, body: dict) -> dict:
        """Download the new installer, check it, start it and quit; it reopens the app."""
        if update.mode() != "installer":
            info = update.latest()
            raise ValueError("This Cortex was not installed with CortexSetup.exe. Update it "
                             f"with: {update.command(info['tag'])}")

        def run(job):
            info, out = self._check(job)
            if not out["newer"]:
                return {**out, "launched": False}

            def said(got, total):
                job.say(f"downloaded {got >> 20} of {total >> 20} MB")
            exe = update.download(info, update.dest_dir(info), said, job.cancelled)
            if exe is None:
                return {"cancelled": True}
            job.say("checked; starting the installer")
            update.launch(exe, os.getpid(), update.install_dir())
            threading.Timer(2, self.stop.set).start()  # the page reads the result first
            return {**out, "launched": True}
        return self.jobs.start("Updating Cortex", "update", run, cancellable=True)

    def quit(self, body: dict) -> dict:
        if self.jobs.running():  # "Running jobs are cancelled": a download's process too
            self.jobs.cur.cancel()
        self.stop.set()
        return {"ok": True}


# ---- HTTP -----------------------------------------------------------------------------------


def _handler(app: App):
    class H(BaseHTTPRequestHandler):
        server_version = "cortex-app"
        sys_version = ""
        timeout = 30

        def log_message(self, *a):  # nothing from requests is logged
            pass

        def _send(self, code: int, body, ctype: str = "application/json"):
            if self.command == "POST" and not getattr(self, "_drained", False):
                self._drained = True  # Windows resets a socket closed with unread data
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    if 0 < n <= MAX_BODY:
                        self.rfile.read(n)
                except (ValueError, OSError):
                    pass
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self._head(code, ctype, len(data))
            self.wfile.write(data)

        def _head(self, code: int, ctype: str, length: int | None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.end_headers()

        def _gate(self, query_token: str | None = None, need_token: bool = True) -> bool:
            hosts = {f"{HOST}:{app.port}", f"localhost:{app.port}"}
            if self.headers.get("Host") not in hosts:  # DNS rebinding
                self._send(403, {"error": "host"})
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{h}" for h in hosts}:
                self._send(403, {"error": "origin"})
                return False
            if not need_token:  # the static page shell holds no data; the API needs the key
                return True
            given = self.headers.get("X-Cortex-Token") or query_token or ""
            if not hmac.compare_digest(given.encode(), app.token.encode()):
                if self.command == "GET" and urlsplit(self.path).path == "/":
                    self._send(401, EXPIRED_INSTALLED if update.mode() == "installer"
                               else EXPIRED, "text/html; charset=utf-8")
                else:
                    self._send(401, {"error": "token"})
                return False
            app.touch()
            return True

        def do_GET(self):
            url = urlsplit(self.path)
            static = STATIC.get(url.path)
            qt = (parse_qs(url.query).get("t") or [None])[0] if static else None
            if not self._gate(qt, need_token=not static):
                return
            if static:
                name, ctype = static
                data = (ASSETS / name).read_bytes()
                if name == "index.html":  # the key goes only into a page opened with it
                    ok = bool(qt) and hmac.compare_digest(qt.encode(), app.token.encode())
                    data = data.replace(b"{{T}}", app.token.encode() if ok else b"") \
                        .replace(b"{{REOPEN}}", reopen().encode())
                self._send(200, data, ctype)
            elif url.path in app.get:
                self._run(app.get[url.path],
                          *([parse_qs(url.query)] if url.path == "/api/chat/get" else []))
            elif url.path in app.post:
                self._send(405, {"error": "use POST"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            path = urlsplit(self.path).path
            if not self._gate():
                return
            if path not in app.post:
                self._send(405 if path in app.get or path in STATIC else 404,
                           {"error": "not found"})
                return
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if path == UPLOAD:
                self._upload(ctype)
                return
            if ctype != "application/json":
                self._send(415, {"error": "Content-Type must be application/json"})
                return
            try:
                n = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._send(411, {"error": "length"})
                return
            if not 0 <= n <= MAX_BODY:
                self.close_connection = True
                self._send(413, {"error": "too large"})
                return
            self._drained = True
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, UnicodeDecodeError, RecursionError):
                body = None
            if not isinstance(body, dict):
                self._send(400, {"error": "the body must be a JSON object"})
                return
            if path == "/api/chat":
                self._chat(body)
                return
            self._run(app.post[path], body)

        def _chat(self, body: dict):
            """Stream one chat turn as NDJSON on this thread (not a job: chat works while an
            import runs). The client going away sets cancel; the partial answer is kept."""
            from cortex import chat, localllm
            try:
                vault, cid, text = app.chat_begin(body)
            except Busy as e:
                self._send(409, {"error": str(e)})
                return
            except ValueError as e:
                self._send(400, {"error": str(e)[:500]})
                return
            cancel, ended, wlock = threading.Event(), threading.Event(), threading.Lock()

            def emit(ev: dict):
                if cancel.is_set():
                    return
                app.touch()
                with wlock:  # the keepalive thread writes too
                    if ended.is_set():
                        return
                    try:
                        self.wfile.write(json.dumps(ev).encode() + b"\n")
                        self.wfile.flush()
                    except OSError:  # BrokenPipe, ConnectionReset/Aborted: the page stopped
                        cancel.set()
                    if ev.get("t") in ("done", "err"):
                        ended.set()

            def keepalive():
                # a closed page shows only on a write: while the model reads or reasons quietly,
                # write now and then, so Stop ends the turn (and frees the slot) early
                while not ended.wait(KEEPALIVE_S):
                    emit({"t": "wait"})
            try:
                self.close_connection = True  # no length: the end of the answer ends the body
                self._head(200, "application/x-ndjson", None)
                threading.Thread(target=keepalive, daemon=True, name="chat-keepalive").start()
                # local only, never cloudllm; looked up only when the turn needs the model
                chat.reply(vault, cid, text, localllm.find, emit, cancel)
            except Exception as e:  # noqa: BLE001  (the type only: a message could quote a page)
                emit({"t": "err", "code": "internal",
                      "message": f"internal error ({type(e).__name__})"})
            finally:
                ended.set()
                app.chatting.release()

        def _upload(self, ctype: str):
            """Stream an export to ~/.cortex/tmp/upload-<id>-<stem><ext>, owner-only (0600).
            Of the client's name (X-Cortex-Name) only the extension and up to 40 letters,
            digits, _ and - of its stem are kept: `importers.detect` reads names such as
            MyActivity.json. The size is capped at MAX_UPLOAD."""
            self.close_connection = True  # a refused body is left unread
            if ctype != "application/octet-stream":
                self._send(415, {"error": "Content-Type must be application/octet-stream"})
                return
            name = PurePosixPath(unquote(self.headers.get("X-Cortex-Name", "")).replace("\\", "/"))
            ext = name.suffix.lower()
            if ext not in UPLOAD_EXT:
                self._send(400, {"error": "Add the export as it came: a .zip, .json, .html, .md, "
                                          ".txt or .ics file."})
                return
            stem = re.sub(r"[^A-Za-z0-9_-]", "", name.stem)[:40]
            try:
                n = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._send(411, {"error": "length"})
                return
            if not 0 < n <= MAX_UPLOAD:
                self._send(413, {"error": "The file is larger than 4 GB; unzip it and paste "
                                          "the path of the file inside instead."})
                return
            self._drained = True
            d = actions.tmp_dir()
            d.mkdir(mode=0o700, parents=True, exist_ok=True)  # Windows: the profile's own ACL
            uid = secrets.token_hex(8)
            p, left = d / f"upload-{uid}{'-' * bool(stem)}{stem}{ext}", n
            try:
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                             0o600)
                with os.fdopen(fd, "wb") as f:
                    while left:
                        chunk = self.rfile.read(min(left, 1 << 20))
                        if not chunk:
                            raise OSError("short body")
                        f.write(chunk)
                        left -= len(chunk)
            except OSError:
                p.unlink(missing_ok=True)
                self._send(400, {"error": "The upload stopped before the end; try again."})
                return
            app.uploads[uid] = p
            self._send(200, {"upload": uid, "size": n})

        def _run(self, fn, *args):
            try:
                self._send(200, fn(*args))
            except Busy as e:
                self._send(409, {"error": str(e)})
            except ValueError as e:
                self._send(400, {"error": str(e)[:500]})
            except Exception as e:  # noqa: BLE001  (the type only: a message could quote a page)
                self._send(500, {"error": f"internal error ({type(e).__name__})"})

        def _refuse(self):
            self._send(405, {"error": "method"})

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse  # noqa: N815

    return H


def make_server(root: Path | None, port: int = 0) -> tuple[ThreadingHTTPServer, App]:
    """The app's server on 127.0.0.1 only (port 0 picks a free one)."""
    app = App(root)
    actions.sweep_uploads()  # left by an app that was killed before its `finally` ran
    actions.sweep_updates()  # installers an update already ran
    from cortex.browse_serve import _Server  # no port sharing: Windows binds a used port
    srv = _Server((HOST, port), _handler(app))  # and the token would go to its owner
    srv.daemon_threads = True
    app.port = srv.server_address[1]
    return srv, app


def state_file() -> Path:
    return Path.home() / ".cortex" / "app.json"


def _save_state(app: App) -> None:
    """~/.cortex/app.json {port, token, pid}, owner-only: a second launch opens this app."""
    p = state_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".app-{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"port": app.port, "token": app.token, "pid": os.getpid()}, f)
    os.replace(tmp, p)


def _drop_state(app: App) -> None:
    try:
        if json.loads(state_file().read_text(encoding="utf-8")).get("token") == app.token:
            state_file().unlink()
    except (OSError, ValueError, AttributeError):
        pass


def running_url() -> str | None:
    """The link of a Cortex app that runs now (app.json names it and it answers with that
    key), else None: a stale or unreadable file is ignored."""
    import http.client
    try:
        s = json.loads(state_file().read_text(encoding="utf-8"))
        port, token = int(s["port"]), str(s["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    c = http.client.HTTPConnection(HOST, port, timeout=2)
    try:
        c.request("GET", "/api/job", headers={"Host": f"{HOST}:{port}", "X-Cortex-Token": token})
        ok = c.getresponse().status == 200
    except OSError:
        return None
    finally:
        c.close()
    return f"http://{HOST}:{port}/?t={token}" if ok else None


def run(root: Path | None, *, open_browser: bool = True, port: int = 0, echo=print) -> None:
    """Serve until Quit, Ctrl+C or 30 minutes with no request and no job."""
    try:
        srv, app = make_server(root, port)
    except OSError as e:
        if not port:
            raise
        raise SystemExit(f"Port {port} is in use or not allowed ({e.strerror or e}). Pick "
                         "another, or leave --port out for a free one.") from None
    url = f"http://{HOST}:{app.port}/?t={app.token}"
    try:
        _save_state(app)
    except OSError:  # no single-instance file: a second launch starts its own app
        traceback.print_exc(limit=1)
    try:
        news.start()  # a fresh install remembers this version, so it shows no What's new
    except Exception:  # noqa: BLE001  (an unwritable config only means no card; the app opens)
        traceback.print_exc(limit=3)
    try:
        actions.migrate_login(app.root if app.entry() else None)
    except Exception:  # noqa: BLE001  (Start at login stays as it was; the app still opens)
        traceback.print_exc(limit=3)

    def watch():
        while not app.stop.wait(5):
            if time.monotonic() - app.last > IDLE_S and not app.jobs.running():
                echo("cortex app: 30 minutes idle; stopping.")
                break
        app.stop.set()
        srv.shutdown()

    def auto():  # browse.auto_import_hours: first check after a minute, then every 10
        from cortex import browse_serve
        wait = browse_serve.AUTO_FIRST_S
        while not app.stop.wait(wait):
            wait = browse_serve.AUTO_EVERY_S
            try:
                app.auto_import()
            except Exception as e:  # noqa: BLE001  (the next check tries again)
                echo(f"cortex app: automatic import check failed ({type(e).__name__})")

    threading.Thread(target=watch, daemon=True).start()
    threading.Thread(target=auto, daemon=True, name="cortex-app-auto-import").start()
    echo(f"Cortex app: {url}")
    echo("It runs on this computer only. Close it with Quit on the page or Ctrl+C here.")
    if open_browser:
        _open(url)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
        _drop_state(app)
        app.receiver.stop()
        for uid in list(app.uploads):  # exports added but never imported
            app._drop(uid)
        srv.server_close()
