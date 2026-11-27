"""Cancel ends a download's whole process tree (keepfold._proc.Tree), not just the immediate
child: a grandchild that inherits the output pipe would otherwise keep running and keep the
job from ending. Real processes, but only synthetic Python scripts in tmp_path."""

import os
import sys
import time

import pytest

from keepfold import _proc
from keepfold.app import actions, server

# the real interpreter: a venv's python.exe is a launcher whose own job would hide the bug
PY = getattr(sys, "_base_executable", None) or sys.executable
PARENT = ("import subprocess, sys\n"
          "print('parent', flush=True)\n"
          "subprocess.Popen([sys.executable, '-c', 'import os, time; "
          "print(\"child\", os.getpid(), flush=True); time.sleep(60)']).wait()\n")


def _alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes
    k32 = ctypes.WinDLL("kernel32")
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    try:
        return bool(k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))) \
            and code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def _wait(cond, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


@pytest.mark.parametrize("job_object", [True, False], ids=["job-object", "fallback"])
def test_cancel_ends_the_grandchild_and_the_job(tmp_path, monkeypatch, job_object):
    if not job_object:  # Windows refused the Job Object: taskkill /T (POSIX: same path)
        monkeypatch.setattr(_proc, "_job_for", lambda p: None, raising=False)  # Windows only
    script = tmp_path / "parent.py"
    script.write_text(PARENT, encoding="utf-8")
    jobs = server.Jobs()
    jobs.start("Downloading", "setup", lambda job: actions.run_proc(job, [PY, str(script)]),
               cancellable=True)
    job = jobs.cur
    assert _wait(lambda: any(s.startswith("child ") for s in job.lines), 20), job.lines
    pid = int(next(s for s in job.lines if s.startswith("child ")).split()[1])
    assert _alive(pid)
    t0 = time.monotonic()
    job.cancel()
    assert _wait(lambda: not jobs.running(), 10), "run_proc still waiting on the pipe"
    assert time.monotonic() - t0 < 10
    assert job.error is None
    assert job.result["cancelled"] is True and job.result["ok"] is False
    assert _wait(lambda: not _alive(pid), 5), "the grandchild outlived Cancel"


def test_a_normal_run_returns_ok_and_its_lines(tmp_path):
    script = tmp_path / "ok.py"
    script.write_text("import subprocess, sys\nprint('one', flush=True)\n"
                      "subprocess.run([sys.executable, '-c', 'print(\"two\")'])\n",
                      encoding="utf-8")
    jobs = server.Jobs()
    jobs.start("Run", "setup", lambda job: actions.run_proc(job, [PY, str(script)]))
    assert _wait(lambda: not jobs.running(), 20)
    assert jobs.cur.error is None
    assert jobs.cur.result == {"ok": True, "cancelled": False, "lines": ["one", "two"]}
    assert jobs.cur.proc.job is None and jobs.cur.proc.ended  # the Job Object handle is closed
