"""TEST-PLAN §2 items 6-7 (data loss): event time and answer parsing, the git guards before a
session commit, the backup drive check, the recall log, unknown concepts parked in the inbox,
the session folders, the app's one-job-at-a-time runner and the local-model setup steps. No
process starts and nothing is downloaded."""

import threading
import time
from datetime import UTC, datetime

import pytest
from conftest import git

from cortex import backup, events, recall_log, session, taint
from cortex.app import actions, server, status, targets, wizard
from cortex.catalog import Catalog, resolve_or_inbox
from cortex.vault import Vault, VaultError

pytestmark = pytest.mark.usefixtures("home")


# ---- 6. events, session git guards, backup, recall log, catalog inbox, taint --------------


def test_parse_ts_reads_only_the_server_format():
    t = events.parse_ts("2026-11-18T09:05:01Z")
    assert t == datetime(2026, 11, 18, 9, 5, 1, tzinfo=UTC)
    for bad in ("2026-11-18T09:05:01", "2026-11-18T09:05:01.5Z", "2026-11-18 09:05:01Z",
                "2026-11-18T09:05:01+00:00", ""):
        with pytest.raises(ValueError):
            events.parse_ts(bad)


@pytest.mark.parametrize("raw,want", [
    ("  O( n log n )  ", "o(nlogn)"),
    ("`O(N^2)`", "o(n^2)"),
    ("$O(1)$ time", "o(1) time"),
    ("Two\tPointers\n", "two pointers"),
    ("", ""),
])
def test_norm_answer(raw, want):
    assert events.norm_answer(raw) == want


def test_git_busy_and_unmerged_paths_during_a_conflict(root):
    assert session.git_busy(root) is None and session.unmerged_paths(root) == []
    f = root / "me" / "profile.md"
    base = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    git(root, "checkout", "-q", "-b", "other")
    f.write_text("other side\n", encoding="utf-8")
    git(root, "commit", "-qam", "other")
    git(root, "checkout", "-q", base)
    f.write_text("this side\n", encoding="utf-8")
    git(root, "commit", "-qam", "this")
    import subprocess
    subprocess.run(["git", "-C", str(root), "merge", "-q", "other"], capture_output=True)
    assert session.unmerged_paths(root) == ["me/profile.md"]
    assert "MERGE_HEAD" in session.git_busy(root)
    git(root, "merge", "--abort")
    assert session.git_busy(root) is None


def test_same_drive(tmp_path):
    (tmp_path / "a").mkdir()
    assert backup.same_drive(tmp_path, tmp_path / "a")


def test_recall_log_rows_eval_set_and_cap(root, monkeypatch):
    v = Vault(root)
    rid = recall_log.log_recall(v, "cortex_recall", "two pointers", ["two-pointers", "lc-1"])
    recall_log.log_open(v, rid, "two-pointers")
    other = recall_log.log_recall(v, "cortex_recall", "unopened", ["lc-1"])
    rows = recall_log.rows(v)
    assert [r.get("rid") or r.get("opened") for r in rows] == [rid, rid, other]
    assert all(r["ts"].endswith("Z") for r in rows)
    assert recall_log.eval_set(rows) == [("two pointers", {"two-pointers"})]
    with (root / recall_log.FILE).open("a", encoding="utf-8") as f:
        f.write("not json\n")
    assert len(recall_log.rows(v)) == 3  # a torn line is skipped, not fatal
    monkeypatch.setattr(recall_log, "CAP", 2)
    recall_log.log_open(v, other, "lc-1")
    lines = (root / recall_log.FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and recall_log.rows(v)[-1]["id"] == "lc-1"  # the oldest go first


def test_recall_log_redacts_secrets_in_the_query(root):
    v = Vault(root)
    key = "sk-" + "a" * 24  # a SECRET_RES pattern, not a real key
    recall_log.log_recall(v, "cortex_recall", f"why does {key} fail", [])
    recall_log.log_recall(v, "cortex_recall", "two pointers", [])
    rows = recall_log.rows(v)
    assert rows[0]["query"] == "why does [redacted] fail" and rows[1]["query"] == "two pointers"
    assert key not in (root / recall_log.FILE).read_text(encoding="utf-8")


def test_recall_log_never_fails_the_tool(root, monkeypatch):
    v = Vault(root)

    def busy(*a, **k):
        raise VaultError("vault_busy", "locked")
    monkeypatch.setattr(Vault, "_atomic_write", busy)
    recall_log.log_open(v, "r", "x")  # logged as a warning only
    assert recall_log.rows(v) == []


def test_resolve_or_inbox_parks_unknown_names_once_above_sections(root):
    v = Vault(root)
    inbox = root / "inbox.md"
    inbox.write_text("# Inbox\n\nSome prose.\n\n## About me (unconfirmed)\n- I am tall\n",
                     encoding="utf-8")
    cat = Catalog.load(v)
    assert resolve_or_inbox(v, cat, "2 pointers") == "concept:two-pointers"  # an alias
    assert resolve_or_inbox(v, cat, "Segment  trees", "from a chat") == "inbox:segment-trees"
    assert resolve_or_inbox(v, cat, "Segment trees", "again") == "inbox:segment-trees"
    text = inbox.read_text(encoding="utf-8")
    assert text.count("- [ ] Segment trees (") == 1
    assert text.index("Segment trees") < text.index("## About me")
    assert "- I am tall" in text and "Some prose." in text
    with pytest.raises(VaultError):
        resolve_or_inbox(v, cat, "  !!  ")


def test_session_dirs(monkeypatch, tmp_path):
    assert taint.session_dirs(str(tmp_path)) == (str(tmp_path), None)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "P")
    monkeypatch.chdir(tmp_path)
    assert taint.session_dirs() == (str(tmp_path), "P")


