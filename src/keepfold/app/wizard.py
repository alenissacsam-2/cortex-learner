"""The Setup wizard (docs/design/keepfold-app.md section 2.6): vault, search model, local AI
runner, a model sized to this computer, a test, then the optional sections.

Nothing downloads or installs unless the owner clicks a button naming the item, its size and
its source; each download runs as a cancellable process. `keepfold.localllm` never downloads."""

from __future__ import annotations

import ctypes
import functools
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from keepfold import session, update
from keepfold._proc import proxy_env
from keepfold.app import actions, status, targets

# The models Setup offers: edit tags and sizes here. Sizes are approximate (4-bit downloads).
MODELS = [
    {"id": "2b", "label": "Qwen 3.5 2B", "min_gb": 0, "ollama": "qwen3.5:2b",
     "lms": "qwen/qwen3.5-2b", "size": "about 2 GB", "bytes": 2 * 10**9,
     "tradeoff": "Fast on any computer; good enough to sort pages into topics."},
    {"id": "4b", "label": "Qwen 3.5 4B", "min_gb": 6, "ollama": "qwen3.5:4b",
     "lms": "qwen/qwen3.5-4b", "size": "about 3 GB", "bytes": 3 * 10**9,
     "tradeoff": "Better one-line summaries; wants 6 GB of graphics memory or RAM."},
    {"id": "9b", "label": "Qwen 3.5 9B", "min_gb": 10, "ollama": "qwen3.5:9b",
     "lms": "qwen/qwen3.5-9b", "size": "about 6 GB", "bytes": 6 * 10**9,
     "tradeoff": "The best answers; slow without 10 GB or more."},
]
EMBED = {"size": "about 150 MB", "source": "huggingface.co"}
# council 18 Nov: connect first (the first useful result; recall falls back to keywords until
# the search model is in), then the search model; the multi-GB local model is optional
ORDER = ("vault", "connect", "embed", "history", "llm")
OLLAMA_PAGE = "https://ollama.com/download"
LMSTUDIO_PAGE = "https://lmstudio.ai/download"
# Ollama's download as shown to the owner, and the bytes its install needs (the free-space
# check). Windows: winget's installer.
_OLLAMA_SIZES = {"win32": ("about 1 GB", 10**9),
                 # shortcut: estimated from the 0.40.19 assets, re-measure on a release bump
                 "darwin": ("about 250 MB", 10**9), "linux": ("about 1.7 GB", 8 * 10**9)}


def _ollama_size() -> tuple[str, int]:
    return _OLLAMA_SIZES.get(sys.platform, _OLLAMA_SIZES["win32"])


OLLAMA_SIZE, OLLAMA_BYTES = _ollama_size()  # this computer's: the server's free-space check
OLLAMA_LICENSE = "https://github.com/ollama/ollama/blob/main/LICENSE"
# Ollama's own release files, fetched on the owner's click where no package manager does it
_OLLAMA_RELEASES = "https://github.com/ollama/ollama/releases/"
OLLAMA_RELEASE = _OLLAMA_RELEASES + "latest/download/"
OLLAMA_ASSET = {"darwin": "Ollama-darwin.zip", "linux": "ollama-linux-amd64.tar.zst"}
OLLAMA_SUMS = "sha256sum.txt"
OLLAMA_APP_BIN = Path("Ollama.app", "Contents", "Resources", "ollama")
GIT_PAGE = "https://git-scm.com/downloads"
GIT_SIZE, GIT_BYTES = "about 70 MB", 70 * 10**6
GIT_LICENSE = "https://git-scm.com/about/free-and-open-source"
# winget run from the app has no console to answer on (stdin is closed): never ask. The
# agreements are accepted only by a button shown beside the package's License link.
WINGET = ["--source", "winget", "--disable-interactivity", "--accept-package-agreements",
          "--accept-source-agreements"]
REGISTRY = "https://registry.ollama.ai"  # where `ollama pull` downloads from
SPARE_BYTES = 10**9  # free space a download must leave behind
LINUX_INSTALL = "curl -fsSL https://ollama.com/install.sh | sh"
RUNNERS = {"ollama": "Ollama", "lmstudio": "LM Studio"}  # Ollama is the recommended one
STOPPED = "{} is installed but not running: click Start (or run `keepfold llm start`)."
START_WAIT_S = 15  # Ollama's server takes a few seconds to answer after a start


# ---- hardware ---------------------------------------------------------------------------------


