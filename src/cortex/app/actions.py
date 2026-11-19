"""The app's long actions, each run as a background job (`server.Jobs`, one at a time) with a
progress line: the existing CLI commands (doctor, backup, dashboard, model download), the local
model's load and test, and the browse actions. Browse results are counts only (`status.safe`):
a title, URL or search never reaches the page."""

from __future__ import annotations

import contextlib
import io
import re
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path

from cortex._proc import NO_WINDOW
from cortex.app import status

TEST_TEXT = "Dijkstra shortest path tutorial"  # synthetic: never the owner's data
TEST_LABELS = ["programming", "cooking", "sports"]
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
_popen = subprocess.Popen  # tests replace this: nothing installs or downloads in a test


class _Lines(io.TextIOBase):
    """A text stream that hands each finished line to `say`."""

    def __init__(self, say):
        self.say, self.buf = say, ""

    def write(self, s: str) -> int:
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.say(line)
        return len(s)

    def flush(self) -> None:
        if self.buf:
            self.say(self.buf)
            self.buf = ""


def capture(job, fn, *args, **kwargs) -> dict:
    """Run a `cortex` CLI command function in-process, its printed lines going to the job's
    progress line; {"ok", "lines"}. (The app runs one job at a time, so redirecting the
    process's stdout is safe.)"""
    import typer
    from click.exceptions import Exit

    lines: list[str] = []
    out = _Lines(lambda s: (lines.append(s), job.say(s)))
    ok = True
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            fn(*args, **kwargs)
        except (Exit, typer.Exit) as e:
            ok = getattr(e, "exit_code", 1) == 0
        out.flush()
    return {"ok": ok, "lines": lines[-300:]}


def run_proc(job, args: list[str]) -> dict:
    """Run a command (a download or an install) with its output as the progress line; Cancel
    terminates it. {"ok", "cancelled", "lines"}."""
    job.say("running: " + subprocess.list2cmdline(args))
    try:
        p = _popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except OSError as e:
        raise ValueError(f"could not start {args[0]}: {e}") from None
    job.proc = p
    lines, buf = [], b""
    read = getattr(p.stdout, "read1", None) or p.stdout.read
    while True:
        chunk = read(4096)
        buf += chunk or b"\n"
        *done, buf = re.split(rb"[\r\n]", buf)
        for raw in done:
            line = _ANSI.sub("", raw.decode("utf-8", "replace")).strip()
            if line:
                job.say(line)
                lines.append(line)
        if not chunk:
            break
    rc = p.wait()
    cancelled = job.cancelled.is_set()
    return {"ok": rc == 0 and not cancelled, "cancelled": cancelled, "lines": lines[-50:]}


# ---- dashboard and vault (the existing CLI commands) ------------------------------------------


# the doctor's fixes that say to open the app, as the app says them (the CLI is unchanged)
APP_FIX = {"Search model": "Setup → Search model → Download search model.",
           "AI app connected": "Setup → Connect AI apps.",
           "Local model": "Setup → Local model → Start.",
           "Vault": "Setup → Vault."}


def doctor(job, root: Path | None) -> dict:
    from cortex import cli
    out = capture(job, cli.doctor, vault=root)
    lines, name = [], ""
    for line in out["lines"]:
        if not line.startswith(" "):  # "WARN  Search model: ..." names the check
            name = line[6:].split(":", 1)[0]
        elif name in APP_FIX and "fix:" in line and ("cortex app" in line or "Start menu" in line):
            line = line[:line.index("fix:")] + "fix: in this app, " + APP_FIX[name]
        lines.append(line)
    return {**out, "lines": lines}


def backup(job, root: Path) -> dict:
    from cortex import cli
    return capture(job, cli.backup, vault=root)


def dashboard(job, root: Path) -> dict:
    from cortex import cli
    return capture(job, cli.dashboard, no_open=True, vault=root)


# ---- local model (adapter to cortex.localllm) ----------------------------------------------


def llm_config(**changes) -> dict:
    """The whole config with `localllm` changed in memory only (for a load or a test)."""
    cfg = status.config()
    old = cfg.get("localllm") if isinstance(cfg.get("localllm"), dict) else {}
    return {**cfg, "localllm": {**old, **changes}}


