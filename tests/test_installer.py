"""installer/setup_app.py (CortexSetup.exe) logic, with uv, the registry and shortcuts mocked,
and the `cortex-app` launcher."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("setup_app", ROOT / "installer" / "setup_app.py")
setup_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup_app)

BIN = r"C:\Users\x\AppData\Local\Programs\Cortex\bin"


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
    monkeypatch.setattr(setup_app, "shortcut_paths", lambda: [])
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
    setup_app.uninstall(d, setup_app.parse_args(["--uninstall", "--dir", str(d)]), said.append)
    assert not d.exists()
    assert (home / ".cortex" / "config.yaml").read_text() == "vault: v\n"
    assert (vault / "notes" / "a.md").read_text() == "mine"
    assert [c[0] for c in calls] == ["path", "unreg"] and "kept" in said[-1]


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


def test_launcher_logs_without_a_console(monkeypatch):
    from cortex.app import launch, server

    seen = {}
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "argv", [str(Path("C:/inst/bin/cortex-app.exe"))])
    monkeypatch.setenv("PATH", "orig")
    monkeypatch.setenv("CORTEX_VAULT", "V")

    def fake_run(root, echo):
        seen["root"] = root
        echo("Cortex app: http://127.0.0.1:1/")

    monkeypatch.setattr(server, "run", fake_run)
    launch.main()
    log = sys.stdout
    log.close()
    assert seen["root"] == Path("V")
    assert (Path.home() / ".cortex" / "logs" / "app.log").read_text().startswith("Cortex app:")
    assert os.environ["PATH"].startswith(str(Path("C:/inst/bin").resolve()) + os.pathsep)
