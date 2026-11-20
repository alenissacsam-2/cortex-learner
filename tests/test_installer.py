"""installer/setup_app.py (CortexSetup.exe) logic, with uv, the registry and shortcuts mocked,
and the `cortex-app` launcher."""

import importlib.util
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "installer"))  # setup_app imports app_exe from beside it
_spec = importlib.util.spec_from_file_location("setup_app", ROOT / "installer" / "setup_app.py")
setup_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup_app)
app_exe = setup_app.app_exe
REAL_MAKE_SHORTCUTS = setup_app.make_shortcuts

BIN = r"C:\Users\x\AppData\Local\Programs\Cortex\bin"


@pytest.fixture(autouse=True)
def _no_real_system(monkeypatch):
    """Nothing here may touch this computer's registry, PATH, shortcuts, scheduled tasks or
    processes; a test that needs one of them fakes it (the `calls` fixture records them)."""
    def refuse(*a, **k):
        pytest.fail("a test touched the real system")
    for name in ("set_user_path", "register", "unregister", "make_shortcuts", "drop_login",
                 "terminate", "remove_later", "start_app"):
        monkeypatch.setattr(setup_app, name, refuse)


def test_merge_path_appends_once():
    assert setup_app.merge_path("", BIN) == BIN
    assert setup_app.merge_path(r"C:\a;C:\b", BIN) == rf"C:\a;C:\b;{BIN}"
    assert setup_app.merge_path(r"C:\a;", BIN) == rf"C:\a;{BIN}"
    for present in (BIN, BIN.upper() + "\\", f'"{BIN}"'):
        current = rf"%USERPROFILE%\bin;{present}"
        assert setup_app.merge_path(current, BIN) == current


def test_drop_path_keeps_the_rest():
    assert setup_app.drop_path(rf"C:\a;{BIN}\;%X%\y;{BIN.lower()}", BIN) == r"C:\a;%X%\y"
    assert setup_app.drop_path(BIN, BIN) == ""


def test_uv_env_keeps_everything_in_the_folder(tmp_path):
    env = setup_app.uv_env(tmp_path, {"PATH": "p", "UV_CACHE_DIR": "elsewhere"})
    assert env["PATH"] == "p"
    for var, sub in (("UV_TOOL_DIR", "tools"), ("UV_TOOL_BIN_DIR", "bin"),
                     ("UV_PYTHON_INSTALL_DIR", "python"), ("UV_CACHE_DIR", "cache")):
        assert env[var] == str(tmp_path / sub)
    assert env["UV_PYTHON_PREFERENCE"] == "only-managed"