def llm_load(job, model: str) -> dict:
    from cortex import localllm
    if not hasattr(localllm, "ensure_lmstudio"):
        raise ValueError("loading through LM Studio is not available in this version yet")
    job.say(f"starting LM Studio's server and loading {model} (no download)")
    url = localllm.ensure_lmstudio(model, config=llm_config(enabled=True, runner="lmstudio"))
    if url is None:
        raise ValueError(f"LM Studio did not load {model}: is it in `lms ls`?")
    return {"ok": True, "lines": [f"{model} loaded in LM Studio"]}


def llm_test(job, config: dict | None = None) -> dict:
    """One synthetic prompt through the runner the config picks; its answer and time."""
    from cortex import localllm
    job.say("looking for a local runner")
    runner = localllm.find(config)
    if runner is None:
        raise ValueError("no local runner answered (start Ollama or LM Studio, and check the "
                         "model name)")
    job.say(f"asking {runner.describe()}")
    t = time.perf_counter()
    got = runner.classify(TEST_TEXT, TEST_LABELS)
    took = time.perf_counter() - t
    if got is None:
        raise ValueError(f"{runner.describe()} gave no valid answer in {took:.1f} s")
    line = f"{TEST_TEXT!r} -> {got[0]} ({got[1]:.2f}) in {took:.2f} s"
    return {"ok": True, "runner": runner.describe(), "answer": got[0], "seconds": round(took, 2),
            "lines": [line]}


# ---- browsing (adapters; counts only) --------------------------------------------------------


def _vault(root: Path):
    from cortex.vault import Vault
    return Vault(root)


def browse_process(job, root: Path) -> dict:
    from cortex import browse_serve
    job.say("processing waiting visits and new browser history (counts only)")
    v = _vault(root)  # like the popup's button: only the profiles imported before
    report = browse_serve.run_import(v, srcs=browse_serve.auto_sources(v))
    return {"ok": True, "counts": status.safe(report)}


def browse_selftest(job) -> dict:
    from cortex.browse import selftest
    lines: list[str] = []
    ok = selftest.run(lambda s: lines.append(str(s)))
    # the check names are fixed strings in selftest.py; only PASS/FAIL lines go on
    lines = [x for x in lines if x.startswith(("PASS", "FAIL"))]
    for x in lines:
        job.say(x)
    return {"ok": bool(ok), "lines": lines}


def browse_scan(job) -> dict:
    from cortex.browse import load_settings, sources
    job.say("reading browser history counts (nothing is written)")
    out = []
    for r in sources.scan(sources.discover(), load_settings()):
        row = {"source": str(r.get("source", "")), **status.safe(r)}
        if "error" in r:
            row["error"] = True
        out.append(row)
    return {"ok": True, "kind": "scan", "rows": out}


class Cancelled(KeyboardInterrupt):
    """Raised inside `build.run` on Cancel. Like Ctrl+C there, it leaves whole pages only and
    the old state, so the next import redoes the same days."""


class _Watched:
    """A local-model runner whose every call first checks for Cancel."""

    def __init__(self, inner, check):
        self._inner, self._check = inner, check

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*a, **k):
            self._check()
            return attr(*a, **k)
        return call