def _vram_gb() -> float | None:
    """The largest NVIDIA GPU's memory, from nvidia-smi; None without one."""
    if targets._which("nvidia-smi") is None:
        return None
    ok, out = targets._run(["nvidia-smi", "--query-gpu=memory.total",
                            "--format=csv,noheader,nounits"], timeout=10)
    mib = [float(x) for x in out.split() if x.replace(".", "", 1).isdigit()] if ok else []
    return round(max(mib) / 1024, 1) if mib else None


def _ram_gb() -> float | None:
    try:
        if sys.platform == "win32":
            class Mem(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            *((n, ctypes.c_ulonglong) for n in (
                                "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
                                "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual"))]
            m = Mem()
            m.dwLength = ctypes.sizeof(Mem)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return None
            return round(m.ullTotalPhys / 2**30, 1)
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1)
    except (AttributeError, OSError, ValueError):
        return None


def _amd_vram_gb(drm: Path = Path("/sys/class/drm")) -> float | None:
    """The largest AMD GPU's memory, from the amdgpu driver's sysfs file (bytes); None
    without one, or when it is under 2 GB (an APU's carve-out of RAM: the RAM rule fits it)."""
    sizes = []
    for f in drm.glob("card*/device/mem_info_vram_total"):
        try:
            sizes.append(int(f.read_text().strip()))
        except (OSError, ValueError):
            continue
    return round(max(sizes) / 2**30, 1) if sizes and max(sizes) >= 2 * 2**30 else None


@functools.cache  # nvidia-smi or a RAM query: the machine does not change while the app runs
def hardware() -> dict:
    if (gb := _vram_gb()) is not None:
        return {"kind": "gpu", "gb": gb, "label": f"{gb:g} GB of graphics memory (NVIDIA)"}
    # Apple Silicon's GPU uses the RAM itself (unified memory)
    if sys.platform == "darwin" and platform.machine() == "arm64" and (gb := _ram_gb()):
        return {"kind": "gpu", "gb": gb, "label": f"{gb:g} GB of unified memory (Apple Silicon)"}
    if sys.platform.startswith("linux") and (gb := _amd_vram_gb()) is not None:
        return {"kind": "gpu", "gb": gb, "label": f"{gb:g} GB of graphics memory (AMD)"}
    gb = _ram_gb()
    none = ("no NVIDIA graphics card found" if sys.platform == "win32"
            else "no graphics memory found")
    return {"kind": "ram", "gb": gb,
            "label": f"{gb:g} GB of memory ({none})" if gb else "memory size unknown"}


def recommend(gb: float | None, kind: str = "gpu") -> str:
    """≤ 4 GB → 2B, 6–8 GB → 4B, ≥ 10 GB → 9B (sizes in between round down); without an
    NVIDIA card ("ram") at most 4B, since 9B runs slowly there (it stays selectable)."""
    if gb is None or gb < 6:
        return "2b"
    return "4b" if gb < 10 or kind != "gpu" else "9b"


def model(id: str) -> dict:
    for m in MODELS:
        if m["id"] == id:
            return m
    raise ValueError(f"unknown model {id!r}")


# ---- runners ------------------------------------------------------------------------------------


def _mac_apps() -> tuple[Path, Path]:
    """Where Ollama.app may be: the per-user folder Setup installs into, then the system one."""
    return Path.home() / "Applications", Path("/Applications")


def _linux_ollama() -> Path:
    """Where Setup unpacks Ollama's Linux release (bin/ollama, lib/ollama)."""
    return install_dir() / "keepfold" / "ollama"


def _ollama_unit() -> Path:
    """The systemd user unit that runs Setup's Linux Ollama."""
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return xdg / "systemd" / "user" / "keepfold-ollama.service"


def ollama_exe() -> str | None:
    """`ollama` on PATH, or where the Windows installer puts it (PATH is not updated for a
    process started before the install), or inside Ollama.app on macOS, or Setup's Linux copy."""
    if found := targets._which("ollama"):
        return found
    own = ([a / OLLAMA_APP_BIN for a in _mac_apps()] if sys.platform == "darwin"
           else [_linux_ollama() / "bin" / "ollama"] if sys.platform.startswith("linux") else [])
    if found := next((str(p) for p in own if p.is_file()), None):
        return found  # the app's own copy: its PATH link may not be made yet
    local = os.environ.get("LOCALAPPDATA")
    p = Path(local, "Programs", "Ollama", "ollama.exe") if local else None
    return str(p) if p and p.is_file() else None