def test_parse_args(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    a = setup_app.parse_args([])
    assert a.dir == (tmp_path / "Programs" / "Cortex").resolve()
    assert not (a.quiet or a.uninstall or a.no_path or a.no_shortcuts or a.no_registry)
    a = setup_app.parse_args(["--quiet", "--uninstall", "--dir", str(tmp_path / "c"),
                              "--no-path", "--no-shortcuts", "--no-registry"])
    assert a.quiet and a.uninstall and a.no_path and a.no_shortcuts and a.no_registry
    assert a.dir == (tmp_path / "c").resolve()


def test_uninstall_values_point_at_the_copied_setup(tmp_path):
    v = setup_app.uninstall_values(tmp_path, "0.1.0")
    assert v["DisplayVersion"] == "0.1.0" and v["InstallLocation"] == str(tmp_path)
    exe = tmp_path / "CortexSetup.exe"
    assert v["UninstallString"] == f'"{exe}" --uninstall --dir "{tmp_path}"'
    assert v["DisplayIcon"] == str(tmp_path / "tools" / "cortex-learner" / "app" / "Cortex.exe")
    assert setup_app.version_of(Path("cortex_learner-1.2.3-py3-none-any.whl")) == "1.2.3"


@pytest.fixture
def payload(tmp_path):
    p = tmp_path / "payload"
    p.mkdir()
    (p / "uv.exe").write_bytes(b"uv")
    (p / "cortex_learner-0.2.0-py3-none-any.whl").write_bytes(b"whl")
    return p


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(setup_app, "run", lambda args, env, say: seen.append(("run", args, env)))
    monkeypatch.setattr(setup_app, "get_user_path", lambda: r"C:\a")
    monkeypatch.setattr(setup_app, "set_user_path", lambda v: seen.append(("path", v)))
    monkeypatch.setattr(setup_app, "make_shortcuts",
                        lambda d, say, existing_only=False: seen.append(("lnk", existing_only)))
    monkeypatch.setattr(app_exe, "make", lambda d, version, say: seen.append(("exe", version)))
    monkeypatch.setattr(setup_app, "register", lambda d, v: seen.append(("reg", v)))
    monkeypatch.setattr(setup_app, "unregister", lambda: seen.append(("unreg",)))
    monkeypatch.setattr(setup_app, "drop_login", lambda: seen.append(("login",)))
    monkeypatch.setattr(setup_app, "shortcut_paths", lambda: [])
    monkeypatch.setattr(setup_app, "in_use", lambda d, procs=None: [])
    return seen


def test_install_runs_uv_inside_the_folder(tmp_path, payload, calls):
    d = tmp_path / "Cortex"
    (d / "cache").mkdir(parents=True)
    (d / setup_app.MARKER).write_text("x")  # an earlier install: an upgrade
    (d / "cortex_learner-0.1.0-py3-none-any.whl").write_bytes(b"old")
    opts = setup_app.parse_args(["--dir", str(d)])
    setup_app.install(d, opts, lambda s: None, src=payload)
    (_, args, env), *rest = calls
    assert [str(a) for a in args] == [str(d / "uv.exe"), "tool", "install", "--force",
                                      "--reinstall-package", "cortex-learner", "--python", "3.12",
                                      str(d / "cortex_learner-0.2.0-py3-none-any.whl")]
    assert env["UV_TOOL_BIN_DIR"] == str(d / "bin")
    assert rest == [("exe", "0.2.0"), ("path", rf"C:\a;{d / 'bin'}"), ("lnk", False),
                    ("reg", "0.2.0")]
    assert sorted(p.name for p in d.iterdir()) == [
        "cortex-install.txt", "cortex_learner-0.2.0-py3-none-any.whl", "uv.exe"]


def test_install_flags_skip_path_shortcuts_registry(tmp_path, payload, calls):
    d = tmp_path / "Cortex"
    opts = setup_app.parse_args(["--dir", str(d), "--no-path", "--no-shortcuts",
                                 "--no-registry"])
    setup_app.install(d, opts, lambda s: None, src=payload)
    assert [c[0] for c in calls] == ["run", "exe"]


def test_install_refuses_a_folder_with_other_files(tmp_path, payload, calls):
    d = tmp_path / "Documents"
    d.mkdir()
    (d / "thesis.docx").write_bytes(b"mine")
    with pytest.raises(setup_app.SetupError, match="other files"):
        setup_app.install(d, setup_app.parse_args(["--dir", str(d)]), lambda s: None, payload)
    assert [p.name for p in d.iterdir()] == ["thesis.docx"] and calls == []


def test_uninstall_keeps_user_data(tmp_path, calls):
    home = Path.home()  # conftest's temp home
    (home / ".cortex").mkdir()
    (home / ".cortex" / "config.yaml").write_text("vault: v\n")
    vault = home / "LearnerVault"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes" / "a.md").write_text("mine")
    d = tmp_path / "Cortex"
    (d / "tools" / "cortex-learner" / "Lib").mkdir(parents=True)
    (d / "bin").mkdir()
    (d / "bin" / "cortex.exe").write_bytes(b"x")
    (d / setup_app.MARKER).write_text("x")
    said = []
    later = setup_app.uninstall(d, setup_app.parse_args(["--uninstall", "--dir", str(d)]),
                                said.append)
    assert not d.exists() and later is False
    assert (home / ".cortex" / "config.yaml").read_text() == "vault: v\n"
    assert (vault / "notes" / "a.md").read_text() == "mine"
    # the AI apps first (the installed cortex.exe), then files, then the entries pointing at them
    assert [c[0] for c in calls] == ["run", "path", "login", "unreg"] and "kept" in said[-1]
    assert [str(a) for a in calls[0][1]] == [str(d / "bin" / "cortex.exe"), "disconnect"]


def _install_tree(d: Path) -> None:
    for sub in ("bin", "tools/cortex-learner/Lib", "python/cpython-3.12", "cache"):
        (d / sub).mkdir(parents=True)
    for name in ("uv.exe", "constraints.txt", "CortexSetup.exe",
                 "cortex_learner-0.2.0-py3-none-any.whl", setup_app.MARKER):
        (d / name).write_bytes(b"x")


def test_uninstall_deletes_only_what_setup_made(tmp_path, calls):
    """A3: a vault (or anything else) the student put in the install folder survives."""
    d = tmp_path / "Cortex"
    _install_tree(d)
    (d / "notes").mkdir()
    (d / "notes" / "a.md").write_text("mine")
    (d / ".cortex").mkdir()
    (d / "thesis.docx").write_bytes(b"mine")
    said = []
    opts = setup_app.parse_args(["--uninstall", "--keep-ai-apps", "--dir", str(d)])
    assert setup_app.uninstall(d, opts, said.append) is False
    assert sorted(p.name for p in d.iterdir()) == [".cortex", "notes", "thesis.docx"]
    assert (d / "notes" / "a.md").read_text() == "mine"
    assert any("Cortex did not make" in s for s in said)
    assert [c[0] for c in calls] == ["path", "login", "unreg"]  # --keep-ai-apps: no disconnect


def test_uninstall_deletes_files_first_and_says_how_to_retry(tmp_path, calls, monkeypatch):
    """A7: a locked file stops it before the shortcuts, PATH and Apps & Features entry go."""
    d = tmp_path / "Cortex"
    _install_tree(d)
    real = setup_app.shutil.rmtree

    def locked(p, **k):
        if Path(p).name == "tools":
            raise PermissionError(13, "in use", str(p / "x.dll"))
        real(p, **k)
    monkeypatch.setattr(setup_app.shutil, "rmtree", locked)
    opts = setup_app.parse_args(["--uninstall", "--keep-ai-apps", "--dir", str(d)])
    with pytest.raises(setup_app.SetupError, match=r"x\.dll.*uninstall again from Installed apps"):
        setup_app.uninstall(d, opts, lambda s: None)
    assert calls == [] and (d / setup_app.MARKER).is_file()


def test_uninstall_refuses_while_cortex_runs(tmp_path, calls, monkeypatch):
    d = tmp_path / "Cortex"
    _install_tree(d)
    monkeypatch.setattr(setup_app, "in_use", lambda d, procs=None: [(7, str(d / "bin" / "x"))])
    opts = setup_app.parse_args(["--uninstall", "--keep-ai-apps", "--dir", str(d)])
    with pytest.raises(setup_app.SetupError, match="by 1 program"):
        setup_app.uninstall(d, opts, lambda s: None)
    assert calls == [] and (d / "bin").is_dir()


def test_a_failed_disconnect_is_only_a_warning(tmp_path, calls, monkeypatch):
    d = tmp_path / "Cortex"
    _install_tree(d)
    (d / "bin" / "cortex.exe").write_bytes(b"x")

    def fail(args, env, say):
        raise setup_app.SetupError("cortex.exe stopped with exit code 1.")
    monkeypatch.setattr(setup_app, "run", fail)
    said = []
    setup_app.uninstall(d, setup_app.parse_args(["--uninstall", "--dir", str(d)]), said.append)
    assert not d.exists() and any(s.startswith("warning: some AI apps") for s in said)


def test_uninstall_running_from_the_folder_leaves_the_exe_to_cmd(tmp_path, calls, monkeypatch):
    """A8: the running CortexSetup.exe stays for remove_later, which deletes only it."""
    d = tmp_path / "Cortex"
    _install_tree(d)
    monkeypatch.setattr(setup_app.sys, "executable", str(d / "CortexSetup.exe"))
    opts = setup_app.parse_args(["--uninstall", "--keep-ai-apps", "--dir", str(d)])
    assert setup_app.uninstall(d, opts, lambda s: None) is True
    assert [p.name for p in d.iterdir()] == ["CortexSetup.exe"]
    seen = []
    monkeypatch.setattr(setup_app.subprocess, "Popen", lambda cmd, **k: seen.append(cmd))
    monkeypatch.setattr(setup_app, "remove_later", _real_remove_later)
    setup_app.remove_later(d)
    assert f'del /f /q "{d / "CortexSetup.exe"}"' in seen[0] and f'rmdir "{d}"' in seen[0]
    assert "/s" not in seen[0].split("rmdir", 1)[1]  # never the folder's other files


_real_remove_later = setup_app.remove_later


def test_check_dir(tmp_path):
    d = tmp_path / "c"
    setup_app.check_dir(d)  # missing: fine
    d.mkdir()
    (d / "CortexSetup.exe").write_bytes(b"x")  # what an uninstall left behind: fine
    setup_app.check_dir(d)
    (d / ".cortex").mkdir()  # a vault: never, marker or not
    (d / setup_app.MARKER).write_text("x")
    with pytest.raises(setup_app.SetupError, match="is a Cortex vault"):
        setup_app.check_dir(d)


def test_install_pins_dependencies_and_survives_a_shortcut_failure(tmp_path, payload, calls,
                                                                   monkeypatch):
    """A9: constraints.txt from uv.lock goes to `uv tool install -c`; A18: no shortcut is a
    warning, and Apps & Features still gets its entry."""
    (payload / "constraints.txt").write_text("numpy==2.0.0\n")

    def blocked(d, say, existing_only=False):
        raise OSError("a managed PC blocks the shortcut object")
    monkeypatch.setattr(setup_app, "make_shortcuts", blocked)
    d = tmp_path / "Cortex"
    said = []
    setup_app.install(d, setup_app.parse_args(["--dir", str(d)]), said.append, src=payload)
    args = [str(a) for a in calls[0][1]]
    assert args[args.index("-c") + 1] == str(d / "constraints.txt")
    assert (d / "constraints.txt").read_text() == "numpy==2.0.0\n"
    assert any(s.startswith("warning: no Start menu shortcut") for s in said)
    assert [c[0] for c in calls] == ["run", "exe", "path", "reg"]
    assert said[-1] == "Cortex is installed."
    env = calls[0][2]
    assert env["UV_NATIVE_TLS"] == "1" and env["UV_COMPILE_BYTECODE"] == "1"


def test_uninstall_refuses_a_folder_without_the_marker(tmp_path, calls):
    d = tmp_path / "NotCortex"
    d.mkdir()
    (d / "keep.txt").write_text("x")
    with pytest.raises(setup_app.SetupError, match="nothing was removed"):
        setup_app.uninstall(d, setup_app.parse_args(["--dir", str(d)]), lambda s: None)
    assert (d / "keep.txt").exists() and calls == []


def test_quiet_main_reports_failure_by_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_app, "LOG", tmp_path / "setup.log")
    assert setup_app.main(["--quiet", "--uninstall", "--dir", str(tmp_path / "none")]) == 1
    assert "nothing was removed" in (tmp_path / "setup.log").read_text()


