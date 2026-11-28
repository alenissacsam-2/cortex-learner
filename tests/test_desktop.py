"""The desktop app (keepfold/app/desktop.py) with fake pywebview and pystray modules and the fake
Run key (conftest): window and tray, close hides, Quit ends everything, a second launch shows
the window, no idle stop, the browser fallback, Start with Windows (the first-run-once rule),
notifications (once a day, private left out, job ends) and their settings API."""

import json
import os
import shlex
import sys
import threading
import time
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import note
from test_app import call, daytime
from test_app import fakes as fakes  # noqa: F401 (fixture: nothing real runs or opens)
from test_app import vault as vault  # noqa: F401 (fixture)

from keepfold.app import desktop, launch, server, status
from keepfold.levels import ConceptState

pytestmark = pytest.mark.usefixtures("home")


class FakeEvent:
    """pywebview's window event: handlers added with +=, `set` runs them."""

    def __init__(self):
        self.fns, self.flag = [], threading.Event()

    def __iadd__(self, fn):
        self.fns.append(fn)
        return self

    def set(self):
        self.flag.set()
        return [fn() for fn in self.fns]

    def wait(self, timeout=None):
        return self.flag.wait(timeout)


class FakeWindow:
    def __init__(self, **kw):
        self.kw, self.calls, self.destroyed = kw, [], threading.Event()
        self.events = types.SimpleNamespace(**{k: FakeEvent() for k in (
            "closing", "closed", "minimized", "maximized", "restored", "shown", "loaded")})
        self.width, self.height, self.x, self.y = 1000, 700, 50, 60

    def show(self):
        self.calls.append("show")

    def hide(self):
        self.calls.append("hide")

    def restore(self):
        self.calls.append("restore")

    def load_url(self, url):
        self.calls.append(("load", url))

    def destroy(self):  # as WinForms: Close() raises the closing event first
        if False not in self.events.closing.set():
            self.destroyed.set()


def fake_webview(start, renderer="edgechromium"):
    wv = types.ModuleType("webview")
    wv.windows = []
    wv.screens = [types.SimpleNamespace(x=0, y=0, width=1920, height=1080)]
    wv.guilib = None  # as in pywebview: the module attribute is None until start()
    wv.initialize = lambda: types.SimpleNamespace(renderer=renderer)
    wv.menu = types.SimpleNamespace(  # webview.menu: Menu(title, items), MenuAction(title, fn)
        Menu=lambda title, items: types.SimpleNamespace(title=title, items=items),
        MenuAction=lambda title, fn: types.SimpleNamespace(title=title, fn=fn),
        MenuSeparator=lambda: types.SimpleNamespace(title=None))

    def create_window(title, url, **kw):
        w = FakeWindow(title=title, url=url, **kw)
        wv.windows.append(w)
        return w
    wv.create_window = create_window

    def start_gui(**kw):
        wv.start_kw = kw
        # as pywebview: start() shows every window, a hidden one shown and hidden at once, so
        # each one's `shown` fires (without it keepfold-end waited 30 s on the capture box)
        for w in wv.windows:
            w.events.shown.flag.set()
        return start(wv)
    wv.start = start_gui
    return wv


class FakeItem:
    def __init__(self, text, action, default=False, checked=None):
        self.text, self.action, self.default, self.checked = text, action, default, checked


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = [i for i in items if isinstance(i, FakeItem)]


class FakeIcon:
    made: list = []

    def __init__(self, name, image, title, menu):
        self.image, self.title, self.menu = image, title, menu
        self.notes, self.updates, self.stopped = [], 0, threading.Event()
        self.ran_on = None  # the thread that ran run() or run_detached()
        FakeIcon.made.append(self)

    def run(self):
        self.ran_on = ("run", threading.current_thread().name)
        self.stopped.wait()

    def run_detached(self):  # Mac and Linux: the GUI loop that webview.start runs drives it
        self.ran_on = ("run_detached", threading.current_thread().name)

    def stop(self):
        self.stopped.set()

    def notify(self, text, title):
        self.notes.append(text)

    def update_menu(self):
        self.updates += 1

    def item(self, text):
        return next(i for i in self.menu.items if FakeIcon.text(i) == text)

    @staticmethod
    def text(i):  # pystray: an item's text may be a callable, given the item
        return i.text(i) if callable(i.text) else i.text


@pytest.fixture
def gui(monkeypatch):
    """Fake pystray; `install(start)` puts a fake webview whose start() runs `start(wv)`. A tray
    host is there (Linux) and logout is not watched (Mac and Linux)."""
    tray = types.ModuleType("pystray")
    tray.Icon, tray.Menu, tray.MenuItem = FakeIcon, FakeMenu, FakeItem
    monkeypatch.setitem(sys.modules, "pystray", tray)
    FakeIcon.made = []
    monkeypatch.setattr(desktop, "_shutting_down", lambda: False)
    monkeypatch.setattr(desktop, "_tray_host", lambda: True)
    monkeypatch.setattr(desktop, "_watch_logout", lambda quit: None)
    monkeypatch.setattr(desktop, "_when_launched", lambda fn: None)
    monkeypatch.delenv("PYSTRAY_BACKEND", raising=False)  # Linux sets it: restored afterwards

    def install(start, renderer="edgechromium"):
        wv = fake_webview(start, renderer)
        monkeypatch.setitem(sys.modules, "webview", wv)
        return wv
    return install


@pytest.fixture
def win32(monkeypatch, tmp_path):
    """Windows, with `keepfold.exe` and `keepfold-app.exe` in a temp bin folder."""
    monkeypatch.setattr(sys, "platform", "win32")
    exe = tmp_path / "bin" / "keepfold.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    (exe.parent / "keepfold-app.exe").write_bytes(b"MZ")
    monkeypatch.setattr("keepfold.clients.keepfold_command", lambda: str(exe))
    return f'"{exe.with_name("keepfold-app.exe")}" --hidden'


def _until(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.02)


