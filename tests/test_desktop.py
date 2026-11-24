"""The desktop app (cortex/app/desktop.py) with fake pywebview and pystray modules and the fake
Run key (conftest): window and tray, close hides, Quit ends everything, a second launch shows
the window, no idle stop, the browser fallback, Start with Windows (the first-run-once rule),
notifications (once a day, private left out, job ends) and their settings API."""

import json
import sys
import threading
import time
import types
from datetime import UTC, datetime

import pytest
from conftest import note
from test_app import call, daytime
from test_app import fakes as fakes  # noqa: F401 (fixture: nothing real runs or opens)
from test_app import vault as vault  # noqa: F401 (fixture)

from cortex.app import desktop, launch, server, status
from cortex.levels import ConceptState

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
            "closing", "minimized", "maximized", "restored", "shown")})
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

    def create_window(title, url, **kw):
        w = FakeWindow(title=title, url=url, **kw)
        wv.windows.append(w)
        return w
    wv.create_window = create_window

    def start_gui(**kw):
        wv.start_kw = kw
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
        FakeIcon.made.append(self)

    def run(self):
        self.stopped.wait()

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
    """Fake pystray; `install(start)` puts a fake webview whose start() runs `start(wv)`."""
    tray = types.ModuleType("pystray")
    tray.Icon, tray.Menu, tray.MenuItem = FakeIcon, FakeMenu, FakeItem
    monkeypatch.setitem(sys.modules, "pystray", tray)
    FakeIcon.made = []
    monkeypatch.setattr(desktop, "_shutting_down", lambda: False)

    def install(start, renderer="edgechromium"):
        wv = fake_webview(start, renderer)
        monkeypatch.setitem(sys.modules, "webview", wv)
        return wv
    return install


@pytest.fixture
def win32(monkeypatch, tmp_path):
    """Windows, with `cortex.exe` and `cortex-app.exe` in a temp bin folder."""
    monkeypatch.setattr(sys, "platform", "win32")
    exe = tmp_path / "bin" / "cortex.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    (exe.parent / "cortex-app.exe").write_bytes(b"MZ")
    monkeypatch.setattr("cortex.clients.cortex_command", lambda: str(exe))
    return f'"{exe.with_name("cortex-app.exe")}" --hidden'


def _until(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.02)


def test_window_tray_close_hides_second_launch_shows_and_quit_ends(
        gui, vault, monkeypatch, _no_real_run_key):
    """The window shows the app's own link; X hides it (the server and tray stay); a second
    `cortex-app` brings it up, `--hidden` does nothing; no idle stop; Quit in the tray closes
    the window, ends the loop, stops the tray, removes app.json and remembers the place."""
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
        monkeypatch.setattr(sys, "argv", ["cortex-app.exe"])
        launch.main()  # a second launch
        monkeypatch.setattr(sys, "argv", ["cortex-app.exe", "--hidden"])
        launch.main()  # Start with Windows while it runs: nothing
        seen["calls"] = list(w.calls)
        icon = FakeIcon.made[0]
        seen["menu"] = [(FakeIcon.text(i), i.default) for i in icon.menu.items]
        icon.item("Quit").action()
        assert w.destroyed.wait(10) and wv.windows[1].destroyed.wait(10)
    gui(start)
    lines = []
    desktop.run(vault, echo=lines.append)
    assert seen["url"] and seen["kw"]["url"] == seen["url"] and seen["kw"]["title"] == "Cortex"
    assert sys.modules["webview"].start_kw == {"icon": str(desktop.ICON)}  # the window's icon
    assert seen["kw"]["hidden"] is False and seen["kw"]["width"] == desktop.SIZE[0]
    assert seen["alive"] is True
    assert seen["calls"] == ["hide", "show"]
    key = " (Ctrl+Alt+Space)" if sys.platform == "win32" else ""  # the hotkey: Windows only
    assert seen["menu"] == [("Open Cortex", True), (f"Quick capture{key}", False),
                            ("Start with Windows", False), ("Quit", False)]
    assert FakeIcon.made[0].stopped.is_set() and ran == ["first_run"]
    assert not server.state_file().exists() and server.running_url() is None
    assert desktop.load_state()["window"] == {"width": 1000, "height": 700, "x": 50, "y": 60}
    assert any("tray" in s for s in lines)


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
            FakeIcon.made[-1].item("Open Cortex").action()
        seen["calls"] = list(w.calls)
        FakeIcon.made[-1].item("Quit").action()
        w.events.shown.set()
        assert w.destroyed.wait(10)
    gui(start)
    desktop.run(vault, hidden=True, echo=lambda s: None)
    assert "#tonight" not in seen["first"]
    (load, url), *rest = seen["calls"]
    assert load == "load" and url.endswith("#tonight") and rest == ["show", "show"]


def test_close_without_a_tray_or_while_windows_shuts_down_really_closes(monkeypatch):
    d = desktop.Desktop(types.SimpleNamespace(), "http://x")
    d.win = FakeWindow()
    monkeypatch.setattr(desktop, "_shutting_down", lambda: False)
    assert d.closing() is True  # no tray to come back from
    d.tray = FakeIcon("cortex", None, "Cortex", FakeMenu())
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
            FakeIcon.made[0].item("Open Cortex").action(),  # the tray opens the browser too
            FakeIcon.made[0].item("Quit").action())).start()
        raise RuntimeError("WebView2 failed")
    gui(start)
    lines = []
    desktop.run(vault, echo=lines.append)
    opened = [c[1] for c in fakes if c[0] == "open"]
    assert len(opened) == 2 and "?t=" in opened[0] and any("window failed" in s for s in lines)
    assert not server.state_file().exists()