def test_parse_args_update_and_wait_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_app, "installed_dir", lambda: tmp_path / "reg")
    a = setup_app.parse_args(["--update", "--wait-pid", "4242"])
    assert a.update and a.wait_pid == 4242 and a.dir == (tmp_path / "reg").resolve()
    # the version in Apps & Features; the shortcuts still there are rewritten (make_shortcuts)
    assert a.no_path and not a.no_shortcuts and not a.no_registry
    a = setup_app.parse_args(["--update", "--dir", str(tmp_path / "x")])
    assert a.dir == (tmp_path / "x").resolve() and a.wait_pid is None
    assert setup_app.parse_args([]).wait_pid is None
    with pytest.raises(SystemExit):
        setup_app.parse_args(["--wait-pid", "app"])


def test_in_use_lists_only_programs_inside_the_folder(tmp_path):
    d = tmp_path / "Cortex"
    procs = [(101, str(d / "bin" / "cortex.exe")),
             (102, str(d / "TOOLS" / "cortex-learner" / "Scripts" / "python.exe").upper()),
             (103, str(tmp_path / "Cortex2" / "bin" / "cortex.exe")),  # a sibling, not inside
             (104, str(tmp_path / "Cortex.exe")),
             (105, r"C:\Windows\explorer.exe"),
             (os.getpid(), str(d / "CortexSetup.exe")),  # this setup and its one-file parent
             (os.getppid(), str(d / "CortexSetup.exe"))]
    got = setup_app.in_use(d, procs)
    assert [pid for pid, _ in got] == ([101, 102] if os.name == "nt" else [101])
    assert setup_app.in_use(d, []) == []
    assert setup_app.in_use_text(got[:1]).startswith("Cortex is in use by 1 program (")


