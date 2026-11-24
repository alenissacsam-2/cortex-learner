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
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, unquote, urlsplit

from cortex import update
from cortex.app import actions, exports, news, status, targets, wizard

HOST = "127.0.0.1"
IDLE_S = 30 * 60
PRIVATE_S = 10 * 60  # the Private switch relocks by itself after 10 minutes
UPDATE_FIRST_S, UPDATE_EVERY_S = 60, 6 * 3600  # the quiet update check: a minute in, then 6 h
WATCH_S = 5  # seconds between idle checks
WARM_STATUS = True  # `start` asks Status's slow probes at once (tests turn it off: conftest)
PIGGYBACK = True  # C8: a fact-harvest pass after each Chat turn (tests turn it off: conftest)
MAX_BODY = 64 * 1024
PICTURE = "/api/brain/picture"  # Save picture: a PNG in base64, so it has its own body cap
PICTURE_MAX = 8 * 1024 * 1024  # decoded bytes
MAX_UPLOAD = 4 * 2**30
UPLOAD = "/api/import/upload"  # the one raw (not JSON) POST: an export file, streamed to disk
UPLOAD_EXT = (".zip", ".json", ".html", ".md", ".txt", ".ics", ".mbox",
              ".pdf", ".docx",  # these two: a document, or a file attached in Chat (C1)
              ".apkg", ".colpkg")  # Anki exports (R22)
CLOUD_FIELDS = ("cloud_processing", "provider", "model", "base_url", "api_key_env")
IMPORTS = ("claude", "chatgpt", "gemini", "perplexity", "google-activity", "notes", "calendar",
           "docs", "repos", "highlights", "mail", "selfchat", "location",
           "photos", "spotify", "voice-memos", "anki")
ASSETS = Path(__file__).resolve().parents[1] / "assets" / "app"
_JS, _CSS = "text/javascript; charset=utf-8", "text/css; charset=utf-8"
# path -> (its file, type); "/" is the live home, filled by dashboard.render_live
STATIC = {"/": (None, "text/html; charset=utf-8"),
          "/manage": (ASSETS / "index.html", "text/html; charset=utf-8"),
          "/app.css": (ASSETS / "app.css", _CSS), "/app.js": (ASSETS / "app.js", _JS),
          # quick capture's box (the desktop app's hotkey): its own small page
          "/capture": (ASSETS / "capture.html", "text/html; charset=utf-8"),
          "/capture.js": (ASSETS / "capture.js", _JS),
          "/dashboard.css": (ASSETS.parent / "dashboard.css", _CSS),
          **{"/" + n: (ASSETS.parent / n, _JS) for n in ("d3.v7.min.js", "brain3d.js",
                                                        "dashboard.js")}}