def history_import(job, root: Path, keys: set[str], takeout: list | None = None) -> dict:
    """The full import of the chosen browsers' past history (`build.run`, no `since`: every
    day with visits not imported yet), plus Google activity rows (`takeout`) when given.
    Without the search model it stops first: every page would land under Other. Progress and
    summary are counts only."""
    from cortex import localllm, semantic
    from cortex.app import wizard
    from cortex.browse import build, load_settings, sources

    srcs = [s for s in sources.discover() if s.key in keys]
    if not srcs and not takeout:
        raise ValueError("Tick at least one browser that is on this computer.")
    embed = semantic.load_embedder()
    if embed is None:
        return {"ok": False, "need": "embed", "lines": [
            f"Download the search model first ({wizard.EMBED['size']}): without it every page "
            "lands under Other."]}

    def check():
        if job.cancelled.is_set():
            raise Cancelled

    runner = budget = None
    if localllm.enabled():
        try:
            runner = localllm.find(background=True)
        except ValueError:  # a non-loopback localllm.url is refused
            runner = None
        if runner is not None:
            cfg = status.config().get("localllm")
            secs = cfg.get("budget_s", 300) if isinstance(cfg, dict) else 300
            budget = localllm.Budget(secs if isinstance(secs, (int, float)) else 300)
    job.say(f"reading {len(srcs)} browser profile(s); the first time can take a while")

    def progress(key, n, new, err):
        job.say(f"{key}: not read ({type(err).__name__}); the others go on" if err
                else f"{key}: {n} visits read, {new} new")
        check()

    def watched_embed(texts):
        check()
        return embed(texts)

    try:
        r = build.run(_vault(root), srcs, load_settings(), embed=watched_embed,
                      runner=_Watched(runner, check) if runner is not None else None,
                      budget=budget, progress=progress, takeout=takeout)
    except Cancelled:
        return {"ok": False, "cancelled": True, "lines": [
            "Stopped. Every page on disk is whole and nothing was marked as imported, so the "
            "next import redoes these days."]}
    summary = {"days_written": r.get("private_written", 0) + r.get("public_written", 0),
               "private_pages": r.get("private_written", 0), "kept": r.get("kept", 0),
               "private": r.get("private", 0), "dropped": r.get("dropped", 0),
               "sources_read": r.get("sources", 0), "sources_failed": r.get("sources_failed", 0),
               "local_model_used": runner is not None, "first_day": r.get("days_from")}
    if takeout is not None:
        summary["google_rows_used"] = r.get("takeout", 0)
    if "days_from" not in r:
        job.say("nothing new since the last import")
    return {"ok": True, "counts": status.safe(summary)}


# ---- guided imports (chat exports through cortex.chats; Google activity through browse) ----

GOOGLE_NOTE = ("Google activity goes through your browsing privacy filter, like your browser "
               "history: adult and sensitive rows follow your Browsing settings, and the pages "
               "stay private unless you made browsing visible to Claude.")


def tmp_dir() -> Path:
    return Path.home() / ".cortex" / "tmp"


def sweep_uploads(max_age_s: float = 24 * 3600) -> None:
    """Delete uploads older than a day: a killed app never ran its own cleanup (r1 S7)."""
    cutoff = time.time() - max_age_s
    for p in tmp_dir().glob("upload-*"):
        with contextlib.suppress(OSError):
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()


def sweep_updates(keep: Path | None = None) -> None:
    """Delete downloaded installers (~/.cortex/tmp/update-*) except `keep`; one still running
    is locked and goes next time."""
    import shutil
    for p in tmp_dir().glob("update-*"):
        if p != keep:
            shutil.rmtree(p, ignore_errors=True)


# importers.detect's kind -> how the page names it, and the card that imports it
KIND = {"chats": ("a chat export", "the Claude, ChatGPT, Gemini or Perplexity card"),
        "takeout": ("Google activity (My Activity)", "the Google activity card"),
        "notes": ("a notes folder or export", "the Notes card"),
        "calendar": ("a calendar", "the Calendar card")}


def _kind(source: str, path: Path) -> str:
    """The importer this card's file needs, detected as `cortex import` does. A file of
    another kind is refused with the card that takes it (R3-1)."""
    from cortex import importers
    want, got = importers.SOURCES[source], _readable(importers.detect, path)
    if want in got:
        return want
    if not got:
        raise ValueError(f"Cortex cannot tell what {path.name} is. Step 1 shows the file to "
                         "pick.")
    what, card = KIND[got[0]]
    raise ValueError(f"This is {what}, not {KIND[want][0]}: use {card} under Add your "
                     "history.")


def _commit(v, what: str) -> None:
    """Commit the vault files an import wrote, as `cortex import` does."""
    from cortex import session
    if v.writes:
        session._commit(v, f"cortex: app import {what}", v.wrote_since(0))


def _s(n: int) -> str:
    return "s" * (n != 1)


def _notes(flavor: str, n: int) -> str:
    """"Obsidian notes", "Notion note"; a plain folder is just "notes", never "Notes notes"."""
    from cortex import importers
    label = importers.LABELS[flavor]
    return ("" if label == "Notes" else label + " ") + "note" + _s(n)


def _skipped_rows(rows) -> str:
    """The count of HTML rows takeout.records skipped for an unreadable time, as a sentence."""
    n = rows.skipped
    return f" {n:,} row{_s(n)} skipped (unreadable date)." if n else ""


def _readable(fn, *args):
    """cortex.chats and takeout refuse a bad file with VaultError; the page shows its text."""
    from cortex.vault import VaultError
    try:
        return fn(*args)
    except VaultError as e:
        raise ValueError(str(e)) from None