def test_window_tray_close_hides_second_launch_shows_and_quit_ends(
        gui, vault, monkeypatch, _no_real_run_key, capsys):
    """The window shows the app's own link; X hides it (the server and tray stay); a second
    `keepfold-app` brings it up, `--hidden` does nothing; no idle stop; Quit in the tray closes
    the window, ends the loop, stops the tray, removes app.json and remembers the place. Nothing
    fails on the way: no "window failed" fallback and no traceback once keepfold-end is done."""
    monkeypatch.setattr(server, "IDLE_S", 0)  # the browser app would stop at once
    monkeypatch.setattr(server, "WATCH_S", 0.05)
    daytime(monkeypatch)
    ran = []
    monkeypatch.setattr(desktop, "first_run", lambda echo: ran.append("first_run"))
    seen = {}

    def start(wv):
        w = wv.windows[0]
        w.events.shown.set()
        url = server.running_url()
        seen["url"], seen["kw"] = url, w.kw
        time.sleep(0.3)  # several idle checks
        seen["alive"] = server.running_url() == url
        assert w.events.closing.set() == [False]  # the X: cancelled, then hidden
        _until(lambda: "hide" in w.calls)
        assert server.running_url() == url
        monkeypatch.setattr(sys, "argv", ["keepfold-app.exe"])
        launch.main()  # a second launch
        monkeypatch.setattr(sys, "argv", ["keepfold-app.exe", "--hidden"])
        launch.main()  # Start with Windows while it runs: nothing
        seen["calls"] = list(w.calls)
        icon = FakeIcon.made[0]
        seen["menu"] = [(FakeIcon.text(i), i.default) for i in icon.menu.items]
        icon.item("Quit").action()
        assert w.destroyed.wait(10) and wv.windows[1].destroyed.wait(10)
    gui(start)
    lines = []
    desktop.run(vault, echo=lines.append)
    assert seen["url"] and seen["kw"]["url"] == seen["url"] and seen["kw"]["title"] == "Keepfold"
    assert sys.modules["webview"].start_kw == {"icon": str(desktop.ICON)}  # the window's icon
    assert seen["kw"]["hidden"] is False and seen["kw"]["width"] == desktop.SIZE[0]
    assert seen["alive"] is True
    assert seen["calls"] == ["hide", "show"]
    # the hotkey: Windows, a Mac (a FakeCarbon), not Linux off GNOME (conftest: no GNOME here)
    key = {"win32": " (Ctrl+Alt+Space)", "darwin": " (Ctrl+Option+Space)"}.get(sys.platform, "")
    assert seen["menu"] == [("Open Keepfold", True), (f"Quick capture{key}", False),
                            (desktop.autostart_label(), False), ("Quit", False)]
    assert FakeIcon.made[0].stopped.is_set() and ran == ["first_run"]
    assert not server.state_file().exists() and server.running_url() is None
    assert desktop.load_state()["window"] == {"width": 1000, "height": 700, "x": 50, "y": 60}
    assert any("tray" in s for s in lines)
    token = seen["url"].split("?t=")[1]  # D1 §4.3: the window opens with the key, the log has none
    assert token and not any(token in s for s in lines)
    for t in threading.enumerate():
        if t.name == "keepfold-end":
            t.join(60)
    assert not any("failed" in s for s in lines) and "Traceback" not in capsys.readouterr().err


def test_hidden_start_and_the_remembered_place(gui, vault, monkeypatch):
    """`--hidden` creates the window hidden; a remembered size is reused, a place off every
    screen is not; a too-small size falls back to the default."""
    monkeypatch.setattr(desktop, "first_run", lambda echo: None)
    desktop.save_state("window", {"width": 900, "height": 650, "x": 5000, "y": 10})
    kws = []

    def start(wv):
        kws.append(wv.windows[0].kw)
        FakeIcon.made[-1].item("Quit").action()
        wv.windows[0].events.shown.set()
        assert wv.windows[0].destroyed.wait(10)
    gui(start)
    desktop.run(vault, hidden=True, echo=lambda s: None)
    assert kws[0]["hidden"] is True and (kws[0]["width"], kws[0]["height"]) == (900, 650)
    assert "x" not in kws[0]
    wv = fake_webview(None)
    desktop.save_state("window", {"width": 900, "height": 650, "x": 100, "y": 80})
    assert desktop._geometry(wv) == {"width": 900, "height": 650, "x": 100, "y": 80}
    desktop.save_state("window", {"width": 100, "height": 650, "x": 100, "y": 80})
    assert desktop._geometry(wv)["width"] == desktop.SIZE[0]
    desktop.save_state("window", "junk")
    assert desktop._geometry(wv) == {"width": desktop.SIZE[0], "height": desktop.SIZE[1]}


def test_a_hidden_start_opens_on_the_page_for_the_time_it_is_shown(gui, vault, monkeypatch):
    """Started hidden at 09:00 (Start with Windows), opened from the tray at 21:00: Tonight.
    Only that first show loads the page; later ones keep where the owner is."""
    monkeypatch.setattr(desktop, "first_run", lambda echo: None)
    clock, orig = [datetime(2026, 11, 20, 9)], server.start_url
    monkeypatch.setattr(server, "start_url", lambda app, now=None: orig(app, clock[0]))
    seen = {}

    def start(wv):
        w = wv.windows[0]
        seen["first"] = w.kw["url"]
        clock[0] = datetime(2026, 11, 20, 21)
        for _ in range(2):
            FakeIcon.made[-1].item("Open Keepfold").action()
        seen["calls"] = list(w.calls)
        FakeIcon.made[-1].item("Quit").action()
        w.events.shown.set()
        assert w.destroyed.wait(10)
    gui(start)
    desktop.run(vault, hidden=True, echo=lambda s: None)
    assert "#tonight" not in seen["first"]
    (load, url), *rest = seen["calls"]
    assert load == "load" and url.endswith("#tonight") and rest == ["show", "show"]


@pytest.mark.parametrize("loads", [True, False])
def test_smoke_checks_the_page_and_the_address_then_quits(gui, vault, monkeypatch, loads):
    """`--smoke` (release-apps.yml): the page loaded and the app answers on its address, so it
    says ok and quits by itself; a page that never loads exits 1. Never Start at login."""
    monkeypatch.setattr(desktop, "SMOKE_LOAD_S", 0.2)
    monkeypatch.setattr(desktop, "SMOKE_SHOW_S", 0)
    monkeypatch.setattr(desktop, "first_run", lambda echo: pytest.fail("turned autostart on"))

    def start(wv):
        if loads:
            wv.windows[0].events.loaded.set()
        assert wv.windows[0].destroyed.wait(10)  # nobody clicks Quit
    gui(start)
    lines = []
    if loads:
        desktop.run(vault, echo=lines.append, smoke=True)
    else:
        with pytest.raises(SystemExit) as e:
            desktop.run(vault, echo=lines.append, smoke=True)
        assert e.value.code == 1
    assert ("Keepfold app: smoke ok" in lines) is loads
    assert ("Keepfold app: smoke FAILED" in lines) is not loads
    assert server.running_url() is None


def test_close_without_a_tray_or_while_windows_shuts_down_really_closes(monkeypatch):
    d = desktop.Desktop(types.SimpleNamespace(), "http://x")
    d.win = FakeWindow()
    monkeypatch.setattr(desktop, "_shutting_down", lambda: False)
    assert d.closing() is True  # no tray to come back from
    d.tray = FakeIcon("keepfold", None, "Keepfold", FakeMenu())
    monkeypatch.setattr(desktop, "_shutting_down", lambda: True)
    assert d.closing() is True  # never blocks sign-out or shutdown
    d.state = "minimized"
    d.show()
    assert d.win.calls == ["show", "restore"]