def test_install_refuses_while_cortex_runs_and_kills_nothing(tmp_path, payload, calls,
                                                             monkeypatch):
    d = tmp_path / "Cortex"
    d.mkdir()
    (d / setup_app.MARKER).write_text("x")
    busy = [(7, str(d / "bin" / "cortex.exe"))]
    monkeypatch.setattr(setup_app, "in_use", lambda d, procs=None: busy)
    monkeypatch.setattr(setup_app, "terminate", lambda pids: pytest.fail("killed without asking"))
    opts = setup_app.parse_args(["--quiet", "--update", "--dir", str(d)])
    with pytest.raises(setup_app.SetupError, match=r"by 1 program .*cortex\.exe \(process 7\)"):
        setup_app.install(d, opts, lambda s: None, src=payload)
    assert calls == []


def test_update_needs_an_install_and_keeps_shortcuts_and_path(tmp_path, payload, calls):
    d = tmp_path / "Cortex"
    opts = setup_app.parse_args(["--update", "--dir", str(d)])
    with pytest.raises(setup_app.SetupError, match="not installed"):
        setup_app.install(d, opts, lambda s: None, src=payload)
    d.mkdir()
    (d / setup_app.MARKER).write_text("x")
    said = []
    setup_app.install(d, opts, said.append, src=payload)
    # the shortcuts still there are rewritten for Cortex.exe (older ones ran cortex-app.exe)
    assert [c[0] for c in calls] == ["run", "exe", "lnk", "reg"] and calls[2] == ("lnk", True)
    assert said[-1] == ("Cortex is updated. AI apps connected to Cortex reconnect when you "
                        "restart them.")