CAPTURE_MAX = 1000  # quick capture: a line's characters
URL_ONLY = re.compile(r"https?://\S+", re.I)  # a quick-capture line that is one link
PAGE_ROWS = 20  # the private chats list, a page at a time
PORT = 47615  # tried first: one origin across launches keeps the page's theme and brain style
QUERY_GETS = {"/api/chat/get", "/api/chat/passage", "/api/home",  # GETs that read the query
              "/api/home/delta", "/api/home/site", "/api/status", "/api/browse/private-chats",
              "/api/chat/pages"}
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
        self.piggy = threading.Lock()  # C8: one fact-harvest piggyback after a Chat turn at once
        self.echo = print  # `start` sets its own: the background passes' log lines
        from cortex import askfile
        self.files = askfile.Store()  # C1: files attached in Chat, in memory only
        self.last = time.monotonic()
        self.stop = threading.Event()
        self.desktop = None  # desktop.Desktop (window and tray) in the desktop app, else None
        self.private_until = 0.0  # the Private switch: ?private=1 applies until this monotonic
        self.update_ready: dict | None = None  # a newer release, prepared by the quiet check
        self.homes = (threading.Lock(), {})  # vault root -> its HomeCache
        self.warming: threading.Thread | None = None  # `warm_status`'s thread
        self.core = None  # (vault root, mcp_server.Cortex) for the Review's clicks
        self.reviewing = threading.Lock()  # one Review click at a time
        self.get = {"/api/status": self.status, "/api/status/report": self.status_report,
                    "/api/connect": self.connect,
                    "/api/app/autostart": self.autostart, "/api/app/notify": self.notify,
                    "/api/app/hotkey": self.hotkey,
                    "/api/home": self.home, "/api/home/version": self.home_version,
                    "/api/home/delta": self.home_delta, "/api/home/site": self.home_site,
                    "/api/home/check": self.cold_check, "/api/import/pickup": self.pickup,
                    "/api/vault/snapshot": None,
                    "/api/llm": self.llm, "/api/browse": self.browse, "/api/vault": self.vault,
                    "/api/setup": self.setup, "/api/chat": self.chat_state,
                    "/api/chat/list": self.chat_list, "/api/chat/get": self.chat_get,
                    "/api/chat/passage": self.chat_passage, "/api/chat/pages": self.chat_pages,
                    "/api/chat/settings": self.chat_settings_get,
                    "/api/update": self.update_state, "/api/job": self.jobs.view,
                    "/api/review": self.review, "/api/xray": self.xray,
                    "/api/tonight": self.tonight,
                    "/api/browse/private-chats": self.private_chats}
        self.post = {
            "/api/connect/plan": self.connect_plan, "/api/connect/apply": self.connect_apply,
            "/api/connect/path": self.connect_path,
            "/api/connect/copied": self.pack_copied,
            "/api/profiles": self.profiles_set,
            "/api/llm/settings": self.llm_settings, "/api/llm/load": self.llm_load,
            "/api/llm/test": self.llm_test, "/api/llm/pull": self.llm_pull,
            "/api/llm/size": self.llm_size,
            "/api/browse/settings": self.browse_settings, "/api/browse/cloud": self.cloud,
            "/api/browse/auto": self.auto_settings,
            "/api/chat": None,  # do_POST streams it
            "/api/chat/delete": self.chat_delete, "/api/chat/settings": self.chat_settings,
            "/api/chat/eval": self.chat_eval,
            "/api/chat/attach": self.chat_attach, "/api/chat/file/remove": self.chat_file_remove,
            "/api/chat/file/save": self.chat_file_save,
            "/api/browse/login": self.login,
            "/api/browse/process": self.process, "/api/browse/selftest": self.selftest,
            "/api/browse/scan": self.scan, "/api/browse/import": self.history_import,
            "/api/vault/export": self.export, "/api/vault/backup": self.backup,
            PICTURE: self.brain_picture,
            "/api/home/unlock": self.unlock, "/api/home/lock": self.lock,
            "/api/home/check/answer": self.cold_answer, "/api/home/date": self.home_date,
            "/api/home/layout": self.home_layout, "/api/home/fact": self.home_fact,
            "/api/vault/doctor": self.doctor, "/api/vault/git": self.git_settings,
            "/api/open": self.open,
            "/api/setup/vault": self.setup_vault, "/api/setup/embed": self.setup_embed,
            "/api/setup/voice": self.setup_voice,
            "/api/setup/index": self.setup_index, "/api/setup/install": self.setup_install,
            "/api/setup/git": self.setup_git,
            "/api/setup/pull": self.setup_pull, "/api/setup/test": self.setup_test,
            "/api/setup/start": self.setup_start, "/api/setup/model": self.setup_model,
            UPLOAD: None,  # do_POST streams it
            "/api/import/preview": self.import_preview, "/api/import/run": self.import_run,
            "/api/import/pickup/add": self.pickup_add,
            "/api/import/pickup/dismiss": self.pickup_dismiss,
            "/api/import/pickup/later": self.pickup_later,
            "/api/import/watch/add": self.watch_add,
            "/api/import/watch/remove": self.watch_remove,
            "/api/job/cancel": self.cancel, "/api/quit": self.quit,
            "/api/setup/skip": self.setup_skip, "/api/update/check": self.update_check,
            "/api/update/apply": self.update_apply, "/api/whats_new/seen": self.news_seen,
            "/api/window/show": self.window_show, "/api/app/autostart": self.autostart_set,
            "/api/app/notify": self.notify_set, "/api/app/chat-import": self.chat_import_set,
            "/api/app/hotkey": self.hotkey_set, "/api/capture/hide": self.capture_hide,
            "/api/review/approve": self.review_approve, "/api/review/dismiss": self.review_dismiss,
            "/api/review/still": self.review_still, "/api/review/retire": self.review_retire,
            "/api/review/conflict": self.review_conflict,
            "/api/review/rule/approve": self.review_rule_approve,
            "/api/review/rule/approve-all": self.review_rule_approve_all,
            "/api/review/resume": self.review_resume,
            "/api/review/rule/dismiss": self.review_rule_dismiss,
            "/api/review/decision/approve": self.review_decision_approve,
            "/api/review/decision/dismiss": self.review_decision_dismiss,
            "/api/review/forget/dismiss": self.review_forget_dismiss,
            "/api/review/import/dismiss": self.review_import_dismiss,
            "/api/review/unsafe/keep": self.review_unsafe_keep,
            "/api/review/outside/keep": self.review_outside_keep,
            "/api/review/outside/remove": self.review_outside_remove,
            "/api/review/crowded/keep": self.review_crowded_keep,
            "/api/review/playbook/keep": self.review_playbook_keep,
            "/api/review/promote/pin": self.review_promote_pin,
            "/api/review/promote/later": self.review_promote_later,
            "/api/xray/pin": self.xray_pin, "/api/xray/mute": self.xray_mute,
            "/api/xray/dryrun": self.xray_dryrun, "/api/xray/trace": self.xray_trace,
            "/api/xray/compare": self.xray_compare, "/api/xray/ack": self.xray_ack,
            "/api/tonight/undo": self.tonight_undo, "/api/capture": self.capture}

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
        from cortex import chat_import, egress
        if self.warming not in (None, threading.current_thread()):
            self.warming.join()  # its probes are already under way: wait for them, not ask twice
        if (q or {}).get("fresh") == ["1"]:
            status.forget()
        v = status.vault(self.root)
        entry = self.entry() if v["ok"] else None
        ready = v["ok"] and (_connect_skipped() or any(
            t["connected"] for t in status.kept(("targets", json.dumps(entry, sort_keys=True)),
                                                lambda: targets.describe(entry))))
        return {"rows": status.rows(self.root) + (self._paused_rows() if v["ok"] else []),
                "first_run": not v["ok"], "vault": v["path"], "update": self.update_state(),
                "whats_new": news.card(), "chat_import": chat_import.enabled(),
                "issue_url": f"https://github.com/{update.REPO}/issues/new?template=bug_report.yml",
                "connects": egress.summary(),  # cortex.egress: where Cortex connects
                "land": "status" if ready else "setup"}  # Setup until its key steps are done

    def status_report(self) -> dict:
        """Copy details: the same block as `cortex doctor --report` (no paths, notes or values)."""
        return {"text": status.report(self.root)}

    def _paused_rows(self) -> list[dict]:
        """RR-10: one Status row per paused Review producer, with its Resume; "imports" also
        when the owner undid most of the lines fact_harvest saved (fact_harvest.ledger)."""
        from cortex import fact_harvest, review
        from cortex.events import EventLog
        try:
            v = self._vault()
            events = list(EventLog(v).iter_events())
            led = review.ledger(events)
            undone = fact_harvest.ledger(v, events)
        except Exception:  # noqa: BLE001  (a Status row must never cost the board)
            return []
        rows = [{"id": f"review-{p}", "label": "Review", "state": "warn",
                 "value": f"{review.PRODUCERS[p]} paused",
                 "detail": f"You dismissed {x['reject']} of the last {x['accept'] + x['reject']} "
                           "of these cards, so Review stopped offering them.", "resume": p}
                for p, x in led.items() if x["paused"]]
        if undone["paused"] and not led[fact_harvest.PRODUCER]["paused"]:
            n = undone["accept"] + undone["reject"]
            rows.append({"id": f"review-{fact_harvest.PRODUCER}", "label": "Review",
                         "state": "warn",
                         "value": f"{review.PRODUCERS[fact_harvest.PRODUCER]} paused",
                         "detail": f"You undid {undone['reject']} of the last {n} lines Cortex "
                                   "saved from your chats, so it stopped saving them.",
                         "resume": fact_harvest.PRODUCER})
        return rows

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
        """Connect: the AI apps, plus (with a vault) the context packs for web chats and the
        per-agent profiles (plan v9 item 8)."""
        from cortex import access, packs, profiles
        from cortex.events import EventLog
        extra = {}
        if self.entry() is not None:
            v = self._vault()
            events = list(EventLog(v).iter_events())  # read once for every pack
            last = packs.copied(events)

            def size(n, s, text):  # R32a: changed only when copied before and now different
                h = packs.digest(text)
                return {"chars": s, "text": text, "hash": h,
                        "changed": (n, s) in last and last[(n, s)] != h}
            extra = {"packs": [{"id": n, "sizes": [size(n, s, t) for s, t in
                                                   packs.sized(v, n, events=events).items()]}
                               for n in packs.PACKS],
                     "profiles": profiles.load(v), "scopes": list(access.SCOPES)}
        return {"targets": targets.describe(self.entry()), "vault": self.entry() is not None,
                "skill_zip": str(_checkout() / "dist" / "cortex-skill.zip"),
                "skill_zip_found": (_checkout() / "dist" / "cortex-skill.zip").is_file(),
                "skill_url": skill_url(), **extra}

    def pack_copied(self, body: dict) -> dict:
        """R32a: the owner pressed Copy on a context pack; one log event, so Connect can say
        when the pack changed since. Only pack, chars and hash come from the app."""
        from cortex import packs
        from cortex.events import EventLog
        if not isinstance(body, dict) or set(body) != {"pack", "chars", "hash"}:
            raise ValueError("send pack, chars and hash only.")
        name, chars, h = body["pack"], body["chars"], body["hash"]
        if not isinstance(name, str) or name not in packs.PACKS:
            raise ValueError(f"pack must be one of {', '.join(packs.PACKS)}.")
        if type(chars) is not int or chars not in packs.SIZES:
            raise ValueError("chars must be 1500 or 5000.")
        if not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{16}", h):
            raise ValueError("hash must be 16 hex characters.")
        EventLog(self._vault()).append(session="cli", kind="log", source="self", fields={
            "payload": {"kind": "pack_copied", "pack": name, "chars": chars, "hash": h}})
        return {"ok": True}

    def profiles_set(self, body: dict) -> dict:
        """The owner's per-agent profiles, saved from the app only (never from MCP input)."""
        from cortex import profiles
        return {"profiles": profiles.save(self._vault(), body)}  # body: {profiles, unknown}

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

    def private_chats(self, query: dict) -> dict:
        """Imported chats made private for a sensitive category (chat_import.made_private),
        newest first, PAGE_ROWS from `offset`: titles, apps, dates and categories only, never
        chat text or browsing. The app's own page only: no MCP tool reads it. While the Private
        switch is locked: counts by category only (`locked`), no row."""
        from cortex import chat_import
        from cortex.events import EventLog
        try:
            at = max(0, int((query.get("offset") or ["0"])[0]))
        except ValueError:
            at = 0
        v = self._vault()
        rows = chat_import.made_private(v, EventLog(v).iter_events())
        cats: dict[str, int] = {}
        for r in rows:
            cats[r["category"]] = cats.get(r["category"], 0) + 1
        if not self.private_unlocked:  # titles only while the Private switch is open
            return {"rows": [], "total": len(rows), "offset": 0, "locked": True,
                    "by_category": cats}
        return {"rows": rows[at:at + PAGE_ROWS], "total": len(rows), "offset": at,
                "locked": False, "by_category": cats}

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
                "chats": chat.list(self._vault()), "files": self.files.list()}

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
            return {"id": None, "messages": [], "remembers": 0}
        msgs = chat.load(self._vault(), cid)  # load checks the id
        return {"id": cid, "messages": msgs,  # where the model's memory starts, for its line
                "remembers": chat.remembers(msgs, chat.get_settings()["sources"])}

    def chat_passage(self, query: dict) -> dict:
        """The passage an answer cited (?path=&at=&len=), under Chat's source switches of now
        (`chat.passage`): a page under a switch that is off is refused, never shown."""
        from cortex import chat
        q = {k: (query.get(k) or [""])[0] for k in ("path", "at", "len")}
        if not q["path"]:  # no page named: not found, as /api/home/site
            raise NotFound
        try:
            return chat.passage(self._vault(), q["path"], q["at"] or 0, q["len"] or 0,
                                chat.get_settings()["sources"])
        except PermissionError as e:
            raise ValueError(str(e)) from None

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
        home = {**home, **self._home_prefs(home)}
        return home if private is not None else {**home, "relock": True}

    # ---- UX-E: Home's pinned layout and its Get started board (config.yaml `app:`) ----
    def _home_prefs(self, home: dict) -> dict:
        """`home_layout` and `first_run_hidden` from config.yaml; while Get started shows,
        `connected`: an AI app is connected, as Setup's Connect step reads it."""
        from cortex import dashboard
        from cortex.app import desktop
        a = desktop._app_cfg()
        out = {"home_layout": dashboard.home_layout({"app": a}),
               "first_run_hidden": a.get("first_run_hidden") is True}
        if home.get("get_started") and not out["first_run_hidden"]:
            entry = self.entry()
            out["connected"] = any(t["connected"] for t in status.kept(
                ("targets", json.dumps(entry, sort_keys=True)), lambda: targets.describe(entry)))
        return out

    def home_layout(self, body: dict) -> dict:
        """Pin layout (`order`: Home's groups in order, null to unpin) and Get started's Hide
        (`first_run_hidden`: true), kept in config.yaml's `app:` as app.notify is."""
        from cortex import dashboard
        from cortex.app import desktop
        if not body or set(body) - {"order", "first_run_hidden"}:
            raise ValueError("send order or first_run_hidden")
        order = body.get("order")
        if "order" in body and order is not None and dashboard.home_layout(
                {"app": {"home_layout": order}}) is None:
            raise ValueError(f"order must list {', '.join(dashboard.HOME_GROUPS)}, each once")
        if "first_run_hidden" in body and body["first_run_hidden"] is not True:
            raise ValueError("first_run_hidden can only be true")

        def merge(a: dict) -> dict:
            a = {**a, **({"first_run_hidden": True} if "first_run_hidden" in body else {})}
            if "order" in body:
                a = {k: v for k, v in a.items() if k != "home_layout"}
                if order is not None:
                    a["home_layout"] = list(order)
            return a
        desktop._merge_app(merge)
        return self._home_prefs({})

    def home_fact(self, body: dict) -> dict:
        """Get started's one line about you, saved as a `//fact:` typed in a chat is (tags:
        me/profile under Background, the owner's own words)."""
        from cortex import tags
        from cortex.vault import Vault
        text = body.get("text")
        text = " ".join(text.split()) if isinstance(text, str) else ""
        if not text:
            raise ValueError("Write one line about you first.")
        if len(text) > tags.MAX_CHARS:
            raise ValueError(f"Keep it to {tags.MAX_CHARS} characters.")
        root, refused = self.need_root(), []
        with self.reviewing:
            saved = tags.apply_in(Vault(root), [("fact", text, self._today())], refused=refused)
        if refused:
            raise ValueError(refused[0])
        return {"saved": saved}

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

    # ---- the 90-second check on Home: self-graded, recorded through cortex_grade ----
    COLD_ANSWERS = {"forgot": ("wrong", None, "Forgot"), "shaky": ("correct", 2, "Shaky"),
                    "knew": ("correct", 4, "Knew it")}

    def cold_check(self) -> dict:
        """Up to 3 cloud-visible concepts due today or tomorrow, most overdue first, with each
        page's one-line summary (home.cold_check). Never a private concept."""
        from datetime import UTC, datetime

        from cortex import home
        return {"items": home.cold_check(self._vault(), datetime.now(UTC))}

    def cold_answer(self, body: dict) -> dict:
        """One answer: a closed-book recall item graded by the user (wrong; right at confidence
        2; right at confidence 4), written by cortex_grade's own code path as the Review's clicks
        are (`_click`: an `app_clicks` Cortex, a session closed at once so it is committed and
        `cortex undo` takes it back), with the app as client and model "self". A concept the
        check would not offer now (unknown, private, not due) is a 404."""
        from datetime import UTC, datetime

        from cortex import home, planner
        from cortex.catalog import Catalog
        from cortex.events import local_day
        from cortex.mcp_server import Cortex
        from cortex.vault import Vault
        cid, answer = body.get("id"), body.get("answer")
        if answer not in self.COLD_ANSWERS:
            raise ValueError("answer must be forgot, shaky or knew.")
        v = self._vault()
        day = local_day(datetime.now(UTC))
        catalog = Catalog.load(v)
        if not isinstance(cid, str) or cid not in home.cold_due(
                planner.current_states(v, day, catalog)[0], catalog,
                planner.hidden(v, catalog), day):
            raise NotFound
        verdict, confidence, said = self.COLD_ANSWERS[answer]
        kwargs = {"concept": cid, "question": "90-second check: recall what you know, then "
                  "reveal.", "item_type": "recall", "learner_answer": said, "verdict": verdict,
                  **({"confidence": confidence} if confidence else {})}
        root = self.need_root()
        with self.reviewing:
            if self.core is None or self.core[0] != root:
                core = Cortex(Vault(root))
                core.app_clicks = True  # every write here is the user's own click
                self.core = (root, core)
            core = self.core[1]
            client = core.sessions.client
            core.sessions.client = "cortex-app"  # server-set, as grader.client always is
            try:
                core.session = core.sessions.start("ambient", None, "self")
                core._reset()
                out = core._call("cortex_grade", core.cortex_grade, kwargs)
            finally:
                core.sessions.client = client
                if core.sessions.current is not None:
                    core.sessions.close(summary="90-second check in the Cortex app")
        if "error" in out:
            raise ValueError(out.get("message") or out["error"])
        return {"recorded": True}

    DATE_KINDS = ("exam", "deadline")

    def home_date(self, body: dict) -> dict:
        """Home's Add a date (R31): an exam or deadline as an assessment page {title, date,
        kind, outcome: pending}, written by cortex_add's own code path as an AI app's add is (an
        `app_clicks` Cortex, the app as client, a session closed at once so `cortex undo` takes
        it back), so the planner, Next up, Chat and the snapshot see it. Refused: a name empty
        or over 120 characters, a date that is not real or before today, another kind, and the
        same name on the same day (an assessment page already has it)."""
        from datetime import date

        from cortex.mcp_server import Cortex
        from cortex.vault import Vault
        what, when, kind = body.get("what"), body.get("when"), body.get("kind", "exam")
        what = what.strip() if isinstance(what, str) else ""
        if not what:
            raise ValueError("Give it a name, such as DSA exam.")
        if len(what) > 120:
            raise ValueError("Keep the name to 120 characters.")
        try:
            day = date.fromisoformat(when) if isinstance(when, str) else None
        except ValueError:
            day = None
        if day is None:
            raise ValueError("Pick a date.")
        if day.isoformat() < self._today():
            raise ValueError("That date has passed. Pick today or a later day.")
        if kind not in self.DATE_KINDS:
            raise ValueError("kind must be exam or deadline.")
        root = self.need_root()
        with self.reviewing:
            v = Vault(root)
            if any(str(n.fields.get("date") or "")[:10] == day.isoformat()
                   and str(n.fields.get("title") or "").casefold() == what.casefold()
                   for n in v.iter_notes("assessment")):
                raise ValueError("That is on your board already.")
            if self.core is None or self.core[0] != root:
                core = Cortex(v)
                core.app_clicks = True  # every write here is the user's own click
                self.core = (root, core)
            core = self.core[1]
            client = core.sessions.client
            core.sessions.client = "cortex-app"  # server-set, as grader.client always is
            try:
                core.session = core.sessions.start("ambient", None, "self")
                core._reset()
                # force: another day's page of the same name (last term's DSA exam) is no copy
                out = core._call("cortex_add", core.cortex_add, {
                    "kind": "assessment", "title": what, "force": True, "fields": {
                        "date": day.isoformat(), "kind": kind, "outcome": "pending"}})
            finally:
                core.sessions.client = client
                if core.sessions.current is not None:
                    core.sessions.close(summary="Added a date in the Cortex app")
        if "error" in out:
            raise ValueError(out.get("message") or out["error"])
        return {"id": out["id"], "title": what, "date": day.isoformat(), "kind": kind}

    def capture(self, body: dict) -> dict:
        """Quick capture (the desktop app's hotkey box): one typed line, written through the
        shared core (`_with_core`) as the owner's own words. //pref, //fact and //forget lines
        take the chat's own tag path (tags.find, tags.apply); a line that is only an http(s)
        URL becomes a resource titled with its host and path (no fetch), unless a resource in
        the index has that URL already; anything else is a journal line. {kind, said}: `said`
        is the hint line's text. A refusal (a secret, an unsafe line) is a ValueError with the
        core's own message, and nothing is written."""
        from datetime import date

        from cortex import chats, dates, tags
        from cortex.events import local_day
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Type something first.")
        if len(text) > CAPTURE_MAX:
            raise ValueError(f"Keep it to {CAPTURE_MAX:,} characters.")
        clean = chats.drop_private(text).strip()
        if not clean.replace(chats.PRIVATE, "").strip():
            raise ValueError("Nothing to save: a <private> part is never saved.")
        if found := tags.find(text):
            def tag(core) -> tuple[list[str], list[str]]:
                refused: list[str] = []
                day = local_day(core._now())
                return tags.apply(core, [(t, x, day) for t, x in found],
                                  refused=refused), refused
            lines, refused = self._with_core(tag, "Quick capture in the Cortex app")
            if refused:
                raise ValueError("; ".join(lines + refused))
            return {"kind": "tag", "said": "; ".join(lines) or "Already saved today."}
        parts = urlsplit(clean) if URL_ONLY.fullmatch(clean) else None
        if parts is not None and parts.hostname:
            want = clean.removesuffix("/")
            title = (parts.hostname + parts.path.removesuffix("/"))[:200]

            def link(core) -> dict:
                core.index.refresh()
                for n in core.index._notes.values():  # the index's parsed frontmatter
                    u = n["fields"].get("url")
                    if n["kind"] == "resource" and isinstance(u, str) \
                            and u.strip().removesuffix("/") == want:
                        return {"already": n["title"]}
                return core._call("cortex_add", core.cortex_add, {
                    "kind": "resource", "title": title, "force": True, "fields": {"url": clean}})
            out = self._with_core(link, "Quick capture in the Cortex app")
            if "error" in out:
                raise ValueError(out.get("message") or out["error"])
            if "already" in out:
                return {"kind": "link", "said": f"Already saved: {out['already']}"}
            return {"kind": "link", "said": "Link saved"}

        def journal(core) -> dict:
            now = core._now()
            # typed just now, so its relative dates get today's day (L1); _log_journal pins
            # nothing itself under app_clicks (mcp_server._pin_day), so this is the one pin
            line = dates.pin(clean, date.fromisoformat(local_day(now)))
            out = core._call("cortex_log", core.cortex_log,
                             {"kind": "journal", "payload": {"text": line}})
            return {**out, "hhmm": f"{now.astimezone():%H:%M}"}
        out = self._with_core(journal, "Quick capture in the Cortex app")
        if "error" in out:
            raise ValueError(out.get("message") or out["error"])
        return {"kind": "journal", "said": f"Added to today's journal {out['hhmm']}"}

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

    def tidy_about_me(self) -> dict:
        """D1-D3: Cortex's own sweep of the about-me lines (mcp_server.Cortex.tidy), at start
        and with each chat-import pass: waiting about-me lines move to their pages, near-copies
        and settled pairs are retired, in one session `cortex undo` can take back. Not an app
        click, so nothing is stamped `clicked`."""
        from cortex.mcp_server import Cortex
        from cortex.vault import Vault
        root = self.root
        if root is None or not (root / ".cortex").is_dir():
            return {"moved": 0, "retired": 0}
        from cortex import review
        with self.reviewing:  # never beside a Review click
            review.record_baseline(Vault(root))  # R5a, first run: the guard's baseline
            return Cortex(Vault(root)).tidy()

    def review(self) -> dict:
        """The Review view and its badge count (cortex.review); shown text only. UX-B: each
        card about a page carries `related` (`_related`)."""
        from cortex import review
        from cortex.events import EventLog
        v = self._vault()
        evs = list(EventLog(v).iter_events())
        return self._related(v, review.view(v, evs, self._today()), evs)

    # ---- UX-B: "#" pages in Chat, related pages beside Review cards ----
    RELATED = 3
    CARDS = ("pending", "expired", "stale", "rules", "imports", "unsafe", "outside", "crowded",
             "promote")  # the Review boards whose cards name their `page`

    def chat_pages(self, query: dict) -> dict:
        """The "#" list (?q=): pages Chat may read now whose title holds the words
        (`chat.pick_list`); {pages: [{id, title, kind}]}."""
        from cortex import chat
        q = (query.get("q") or [""])[0][:200]
        return {"pages": chat.pick_list(self._vault(), q, chat.get_settings()["sources"])}

    def _related(self, v, view: dict, evs: list) -> dict:
        """Each card with a `page` gets `related`: [{title, path}] of up to RELATED pages linked
        with it (its [[links]] either way: the edges arm of `semantic.linked`, the cheap one;
        the name arms cost ~180 ms a page at 5k), most links first, then by id; never the
        card's own, a rollup, a hub or a quieted page. Chat's kept index holds no private
        page, so none is listed (a card on a private page has none)."""
        from cortex import chat, chat_study, harvest, semantic
        from cortex.topics import is_hub
        cards = [x for k in self.CARDS for x in view.get(k) or []
                 if isinstance(x.get("page"), str)]
        # never build Chat's index (~3 s at 5k) just for Review: before Chat's first use, no line
        if not cards or chat._INDEX.get(v.root) is None:
            return view
        quiet, got = harvest.quiet(evs), {}
        with chat._INDEX_LOCK:
            idx = chat._index(v, chat.FRESH)
            paths = {i: p for p, i in idx._ids.items()}
            for page in {x["page"] for x in cards}:
                n: dict = {}
                for i, _ in semantic._neighbours(idx, page):
                    note = idx._notes.get(i)
                    if i != page and i in paths and note and note["kind"] != "rollup" \
                            and not is_hub(note) and quiet.get(i) != 0:
                        n[i] = n.get(i, 0) + 1
                got[page] = [{"title": chat_study._title(idx._notes[i]["title"]) or i,
                              "path": idx._rel(paths[i])}
                             for i in sorted(n, key=lambda i: (-n[i], i))[:self.RELATED]]
        for x in cards:
            x["related"] = got[x["page"]]
        return view

    def _click(self, tool: str, method: str, kwargs: dict, card: str | None = None,
               producer: str | None = None) -> dict:
        """One Review click, written by an mcp_server.Cortex with `app_clicks` set through the
        tool's own code path (cortex_patch, cortex_log), in a session closed at once so it is
        committed and `cortex undo` can take it back. Returns the fresh Review view. `card`
        (R3-4) and `producer` (RR-10, review.ledger): stamped on each change the click
        writes."""
        return self._clicks([(tool, method, kwargs, card, producer)])

    def _with_core(self, fn, summary: str):
        """fn(core) with the app's shared mcp_server.Cortex (`app_clicks` set: every write is
        the owner's own) under the Review lock, in a session closed at once so it is committed
        and `cortex undo` can take it back; fn's own result."""
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
                return fn(core)
            finally:
                if core.sessions.current is not None:
                    core.sessions.close(summary=summary)

    def _clicks(self, calls: list[tuple]) -> dict:
        """One answer's writes, each a `_click` (tool, method, kwargs, card, producer), all in
        one session, so one `cortex undo` takes the whole answer back (D5: an answer that
        covers near-copies). Stops at the first error; what was written before it is kept."""
        def run(core) -> dict:
            out: dict = {}
            try:
                for tool, method, kwargs, card, producer in calls:
                    core.sessions.stamp = {k: v for k, v in (("card", card),
                                                             ("producer", producer)) if v}
                    out = core._call(tool, getattr(core, method), kwargs)
                    if "error" in out:
                        break
            finally:
                core.sessions.stamp = {}
            return out
        out = self._with_core(run, "Review in the Cortex app")
        if "error" in out:
            raise ValueError(out.get("message") or out["error"])
        # R3-7: the rules-file block; R5a: a Keep or Remove on a line changed outside Cortex
        if any(c[2].get("id") == "me/core" or c[2].get("page") in ("outside", "core")
               for c in calls):
            try:  # follows the core at once
                targets.refresh_rules(self.entry())
            except Exception as e:  # noqa: BLE001  (the periodic pass tries again)
                print(f"cortex app: rules block refresh failed ({type(e).__name__})",
                      file=sys.stderr)
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

    def _waiting_group(self, p: dict) -> list[dict]:
        """D5: the waiting lines of `p`'s page that say nearly what `p` says, `p` first."""
        from cortex import review
        same = {x["key"]: x for x in review.pending(self._vault(), self._today())
                if x["page"] == p["page"]}
        return [same[k] for k in review.group(list(same.values()), p["key"]) if k in same]

    def review_approve(self, body: dict) -> dict:
        """Approve: the waiting line written as cortex_patch writes a confirmed one, stamped
        `confirm: clicked`; a teaching-contract rule applies only this way (E2). D5: its
        near-copies go with it: on a claim page each is approved too (cortex_patch keeps one of
        near-duplicates, D1), elsewhere they are dismissed. Only the first click is the card."""
        from cortex import claims
        p = self._waiting(body)
        calls = []
        for n, x in enumerate(self._waiting_group(p)):
            card = "pending" if n == 0 else None
            if n and x["page"] not in claims.PAGES:
                calls.append(("cortex_patch", "_dismiss", {
                    "page": x["page"], "section": x["section"], "text": x["text"]}, card,
                    self._producer(x)))
                continue
            rule = x["page"] == "teaching-contract"  # stored bare; a rule is a list line
            calls.append(("cortex_patch", "cortex_patch", {
                "id": f"me/{x['page']}", "op": "append_to_section", "target": x["section"],
                "text": "- " * rule + x["text"], "user_confirmed": True}, card,
                self._producer(x)))
        return self._clicks(calls)

    def review_dismiss(self, body: dict) -> dict:
        """Dismiss: a claim page's line goes to its `## Not me` (cortex_patch reject); another
        page's (the contract, the portrait) leaves a `dismiss` change. Never proposed again.
        D5: it covers the line's near-copies too."""
        from cortex import claims
        calls = []
        for n, p in enumerate(self._waiting_group(self._waiting(body))):
            card = "pending" if n == 0 else None
            if p["page"] in claims.PAGES:
                calls.append(("cortex_patch", "cortex_patch", {
                    "id": f"me/{p['page']}", "op": "reject",
                    "target": f"{p['section']}: {p['text']}"}, card, self._producer(p)))
            else:
                calls.append(("cortex_patch", "_dismiss", {
                    "page": p["page"], "section": p["section"], "text": p["text"]}, card,
                    self._producer(p)))
        return self._clicks(calls)

    def _line_group(self, cid: str) -> list[str]:
        """D5: the live line `cid` and, when it is on the Still true? or the unsafe board, the
        near-copies that card stands for (review.stale's `ids`, review.collapse)."""
        from cortex import review
        from cortex.events import EventLog
        v = self._vault()
        events = list(EventLog(v).iter_events())
        for x in review.stale(review.live_rows(v, events, self._today()), events,
                              self._today(), review.known_texts(v)):
            if cid in x["ids"]:
                return [cid, *(i for i in x["ids"] if i != cid)]
        hidden = review._private(v)
        return review.group([x for x in review.unsafe_lines(v, events)
                             if x["page"] not in hidden], cid, "id")

    def review_still(self, body: dict) -> dict:
        """Still true: a reconfirm stamped `confirm: clicked` (claims.tiers: confirmed), for
        the line and the near-copies its card stands for (D5)."""
        return self._clicks([("cortex_log", "cortex_log", {
            "kind": "reconfirm", "payload": {"claim": cid, "verdict": "yes"}}, None, None)
            for cid in self._line_group(self._live(body)["id"])])

    def review_retire(self, body: dict) -> dict:
        """Retire: cortex_patch's retire op moves the line to `## Past`. A //forget card's
        candidate is the forget producer's accept (RR-10); a crowded line's (G7) is stamped
        `crowded`, so it counts in that card's monthly cap. D5: on a Still true? or unsafe card
        the near-copies it stands for are retired too."""
        from cortex import claims, review, tags
        from cortex.events import EventLog
        r = self._live(body)
        v = self._vault()
        events = list(EventLog(v).iter_events())
        cards = [x["text"] for x in tags.forget_cards(v, events, self._today())
                 if any(c["id"] == r["id"] for c in x["candidates"])]
        forget = "forget" if cards else None
        card = forget or ("crowded" if any(x["id"] == r["id"] for x in review.crowded(
            v, review.live_rows(v, events, self._today()))[0]) else None)
        live = {x["id"]: x for x in claims.rows(v) if x["status"] == "live"}
        view = self._clicks([("cortex_patch", "cortex_patch", {
            "id": f"me/{live[cid]['page']}", "op": "retire", "target": cid,
            "user_confirmed": True}, card if n == 0 else None, forget)
            for n, cid in enumerate(self._line_group(r["id"]) if card is None else [r["id"]])
            if cid in live])
        if not forget or r["id"] not in live:
            return view
        # R10: a //forget card's line then goes from every store; the answer carries the receipt
        from cortex import forget as forget_
        from cortex import session
        mark = v.writes
        receipt = forget_.claim(v, live[r["id"]]["page"], r["id"], also=tuple(cards))
        session._commit(v, "cortex: forget a line", v.wrote_since(mark))
        return {**self.review(), "receipt": receipt, "receipt_lines": forget_.lines(receipt)}

    def review_crowded_keep(self, body: dict) -> dict:
        """G7: Keep, on a line the X-ray shows cut for space in most primes: the X-ray's Pin
        (prime serves it first, within the Preferences cap) and a `dismiss` change on id
        `crowded` holding its id's hash, stamped `crowded` for the monthly cap."""
        from cortex import review, xray
        from cortex.events import EventLog
        v, cid = self._vault(), body.get("id")
        if not any(x["id"] == cid for x in review.crowded(
                v, review.live_rows(v, EventLog(v).iter_events(), self._today()))[0]):
            raise NotFound
        with self.reviewing:
            xray.set_flag(v, "pin", xray.claim(cid), True)
        return self._click("cortex_patch", "_dismiss", {
            "page": "crowded", "section": "Crowded", "text": cid, "waiting": False}, "crowded")

    def review_playbook_keep(self, body: dict) -> dict:
        """G7: Keep, on a playbook no AI app was served lately: a `dismiss` change on id
        `playbooks` holding its id's hash, so it is not asked about for review.UNUSED_DAYS;
        stamped `unused` for the monthly cap. The page stays as it is."""
        from cortex import review
        from cortex.events import EventLog
        v, pid = self._vault(), body.get("id")
        if not any(x["id"] == pid for x in review.unused(
                v, list(EventLog(v).iter_events()), self._today())[0]):
            raise NotFound
        return self._click("cortex_patch", "_dismiss", {
            "page": "playbooks", "section": "Playbooks", "text": pid, "waiting": False},
            "unused")

    def _promoted(self, body: dict) -> dict:
        """R28a: the offered "Pin it to Core?" line (review.promote) whose id `body` names."""
        from cortex import review
        from cortex.events import EventLog
        v, cid = self._vault(), body.get("id")
        events = list(EventLog(v).iter_events())
        x = next((x for x in review.promote(v, review.live_rows(v, events, self._today()),
                                            events, self._today()) if x["id"] == cid), None)
        if x is None:
            raise NotFound
        return x

    def review_promote_pin(self, body: dict) -> dict:
        """R28a: Pin to Core: the line written to me/core's `## Core` as Approve writes a
        repeated correction there (`confirm: clicked`), stamped `promote_core` for the card's
        monthly cap."""
        from cortex import claims, review
        x = self._promoted(body)
        return self._click("cortex_patch", "cortex_patch", {
            "id": "me/core", "op": "append_to_section", "target": "Core",
            "text": " ".join(claims._MARK.sub("", x["text"]).split()),
            "user_confirmed": True}, review.PROMOTE)

    def review_promote_later(self, body: dict) -> dict:
        """R28a: Not now: a `dismiss` change on id `promote_core` holding the line id's hash,
        so it is not offered for review.PROMOTE_QUIET days; stamped for the monthly cap."""
        from cortex import review
        x = self._promoted(body)
        return self._click("cortex_patch", "_dismiss", {
            "page": review.PROMOTE, "section": "Core", "text": x["id"], "waiting": False},
            review.PROMOTE)

    def _producer(self, p: dict) -> str:
        """RR-10: the producer of a waiting line (review.producer_of)."""
        from cortex import review
        from cortex.events import EventLog
        v = self._vault()
        return review.producer_of(review._proposals(EventLog(v).iter_events()), p["page"],
                                  p["text"])

    def review_resume(self, body: dict) -> dict:
        """RR-10: Resume a paused Review producer (a `resume` change; Status names it)."""
        from cortex import fact_harvest, review
        from cortex.events import EventLog
        p, v = body.get("producer"), self._vault()
        if not review.ledger(EventLog(v).iter_events()).get(p, {}).get("paused") and not (
                p == fact_harvest.PRODUCER and fact_harvest.ledger(v)["paused"]):
            raise NotFound
        return self._click("cortex_patch", "_resume", {"producer": p})

    def review_conflict(self, body: dict) -> dict:
        """A conflict pair (plan 5b, D2: two of the owner's lines or two inferred ones that
        clash): `keep` both (a `keep_both` change, which also covers later near-copies of the
        pair, review.kept_cover), or `left` or `right`, which retires the other line and its
        near-copies (review.copies) through cortex_patch's retire op."""
        from cortex import review
        from cortex.events import EventLog
        v, key, keep = self._vault(), body.get("key"), body.get("keep")
        if keep not in ("both", "left", "right"):
            raise ValueError("keep must be both, left or right.")
        events = list(EventLog(v).iter_events())
        rows = review.live_rows(v, events, self._today())
        pair = next((c for c in review.conflicts(rows, events, review.known_texts(v))
                     if c["key"] == key), None)
        if pair is None:
            raise NotFound
        card = "conflict"
        if keep == "both":
            return self._click("cortex_patch", "_keep_both", {"page": pair["left"]["page"],
                                                              "pair": pair["key"]}, card)
        # D5: the other line's near-copies go too, so the card does not come back
        return self._clicks([("cortex_patch", "cortex_patch", {
            "id": f"me/{r['page']}", "op": "retire", "target": r["id"],
            "user_confirmed": True}, card if n == 0 else None, None)
            for n, r in enumerate(review.copies(rows, pair["right" if keep == "left"
                                                     else "left"]))])

    def _rule(self, body: dict) -> dict:
        from cortex import corrections, review
        from cortex.events import EventLog
        v, key = self._vault(), body.get("key")
        events = list(EventLog(v).iter_events())
        rows = review.live_rows(v, events, self._today())
        item = next((x for x in corrections.proposals(v, events, rows) if x["key"] == key), None)
        if item is None:
            raise NotFound
        return item

    def review_rule_approve(self, body: dict) -> dict:
        """A repeated correction (plan 5b) approved: me/core's `## Core` (a standing rule) or
        me/preferences' `## Personal` gets it as the user's own line (`corrections.bound`),
        written as Approve writes a waiting one (`confirm: clicked`)."""
        x = self._rule(body)
        return self._click("cortex_patch", "cortex_patch", {
            "id": f"me/{x['page']}", "op": "append_to_section", "target": x["section"],
            "text": x["text"],
            "user_confirmed": True}, "rule", "corrections")

    def review_rule_approve_all(self, body: dict) -> dict:
        """R3-4: Approve all, only on repeated corrections (mined patterns, each seen in 2+
        chats): every offered one of the shown `keys`, each written as its own Approve."""
        keys = body.get("keys")
        if not isinstance(keys, list):
            raise ValueError("keys must be the shown corrections' keys.")
        items = [x for k in dict.fromkeys(keys) if isinstance(k, str)
                 for x in [self._rule({"key": k})]]  # an unknown key is 404 before any write
        out = self.review()
        for x in items:
            out = self._click("cortex_patch", "cortex_patch", {
                "id": f"me/{x['page']}", "op": "append_to_section",
                "target": x["section"], "text": x["text"],
                "user_confirmed": True}, "rule", "corrections")
        return out

    def review_rule_dismiss(self, body: dict) -> dict:
        """A repeated correction dismissed: a `dismiss` change on page `corrections` holds its
        text's hash, so it is not offered again."""
        x = self._rule(body)
        return self._click("cortex_patch", "_dismiss", {
            "page": "corrections", "section": "Core", "text": x["text"], "waiting": False}, "rule",
            "corrections")

    def _decision(self, body: dict) -> dict:
        from cortex import more_sources
        from cortex.events import EventLog
        v, key = self._vault(), body.get("key")
        item = next((x for x in more_sources.decision_proposals(v, EventLog(v).iter_events())
                     if x["key"] == key), None)
        if item is None:
            raise NotFound
        return item

    def review_decision_approve(self, body: dict) -> dict:
        """RR-9: a decision-like commit subject approved becomes a decision page (status
        accepted, the commit's day, linked to the repo page; private when that page is)."""
        x = self._decision(body)
        from datetime import date, timedelta

        from cortex import loops  # follow up REVIEW_DAYS from the approve day, not the commit's
        due = date.fromisoformat(self._today()) + timedelta(days=loops.REVIEW_DAYS)
        fields = {"date": x["date"], "status": "accepted", "related": [x["page"]],
                  "review_on": due.isoformat(),
                  "summary": f"From my commit {x['hash'][:7]} in {x['project']} "
                             f"(\"{x['word']}\")."}
        if x["private"]:
            fields["private"] = True
        return self._click("cortex_add", "_decide", {"title": x["subject"], "fields": fields,
                                                     "hash": x["key"]}, "decision",
                           "decisions")

    def review_decision_dismiss(self, body: dict) -> dict:
        """RR-9: a dismissed commit decision leaves a `dismiss` change on id `decisions` with its
        key, so it is not offered again."""
        x = self._decision(body)
        return self._click("cortex_patch", "_dismiss", {
            "page": "decisions", "section": x["project"], "text": f"{x['page']} {x['hash']}",
            "waiting": False}, "decision", "decisions")

    def review_forget_dismiss(self, body: dict) -> dict:
        """A //forget card (cortex.tags) dismissed: a `dismiss` change on page `forget` holds
        the hash of its event id, so the card closes; no line changes."""
        from cortex import review, tags
        from cortex.events import EventLog
        v, key = self._vault(), body.get("key")
        cards = tags.forget_cards(v, EventLog(v).iter_events(), self._today())
        if not any(x["key"] == key for x in cards):
            raise NotFound
        return self._clicks([("cortex_patch", "_dismiss", {  # D5: the card's near-copies too
            "page": "forget", "section": "Forget", "text": k, "waiting": False},
            "forget" if n == 0 else None, "forget")
            for n, k in enumerate(review.group(cards, key))])

    def review_import_dismiss(self, body: dict) -> dict:
        """RR-18: a card for a hand-edited imported page dismissed: a `dismiss` change on id
        `imports` holding its key; both pages stay as they are."""
        from cortex import sources
        from cortex.events import EventLog
        v, key = self._vault(), body.get("key")
        if not any(x["key"] == key for x in sources.edited_pages(v, EventLog(v).iter_events())):
            raise NotFound
        return self._click("cortex_patch", "_dismiss", {
            "page": "imports", "section": "Imports", "text": key, "waiting": False})

    def review_unsafe_keep(self, body: dict) -> dict:
        """G6: Keep it, on a line that reads as an instruction to an AI or holds invisible
        characters: a `dismiss` change on id `unsafe` holding its key, so it is served again
        (review.unsafe_lines); the line stays as it is. Keep it out is Retire."""
        from cortex import review
        from cortex.events import EventLog
        v, key = self._vault(), body.get("key")
        lines = review.unsafe_lines(v, EventLog(v).iter_events())
        if not any(x["key"] == key for x in lines):
            raise NotFound
        hidden = review._private(v)
        return self._clicks([("cortex_patch", "_dismiss", {
            "page": "unsafe", "section": "Unsafe", "text": k, "waiting": False}, None, None)
            for k in review.group([x for x in lines if x["page"] not in hidden], key)])  # D5

    def _outside(self, body: dict) -> dict:
        """R5a: the card's line changed outside Cortex (review.outside_lines), by its key."""
        from cortex import review
        from cortex.events import EventLog
        v, key = self._vault(), body.get("key")
        hidden = review._private(v)
        x = next((x for x in review.outside_lines(v, EventLog(v).iter_events())
                  if isinstance(key, str) and x["key"] == key and x["page"] not in hidden), None)
        if x is None:
            raise NotFound
        return x

    def review_outside_keep(self, body: dict) -> dict:
        """R5a: Keep, on a line changed outside Cortex: a `dismiss` change on id `outside`
        holding its key, so AI apps are served it from now on; the line stays as it is."""
        x = self._outside(body)
        return self._click("cortex_patch", "_dismiss", {
            "page": "outside", "section": "Outside", "text": x["key"], "waiting": False})

    def review_outside_remove(self, body: dict) -> dict:
        """R5a: Remove, on a line changed outside Cortex: a claim line is retired to `## Past`
        (cortex_patch retire), any other line deleted (Cortex._remove_outside); undoable."""
        x = self._outside(body)
        if x["id"] and x["page"] != "teaching-contract":
            return self._click("cortex_patch", "cortex_patch", {
                "id": f"me/{x['page']}", "op": "retire", "target": x["id"],
                "user_confirmed": True})
        return self._click("cortex_patch", "_remove_outside", {"page": x["page"],
                                                               "key": x["key"]})

    # ---- Tonight (Gemini round 3 "Nightly Closeout"): close the day ----
    def tonight(self) -> dict:
        """The Tonight tab (cortex.tonight) for the current Cortex day, plus the Review view
        its Today's 5 board shows; read-only, shown text only."""
        from datetime import UTC, datetime

        from cortex import tonight
        from cortex.events import EventLog
        v = self._vault()
        return {**tonight.view(v, list(EventLog(v).iter_events()), datetime.now(UTC)),
                "review": self.review()}

    def tonight_undo(self, body: dict) -> dict:
        """Undo one AI app's session shown under "What AI apps learned today", as `cortex
        undo <session>` does (the CLI's own code, as a job). A session not shown there
        (unknown, or already undone: its events went with it) is 404. A session still open
        has no commit yet, so it is refused rather than "undone" with nothing reverted."""
        from cortex import cli
        from cortex.events import EventLog
        sid, root = body.get("session"), self.need_root()
        if not isinstance(sid, str) or sid not in {
                g["session"] for g in self.tonight()["learned"]}:
            raise NotFound
        if not any(e.get("session") == sid and e.get("kind") == "close"
                   for e in EventLog(self._vault()).iter_events()):
            raise ValueError("This AI app's session is still open, so nothing is saved to undo "
                             "yet. Undo works once the session closes.")

        def run(job):
            try:
                return actions.capture(job, cli.undo, sid, vault=root)
            finally:
                with self.reviewing:  # never mid-click: the Review clicks' Cortex reloads
                    self.core = None  # the reverted catalog

        return self.jobs.start("Undoing an AI app's changes", "tonight", run)

    # ---- Agent X-ray (v9 item 6): what prime and context served each AI app ----
    def xray(self) -> dict:
        from cortex import xray
        return xray.view(self._vault())

    def _xray_set(self, which: str, body: dict) -> dict:
        """Pin or Mute (`on`, default true) one id, or a private one by its view `token`. The
        app's click is the only way in: no MCP tool sets these (cortex.xray)."""
        from cortex import xray
        v, on = self._vault(), body.get("on", True)
        id = xray.resolve_token(v, body["token"]) if isinstance(body.get("token"), str) \
            else body.get("id")
        if not isinstance(on, bool) or id is None:
            raise ValueError("Pin or mute takes one page or claim id.")
        with self.reviewing:
            xray.set_flag(v, which, id, on)
        return self.xray()

    def xray_pin(self, body: dict) -> dict:
        return self._xray_set("pin", body)

    def xray_mute(self, body: dict) -> dict:
        return self._xray_set("mute", body)

    def xray_dryrun(self, body: dict) -> dict:
        """What cortex_prime would serve one AI app for a prompt (mcp_server.dry_prime), shown
        as an X-ray call: titles only. Nothing is written or logged, the prompt included."""
        from cortex import xray
        from cortex.mcp_server import dry_prime
        client, prompt = body.get("client"), body.get("prompt")
        if not isinstance(client, str) or len(client) > 100:
            raise ValueError("client must be an AI app's name, at most 100 characters.")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000:
            raise ValueError("Type a prompt of at most 2,000 characters.")
        v = self._vault()
        got = dry_prime(v, client, prompt)
        return xray.present(v, got["served"], got["trimmed"], got["chars"])

    @staticmethod
    def _xray_client(body: dict, key: str = "client") -> str:
        c = body.get(key)
        if not isinstance(c, str) or len(c) > 100:
            raise ValueError("client must be an AI app's name, at most 100 characters.")
        return c

    def xray_trace(self, body: dict) -> dict:
        """R27b: which of what Cortex gave one AI app lately a pasted reply echoes
        (xray.trace, local words only). The reply is never written or logged: not here, not in
        an X-ray row or event, and an error answers with its type or our own message only."""
        from cortex import xray
        client, text = self._xray_client(body), body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > xray.TRACE_MAX:
            raise ValueError(f"Paste a reply of at most {xray.TRACE_MAX:,} characters.")
        return xray.trace(self._vault(), client, text)

    def xray_compare(self, body: dict) -> dict:
        """R27b: two AI apps' dry runs for one prompt and what each one's profile withholds
        (xray.compare). Nothing is written or logged, the prompt included."""
        from cortex import xray
        a, b, prompt = self._xray_client(body, "a"), self._xray_client(body, "b"), \
            body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000:
            raise ValueError("Type a prompt of at most 2,000 characters.")
        if (a or "unknown") == (b or "unknown"):
            raise ValueError("Pick two different apps.")
        return xray.compare(self._vault(), prompt, a, b)

    def xray_ack(self, body: dict) -> dict:
        """R27b: Got it on an app's broad-read warning; like Pin and Mute, only this click sets
        it (xray.ack)."""
        from cortex import xray
        client, end = self._xray_client(body), body.get("end")
        if not isinstance(end, str) or len(end) > 40:
            raise ValueError("end must be the warning's time.")
        v = self._vault()
        with self.reviewing:
            xray.ack(v, client, end)
        return self.xray()

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
                "imports": status.imports(self.root if self.entry() else None),
                "watched": self._watched()}

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

    def connect_path(self, body: dict) -> dict:
        """Status's Claude Code Fix (status.claude_code_row): the installed Cortex's bin folder
        first on the user PATH, so the `cortex` Claude Code starts is the app's."""
        from cortex import clients
        from cortex.app import winpath
        exe = clients.installed_cortex()
        if exe is None:
            raise ValueError("This Cortex was not installed with CortexSetup.exe.")
        winpath.put_first(str(exe.parent))
        # this app's own PATH too, so Status and what it starts see the change at once
        os.environ["PATH"] = winpath.first(os.environ.get("PATH", ""), str(exe.parent))
        print("PATH: Cortex moved first")  # never the PATH itself: it may name private folders
        return {"ok": True, "say": "Done. Quit Claude completely and open it again, so its "
                                   f"Cortex tools use {update.current()}."}

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

    def safety_net_auto(self) -> dict | None:
        """One safety_net pass over the chat pages earlier imports wrote, in this thread, when
        the chat import is on, a vault is set up and no job runs; committed like an import.
        Its counts, or None when it did not run."""
        from cortex import chat_import, safety_net
        from cortex.vault import Vault
        if not chat_import.enabled() or not self.entry() or self.jobs.running():
            return None
        v = Vault(self.root)
        if not safety_net.pending(v):  # stats only: no model probe when nothing changed
            return None
        runner, embed = safety_net.background_tools()
        try:
            return safety_net.run(v, runner, embed)
        finally:
            actions._commit(v, "safety net")

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

    def piggyback(self) -> threading.Thread | None:
        """C8: after a Chat turn ends, a daemon thread waits fact_harvest.PIGGY_WAIT_S and then
        runs fact_harvest.after_chat, unless the app is quitting or a new Chat turn started
        meanwhile. At most one at a time (`piggy`); it never holds `chatting`. The thread, or
        None when one already runs."""
        if not PIGGYBACK or not self.piggy.acquire(blocking=False):
            return None

        def go():
            from cortex import fact_harvest
            try:
                if self.stop.wait(fact_harvest.PIGGY_WAIT_S) or self.chatting.locked():
                    return  # quitting, or the owner asked again: that turn's end tries again
                c = fact_harvest.after_chat(self)
                if c:  # counts only: never a page's text
                    self.echo("cortex app: facts from chats after a Chat turn: " + ", ".join(
                        f"{k} {n}" for k, n in sorted(c.items()) if k != "session"))
            except Exception as e:  # noqa: BLE001  (the next turn or chats pass tries again)
                self.echo(f"cortex app: saving facts after a Chat turn failed "
                          f"({type(e).__name__})")
            finally:
                self.piggy.release()

        t = threading.Thread(target=go, daemon=True, name="fact-piggyback")
        try:
            t.start()
        except RuntimeError:  # no thread to spare: never keep `piggy` held
            self.piggy.release()
            return None
        return t

    def chat_delete(self, body: dict) -> dict:
        from cortex import chat
        if self.chatting.locked():  # the answer's final save would bring the chat back
            raise Busy("An answer is still being written; press Stop first, then delete.")
        if body.get("all") is True:
            self.files.clear()  # the chats are cleared: their attached files go too
            return {"deleted": chat.delete_all(self._vault())}
        return {"deleted": int(chat.delete(self._vault(), body.get("id")))}

    def chat_settings(self, body: dict) -> dict:
        from cortex import chat
        return chat.save_settings(body)

    # ---- C1: a file attached in Chat, kept in memory only (cortex.askfile) ----
    def chat_attach(self, body: dict) -> dict:
        """Read the file an upload brought (`body.upload`, named `body.name` by the page) into
        the attached files: parsed in the child, chunked and embedded in memory. The uploaded
        copy is deleted at once, read or not."""
        from cortex import askfile
        uid = body.get("upload")
        p = self.uploads.pop(uid, None) if isinstance(uid, str) else None
        if p is None:
            raise ValueError("That file is gone; attach it again.")
        try:
            name = askfile.safe_name(body.get("name"))
            if not name.lower().endswith(p.suffix.lower()):  # the upload's checked extension
                name += p.suffix
            f = askfile.read(p, name)
        finally:
            p.unlink(missing_ok=True)
        return self.files.add(f).info()

    def chat_file_remove(self, body: dict) -> dict:
        if body.get("all") is True:
            self.files.clear()
        else:
            self.files.remove(body.get("id"))
        return {"files": self.files.list()}

    def chat_file_save(self, body: dict) -> dict:
        """Save to your brain: the attached file through the documents import (askfile.save),
        committed as other imports are; {saved: {title, path}, files}."""
        from cortex import askfile
        from cortex.vault import Vault
        got = self.files.get([body.get("id")])
        if not got:
            raise ValueError("That file is no longer attached; attach it again.")
        v = Vault(self.need_root())
        saved = askfile.save(v, got[0])
        actions._commit(v, "docs")
        return {"saved": saved, "files": self.files.list()}

    def chat_eval(self, body: dict) -> dict:
        """Run Chat's offline test (cortex.chat_eval) as a job: a made-up vault in a temp
        folder, never the owner's; the result is the score, blockers and failures."""
        from cortex import chat_eval

        def run(job):
            r = chat_eval.run(cancel=job.cancelled, progress=lambda d, n, qid: job.progress(
                1, 1, d, n, f"Question {qid}"))
            return {"kind": "chat_eval", "passed": r["passed"], "ran": r["ran"],
                    "result": r["result"], "manifest": r["manifest"][:12],
                    "queries": {k: v["rate"] for k, v in r["queries"].items()},
                    "misses": {k: len(v) for k, v in r["misses"].items()},
                    "blocked": r["blocked"], "total": r["total"], "bar": r["pass_bar"],
                    "model": r["model"], "model_note": r["model_note"], "stopped": r["stopped"],
                    "blockers": [{k: b[k] for k in ("name", "state")} for b in r["blockers"]],
                    "failures": [{"id": q["id"], "question": q["question"],
                                  "why": "; ".join(q["why"]), "answer": q["answer"][:300]}
                                 for q in r["questions"] if q["status"] == "fail"],
                    "recall": r["retrieval"]["recall"], "median_s": r["latency"]["median"]}
        return self.jobs.start("Running the Chat test", "chat", run, cancellable=True)

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

    def brain_picture(self, body: dict) -> dict:
        """Save picture: the brain page's PNG (`png`, base64; `day`, the page's day, names it).
        The desktop app asks where to save it (Downloads first) and writes it there; the browser
        app answers `download` and the page saves it itself. Never written anywhere else."""
        import base64
        import binascii
        bad = ValueError("That picture could not be saved.")
        b64 = body.get("png")
        if not isinstance(b64, str) or len(b64) > (PICTURE_MAX + 2) // 3 * 4:
            raise bad
        try:
            png = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise bad from None
        if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > PICTURE_MAX:
            raise bad
        win = self.desktop.win if self.desktop is not None else None
        if win is None:
            return {"download": True}
        day = body.get("day")
        if not (isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)):
            day = time.strftime("%Y-%m-%d")
        import webview
        got = win.create_file_dialog(webview.FileDialog.SAVE, directory=str(exports.downloads()),
                                     save_filename=f"cortex-brain-{day}.png",
                                     file_types=("PNG picture (*.png)",))
        if not got:
            return {"cancelled": True}
        dest = Path(got if isinstance(got, str) else got[0])
        try:
            dest.write_bytes(png)  # the owner's chosen file, outside the vault
        except OSError:  # a read-only folder, a full disk
            raise bad from None
        return {"saved": dest.name}

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

    def setup_voice(self, body: dict) -> dict:
        return self.jobs.start("Installing the speech model", "setup", wizard.voice,
                               cancellable=True)

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
        root = self.need_root() if source in actions.TIERED or (
            source == "google-activity" and not keep) else self.root

        def run(job):
            out = None
            try:
                out = fn(job, source, p, root)
                return out
            finally:  # kept after a preview, or to retry once the search model is in
                if out is None or not (keep or out.get("need")):
                    self._drop(uid)
        return self.jobs.start(title, "setup", run,
                               cancellable=source in ("google-activity", "voice-memos"),
                               quiet_errors=source == "google-activity")

    def import_preview(self, body: dict) -> dict:
        return self._import(body, "Checking the file", actions.import_preview, keep=True)

    def import_run(self, body: dict) -> dict:
        # C2: the tier the page's choice sent; only the notes and calendar cards send one
        if body.get("tier") not in (None, "skip", "local", "cloud"):  # before the job: the
            raise ValueError("tier must be one of skip, local, cloud")  # upload is kept
        fn = actions.import_run if body.get("tier") is None else \
            partial(actions.import_run, tier=body["tier"])
        return self._import(body, "Importing", fn, keep=False)

    # B2-b: an export in Downloads, offered on Home (cortex.app.exports)
    def pickup(self) -> dict:
        return exports.offer()

    def pickup_add(self, body: dict) -> dict:
        """Add chats: the Import page's import of the offered zip, without its preview."""
        c = exports.pick(body.get("path"))
        out = self._import({"source": c["source"], "path": c["path"]}, "Importing",
                           actions.import_run, keep=False)
        exports.mark(c, "added")
        return out

    def pickup_dismiss(self, body: dict) -> dict:
        exports.mark(exports.pick(body.get("path")), "dismissed")
        return {"ok": True}

    def pickup_later(self, body: dict) -> dict:
        exports.later()
        return {"ok": True}

    # R18: watched folders (cortex.watch), re-imported by the periodic pass (`watch_auto`)
    def _watched(self) -> dict | None:
        from cortex import watch
        if self.root is None or not (self.root / ".cortex").is_dir():
            return None
        return watch.listing(self._vault())

    def watch_add(self, body: dict) -> dict:
        from cortex import watch
        from cortex.vault import VaultError
        path, source, tier = body.get("path"), body.get("source"), body.get("tier")
        if source not in watch.SOURCES:
            raise ValueError(f"source must be one of {', '.join(watch.SOURCES)}")
        if tier not in (None, "skip", "local", "cloud"):
            raise ValueError("tier must be one of skip, local, cloud")
        if not isinstance(path, str | None) or (not (path or "").strip() and source != "zotero"):
            raise ValueError("Paste the folder's full path.")
        try:  # an empty path: Zotero finds its own library
            watch.add(self._vault(), (path or "").strip() or None, source, tier)
        except VaultError as e:
            raise ValueError(str(e)) from None
        return {"ok": True, "watched": self._watched()}

    def watch_remove(self, body: dict) -> dict:
        from cortex import watch
        if not isinstance(body.get("path"), str) or not watch.remove(self._vault(), body["path"]):
            raise ValueError("That folder is not watched.")
        return {"ok": True, "watched": self._watched()}

    def watch_auto(self) -> list | None:
        """One pass over the watched folders (cortex.watch.run), in the periodic pass's thread,
        when a vault is set up and no job runs; committed like an import. Its results, or None
        when it did not run."""
        from cortex import watch
        from cortex.vault import Vault
        if not self.entry() or self.jobs.running():
            return None
        v = Vault(self.root)
        try:
            return watch.run(v)
        finally:
            actions._commit(v, "watched folders")

    def forget_due(self) -> list | None:
        """R10: the //forget lines a prompt hook or cortex_prime retired and queued, swept from
        every store (cortex.forget.run_due), in the periodic pass's thread when a vault is set
        up and no job runs; committed. The receipts, or None when it did not run."""
        from cortex import forget, session
        from cortex.vault import Vault
        if not self.entry() or self.jobs.running():
            return None
        v = Vault(self.root)
        try:
            return forget.run_due(v)
        finally:
            if v.writes:
                session._commit(v, "cortex: forget a line", v.wrote_since(0))

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

    def hotkey(self) -> dict:
        """Quick capture's key: the choice, the choices, and whether another app holds it
        (`taken`). `desktop`: false in the browser app, which has no hotkey."""
        from cortex.app import desktop
        h = self.desktop.hotkey if self.desktop is not None else None
        return {"desktop": h is not None, "key": desktop.hotkey_setting(),
                "choices": [{"key": k, "label": v[2] if v else "Off"}
                            for k, v in desktop.HOTKEYS.items()],
                "taken": bool(h and h.taken)}

    def hotkey_set(self, body: dict) -> dict:
        from cortex.app import desktop
        desktop.save_hotkey(body)
        if self.desktop is not None:
            self.desktop.change_hotkey()  # registered again now, no restart
        return self.hotkey()

    def capture_hide(self, body: dict) -> dict:
        """The capture box's Esc, a saved line, or the box losing focus."""
        return {"hidden": self.desktop is not None and self.desktop.hide_box()}


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
            try:
                self._head(code, ctype, len(data), extra)
                self.wfile.write(data)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                self.close_connection = True  # the browser left mid-answer: nothing to tell it

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
                if url.path in ("/manage", "/capture"):
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
            if not 0 <= n <= (PICTURE_MAX * 3 // 2 if path == PICTURE else MAX_BODY):
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
                # local only, never cloudllm; looked up only when the turn needs the model. An
                # attached file (C1) is asked like any question: the person asked, so the
                # configured local model may start, as for every Chat turn
                files = app.files.get(body.get("files"))
                chat.reply(vault, cid, text, localllm.find, emit, cancel, files=files,
                           pages=body.get("pages"))  # UX-B: ids picked with "#"
            except Exception as e:  # noqa: BLE001  (the type only: a message could quote a page)
                emit({"t": "err", "code": "internal",
                      "message": f"internal error ({type(e).__name__})"})
            finally:
                ended.set()
                app.chatting.release()
                app.piggyback()  # C8: the model is warm; a short fact-harvest pass

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
                                          ".txt, .ics, .mbox, .pdf, .docx, .apkg or .colpkg "
                                          "file."})
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