def test_the_tray_tells_the_page_it_is_hidden_and_it_sleeps_until_shown(monkeypatch):
    """M2: WinForms' Hide left the WebView2 page visible (its 3D brain and polls ran on); the
    control is hidden first, while the form still shows, so WebView2 hides the page. SLEEP_S
    later, still in the tray, the page sleeps (TrySuspendAsync); a show in between keeps it
    awake. Showing makes the control visible again (WebView2 wakes the page)."""
    log = []
    system = types.ModuleType("System")  # pythonnet's delegate: Func[Type](fn) is fn here
    system.Type = object
    system.Func = {object: lambda fn: fn}
    monkeypatch.setitem(sys.modules, "System", system)

    class Control:
        Visible = True
        CoreWebView2 = types.SimpleNamespace(TrySuspendAsync=lambda: log.append("suspend"))

        def __setattr__(self, k, v):
            log.append(f"control {'shown' if v else 'hidden'}")
            object.__setattr__(self, k, v)

    class Form:
        Visible = True
        browser = types.SimpleNamespace(webview=Control())

        def Invoke(self, fn):  # noqa: N802  (WinForms names)
            log.append("gui")
            fn()

        def Hide(self):  # noqa: N802
            log.append("form hidden")
            self.Visible = False

        def Show(self):  # noqa: N802
            log.append("form shown")
            self.Visible = True

        def Activate(self):  # noqa: N802
            log.append("activate")

    monkeypatch.setattr(desktop, "SLEEP_S", 0.1)
    d = desktop.Desktop(types.SimpleNamespace(), "http://x")
    d.win = FakeWindow()
    d.win.native = Form()
    d.hide()
    assert log == ["gui", "control hidden", "form hidden"] and d.win.calls == []
    _until(lambda: "suspend" in log)
    assert log[3:] == ["gui", "suspend"]
    del log[:]
    d.show()
    assert log == ["gui", "form shown", "control shown", "activate"] and d.win.calls == []
    d.hide()
    d.show()  # back before SLEEP_S: the page stays awake
    time.sleep(0.3)
    assert "suspend" not in log
    d.win.native.Visible = False  # started hidden (run's sleep_later): never shown, it sleeps
    d.sleep_later()
    _until(lambda: "suspend" in log)
    d.quitting = True  # Quit within SLEEP_S: no call to a window being destroyed
    del log[:]
    d.hide()
    time.sleep(0.3)
    assert log == ["gui", "control hidden", "form hidden"]
    assert desktop._native(FakeWindow(), lambda f, c: log.append("ran")) is False  # not WinForms


def test_the_capture_box_sleeps_while_hidden_and_a_show_keeps_it_awake(monkeypatch):
    """UX-A: the quick-capture box is a second WebView2 window, hidden nearly all the time:
    SLEEP_S after it is hidden, still hidden, its page sleeps; shown again before that, it
    stays awake. (Its placement, _show_box, is WinForms only: _show stands in.)"""
    log = []
    system = types.ModuleType("System")
    system.Type = object
    system.Func = {object: lambda fn: fn}
    monkeypatch.setitem(sys.modules, "System", system)
    monkeypatch.setattr(desktop, "_show_box", desktop._show)
    monkeypatch.setattr(desktop, "SLEEP_S", 0.1)

    class Control:
        Visible = True
        CoreWebView2 = types.SimpleNamespace(TrySuspendAsync=lambda: log.append("suspend"))

    class Form:
        Visible = False  # made hidden
        browser = types.SimpleNamespace(webview=Control())

        def Invoke(self, fn):  # noqa: N802  (WinForms names)
            fn()

        def Hide(self):  # noqa: N802
            self.Visible = False

        def Show(self):  # noqa: N802
            self.Visible = True

        def Activate(self):  # noqa: N802
            pass

    d = desktop.Desktop(types.SimpleNamespace(), "http://x")
    d.win, d.box = FakeWindow(), FakeWindow()
    d.box.native = Form()
    d.sleep_later(d.box)  # as run does once the box is made
    _until(lambda: log == ["suspend"])
    del log[:]
    assert d.capture() and d.box.native.Visible
    assert d.hide_box() and not d.box.native.Visible
    _until(lambda: log == ["suspend"])  # hidden SLEEP_S: asleep
    del log[:]
    d.capture()
    d.hide_box()
    d.capture()  # shown again before SLEEP_S: no suspend
    time.sleep(0.3)
    assert log == [] and d.box.native.Visible and d.box.calls == []


def test_minimise_and_hide_to_the_tray_relock_the_private_switch(gui, vault, monkeypatch):
    """The Private switch relocks when the window is minimised or hidden to the tray."""
    apps, seen, real = [], {}, server.start

    def start_server(*a, **k):
        srv, app, url = real(*a, **k)
        apps.append(app)
        return srv, app, url

    def start(wv):
        w = wv.windows[0]
        w.events.shown.set()
        for event in ("minimized", "closing"):  # closing: the X hides it to the tray
            apps[0].private_until = time.monotonic() + 600  # unlocked
            getattr(w.events, event).set()
            seen[event] = apps[0].private_unlocked
        FakeIcon.made[-1].item("Quit").action()
        assert w.destroyed.wait(10)
    monkeypatch.setattr(server, "start", start_server)
    monkeypatch.setattr(desktop, "first_run", lambda echo: None)
    gui(start)
    desktop.run(vault, echo=lambda s: None)
    assert seen == {"minimized": False, "closing": False}


def test_no_pywebview_or_no_webview2_falls_back_to_the_browser(gui, vault, monkeypatch):
    runs = []
    monkeypatch.setattr(server, "run", lambda root, port, echo: runs.append(root))
    monkeypatch.setitem(sys.modules, "webview", None)  # import webview fails
    lines = []
    desktop.run(vault, echo=lines.append)
    assert runs == [vault] and "no app window (ModuleNotFoundError" in lines[0]
    desktop.run(vault, hidden=True, echo=lines.append)  # at login: no browser tab
    assert runs == [vault]
    monkeypatch.setattr(sys, "platform", "win32")
    gui(lambda wv: pytest.fail("no window without WebView2"), renderer="mshtml")
    desktop.run(vault, echo=lines.append)
    assert runs == [vault, vault] and "WebView2" in lines[-1]


def test_a_window_that_fails_to_start_opens_the_browser(gui, vault, fakes, monkeypatch):
    monkeypatch.setattr(desktop, "first_run", lambda echo: None)

    def start(wv):
        threading.Thread(target=lambda: (
            _until(lambda: any(c[0] == "open" for c in fakes)),
            FakeIcon.made[0].item("Open Keepfold").action(),  # the tray opens the browser too
            FakeIcon.made[0].item("Quit").action())).start()
        raise RuntimeError("WebView2 failed")
    gui(start)
    lines = []
    desktop.run(vault, echo=lines.append)
    opened = [c[1] for c in fakes if c[0] == "open"]
    assert len(opened) == 2 and "?t=" in opened[0] and any("window failed" in s for s in lines)
    assert not server.state_file().exists()


@pytest.mark.parametrize("platform", [
    "darwin", "linux",
    pytest.param("win32", marks=pytest.mark.skipif(sys.platform != "win32",
                                                    reason="the real Windows run path")),
])
def test_the_tray_starts_per_os_before_the_window(gui, vault, monkeypatch, platform):
    """Mac and Linux: pystray's run_detached() on the main thread before webview.start(), whose
    GUI loop then drives the icon (spike S1, S2); Linux asks for the AppIndicator backend.
    Windows: icon.run() on its own keepfold-tray thread, as before."""
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(desktop, "first_run", lambda echo: None)
    main, seen = threading.current_thread().name, {}

    def start(wv):
        icon = FakeIcon.made[-1]
        if platform == "win32":
            _until(lambda: icon.ran_on is not None)
        seen["tray"], seen["start"] = icon.ran_on, threading.current_thread().name
        seen["backend"] = os.environ.get("PYSTRAY_BACKEND")
        icon.item("Quit").action()
        assert wv.windows[0].destroyed.wait(10)
    gui(start)
    desktop.run(vault, echo=lambda s: None)
    want = ("run", "keepfold-tray") if platform == "win32" else ("run_detached", main)
    assert seen == {"tray": want, "start": main,
                    "backend": "appindicator" if platform == "linux" else None}