def test_quiet_update_waits_for_the_app_then_reopens_it(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(setup_app, "LOG", tmp_path / "setup.log")
    monkeypatch.setattr(setup_app, "wait_pid", lambda pid: seen.append(("wait", pid)))
    monkeypatch.setattr(setup_app, "in_use", lambda d, procs=None: [])
    monkeypatch.setattr(setup_app, "install", lambda d, o, say: seen.append(("install", o.update)))
    monkeypatch.setattr(setup_app, "start_app", lambda d: seen.append(("open", d)))
    d = tmp_path / "Cortex"
    assert setup_app.main(["--quiet", "--update", "--wait-pid", "9", "--dir", str(d)]) == 0
    assert seen == [("wait", 9), ("install", True), ("open", d.resolve())]


@pytest.mark.slow
def test_the_wheel_ships_the_extension_and_the_receiver(tmp_path):
    """A2: an installer install (the wheel) has the browser extension as package data; A10:
    and the no-console receiver entry point."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    subprocess.run([uv, "build", "--wheel", "-o", str(tmp_path)], cwd=ROOT, check=True,
                   capture_output=True)
    z = zipfile.ZipFile(next(tmp_path.glob("cortex_learner-*.whl")))
    names = set(z.namelist())
    for f in (ROOT / "extension").iterdir():
        assert f"cortex/assets/extension/{f.name}" in names, f.name
    ep = next(n for n in names if n.endswith(".dist-info/entry_points.txt"))
    assert "cortex-receiver = cortex.app.launch:receiver" in z.read(ep).decode()


def test_launcher_logs_without_a_console(monkeypatch):
    from cortex.app import desktop, launch, server

    seen = {}
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "argv", [str(Path("C:/inst/bin/cortex-app.exe")), "--hidden"])
    monkeypatch.setenv("PATH", "orig")
    monkeypatch.setenv("CORTEX_VAULT", "V")
    monkeypatch.setattr(server, "running_url", lambda: None)

    def fake_run(root, hidden, echo):
        seen.update(root=root, hidden=hidden)
        echo("Cortex app: http://127.0.0.1:1/")

    monkeypatch.setattr(desktop, "run", fake_run)
    launch.main()
    log = sys.stdout
    log.close()
    assert seen == {"root": Path("V"), "hidden": True}
    assert (Path.home() / ".cortex" / "logs" / "app.log").read_text().startswith("Cortex app:")
    assert os.environ["PATH"].startswith(str(Path("C:/inst/bin").resolve()) + os.pathsep)


def test_launcher_puts_the_install_bin_on_path(tmp_path, monkeypatch):
    """Cortex.exe runs from the tool venv's folder: PATH gets the install's bin
    (`cortex.exe`)."""
    from cortex import update
    from cortex.app import desktop, launch, server

    d = tmp_path / "Cortex"
    exe = app_exe.path(d)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    (d / update.MARKER).write_text("x")
    monkeypatch.setattr(sys, "prefix", str(app_exe.venv(d)))
    monkeypatch.setattr(sys, "executable", str(exe).upper())  # Windows names: any case
    monkeypatch.setattr(sys, "argv", [str(ROOT / "src" / "cortex" / "app" / "launch.py")])
    monkeypatch.setenv("PATH", "orig")
    monkeypatch.setattr(server, "running_url", lambda: None)
    monkeypatch.setattr(desktop, "run", lambda root, hidden, echo: None)
    launch.main()
    assert os.environ["PATH"] == str((d / "bin").resolve()) + os.pathsep + "orig"


def test_launcher_hands_over_to_cortex_exe(tmp_path, monkeypatch):
    """An older shortcut or Start with Windows value runs bin\\cortex-app.exe: it starts the
    venv's Cortex.exe with the same arguments, so the window belongs to Cortex.exe."""
    from cortex.app import desktop, launch

    venv = tmp_path / "cortex-learner"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "app").mkdir()
    (venv / "app" / "Cortex.exe").write_bytes(b"MZ")
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "executable", str(venv / "Scripts" / "pythonw.exe"))
    monkeypatch.setattr(sys, "argv", ["cortex-app.exe", "--hidden"])
    started = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda args, **kw: started.append(args))
    monkeypatch.setattr(desktop, "run", lambda *a, **k: pytest.fail("ran in the wrong process"))
    launch.main()
    assert started == [[str(venv / "app" / "Cortex.exe"), "-m", "cortex.app.launch", "--hidden"]]


