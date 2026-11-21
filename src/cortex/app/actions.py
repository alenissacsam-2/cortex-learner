"""The app's long actions, each run as a background job (`server.Jobs`, one at a time) with a
progress line: the existing CLI commands (doctor, backup, dashboard, model download), the local
model's load and test, and the browse actions. Browse results are counts only (`status.safe`):
a title, URL or search never reaches the page."""

from __future__ import annotations

import contextlib
import io
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

from cortex._proc import NO_WINDOW, Tree
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


def run_proc(job, args: list[str], env: dict | None = None) -> dict:
    """Run a command (a download or an install) with its output as the progress line; Cancel
    terminates it and everything it started (`_proc.Tree`). {"ok", "cancelled", "lines"}.
    `env`: the child's environment (default ours)."""
    job.say("running: " + subprocess.list2cmdline(args))
    try:
        p = _popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, env=env,
                   start_new_session=True)  # POSIX: its own process group, for Cancel
    except OSError as e:
        raise ValueError(f"could not start {args[0]}: {e}") from None
    tree = job.proc = Tree(p)
    lines, buf = [], b""
    read = getattr(p.stdout, "read1", None) or p.stdout.read
    try:
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
    finally:
        tree.close()
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
    job.say("importing new browser history (counts only)")
    v = _vault(root)  # only the profiles imported before; old extension visits not yet
    # imported come along (build.run reads what is left of its inbox)
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
    """Raised inside `build.run` on Cancel. Like Ctrl+C there, it leaves whole pages only;
    what was read and checked is saved in the store, so the next import goes on from there."""