def test_no_tray_host_on_linux_closing_quits_and_the_menu_has_the_tray_items(
        gui, vault, monkeypatch):
    """No StatusNotifierWatcher on D-Bus (GNOME without the AppIndicator extension): no icon,
    the tray's items are in the window's own menu, and closing the window ends the app."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(desktop, "_tray_host", lambda: False)
    monkeypatch.setattr(desktop, "first_run", lambda echo: None)
    seen = {}

    def start(wv):
        w, box = wv.windows
        (menu,) = wv.start_kw["menu"]
        seen["menu"] = (menu.title, [i.title for i in menu.items])
        d = w.events.closing.fns[0].__self__  # the Desktop: its closing handler's owner
        seen["quit"] = next(i.fn for i in menu.items if i.title == "Quit") == d.quit
        seen["closing"] = w.events.closing.set()  # the X: really closes, nothing to hide to
        w.events.closed.set()  # pywebview: the window is gone, the hidden box is not
        assert box.destroyed.wait(10)
    gui(start)
    lines = []
    desktop.run(vault, echo=lines.append)
    assert FakeIcon.made == [] and any("no tray icon" in s for s in lines)
    assert seen["menu"] == ("Keepfold", ["Open Keepfold", "Quick capture", "Start at login",
                                         None, "Quit"])
    assert seen["closing"] == [True] and seen["quit"] is True
    assert not server.state_file().exists()  # the app ended: app.json removed


def test_logout_quits_as_the_tray_quit_does(gui, vault, monkeypatch):
    """Mac and Linux: the logout watch gets the tray's own Quit (Desktop.quit). Once it fires
    the window closes when asked instead of hiding, so the OS's quit at logout never waits."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(desktop, "first_run", lambda echo: None)
    quits, watched, orig = [], [], desktop.Desktop.quit
    monkeypatch.setattr(desktop.Desktop, "quit", lambda self: (quits.append(1), orig(self)))
    monkeypatch.setattr(desktop, "_watch_logout", watched.append)
    seen = {}

    def start(wv):
        d = wv.windows[0].events.closing.fns[0].__self__  # the Desktop
        seen["same"] = watched == [d.quit]
        watched[0]()  # the user logs out
        seen["closing"] = d.closing()  # the OS asks the window to close: yes, not hidden
        assert wv.windows[0].destroyed.wait(10)
    gui(start)
    desktop.run(vault, echo=lambda s: None)
    assert seen == {"same": True, "closing": True} and quits == [1]
    assert not server.state_file().exists()
    FakeIcon.made[-1].item("Quit").action()  # the tray's Quit: the same Desktop.quit
    assert quits == [1, 1]


def test_the_logout_watch_mac_and_linux_never_holds_logout_up(monkeypatch):
    """The OS hooks behind the replaceable `_appkit` and `_gio`: a Mac observes
    NSWorkspaceWillPowerOffNotification; Linux listens to logind for PrepareForShutdown(true)
    and its own session's removal. Each only calls quit. `_tray_host` asks D-Bus for a
    StatusNotifierWatcher; no D-Bus at all is no tray."""
    quits = []
    center = types.SimpleNamespace(added=[])

    def observe(name, obj, queue, block):
        center.added.append((name, obj, queue))
        center.block = block
        return "token"
    center.addObserverForName_object_queue_usingBlock_ = observe
    appkit = types.SimpleNamespace(
        NSWorkspaceWillPowerOffNotification="NSWorkspaceWillPowerOffNotification",
        NSWorkspace=types.SimpleNamespace(sharedWorkspace=lambda: types.SimpleNamespace(
            notificationCenter=lambda: center)))
    monkeypatch.setattr(desktop, "_appkit", lambda: appkit)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert desktop._watch_logout(lambda: quits.append("mac")) == "token"
    assert center.added == [("NSWorkspaceWillPowerOffNotification", None, None)]
    center.block(object())
    assert quits == ["mac"]

    subs, owner = [], [":1.42"]

    def proxy(*a):
        if owner[0] == "boom":
            raise OSError("no session bus")
        return types.SimpleNamespace(args=a, get_name_owner=lambda: owner[0])
    bus = types.SimpleNamespace(signal_subscribe=lambda *a: subs.append(a) or 7)
    gio = types.SimpleNamespace(
        BusType=types.SimpleNamespace(SYSTEM="system", SESSION="session"),
        DBusSignalFlags=types.SimpleNamespace(NONE=0),
        DBusProxyFlags=types.SimpleNamespace(DO_NOT_LOAD_PROPERTIES=1, DO_NOT_CONNECT_SIGNALS=2,
                                             DO_NOT_AUTO_START=4),
        bus_get_sync=lambda kind, cancellable: bus if kind == "system" else None,
        DBusProxy=types.SimpleNamespace(new_for_bus_sync=proxy))
    monkeypatch.setattr(desktop, "_gio", lambda: gio)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_SESSION_ID", "3")
    del quits[:]
    assert desktop._watch_logout(lambda: quits.append("linux")) is bus
    (sub,) = subs
    assert sub[:6] == ("org.freedesktop.login1", "org.freedesktop.login1.Manager", None,
                       "/org/freedesktop/login1", None, 0)

    def signal(name, *args):
        sub[6](bus, ":1.1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager", name,
               types.SimpleNamespace(unpack=lambda: args))
    signal("PrepareForShutdown", False)  # a shutdown cancelled: nothing
    signal("SessionRemoved", "4", "/org/freedesktop/login1/session/_34")  # someone else
    signal("SessionNew", "3", "/org/freedesktop/login1/session/_33")
    assert quits == []
    signal("PrepareForShutdown", True)
    signal("SessionRemoved", "3", "/org/freedesktop/login1/session/_33")
    assert quits == ["linux", "linux"]
    monkeypatch.setattr(sys, "platform", "win32")
    assert desktop._watch_logout(lambda: quits.append("win")) is None  # _shutting_down there

    monkeypatch.setattr(sys, "platform", "linux")
    assert desktop._tray_host() is True
    owner[0] = None
    assert desktop._tray_host() is False
    owner[0] = "boom"
    assert desktop._tray_host() is False


def test_start_with_windows_on_off_and_the_first_run_rule(win32, _no_real_run_key):
    assert desktop.autostart_state() == {"supported": True, "enabled": False,
                                         "label": "Start with Windows"}
    desktop.first_run()
    assert _no_real_run_key == {desktop.AUTOSTART: win32}
    assert status.config()["app"]["autostart_asked"] is True
    assert desktop.set_autostart(False) == {"supported": True, "enabled": False,
                                            "label": "Start with Windows"}
    desktop.first_run()  # asked already: off stays off
    assert _no_real_run_key == {}


def test_start_with_windows_off_before_the_first_run_sticks(win32, _no_real_run_key):
    desktop.set_autostart(False)
    desktop.first_run()
    assert _no_real_run_key == {}