# ---- Cortex.exe (installer/app_exe.py) -------------------------------------------------------

ICO = ROOT / "src" / "cortex" / "assets" / "cortex.ico"
windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows executables")


def test_icon_resources_split_the_ico():
    images, group = app_exe.icon_resources(ICO.read_bytes())
    assert len(images) == 7 and all(img.startswith(b"\x89PNG") for img in images)
    assert group[:6] == b"\0\0\1\0\7\0" and len(group) == 6 + 14 * 7
    w, h, _, _, planes, bits, size, rid = app_exe.struct.unpack_from("<BBBBHHIH", group, 6)
    assert (w, h, planes, bits, size, rid) == (16, 16, 1, 32, len(images[0]), 1)
    with pytest.raises(OSError):
        app_exe.icon_resources(b"MZ not an icon")


def test_version_resource_names_cortex():
    v = app_exe.version_resource("0.2.13")
    assert app_exe.struct.unpack_from("<HHH", v) == (len(v), 52, 0)  # VS_FIXEDFILEINFO
    assert app_exe.struct.unpack_from("<4I", v, 40) == (0xFEEF04BD, 0x00010000, 2, 13 << 16)
    text = v.decode("utf-16-le", errors="replace")
    for key, value in (("FileDescription", "Cortex"), ("ProductName", "Cortex"),
                       ("FileVersion", "0.2.13"), ("OriginalFilename", "Cortex.exe")):
        assert f"{key}\0" in text and f"{value}\0" in text


