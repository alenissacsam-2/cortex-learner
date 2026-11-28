"""The local model's checks of titles and search terms (C1), in batches, and when they may run.

`check` asks about BATCH texts per call (`localllm.Runner.classify_many`, a numbered JSON
reply); a reply that leaves lines out is asked again in halves, down to one text per call
(`classify`), so a bad answer costs a few calls, never the batch. Each batch's labels are saved
before the next is asked (`save`), so a stop keeps them and no text is asked twice.

`may_run` is the gate for work nobody is waiting for (the app's background pass, the import
job's title checks): the PC idle for IDLE_S (the OS's last input; nothing full screen), and the
configured local model already loaded (`localllm.find_loaded`: Ollama's /api/ps, LM Studio's
model list). It never starts a runner or loads a model.
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import sys
import time
from collections import Counter, deque

BATCH = 25
CAP = 300  # texts per background pass
PAUSE_S = 1.0  # between batches in the background, so the GPU is never held flat out
IDLE_S = 120


MUTTER = ["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
          "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
          "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"]
SCREENSAVER = ["gdbus", "call", "--session", "--dest", "org.freedesktop.ScreenSaver",
               "--object-path", "/org/freedesktop/ScreenSaver",
               "--method", "org.freedesktop.ScreenSaver.GetSessionIdleTime"]


def _query(args: list[str]) -> str | None:  # tests replace this: never a real ioreg or gdbus
    """The command's stdout, or None on any error, a timeout or a missing program."""
    try:
        from keepfold._proc import NO_WINDOW
        return subprocess.run(args, capture_output=True, text=True, timeout=5,
                              creationflags=NO_WINDOW, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def parse_ioreg(text: str | None) -> float | None:
    """The first `"HIDIdleTime" = <ns>` in `ioreg` output, in seconds."""
    m = re.search(r'"HIDIdleTime" = (\d+)', text or "")
    return int(m.group(1)) / 1e9 if m else None


def _gdbus_uint(text: str | None) -> int | None:
    m = re.search(r"\(uint(?:32|64) (\d+),\)", text or "")
    return int(m.group(1)) if m else None


def parse_gdbus_idle(text: str | None) -> float | None:
    """Mutter's `(uint64 <ms>,)` reply, in seconds."""
    ms = _gdbus_uint(text)
    return ms / 1000.0 if ms is not None else None


def idle_seconds() -> float | None:
    """Seconds since the last keyboard or mouse input (Windows' last input, macOS' HIDIdleTime,
    GNOME's Mutter idle monitor or else the freedesktop ScreenSaver), else None."""
    if sys.platform == "darwin":
        return parse_ioreg(_query(["ioreg", "-c", "IOHIDSystem", "-d", "4"]))
    if sys.platform.startswith("linux"):
        s = parse_gdbus_idle(_query(MUTTER))
        if s is None:
            sec = _gdbus_uint(_query(SCREENSAVER))  # this one answers in seconds
            s = float(sec) if sec is not None else None
        return s
    if sys.platform != "win32":
        return None

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    try:
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    except (AttributeError, OSError):
        return None
    return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0


def full_screen() -> bool:
    """True while a full-screen app, game or presentation runs (Windows' notification state
    busy, D3D full screen or presentation mode)."""
    if sys.platform != "win32":
        # shortcut: no full-screen check on Mac/Linux, add when a user reports a game interrupted
        return False
    state = ctypes.c_int(0)
    try:
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
    except (AttributeError, OSError):
        return False
    return state.value in (2, 3, 4)


def idle(min_s: float = IDLE_S) -> bool:
    """The owner is away: no input for `min_s` and nothing full screen. False where it cannot
    tell (no idle source): background model work then waits for an explicit run."""
    s = idle_seconds()
    return s is not None and s >= min_s and not full_screen()


def may_run(min_idle_s: float = IDLE_S):
    """(runner, why not): the loaded local runner when the PC is idle, else (None, reason)."""
    from keepfold import localllm

    if not localllm.enabled():
        return None, "no local model is set up"
    if not idle(min_idle_s):
        return None, "waiting until the PC is idle"
    try:
        r = localllm.find_loaded()
    except ValueError:
        r = None
    return (r, None) if r is not None else (None, "the local model is not loaded")


def check(texts: dict[str, str], runner, labels: list[str], budget=None, *, save=None,
          step=None, gate=None, pause: float = 0.0) -> tuple[dict[str, str], Counter]:
    """Label `texts` ({hash: text}, asked in that order) in batches of BATCH: (labels got,
    counts "calls", "asked", "answered"). Stops when the runner does not answer at all, the
    budget is spent, or `gate()` turns False (the owner came back); what was saved stays.
    `save(labels)` after each call that labelled anything; `step(done, total)` per call."""
    c: Counter = Counter()
    got: dict[str, str] = {}
    hs = list(texts)
    queue = deque(hs[i:i + BATCH] for i in range(0, len(hs), BATCH))
    done, sent = 0, set()
    while queue:
        if budget is not None and not budget.ok():
            c["left"] = sum(map(len, queue))
            break
        if gate is not None and not gate():
            c["paused"] = 1
            c["left"] = sum(map(len, queue))
            break
        batch = queue.popleft()
        c["calls"] += 1
        sent.update(batch)
        if len(batch) == 1:
            r = runner.classify(texts[batch[0]], labels, budget=budget)
            lab = {batch[0]: r[0]} if r else {}
            done += 1  # answered or not: asked again on a later pass, never in this one
        else:
            r = runner.classify_many([texts[h] for h in batch], labels, budget=budget)
            if r is None:  # no answer: the runner is gone or busy; the rest waits
                c["left"] = sum(map(len, queue)) + len(batch)
                break
            lab = {batch[i]: v for i, v in r.items() if 0 <= i < len(batch)}
            miss = [h for h in batch if h not in lab]
            done += len(batch) - len(miss)
            if miss:  # asked again in halves, before anything else
                half = (len(miss) + 1) // 2
                queue.extendleft([x for x in (miss[half:], miss[:half]) if x])
        c["answered"] += len(lab)
        got.update(lab)
        if save and lab:
            save(lab)
        if step:
            step(done, len(hs))
        if pause and queue:
            time.sleep(pause)
    c["asked"] = len(sent)
    return got, c