def test_start_with_windows_needs_the_app_exe_and_windows(win32, tmp_path, monkeypatch,
                                                          _no_real_run_key):
    (tmp_path / "bin" / "keepfold-app.exe").unlink()
    lines = []
    desktop.first_run(lines.append)
    assert _no_real_run_key == {} and "keepfold-app.exe" in lines[0]
    assert status.config().get("app", {}).get("autostart_asked") is None  # tried again later
    monkeypatch.setattr(sys, "platform", "linux")
    assert desktop.autostart_state() == {"supported": False, "enabled": False,
                                         "label": "Start at login"}
    with pytest.raises(ValueError):
        desktop.set_autostart(True)


def _keepfold_app(monkeypatch, platform: str) -> str:
    """The frozen Keepfold app (update.mode() "app") on `platform`, its executable under ~."""
    from keepfold import update
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(update, "mode", lambda: "app")
    exe = (Path.home() / "Keepfold.app" / "Contents" / "MacOS" / "Keepfold").as_posix()
    monkeypatch.setattr(sys, "executable", exe)
    return exe


def _exec_line(f: Path) -> str:
    return next(s for s in f.read_text(encoding="utf-8").splitlines() if s.startswith("Exec="))


class FakeSMApp:
    """SMAppService.mainAppService(): status() 0 not registered, 1 enabled, 2 requires
    approval; register and unregister answer (ok, NSError) as pyobjc does."""

    def __init__(self, status=0, fail=None):
        self.st, self.fail, self.calls = status, fail, []

    def status(self):
        return self.st

    def _answer(self, name, st):
        self.calls.append(name)
        if self.fail:
            return False, types.SimpleNamespace(localizedDescription=lambda: self.fail)
        self.st = st
        return True, None

    def registerAndReturnError_(self, err):
        return self._answer("register", 1)

    def unregisterAndReturnError_(self, err):
        return self._answer("unregister", 0)


def test_mac_app_autostart_uses_smappservice(monkeypatch):
    """Spike S4: the frozen Mac app is its own login item (SMAppService.mainAppService), not a
    LaunchAgent; switching removes the plist Task 13 wrote."""
    _keepfold_app(monkeypatch, "darwin")
    svc = FakeSMApp()
    monkeypatch.setattr(desktop, "_smapp", lambda: svc)
    plist = Path.home() / "Library" / "LaunchAgents" / "io.github.alenissacsam-2.keepfold.plist"
    assert desktop.autostart_file() == plist
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>", encoding="utf-8")  # Task 13's
    assert desktop.autostart_state() == {"supported": True, "enabled": False,
                                         "label": "Start at login"}
    lines = []
    desktop.first_run(lines.append)  # on by default, as on Windows
    assert lines == [] and svc.calls == ["register"] and not plist.exists()
    assert desktop.autostart_state()["enabled"] is True
    assert status.config()["app"]["autostart_asked"] is True
    assert desktop.set_autostart(False)["enabled"] is False
    assert svc.calls == ["register", "unregister"]
    svc.fail = "Operation not permitted"
    with pytest.raises(ValueError, match="Operation not permitted"):
        desktop.set_autostart(True)
    assert desktop.set_autostart(False)["enabled"] is False  # not registered: off is off


def test_mac_app_autostart_waiting_for_approval_says_where(monkeypatch, gui):
    """Status 2 (requires approval: switched off in System Settings) is not on, and says where
    to allow it; the tray's toggle shows that note."""
    _keepfold_app(monkeypatch, "darwin")
    svc = FakeSMApp(status=2)
    monkeypatch.setattr(desktop, "_smapp", lambda: svc)
    state = desktop.autostart_state()
    assert state["enabled"] is False
    assert "System Settings > General > Login Items" in state["note"]
    svc.fail = "Operation not permitted"  # register while unapproved: no error, the note
    assert desktop.set_autostart(True)["note"] == state["note"]
    shown = []  # a Mac notification goes through the notification center, not the tray
    monkeypatch.setattr(desktop, "_notify_mac", shown.append)
    d = desktop.Desktop(types.SimpleNamespace(), "http://x")
    d.tray = FakeIcon("k", None, "Keepfold", FakeMenu())
    d.toggle_autostart()
    assert shown == [state["note"]]


