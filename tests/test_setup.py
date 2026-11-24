"""`cortex setup` (idempotent wizard) and the packaged sample vault."""

import json
import shutil
from datetime import UTC, datetime

import pytest
from test_cli import run  # the CliRunner helper, not a fixture

from cortex import clients, dashboard, levels, packs, sample
from cortex.app import targets
from cortex.catalog import Catalog
from cortex.events import EventLog, local_day
from cortex.lint import lint_report
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")  # isolates HOME, APPDATA and LOCALAPPDATA

FLAGS = ["--yes", "--no-model", "--no-desktop", "--no-code"]


def test_sample_files_are_marked_and_synthetic():
    rels = sample.files()
    assert 12 <= len(rels) <= 16
    for rel in rels:
        text = (sample.SAMPLE_DIR / rel).read_text(encoding="utf-8")
        assert "\nsample: true\n" in text.split("\n---", 2)[0] + "\n", rel


def test_setup_twice_seeds_sample_and_is_idempotent(tmp_path, home):
    root = tmp_path / "vault"
    first = run("setup", "--vault", root, *FLAGS).output
    assert "created  sample concepts/loops.md" in first
    assert "created  git init" in first and "Checks:" in first and "Privacy:" in first
    assert "PASS  Vault git" in first and "FAIL" not in first
    assert str(root.resolve()) in (home / ".cortex/config.yaml").read_text(encoding="utf-8")
    v = Vault(root)
    assert "Sam" in (root / "me/profile.md").read_text(encoding="utf-8")  # sample beat template
    assert (root / "me/teaching-contract.md").is_file()  # init still adds the rest
    snap = {p: p.read_bytes() for p in root.rglob("*.md")}
    n_events = len(list(EventLog(v).iter_events()))
    assert n_events == len(sample.SEED)

    assert "  sample " not in run("setup", "--vault", root, *FLAGS).output  # not new: no sample
    second = run("setup", "--vault", root, "--sample", *FLAGS).output
    assert "created  sample" not in second and "created  git init" not in second
    assert f"skipped  sample {len(sample.SEED)} events (exists)" in second
    assert {p: p.read_bytes() for p in root.rglob("*.md")} == snap
    assert len(list(EventLog(v).iter_events())) == n_events

    # the sample is a working vault: catalog, levels, lint, dashboard
    cat = Catalog.load(v)
    assert {"loops", "functions", "recursion", "big-o-notation"} <= set(cat.entries)
    states, _ = levels.fold(EventLog(v).iter_events(), local_day(datetime.now(UTC)))
    assert {"loops", "functions", "recursion"} <= set(states)
    assert lint_report(v)[0] == []
    assert "default-src 'none'" in dashboard.snapshot(v, now=datetime.now(UTC))


def test_sample_remove(tmp_path, home):
    root = tmp_path / "vault"
    run("setup", "--vault", root, *FLAGS)
    v = Vault(root)
    mine = root / "concepts" / "mine.md"
    mine.write_text("---\nid: mine\ntitle: Mine\n---\n# Mine\n", encoding="utf-8")
    out = run("sample", "remove", "--vault", root).output
    assert "removed  concepts/loops.md" in out and "created  me/profile.md" in out
    assert f"voided   {len(sample.SEED)} sample events" in out
    left = [p for p in root.rglob("*.md") if ".git" not in p.parts
            and "sample: true" in p.read_text(encoding="utf-8")]
    assert left == [] and mine.is_file()
    assert "Sam" not in (root / "me/profile.md").read_text(encoding="utf-8")
    assert not {"loops", "programming", "programming.basics"} & set(Catalog.load(v).entries)
    events = list(EventLog(v).iter_events())
    voided = {e["ref"] for e in events if e["kind"] == "void"}
    assert {e["id"] for e in events if e["key"].startswith("sample:")} == voided
    again = run("sample", "remove", "--vault", root).output  # nothing left to do
    assert "removed" not in again and "voided   0 sample events" in again
    run("catalog", "sync", "--vault", root)  # sync must not bring the sample back
    assert not (root / "concepts" / "loops.md").exists()


def test_missing_git_warns(tmp_path, home, monkeypatch):
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda cmd, *a, **k: None if cmd == "git"
                        else real(cmd, *a, **k))
    root = tmp_path / "vault"
    out = run("setup", "--vault", root, "--no-sample", *FLAGS).output
    assert "git not found" in out and "created  git init" not in out
    assert not (root / ".git").exists() and (root / "me" / "profile.md").is_file()


def test_dry_run_touches_nothing(tmp_path, home):
    cfg = home / "APPDATA" / "Claude" / clients.CONFIG
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    root = tmp_path / "vault"
    out = run("setup", "--dry-run", "--yes", "--no-model", "--vault", root).output
    assert "dry run: nothing written" in out and "plan     sample: 15 of 15" in out
    if clients.claude_desktop_config_paths():  # the Windows layout above
        assert f"{cfg}: updated" in out
    assert not root.exists() and not (home / ".cortex").exists()
    assert json.loads(cfg.read_text(encoding="utf-8")) == {"mcpServers": {}}


