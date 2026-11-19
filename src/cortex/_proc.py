"""Shared subprocess flags. The installed app (`cortex-app.exe`) has no console, and on Windows
every console child of such a process opens its own visible window unless it is started with
CREATE_NO_WINDOW: pass `creationflags=NO_WINDOW` to every subprocess (0 elsewhere)."""

import contextlib
import os
import signal
import subprocess
import threading
import urllib.request

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


def proxy_env(base: dict | None = None) -> dict:
    """`base` (default os.environ) plus the system proxy (the Windows registry, macOS settings)
    as HTTP(S)_PROXY/NO_PROXY when neither is set, for downloaders that read only the env (uv,
    hf_xet, ollama). shortcut: a PAC file (automatic proxy script) is not read; a network that
    needs one still needs HTTPS_PROXY set by hand."""
    env = dict(os.environ if base is None else base)
    if not any(env.get(k) for k in _PROXY_VARS):
        for scheme, url in urllib.request.getproxies().items():
            if scheme in ("http", "https", "no"):
                env[f"{scheme.upper()}_PROXY"] = url
    return env


class Tree:
    """A started process and every process it starts, for Cancel: `terminate()` ends them all,
    where Popen.terminate() would leave grandchildren (an installer under winget, the model
    download under uv) running and holding our pipe. Windows: a Job Object with
    KILL_ON_JOB_CLOSE (taskkill /T when the process could not join one). Elsewhere: the child's
    own session (start it with start_new_session=True), SIGTERM then SIGKILL to the group.
    `close()` once the process has ended."""

    def __init__(self, p):
        self.p, self.ended = p, False
        self.job = _job_for(p) if os.name == "nt" else None

    def terminate(self) -> None:
        pid = getattr(self.p, "pid", None)
        if not self.ended:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                if self.job:
                    _k32.TerminateJobObject(self.job, 1)
                elif os.name == "nt" and isinstance(pid, int):
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True, timeout=10, creationflags=NO_WINDOW)
                elif isinstance(pid, int):
                    _killpg(pid, signal.SIGTERM)
                    threading.Timer(3, _killpg, (pid, signal.SIGKILL)).start()
        self.p.terminate()

    def close(self) -> None:
        self.ended = True
        if self.job:
            _set_kill_on_close(self.job, False)  # a server the finished command started lives on
            _k32.CloseHandle(self.job)
            self.job = None


def _killpg(pgid: int, sig) -> None:
    with contextlib.suppress(OSError):
        os.killpg(pgid, sig)


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                             wintypes.DWORD)
    _k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)

    class _Limits(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        _fields_ = [("times", ctypes.c_int64 * 2), ("flags", wintypes.DWORD),
                    ("working_set", ctypes.c_size_t * 2), ("active", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD), ("io", ctypes.c_uint64 * 6),
                    ("memory", ctypes.c_size_t * 4)]

    def _set_kill_on_close(job, on: bool) -> bool:
        info = _Limits(flags=0x2000 if on else 0)  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        return bool(_k32.SetInformationJobObject(job, 9, ctypes.byref(info),  # extended limits
                                                 ctypes.sizeof(info)))

    def _job_for(p):
        """A new Job Object holding `p` (a Popen), or None (a fake, or Windows refused)."""
        handle = getattr(p, "_handle", None)
        job = _k32.CreateJobObjectW(None, None) if handle is not None else None
        if not job:
            return None
        if _set_kill_on_close(job, True) and _k32.AssignProcessToJobObject(job, int(handle)):
            return job
        _k32.CloseHandle(job)
        return None