def test_linux_app_autostart_uses_stable_launcher(monkeypatch, tmp_path):
    """The .deb's /usr/bin/keepfold-app picks the newest copy at each login; the frozen
    executable's own path is versioned and goes away after an update."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    _keepfold_app(monkeypatch, "linux")
    monkeypatch.setattr(sys, "executable", "/home/u/.local/share/keepfold/app/0.7.0/keepfold-app")
    f = tmp_path / "xdg" / "autostart" / "io.github.alenissacsam_2.keepfold.desktop"
    assert desktop.autostart_file() == f
    lines = []
    desktop.first_run(lines.append)  # the first start turns it on, as on Windows
    assert lines == [] and _exec_line(f) == 'Exec="/usr/bin/keepfold-app" "--hidden"'
    assert shlex.split(_exec_line(f).removeprefix("Exec=")) == ["/usr/bin/keepfold-app",
                                                                "--hidden"]
    assert f.read_text(encoding="utf-8").startswith("[Desktop Entry]\nType=Application\n")
    assert desktop.autostart_state()["enabled"] is True
    desktop.set_autostart(False)
    desktop.first_run()  # asked already: off stays off
    assert not f.exists()


def test_linux_autostart_file_in_an_odd_home(monkeypatch, tmp_path):
    """Review focus 2: a home folder with a space and a non-ASCII letter still gets the entry."""
    h = tmp_path / "Ana María l"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    _keepfold_app(monkeypatch, "linux")
    desktop.set_autostart(True)
    f = desktop.autostart_file()
    assert f.parent == h / ".config" / "autostart" and f.is_file()


def test_login_launch_starts_hidden(gui, vault, monkeypatch):
    """The Mac app's login item passes no --hidden: when NSApp finishes launching, a launch
    Apple Event marked 'lgit' (keyAELaunchedAsLogInItem) hides the window; anything else, or
    no way to tell, leaves it shown."""
    _keepfold_app(monkeypatch, "darwin")
    monkeypatch.setattr(desktop, "first_run", lambda echo: None)
    login = [True]
    monkeypatch.setattr(desktop, "_login_launch", lambda: login[0])
    for at_login, want in ((True, ["hide"]), (False, [])):
        login[0], hooks, seen = at_login, [], {}
        monkeypatch.setattr(desktop, "_when_launched", hooks.append)

        def start(wv, hooks=hooks, seen=seen):
            (hook,) = hooks
            hook()  # NSApplicationDidFinishLaunchingNotification
            w = wv.windows[0]
            seen["calls"] = list(w.calls)
            seen["fresh"] = w.events.closing.fns[0].__self__.fresh
            FakeIcon.made[-1].item("Quit").action()
            assert w.destroyed.wait(10)
        gui(start)
        desktop.run(vault, echo=lambda s: None)
        assert seen == {"calls": want, "fresh": at_login}
    hooks = []
    monkeypatch.setattr(desktop, "_when_launched", hooks.append)

    def quit_now(wv):
        FakeIcon.made[-1].item("Quit").action()
        assert wv.windows[0].destroyed.wait(10)
    gui(quit_now)
    desktop.run(vault, hidden=True, echo=lambda s: None)  # --hidden: nothing to tell apart
    assert hooks == []


def test_login_launch_reads_the_launch_apple_event(monkeypatch):
    """`_login_launch`: keyAEPropData of the current Apple Event is 'lgit'; no event, another
    value or any error is False (the window shows)."""
    events = []

    def event(code):
        prop = types.SimpleNamespace(enumCodeValue=lambda: code)
        return types.SimpleNamespace(paramDescriptorForKeyword_=lambda k: prop
                                     if k == int.from_bytes(b"prop", "big") else None)
    manager = types.SimpleNamespace(currentAppleEvent=lambda: events[-1])
    appkit = types.SimpleNamespace(NSAppleEventManager=types.SimpleNamespace(
        sharedAppleEventManager=lambda: manager))
    monkeypatch.setattr(desktop, "_appkit", lambda: appkit)
    events.append(event(int.from_bytes(b"lgit", "big")))
    assert desktop._login_launch() is True
    events.append(event(int.from_bytes(b"xxxx", "big")))
    assert desktop._login_launch() is False
    events.append(None)  # not inside the launch event
    assert desktop._login_launch() is False
    monkeypatch.setattr(desktop, "_appkit", lambda: 1 / 0)
    assert desktop._login_launch() is False

    center, ran = types.SimpleNamespace(), []

    def observe(name, obj, queue, block):
        center.args, center.block = (name, obj, queue), block
        return "token"
    center.addObserverForName_object_queue_usingBlock_ = observe
    appkit = types.SimpleNamespace(
        NSApplicationDidFinishLaunchingNotification="didFinish",
        NSNotificationCenter=types.SimpleNamespace(defaultCenter=lambda: center))
    monkeypatch.setattr(desktop, "_appkit", lambda: appkit)
    assert desktop._when_launched(lambda: ran.append(1)) == "token"
    assert center.args == ("didFinish", None, None)  # queue None: on the posting (main) thread
    center.block(object())
    assert ran == [1]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_autostart_unsupported_in_dev_mode_off_windows(monkeypatch, platform):
    from keepfold import update
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(update, "mode", lambda: "dev")
    assert desktop.autostart_state() == {"supported": False, "enabled": False,
                                         "label": "Start at login"}
    with pytest.raises(ValueError, match="Start at login comes with the Keepfold app."):
        desktop.autostart_args()
    with pytest.raises(ValueError, match="comes with the Keepfold app"):
        desktop.set_autostart(True)
    lines = []
    desktop.first_run(lines.append)  # a PyPI install: nothing to start, nothing said
    assert lines == [] and not desktop.autostart_file().exists()
    assert status.config().get("app", {}).get("autostart_asked") is None


# status.login_text's output, captured from the code before render_login was factored out
_GOLDEN_LOGIN = {
    ("darwin", False): '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0">\n<dict>\n\t<key>Label</key>\n\t<string>keepfold-browse-receiver</string>\n\t<key>ProgramArguments</key>\n\t<array>\n\t\t<string>/opt/kf bin/keepfold</string>\n\t\t<string>browse</string>\n\t\t<string>serve</string>\n\t\t<string>--vault</string>\n\t\t<string>/home/ana mar\xeda/v "q" $x %y `z` \\w &amp; &lt;t&gt;</string>\n\t</array>\n\t<key>RunAtLoad</key>\n\t<true/>\n</dict>\n</plist>\n',  # noqa: E501
    ("linux", True): '[Unit]\nDescription=Keepfold browse receiver\n\n[Service]\nExecStart="/opt/kf bin/keepfold" "browse" "serve" "--vault" "/home/ana mar\xeda/v \\"q\\" $$x %%y `z` \\\\w & <t>"\n\n[Install]\nWantedBy=default.target\n',  # noqa: E501
    ("linux", False): '[Desktop Entry]\nType=Application\nName=Keepfold browse receiver\nExec="/opt/kf bin/keepfold" "browse" "serve" "--vault" "/home/ana mar\xeda/v \\\\"q\\\\" \\\\$x %%y \\\\`z\\\\` \\\\\\\\w & <t>"\nNoDisplay=true\n',  # noqa: E501
}


@pytest.mark.parametrize(("platform", "systemd"), list(_GOLDEN_LOGIN))
def test_login_text_unchanged(monkeypatch, platform, systemd):
    from pathlib import PurePosixPath
    monkeypatch.setattr("keepfold.clients.keepfold_command", lambda: "/opt/kf bin/keepfold")
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(status, "_systemd", lambda: systemd)
    root = PurePosixPath("/home/ana maría/v \"q\" $x %y `z` \\w & <t>")
    assert status.login_text(root) == _GOLDEN_LOGIN[(platform, systemd)]


def test_label_per_os(monkeypatch, gui):
    d = desktop.Desktop(types.SimpleNamespace(), "http://x")
    for platform, want in (("win32", "Start with Windows"), ("darwin", "Start at login"),
                           ("linux", "Start at login")):
        monkeypatch.setattr(sys, "platform", platform)
        assert desktop.autostart_label() == want
        assert desktop.autostart_state()["label"] == want  # the page's label
        icon = desktop._tray(d)  # the tray's item
        assert [FakeIcon.text(i) for i in icon.menu.items][2] == want
        icon.stop()


@pytest.fixture
def served(vault):
    srv, app = server.make_server(vault)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    yield app
    srv.shutdown()
    srv.server_close()


def test_the_app_api_for_autostart_notify_and_window(served, win32, _no_real_run_key):
    app = served
    assert call(app, "GET", "/api/app/autostart")[1] == {
        "supported": True, "enabled": False, "label": "Start with Windows"}
    assert call(app, "POST", "/api/app/autostart", {"enabled": True})[1]["enabled"] is True
    assert _no_real_run_key == {desktop.AUTOSTART: win32}
    assert call(app, "POST", "/api/app/autostart", {"enabled": "yes"})[0] == 400
    assert call(app, "GET", "/api/app/notify")[1] == {"reviews": True, "exams": True,
                                                       "jobs": True, "updates": True,
                                                       "system_off": False}
    assert call(app, "POST", "/api/app/notify", {"jobs": False})[1]["jobs"] is False
    assert desktop.notify_settings() == {"reviews": True, "exams": True, "jobs": False,
                                         "updates": True}
    for bad in ({}, {"bogus": True}, {"jobs": "no"}):
        assert call(app, "POST", "/api/app/notify", bad)[0] == 400
    assert call(app, "POST", "/api/window/show", {})[1] == {"shown": False}  # browser app
    assert call(app, "POST", "/api/window/show", {}, token=False)[0] == 401
    d = desktop.Desktop(app, "http://x")
    d.win, d.tray = FakeWindow(), FakeIcon("keepfold", None, "Keepfold", FakeMenu())
    app.desktop = d
    assert call(app, "POST", "/api/window/show", {})[1] == {"shown": True}
    assert d.win.calls == ["show"]
    call(app, "POST", "/api/app/autostart", {"enabled": False})
    assert d.tray.updates == 1  # the tray's tick follows the page


def test_idle_stop_only_in_the_browser_app(vault, monkeypatch):
    monkeypatch.setattr(server, "IDLE_S", 0)
    monkeypatch.setattr(server, "WATCH_S", 0.05)
    for idle in (False, True):
        srv, app, url = server.start(vault, idle=idle, echo=lambda s: None)
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05})
        t.start()
        assert app.stop.wait(0.4) is idle  # the browser app stops; the desktop app does not
        app.stop.set()  # Quit
        t.join(5)
        assert not t.is_alive()
        server.finish(srv, app)


NOON = datetime(2026, 11, 17, 12, tzinfo=UTC)  # the Acme OA (vault_mini) is on 2026-11-20


@pytest.fixture
def due(vault, monkeypatch):
    """two-pointers and the private secret-diary are due; a private exam is in 3 days too."""
    from keepfold import planner
    note(vault, "assessments/hidden-exam.md", 'id: "hidden-exam"\ncompany: "HiddenCorp"\n'
         'date: "2026-11-20"\noutcome: "pending"\nprivate: true\n', "# Hidden")
    states = {c: ConceptState(c, state="needs_refresh") for c in ("two-pointers", "secret-diary")}
    states["arrays-basics"] = ConceptState("arrays-basics", state="solid", next_due="2099-01-01")
    monkeypatch.setattr(planner, "current_states", lambda *a, **k: (states, {}))
    return vault


def _notifier(root):
    sent = []
    app = types.SimpleNamespace(root=root, entry=lambda: {}, stop=threading.Event())
    return desktop.Notifier(app, sent.append), sent


def test_daily_notes_once_a_day_without_private_ones(due):
    n, sent = _notifier(due)
    n.daily(NOON)
    assert sent == ["1 review due today", "Acme test in 3 days"]
    n.daily(NOON)  # the same day: nothing again
    assert len(sent) == 2
    n.daily(NOON.replace(day=18))  # the next day: reviews again, no exam (2 days to go)
    n.daily(NOON.replace(day=19))  # tomorrow
    assert sent[2:] == ["1 review due today", "1 review due today", "Acme test tomorrow"]
    assert not any("Hidden" in s or "Private" in s or "secret" in s for s in sent)
    assert set(desktop.load_state()["notified"].values()) <= {"2026-11-17", "2026-11-18",
                                                              "2026-11-19"}


def test_daily_notes_follow_the_settings(due):
    desktop.save_notify({"reviews": False})
    n, sent = _notifier(due)
    n.daily(NOON)
    assert sent == ["Acme test in 3 days"]
    desktop.save_notify({"exams": False})
    n.daily(NOON.replace(day=19))
    assert sent == ["Acme test in 3 days"]
    n.app.entry = lambda: None  # no vault yet: nothing at all
    desktop.save_notify({"reviews": True, "exams": True})
    n.daily(NOON.replace(day=19))
    assert sent == ["Acme test in 3 days"]


def test_job_ends_notify_finished_or_failed_not_cancelled():
    n, sent = _notifier(None)
    jobs = server.Jobs()
    ended = []

    def on_end(job):
        n.job_ended(job)
        ended.append(job)
    jobs.on_end = on_end

    def run(name, section, fn):  # until on_end ran: `running` clears before it, so a loaded
        k = len(ended) + 1       # machine could otherwise reorder the notes
        jobs.start(name, section, fn)
        _until(lambda: len(ended) >= k, timeout=30.0)
    run("Backing up the vault", "vault", lambda job: {"ok": True})
    run("Running the doctor", "vault", lambda job: {"ok": False})
    run("Importing", "setup", lambda job: 1 / 0)

    def cancelled(job):
        job.cancel()
        return {"cancelled": True}
    run("Downloading", "setup", cancelled)
    assert sent == ["Backing up the vault finished", "Running the doctor failed",
                    "Importing failed"]
    desktop.save_notify({"jobs": False})
    run("Backing up the vault", "vault", lambda job: {"ok": True})
    assert len(sent) == 3


def test_the_tray_notifies_and_toggles_start_with_windows(win32, _no_real_run_key):
    d = desktop.Desktop(types.SimpleNamespace(), "http://x")
    d.tray = FakeIcon("keepfold", desktop._icon_image(), "Keepfold", FakeMenu())
    assert d.tray.image.size == (64, 64)
    d.notify("3 reviews due today")
    assert d.tray.notes == ["3 reviews due today"]
    d.toggle_autostart()
    assert _no_real_run_key == {desktop.AUTOSTART: win32} and d.tray.updates == 1
    d.toggle_autostart()
    assert _no_real_run_key == {}


class FakeUNCenter:
    """UNUserNotificationCenter: the authorization answer is given by the test (`answer`)."""

    def __init__(self):
        self.asked, self.sent = [], []

    def requestAuthorizationWithOptions_completionHandler_(self, options, done):
        self.asked.append(options)
        self.answer = done

    def addNotificationRequest_withCompletionHandler_(self, request, done):
        self.sent.append(request)


class FakeUNContent:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def setTitle_(self, t):
        self.title = t

    def setBody_(self, b):
        self.body = b


@pytest.fixture
def unc(monkeypatch, home):
    """A Mac with a fake UserNotifications, fresh authorization state and no real osascript."""
    center = FakeUNCenter()
    un = types.SimpleNamespace(
        UNAuthorizationOptionAlert=4, UNAuthorizationOptionSound=2,
        UNUserNotificationCenter=types.SimpleNamespace(currentNotificationCenter=lambda: center),
        UNMutableNotificationContent=FakeUNContent,
        UNNotificationRequest=types.SimpleNamespace(
            requestWithIdentifier_content_trigger_=lambda i, c, t: (c.title, c.body, t)))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(desktop, "_unc", lambda: un)
    monkeypatch.setattr(desktop, "_MAC", {"granted": None, "pending": None})
    monkeypatch.setattr(launch, "_run_alert", lambda argv: pytest.fail("ran osascript"))
    return center


def test_mac_notifies_through_user_notifications_asking_once(unc):
    """The first notification asks for authorization; notifications sent while it is asked
    wait for the answer; later ones never ask again. Under Keepfold's own name, not the tray."""
    d = desktop.Desktop(types.SimpleNamespace(), "http://x")
    d.tray = FakeIcon("keepfold", None, "Keepfold", FakeMenu())
    d.notify("3 reviews due today")
    d.notify("An exam tomorrow")
    assert unc.asked == [4 | 2] and unc.sent == []
    unc.answer(True, None)
    d.notify("Importing finished")
    assert unc.asked == [6]
    assert unc.sent == [("Keepfold", t, None) for t in
                        ("3 reviews due today", "An exam tomorrow", "Importing finished")]
    assert d.tray.notes == [] and desktop.load_state()["notify_denied"] is False


