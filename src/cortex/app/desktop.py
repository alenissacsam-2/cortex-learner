"""The desktop app (`cortex-app`): the app page in its own window, with a tray icon.

Threads. pywebview's Windows GUI (WinForms with Edge WebView2) must own the main thread;
pystray's Windows icon runs its own message loop on any thread:
- main: `webview.start()`, the window's GUI loop, until the window is destroyed;
- cortex-app-http: the app server (`server.start` adds its auto-import and watch threads; the
  desktop app has no idle stop);
- cortex-tray: the tray icon (Open Cortex, Start with Windows, Quit) and its notifications;
- cortex-notify: the once-a-day notifications (reviews due, an exam in 3 days or tomorrow);
- cortex-hotkey: quick capture's global hotkey (`Hotkey`, Windows only), a GetMessageW loop that
  shows the capture box (a second, hidden window) on each press; Quit posts it WM_QUIT;
- cortex-end: waits for `app.stop` (Quit in the tray or on the page, an update) and destroys
  the window, which ends the main thread's GUI loop; `run` then stops the rest.
Closing the window only hides it, and its page sleeps a minute later (`Desktop.hide`). Without
pywebview or the WebView2 runtime the app opens in the browser, as `cortex app` does.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from cortex.app import server, status

AUTOSTART = "Cortex"  # the HKCU Run value: `"<Cortex.exe>" -m cortex.app.launch --hidden`
APP_ID = "Cortex.Learner"  # the AppUserModelID, also on the shortcuts: pins and groups as Cortex
ICON = Path(__file__).resolve().parent.parent / "assets" / "cortex.ico"  # scripts/make_icon.py
SIZE, MIN_SIZE = (1200, 800), (640, 480)
AMBER, GROUND = "#ffb000", "#0b0b0a"  # app.css's night --lit and --ground
NOTIFY = ("reviews", "exams", "jobs", "updates")  # app.notify in config.yaml, all on by default
EXAM_DAYS = (3, 1)
FIRST_S, EVERY_S = 60, 30 * 60  # the first daily check after start, then every 30 minutes
KEEP_DAYS = 40  # a sent notification's key is remembered this long
SM_SHUTTINGDOWN = 0x2000
GWL_EXSTYLE, WS_EX_TOOLWINDOW = -20, 0x80
SLEEP_S = 60  # this long in the tray, the page sleeps (WebView2's TrySuspendAsync)
# quick capture's global hotkey (app.hotkey in config.yaml): choice -> (modifiers, key, label)
MOD_ALT, MOD_CONTROL, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x8, 0x4000
VK_SPACE, VK_K = 0x20, 0x4B
WM_QUIT, WM_HOTKEY, WM_APP = 0x12, 0x312, 0x8000  # WM_APP: app.hotkey changed, register again
HOTKEYS = {"ctrl+alt+space": (MOD_CONTROL | MOD_ALT, VK_SPACE, "Ctrl+Alt+Space"),
           "win+alt+space": (MOD_WIN | MOD_ALT, VK_SPACE, "Win+Alt+Space"),
           "ctrl+alt+k": (MOD_CONTROL | MOD_ALT, VK_K, "Ctrl+Alt+K"),
           "off": None}
HOTKEY_DEFAULT = "ctrl+alt+space"
BOX = (560, 120)  # the quick-capture box, CSS px

_lock = threading.Lock()


# ---- ~/.cortex/desktop.json: the window's place and the notifications already sent ----------


def state_file() -> Path:
    return Path.home() / ".cortex" / "desktop.json"


def load_state() -> dict:
    try:
        d = json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def save_state(key: str, value) -> None:
    """Set one key of desktop.json (temp file, then os.replace)."""
    with _lock:
        d = {**load_state(), key: value}
        p = state_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".desktop-{os.getpid()}.tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, p)


def _app_cfg() -> dict:
    a = status.config().get("app")
    return a if isinstance(a, dict) else {}


def _merge_app(fn) -> None:
    """`app:` in config.yaml becomes fn(app) (its other keys kept), under the config lock."""
    from cortex import taint
    taint.write_config(lambda cfg: {"app": fn(
        cfg["app"] if isinstance(cfg.get("app"), dict) else {})})


# ---- Start with Windows -----------------------------------------------------------------------


def autostart_command() -> str:
    """`Cortex.exe -m cortex.app.launch --hidden` (installed), else `cortex-app --hidden`
    (beside `cortex`): Cortex starts in the tray, window hidden."""
    from cortex.app.launch import installed_app
    if app := installed_app():
        return f'"{app}" -m cortex.app.launch --hidden'
    from cortex import clients
    cortex = Path(clients.cortex_command())
    exe = cortex.with_name("cortex-app" + cortex.suffix)
    if not exe.is_file():
        raise ValueError(f"{exe.name} is not beside {cortex.name}; reinstall Cortex to start "
                         "it with Windows.")
    return f'"{exe}" --hidden'


def autostart_state() -> dict:
    on = sys.platform == "win32"
    return {"supported": on, "enabled": on and status.run_get(AUTOSTART) is not None}


def set_autostart(enabled: bool) -> dict:
    """Start with Windows on or off. Either choice counts as made, so the first-run default
    (`first_run`) never overrides it."""
    if sys.platform != "win32":
        raise ValueError("Start with Windows is a Windows setting.")
    if enabled:
        status.run_set(autostart_command(), AUTOSTART)
    else:
        status.run_delete(AUTOSTART)
    _merge_app(lambda a: {**a, "autostart_asked": True})
    return autostart_state()


def first_run(echo=print) -> None:
    """The desktop app's first start turns Start with Windows on (the owner's choice), once."""
    if sys.platform != "win32" or _app_cfg().get("autostart_asked") is True:
        return
    try:
        set_autostart(True)
    except (OSError, ValueError) as e:  # tried again at the next start
        echo(f"Cortex app: Start with Windows stays off ({e})")


# ---- notifications ----------------------------------------------------------------------------


def notify_settings() -> dict:
    n = _app_cfg().get("notify")
    n = n if isinstance(n, dict) else {}
    return {k: n.get(k) is not False for k in NOTIFY}


def save_notify(body: dict) -> dict:
    if not body or set(body) - set(NOTIFY) or not all(isinstance(v, bool) for v in body.values()):
        raise ValueError(f"send true or false for any of {', '.join(NOTIFY)}")

    def merge(a: dict) -> dict:
        old = a.get("notify") if isinstance(a.get("notify"), dict) else {}
        return {**a, "notify": {**old, **body}}
    _merge_app(merge)
    return notify_settings()


def hotkey_setting() -> str:
    """app.hotkey: one of HOTKEYS; a missing or unknown value is the default."""
    k = _app_cfg().get("hotkey")
    return k if isinstance(k, str) and k in HOTKEYS else HOTKEY_DEFAULT


def save_hotkey(body: dict) -> str:
    k = body.get("hotkey")
    if not isinstance(k, str) or k not in HOTKEYS:
        raise ValueError(f"hotkey must be one of {', '.join(HOTKEYS)}")
    _merge_app(lambda a: {**a, "hotkey": k})
    return k


def _user32():  # tests replace this: never a real global hotkey in a test
    import ctypes
    return ctypes.windll.user32


class Hotkey:
    """Quick capture's global hotkey, on its own thread `cortex-hotkey`: RegisterHotKey delivers
    WM_HOTKEY to the thread that registered it, so that thread owns a GetMessageW loop. `change`
    posts WM_APP (register app.hotkey again), `stop` posts WM_QUIT; the loop then ends with
    UnregisterHotKey. A combination another app holds leaves `taken` set and nothing else."""

    ID = 1

    def __init__(self, on_press):
        self.on_press = on_press
        self.key: str | None = None  # the choice registered (or tried)
        self.taken = False
        self.on = False  # registered now
        self.tid: int | None = None
        self.done = threading.Event()  # set after each (re-)registration
        self.thread: threading.Thread | None = None

    def label(self) -> str | None:
        """"Ctrl+Alt+Space" while registered, else None (off, taken, not started)."""
        return HOTKEYS[self.key][2] if self.on and self.key else None

    def start(self) -> None:
        self.thread = threading.Thread(target=self.loop, daemon=True, name="cortex-hotkey")
        self.thread.start()
        self.done.wait(5)

    def _register(self, u) -> None:
        if self.on:
            u.UnregisterHotKey(None, self.ID)
            self.on = False
        self.key = hotkey_setting()
        spec = HOTKEYS[self.key]
        self.on = bool(spec) and bool(u.RegisterHotKey(None, self.ID, spec[0] | MOD_NOREPEAT,
                                                       spec[1]))
        self.taken = bool(spec) and not self.on
        self.done.set()

    def loop(self) -> None:
        import ctypes
        from ctypes import wintypes
        u = _user32()
        msg = wintypes.MSG()
        self.tid = threading.get_native_id()  # GetCurrentThreadId
        u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # makes the queue PostThreadMessage needs
        try:
            self._register(u)
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:  # 0: WM_QUIT, -1: error
                if msg.message == WM_HOTKEY:
                    try:
                        self.on_press()
                    except Exception:  # noqa: BLE001  (a failed show never ends the hotkey)
                        traceback.print_exc(limit=1)
                elif msg.message == WM_APP:
                    self._register(u)
        finally:
            if self.on:
                u.UnregisterHotKey(None, self.ID)
                self.on = False
            self.done.set()

    def _post(self, message: int) -> bool:
        if self.tid is None or self.thread is None or not self.thread.is_alive():
            return False
        return bool(_user32().PostThreadMessageW(self.tid, message, 0, 0))

    def change(self) -> None:
        """Register app.hotkey again at once; returns once it has (or after 2 s)."""
        self.done.clear()
        if self._post(WM_APP):
            self.done.wait(2)

    def stop(self) -> None:
        self._post(WM_QUIT)
        if self.thread is not None:
            self.thread.join(2)


def daily_notes(root: Path, now: datetime) -> list[tuple[str, str]]:
    """Today's (key, text) notes: reviews due and exams in 3 days or tomorrow, counted and named
    from non-private concepts and assessments only. Each key is sent once (`Notifier.daily`)."""
    from cortex import planner
    from cortex.catalog import Catalog
    from cortex.events import local_day
    from cortex.vault import Vault
    on, vault, day = notify_settings(), Vault(root), local_day(now)
    out = []
    if on["reviews"]:
        cat = Catalog.load(vault)
        states, _ = planner.current_states(vault, day, cat)
        hide = planner.hidden(vault, cat)
        n = sum(1 for c, st in states.items() if c not in hide and planner.is_due(st, day))
        if n:
            out.append((f"reviews:{day}", f"{n} review{'s' * (n != 1)} due today"))
    if on["exams"]:
        for a in planner._upcoming(vault, date.fromisoformat(day)):
            if not a["private"] and a["days"] in EXAM_DAYS:
                when = "tomorrow" if a["days"] == 1 else f"in {a['days']} days"
                name = (a["company"] or a["id"])[:60]
                out.append((f"exam:{a['id']}:{a['date']}:{a['days']}", f"{name} test {when}"))
    return out


class Notifier:
    """Notifications through `send(text)` (the tray): the daily notes and each job's end."""

    def __init__(self, app, send):
        self.app, self.send = app, send

    def daily(self, now: datetime | None = None) -> None:
        from cortex.events import local_day
        if self.app.entry() is None:  # no vault yet
            return
        now = now or datetime.now(UTC)
        day = local_day(now)
        sent = load_state().get("notified")
        sent = sent if isinstance(sent, dict) else {}
        new = [(k, text) for k, text in daily_notes(self.app.root, now) if k not in sent]
        for _, text in new:
            self.send(text)
        if new:
            keep = (date.fromisoformat(day) - timedelta(KEEP_DAYS)).isoformat()
            save_state("notified", {**{k: v for k, v in sent.items()
                                       if isinstance(v, str) and v >= keep},
                                    **{k: day for k, _ in new}})

    def loop(self, echo=print) -> None:
        wait = FIRST_S
        while not self.app.stop.wait(wait):
            wait = EVERY_S
            try:
                self.daily()
            except Exception as e:  # noqa: BLE001  (the type only: a message could quote a page)
                echo(f"Cortex app: the notification check failed ({type(e).__name__})")

    def job_ended(self, job) -> None:
        """"<job> finished" or "<job> failed"; a cancelled job, or a background one whose result
        is `quiet` (it found nothing to do), says nothing. Job names are the app's own words,
        never a page's title."""
        quiet = isinstance(job.result, dict) and job.result.get("quiet") is True
        if job.cancelled.is_set() or quiet or not notify_settings()["jobs"]:
            return
        failed = job.error is not None or (
            isinstance(job.result, dict) and job.result.get("ok") is False)
        self.send(f"{job.name} {'failed' if failed else 'finished'}")