def test_start_with_windows_on_off_and_the_first_run_rule(win32, _no_real_run_key):
    assert desktop.autostart_state() == {"supported": True, "enabled": False}
    desktop.first_run()
    assert _no_real_run_key == {desktop.AUTOSTART: win32}
    assert status.config()["app"]["autostart_asked"] is True
    assert desktop.set_autostart(False) == {"supported": True, "enabled": False}
    desktop.first_run()  # asked already: off stays off
    assert _no_real_run_key == {}


def test_start_with_windows_off_before_the_first_run_sticks(win32, _no_real_run_key):
    desktop.set_autostart(False)
    desktop.first_run()
    assert _no_real_run_key == {}


def test_start_with_windows_needs_the_app_exe_and_windows(win32, tmp_path, monkeypatch,
                                                          _no_real_run_key):
    (tmp_path / "bin" / "cortex-app.exe").unlink()
    lines = []
    desktop.first_run(lines.append)
    assert _no_real_run_key == {} and "cortex-app.exe" in lines[0]
    assert status.config().get("app", {}).get("autostart_asked") is None  # tried again later
    monkeypatch.setattr(sys, "platform", "linux")
    assert desktop.autostart_state() == {"supported": False, "enabled": False}
    with pytest.raises(ValueError):
        desktop.set_autostart(True)


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
    assert call(app, "GET", "/api/app/autostart")[1] == {"supported": True, "enabled": False}
    assert call(app, "POST", "/api/app/autostart", {"enabled": True})[1]["enabled"] is True
    assert _no_real_run_key == {desktop.AUTOSTART: win32}
    assert call(app, "POST", "/api/app/autostart", {"enabled": "yes"})[0] == 400
    assert call(app, "GET", "/api/app/notify")[1] == {"reviews": True, "exams": True,
                                                       "jobs": True, "updates": True}
    assert call(app, "POST", "/api/app/notify", {"jobs": False})[1]["jobs"] is False
    assert desktop.notify_settings() == {"reviews": True, "exams": True, "jobs": False,
                                         "updates": True}
    for bad in ({}, {"bogus": True}, {"jobs": "no"}):
        assert call(app, "POST", "/api/app/notify", bad)[0] == 400
    assert call(app, "POST", "/api/window/show", {})[1] == {"shown": False}  # browser app
    assert call(app, "POST", "/api/window/show", {}, token=False)[0] == 401
    d = desktop.Desktop(app, "http://x")
    d.win, d.tray = FakeWindow(), FakeIcon("cortex", None, "Cortex", FakeMenu())
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
    from cortex import planner
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
    jobs.on_end = n.job_ended

    def wait():
        _until(lambda: not jobs.running())
    jobs.start("Backing up the vault", "vault", lambda job: {"ok": True})
    wait()
    jobs.start("Running the doctor", "vault", lambda job: {"ok": False})
    wait()
    jobs.start("Importing", "setup", lambda job: 1 / 0)
    wait()

    def cancelled(job):
        job.cancel()
        return {"cancelled": True}
    jobs.start("Downloading", "setup", cancelled)
    wait()
    _until(lambda: len(sent) >= 3)
    time.sleep(0.05)
    assert sent == ["Backing up the vault finished", "Running the doctor failed",
                    "Importing failed"]
    desktop.save_notify({"jobs": False})
    jobs.start("Backing up the vault", "vault", lambda job: {"ok": True})
    wait()
    time.sleep(0.05)
    assert len(sent) == 3


def test_the_tray_notifies_and_toggles_start_with_windows(win32, _no_real_run_key):
    d = desktop.Desktop(types.SimpleNamespace(), "http://x")
    d.tray = FakeIcon("cortex", desktop._icon_image(), "Cortex", FakeMenu())
    assert d.tray.image.size == (64, 64)
    d.notify("3 reviews due today")
    assert d.tray.notes == ["3 reviews due today"]
    d.toggle_autostart()
    assert _no_real_run_key == {desktop.AUTOSTART: win32} and d.tray.updates == 1
    d.toggle_autostart()
    assert _no_real_run_key == {}


def test_desktop_json_is_written_whole(home):
    desktop.save_state("window", {"width": 1})
    desktop.save_state("notified", {"a": "2026-11-19"})
    assert json.loads(desktop.state_file().read_text()) == {
        "window": {"width": 1}, "notified": {"a": "2026-11-19"}}
    assert [p.name for p in desktop.state_file().parent.iterdir()
            if p.name.endswith(".tmp")] == []


def test_the_mark_is_crisp_when_small_and_the_ico_is_drawn_from_it():
    """The logo: a dark rounded square with an amber dot-matrix C. Up to 32 px each lit cell is
    whole pixels of exactly AMBER on GROUND; the committed cortex.ico is `mark` at every size
    (scripts/make_icon.py), and the tray icon is the same mark."""
    from PIL import Image

    amber, ground = (255, 176, 0, 255), (11, 11, 10, 255)
    lit = sum(line.count("#") for line in desktop.LETTER_C)
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


def test_start_with_windows_runs_cortex_exe_when_installed(win32, tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "Cortex.exe").write_bytes(b"MZ")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))  # the tool venv
    want = f'"{tmp_path / "app" / "Cortex.exe"}" -m cortex.app.launch --hidden'
    assert desktop.autostart_command() == want
