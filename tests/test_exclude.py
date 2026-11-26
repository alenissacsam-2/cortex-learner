"""The `exclude:` list in ~/.keepfold/config.yaml: no priming and no Keepfold tools there."""

import json
import sys

import pytest
from conftest import events
from test_prime import payload, vault  # noqa: F401
from typer.testing import CliRunner

from keepfold import mcp_server, prime_hook, taint
from keepfold.cli import app
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    w = tmp_path / "Work" / "client"
    (w / "src").mkdir(parents=True)
    (tmp_path / "Work" / "client-repo").mkdir()
    return w


def cli(*args):
    r = CliRunner().invoke(app, list(args))
    assert r.exit_code == 0, r.output
    return r.output


def test_excluded_matches_whole_components(work, home):
    (home / ".keepfold").mkdir()
    (home / ".keepfold" / "config.yaml").write_text(
        f"vault: V\nexclude:\n  - '{work}'\n", encoding="utf-8")
    assert taint.excluded(work) == str(work.resolve())
    assert taint.excluded(work / "src") == str(work.resolve())
    assert taint.excluded(work.parent / "client-repo") is None  # not a string prefix
    assert taint.excluded(work.parent, None) is None
    if sys.platform == "win32":
        assert taint.excluded(str(work / "src").upper()) is not None
    assert taint._vault_root().name == "V"


def test_cli_add_list_remove_keeps_other_keys(work, home):
    (home / ".keepfold").mkdir()
    cfg = home / ".keepfold" / "config.yaml"
    cfg.write_text("vault: V\nrerank: false\n", encoding="utf-8")
    cli("exclude", "add", str(work))
    assert "already excluded" in cli("exclude", "add", str(work / "src"))
    assert cli("exclude", "list").splitlines() == [str(work.resolve())]
    assert taint.config()["vault"] == "V" and taint.config()["rerank"] is False
    doctor = CliRunner().invoke(app, ["doctor", "--vault", str(work)]).output
    assert f"Excluded folders: {work.resolve()}" in doctor
    cli("exclude", "remove", str(work))
    assert cli("exclude", "list") == "" and taint.config()["vault"] == "V"
    r = CliRunner().invoke(app, ["exclude", "remove", str(work)])
    assert r.exit_code == 1 and "not in the exclude list" in r.output


def test_me_commands_refuse_in_an_excluded_folder(root, work, monkeypatch):
    """review r4 L7: commands that print me/ text refuse there, like `export context`."""
    cli("exclude", "add", str(work))
    monkeypatch.chdir(work / "src")
    for cmd in (["know"], ["saved"], ["claims"], ["brief"], ["export", "context"]):
        r = CliRunner().invoke(app, [*cmd, "--vault", str(root)])
        assert r.exit_code == 1 and "is excluded; refusing" in r.output, cmd
    monkeypatch.chdir(work.parent / "client-repo")
    for cmd in (["know"], ["saved"], ["claims"], ["brief"]):
        assert CliRunner().invoke(app, [*cmd, "--vault", str(root)]).exit_code == 0, cmd


def test_init_keeps_the_exclude_list(work, home, tmp_path):
    cli("exclude", "add", str(work))
    cli("init", str(tmp_path / "v2"))
    assert taint.exclude_list() == [str(work.resolve())]
    assert taint.config()["vault"] == str(tmp_path / "v2")


def test_prime_hook_prints_nothing_in_an_excluded_folder(vault, work, monkeypatch):  # noqa: F811
    monkeypatch.setenv("KEEPFOLD_VAULT", str(vault))
    cli("exclude", "add", str(work))
    inside = json.dumps({**json.loads(payload()), "cwd": str(work / "src")})
    assert prime_hook.run(inside) == ""
    assert not (vault / prime_hook.STATE).exists()  # not even marked as primed
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(work))  # the project dir counts too
    assert prime_hook.run(json.dumps({**json.loads(payload()), "cwd": str(vault)})) == ""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    outside = json.dumps({**json.loads(payload()), "cwd": str(work.parent / "client-repo")})
    assert prime_hook.run(outside).startswith('{"hookSpecificOutput"')


