"""`cortex app`: a stdlib HTTP server on 127.0.0.1 and one page (docs/design/cortex-app.md).

Security, because the app changes other apps' config files:
- a per-launch random token on every request: the page, its CSS and JS take it as `?t=` (the
  page then cleans the URL); every API call sends it as the `X-Cortex-Token` header;
- the Host must be the app's own loopback address (DNS rebinding), and any Origin its own;
- GET never changes anything; changes are POST with `Content-Type: application/json`;
- CSP `default-src 'self'`, no inline script; the page builds its DOM without innerHTML;
- in the browser it stops after 30 minutes idle, or on Quit; the desktop app (desktop.py: its
  own window and a tray icon) runs until Quit.
Long actions run as background jobs, one at a time, with a progress line (`Jobs`).
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import socket
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
PRIVATE_S = 10 * 60  # the Private switch relocks by itself after 10 minutes
UPDATE_FIRST_S, UPDATE_EVERY_S = 60, 6 * 3600  # the quiet update check: a minute in, then 6 h
WATCH_S = 5  # seconds between idle checks
WARM_STATUS = True  # `start` asks Status's slow probes at once (tests turn it off: conftest)
MAX_BODY = 64 * 1024
MAX_UPLOAD = 4 * 2**30
UPLOAD = "/api/import/upload"  # the one raw (not JSON) POST: an export file, streamed to disk
UPLOAD_EXT = (".zip", ".json", ".html", ".md", ".txt", ".ics")
CLOUD_FIELDS = ("cloud_processing", "provider", "model", "base_url", "api_key_env")
IMPORTS = ("claude", "chatgpt", "gemini", "perplexity", "google-activity", "notes", "calendar")
ASSETS = Path(__file__).resolve().parents[1] / "assets" / "app"
_JS, _CSS = "text/javascript; charset=utf-8", "text/css; charset=utf-8"
# path -> (its file, type); "/" is the live home, filled by dashboard.render_live
STATIC = {"/": (None, "text/html; charset=utf-8"),
          "/manage": (ASSETS / "index.html", "text/html; charset=utf-8"),
          "/app.css": (ASSETS / "app.css", _CSS), "/app.js": (ASSETS / "app.js", _JS),
          "/dashboard.css": (ASSETS.parent / "dashboard.css", _CSS),
          **{"/" + n: (ASSETS.parent / n, _JS) for n in ("d3.v7.min.js", "brain3d.js",
                                                        "dashboard.js")}}
PORT = 47615  # tried first: one origin across launches keeps the page's theme and brain style
QUERY_GETS = {"/api/chat/get", "/api/home", "/api/home/delta",  # GETs that read the query
              "/api/home/site", "/api/status"}
CSP = ("default-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")
EXPIRED = (b"<!doctype html><meta charset=utf-8><title>Cortex</title><p>This link has no valid "
           b"key. Open the link that <code>cortex app</code> printed, or run it again.</p>")
EXPIRED_INSTALLED = (b"<!doctype html><meta charset=utf-8><title>Cortex</title><p>This link "
                     b"has no valid key. Open Cortex from the Start menu again.</p>")


def snapshot_name() -> str:
    return f"cortex-snapshot-{time.strftime('%Y-%m-%d')}.html"


def reopen() -> str:
    """How this user reopens the app (A12): the Start menu for an installer install."""
    return ("Open Cortex from the Start menu to reopen it." if update.mode() == "installer"
            else "Run cortex app again to reopen it.")
_open = webbrowser.open  # tests replace this


def _checkout() -> Path:
    from cortex import cli
    return cli.CHECKOUT


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


class NotFound(Exception):  # a 404: the item does not exist, or may not be shown now
    pass


class Job:
    def __init__(self, name: str, section: str, cancellable: bool):
        self.name, self.section, self.cancellable = name, section, cancellable
        self.lines: list[str] = []
        self.result = self.error = None
        self.running, self.started, self.ended = True, time.time(), None
        self.cancelled = threading.Event()
        self.proc = None
        self.steps: dict | None = None  # a stepped job's place (`progress`)

    def progress(self, step: int, steps: int, done: int, total: int, label: str) -> None:
        """Where a stepped job is: step `step` of `steps`, named `label`, `done` of `total` in
        it. The overall share never goes back."""
        frac = min(max(done / total, 0.0), 1.0) if total else 0.0
        overall = ((step - 1) + frac) / steps if steps else 0.0
        if self.steps is not None:
            overall = max(overall, self.steps["overall"])
        self.steps = {"step": step, "steps": steps, "label": label, "done": done,
                      "total": total, "pct": round(frac * 100), "overall": overall,
                      "at": time.time()}

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
        out = {"name": self.name, "section": self.section, "running": self.running,
               "cancellable": self.cancellable, "line": self.lines[-1] if self.lines else "",
               "seconds": round((self.ended or time.time()) - self.started, 1),
               "result": self.result, "error": self.error}
        if (s := self.steps) is not None:
            took, overall = s["at"] - self.started, s["overall"]
            # time left from the pace so far, once there is a pace to go by
            eta = took * (1 - overall) / overall if overall >= 0.02 and took >= 2 else None
            out["progress"] = {**{k: s[k] for k in ("step", "steps", "label", "done", "total",
                                                    "pct")},
                               "overall": round(overall * 100),
                               "eta_s": None if eta is None else round(eta)}
        return out


class Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.cur: Job | None = None
        self.seq = 0
        self.on_end = None  # called with each job once it ends (the desktop app's notifier)

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
                status.forget()  # a job may have changed what Status shows
                if self.on_end is not None:
                    try:
                        self.on_end(job)
                    except Exception:  # noqa: BLE001  (a notification never fails the job)
                        traceback.print_exc(limit=3)

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


def _disk(body: dict, where: Path, need: int) -> dict | None:
    """{"disk": message} when a download of `need` bytes would leave less than 1 GB free in
    `where` and the owner has not chosen to continue anyway (`override`), else None."""
    ok, msg = wizard.free_space_ok(where, need)
    return None if ok or body.get("override") is True else {"disk": msg}


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
        self.scanned: dict = {}  # source key -> the last scan's counts
        self.uploads: dict[str, Path] = {}  # upload id -> its file under ~/.cortex/tmp
        self.chatting = threading.Lock()  # one chat answer at a time, beside any job
        self.last = time.monotonic()
        self.stop = threading.Event()
        self.desktop = None  # desktop.Desktop (window and tray) in the desktop app, else None
        self.private_until = 0.0  # the Private switch: ?private=1 applies until this monotonic
        self.update_ready: dict | None = None  # a newer release, prepared by the quiet check
        self.homes = (threading.Lock(), {})  # vault root -> its HomeCache
        self.warming: threading.Thread | None = None  # `warm_status`'s thread
        self.core = None  # (vault root, mcp_server.Cortex) for the Review's clicks
        self.reviewing = threading.Lock()  # one Review click at a time
        self.get = {"/api/status": self.status, "/api/connect": self.connect,
                    "/api/app/autostart": self.autostart, "/api/app/notify": self.notify,
                    "/api/home": self.home, "/api/home/version": self.home_version,
                    "/api/home/delta": self.home_delta, "/api/home/site": self.home_site,
                    "/api/vault/snapshot": None,
                    "/api/llm": self.llm, "/api/browse": self.browse, "/api/vault": self.vault,
                    "/api/setup": self.setup, "/api/chat": self.chat_state,
                    "/api/chat/list": self.chat_list, "/api/chat/get": self.chat_get,
                    "/api/chat/settings": self.chat_settings_get,
                    "/api/update": self.update_state, "/api/job": self.jobs.view,
                    "/api/review": self.review}
        self.post = {
            "/api/connect/plan": self.connect_plan, "/api/connect/apply": self.connect_apply,
            "/api/llm/settings": self.llm_settings, "/api/llm/load": self.llm_load,
            "/api/llm/test": self.llm_test, "/api/llm/pull": self.llm_pull,
            "/api/llm/size": self.llm_size,
            "/api/browse/settings": self.browse_settings, "/api/browse/cloud": self.cloud,
            "/api/browse/auto": self.auto_settings,
            "/api/chat": None,  # do_POST streams it
            "/api/chat/delete": self.chat_delete, "/api/chat/settings": self.chat_settings,
            "/api/browse/login": self.login,
            "/api/browse/process": self.process, "/api/browse/selftest": self.selftest,
            "/api/browse/scan": self.scan, "/api/browse/import": self.history_import,
            "/api/vault/export": self.export, "/api/vault/backup": self.backup,
            "/api/home/unlock": self.unlock, "/api/home/lock": self.lock,
            "/api/vault/doctor": self.doctor, "/api/vault/git": self.git_settings,
            "/api/open": self.open,
            "/api/setup/vault": self.setup_vault, "/api/setup/embed": self.setup_embed,
            "/api/setup/index": self.setup_index, "/api/setup/install": self.setup_install,
            "/api/setup/git": self.setup_git,
            "/api/setup/pull": self.setup_pull, "/api/setup/test": self.setup_test,
            "/api/setup/start": self.setup_start, "/api/setup/model": self.setup_model,
            UPLOAD: None,  # do_POST streams it
            "/api/import/preview": self.import_preview, "/api/import/run": self.import_run,
            "/api/job/cancel": self.cancel, "/api/quit": self.quit,
            "/api/setup/skip": self.setup_skip, "/api/update/check": self.update_check,
            "/api/update/apply": self.update_apply, "/api/whats_new/seen": self.news_seen,
            "/api/window/show": self.window_show, "/api/app/autostart": self.autostart_set,
            "/api/app/notify": self.notify_set, "/api/app/chat-import": self.chat_import_set,
            "/api/review/approve": self.review_approve, "/api/review/dismiss": self.review_dismiss,
            "/api/review/still": self.review_still, "/api/review/retire": self.review_retire}

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
        return clients.server_entry(clients.mcp_command(), self.root)

    # ---- GET (never changes anything) ----
    def status(self, q: dict | None = None) -> dict:
        """The Status board; its slow probes are `status.kept` (`?fresh=1`: asked again)."""
        from cortex import chat_import
        if self.warming not in (None, threading.current_thread()):
            self.warming.join()  # its probes are already under way: wait for them, not ask twice
        if (q or {}).get("fresh") == ["1"]:
            status.forget()
        v = status.vault(self.root)
        entry = self.entry() if v["ok"] else None
        ready = v["ok"] and (_connect_skipped() or any(
            t["connected"] for t in status.kept(("targets", json.dumps(entry, sort_keys=True)),
                                                lambda: targets.describe(entry))))
        return {"rows": status.rows(self.root),
                "first_run": not v["ok"], "vault": v["path"], "update": self.update_state(),
                "whats_new": news.card(), "chat_import": chat_import.enabled(),
                "land": "status" if ready else "setup"}  # Setup until its key steps are done

    def warm_status(self) -> None:
        """Status and Setup read once in the background at start: a first read waits ~3-4 s
        on the LLM probes (LM Studio's `lms`, refused loopback connects) and first imports;
        Status, Setup and Local model then answer from `status.kept` (refreshed after KEEP_S)."""
        def warm():
            self.status()
            self.setup()  # Settings opens on Setup until it is done: its probes too
        self.warming = threading.Thread(target=warm, daemon=True, name="cortex-app-warm-status")
        self.warming.start()

    def connect(self) -> dict:
        return {"targets": targets.describe(self.entry()), "vault": self.entry() is not None,
                "skill_zip": str(_checkout() / "dist" / "cortex-skill.zip"),
                "skill_zip_found": (_checkout() / "dist" / "cortex-skill.zip").is_file(),
                "skill_url": skill_url()}

    def llm(self) -> dict:
        """The Local model page: both runners' chat models (embedding models left out by the
        chat's own name rule) and the model sized to this computer."""
        from cortex import localllm
        if self.warming not in (None, threading.current_thread()):
            self.warming.join()  # the warm-up is asking the same probes: wait, not ask twice
        d, hw = status.llm(quick=True), wizard.hardware()  # kept like Status's (~3 s fresh)

        def chats(names):
            return [n for n in names if not localllm._NOT_CHAT.search(n)]
        return {**d, "ollama": None if d["ollama"] is None else chats(d["ollama"]),
                "ollama_state": wizard._ollama_state(d["ollama"]),  # the words Setup uses
                "models": [m for m in status.kept(("lms_models", None), status.lms_models)
                           if chats([m["key"]])],
                "hardware": hw, "recommend": wizard.model(wizard.recommend(hw["gb"], hw["kind"]))}

    def browse(self) -> dict:
        b = status.browse(self.root if self.entry() else None)
        return {**b, "history": status.history(self.root if self.entry() else None, self.scanned),
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

    def _home(self):
        from cortex.home import HomeCache
        from cortex.vault import Vault
        root = self.need_root()
        lock, homes = self.homes
        with lock:
            if root not in homes:
                homes.clear()  # one vault at a time
                homes[root] = HomeCache(Vault(root))
            return homes[root]

    # ---- the Private switch: private items only after Windows Hello, for 10 minutes ----
    @property
    def private_unlocked(self) -> bool:
        return time.monotonic() < self.private_until

    def _private(self, query: dict) -> bool | None:
        """True: the page asks for private items and may have them. None: it asks while the
        switch is locked (it expired, or the window was minimised), so it relocks."""
        if (query.get("private") or [""])[0] != "1":
            return False
        return True if self.private_unlocked else None

    def warm_home(self) -> None:
        try:
            self._home().get(False)
        except Exception as e:  # noqa: BLE001  (the page's own read reports a real problem)
            print(f"cortex app: the home warm-up failed ({type(e).__name__})", file=sys.stderr)

    def home(self, query: dict) -> dict:
        """The live home (cortex.home): the dashboard's data plus feed, next_up and exams.
        Builds and keeps the brain layout (`.cortex/brain-layout.json`, a view cache). A page
        that must relock gets the public home with `relock`."""
        private = self._private(query)
        home = self._home().get(private is True)
        return home if private is not None else {**home, "relock": True}

    def home_version(self) -> dict:
        """The home's version, the Private switch and a prepared update (the page's 3 s poll)."""
        r = self.update_ready
        return {"v": self._home().version(), "private": self.private_unlocked,
                "update": r and {"version": r["version"], "mode": r["mode"]}}

    def home_delta(self, query: dict) -> dict:
        """Changes since `since`; a page that must relock gets the public home whole, so no
        private item stays on it."""
        private = self._private(query)
        if private is None:
            return {**self._home().get(False), "full": True, "relock": True}
        return self._home().delta((query.get("since") or [None])[0], private)

    def home_site(self, query: dict) -> dict:
        """One website of the brain's stem, read from the browsing store (never the vault's
        Markdown). A private site while the switch is locked is a 404, as if it did not exist."""
        got = self._home().site((query.get("site") or [""])[0], self._private(query) is True)
        if got is None:
            raise NotFound
        return got

    def unlock(self, body: dict) -> dict:
        """The Private switch: Windows asks the user to verify (Windows Hello); private items
        then show for PRIVATE_S, until the switch is pressed again or the window minimised."""
        from cortex.app import hello
        ok, why = hello.verify()
        if ok:
            self.private_until = time.monotonic() + PRIVATE_S
        return {"unlocked": ok, "message": why}

    def lock(self, body: dict | None = None) -> dict:
        self.private_until = 0.0
        return {"ok": True}

    # ---- Review (plan 5a): what waits for the user's own click ----
    @staticmethod
    def _today() -> str:
        from datetime import UTC, datetime

        from cortex.events import local_day
        return local_day(datetime.now(UTC))

    def review(self) -> dict:
        """The Review view and its badge count (cortex.review); shown text only."""
        from cortex import review
        from cortex.events import EventLog
        v = self._vault()
        return review.view(v, EventLog(v).iter_events(), self._today())

    def _click(self, tool: str, method: str, kwargs: dict) -> dict:
        """One Review click, written by an mcp_server.Cortex with `app_clicks` set through the
        tool's own code path (cortex_patch, cortex_log), in a session closed at once so it is
        committed and `cortex undo` can take it back. Returns the fresh Review view."""
        from cortex.mcp_server import Cortex
        from cortex.vault import Vault
        root = self.need_root()
        with self.reviewing:
            if self.core is None or self.core[0] != root:
                core = Cortex(Vault(root))
                core.app_clicks = True  # every write here is the user's own click
                self.core = (root, core)
            core = self.core[1]
            try:
                out = core._call(tool, getattr(core, method), kwargs)
            finally:
                if core.sessions.current is not None:
                    core.sessions.close(summary="Review in the Cortex app")
        if "error" in out:
            raise ValueError(out.get("message") or out["error"])
        return self.review()

    def _waiting(self, body: dict) -> dict:
        from cortex import review
        key = body.get("key")
        item = next((p for p in review.pending(self._vault(), self._today())
                     if isinstance(key, str) and p["key"] == key), None)
        if item is None:
            raise NotFound
        return item

    def _live(self, body: dict) -> dict:
        from cortex import claims
        cid = body.get("id")
        row = next((r for r in claims.rows(self._vault()) if isinstance(cid, str)
                    and r["id"] == cid and r["status"] == "live"), None)
        if row is None:
            raise NotFound
        return row

    def review_approve(self, body: dict) -> dict:
        """Approve: the waiting line written as cortex_patch writes a confirmed one, stamped
        `confirm: clicked`; a teaching-contract rule applies only this way (E2)."""
        p = self._waiting(body)
        rule = p["page"] == "teaching-contract"  # a proposal is stored bare; a rule is a list line
        return self._click("cortex_patch", "cortex_patch", {
            "id": f"me/{p['page']}", "op": "append_to_section", "target": p["section"],
            "text": "- " * rule + p["text"], "user_confirmed": True})

    def review_dismiss(self, body: dict) -> dict:
        """Dismiss: a claim page's line goes to its `## Not me` (cortex_patch reject); another
        page's (the contract, the portrait) leaves a `dismiss` change. Never proposed again."""
        from cortex import claims
        p = self._waiting(body)
        if p["page"] in claims.PAGES:
            return self._click("cortex_patch", "cortex_patch", {
                "id": f"me/{p['page']}", "op": "reject",
                "target": f"{p['section']}: {p['text']}"})
        return self._click("cortex_patch", "_dismiss", {
            "page": p["page"], "section": p["section"], "text": p["text"]})

    def review_still(self, body: dict) -> dict:
        """Still true: a reconfirm stamped `confirm: clicked` (claims.tiers: confirmed)."""
        r = self._live(body)
        return self._click("cortex_log", "cortex_log", {
            "kind": "reconfirm", "payload": {"claim": r["id"], "verdict": "yes"}})

    def review_retire(self, body: dict) -> dict:
        """Retire: cortex_patch's retire op moves the line to `## Past`."""
        r = self._live(body)
        return self._click("cortex_patch", "cortex_patch", {
            "id": f"me/{r['page']}", "op": "retire", "target": r["id"], "user_confirmed": True})

    def chat_settings_get(self) -> dict:
        from cortex import chat
        return chat.get_settings()

    def setup(self) -> dict:
        from cortex import chats, cli, importers
        if self.warming not in (None, threading.current_thread()):
            self.warming.join()  # the warm-up is asking the same probes: wait, not ask twice
        return {**wizard.state(self.root, self.scanned),
                "connect": [{k: t[k] for k in ("id", "label", "detected", "connected")}
                            for t in targets.describe(self.entry())],
                "first_prompts": list(cli.FIRST_PROMPTS), "connect_skipped": _connect_skipped(),
                "guides": {**chats.GUIDES, **importers.GUIDES},
                "imports": status.imports(self.root if self.entry() else None)}

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

    def llm_size(self, body: dict) -> dict:
        """A model's download size, asked before the page offers to download it (one request
        to ollama.com for a name Setup does not list)."""
        n = wizard.ollama_size(body.get("name"))
        return {"name": body["name"], "bytes": n, "size": wizard.size_text(n),
                "source": "ollama.com"}

    def llm_pull(self, body: dict) -> dict:
        from cortex import localllm
        name = wizard.ollama_name(body.get("name"))
        if not localllm._local_name(status.ollama_models() or [], name) and (
                stop := _disk(body, wizard.models_dir("ollama"), wizard.ollama_size(name))):
            return stop
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
        """The live import, every AUTO_EVERY_S (browse.auto_import_hours 0 turns it off): only
        the new visits of the browsers and profiles imported before, quietly (no job: it takes
        seconds), when no job runs and the browse-wide lock is free; never before the user's
        first import. The site pages first move into the store (`store.migrate`), as a job
        with its steps when there are pages to move. {"live": counts}, {"job": ...}, or None
        when it did not run."""
        from cortex import browse_serve
        from cortex.browse import load_settings, store
        from cortex.vault import Vault
        if not self.entry() or self.jobs.running():
            return None
        if not status.embedding()["downloaded"]:  # every page would land under Other: wait
            return None                            # for the search model
        root = self.root
        v = Vault(root)
        if not browse_serve.auto_import_due(v, live=True):
            return None
        srcs = browse_serve.auto_sources(v)  # never an unticked one
        if not srcs:
            return None
        if not store.migrated(v):
            if store._site_page_files(v):
                try:
                    return self.jobs.start("Moving site pages into Cortex", "browse",
                                           lambda job: actions.browse_migrate(job, root),
                                           quiet_errors=True)
                except Busy:
                    return None
            store.migrate(v, settings=load_settings())  # no page to move: only marks it done
        return {"live": status.safe(browse_serve.run_import(v, srcs=srcs,
                                                            say=lambda s: None))}

    def chat_import_auto(self) -> dict | None:
        """Start a chat import job (chat_import.run) when the setting is on, a vault is set up,
        no job runs and a transcript is new or changed (stats only); None when it did not."""
        from cortex import chat_import
        from cortex.vault import Vault
        if not chat_import.enabled() or not self.entry() or self.jobs.running():
            return None
        root = self.root
        if not chat_import.pending(Vault(root)):
            return None
        try:
            return self.jobs.start("Chat import", "status",
                                   lambda job: actions.chat_import(job, root),
                                   cancellable=True, quiet_errors=True)
        except Busy:  # a job started meanwhile: the next pass tries again
            return None

    def chat_import_set(self, body: dict) -> dict:
        """Turn the automatic chat import on or off (`app.chat_import`)."""
        from cortex import chat_import
        from cortex.app import desktop
        on = _bool(body, "enabled")
        desktop._merge_app(lambda a: {**a, "chat_import": on})
        return {"enabled": chat_import.enabled()}

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

    def login(self, body: dict) -> dict:
        on, root = _bool(body, "on"), self.need_root()
        return self.jobs.start("Start at login" if on else "Removing start at login", "browse",
                               lambda job: actions.login(job, root, on))

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

    def snapshot(self) -> bytes:
        """The self-contained snapshot page (dashboard.snapshot: CSP default-src 'none', no
        private item)."""
        from datetime import UTC, datetime

        from cortex import dashboard
        return dashboard.snapshot(self._vault(), now=datetime.now(UTC)).encode()

    def export(self, body: dict) -> dict:
        """Export snapshot. The desktop app asks where to save it (a save dialog) and writes it
        as a job; the browser downloads it from GET /api/vault/snapshot."""
        self.need_root()
        win = self.desktop.win if self.desktop is not None else None
        if win is None:
            return {"download": "/api/vault/snapshot"}
        import webview
        got = win.create_file_dialog(webview.FileDialog.SAVE, save_filename=snapshot_name(),
                                     file_types=("Web page (*.html)",))
        if not got:
            return {"cancelled": True}
        dest = Path(got if isinstance(got, str) else got[0])

        def run(job):
            job.say("building the snapshot")
            dest.write_bytes(self.snapshot())  # the owner's chosen file, outside the vault
            return {"ok": True, "path": str(dest)}
        return self.jobs.start("Exporting a snapshot", "vault", run)

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
        if what == "skill_zip":
            p = _checkout() / "dist"
            if not p.is_dir():
                raise ValueError(f"{p} is not there; build it first.")
            _open_path(p)
            return {"ok": True}
        root = self.need_root()
        if what in ("vault", "private"):
            from cortex.vault import PRIVATE_BROWSE_DIR
            p = root / PRIVATE_BROWSE_DIR / "browse-private-index.md" if what == "private" \
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
        if stop := _disk(body, wizard.install_dir(), wizard.OLLAMA_BYTES):
            return stop
        return self.jobs.start("Installing Ollama", "setup", wizard.install_ollama,
                               cancellable=True)

    def setup_git(self, body: dict) -> dict:
        """winget's Git for this Windows account; scope "machine" only when the owner chose
        it after that failed. `from` "status": the result shows on Status."""
        scope = "machine" if body.get("scope") == "machine" else "user"
        if stop := _disk(body, wizard.install_dir(), wizard.GIT_BYTES):
            return stop
        root = self.root
        return self.jobs.start("Installing Git", "status" if body.get("from") == "status"
                               else "setup", lambda job: wizard.install_git(job, scope, root),
                               cancellable=True)

    def _model_runner(self, body: dict) -> tuple[str, str]:
        m, r = body.get("model"), body.get("runner")
        wizard.model(m if isinstance(m, str) else "")
        if r not in ("ollama", "lmstudio"):
            raise ValueError("runner must be ollama or lmstudio")
        return m, r

    @staticmethod
    def _model_disk(body: dict, m: str, r: str) -> dict | None:
        """The free-space refusal for a Setup model this runner does not hold yet."""
        have = wizard._have(wizard.model(m), status.ollama_models(),
                            status.lms_models() if r == "lmstudio" else [])[r]
        return None if have else _disk(body, wizard.models_dir(r), wizard.model(m)["bytes"])

    def setup_pull(self, body: dict) -> dict:
        m, r = self._model_runner(body)
        if stop := self._model_disk(body, m, r):
            return stop
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
        if stop := self._model_disk(body, m, r):
            return stop

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

    # ---- updates: checked and prepared quietly, applied only on a click ----
    def update_state(self) -> dict:
        return {"current": update.current(), "mode": update.mode(), "ready": self.update_ready}

    def update_quietly(self) -> None:
        """The background check (UPDATE_FIRST_S after start, then every UPDATE_EVERY_S): a
        newer release is downloaded and checked (installer), or its command noted (dev), then
        shown as Restart to update and notified once per version. Never applied here."""
        info, cur = update.latest(), update.current()
        if not update.newer(info["version"], update.parse_version(cur)):
            return
        version = ".".join(map(str, info["version"]))
        if self.update_ready and self.update_ready["version"] == version:
            return
        ready = {"version": version, "mode": update.mode(), "notes_url": info["notes_url"]}
        if ready["mode"] == "installer":
            dest = update.dest_dir(info)
            actions.sweep_updates(dest)
            if update.download(info, dest, None, self.stop) is None:  # the app is stopping
                return
        else:
            ready["command"] = update.command(info["tag"])
        self.update_ready = ready
        from cortex.app import desktop
        if self.desktop is not None and desktop.notify_settings()["updates"] \
                and desktop.load_state().get("update_notified") != version:
            self.desktop.notify(f"Cortex {version} is ready: restart to update")
            desktop.save_state("update_notified", version)

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
        """Download the new installer (unless the quiet check already did), check it, start it
        and quit; it reopens the app. Restart to update and Update now both come here."""
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
            ready = self.update_ready
            exe = update.dest_dir(info) / update.EXE  # checked when the quiet check fetched it
            if not (ready and ready["version"] == out["latest"] and exe.is_file()):
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

    # ---- the desktop app: window, Start with Windows, notifications ----
    def window_show(self, body: dict) -> dict:
        """A second `cortex-app` launch brings this app's window up (False: the browser app)."""
        return {"shown": self.desktop is not None and self.desktop.show()}

    def autostart(self) -> dict:
        from cortex.app import desktop
        return desktop.autostart_state()

    def autostart_set(self, body: dict) -> dict:
        from cortex.app import desktop
        out = desktop.set_autostart(_bool(body, "enabled"))
        if self.desktop is not None:
            self.desktop.refresh_tray()  # its Start with Windows tick
        return out

    def notify(self) -> dict:
        from cortex.app import desktop
        return desktop.notify_settings()

    def notify_set(self, body: dict) -> dict:
        from cortex.app import desktop
        return desktop.save_notify(body)


# ---- HTTP -----------------------------------------------------------------------------------


def _handler(app: App):
    class H(BaseHTTPRequestHandler):
        server_version = "cortex-app"
        sys_version = ""
        timeout = 30

        def log_message(self, *a):  # nothing from requests is logged
            pass

        def _send(self, code: int, body, ctype: str = "application/json", extra=None):
            if self.command == "POST" and not getattr(self, "_drained", False):
                self._drained = True  # Windows resets a socket closed with unread data
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    if 0 < n <= MAX_BODY:
                        self.rfile.read(n)
                except (ValueError, OSError):
                    pass
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self._head(code, ctype, len(data), extra)
            self.wfile.write(data)

        def _head(self, code: int, ctype: str, length: int | None, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.end_headers()

        def _redirect(self, where: str):
            self.send_response(302)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
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
                path, ctype = static
                # the key goes only into a page opened with it
                ok = bool(qt) and hmac.compare_digest(qt.encode(), app.token.encode())
                key = app.token if ok else ""
                if path is None:
                    try:
                        app.need_root()
                    except ValueError:  # first run: no vault yet, so Setup
                        self._redirect("/manage" + (f"?t={key}" if key else "") + "#setup")
                        return
                    from cortex import dashboard
                    data = dashboard.render_live(key).encode()
                else:
                    data = path.read_bytes()
                if url.path == "/manage":
                    data = data.replace(b"{{T}}", key.encode()) \
                        .replace(b"{{REOPEN}}", reopen().encode())
                self._send(200, data, ctype)
            elif url.path == "/api/vault/snapshot":  # Export snapshot in the browser: a download
                self._run(app.snapshot, ctype="text/html; charset=utf-8", extra={
                    "Content-Disposition": f'attachment; filename="{snapshot_name()}"'})
            elif url.path in app.get:
                self._run(app.get[url.path],
                          *([parse_qs(url.query)] if url.path in QUERY_GETS else []))
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
            status.forget()  # an action: Status asks its slow probes again
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

        def _run(self, fn, *args, ctype: str = "application/json", extra=None):
            try:
                self._send(200, fn(*args), ctype, extra)
            except Busy as e:
                self._send(409, {"error": str(e)})
            except NotFound:
                self._send(404, {"error": "not found"})
            except ValueError as e:
                self._send(400, {"error": str(e)[:500]})
            except Exception as e:  # noqa: BLE001  (the type only: a message could quote a page)
                self._send(500, {"error": f"internal error ({type(e).__name__})"})

        def _refuse(self):
            self._send(405, {"error": "method"})

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse  # noqa: N815

    return H


MAX_CONNECTIONS = 32  # open connections at once: one thread each


class _Server(ThreadingHTTPServer):
    """No port sharing: Windows binds a used port without SO_EXCLUSIVEADDRUSE, and the token
    would go to its owner. At most MAX_CONNECTIONS connections at once: a local program opening
    thousands of idle ones would otherwise start a thread for each; one over the cap is closed
    at once. Each handler's `timeout` bounds how long an idle one keeps its slot."""
    allow_reuse_address = False

    def __init__(self, *a, **kw):
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*a, **kw)

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:  # no thread started, so none will release the slot
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_server(root: Path | None, port: int = 0) -> tuple[ThreadingHTTPServer, App]:
    """The app's server on 127.0.0.1 only (port 0 picks a free one)."""
    app = App(root)
    actions.sweep_uploads()  # left by an app that was killed before its `finally` ran
    actions.sweep_updates(update.prepared())  # installers an update already ran; a newer one
    # downloaded and still matching its checksum stays for Restart to update
    srv = _Server((HOST, port), _handler(app))
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


def show_window(url: str) -> bool:
    """Ask the app at `url` (from running_url) to bring its window up; False when it has no
    window (the browser app) or does not answer."""
    import http.client
    u = urlsplit(url)
    token = (parse_qs(u.query).get("t") or [""])[0]
    c = http.client.HTTPConnection(HOST, u.port, timeout=10)
    try:
        c.request("POST", "/api/window/show", body=b"{}", headers={
            "Host": u.netloc, "X-Cortex-Token": token, "Content-Type": "application/json"})
        r = c.getresponse()
        return r.status == 200 and json.loads(r.read()).get("shown") is True
    except (OSError, ValueError):
        return False
    finally:
        c.close()


def start(root: Path | None, *, port: int = 0, echo=print, idle: bool = True,
          ) -> tuple[ThreadingHTTPServer, App, str]:
    """The app's server, app.json and its watch and auto-import threads, not yet serving: the
    caller runs `srv.serve_forever` and then `finish`. `idle`: stop after 30 minutes with no
    request and no job (the browser app; the desktop app runs until Quit). No `port`: PORT if
    it is free, else any free one."""
    try:
        srv, app = make_server(root, port or PORT)
    except OSError as e:
        if port:
            raise SystemExit(f"Port {port} is in use or not allowed ({e.strerror or e}). Pick "
                             "another, or leave --port out for a free one.") from None
        srv, app = make_server(root, 0)  # PORT is taken (a second Cortex, another program)
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
    if app.entry() is not None:
        try:
            from cortex import dashboard
            dashboard.retire_old(app._vault())  # the old .cortex/dashboard.html, once
        except Exception:  # noqa: BLE001  (it is moved at the next start; the app still opens)
            traceback.print_exc(limit=3)

    def watch():
        while not app.stop.wait(WATCH_S):
            if idle and time.monotonic() - app.last > IDLE_S and not app.jobs.running():
                echo("cortex app: 30 minutes idle; stopping.")
                break
        app.stop.set()
        srv.shutdown()

    def auto():  # the live import: first after a minute, then every 10
        from cortex import browse_serve
        wait = browse_serve.AUTO_FIRST_S
        while not app.stop.wait(wait):
            wait = browse_serve.AUTO_EVERY_S
            try:
                app.auto_import()
            except Exception as e:  # noqa: BLE001  (the next check tries again)
                echo(f"cortex app: live import failed ({type(e).__name__}); retried in "
                     f"{browse_serve.AUTO_EVERY_S // 60} minutes")

    def chats():  # app.chat_import: the first pass a minute after start, then every 5 minutes
        from cortex import chat_import
        wait = chat_import.FIRST_S
        while not app.stop.wait(wait):
            wait = chat_import.EVERY_S
            try:
                app.chat_import_auto()
            except Exception as e:  # noqa: BLE001  (the next pass tries again)
                echo(f"cortex app: automatic chat import check failed ({type(e).__name__})")

    def updates():  # the quiet update check: a minute after start, then every 6 hours
        wait = UPDATE_FIRST_S
        while not app.stop.wait(wait):
            wait = UPDATE_EVERY_S
            try:
                app.update_quietly()
            except Exception as e:  # noqa: BLE001  (offline, GitHub busy: the next check tries)
                echo(f"cortex app: the update check failed ({type(e).__name__})")

    threading.Thread(target=watch, daemon=True).start()
    threading.Thread(target=auto, daemon=True, name="cortex-app-auto-import").start()
    threading.Thread(target=chats, daemon=True, name="cortex-app-chat-import").start()
    threading.Thread(target=updates, daemon=True, name="cortex-app-updates").start()
    if WARM_STATUS:
        app.warm_status()
        if app.entry() is not None:  # the first page: its brain built before the window asks
            threading.Thread(target=app.warm_home, daemon=True, name="cortex-app-warm-home").start()
    return srv, app, url


def finish(srv: ThreadingHTTPServer, app: App) -> None:
    """After serving: app.json and unused uploads go; the port closes."""
    app.stop.set()
    _drop_state(app)
    for uid in list(app.uploads):  # exports added but never imported
        app._drop(uid)
    srv.server_close()


def run(root: Path | None, *, open_browser: bool = True, port: int = 0, echo=print) -> None:
    """Serve until Quit, Ctrl+C or 30 minutes with no request and no job."""
    srv, app, url = start(root, port=port, echo=echo)
    echo(f"Cortex app: {url}")
    echo("It runs on this computer only. Close it with Quit on the page or Ctrl+C here.")
    if open_browser:
        _open(url)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        finish(srv, app)
