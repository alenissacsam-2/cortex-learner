"""The browse receiver: `cortex browse serve` (and `cortex-receiver.exe`, which Start at login
runs) imports new browser history in the background. It opens no window and no network port.

From AUTO_FIRST_S after start, then every AUTO_EVERY_S, it checks whether the last import is older
than `browse.auto_import_hours`; when it is, it imports the new visits of the browsers and profiles
the user imported before (`run_import`). Once, DEEPEN_IDLE_S after start, the local model's
deepen pass runs when one is enabled. Nothing that holds a URL or a title is ever logged.

It was also the capture extension's endpoint on 127.0.0.1:8799 until 0.3.2, which removed the
extension. The inbox files the extension wrote still import (cortex.browse.build), but no new
ones arrive.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cortex.browse import Settings, load_settings
from cortex.vault import Vault, VaultError

log = logging.getLogger(__name__)

DEEPEN_IDLE_S = 30 * 60  # the local LLM's deepen pass runs once, this long after start
AUTO_IMPORT_HOURS = 6  # browse.auto_import_hours default; 0 turns the automatic import off
AUTO_FIRST_S = 60  # the first automatic-import check after a start
AUTO_EVERY_S = 10 * 60  # then this often


def _vault(vault) -> Vault:
    return vault if isinstance(vault, Vault) else Vault(Path(vault))


# ---- the import and the deepen pass ------------------------------------------------------------


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


def idle_runner(budget_s: float | None = None):
    """(runner, budget) for the imports' title checks: the configured local model only when
    it is already loaded and the PC is idle (`titles.may_run`: it never starts a runner or
    loads a model), else (None, None)."""
    from cortex.browse import titles

    runner, _ = titles.may_run()
    return (runner, _budget(budget_s)) if runner is not None else (None, None)


def check_titles(v: Vault, runner, settings: Settings, budget) -> dict:
    """C1's background pass: up to titles.CAP titles and search terms with no verdict yet,
    newest visit first (`store.pending`), to the loaded local model in batches with a pause
    between them, while the PC stays idle. Each batch's labels are saved as it comes (their
    days are made again by the import that follows); a Don't-save verdict takes what it
    matches out of every store. Counts only."""
    from contextlib import closing

    from cortex.browse import build, forget, privacy, sites, store, titles

    if not store.exists(v):
        return {}
    with closing(store.connect(v)) as con:
        texts = store.pending(con, settings, sites.load_verdicts(v), titles.CAP)
        got, c = titles.check(texts, runner, list(build.VET_LABELS), budget,
                              save=lambda lab: store.put_verdicts(con, lab), gate=titles.idle,
                              pause=titles.PAUSE_S)
    skip = frozenset(x for x in got.values() if privacy.audience(x, settings) == "skip")
    if skip:
        forget.purge(v, forget.Rule(cats=skip), write=False)
    return {"titles_checked": c["answered"], "titles_calls": c["calls"]}


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


def auto_sources(v: Vault) -> list:
    """The automatic import's browsers and profiles: those `sources.chosen` finds now that the
    user imported before (keys in the browse state), never one they never ticked."""
    from cortex.browse import build, sources

    try:  # a profile folder the OS refuses (PermissionError) must not stop the receiver
        done = build.load_state(v).get("sources") or {}
        return [s for s in sources.chosen(load_settings()) if s.key in done]
    except OSError as e:
        log.warning("browse: browsers not listed (%s)", type(e).__name__)
        return []


def run_import(v: Vault, progress=None, say=None, srcs: list | None = None) -> dict:
    """The background import (the receiver's runs, the app's live import every
    AUTO_EVERY_S), with every browser found (or `srcs`) and no `since`: the import itself
    reads only what is new and makes the days it changed. The local model only when it is
    already loaded and the PC idle (`idle_runner`): then first the titles waiting for its
    verdict (`check_titles`); never a download or a model load. It rests build.PAUSE_S after
    each chunk, so the machine stays quick. Then the private browse vectors, while the PC is
    idle. Its embedding is background work (`semantic.background`)."""
    from cortex import semantic
    from cortex.browse import build, sites, sources

    say = say or _say
    runner, budget = idle_runner()
    settings = load_settings()
    srcs = sources.chosen(settings) if srcs is None else srcs
    checked = (check_titles(v, runner, settings, budget)
               if runner is not None and settings.visible_to_claude else {})
    with semantic.background():
        r = build.run(v, srcs, settings, embed=semantic.load_embedder(),
                      runner=runner, budget=budget, progress=progress, say=say,
                      pause=build.PAUSE_S)
        r.update(checked)
        r["model"] = runner is not None
        sites.update_vectors(v, say, background=True)
    return r


def run_deepen(v: Vault, say=None, budget_s: float | None = None) -> dict | None:
    """The local LLM's pass over the site pages (cortex.browse.sites.deepen), each page's runner
    picked by `runner_for` (visible pages may go to the cloud runner when cloud processing is
    on), then the private browse vectors while the PC is idle (background work); None when
    neither runner is available. The cloud runner's run leaves one counts-only audit line."""
    from cortex import cloudllm, semantic
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
    with semantic.background():
        sites.update_vectors(v, say, background=True)
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