def test_mac_denied_drops_notifications_and_notes_it(unc, served):
    d = desktop.Desktop(types.SimpleNamespace(), "http://x")
    d.notify("3 reviews due today")
    unc.answer(False, None)
    d.notify("An exam tomorrow")
    assert unc.asked == [6] and unc.sent == []
    assert desktop.load_state()["notify_denied"] is True
    assert call(served, "GET", "/api/app/notify")[1]["system_off"] is True


@pytest.mark.parametrize("broken", [ImportError, RuntimeError])
def test_mac_falls_back_to_osascript_without_the_framework(unc, monkeypatch, broken):
    """No UserNotifications (or no bundle, so no center): osascript's display notification,
    the text an AppleScript string in an argv list."""
    ran = []

    def unavailable():
        raise broken("no UserNotifications")
    monkeypatch.setattr(desktop, "_unc", unavailable)
    monkeypatch.setattr(launch, "_run_alert", ran.append)
    desktop.Desktop(types.SimpleNamespace(), "http://x").notify('say "hi"; rm -rf ~')
    assert ran == [["osascript", "-e",
                    'display notification "say \\"hi\\"; rm -rf ~" with title "Keepfold"']]


class FakeGApp:
    made = []

    def __init__(self, app_id, flags):
        self.app_id, self.flags, self.registered, self.sent = app_id, flags, 0, []
        FakeGApp.made.append(self)

    def register(self, cancellable):
        self.registered += 1
        return True

    def send_notification(self, nid, n):
        self.sent.append((nid, n.title, n.body))


