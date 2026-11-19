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
_spec = importlib.util.spec_from_file_location("setup_app", ROOT / "installer" / "setup_app.py")
setup_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup_app)

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
    monkeypatch.setattr(setup_app, "make_shortcuts", lambda t, say: seen.append(("lnk", t)))
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
    assert rest == [("path", rf"C:\a;{d / 'bin'}"), ("lnk", d / "bin" / "cortex-app.exe"),
                    ("reg", "0.2.0")]
    assert sorted(p.name for p in d.iterdir()) == [
        "cortex-install.txt", "cortex_learner-0.2.0-py3-none-any.whl", "uv.exe"]


def test_install_flags_skip_path_shortcuts_registry(tmp_path, payload, calls):
    d = tmp_path / "Cortex"
    opts = setup_app.parse_args(["--dir", str(d), "--no-path", "--no-shortcuts",
                                 "--no-registry"])
    setup_app.install(d, opts, lambda s: None, src=payload)
    assert [c[0] for c in calls] == ["run"]


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

    def blocked(target, say):
        raise setup_app.SetupError("powershell.exe stopped with exit code 1.")
    monkeypatch.setattr(setup_app, "make_shortcuts", blocked)
    d = tmp_path / "Cortex"
    said = []
    setup_app.install(d, setup_app.parse_args(["--dir", str(d)]), said.append, src=payload)
    args = [str(a) for a in calls[0][1]]
    assert args[args.index("-c") + 1] == str(d / "constraints.txt")
    assert (d / "constraints.txt").read_text() == "numpy==2.0.0\n"
    assert any(s.startswith("warning: no Start menu shortcut") for s in said)
    assert [c[0] for c in calls] == ["run", "path", "reg"] and said[-1] == "Cortex is installed."
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
    assert a.no_shortcuts and a.no_path and not a.no_registry  # the version in Apps & Features
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
    assert [c[0] for c in calls] == ["run", "reg"]
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