def test_mcp_in_an_excluded_folder_is_marked_excluded(root, work, monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp_server, "serve", lambda v, role, excluded, **k: seen.update(
        role=role, excluded=excluded))
    monkeypatch.chdir(work / "src")
    cli("mcp", "--vault", str(root))
    assert seen == {"role": "owner", "excluded": None}
    cli("exclude", "add", str(work))
    cli("mcp", "--vault", str(root))
    assert seen["excluded"] == str(work.resolve())


BROKEN = "vault: V\nexclude:\n  - D:/x\nbrowse:\n\tdeny: hunter2-secret\n"  # a tab: line 5


def test_an_unreadable_config_excludes_every_folder(vault, work, home, monkeypatch):  # noqa: F811
    """D1-audit 1.2: a typo in config.yaml must not reset the exclude list to none. The hook
    primes nothing, `keepfold mcp` refuses every tool, and doctor names the file and line but
    never the line's text."""
    monkeypatch.setenv("KEEPFOLD_VAULT", str(vault))
    (home / ".keepfold").mkdir()
    cfg = home / ".keepfold" / "config.yaml"
    cfg.write_text(BROKEN, encoding="utf-8")
    err = taint.config_error()
    assert err == f"{cfg} cannot be read: ScannerError at line 5"
    for d in (work, work.parent / "client-repo", vault):
        assert taint.excluded(d) == f"{d.resolve()} (every folder is, while {err})"
    assert prime_hook.run(payload()) == ""
    assert not (vault / prime_hook.STATE).exists()
    seen = {}
    monkeypatch.setattr(mcp_server, "serve", lambda v, role, excluded, **k: seen.update(
        excluded=excluded))
    monkeypatch.chdir(work.parent / "client-repo")
    cli("mcp", "--vault", str(vault))
    assert err in seen["excluded"]
    r = CliRunner().invoke(app, ["doctor", "--vault", str(vault)])
    assert r.exit_code == 1 and f"FAIL  Settings file: {err}\n" in r.output
    assert "delete the file to start from the defaults" in r.output
    for args in (["exclude", "list"], ["exclude", "add", str(work)]):
        r2 = CliRunner().invoke(app, args)
        assert r2.exit_code == 1 and err in r2.output, args
    assert "hunter2" not in r.output + r2.output + seen["excluded"]
    assert cfg.read_text(encoding="utf-8") == BROKEN  # nothing rewrote it
    cfg.unlink()  # missing: the defaults again, nothing excluded
    assert taint.excluded(work) is None
    assert prime_hook.run(payload()).startswith('{"hookSpecificOutput"')


def test_excluded_server_refuses_every_tool_and_says_why(root, monkeypatch):
    made, real = {}, mcp_server.build_server

    def build(*a, **k):
        made["server"] = real(*a, **k)
        made["server"].run = lambda transport: None  # no stdio in a test
        return made["server"]

    monkeypatch.setattr(mcp_server, "build_server", build)
    mcp_server.serve(Vault(root), "owner", "D:\\Work\\client")
    server = made["server"]
    core = server.keepfold
    assert core.role == "reader"
    assert server.instructions == mcp_server.EXCLUDED_INSTRUCTIONS
    assert "keepfold_prime" not in server.instructions
    for name, args in (
            ("keepfold_prime", {"prompt": "pointers"}), ("keepfold_recall", {"query": "pointers"}),
            ("keepfold_find", {"query": "pointers"}), ("keepfold_chats", {}),
            ("keepfold_read", {"id": "two-pointers"}),
            ("keepfold_patch", {"id": "me/profile", "op": "append_to_section",
                              "target": "Background", "text": "- x"})):
        out = core._call(name, getattr(core, name), args)
        assert out["error"] == "role_refused" and "D:\\Work\\client" in out["message"], name
        assert "keepfold exclude remove" in out["next"]
    assert events(root) == []