def install_plan() -> dict:
    """How to install Ollama here: a command the app may run on click, or one shown only."""
    if sys.platform == "win32":
        ok = targets._which("winget") is not None
        return {"run": ["winget", "install", "--id", "Ollama.Ollama", "-e", *WINGET]
                if ok else None, "source": "winget package Ollama.Ollama, from ollama.com",
                "size": OLLAMA_SIZE, "page": OLLAMA_PAGE, "license": OLLAMA_LICENSE}
    size = _ollama_size()[0]
    if sys.platform == "darwin" and targets._which("brew") is not None:
        return {"run": ["brew", "install", "ollama"], "source": "Homebrew",
                "size": size, "page": OLLAMA_PAGE, "license": OLLAMA_LICENSE}
    linux64 = sys.platform.startswith("linux") and platform.machine() == "x86_64"
    if sys.platform == "darwin" or linux64:
        return {"run": None, "fetch": OLLAMA_RELEASE + OLLAMA_ASSET[sys.platform],
                "sums": OLLAMA_RELEASE + OLLAMA_SUMS,
                "to": str(_linux_ollama() if linux64 else install_dir()),
                "source": "github.com/ollama/ollama releases", "size": size,
                "page": OLLAMA_PAGE, "license": OLLAMA_LICENSE}
    return {"run": None, "show": LINUX_INSTALL, "source": "ollama.com", "size": size,
            "page": OLLAMA_PAGE}


def _ollama_state(serving: list[str] | None) -> str:
    return "running" if serving is not None else "stopped" if ollama_exe() else "not_installed"


def _lms_state(exe: str | None) -> str:
    if exe is None:
        return "not_installed"
    from keepfold import localllm
    st = localllm.lmstudio_state()
    return "running" if st and st["port"] else "stopped"


def runner_state(runner: str) -> str:
    """"not_installed", "stopped" or "running"."""
    if runner == "ollama":
        return _ollama_state(status.ollama_models())
    if runner == "lmstudio":
        return _lms_state(status.lms_path())
    raise ValueError("runner must be ollama or lmstudio")


def _ollama_start_cmd() -> list[str]:
    """Ollama's desktop app when installed (it runs the server), else `ollama serve`."""
    local = os.environ.get("LOCALAPPDATA")
    if sys.platform == "win32" and local and (
            app := Path(local, "Programs", "Ollama", "ollama app.exe")).is_file():
        return [str(app)]
    if sys.platform == "darwin":
        for a in _mac_apps():
            if (app := a / "Ollama.app").is_dir():
                return ["open", "-a", str(app)]
    if sys.platform.startswith("linux") and _ollama_unit().is_file():
        return ["systemctl", "--user", "start", "keepfold-ollama"]
    return [ollama_exe(), "serve"]


def start(runner: str) -> dict:
    """Start an installed runner and wait until it answers. Ollama: its desktop app, else a
    detached `ollama serve` with no OLLAMA_HOST set by us; LM Studio: `lms server start`.
    {"ok", "state", "lines"}; raises ValueError when it is not installed or fails to start."""
    if runner not in RUNNERS:
        raise ValueError("runner must be ollama or lmstudio")
    name, now = RUNNERS[runner], runner_state(runner)
    if now == "running":
        return {"ok": True, "state": now, "lines": [f"{name} is already running"]}
    if now == "not_installed":
        raise ValueError(f"{name} is not installed yet (step 3).")
    if runner == "lmstudio":
        args = [status.lms_path(), "server", "start"]
        ok, out = targets._run(args, timeout=60)
        if not ok:
            raise ValueError(f"`lms server start` failed: {out[-300:]}")
    else:
        args = _ollama_start_cmd()
        detach = ({"creationflags": subprocess.DETACHED_PROCESS
                   | subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
                  else {"start_new_session": True})  # outlives the app and its console
        try:
            actions._popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, close_fds=True, **detach)
        except OSError as e:
            raise ValueError(f"could not start {args[0]}: {e}") from None
    deadline = time.monotonic() + START_WAIT_S
    while (now := runner_state(runner)) != "running" and time.monotonic() < deadline:
        time.sleep(0.5)
    ran = "ran: " + subprocess.list2cmdline(args)
    if now != "running":
        return {"ok": False, "state": now, "lines": [
            ran, f"{name} did not answer within {START_WAIT_S} s; try Start again, or open "
            f"{name} yourself."]}
    return {"ok": True, "state": now, "lines": [ran, f"{name} is running"]}


