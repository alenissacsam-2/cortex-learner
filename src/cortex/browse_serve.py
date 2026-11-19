"""The browse receiver: a loopback-only HTTP endpoint for the capture extension.

`pair` makes the token, `serve` runs the server. Every record goes through the privacy
filter (docs/design/browse-brain.md section 2) BEFORE anything is stored; accepted records
are appended to `.cortex/browse/inbox-YYYY-MM-DD.jsonl` under the vault lock. When a browsing
session ends (a batch with `session_end: true`, or 20 min without a batch after records were
stored) the import runs on a background thread, one run at a time. Nothing that holds a URL or
a title is ever logged.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cortex import browse
from cortex.browse import Settings, lists, load_settings
from cortex.browse.privacy import in_domains
from cortex.events import local_day
from cortex.vault import Vault, VaultError, secret_pattern

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 8799
MAX_BODY = 256 * 1024
MAX_BATCH = 500
MAX_ACTIVE_MS = 6 * 3600 * 1000
MAX_EXCERPT = 2000
MAX_TITLE = 500
MAX_URL = 4096
KINDS = {"page", "video", "search"}
IDLE_PROCESS_S = 20 * 60  # no batch for this long after records were stored: process them
DEBOUNCE_S = 10  # a session end waits this long, so several in a row make one run
DEEPEN_IDLE_S = 30 * 60  # no batch for this long: the local LLM's deepen pass, once
# GET /v1/hello?n=<nonce> needs no token and answers HMAC-SHA256(token, nonce) in hex, so the
# app and the extension can tell this receiver from another program on the port.
_HELLO = re.compile(r"/v1/hello\?n=([0-9a-f]{32,128})")
AUTO_IMPORT_HOURS = 6  # browse.auto_import_hours default; 0 turns the automatic import off
AUTO_FIRST_S = 60  # the first automatic-import check after a start
AUTO_EVERY_S = 10 * 60  # then this often
_EXT_ORIGIN = re.compile(r"(chrome-extension|moz-extension)://[A-Za-z0-9-]{1,64}")

# ---- privacy filter (spec section 2): cortex.browse's filter, shared with the importer ----


def filter_record(rec: dict, settings: Settings) -> dict | None:
    """A validated record through cortex.browse's section 2 filter: None to drop it, else
    the record with the scrubbed URL, a search's term as its title, and `private: True` for
    sensitive categories. The excerpt is cleared where only the site name may be kept, and on
    every private record (it is never needed: private pages list titles only)."""
    k = browse.filter_record(rec["url"], rec["title"], settings=settings,
                             channel=rec.get("channel", ""))
    if k is None:
        return None
    webmail = in_domains(k.host, lists.WEBMAIL_HOSTS)
    kind = "search" if k.search else "video" if k.video else (
        "page" if rec["kind"] == "search" or webmail else rec["kind"])
    out = {**rec, "url": k.url, "title": k.search or k.title, "kind": kind}
    if kind == "search" or webmail or k.private or secret_pattern(out["excerpt"]):
        out["excerpt"] = ""  # a page that shows a key keeps its visit, never the key
    if kind != "video":
        out.pop("channel", None)
    if k.private:
        out["private"] = True
    return out


# ---- validation --------------------------------------------------------------------------


def _start(v) -> datetime | None:
    try:
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            t = datetime.fromtimestamp(v / 1000, UTC)
        elif isinstance(v, str) and len(v) <= 40:
            t = datetime.fromisoformat(v.replace("Z", "+00:00"))
            t = t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    now = datetime.now(UTC)
    return t if datetime(2000, 1, 1, tzinfo=UTC) <= t <= now + timedelta(days=1) else None


def _utf8(v):
    """A string with lone surrogates (a page's `document.title = "\\ud800"`) replaced, so it
    always encodes; anything else as it is."""
    return v.encode("utf-8", "replace").decode("utf-8") if isinstance(v, str) else v


def validate(rec) -> dict | None:
    """A clean record with only the known fields, or None when it is malformed (a URL that
    does not encode as UTF-8 too)."""
    if not isinstance(rec, dict):
        return None
    url, title = rec.get("url"), _utf8(rec.get("title", ""))
    excerpt, kind, ms = _utf8(rec.get("excerpt", "")), rec.get("kind"), rec.get("active_ms")
    if isinstance(url, str) and _utf8(url) != url:
        return None
    start = _start(rec.get("start"))
    if not (isinstance(url, str) and 0 < len(url) <= MAX_URL and isinstance(title, str)
            and isinstance(excerpt, str) and kind in KINDS and start is not None
            and isinstance(ms, int) and not isinstance(ms, bool) and ms >= 0):
        return None
    out = {"url": url, "title": " ".join(title.split())[:MAX_TITLE],
           "start": start.strftime("%Y-%m-%dT%H:%M:%S.") + f"{start.microsecond // 1000:03d}Z",
           "active_ms": min(ms, MAX_ACTIVE_MS), "excerpt": excerpt[:MAX_EXCERPT], "kind": kind}
    channel = _utf8(rec.get("channel"))
    if kind == "video" and isinstance(channel, str) and channel.strip():
        out["channel"] = " ".join(channel.split())[:200]
    return out


# ---- storage -------------------------------------------------------------------------------


def _vault(vault) -> Vault:
    return vault if isinstance(vault, Vault) else Vault(Path(vault))


def _dir(v: Vault) -> Path:
    return v.root / ".cortex" / "browse"


def pair(vault) -> str:
    """Create or rotate the pairing token (`.cortex/browse/token`) and return it."""
    v = _vault(vault)
    token = secrets.token_urlsafe(32)
    v.write_text(_dir(v) / "token", token)
    return token


def _token(v: Vault) -> str | None:
    try:
        return (_dir(v) / "token").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def store(v: Vault, records: list[dict]) -> None:
    """Append filtered records to inbox-<local day of start>.jsonl, under the vault lock. All
    or nothing per batch: every line is encoded before any file is touched."""
    by_day: dict[str, list[bytes]] = {}
    for r in records:
        day = local_day(datetime.fromisoformat(r["start"].replace("Z", "+00:00")))
        by_day.setdefault(day, []).append(
            (json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    with v.lock():
        _dir(v).mkdir(parents=True, exist_ok=True)
        for day, lines in sorted(by_day.items()):
            path = _dir(v) / f"inbox-{day}.jsonl"
            with open(path, "ab+") as f:  # append like events.EventLog._write
                size = f.seek(0, os.SEEK_END)
                if size:
                    f.seek(size - 1)
                    if f.read(1) != b"\n":
                        f.write(b"\n")  # a torn last line stays alone, the new ones stay whole
                f.write(b"".join(lines))
                f.flush()
                os.fsync(f.fileno())
            v._track(path)


# ---- processing at session end -------------------------------------------------------------


def local_runner(budget_s: float | None = None):
    """(runner, budget) when config enables the local LLM and one answers, else (None, None).
    The budget is `budget_s`, else `localllm.budget_s` (default 300)."""
    from cortex import localllm

    if not localllm.enabled():
        return None, None
    try:
        runner = localllm.find(background=True)
    except ValueError:  # a non-loopback localllm.url is refused
        return None, None
    if runner is None:
        return None, None
    return runner, _budget(budget_s)


def _budget(budget_s: float | None):
    from cortex import localllm, taint

    if budget_s is None:
        cfg = taint.config().get("localllm")
        budget_s = cfg.get("budget_s", 300) if isinstance(cfg, dict) else 300
    return localllm.Budget(budget_s if isinstance(budget_s, (int, float)) else 300)


def runner_for(audience: str, local, cloud, settings: Settings | None = None):
    """The only place a runner is picked for browse work (docs/design/data-access.md §2): the
    cloud runner for a "cloud" item with browse.cloud_processing on; else the local runner.
    "local" never gets the cloud runner, even with no local one (no fallback); "skip" gets
    none. `settings` default to a fresh read, so turning cloud processing off stops the next
    call (the kill switch)."""
    if audience == "skip":
        return None
    settings = load_settings() if settings is None else settings
    if audience == "cloud" and cloud is not None and settings.cloud_processing:
        return cloud
    return local


def run_import(v: Vault, progress=None, say=None) -> dict:
    """What the import command runs, with every browser found and no `since`: the import
    itself picks the days with new History visits or changed inbox files (a `since` would skip
    the History days before it while still moving their cursor). The local LLM only when
    config enables it; never a download. Then the private browse vectors."""
    from cortex import semantic
    from cortex.browse import build, sites, sources

    say = say or _say
    runner, budget = local_runner()
    settings = load_settings()
    r = build.run(v, sources.chosen(settings), settings, embed=semantic.load_embedder(),
                  runner=runner, budget=budget, progress=progress, say=say)
    r["model"] = runner is not None
    sites.update_vectors(v, say)
    return r


def run_deepen(v: Vault, say=None, budget_s: float | None = None) -> dict | None:
    """The local LLM's pass over the site pages (cortex.browse.sites.deepen), each page's runner
    picked by `runner_for` (visible pages may go to the cloud runner when cloud processing is
    on), then the private browse vectors; None when neither runner is available. The cloud
    runner's run leaves one counts-only audit line."""
    from cortex import cloudllm
    from cortex.browse import sites

    say = say or _say
    local, budget = local_runner(budget_s)
    cloud = cloudllm.find() if load_settings().cloud_processing else None
    if local is None and cloud is None:
        return None
    try:
        r = sites.deepen(v, local, budget or _budget(budget_s), load_settings(), say=say,
                         pick=lambda audience: runner_for(audience, local, cloud),
                         fresh=lambda: load_settings())
    finally:
        if cloud is not None:
            cloudllm.audit(v, cloud)
    sites.update_vectors(v, say)
    return r


def auto_import_hours(config: dict | None = None) -> float:
    """`browse.auto_import_hours` (0-168; 0 = off); a missing or bad value is the default."""
    if config is None:
        from cortex.taint import config as read
        config = read()
    b = config.get("browse") if isinstance(config, dict) else None
    h = b.get("auto_import_hours", AUTO_IMPORT_HOURS) if isinstance(b, dict) else None
    ok = isinstance(h, (int, float)) and not isinstance(h, bool) and 0 <= h <= 168
    return h if ok else AUTO_IMPORT_HOURS


def auto_import_due(v: Vault, now: datetime | None = None) -> bool:
    """True when the last import is older than `auto_import_hours` and the browse-wide lock is
    free (no import, deepen or forget runs). Never before the user's first import: a fresh
    install reads no history until they ask once."""
    from cortex.browse import build, sites

    hours = auto_import_hours()
    last = build.load_state(v).get("last_run")
    if not hours or not isinstance(last, str):
        return False
    try:
        t = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return False
    if (now or datetime.now(UTC)) - t < timedelta(hours=hours):
        return False
    try:
        with sites.busy(v, 0):  # a peek: the import takes it again for its whole run
            return True
    except (VaultError, OSError):
        return False


def _say(text: str) -> None:
    """One line on the receiver's terminal: counts and states only, never a URL or title."""
    print(f"{time.strftime('%H:%M:%S')}  {text}", flush=True)


def summary(r: dict, took: float) -> str:
    """The counts-only line for a finished run (the terminal and the popup)."""
    if "days_from" not in r:
        return f"done in {took:.0f} s: nothing new since the last run"
    return (f"done in {took:.0f} s: {r.get('private_written', 0)} private day pages written "
            f"(kept {r.get('kept', 0)} visits, private {r.get('private', 0)}, dropped "
            f"{r.get('dropped', 0)}); local model {'used' if r.get('model') else 'not used'}")


class Processor:
    """Runs `run_import` when a session ended (debounced), records sat idle, or the last import
    is older than browse.auto_import_hours (checked AUTO_FIRST_S after start, then every
    AUTO_EVERY_S); `tick` is the only runner and never overlaps itself. `clock` (monotonic) and
    `now` (wall time, for the last import's age) are injectable for tests."""

    def __init__(self, v: Vault, clock=time.monotonic, say=_say, now=None):
        self.v, self.clock, self.say = v, clock, say
        self.now = now or (lambda: datetime.now(UTC))
        self._auto_at = clock() + AUTO_FIRST_S
        self.processing = False
        self.last: str = ""  # the last run's counts-only summary, for the popup
        self._lock = threading.Lock()
        self._due: float | None = None  # when a requested run may start
        self._stored_at: float | None = None  # last batch while records wait
        self._last_batch = clock()  # a deepen pass waits for DEEPEN_IDLE_S after it
        self._deepened = False  # once per idle period

    def batch(self, stored: int, session_end: bool) -> None:
        now = self.clock()
        with self._lock:
            self._last_batch, self._deepened = now, False
            if stored or self._stored_at is not None:
                self._stored_at = now
            if session_end:
                if self._due is None and not self.processing:
                    self.say(f"session end received: processing in {DEBOUNCE_S} s")
                self._due = now + DEBOUNCE_S

    def tick(self) -> bool:
        """Run the import if it is due and none is running, else the local LLM's deepen pass
        once after DEEPEN_IDLE_S with no batch; True if either ran."""
        with self._lock:
            now = self.clock()
            idle = self._stored_at is not None and now - self._stored_at >= IDLE_PROCESS_S
            due = idle or (self._due is not None and now >= self._due)
            aged = False
            if not due and not self.processing and now >= self._auto_at:
                self._auto_at = now + AUTO_EVERY_S
                aged = due = auto_import_due(self.v, self.now())
            deep = not due and not self._deepened and now - self._last_batch >= DEEPEN_IDLE_S
            if self.processing or not (due or deep):
                return False
            self.processing = True
            if due:
                self._due, self._stored_at = None, None
            else:
                self._deepened = True
        if deep:
            return self._deepen()
        if aged:
            self.say(f"last import over {auto_import_hours():g} hours ago: importing new history")
        self.say("processing your browsing (the first run reads your whole history and can "
                 "take several minutes)…")
        t0 = time.perf_counter()

        def progress(key, n, new, err):
            self.say(f"  {key}: not read ({type(err).__name__}); the others go on" if err
                     else f"  {key}: {n} visits read, {new} new")

        try:
            self.last = summary(run_import(self.v, progress=progress),
                                time.perf_counter() - t0)
        except Exception as e:  # logged by type only: a message may hold a URL or title
            self.last = f"failed: {type(e).__name__}"
            log.warning("browse: processing failed: %s", type(e).__name__)
        finally:
            with self._lock:
                self.processing = False
        self.say(self.last)
        return True

    def _deepen(self) -> bool:
        """The deepen pass; False when no local LLM is enabled and answering."""
        r = None
        try:
            r = run_deepen(self.v, say=self.say)
            if r is not None:
                self.say(f"deepen done: {r.get('described', 0)} sites described, "
                         f"{r.get('left', 0)} left")
        except Exception as e:  # by type only, like the import
            log.warning("browse: deepen failed: %s", type(e).__name__)
            self.say(f"deepen failed: {type(e).__name__}")
            r = {}
        finally:
            with self._lock:
                self.processing = False
        return r is not None

    def loop(self, stop: threading.Event, every: float = 1.0) -> None:
        while not stop.wait(every):
            self.tick()

    def status(self) -> dict:
        from cortex.browse import build

        queued = 0
        for p in _dir(self.v).glob("inbox-*.jsonl"):
            try:
                queued += p.read_bytes().count(b"\n")
            except OSError:
                pass
        return {"ok": True, "queued_records": queued,
                "last_processed": build.load_state(self.v).get("last_run"),
                "processing": self.processing, "last_summary": self.last}


# ---- server --------------------------------------------------------------------------------


def _handler(v: Vault, port_ref: list[int], proc: Processor):
    class H(BaseHTTPRequestHandler):
        server_version = "cortex-browse"
        sys_version = ""
        timeout = 10  # seconds per socket read: a stalled client cannot hold a thread

        def log_message(self, *a):  # paths, titles and URLs are never logged
            pass

        def _send(self, code: int, body: dict | None = None):
            # A refused POST still drains its (capped) body: on Windows, closing a socket with
            # unread data resets it, and the client sees an abort instead of the refusal.
            if self.command == "POST" and not getattr(self, "_drained", False):
                self._drained = True
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    if 0 < n <= MAX_BODY:
                        self.rfile.read(n)
                except (ValueError, OSError):
                    pass
            data = json.dumps(body if body is not None else {"ok": code < 300}).encode()
            self.send_response(code)
            origin = self.headers.get("Origin")
            if origin and _EXT_ORIGIN.fullmatch(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "GET, POST")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Vary", "Origin")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _gate(self, need_token: bool = True) -> bool:
            """Host, Origin and token checks; sends the refusal and returns False."""
            hosts = {f"127.0.0.1:{port_ref[0]}", f"localhost:{port_ref[0]}"}
            if self.headers.get("Host") not in hosts:  # DNS rebinding
                self._send(403, {"ok": False, "error": "host"})
                return False
            origin = self.headers.get("Origin")
            if origin is not None and not _EXT_ORIGIN.fullmatch(origin):
                self._send(403, {"ok": False, "error": "origin"})
                return False
            if not need_token:
                return True
            token, auth = _token(v), self.headers.get("Authorization", "")
            given = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
            if not token or not hmac.compare_digest(given.encode(), token.encode()):
                self._send(401, {"ok": False, "error": "token"})
                return False
            return True

        def do_OPTIONS(self):
            origin = self.headers.get("Origin")
            self._send(204 if origin and _EXT_ORIGIN.fullmatch(origin) else 403)

        def do_GET(self):
            hello = _HELLO.fullmatch(self.path)
            if hello:  # identity proof: callers send their token only to a listener that has it
                if not self._gate(need_token=False):
                    return
                token = _token(v)
                if not token:
                    self._send(503, {"ok": False, "error": "not paired"})
                else:
                    self._send(200, {"proof": hmac.new(token.encode(), hello[1].encode(),
                                                       "sha256").hexdigest()})
                return
            if not self._gate():
                return
            if self.path == "/v1/ping":  # the deny list, so the extension skips it too
                self._send(200, {"ok": True, "deny_domains": list(load_settings().deny)})
            elif self.path == "/v1/status":
                self._send(200, proc.status())
            else:
                self._send(404, {"ok": False})

        def do_POST(self):
            if self.path not in ("/v1/visit", "/v1/batch"):
                self._send(404, {"ok": False})
                return
            if not self._gate():
                return
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._send(415, {"ok": False, "error": "content-type"})
                return
            try:
                n = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._send(411, {"ok": False, "error": "length"})
                return
            if n < 0 or n > MAX_BODY:
                self.close_connection = True
                self._send(413, {"ok": False, "error": "too large"})
                return
            try:
                self._drained = True
                body = json.loads(self.rfile.read(n))
            except (ValueError, UnicodeDecodeError, RecursionError):
                self._send(400, {"ok": False, "error": "json"})
                return
            end = False
            if self.path == "/v1/batch" and isinstance(body, dict) and "records" in body:
                end = body.get("session_end") is True  # {records, session_end}
                body = body["records"]
            if self.path == "/v1/visit":
                items = [body]
            elif isinstance(body, list) and len(body) <= MAX_BATCH:
                items = body
            else:
                self._send(400, {"ok": False, "error": f"batch must be a list of at most "
                                                      f"{MAX_BATCH} records"})
                return
            clean = [validate(r) for r in items]
            if self.path == "/v1/visit" and clean[0] is None:
                self._send(400, {"ok": False, "error": "invalid record"})
                return
            settings = load_settings()
            kept = [k for k in (filter_record(c, settings) for c in clean if c) if k]
            if kept:
                store(v, kept)
            invalid = sum(c is None for c in clean)
            log.info("browse: %d stored, %d dropped, %d invalid", len(kept),
                     len(clean) - invalid - len(kept), invalid)
            proc.batch(len(kept), end)
            self._send(200, {"ok": True, "stored": len(kept),
                             "dropped": len(clean) - invalid - len(kept), "invalid": invalid})

    return H


MAX_CONNECTIONS = 32  # open connections per server (the app's too): one thread each


class _Server(ThreadingHTTPServer):
    """No port sharing: a second receiver would split one state.json between two Processors.
    At most MAX_CONNECTIONS connections at once: a local program opening thousands of idle
    ones would otherwise start a thread for each; one over the cap is closed at once. Each
    handler's `timeout` bounds how long an idle one keeps its slot."""
    allow_reuse_address = False

    def __init__(self, *a, **kw):
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*a, **kw)

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows binds a used port without it
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