def start_url(app: App, now=None) -> str:
    """The page the app opens on when it starts: Home, or Tonight from 20:00 until the Cortex
    day rolls over at 04:00 local time (the owner's choice), once a vault exists. `now`: a
    naive local datetime (tests); later navigation is never moved."""
    from datetime import datetime

    from cortex.events import DAY_ROLLOVER
    h = (now or datetime.now()).hour
    night = (h >= 20 or h < DAY_ROLLOVER.seconds // 3600) and app.root is not None \
        and (app.root / ".cortex").is_dir()
    return f"http://{HOST}:{app.port}/" + (f"manage?t={app.token}#tonight" if night
                                           else f"?t={app.token}")


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
    app.echo = echo
    url = start_url(app)
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
        try:
            from cortex.browse import legacy
            legacy.migrate(app._vault())  # the browse files from before the store, once
        except Exception:  # noqa: BLE001  (tried again at the next start or import)
            traceback.print_exc(limit=3)

    def watch():
        while not app.stop.wait(WATCH_S):
            if idle and time.monotonic() - app.last > IDLE_S and not app.jobs.running():
                echo("cortex app: 30 minutes idle; stopping.")
                break
        app.stop.set()
        srv.shutdown()

    def forget_due():  # R10: a //forget's sweep, queued by a hook or cortex_prime
        try:
            app.forget_due()
        except Exception as e:  # noqa: BLE001  (the next pass tries again)
            echo(f"cortex app: forgetting a //forget line failed ({type(e).__name__})")

    def auto():  # the live import: first after a minute, then every 10
        from cortex import browse_serve
        wait = browse_serve.AUTO_FIRST_S
        forget_due()  # once at start, then with each pass
        while not app.stop.wait(wait):
            wait = browse_serve.AUTO_EVERY_S
            try:
                app.auto_import()
            except Exception as e:  # noqa: BLE001  (the next check tries again)
                echo(f"cortex app: live import failed ({type(e).__name__}); retried in "
                     f"{browse_serve.AUTO_EVERY_S // 60} minutes")
            try:  # R18: the watched folders' new and changed files; stats only when none
                app.watch_auto()
            except Exception as e:  # noqa: BLE001  (the next pass tries again)
                echo(f"cortex app: watched folders failed ({type(e).__name__})")
            forget_due()
            try:  # the rollup pages after the import; a stamp check when nothing changed
                if app.root is not None and (app.root / ".cortex").is_dir() \
                        and not app.jobs.running():
                    from cortex import rollups
                    rollups.run(app._vault())
            except Exception as e:  # noqa: BLE001  (the next pass tries again)
                echo(f"cortex app: rollup pages failed ({type(e).__name__})")

    def tidy():  # D1-D3: the about-me sweep, at start and with each chat-import pass
        try:
            app.tidy_about_me()
        except Exception as e:  # noqa: BLE001  (the next pass tries again)
            echo(f"cortex app: tidying the about-me lines failed ({type(e).__name__})")

    def chats():  # app.chat_import: the first pass a minute after start, then every 5 minutes
        from cortex import chat_import
        wait = chat_import.FIRST_S
        tidy()
        while not app.stop.wait(wait):
            wait = chat_import.EVERY_S
            tidy()
            # the safety net reads the pages the last import wrote; first, since the import
            # below starts a job and the net waits while any job runs
            try:
                app.safety_net_auto()
            except Exception as e:  # noqa: BLE001  (the next pass tries again)
                echo(f"cortex app: the chat safety net failed ({type(e).__name__}); retried in "
                     f"{chat_import.EVERY_S // 60} minutes")
            try:  # facts the owner stated in those chats, saved as about-me guesses
                from cortex import fact_harvest
                fact_harvest.auto(app)
            except Exception as e:  # noqa: BLE001  (the next pass tries again)
                echo(f"cortex app: saving facts from chats failed ({type(e).__name__})")
            try:
                app.chat_import_auto()
            except Exception as e:  # noqa: BLE001  (the next pass tries again)
                echo(f"cortex app: automatic chat import check failed ({type(e).__name__})")
            try:  # plan v9 item 8: the Cortex block in agents' rules files follows the core
                targets.refresh_rules(app.entry())
            except Exception as e:  # noqa: BLE001  (the next pass tries again)
                echo(f"cortex app: rules block refresh failed ({type(e).__name__})")

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