def import_preview(job, source: str, path: Path, root: Path | None = None) -> dict:
    """A dry run: what an import of `path` would bring, counts and dates only."""
    from cortex import chats, importers
    job.say("reading the file (nothing is imported yet)")
    kind = _kind(source, path)
    if kind == "takeout":
        from cortex import semantic
        from cortex.browse import takeout
        rows = _readable(takeout.records, path)
        if not rows:
            raise ValueError("Found no Search, YouTube or Chrome activity in this "
                             "export (only English Takeouts are read)."
                             + _skipped_rows(rows))
        kinds = Counter(v.source.rsplit("/", 1)[-1] for v in rows)
        out = {"ok": True, "kind": "import-preview", "found": len(rows), "note": GOOGLE_NOTE,
               "lines": [f"Found {len(rows):,} activity rows from {rows[0].start:%Y-%m-%d} to "
                         f"{rows[-1].start:%Y-%m-%d}: " + ", ".join(
                             f"{k.capitalize() if k != 'youtube' else 'YouTube'} {n:,}"
                             for k, n in sorted(kinds.items())) + "." + _skipped_rows(rows)]}
        if semantic.load_embedder() is None:  # said here, not after Import (R3-3)
            from cortex.app import wizard
            out.update(ok=False, need="embed", lines=[*out["lines"], (
                f"Download the search model first ({wizard.EMBED['size']}): it sorts each page "
                "into a topic, and without it every page lands under Other.")])
        return out
    if kind == "notes":
        r = _readable(importers.import_notes, _vault(root), path, None, True)
        new, same, skipped = r["new"], r["same"], r["skipped"]
        return {"ok": True, "kind": "import-preview", "found": new, "lines": [
            f"Found {new:,} new {_notes(r['source'], new)}"
            + (f", {same:,} already imported" if same else "")
            + (f"; {skipped:,} file{_s(skipped)} skipped (hidden, not text or over 5 MB)"
               if skipped else "") + "."]}
    if kind == "calendar":
        r = _readable(importers.import_calendar, _vault(root), path, True)
        return {"ok": True, "kind": "import-preview", "found": r["new"], "lines": [
            f"Found {r['events']:,} event{_s(r['events'])} in {r['months']:,} "
            f"month{_s(r['months'])}, {r['new']:,} of them new."]}
    got, new, old = _readable(chats.import_export, path, None, True)
    n = new + old
    name = chats.GUIDES[got]["name"]
    lines = [f"Found {n:,} {name} conversation{_s(n)}"
             + (f", {old:,} already imported (those are updated)." if old else ".")]
    if got != source:
        lines.append(f"This is a {name} export, so it is imported as {name}.")
    return {"ok": True, "kind": "import-preview", "found": n, "already": old,
            "detected": got, "lines": lines}


def _cortex_hint() -> str:
    """Where to use imported chats: /cortex in a connected app, else connect one first."""
    from cortex.app import targets
    on = [t["label"] for t in targets.describe(None) if t["connected"]]
    return (f"In {', '.join(on)}, type /cortex to learn from them." if on else
            "Connect an AI app (Setup, Connect AI apps), then type /cortex in it to learn "
            "from them.")