def make_server(vault, port: int = PORT, *, host: str = HOST) -> ThreadingHTTPServer:
    """The receiver, bound to 127.0.0.1 only; any other host is refused. Port 0 picks one."""
    if host != HOST:
        raise ValueError("browse serve binds to 127.0.0.1 only; browsing never leaves "
                         "this machine")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError("port must be 0-65535")
    v = _vault(vault)
    port_ref = [port]
    proc = Processor(v)
    srv = _Server((HOST, port), _handler(v, port_ref, proc))
    srv.processor = proc  # its loop is started by `serve`
    srv.daemon_threads = True
    port_ref[0] = srv.server_address[1]
    if srv.server_address[0] != HOST:  # belt and braces: never serve off loopback
        srv.server_close()
        raise ValueError("browse serve bound off loopback; refused")
    return srv


def serve(vault, port: int = PORT, *, stop: threading.Event | None = None,
          host: str = HOST) -> None:
    """Run the receiver until `stop` is set (or Ctrl+C). Needs a token from `pair` first."""
    v = _vault(vault)
    if _token(v) is None:
        raise ValueError("no pairing token; run `cortex browse pair` first")
    srv = make_server(v, port, host=host)
    if stop is not None:
        threading.Thread(target=lambda: (stop.wait(), srv.shutdown()), daemon=True).start()
    done = threading.Event()
    threading.Thread(target=srv.processor.loop, args=(done,), daemon=True).start()
    try:
        srv.serve_forever(poll_interval=0.2)
    finally:
        done.set()
        srv.server_close()