def test_prompted_path_quotes_and_refused_folder(tmp_path, home):
    from test_cli import app, runner

    root = tmp_path / "my vault"
    r = runner.invoke(app, ["setup", "--dry-run", "--no-model", "--no-desktop", "--no-code",
                            "--no-sample"], input=f'"{root}"\n')
    assert r.exit_code == 0 and f"plan     vault {root.resolve()} (new)" in r.output
    root.mkdir()
    (root / "notes.txt").write_text("mine", encoding="utf-8")
    r = runner.invoke(app, ["setup", "--vault", str(root), "--no-model", "--no-desktop",
                            "--no-code"], input="n\n")
    assert r.exit_code == 1 and "Nothing changed" in r.output
    assert sorted(p.name for p in root.iterdir()) == ["notes.txt"]


def test_setup_writes_desktop_config(tmp_path, home, monkeypatch):
    monkeypatch.setattr(clients, "claude_desktop_config_paths",
                        lambda platform=None: [home / "claude" / clients.CONFIG])
    cfg = home / "claude" / clients.CONFIG
    cfg.parent.mkdir()
    cfg.write_text('{"theme": "dark"}', encoding="utf-8")
    old_bak = cfg.with_name(cfg.name + ".bak")  # the user's own: setup never overwrites it
    old_bak.write_text("mine", encoding="utf-8")
    root = tmp_path / "vault"
    out = run("setup", "--vault", root, "--yes", "--no-model", "--no-code", "--no-sample").output
    got = json.loads(cfg.read_text(encoding="utf-8"))
    assert got["mcpServers"]["cortex"]["args"] == ["mcp", "--vault", str(root.resolve())]
    assert got["theme"] == "dark" and "Fully quit Claude desktop" in out
    assert targets.PASTE_DESKTOP in out and packs.USE_TEXT in out  # CN: what to paste, where
    assert f"updated  {cfg}" in out and ".cortex-" in out
    assert old_bak.read_text(encoding="utf-8") == "mine"
    baks = list(cfg.parent.glob(cfg.name + ".cortex-*.bak"))  # the app's timestamped backup
    assert [json.loads(b.read_text(encoding="utf-8")) for b in baks] == [{"theme": "dark"}]
    out = run("setup", "--vault", root, "--yes", "--no-model", "--no-code").output
    assert "cortex entry exists" in out
    assert len(list(cfg.parent.glob("*.bak"))) == 2


def test_code_step_prints_commands_without_claude(tmp_path, home, monkeypatch):
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda cmd, *a, **k: None if cmd == "claude"
                        else real(cmd, *a, **k))
    out = run("setup", "--vault", tmp_path / "v", "--yes", "--no-model", "--no-desktop",
              "--no-sample").output
    assert "/plugin marketplace add" in out and "/plugin install cortex@cortex" in out


def test_setup_points_at_the_checkout_never_a_placeholder(tmp_path, home, monkeypatch):
    """Audit F10: the local checkout's marketplace, or a clear note; never `<owner>/<repo>`."""
    from cortex import cli

    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda cmd, *a, **k: None if cmd == "claude"
                        else real(cmd, *a, **k))
    monkeypatch.setattr(clients, "claude_desktop_config_paths",
                        lambda platform=None: [home / "claude" / clients.CONFIG])
    checkout = tmp_path / "checkout"
    (checkout / ".claude-plugin").mkdir(parents=True)
    (checkout / ".claude-plugin" / "marketplace.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "CHECKOUT", checkout)
    args = ("setup", "--vault", tmp_path / "v", "--yes", "--no-model", "--no-sample")
    out = run(*args).output
    assert f"/plugin marketplace add {checkout}" in out and str(checkout / "dist") in out
    assert f"marketplace add {checkout}" in run(*args, "--dry-run").output
    monkeypatch.setattr(cli, "CHECKOUT", tmp_path / "nowhere")  # e.g. a non-editable install
    out = run(*args).output + run(*args, "--dry-run").output
    assert f"marketplace add {cli.REPO}" in out  # the published repo, once there is one
    assert "<owner>" not in out
    # CN: Claude Code clones the published repo with git; without git, say so first
    from cortex import session
    monkeypatch.setattr(session, "git_exe", lambda: None)
    out = run(*args).output
    assert "Claude Code needs Git" in out and "marketplace add" not in out


def test_setup_ends_by_offering_the_app(tmp_path, home, monkeypatch):
    """N1: an interactive setup offers `cortex app`; --yes never opens it."""
    from test_cli import app, runner

    from cortex import cli

    opened = []
    monkeypatch.setattr(cli, "app_cmd", lambda vault=None, **k: opened.append(vault))
    root = tmp_path / "vault"
    args = ["setup", "--vault", str(root), "--no-model", "--no-desktop", "--no-code",
            "--no-sample"]
    r = runner.invoke(app, args, input="y\n")
    assert r.exit_code == 0 and "Open the Cortex app" in r.output
    assert opened == [root.resolve()]
    assert runner.invoke(app, args, input="n\n").exit_code == 0 and len(opened) == 1
    run(*args, "--yes")
    assert len(opened) == 1