def import_run(job, source: str, path: Path, root: Path | None = None) -> dict:
    from cortex import chats, importers
    job.say("reading the file")
    kind = _kind(source, path)
    if kind == "notes":
        job.say("importing the notes")
        v = _vault(root)
        r = _readable(importers.import_notes, v, path)
        _commit(v, "notes")
        return {"ok": True, "kind": "import", "counts": {"notes": r["new"]}, "lines": [
            f"Imported {r['new']:,} {_notes(r['source'], r['new'])} into "
            f"{root / 'resources'}" + (f"; {r['same']:,} were already in." if r["same"]
                                       else "."),
            importers.report_line(importers.LABELS[r["source"]], r)]}
    if kind == "calendar":
        job.say("importing the calendar")
        v = _vault(root)
        r = _readable(importers.import_calendar, v, path)
        _commit(v, "calendar")
        return {"ok": True, "kind": "import", "counts": {"events": r["new"]}, "lines": [
            f"Imported {r['new']:,} new event{_s(r['new'])} into the calendar-YYYY-MM month "
            f"pages in {root / 'resources'}.", importers.report_line("Calendar", r)]}
    if kind == "takeout":
        from cortex.browse import load_settings, sources, takeout
        rows = _readable(takeout.records, path)
        # S2: only the browsers `browse.browsers` allows
        out = history_import(job, root, {s.key for s in sources.chosen(load_settings())},
                             takeout=rows)
        if out.get("ok"):
            out["lines"] = [f"Your Google activity is in the private day pages under "
                            f"{root / 'browse' / 'private'}. Rows on days your browsers "
                            "already hold were skipped: the browsers have them."
                            + _skipped_rows(rows), GOOGLE_NOTE]
        return out
    job.say("importing the conversations")
    res = _readable(chats.import_export, path)
    got, n = res
    where = chats.export_root() / chats.EXPORT_DIRS[got][0]
    return {"ok": True, "kind": "import", "counts": {"conversations": n}, "lines": [
        f"Imported {n:,} {chats.GUIDES[got]['name']} conversation{_s(n)} into {where}. "
        + _cortex_hint(), importers.report_line(chats.GUIDES[got]["name"], res.report())]}


def pair(root: Path) -> str:
    from cortex import browse_serve
    return browse_serve.pair(_vault(root))


class Receiver:
    """The browse receiver run inside the app (it stops when the app stops)."""

    def __init__(self):
        self.srv = self.done = None

    def status(self) -> dict | None:
        if self.srv is None:
            return None
        proc = getattr(self.srv, "processor", None)
        try:
            return status.safe(proc.status()) if proc is not None else {}
        except Exception:  # noqa: BLE001
            return {}

    def start(self, root: Path) -> None:
        from cortex import browse_serve
        if self.srv is not None:
            return
        if not (root / ".cortex" / "browse" / "token").is_file():
            raise ValueError("Pair the extension first: the receiver needs its token.")
        try:
            srv = browse_serve.make_server(_vault(root), browse_serve.PORT)
        except OSError:
            raise ValueError(f"port {browse_serve.PORT} is busy: a receiver may already run "
                             "(Start at login?)") from None
        self.done = threading.Event()
        if (proc := getattr(srv, "processor", None)) is not None:
            threading.Thread(target=proc.loop, args=(self.done,), daemon=True).start()
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                         daemon=True).start()
        self.srv = srv

    def stop(self) -> None:
        if self.srv is None:
            return
        srv, self.srv = self.srv, None
        self.done.set()
        srv.shutdown()
        srv.server_close()


def drop_old_task() -> bool:
    """Delete the Task Scheduler entry older versions made (Windows); True when one was there."""
    from cortex.app import targets
    return targets._run(["schtasks", "/Delete", "/F", "/TN", status.LOGIN_TASK], timeout=30)[0]


def migrate_login(root: Path | None) -> None:
    """At app start (Windows): an old Task Scheduler entry becomes the Run value, for this
    app's vault (shortcut: assumes the task served the default vault, as the app made it)."""
    if root is None or status.login_file() is not None or status.run_get() is not None:
        return
    if drop_old_task():
        status.run_set(status.login_command(root))


def login(job, root: Path, on: bool) -> dict:
    """Create or delete what starts the receiver at login: a Run value on Windows (and any old
    Task Scheduler entry goes), else `status.login_file()` with its launchctl or systemctl
    commands."""
    from cortex.app import targets
    if (f := status.login_file()) is None:
        if on:
            value = status.login_command(root)
            status.run_set(value)
            job.say(f"added the Run value: {value}")
        else:
            status.run_delete()
            job.say("removed the Run value")
        if drop_old_task():
            job.say("removed the old Task Scheduler entry")
        return {"ok": True, "lines": [("Starts at login: " + value) if on
                                      else "No longer starts at login."]}
    if on:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(status.login_text(root), encoding="utf-8", newline="\n")
        job.say(f"wrote {f}")
    lines = []
    for args in status.login_commands(on):
        job.say("running: " + " ".join(args))
        ok, out = targets._run(args, timeout=30)
        if not ok and on:
            raise ValueError(f"{args[0]} failed: {out[-300:]}")
        lines.append(out[-300:])
    if not on:  # removing the file is what stops it, even when unload or disable failed
        f.unlink(missing_ok=True)
        job.say(f"removed {f}")
    return {"ok": True, "lines": lines}
