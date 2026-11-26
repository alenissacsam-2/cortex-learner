"""R3: the first-run move from Cortex (keepfold.migrate). Temp homes (conftest's autouse
`_no_real_home`, or a home of the test's own) and temp vaults only."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from keepfold import migrate

OLD_IGNORE = (".cortex/*\n!.cortex/config.yaml\n.obsidian/workspace*.json\nbrowse/private/\n"
              "archive/chats/\n.*.tmp\n")  # cli.GITIGNORE as 0.6.1 wrote it
XRAY_ROW = ('{"ts":"2026-11-01T10:00:00Z","tool":"cortex_find","client":"cortex-app",'
            '"served":{},"trimmed":{},"chars":10}\n')
NOTE = ('---\nid: "{id}"\ntitle: "{id}"\ncortex_status: "solid"\ncortex_next: "2026-12-01"\n'
        '---\nBody.\n')


@pytest.fixture
def auto(monkeypatch):
    """Runs as an installed copy does (not a source checkout), without waits."""
    monkeypatch.setattr(migrate, "_dev", False)
    monkeypatch.setattr(migrate, "BACKOFF", 0)
    monkeypatch.setattr(migrate, "PEER_WAIT", 0.3)
    monkeypatch.setattr(migrate, "_SEEN", set())


def git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=check)


def old_home(h: Path, vault: Path | None = None) -> Path:
    old = h / ".cortex"
    (old / "logs").mkdir(parents=True)
    (old / "config.yaml").write_text(f"vault: {json.dumps(str(vault))}\n" if vault else "{}\n",
                                      encoding="utf-8")
    (old / "logs" / "app.log").write_text("started\n", encoding="utf-8")
    return old


def old_vault(root: Path, *, use_git=False, notes=("alpha", "beta")) -> Path:
    old = root / ".cortex"
    (old / "backups").mkdir(parents=True)
    (old / "browse").mkdir()
    (old / "config.yaml").write_text("schema: 1\n", encoding="utf-8")
    (old / "xray.jsonl").write_text(XRAY_ROW, encoding="utf-8")
    (old / "backups" / "cortex-20261101-101010.zip").write_bytes(b"zip")
    (old / "browse" / "store.db").write_bytes(b"private")
    (root / ".gitignore").write_text(OLD_IGNORE, encoding="utf-8")
    (root / "concepts").mkdir()
    for n in notes:
        (root / "concepts" / f"{n}.md").write_text(NOTE.format(id=n), encoding="utf-8")
    (root / "concepts" / "plain.md").write_text('---\nid: "plain"\n---\nNo keys.\n',
                                                encoding="utf-8")
    (root / "views").mkdir()
    (root / "views" / "Due.base").write_text("filters:\n  - cortex_next <= today()\n",
                                              encoding="utf-8")
    if use_git:
        git(root, "init", "-q")
        git(root, "config", "user.name", "Cortex")
        git(root, "config", "user.email", "cortex@localhost")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "cortex: init")
    return root


def tree(p: Path) -> dict:
    return {str(f.relative_to(p)): f.read_bytes() for f in sorted(p.rglob("*")) if f.is_file()}


def moved_ok(root: Path) -> None:
    new = root / ".keepfold"
    assert not (root / ".cortex").exists() and new.is_dir()
    assert (new / "migrated").is_file() and not (new / "migrating").exists()
    assert not (new / "lock").exists()
    assert (new / "browse" / "store.db").read_bytes() == b"private"
    for n in ("alpha", "beta"):
        text = (root / "concepts" / f"{n}.md").read_text(encoding="utf-8")
        assert 'keepfold_status: "solid"\nkeepfold_next: "2026-12-01"' in text
        assert "cortex_" not in text
    assert "keepfold_next" in (root / "views" / "Due.base").read_text(encoding="utf-8")
    assert (new / "backups" / "keepfold-20261101-101010.zip").is_file()
    ring = (new / "xray.jsonl").read_text(encoding="utf-8")
    assert '"tool":"keepfold_find"' in ring and '"client":"keepfold-app"' in ring
    ignore = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ignore[:2] == [".keepfold/*", "!.keepfold/config.yaml"]
    assert ".cortex/*" not in ignore and ".cortex/" in ignore


# ---- home ---------------------------------------------------------------------------------


def test_plain_home_move_then_nothing_twice(auto):
    h = Path.home()
    old_home(h)
    r = migrate.home()
    assert r["state"] == "done"
    assert not (h / ".cortex").exists()
    assert (h / ".keepfold" / "config.yaml").read_text(encoding="utf-8") == "{}\n"
    assert (h / ".keepfold" / "logs" / "app.log").is_file()
    assert (h / ".keepfold" / "migrated").is_file()
    before = tree(h / ".keepfold")
    assert migrate.home()["state"] == "done" and tree(h / ".keepfold") == before


def test_nothing_to_move(auto, tmp_path):
    assert migrate.home()["state"] == "nothing"
    (tmp_path / "v").mkdir()
    assert migrate.vault(tmp_path / "v")["state"] == "nothing"
    r = migrate.everything()
    assert (r["state"], r["exit"], r["vaults"]) == ("nothing", 0, [])
    assert not (Path.home() / ".keepfold").exists()  # nothing created


def test_both_homes_never_merge(auto):
    h = Path.home()
    old_home(h)
    (h / ".keepfold").mkdir()
    (h / ".keepfold" / "config.yaml").write_text("vault: x\n", encoding="utf-8")
    old, new = tree(h / ".cortex"), tree(h / ".keepfold")
    r = migrate.home()
    assert r["state"] == "both" and str(h / ".keepfold") in r["message"]
    assert tree(h / ".cortex") == old and tree(h / ".keepfold") == new


def _locked(monkeypatch, name):
    """os.replace fails with PermissionError for a folder called `name` (an open file in it)."""
    real = os.replace

    def replace(src, dst, *a, **k):
        if Path(src).name == name and Path(src).is_dir():
            raise PermissionError(13, "in use")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "replace", replace)


def test_locked_home_changes_nothing(auto, monkeypatch):
    h = Path.home()
    old_home(h)
    before = tree(h / ".cortex")
    _locked(monkeypatch, ".cortex")
    r = migrate.home()
    assert r["state"] == "blocked" and migrate.CLOSE in r["message"]
    assert tree(h / ".cortex") == before and not (h / ".keepfold").exists()


def test_at_start_blocked_home(auto, monkeypatch, capsys):
    old_home(Path.home())
    _locked(monkeypatch, ".cortex")
    assert migrate.at_start(["keepfold", "taint"]) == 0  # a hook stops quietly
    assert capsys.readouterr().err == ""
    assert migrate.at_start(["keepfold", "find", "x"]) == 2
    assert migrate.CLOSE in capsys.readouterr().err
    assert migrate.at_start(["keepfold", "doctor"]) is None  # doctor reports it
    assert migrate.at_start(["keepfold", "migrate"]) is None  # the command does it itself
    assert not (Path.home() / ".keepfold").exists()


def test_at_start_moves_the_home(auto):
    old_home(Path.home())
    assert migrate.at_start(["keepfold", "find", "x"]) is None
    assert (Path.home() / ".keepfold" / "config.yaml").is_file()


# ---- vault ---------------------------------------------------------------------------------


def test_vault_without_git(auto, tmp_path):
    root = old_vault(tmp_path / "v")
    r = migrate.vault(root)
    assert r["state"] == "done" and r["notes"] == 2 and r["skipped"] == 0
    moved_ok(root)
    assert (root / "concepts" / "plain.md").read_text(encoding="utf-8").endswith("No keys.\n")
    assert migrate.vault(root)["state"] == "done"  # idempotent


def test_vault_with_git(auto, tmp_path):
    root = old_vault(tmp_path / "v", use_git=True)
    assert migrate.vault(root)["state"] == "done"
    moved_ok(root)
    assert git(root, "config", "user.name").stdout.strip() == "Keepfold"
    assert git(root, "config", "user.email").stdout.strip() == "keepfold@localhost"
    for p in (".keepfold/browse/x", ".keepfold/browse/store.db", ".keepfold/xray.jsonl",
              ".keepfold/lock", ".cortex/browse/x", "browse/private/x", "archive/chats/x.md"):
        assert git(root, "check-ignore", "-q", p, check=False).returncode == 0, p
    assert git(root, "check-ignore", "-q", ".keepfold/config.yaml", check=False).returncode == 1
    tracked = git(root, "ls-files").stdout.splitlines()
    assert ".keepfold/config.yaml" in tracked
    assert not [t for t in tracked if t.startswith((".cortex/", ".keepfold/browse"))]
    log = git(root, "log", "--format=%s").stdout.splitlines()
    assert log[:2] == ["keepfold: moved from Cortex", "keepfold: before the move from Cortex"]
    assert git(root, "status", "--porcelain").stdout == ""
    # the key rewrite is its own commit: one revert undoes it
    assert "concepts/alpha.md" in git(root, "show", "--name-only", "--format=", "HEAD").stdout


def test_fresh_old_lock_blocks_then_a_stale_one_is_taken(auto, tmp_path):
    from keepfold.vault import Vault, VaultError
    root = old_vault(tmp_path / "v")
    lock = root / ".cortex" / "lock"
    lock.write_text(f"999999 {time.time():.3f} another-pc")
    before = tree(root)
    r = migrate.vault(root)
    assert r["state"] == "blocked" and migrate.CLOSE in r["message"]
    assert tree(root) == before  # nothing changed, not even .gitignore
    with pytest.raises(VaultError) as e:  # every open waits for it
        Vault(root)
    assert e.value.code == "vault_locked" and migrate.CLOSE in str(e.value)
    t = time.time() - 3600  # the old process died an hour ago
    lock.write_text(f"999999 {t:.3f} another-pc")
    os.utime(lock, (t, t))
    assert migrate.vault(root)["state"] == "done"
    moved_ok(root)


def test_locked_vault_folder(auto, tmp_path, monkeypatch):
    from keepfold.vault import Vault, VaultError
    root = old_vault(tmp_path / "v")
    old = tree(root / ".cortex")
    _locked(monkeypatch, ".cortex")
    r = migrate.vault(root)
    assert r["state"] == "blocked"
    assert tree(root / ".cortex") == old and not (root / ".keepfold").exists()
    # the .keepfold lines went in first and stay: they only ever ignore more
    assert ".keepfold/*" in (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    with pytest.raises(VaultError):
        Vault(root)
    monkeypatch.undo()
    monkeypatch.setattr(migrate, "_dev", False)
    assert Vault(root).root == root.resolve()  # the next open moves it
    moved_ok(root)


def test_crash_midway_through_the_keys_then_resume(auto, tmp_path, monkeypatch):
    root = old_vault(tmp_path / "v", notes=("alpha", "beta", "gamma"))
    real, calls = migrate._key_change, []

    def crash(text):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("power cut")
        return real(text)
    monkeypatch.setattr(migrate, "_key_change", crash)
    r = migrate.vault(root)
    assert r["state"] == "error" and "keepfold migrate" in r["message"]
    new = root / ".keepfold"
    assert (new / "migrating").is_file() and not (new / "migrated").exists()
    assert not (new / "lock").exists()
    texts = [(root / "concepts" / f"{n}.md").read_text(encoding="utf-8")
             for n in ("alpha", "beta", "gamma")]
    assert sum("keepfold_status" in t for t in texts) == 1  # one page done before the crash
    monkeypatch.setattr(migrate, "_key_change", real)
    assert migrate.vault(root)["state"] == "done"
    for n in ("alpha", "beta", "gamma"):
        t = (root / "concepts" / f"{n}.md").read_text(encoding="utf-8")
        assert "cortex_" not in t and t.count("keepfold_status") == 1


def test_key_change_keeps_everything_else():
    text = ('---\r\nid: "a"\r\ncortex_status: "learning"  # mine\r\ntags: [x]\r\n'
            'keepfold_next: "2026-12-02"\r\ncortex_next: "2026-12-01"\r\n---\r\nBody\r\n')
    assert migrate._key_change(text) == (
        '---\r\nid: "a"\r\nkeepfold_status: "learning"  # mine\r\ntags: [x]\r\n'
        'keepfold_next: "2026-12-02"\r\n---\r\nBody\r\n')
    assert migrate._key_change("no front matter cortex_status: x\n") == \
        "no front matter cortex_status: x\n"


def test_both_vault_folders(auto, tmp_path):
    root = old_vault(tmp_path / "v")
    (root / ".keepfold").mkdir()
    old = tree(root / ".cortex")
    r = migrate.vault(root)
    assert r["state"] == "both" and r["notes"] == 2
    assert tree(root / ".cortex") == old  # never merged
    ignore = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".cortex/*" in ignore and ".keepfold/*" in ignore  # both stay ignored
    assert migrate.vault(root)["state"] == "both"


def test_space_and_non_ascii_paths(auto, tmp_path, monkeypatch):
    h = tmp_path / "José Smith"
    h.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    root = old_vault(tmp_path / "My Vault é", use_git=True)
    old_home(h, root)
    r = migrate.everything()
    assert (r["state"], r["exit"]) == ("done", 0)
    assert r["vaults"][0]["path"] == str(root.resolve())
    moved_ok(root)
    assert (h / ".keepfold" / "config.yaml").is_file()


def test_several_vaults_move_when_opened(auto, tmp_path):
    from keepfold.vault import Vault
    a, b = old_vault(tmp_path / "a"), old_vault(tmp_path / "b")
    Vault(a), Vault(b)
    moved_ok(a)
    moved_ok(b)


def test_fast_path_does_no_work_twice(auto, tmp_path, monkeypatch):
    from keepfold.vault import Vault
    root = old_vault(tmp_path / "v")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    runs = []
    real = migrate.vault
    monkeypatch.setattr(migrate, "vault", lambda r: runs.append(r) or real(r))
    Vault(root), Vault(root), Vault(fresh), Vault(fresh)
    assert runs == [root.resolve()]  # once, and never for a vault with nothing to move
    stats = []
    monkeypatch.setattr(os.path, "isdir", lambda p: stats.append(p) or False)
    monkeypatch.setattr(os.path, "isfile", lambda p: stats.append(p) or False)
    other = tmp_path / "other"
    migrate.on_open(other)
    assert len(stats) == 2  # the old folder and the unfinished-move marker
    migrate.on_open(other), migrate.on_open(root.resolve())
    assert len(stats) == 2
    stats.clear()
    assert migrate.at_start(["keepfold", "taint"]) is None and len(stats) == 1  # ~/.cortex


def test_source_checkout_guard(tmp_path, monkeypatch, capsys):
    from keepfold.vault import Vault
    monkeypatch.setattr(migrate, "_dev", None)
    assert migrate.source_checkout()  # this test runs from the repo
    old_home(Path.home())
    root = old_vault(tmp_path / "v")
    assert migrate.at_start(["keepfold", "find", "x"]) is None
    assert "keepfold migrate" in capsys.readouterr().err
    assert migrate.at_start(["keepfold", "taint"]) is None and capsys.readouterr().err == ""
    Vault(root)
    assert migrate.launch() is None
    assert (Path.home() / ".cortex").is_dir() and (root / ".cortex").is_dir()  # untouched
    assert not (Path.home() / ".keepfold").exists() and not (root / ".keepfold").exists()
    # by hand it moves, even from a checkout
    assert migrate.everything([root])["state"] == "done"
    moved_ok(root)


# ---- events, doctor, the CLI ------------------------------------------------------------------


def _events(root: Path, client: str, tool: str) -> list[dict]:
    from keepfold.events import EventLog
    from keepfold.vault import Vault
    sid = "s_01JABCDEFGHJKMNPQRSTVWXYZ0"
    rows = [{"id": "e0", "ts": "2026-11-01T09:00:00Z", "day": "2026-11-01", "session": sid,
             "kind": "open", "payload": {"client": client}}]
    rows += [{"id": f"e{d}", "ts": f"2026-11-{d:02}T10:00:00Z", "day": f"2026-11-{d:02}",
              "session": sid, "kind": "grade", "object": "concept:c", "verdict": v,
              "grader": {"client": client, "model": "m"}}
             for d, v in ((1, "correct"), (3, "wrong"), (6, "correct"))]
    rows.append({"id": "e9", "ts": "2026-11-06T11:00:00Z", "day": "2026-11-06", "session": sid,
                 "kind": "change", "key": "k1", "payload": {"note": "keyed", "tool": tool}})
    p = root / "events" / "2026" / "11" / f"{sid}.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return list(EventLog(Vault(root)).iter_events())


def test_old_events_fold_the_same(tmp_path):
    from keepfold import levels, tonight
    old = _events(tmp_path / "old", "cortex-app", "cortex_log taught")
    new = _events(tmp_path / "new", "keepfold-app", "keepfold_log taught")
    assert levels.fold(old, "2026-11-20") == levels.fold(new, "2026-11-20")
    assert tonight.clients(old) == tonight.clients(new) == {}  # the app's own: not an AI app
    assert [e.get("grader") for e in old] == [e.get("grader") for e in new]
    assert old[-1]["payload"]["tool"] == "keepfold_log taught"
    raw = next((tmp_path / "old" / "events").rglob("*.jsonl")).read_text(encoding="utf-8")
    assert "cortex-app" in raw  # history stays as written


def test_doctor_row_and_old_env(auto, tmp_path, monkeypatch):
    from keepfold import doctor
    assert doctor.check_moved(None) == []  # never had Cortex: no row
    old_home(Path.home())
    root = old_vault(tmp_path / "v")
    (state, name, value, fix), = doctor.check_moved(root)
    assert (state, value) == ("WARN", "not moved yet") and migrate.CLOSE in fix
    migrate.everything([root])
    assert doctor.check_moved(root)[0][:3] == ("PASS", "Moved from Cortex", "done")
    (root / ".cortex").mkdir()
    row = doctor.check_moved(root)[0]
    assert row[2] == "both folders exist" and "Keepfold uses" in row[3]
    monkeypatch.setenv("CORTEX_VAULT", str(root))
    env = doctor.check_moved(root)[1]
    assert env[0] == "WARN" and "CORTEX_VAULT to KEEPFOLD_VAULT" in env[3]


def test_status_row(auto, tmp_path):
    from keepfold.app import status
    assert status.moved_row(None)["value"] == "nothing to move"
    root = old_vault(tmp_path / "v")
    assert status.moved_row(root)["state"] == "warn"
    migrate.vault(root)
    assert status.moved_row(root)["state"] == "ok"


def test_cli_exit_codes_and_json(auto, tmp_path, monkeypatch):
    from keepfold.cli import app
    none = {"state": "nothing", "rows": [], "message": ""}
    monkeypatch.setattr(migrate, "_apps", lambda: none)  # R4's part, tested on its own
    runner = CliRunner()
    r = runner.invoke(app, ["migrate", "--json"])
    assert r.exit_code == 0
    assert json.loads(r.stdout) == {"state": "nothing", "exit": 0, "apps": none, "vaults": [],
                                    "message": "Nothing from Cortex was left to move.",
                                    "home": {"old": str(Path.home() / ".cortex"),
                                             "new": str(Path.home() / ".keepfold"),
                                             "state": "nothing"}}
    a, b = old_vault(tmp_path / "a"), old_vault(tmp_path / "b é")
    old_home(Path.home(), a)
    r = runner.invoke(app, ["migrate", "--json"])  # the configured vault
    out = json.loads(r.stdout)
    assert (r.exit_code, out["state"], [v["state"] for v in out["vaults"]]) == (0, "done", ["done"])
    r = runner.invoke(app, ["migrate", "--vault", str(a), "--vault", str(b)])
    assert r.exit_code == 0 and f"Vault {b.resolve()}: done, 2 pages updated" in r.stdout
    moved_ok(b)
    c = old_vault(tmp_path / "c")
    (c / ".cortex" / "lock").write_text(f"1 {time.time():.3f} another-pc")
    r = runner.invoke(app, ["migrate", "--json", "--vault", str(c)])
    assert r.exit_code == 2 and json.loads(r.stdout)["state"] == "blocked"
    monkeypatch.setattr(migrate, "_vault", lambda *a: 1 / 0)
    r = runner.invoke(app, ["migrate", "--vault", str(c)])
    assert r.exit_code == 1 and "failed" in r.stdout and "nothing was deleted" in r.stdout


def test_apps_result_message_and_once_at_launch(auto, tmp_path, monkeypatch):
    """The AI-app rows set the overall state and the installer's one line; the app's start
    repoints them once after a move from Cortex, and a failure there never stops the app."""
    from keepfold.app import targets
    rows = [{"app": "Cursor", "file": "x", "action": "moved", "backup": "b", "note": "n"}]
    calls = []
    monkeypatch.setattr(targets, "migrate_old_entries",
                        lambda: calls.append(1) or [dict(r) for r in rows])
    old_home(Path.home(), old_vault(tmp_path / "v"))
    r = migrate.everything()
    assert (r["state"], r["exit"], r["apps"]["state"]) == ("done", 0, "done")
    assert "now use Keepfold" in r["message"]
    assert (Path.home() / ".keepfold" / migrate.APPS).is_file()
    monkeypatch.setattr(migrate, "source_checkout", lambda: False)
    assert migrate.launch() is None and calls == [1]  # done once: the start skips it
    (Path.home() / ".keepfold" / migrate.APPS).unlink()
    rows[0].update(action="error", note="its config is not valid JSON.")
    assert migrate.launch() is None and calls == [1, 1]  # an AI app's error is not a stop
    assert not (Path.home() / ".keepfold" / migrate.APPS).is_file()  # tried again next start
    r = migrate.everything()
    assert (r["state"], r["exit"]) == ("error", 1)
    assert r["message"] == "Cursor: its config is not valid JSON."
    monkeypatch.setattr(targets, "migrate_old_entries", lambda: 1 / 0)
    assert migrate.everything()["apps"]["state"] == "error"