def _have(m: dict, ollama: list[str] | None, lms: list[dict]) -> dict:
    tag, key = m["ollama"].lower(), m["lms"].lower()
    return {"ollama": any(n.lower() in (tag, tag + ":latest") for n in ollama or []),
            "lmstudio": any(key in (r["key"].lower(), r["name"].lower()) or
                            r["key"].lower().endswith("/" + key.split("/")[-1]) for r in lms)}


def state(root: Path | None, scanned: dict | None = None) -> dict:
    v = status.vault(root)
    e = status.embedding(root if v["ok"] else None)
    q = status.llm(quick=True)  # the slow probes (~2.7 s), kept and shared with Status's
    ollama, lms_exe = q["ollama"], q["lms"]
    lms = status.kept(("lms_models", None), status.lms_models) if lms_exe else []
    hw = hardware()
    models = [{**m, "have": _have(m, ollama, lms)} for m in MODELS]
    cfg = status.config().get("localllm")
    cfg = cfg if isinstance(cfg, dict) else {}
    from keepfold import cli
    o_state = _ollama_state(ollama)
    l_state = "not_installed" if lms_exe is None else "running" if q["server_port"] else "stopped"
    return {
        # moved: the config names a folder that is gone; Setup asks instead of recreating it
        "vault": {"done": v["ok"], "path": v["path"], "default": str(cli._default_vault()),
                  "moved": bool(v["path"]) and not v.get("exists")},
        "git": {"found": session.git_exe() is not None, "install": git_plan(),
                "enabled": session.git_choice()[0]},
        "embed": {"done": e["downloaded"], "indexed": e.get("indexed"), **EMBED},
        "runner": {"done": "running" in (o_state, l_state), "recommend": "ollama",
                   "ollama": {"found": ollama_exe() is not None, "serving": ollama is not None,
                              "state": o_state},
                   "lmstudio": {"found": lms_exe is not None, "page": LMSTUDIO_PAGE,
                                "state": l_state},
                   "install": install_plan()},
        "model": {"done": any(m["have"]["ollama"] or m["have"]["lmstudio"] for m in models),
                  "hardware": hw, "recommend": recommend(hw["gb"], hw["kind"]), "options": models},
        "test": {"done": cfg.get("enabled") is True and bool(cfg.get("model")),
                 "model": cfg.get("model"), "runner": cfg.get("runner")},
        "history": (h := status.history(root if v["ok"] else None, scanned or {}))
        | {"done": bool(h.get("imported"))},
        "order": list(ORDER),
    }


# ---- the steps' jobs ---------------------------------------------------------------------------


TEMP_REFUSED = ("That folder is inside Windows' temporary folder, which Windows can empty at any "
                "time. Pick a folder outside it, such as one in Documents.")


def vault_path(path) -> Path:
    """The folder `make_vault` would set up, or ValueError in plain words. A temp folder is
    refused while a real vault exists; a first setup may use it (Status warns). The app checks
    this before the job starts, so a refused folder is one message, not a failed job too."""
    from keepfold import taint
    from keepfold.vault import in_temp_folder
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Choose a folder for the vault.")
    root = Path(path.strip().strip("\"'")).expanduser()
    if not root.is_absolute():
        raise ValueError("Give the full path of the folder, such as C:\\Users\\you\\Keepfold.")
    root = root.resolve()
    if root.exists() and not root.is_dir():
        raise ValueError(f"{root} is a file; pick a folder.")
    current = taint.config().get("vault")
    if (current and Path(current).is_dir() and not in_temp_folder(current)
            and in_temp_folder(root)):
        raise ValueError(TEMP_REFUSED)
    return root


def make_vault(job, path: str) -> tuple[Path, dict]:
    """`keepfold init` on the chosen folder (it becomes the default vault), refused as
    `vault_path` says before anything is made there."""
    from keepfold import cli
    root = vault_path(path)
    job.say(f"setting up {root}")
    did, skipped, warned = cli._init(root, default=True)
    from keepfold import doctor
    if (sync := doctor.check_onedrive(root))[0] != "PASS":
        warned.append(f"{sync[2]}: {sync[3]}")
    for line in [*did, *warned]:
        job.say(str(line))
    return root, {"ok": True, "vault": str(root),
                  "lines": [str(x) for x in [*did, *warned]][-100:]}