class FakeGNotification:
    def __init__(self, title):
        self.title = title

    def set_body(self, body):
        self.body = body


@pytest.fixture
def gnote(monkeypatch):
    FakeGApp.made = []
    gio = types.SimpleNamespace(
        ApplicationFlags=types.SimpleNamespace(NON_UNIQUE=32),
        Application=types.SimpleNamespace(new=FakeGApp),
        Notification=types.SimpleNamespace(new=FakeGNotification))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(desktop, "_gio", lambda: gio)
    monkeypatch.setattr(desktop, "_GAPP", [])
    monkeypatch.setattr(launch, "_run_alert", lambda argv: pytest.fail("ran notify-send"))
    return FakeGApp.made


def test_linux_notifies_through_one_gio_application_with_the_desktop_id(gnote):
    d = desktop.Desktop(types.SimpleNamespace(), "http://x")
    d.notify("3 reviews due today")
    d.notify("An exam tomorrow")
    (app,) = gnote
    assert (app.app_id, app.flags, app.registered) == ("io.github.alenissacsam_2.keepfold", 32, 1)
    assert app.app_id == desktop.DESKTOP_ID
    assert app.sent == [(None, "Keepfold", "3 reviews due today"),
                        (None, "Keepfold", "An exam tomorrow")]


def test_linux_falls_back_to_notify_send_without_gio(gnote, monkeypatch):
    ran = []

    def no_gi():
        raise ImportError("No module named 'gi'")
    monkeypatch.setattr(desktop, "_gio", no_gi)
    monkeypatch.setattr(launch, "_run_alert", ran.append)
    desktop.Desktop(types.SimpleNamespace(), "http://x").notify('say "hi"; rm -rf ~')
    assert ran == [["notify-send", "--app-name=Keepfold", "Keepfold", 'say "hi"; rm -rf ~']]

    def missing(argv):
        raise FileNotFoundError("notify-send")
    monkeypatch.setattr(launch, "_run_alert", missing)
    desktop.Desktop(types.SimpleNamespace(), "http://x").notify("dropped, quietly")


def test_desktop_json_is_written_whole(home):
    desktop.save_state("window", {"width": 1})
    desktop.save_state("notified", {"a": "2026-11-19"})
    assert json.loads(desktop.state_file().read_text()) == {
        "window": {"width": 1}, "notified": {"a": "2026-11-19"}}
    assert [p.name for p in desktop.state_file().parent.iterdir()
            if p.name.endswith(".tmp")] == []


def test_the_mark_is_crisp_when_small_and_the_ico_is_drawn_from_it():
    """The logo: a dark rounded square with an amber dot-matrix K. Up to 32 px each lit cell is
    whole pixels of exactly AMBER on GROUND; the committed keepfold.ico is `mark` at every size
    (scripts/make_icon.py), and the tray icon is the same mark."""
    from PIL import Image

    amber, ground = (255, 176, 0, 255), (11, 11, 10, 255)
    lit = sum(line.count("#") for line in desktop.LETTER_K)
    for size, pitch in ((16, 1), (24, 2), (32, 2)):
        img = desktop.mark(size)
        colours = dict((c, n) for n, c in img.getcolors(size * size))
        assert set(colours) <= {amber, ground, (0, 0, 0, 0)} and img.getpixel((0, 0))[3] == 0
        assert colours[amber] == lit * pitch ** 2
    big = desktop.mark(256)
    assert big.size == (256, 256) and big.getpixel((0, 0))[3] == 0
    assert big.getpixel((128, 128)) == ground  # the C's open middle
    ico = Image.open(desktop.ICON)
    for size in (16, 24, 32, 48, 64, 128, 256):
        got = ico.ico.getimage((size, size)).convert("RGBA")
        assert got.tobytes() == desktop.mark(size).tobytes(), size
    assert desktop._icon_image().tobytes() == desktop.mark(64).tobytes()


def test_start_with_windows_runs_keepfold_exe_when_installed(win32, tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "Keepfold.exe").write_bytes(b"MZ")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))  # the tool venv
    want = f'"{tmp_path / "app" / "Keepfold.exe"}" -m keepfold.app.launch --hidden'
    assert desktop.autostart_command() == want


def test_log_redirects_without_tty_off_windows(monkeypatch):
    from pathlib import Path
    log = Path.home() / ".keepfold" / "logs" / "app.log"
    monkeypatch.setattr(launch, "_tty", lambda: True)
    monkeypatch.setattr(sys, "platform", "linux")
    out = sys.stdout
    launch._log_to("app.log")  # a terminal: output stays there
    assert sys.stdout is out and not log.exists()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(launch, "_tty", lambda: False)
    launch._log_to("app.log")  # Windows with a console stays as it was
    assert sys.stdout is out and not log.exists()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "stdout", out)  # monkeypatch restores the real streams afterwards
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    launch._log_to("app.log")  # started from the dock or app menu: no terminal
    try:
        assert sys.stdout is not out and sys.stderr is sys.stdout
        print("hello")
    finally:
        sys.stdout.close()
    assert "hello" in log.read_text(encoding="utf-8")


def test_quote_applescript():
    assert launch._quote_applescript('a "b" \\ c') == '"a \\"b\\" \\\\ c"'


@pytest.mark.parametrize(("platform", "have", "want"), [
    ("darwin", set(), ["osascript", "-e",
                       'display alert "Keepfold" message "say \\"hi\\"; rm -rf ~" as critical']),
    ("linux", {"zenity", "notify-send"}, ["zenity", "--error", "--text", 'say "hi"; rm -rf ~']),
    ("linux", {"notify-send"}, ["notify-send", "Keepfold", 'say "hi"; rm -rf ~']),
])
def test_alert_off_windows_runs_the_os_dialog_with_the_text_as_an_argument(
        monkeypatch, platform, have, want):
    ran = []
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(launch.shutil, "which", lambda name: f"/usr/bin/{name}" if name in have
                        else None)
    monkeypatch.setattr(launch, "_run_alert", ran.append)
    launch._alert('say "hi"; rm -rf ~')
    assert ran == [want]


def test_alert_on_linux_without_a_dialog_tool_writes_stderr(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(launch.shutil, "which", lambda name: None)
    monkeypatch.setattr(launch, "_run_alert", lambda argv: pytest.fail("ran a program"))
    launch._alert("Keepfold could not start.")
    assert "Keepfold could not start." in capsys.readouterr().err


def test_alert_falls_back_to_stderr_when_the_dialog_fails(monkeypatch, capsys):
    def broken(argv):
        raise OSError("no display")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(launch, "_run_alert", broken)
    launch._alert("Keepfold could not start.")
    assert "Keepfold could not start." in capsys.readouterr().err