# ---- the window and the tray -----------------------------------------------------------------


def _shutting_down() -> bool:  # tests replace this
    """Windows is signing out or shutting down: the window must close, not hide."""
    if sys.platform != "win32":
        return False
    import ctypes
    return bool(ctypes.windll.user32.GetSystemMetrics(SM_SHUTTINGDOWN))


def _native(win, fn) -> bool:
    """fn(form, control) on the GUI thread, given pywebview's WinForms form and its Edge WebView2
    control; False, with nothing run, for another backend or a window already gone."""
    form = getattr(win, "native", None)
    control = getattr(getattr(form, "browser", None), "webview", None)
    if not hasattr(type(control), "CoreWebView2"):  # its value: only on the GUI thread
        return False
    try:
        from System import Func, Type  # pythonnet: pywebview's WinForms backend loaded it
        form.Invoke(Func[Type](lambda: fn(form, control)))
        return True
    except Exception:  # noqa: BLE001  (closing at Quit; pythonnet's errors)
        traceback.print_exc(limit=1)
        return False


def _hide(form, control) -> None:
    # WinForms' Hide alone leaves WebView2's page visible (document.hidden false: its 3D brain
    # and polls ran on, docs/benchmarks/memory.md); hiding the control while the form is still
    # shown sets the WebView2 IsVisible false, so the page knows and can sleep
    control.Visible = False
    form.Hide()