def _keepfold() -> str:
    from keepfold import clients
    return clients.keepfold_command()


def embed(job, root: Path | None) -> dict:
    """`keepfold model download` (the only path that fetches the search model)."""
    return actions.run_proc(job, [_keepfold(), "model", "download",
                                  *(["--vault", str(root)] if root else [])], env=proxy_env())


def voice(job) -> dict:
    """`keepfold model download --voice` (the only path that installs faster-whisper and fetches
    the speech model), on the owner's click."""
    return actions.run_proc(job, [_keepfold(), "model", "download", "--voice"], env=proxy_env())


def index(job, root: Path) -> dict:
    return actions.run_proc(job, [_keepfold(), "model", "build", "--vault", str(root)])


def git_plan(scope: str = "user") -> dict:
    """How to get git (the vault's history and undo): winget on Windows, else its page.
    `scope` "user" needs no admin; "machine" is offered only after that failed, and Windows
    then asks for admin permission itself (Keepfold never elevates). macOS: the OS's own
    Command Line Tools dialog. Linux: the apt command to show (Keepfold never runs root)."""
    if scope not in ("user", "machine"):
        raise ValueError("scope must be user or machine")
    if sys.platform == "darwin":
        return {"run": ["xcode-select", "--install"], "source": "Apple's Command Line Tools",
                "size": "about 1 GB", "page": GIT_PAGE, "license": GIT_LICENSE}
    if sys.platform.startswith("linux"):
        return {"run": None, "show": "sudo apt install git", "source": "Ubuntu's apt",
                "size": GIT_SIZE, "page": GIT_PAGE, "license": GIT_LICENSE}
    run = (["winget", "install", "--id", "Git.Git", "-e", "--scope", scope, *WINGET]
           if sys.platform == "win32" and targets._which("winget") else None)
    return {"run": run, "source": "winget package Git.Git, from git-scm.com", "size": GIT_SIZE,
            "page": GIT_PAGE, "license": GIT_LICENSE}


ADMIN_GIT = ("Git could not be installed for your Windows account only. It can be installed "
             "for everyone on this computer instead; Windows will ask for admin permission.")


def install_git(job, scope: str = "user", root: Path | None = None) -> dict:
    """winget's Git, then (git on, `root` a vault) the vault's history starts at once: the
    app finds the new git itself, so nobody quits and reopens Keepfold. A failed user-scope
    install says so and sets "admin" (the page then offers the machine-scope install)."""
    plan = git_plan(scope)
    if not plan["run"]:
        raise ValueError(f"Install git yourself from {plan['page']}")
    if sys.platform == "darwin":  # xcode-select only opens the OS dialog and returns at once
        targets._run(plan["run"])
        return {"ok": True, "lines": ["macOS is asking to install the Command Line Tools; "
                                      "click Install, then come back and click Check again."]}
    out = actions.run_proc(job, plan["run"])
    if out["cancelled"]:
        return out
    if not out["ok"]:
        if scope == "user":  # the page offers the machine-scope install with its License link
            out |= {"admin": ADMIN_GIT, "license": plan["license"]}
        return out
    if session.git_exe() is None:
        out["lines"].append("Git is installed, but Keepfold cannot find it yet. Quit Keepfold and "
                            "open it again, then click Use this folder under Vault.")
        return out
    if root is None or not session.git_choice()[0] or not (root / ".keepfold").is_dir():
        out["lines"].append("Git is installed.")
        return out
    from keepfold import cli
    from keepfold.vault import VaultError
    try:
        did, _, warned = cli._init(root)
    except VaultError as e:
        out["lines"].append(f"Git is installed, but starting your vault's history failed: {e} "
                            "Click Use this folder under Vault to try again.")
        return out
    out["lines"] += [str(x) for x in [*did, *warned]]
    out["lines"].append("Git is installed and your vault's history has started.")
    return out


def size_text(n: int) -> str:
    """"about 2.0 GB" or "about 70 MB"."""
    return f"about {n / 10**9:.1f} GB" if n >= 10**9 else f"about {max(1, round(n / 10**6))} MB"


def free_space_ok(path, need_bytes: int) -> tuple[bool, str]:
    """(ok, message) for a download of `need_bytes` into `path` (its nearest existing folder
    counts): ok while it leaves SPARE_BYTES free. The message says both sizes, for example
    "Needs about 2.0 GB; 1.1 GB free on C:." A failed check never blocks."""
    p = Path(path).expanduser().absolute()
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        free = shutil.disk_usage(p).free
    except OSError:
        return True, ""
    msg = f"Needs {size_text(need_bytes)}; {free / 10**9:.1f} GB free on {p.drive or 'this disk'}."
    if free >= need_bytes + SPARE_BYTES:
        return True, msg
    return False, msg + " Free up some space first, or continue anyway."