def browse_migrate(job, root: Path) -> dict:
    """The site Markdown pages into the browsing store (`store.migrate`): zipped into
    .cortex/backups first, removed from the vault only once the store holds every one. Counts
    only."""
    from cortex.browse import load_settings, store
    steps = store.MIGRATE_STEPS
    job.say("moving the site pages into Cortex's own store")
    r = store.migrate(_vault(root), settings=load_settings(), step=lambda n, d, t: job.progress(
        n, len(steps), d, t, steps[n - 1]))
    if r.get("missing") or r.get("error"):
        return {"ok": False, "counts": status.safe(r), "lines": [
            "The site pages were not moved: the store does not hold every one yet. Nothing "
            "was removed."]}
    return {"ok": True, "counts": status.safe(r)}


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
    visit not read yet), plus Google activity rows (`takeout`) when given, in named steps
    (`job.progress`; the site pages' move into the store first, once). Without the search
    model it stops first: every page would land under Other. The local model checks titles
    only when it is already loaded and the PC idle (`titles.may_run`); its summaries wait for
    the background runs. Cancel keeps what was saved. Progress and summary are counts only."""
    from cortex import browse_serve, semantic
    from cortex.app import wizard
    from cortex.browse import build, load_settings, sources, store

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

    runner, budget = browse_serve.idle_runner()
    v = _vault(root)
    moving = () if store.migrated(v) else store.MIGRATE_STEPS
    names = (*moving, *build.PHASES)

    def step(n, done, total):  # n counts from 1 over `names`
        job.progress(n, len(names), done, total, names[n - 1])
        check()

    def progress(key, n, new, err):
        job.say(f"{key}: not read ({type(err).__name__}); the others go on" if err
                else f"{key}: {n} visits read, {new} new")
        check()

    def watched_embed(texts):
        check()
        return embed(texts)

    try:
        if moving:
            store.migrate(v, settings=load_settings(), step=step)
            if not store.migrated(v):
                return {"ok": False, "lines": ["The site pages could not be moved into the "
                                               "store; nothing was imported."]}
        job.say(f"reading {len(srcs)} browser profile(s); the first time can take a while")
        r = build.run(v, srcs, load_settings(), embed=watched_embed,
                      runner=_Watched(runner, check) if runner is not None else None,
                      budget=budget, progress=progress, takeout=takeout,
                      step=lambda n, d, t: step(n + len(moving), d, t), summaries=False)
    except Cancelled:
        return {"ok": False, "cancelled": True, "lines": [
            "Stopped. What was read is saved; the next import goes on from there."]}
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


def chat_import(job, root: Path) -> dict:
    """One `chat_import.run` pass, committed. `quiet` (no notification) when nothing was saved
    or failed; a failed chat makes the job fail (`ok: false`). Counts only."""
    from cortex import chat_archive
    from cortex import chat_import as ci
    from cortex.vault import VaultError
    v = _vault(root)
    try:  # C5: once, chat pages already in resources/ move to the git-ignored archive
        if (moved := chat_archive.auto(v)) and moved["ids"]:
            job.say(f"{len(moved['ids'])} chat pages moved out of git, to archive/chats/")
    except VaultError as e:  # busy (another import) or a file open elsewhere: next pass
        print(f"cortex app: chat archive not moved yet ({e.code})")
    try:
        counts = ci.run(v, progress=lambda k, n, s: job.say(ci.eta(k, n, s)),
                        cancelled=job.cancelled.is_set)
    except Exception as e:
        print(f"cortex app: chat import failed ({type(e).__name__})")  # type only: no chat text
        raise
    finally:
        _commit(v, "chats")
    wrote = counts.get("saved", 0) + counts.get("updated", 0)
    job.say(f"{wrote} chat page{_s(wrote)} written" if wrote else "no new chats")
    return {"ok": not counts.get("failed"), "quiet": not (wrote or counts.get("failed")),
            "cancelled": bool(counts.get("stopped")), "counts": status.safe(counts)}


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
        "calendar": ("a calendar", "the Calendar card"),
        "docs": ("a folder of documents", "the Documents card"),
        "repos": ("a folder of git projects", "the code projects card"),
        "highlights": ("reading highlights", "the Reading highlights card"),
        "mail": ("sent mail (.mbox)", "the Sent mail card"),
        "selfchat": ("notes to yourself", "the Notes to self card"),
        "location": ("a location history", "the Location card"),
        "photos": ("a photo folder", "the Photos card"),
        "spotify": ("a Spotify export", "the Spotify card"),
        "voice-memos": ("a folder of recordings", "the Voice memos card")}
TIERED = ("notes", "calendar", "docs", "repos", "highlights", "mail", "selfchat",
          "location", "photos", "spotify", "voice-memos")  # imports that ask the tier
OWN_CHECK = ("docs", "repos", "mail", "selfchat", "location", "photos",
             "spotify", "voice-memos")  # the source checks the path itself


def _kind(source: str, path: Path) -> str:
    """The importer this card's file needs, detected as `cortex import` does. A file of
    another kind is refused with the card that takes it (R3-1)."""
    from cortex import importers
    if source in OWN_CHECK:  # the source says what it skips, or refuses
        return source
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
    from cortex import chats, importers, more_sources, sources
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
    if kind == "voice-memos":
        from cortex import voice_memos
        if not path.is_dir():
            raise ValueError(f"{path.name} is not a folder; paste the folder of recordings.")
        if voice_memos.load_transcriber() is None:  # asked at Check; nothing downloads unasked
            return {"ok": False, "kind": "import-preview", "need": "voice", "found": 0,
                    "lines": ["Needs the local speech model (~150 MB). Install?"],
                    "note": "It transcribes on this computer: your recordings never leave it, "
                            "and nothing is downloaded until you click Install."}
    if kind in TIERED:
        # C2: the page asks the tier before Import, starting from the last choice or default
        v = _vault(root)
        t = _tier(v, kind, path)[0]
        counted = sources.Audience.CLOUD if t is sources.Audience.SKIP else t
    if kind == "voice-memos":  # counted only: transcribing waits for Import
        n, new = voice_memos.count(v, path)
        return {"ok": True, "kind": "import-preview", "found": new, "tier": t.value,
                "lines": [f"Found {n:,} recording{_s(n)}, {new:,} of them new. Each new one is "
                          "transcribed on this computer when you click Import, which can take "
                          "a while; the progress shows at the top."]}
    if kind in more_sources.SOURCE_OF:
        src = _source(kind, path)
        r = _readable(sources.run, v, src, counted, True)
        return {"ok": True, "kind": "import-preview", "found": r["imported"] + r["updated"],
                "tier": t.value, "lines": [_found(r), importers.report_line(src.label, r)]}
    if kind == "notes":
        r = _readable(importers.import_notes, v, path, None, True, False, counted)
        new, same, skipped = r["new"], r["same"], r["skipped"]
        return {"ok": True, "kind": "import-preview", "found": new, "tier": t.value, "lines": [
            f"Found {new:,} new {_notes(r['source'], new)}"
            + (f", {same:,} already imported" if same else "")
            + (f"; {skipped:,} file{_s(skipped)} skipped (hidden, not text or over 5 MB)"
               if skipped else "") + "."]}
    if kind == "calendar":
        r = _readable(importers.import_calendar, v, path, True, False, counted)
        return {"ok": True, "kind": "import-preview", "found": r["new"], "tier": t.value,
                "lines": [f"Found {r['events']:,} event{_s(r['events'])} in {r['months']:,} "
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


def _source(kind: str, path: Path, job=None):
    from cortex import importers, more_sources, voice_memos
    if kind == "voice-memos":  # the job: a line per recording, and Cancel
        return _readable(voice_memos.voice_source, path, job)
    return _readable({"notes": importers.notes_source, "calendar": importers.calendar_source,
                      **more_sources.SOURCE_OF}[kind], path)


def _found(r: dict) -> str:
    """A docs, repos or highlights preview's count, as a sentence."""
    return (f"Found {r['imported']:,} new item{_s(r['imported'])}"
            + (f", {r['updated']:,} changed" if r["updated"] else "")
            + (f", {r['duplicate']:,} already in" if r["duplicate"] else "") + ".")


def _tier(v, kind: str, path: Path, given: str | None = None, said=None):
    """(tier, how) for a tiered import: the page's choice (remembered), else the last choice,
    else the source's default (sources.pick_tier; a stricter choice's line goes to `said`)."""
    from cortex import sources
    return sources.pick_tier(v, _source(kind, path), given, said=said)


def import_run(job, source: str, path: Path, root: Path | None = None,
               tier: str | None = None) -> dict:
    """Import `path`; `tier` (skip, local or cloud) is the page's choice for notes and
    calendars, the user's click."""
    from cortex import chats, importers, more_sources, sources
    job.say("reading the file")
    kind, told = _kind(source, path), []  # told: RR-29's line when the tier got stricter
    if kind in TIERED:
        v = _vault(root)
        t = _tier(v, kind, path, tier, told.append)[0]
        if t is sources.Audience.SKIP:
            return {"ok": True, "kind": "import", "counts": {
                "notes" if kind == "notes" else "events" if kind == "calendar" else kind: 0},
                "lines": ["Nothing was imported: you chose Don't save.", *told]}
    if kind in more_sources.SOURCE_OF:
        job.say("importing")
        src = _source(kind, path, job)
        r = _readable(sources.run, v, src, t, False)
        _commit(v, kind)
        return {"ok": True, "kind": "import", "counts": {kind: r["imported"] + r["updated"]},
                "lines": [f"{_found(r).replace('Found', 'Imported', 1)} Pages are in "
                          f"{root / 'resources'}.", importers.report_line(src.label, r), *told]}
    if kind == "notes":
        job.say("importing the notes")
        r = _readable(importers.import_notes, v, path, None, False, False, t)
        _commit(v, "notes")
        return {"ok": True, "kind": "import", "counts": {"notes": r["new"]}, "lines": [
            f"Imported {r['new']:,} {_notes(r['source'], r['new'])} into "
            f"{root / 'resources'}" + (f"; {r['same']:,} were already in." if r["same"]
                                       else "."),
            importers.report_line(importers.LABELS[r["source"]], r), *told]}
    if kind == "calendar":
        job.say("importing the calendar")
        r = _readable(importers.import_calendar, v, path, False, False, t)
        _commit(v, "calendar")
        return {"ok": True, "kind": "import", "counts": {"events": r["new"]}, "lines": [
            f"Imported {r['new']:,} new event{_s(r['new'])} into the calendar-YYYY-MM month "
            f"pages in {root / 'resources'}.", importers.report_line("Calendar", r), *told]}
    if kind == "takeout":
        from cortex.browse import load_settings, sources, takeout
        from cortex.vault import PRIVATE_BROWSE_DIR
        rows = _readable(takeout.records, path)
        # S2: only the browsers `browse.browsers` allows
        out = history_import(job, root, {s.key for s in sources.chosen(load_settings())},
                             takeout=rows)
        if out.get("ok"):
            out["lines"] = [f"Your Google activity is in the private day pages under "
                            f"{root / PRIVATE_BROWSE_DIR}. Rows on days your browsers "
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