def _show(form, control) -> None:
    form.Show()
    control.Visible = True  # IsVisible again: a sleeping page wakes
    form.Activate()


def _show_box(form, control) -> None:
    """The quick-capture box: kept out of the taskbar (a tool window, set while hidden, so
    the form keeps its handle and its WebView2), centred on the monitor with the mouse, a
    third of the way down, then shown and focused."""
    import ctypes

    from System.Drawing import Point  # pythonnet, loaded by pywebview's WinForms backend
    from System.Windows.Forms import Cursor, Screen
    if not form.Visible:
        u, hwnd = ctypes.windll.user32, ctypes.c_void_p(form.Handle.ToInt64())
        u.SetWindowLongW(hwnd, GWL_EXSTYLE, u.GetWindowLongW(hwnd, GWL_EXSTYLE)
                         | WS_EX_TOOLWINDOW)
    area = Screen.FromPoint(Cursor.Position).WorkingArea
    form.Location = Point(area.X + (area.Width - form.Width) // 2,
                          area.Y + area.Height // 3 - form.Height // 2)
    _show(form, control)
    control.Focus()


def _sleep(form, control) -> None:
    if not form.Visible and control.CoreWebView2 is not None:  # still hidden, WebView2 ready
        control.CoreWebView2.TrySuspendAsync()


class Desktop:
    """One running app's window and tray icon (threads: see the module doc)."""

    def __init__(self, app, url: str):
        self.app, self.url = app, url
        self.win = self.tray = None
        self.box = None  # the quick-capture box: a second window, made once, shown and hidden
        self.hotkey: Hotkey | None = None
        self.quitting, self.state = False, "normal"  # normal | minimized | maximized
        self.fresh = False  # started hidden and never shown: the first show loads start_url

    def show(self) -> bool:
        """Bring the window up (Open Cortex, a second launch); with no window, the browser.
        The page is the one for now (server.start_url: Tonight in the evening) in a new tab or
        on the first show after a hidden start; a window already seen keeps its page."""
        if self.win is None:
            server._open(server.start_url(self.app))
            return True
        if self.fresh:
            self.fresh = False
            self.win.load_url(server.start_url(self.app))
        if not _native(self.win, _show):
            self.win.show()
        if self.state == "minimized":
            self.win.restore()
        return True

    def closing(self) -> bool:
        """The window's X (pywebview's closing event; False cancels the close): remember its
        place and hide it, Cortex stays in the tray. Quit, a shutdown or no tray closes it."""
        if self.state == "normal":
            try:
                w = self.win
                save_state("window", {"width": w.width, "height": w.height, "x": w.x, "y": w.y})
            except Exception:  # noqa: BLE001  (a place not remembered never blocks closing)
                traceback.print_exc(limit=1)
        if self.quitting or self.tray is None or _shutting_down():
            return True
        self.app.lock()  # the Private switch relocks when the window goes to the tray
        # hidden from another thread: this handler runs on the GUI thread inside FormClosing
        threading.Thread(target=self.hide, daemon=True, name="cortex-hide").start()
        return False

    def hide(self) -> None:
        """To the tray; SLEEP_S later, still there, its page sleeps: WebView2 pauses its
        timers and animations and lets Windows take the renderer's memory back, and wakes it
        when the window shows."""
        if _native(self.win, _hide):
            self.sleep_later()
        else:
            self.win.hide()

    def capture(self) -> bool:
        """The hotkey or the tray's Quick capture: show the box, its input focused."""
        if self.box is None:
            return False
        if not _native(self.box, _show_box):
            self.box.show()
        return True

    def hide_box(self) -> bool:
        """Esc, Enter's saved line or the box losing focus: hide it (its text stays)."""
        if self.box is None:
            return False
        if not _native(self.box, _hide):
            self.box.hide()
        return True

    def box_closing(self) -> bool:
        """Alt+F4 on the box hides it; only Quit closes it."""
        if self.quitting:
            return True
        threading.Thread(target=self.hide_box, daemon=True, name="cortex-hide").start()
        return False

    def capture_label(self) -> str:
        key = self.hotkey.label() if self.hotkey is not None else None
        return f"Quick capture ({key})" if key else "Quick capture"

    def change_hotkey(self) -> None:
        """app.hotkey changed: register it again now, and the tray's text follows."""
        if self.hotkey is not None:
            self.hotkey.change()
        self.refresh_tray()

    def sleep_later(self) -> None:
        t = threading.Timer(SLEEP_S, lambda: self.quitting or _native(self.win, _sleep))
        t.daemon = True
        t.start()

    def minimized(self) -> None:
        self.state = "minimized"
        self.app.lock()  # the Private switch relocks when the window is minimised

    def end(self) -> None:
        """cortex-end: once the app stops, close the window (that ends `webview.start`)."""
        self.app.stop.wait()
        self.quitting = True
        if self.hotkey is not None:
            self.hotkey.stop()  # WM_QUIT: its thread unregisters the key
        for w in (self.box, self.win):  # the main window last: that ends webview.start
            if w is None:
                continue
            try:
                w.events.shown.wait(30)  # a window not yet started cannot be destroyed
                w.destroy()
            except Exception:  # noqa: BLE001  (already gone)
                traceback.print_exc(limit=1)

    def quit(self) -> None:
        self.app.quit({})  # cancels a running job and sets app.stop

    def notify(self, text: str) -> None:
        if self.tray is not None:
            self.tray.notify(text, "Cortex")

    def toggle_autostart(self) -> None:
        try:
            set_autostart(not autostart_state()["enabled"])
        except (OSError, ValueError) as e:
            self.notify(str(e)[:200])
        self.refresh_tray()

    def refresh_tray(self) -> None:
        if self.tray is not None:
            self.tray.update_menu()


LETTER_C = (".###.", "#...#", "#....", "#....", "#....", "#...#", ".###.")
SMALL = 32  # up to this size the dots are whole-pixel squares, so the C stays crisp


def mark(size: int):
    """The Cortex logo at `size` px (Pillow, already a dependency): a dark rounded square with
    an amber dot-matrix C, as the CORTEX wordmark draws it. The tray, the window, the
    shortcuts and Cortex.exe all use it (scripts/make_icon.py writes assets/cortex.ico)."""
    from PIL import Image, ImageDraw
    k = 1 if size <= SMALL else 4  # round dots are drawn at 4x and scaled down
    n = size * k
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    m = round(size * 0.06) * k
    side = n - 2 * m
    draw.rounded_rectangle((m, m, n - m - 1, n - m - 1), radius=round(side * 0.22), fill=GROUND)
    rows, cols = len(LETTER_C), len(LETTER_C[0])
    if k == 1:  # whole pixels
        pitch = max(1, round(side * 0.6 / rows))
        x0, y0 = (n - cols * pitch) // 2, (n - rows * pitch) // 2
    else:
        pitch = side * 0.6 / rows
        x0, y0 = (n - cols * pitch) / 2, (n - rows * pitch) / 2
    gap = pitch * 0.1  # round dots are 80% of the pitch
    for r, line in enumerate(LETTER_C):
        for c, cell in enumerate(line):
            if cell != "#":
                continue
            x, y = x0 + c * pitch, y0 + r * pitch
            if k == 1:
                draw.rectangle((x, y, x + pitch - 1, y + pitch - 1), fill=AMBER)
            else:
                draw.ellipse((x + gap, y + gap, x + pitch - gap, y + pitch - gap), fill=AMBER)
    return img if k == 1 else img.resize((size, size), Image.LANCZOS)


def _icon_image():
    """The tray icon: the logo at 64 px."""
    return mark(64)


def _tray(d: Desktop):
    """The tray icon on its own thread; a left click is Open Cortex."""
    import pystray
    item = pystray.MenuItem
    icon = pystray.Icon("cortex", _icon_image(), "Cortex", pystray.Menu(
        item("Open Cortex", lambda: d.show(), default=True),
        item(lambda _: d.capture_label(), lambda: d.capture()),
        item("Start with Windows", lambda: d.toggle_autostart(),
             checked=lambda _: autostart_state()["enabled"]),
        pystray.Menu.SEPARATOR,
        item("Quit", lambda: d.quit())))
    threading.Thread(target=icon.run, daemon=True, name="cortex-tray").start()
    return icon


def _webview():
    """pywebview drawing with Edge WebView2, or an exception that says why not."""
    import webview
    if sys.platform == "win32":  # webview.guilib is None until start(); initialize() is reused
        if webview.initialize().renderer != "edgechromium":  # MSHTML cannot run the app page
            raise RuntimeError("the Microsoft Edge WebView2 Runtime is not installed")
    return webview


def _geometry(webview) -> dict:
    """The remembered size, and place while it is still on a screen (a monitor may be gone)."""
    w = load_state().get("window")
    out = {"width": SIZE[0], "height": SIZE[1]}
    try:
        width, height, x, y = (int(w[k]) for k in ("width", "height", "x", "y"))
    except (KeyError, TypeError, ValueError):
        return out
    if width >= MIN_SIZE[0] and height >= MIN_SIZE[1]:
        out.update(width=width, height=height)
    if any(s.x <= x < s.x + s.width - 100 and s.y <= y < s.y + s.height - 100
           for s in webview.screens):
        out.update(x=x, y=y)
    return out


def run(root: Path | None, *, hidden: bool = False, port: int = 0, echo=print) -> None:
    """The desktop app until Quit; `hidden` starts it in the tray (Start with Windows). With no
    window possible, the browser app as `cortex app` runs it (nothing when hidden)."""
    try:
        webview = _webview()
    except Exception as e:  # noqa: BLE001  (ImportError, pythonnet's errors, no WebView2)
        echo(f"Cortex app: no app window ({type(e).__name__}: {e}); using the browser")
        if not hidden:  # at login: no browser tab nobody asked for
            server.run(root, port=port, echo=echo)
        return
    if sys.platform == "win32":  # before any window: the taskbar shows and pins Cortex, not Python
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    srv, app, url = server.start(root, port=port, echo=echo, idle=False)
    d = app.desktop = Desktop(app, url)
    d.fresh = hidden
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True,
                     name="cortex-app-http").start()
    try:
        d.tray = _tray(d)
    except Exception as e:  # noqa: BLE001  (no tray: closing the window quits)
        echo(f"Cortex app: no tray icon ({type(e).__name__}: {e})")
    notes = Notifier(app, d.notify)
    app.jobs.on_end = notes.job_ended
    threading.Thread(target=notes.loop, args=(echo,), daemon=True, name="cortex-notify").start()
    first_run(echo)
    echo(f"Cortex app: {url}")
    echo("Closing the window keeps Cortex in the tray; Quit there stops it.")
    try:
        d.win = webview.create_window("Cortex", url, hidden=hidden, min_size=MIN_SIZE,
                                      background_color=GROUND, **_geometry(webview))
        ev = d.win.events
        ev.closing += d.closing
        ev.minimized += d.minimized
        ev.maximized += lambda: setattr(d, "state", "maximized")
        ev.restored += lambda: setattr(d, "state", "normal")
        parts = urlsplit(url)  # quick capture's box: made now, hidden, shown by the hotkey
        d.box = webview.create_window(
            "Quick capture", f"{parts.scheme}://{parts.netloc}/capture?t={app.token}",
            hidden=True, frameless=True, on_top=True, resizable=False, width=BOX[0],
            height=BOX[1], background_color=GROUND)
        d.box.events.closing += d.box_closing
        if sys.platform == "win32":
            d.hotkey = Hotkey(d.capture)
            d.hotkey.start()
            d.refresh_tray()  # its Quick capture text names the key
        if hidden:  # Start with Windows: the page sleeps until the window is first opened
            d.sleep_later()
        threading.Thread(target=d.end, daemon=True, name="cortex-end").start()
        webview.start(icon=str(ICON))  # blocks this (main) thread until the window is destroyed
    except Exception as e:  # noqa: BLE001  (WebView2 failed to start: the page in the browser)
        echo(f"Cortex app: the window failed ({type(e).__name__}: {e}); using the browser")
        d.win = None
        if not hidden:
            d.show()
        app.stop.wait()  # the tray's Open Cortex now opens the browser
    finally:
        app.stop.set()
        if d.hotkey is not None:
            d.hotkey.stop()
        srv.shutdown()
        if d.tray is not None:
            d.tray.stop()
        server.finish(srv, app)