def install_dir() -> Path:
    """Where winget's per-user installs go (Ollama, Git), for the free-space check; on macOS
    the per-user Applications folder Ollama.app is unzipped into; on Linux ~/.local/share."""
    if sys.platform == "darwin":
        return Path.home() / "Applications"
    if sys.platform.startswith("linux"):
        return Path.home() / ".local" / "share"
    return Path(os.environ.get("LOCALAPPDATA") or Path.home())


def models_dir(runner: str) -> Path:
    """Where a runner keeps its models, for the free-space check."""
    if runner == "ollama":
        return Path(os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama")
    return Path.home() / ".lmstudio"


def _download(url: str, dest: Path, job) -> Path:
    """`url`, an Ollama release file, saved as `dest` through the system proxy (the updater's
    opener), following redirects only to GitHub's download hosts; job.say once per MB. Cancel
    raises actions.Cancelled. A partial file is deleted."""
    if not url.startswith(_OLLAMA_RELEASES):
        raise ValueError(f"Refused a download that is not an Ollama release file: {url}")
    for _ in range(5):
        try:
            r = update._get(url, None, "application/octet-stream")
            break
        except urllib.error.HTTPError as e:
            if e.code not in update._REDIRECTS:
                raise ValueError(f"GitHub answered {e.code} for {url}. Try again later.") \
                    from None
            url = urljoin(url, e.headers.get("Location") or "")
            p = urlsplit(url)
            if not url.startswith(_OLLAMA_RELEASES) and (
                    p.scheme != "https" or (p.hostname or "") not in update._CDN):
                raise ValueError(f"The download was sent to {p.hostname or url}, which is not "
                                 "GitHub; stopped.") from None
        except OSError as e:
            raise ValueError("The download stopped. Check your internet connection and try "
                             f"again ({getattr(e, 'reason', e)}).") from None
    else:
        raise ValueError("The download was redirected too many times; stopped.")
    part = dest.with_name(dest.name + ".part")
    total, got, said = int(r.headers.get("Content-Length") or 0), 0, -1
    try:
        with r, open(part, "wb") as f:
            while chunk := r.read(1 << 16):
                if job.cancelled.is_set():
                    raise actions.Cancelled
                f.write(chunk)
                got += len(chunk)
                if got >> 20 != said:
                    said = got >> 20
                    job.say(f"{dest.name}: {said} MB" + (f" of {total >> 20} MB" if total else ""))
    except OSError as e:
        part.unlink(missing_ok=True)
        raise ValueError("The download stopped. Check your internet connection and try again "
                         f"({getattr(e, 'reason', e)}).") from None
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, dest)
    return dest


def _unpack(src: Path, to: Path) -> None:
    """Unpack `src` into `to`. A .tar.zst (Linux) streams through zstandard into tarfile's
    "data" filter, which refuses paths and links outside `to` and drops special bits.
    A zip keeps each file's Unix permission bits (zipfile drops them, and Ollama.app's
    programs need their executable bits; never setuid, setgid or sticky) and its symlinks (app
    frameworks use them). ValueError, before anything is written, for a zip file or link that
    would land outside `to`."""
    if src.name.endswith(".tar.zst"):
        import zstandard  # Linux only (the dependency's platform marker)
        try:
            with (open(src, "rb") as f, zstandard.ZstdDecompressor().stream_reader(f) as z,
                  tarfile.open(fileobj=z, mode="r|") as t):
                t.extractall(to, filter="data")
        except (tarfile.TarError, zstandard.ZstdError) as e:
            raise ValueError(f"Could not unpack {src.name}: {e}") from None
        return
    root = to.resolve()

    def inside(p: Path) -> Path:
        if not p.resolve().is_relative_to(root):
            raise ValueError(f"Refused the archive: it would write outside {to} ({p.name}).")
        return p

    with zipfile.ZipFile(src) as z:
        members = [(i, i.external_attr >> 16) for i in z.infolist()]
        links = {i.filename: z.read(i).decode() for i, mode in members if stat.S_ISLNK(mode)}
        for i, _ in members:  # all checked before the first write
            inside(root / i.filename)
            if i.filename in links:
                inside((root / i.filename).parent / links[i.filename])
        for i, mode in members:
            out = inside(root / i.filename)  # again: the links made so far now resolve
            if i.filename in links:
                inside(out.parent / links[i.filename])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.unlink(missing_ok=True)
                os.symlink(links[i.filename], out)
            elif i.is_dir():
                out.mkdir(parents=True, exist_ok=True)
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(i) as r, open(out, "wb") as f:
                    shutil.copyfileobj(r, f)
                if mode & 0o777:
                    os.chmod(out, mode & 0o777)