def auto_import_due(v: Vault, now: datetime | None = None, *, live: bool = False) -> bool:
    """True when the last import is older than `auto_import_hours` and the browse-wide lock is
    free (no import, deepen or forget runs). Never before the user's first import: a fresh
    install reads no history until they ask once. `live` (the app's live import, every
    AUTO_EVERY_S): any age; auto_import_hours 0 still turns it off."""
    from cortex.browse import build, sites

    hours = auto_import_hours()
    last = build.load_state(v).get("last_run")
    if not hours or not isinstance(last, str):
        return False
    try:
        t = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return False
    if not live and (now or datetime.now(UTC)) - t < timedelta(hours=hours):
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
    """The counts-only line for a finished run (the receiver's terminal or log)."""
    if "days_from" not in r:
        return f"done in {took:.0f} s: nothing new since the last run"
    return (f"done in {took:.0f} s: {r.get('private_written', 0)} private day pages written "
            f"(kept {r.get('kept', 0)} visits, private {r.get('private', 0)}, dropped "
            f"{r.get('dropped', 0)}); local model {'used' if r.get('model') else 'not used'}")


class Processor:
    """Runs `run_import` on the sources imported before (`auto_sources`) when the last import is
    older than browse.auto_import_hours (checked AUTO_FIRST_S after start, then every
    AUTO_EVERY_S), and the local LLM's deepen pass once, DEEPEN_IDLE_S after start; `tick` is the
    only runner and never overlaps itself. `clock` (monotonic) and `now` (wall time, for the last
    import's age) are injectable for tests."""

    def __init__(self, v: Vault, clock=time.monotonic, say=_say, now=None):
        self.v, self.clock, self.say = v, clock, say
        self.now = now or (lambda: datetime.now(UTC))
        self._auto_at = clock() + AUTO_FIRST_S
        self._deepen_at = clock() + DEEPEN_IDLE_S
        self.processing = False
        self.last: str = ""  # the last run's counts-only summary
        self._lock = threading.Lock()
        self._deepened = False  # once per start

    def tick(self) -> bool:
        """Run the import if it is due and none is running, else the local LLM's deepen pass
        once DEEPEN_IDLE_S after start; True if either ran."""
        with self._lock:
            now = self.clock()
            picked = None
            if not self.processing and now >= self._auto_at:
                self._auto_at = now + AUTO_EVERY_S
                if auto_import_due(self.v, self.now()):
                    picked = auto_sources(self.v)
            deep = not picked and not self._deepened and now >= self._deepen_at
            if self.processing or not (picked or deep):
                return False
            self.processing = True
            if deep:
                self._deepened = True
        if deep:
            return self._deepen()
        self.say(f"last import over {auto_import_hours():g} hours ago: importing new history")
        self.say("processing your browsing (the first run reads your whole history and can "
                 "take several minutes)…")
        t0 = time.perf_counter()

        def progress(key, n, new, err):
            self.say(f"  {key}: not read ({type(err).__name__}); the others go on" if err
                     else f"  {key}: {n} visits read, {new} new")

        try:
            # nothing here is a click: only the browsers and profiles imported before are read
            self.last = summary(run_import(self.v, progress=progress, srcs=picked),
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
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001  one bad tick never ends the receiver's runs
                log.warning("browse: background check failed: %s", type(e).__name__)


def serve(vault, *, stop: threading.Event | None = None) -> None:
    """Run the background import until `stop` is set (or Ctrl+C)."""
    Processor(_vault(vault)).loop(stop or threading.Event())