def _fake_tool(d: Path) -> Path:
    """The uv tool venv of an install, made from this test's own base Python."""
    v = app_exe.venv(d)
    (v / "Scripts").mkdir(parents=True)
    (v / "Scripts" / "cortex.exe").write_bytes(b"uv's console script")  # never overwritten
    (v / "pyvenv.cfg").write_text(f"home = {Path(sys.base_prefix)}\nversion_info = 3.12\n")
    assets = app_exe.ico(d).parent
    assets.mkdir(parents=True)
    shutil.copy2(ICO, assets / "cortex.ico")
    return v


@windows
def test_make_copies_and_stamps_a_cortex_exe_that_runs_in_the_venv(tmp_path):
    d = tmp_path / "Cortex"
    v = _fake_tool(d)
    said = []
    exe = app_exe.make(d, "0.2.13", said.append)
    assert exe == v / "app" / "Cortex.exe" and not any("warning" in s for s in said)
    assert (v / "Scripts" / "cortex.exe").read_bytes() == b"uv's console script"
    assert (exe.parent / f"python{sys.version_info[0]}{sys.version_info[1]}.dll").is_file()
    assert app_exe.read_resource(exe, app_exe.RT_VERSION, 1) == app_exe.version_resource("0.2.13")
    assert app_exe.read_resource(exe, app_exe.RT_GROUP_ICON, 1) == \
        app_exe.icon_resources(ICO.read_bytes())[1]
    assert b"longPathAware" in app_exe.read_resource(exe, app_exe.RT_MANIFEST, 1)
    assert app_exe.read_resource(exe, app_exe.RT_ICON, 8) is None  # pythonw's other icons gone
    out = subprocess.run([str(exe), "-c", "import sys; print(sys.prefix)"], capture_output=True,
                         text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert Path(out.stdout.strip()) == v  # the venv, from ..\pyvenv.cfg


@windows
def test_a_failed_stamp_keeps_a_working_exe(tmp_path):
    d = tmp_path / "Cortex"
    _fake_tool(d)
    app_exe.ico(d).write_bytes(b"broken")
    said = []
    exe = app_exe.make(d, "0.2.13", said.append)
    assert exe.is_file() and any("keeps Python's icon" in s for s in said)


@windows
def test_shortcut_carries_the_app_id_and_icon(tmp_path):
    lnk, target = tmp_path / "Cortex.lnk", tmp_path / "Cortex.exe"
    target.write_bytes(b"MZ")
    app_exe.shortcut(lnk, target, "-m cortex.app.launch", target)
    ps = ("$l = (New-Object -ComObject WScript.Shell).CreateShortcut($env:L); "
          "$l.TargetPath; $l.Arguments; $l.IconLocation; "
          "(New-Object -ComObject Shell.Application).NameSpace($env:D).ParseName('Cortex.lnk')"
          ".ExtendedProperty('System.AppUserModel.ID')")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                         capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                         env={**os.environ, "L": str(lnk), "D": str(tmp_path)})
    assert out.stdout.splitlines() == [str(target), "-m cortex.app.launch", f"{target},0",
                                       app_exe.APP_ID]


def test_make_shortcuts_on_update_rewrites_only_those_still_there(tmp_path, monkeypatch):
    menu, desk = tmp_path / "Programs" / "Cortex.lnk", tmp_path / "Desktop" / "Cortex.lnk"
    menu.parent.mkdir()
    menu.write_bytes(b"old")
    monkeypatch.setattr(setup_app, "shortcut_paths", lambda: [menu, desk])
    made = []
    monkeypatch.setattr(app_exe, "shortcut", lambda lnk, exe, args, icon: made.append(
        (lnk, exe, args, icon)))
    d = tmp_path / "Cortex"
    REAL_MAKE_SHORTCUTS(d, lambda s: None, existing_only=True)
    exe = app_exe.path(d)
    assert made == [(menu, exe, "-m cortex.app.launch", exe)]
    REAL_MAKE_SHORTCUTS(d, lambda s: None)
    assert [m[0] for m in made] == [menu, menu, desk]