def _ollama_service(to: Path) -> list[str]:
    """Linux with systemd: a user unit runs `<to>/bin/ollama serve` now and at each login.
    Without one (or when systemctl fails, the unit then removed) Start runs `ollama serve`."""
    if not status._systemd():
        return ["Click Start to run Ollama."]
    unit = _ollama_unit()
    try:
        text = status.render_login(".service", "Ollama for Keepfold",
                                   [str(to / "bin" / "ollama"), "serve"])
    except ValueError as e:
        return [str(e), "Click Start to run Ollama."]
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(text, encoding="utf-8", newline="\n")
    for args in (["systemctl", "--user", "daemon-reload"],
                 ["systemctl", "--user", "enable", unit.stem],
                 ["systemctl", "--user", "restart", unit.stem]):  # a reinstall runs the new copy
        ok, out = targets._run(args, timeout=30)
        if not ok:
            unit.unlink(missing_ok=True)
            return [f"`{' '.join(args)}` failed: {out[-300:]}", "Click Start to run Ollama."]
    return [f"wrote {unit}; Ollama runs now and at each login."]


def fetch_ollama(job, plan: dict) -> dict:
    """Ollama from its GitHub release, on the owner's click: the archive into ~/.keepfold/tmp,
    checked against the release's sha256sum.txt, unpacked into a staging folder beside its
    place, then swapped in whole for any older copy: Ollama.app into plan["to"] (macOS), or
    plan["to"] itself (Linux, which then gets its systemd user unit). A mismatch or failed
    unpack raises ValueError and leaves no partial Ollama; nothing asks for an admin password."""
    tmp = Path.home() / ".keepfold" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    name = plan["fetch"].rsplit("/", 1)[-1]
    archive, sums = tmp / name, tmp / OLLAMA_SUMS
    zipped = name.endswith(".zip")
    # in ~/Applications only Ollama.app is replaced; the Linux folder is Ollama's alone
    dest = Path(plan["to"]) / "Ollama.app" if zipped else Path(plan["to"])
    stage = None
    try:
        text = _download(plan["sums"], sums, job).read_text(encoding="utf-8", errors="replace")
        m = re.search(rf"^([0-9a-fA-F]{{64}})\s+\*?(?:\./)?{re.escape(name)}\s*$", text, re.M)
        if m is None:
            raise ValueError(f"The release's checksum file has no line for {name}; stopped.")
        _download(plan["fetch"], archive, job)
        if update._sha256(archive) != m[1].lower():
            raise ValueError(f"The download of {name} does not match the release's checksum, "
                             "so it was deleted. Try again later.")
        job.say(f"{name}: checksum OK; unpacking into {plan['to']}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".keepfold-ollama-", dir=dest.parent))
        _unpack(archive, stage / "new")
        new = stage / "new" / dest.name if zipped else stage / "new"
        if not (stage / "new" / OLLAMA_APP_BIN if zipped else new / "bin" / "ollama").is_file():
            raise ValueError(f"{name} holds no ollama program; nothing was installed.")
        if dest.exists() or dest.is_symlink():
            os.replace(dest, stage / "old")
        try:
            os.replace(new, dest)
        except OSError:
            if (stage / "old").exists():  # put the older copy back
                os.replace(stage / "old", dest)
            raise
    except actions.Cancelled:
        return {"ok": False, "cancelled": True, "lines": ["Cancelled; Ollama was not installed."]}
    finally:
        archive.unlink(missing_ok=True)
        sums.unlink(missing_ok=True)
        if stage:
            shutil.rmtree(stage, ignore_errors=True)
    lines = [f"downloaded {name} from {plan['source']}; checksum OK",
             f"Ollama is installed in {plan['to']}."]
    if sys.platform.startswith("linux"):
        lines += _ollama_service(dest)
    return {"ok": True, "lines": lines}


def install_ollama(job) -> dict:
    plan = install_plan()
    if plan.get("fetch"):
        return fetch_ollama(job, plan)
    if not plan["run"]:
        raise ValueError(f"Install Ollama yourself from {plan['page']}"
                         + (f" or run: {plan['show']}" if plan.get("show") else ""))
    return actions.run_proc(job, plan["run"])


# an Ollama registry name: name[:tag] or namespace/name[:tag]; names are lowercase, tags may
# not be (qwen3.5:4b-q4_K_M); never a URL or option
_OLLAMA_NAME = re.compile(r"(?:[a-z0-9][a-z0-9_-]*/)?[a-z0-9][a-z0-9._-]*"
                          r"(?::[A-Za-z0-9][A-Za-z0-9._-]*)?")


def ollama_name(name) -> str:
    """`name` when it is a local Ollama model name to download; ValueError otherwise."""
    from keepfold import localllm
    if not isinstance(name, str) or len(name) > 100 or not _OLLAMA_NAME.fullmatch(name):
        raise ValueError("Not an Ollama model name: use one like qwen3.5:4b.")
    if localllm._cloud(name):
        raise ValueError("That is a cloud model; Keepfold downloads local models only.")
    return name


_SIZES: dict[str, int] = {}  # a model's download size, once the registry told it


def ollama_size(name: str) -> int:
    """The download size in bytes of Ollama model `name`: a Setup model's from MODELS, else
    the total of its manifest on ollama.com's registry (one request, made on the owner's
    click). ValueError in plain words when the registry does not know it or cannot be
    reached."""
    name = ollama_name(name)
    if m := next((m for m in MODELS if m["ollama"] == name), None):
        return m["bytes"]
    if name in _SIZES:
        return _SIZES[name]
    repo, _, tag = name.partition(":")
    repo = repo if "/" in repo else "library/" + repo
    req = urllib.request.Request(f"{REGISTRY}/v2/{repo}/manifests/{tag or 'latest'}", headers={
        "Accept": "application/vnd.docker.distribution.manifest.v2+json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read(1_000_000))
        total = sum(int(x.get("size") or 0)
                    for x in [data.get("config") or {}, *(data.get("layers") or [])])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(f"ollama.com has no model named {name}. Check the name on "
                             "ollama.com/library.") from None
        total = 0
    except (OSError, ValueError, TypeError, AttributeError):
        total = 0
    if total <= 0:
        raise ValueError(f"Keepfold could not get the size of {name} from ollama.com. Check your "
                         "internet connection and try again.")
    _SIZES[name] = total
    return total


def pull_ollama(job, name: str) -> dict:
    if (exe := ollama_exe()) is None:
        raise ValueError("Ollama is not installed yet: install it under Setup → Local model.")
    if status.ollama_models() is None:  # `ollama pull` needs the server
        raise ValueError(STOPPED.format("Ollama"))
    return actions.run_proc(job, [exe, "pull", name], env=proxy_env())


def pull(job, id: str, runner: str) -> dict:
    m = model(id)
    if runner == "ollama":
        return pull_ollama(job, m["ollama"])
    if runner == "lmstudio":
        if (exe := status.lms_path()) is None:
            raise ValueError("LM Studio's `lms` was not found (step 3).")
        return actions.run_proc(job, [exe, "get", m["lms"], "--yes"], env=proxy_env())
    raise ValueError("runner must be ollama or lmstudio")


def test_and_save(job, id: str, runner: str) -> dict:
    """Test the model with a synthetic prompt; on success turn it on in config."""
    from keepfold import taint
    m = model(id)
    if runner not in ("ollama", "lmstudio"):
        raise ValueError("runner must be ollama or lmstudio")
    name = m["ollama"] if runner == "ollama" else m["lms"]
    old = status.config().get("localllm")
    local = {**(old if isinstance(old, dict) else {}), "enabled": True, "model": name,
             "runner": runner}
    try:
        out = actions.llm_test(job, actions.llm_config(**local))
    except ValueError:
        if runner_state(runner) == "stopped":
            raise ValueError(STOPPED.format(RUNNERS[runner])) from None
        raise
    taint.write_config(lambda cfg: {"localllm": {  # merged under the lock
        **(cfg["localllm"] if isinstance(cfg.get("localllm"), dict) else {}),
        "enabled": True, "model": name, "runner": runner}})
    job.say(f"saved: localllm on, {name} ({runner})")
    return {**out, "lines": [*out["lines"], f"saved: local model on, {name}"]}