# ---- 7. app jobs and the local-model setup -------------------------------------------------


def _wait(jobs: server.Jobs) -> server.Job:
    for _ in range(400):
        if not jobs.cur.running:
            return jobs.cur
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_jobs_one_at_a_time_cancel_and_errors():
    jobs, go = server.Jobs(), threading.Event()

    def slow(job):
        go.wait(5)
        return {"cancelled": job.cancelled.is_set()}
    assert jobs.start("A", "s", slow, cancellable=True) == {"job": "A", "seq": 1}
    with pytest.raises(server.Busy, match="A is still running"):
        jobs.start("B", "s", lambda job: None)
    assert jobs.running()

    class Proc:
        terminated = False

        def terminate(self):
            Proc.terminated = True
    jobs.cur.proc = Proc()
    jobs.cur.cancel()
    go.set()
    job = _wait(jobs)
    assert job.result == {"cancelled": True} and Proc.terminated and not jobs.running()

    jobs.start("C", "s", lambda job: (_ for _ in ()).throw(RuntimeError("TITLEMARKER")),
               quiet_errors=True)
    assert _wait(jobs).error == "RuntimeError"  # a message could quote a page
    jobs.start("D", "s", lambda job: (_ for _ in ()).throw(ValueError("Fix the path")),
               quiet_errors=True)
    assert _wait(jobs).error == "Fix the path"  # our own messages still show
    assert jobs.view()["seq"] == 3


def test_job_lines_are_capped():
    job = server.Job("x", "s", False)
    for k in range(500):
        job.say("y" * 1000 if k == 499 else k)
    assert len(job.lines) == 300 and len(job.lines[-1]) == 400 and job.lines[0] == "200"


@pytest.fixture
def config(home):
    p = home / ".cortex" / "config.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("vault: V\nrerank: false\nlocalllm:\n  budget_s: 60\n", encoding="utf-8")
    return p


def test_test_and_save_writes_config_only_after_a_pass(config, monkeypatch):
    m = wizard.MODELS[0]["id"]
    before = config.read_bytes()

    def fail(job, cfg=None):
        raise ValueError("no answer")
    monkeypatch.setattr(actions, "llm_test", fail)
    monkeypatch.setattr(wizard, "runner_state", lambda r: "running")
    with pytest.raises(ValueError, match="no answer"):
        wizard.test_and_save(server.Job("t", "s", False), m, "ollama")
    assert config.read_bytes() == before
    monkeypatch.setattr(wizard, "runner_state", lambda r: "stopped")
    with pytest.raises(ValueError, match="not running"):
        wizard.test_and_save(server.Job("t", "s", False), m, "ollama")
    with pytest.raises(ValueError, match="runner must be"):
        wizard.test_and_save(server.Job("t", "s", False), m, "vllm")
    assert config.read_bytes() == before

    seen = []
    monkeypatch.setattr(actions, "llm_test", lambda job, cfg=None: (
        seen.append(cfg), {"ok": True, "lines": ["ok"]})[1])
    out = wizard.test_and_save(server.Job("t", "s", False), m, "ollama")
    assert out["ok"] and seen[0]["localllm"]["enabled"] is True
    c = status.config()
    assert c["vault"] == "V" and c["rerank"] is False
    assert c["localllm"] == {"budget_s": 60, "enabled": True, "runner": "ollama",
                             "model": wizard.model(m)["ollama"]}


def test_install_ollama_runs_only_the_shown_command(monkeypatch):
    monkeypatch.setattr(targets, "_which", lambda c: None)
    monkeypatch.setattr(actions, "_popen", lambda *a, **k: pytest.fail("nothing may run"))
    with pytest.raises(ValueError, match="Install Ollama yourself"):
        wizard.install_ollama(server.Job("i", "s", True))
    monkeypatch.setattr(targets, "_which", lambda c: f"C:/bin/{c}.exe")
    ran = []

    class P:
        stdout = __import__("io").BytesIO(b"done\n")

        def wait(self):
            return 0
    monkeypatch.setattr(actions, "_popen", lambda args, **k: (ran.append(args), P())[1])
    plan = wizard.install_plan()
    if plan["run"] is None:
        pytest.skip("no click-to-run installer on this platform")
    assert wizard.install_ollama(server.Job("i", "s", True))["ok"] and ran == [plan["run"]]


def test_ollama_exe_finds_the_windows_install_off_path(monkeypatch, home):
    monkeypatch.setattr(targets, "_which", lambda c: None)
    monkeypatch.setenv("LOCALAPPDATA", str(home / "L"))
    assert wizard.ollama_exe() is None
    exe = home / "L" / "Programs" / "Ollama" / "ollama.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert wizard.ollama_exe() == str(exe)
    monkeypatch.setattr(targets, "_which", lambda c: "C:/on/path/ollama.exe")
    assert wizard.ollama_exe() == "C:/on/path/ollama.exe"
