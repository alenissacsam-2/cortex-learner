"""The Setup wizard (docs/design/cortex-app.md section 2.6): vault, search model, local AI
runner, a model sized to this computer, a test, then the optional sections.

Nothing downloads or installs unless the owner clicks a button naming the item, its size and
its source; each download runs as a cancellable process. `cortex.localllm` never downloads."""

from __future__ import annotations

import ctypes
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from cortex import session
from cortex._proc import proxy_env
from cortex.app import actions, status, targets

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
OLLAMA_SIZE, OLLAMA_BYTES = "about 1 GB", 10**9
OLLAMA_LICENSE = "https://github.com/ollama/ollama/blob/main/LICENSE"
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
STOPPED = "{} is installed but not running: click Start (or run `cortex llm start`)."
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


@functools.cache  # nvidia-smi or a RAM query: the machine does not change while the app runs
def hardware() -> dict:
    if (gb := _vram_gb()) is not None:
        return {"kind": "gpu", "gb": gb, "label": f"{gb:g} GB of graphics memory (NVIDIA)"}
    gb = _ram_gb()
    return {"kind": "ram", "gb": gb,
            "label": f"{gb:g} GB of memory (no NVIDIA graphics card found)" if gb
            else "memory size unknown"}


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


def ollama_exe() -> str | None:
    """`ollama` on PATH, or where the Windows installer puts it (PATH is not updated for a
    process started before the install)."""
    if found := targets._which("ollama"):
        return found
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
    if sys.platform == "darwin":
        ok = targets._which("brew") is not None
        return {"run": ["brew", "install", "ollama"] if ok else None,
                "source": "Homebrew", "size": OLLAMA_SIZE, "page": OLLAMA_PAGE}
    return {"run": None, "show": LINUX_INSTALL, "source": "ollama.com", "size": OLLAMA_SIZE,
            "page": OLLAMA_PAGE}


def _ollama_state(serving: list[str] | None) -> str:
    return "running" if serving is not None else "stopped" if ollama_exe() else "not_installed"


def _lms_state(exe: str | None) -> str:
    if exe is None:
        return "not_installed"
    from cortex import localllm
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
    if sys.platform == "darwin" and Path("/Applications/Ollama.app").is_dir():
        return ["open", "-a", "Ollama"]
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
    ollama, lms_exe = status.ollama_models(), status.lms_path()
    lms = status.lms_models() if lms_exe else []
    hw = hardware()
    models = [{**m, "have": _have(m, ollama, lms)} for m in MODELS]
    cfg = status.config().get("localllm")
    cfg = cfg if isinstance(cfg, dict) else {}
    from cortex import cli
    o_state, l_state = _ollama_state(ollama), _lms_state(lms_exe)
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


def make_vault(job, path: str) -> tuple[Path, dict]:
    """`cortex init` on the chosen folder (it becomes the default vault)."""
    from cortex import cli
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Choose a folder for the vault.")
    root = Path(path.strip().strip("\"'")).expanduser()
    if not root.is_absolute():
        raise ValueError("Give the full path of the folder, such as C:\\Users\\you\\Cortex.")
    root = root.resolve()
    if root.exists() and not root.is_dir():
        raise ValueError(f"{root} is a file; pick a folder.")
    job.say(f"setting up {root}")
    did, skipped, warned = cli._init(root, default=True)
    from cortex import doctor
    if (sync := doctor.check_onedrive(root))[0] != "PASS":
        warned.append(f"{sync[2]}: {sync[3]}")
    for line in [*did, *warned]:
        job.say(str(line))
    return root, {"ok": True, "lines": [str(x) for x in [*did, *warned]][-100:]}


def _cortex() -> str:
    from cortex import clients
    return clients.cortex_command()


def embed(job, root: Path | None) -> dict:
    """`cortex model download` (the only path that fetches the search model)."""
    return actions.run_proc(job, [_cortex(), "model", "download",
                                  *(["--vault", str(root)] if root else [])], env=proxy_env())


def index(job, root: Path) -> dict:
    return actions.run_proc(job, [_cortex(), "model", "build", "--vault", str(root)])


def git_plan(scope: str = "user") -> dict:
    """How to get git (the vault's history and undo): winget on Windows, else its page.
    `scope` "user" needs no admin; "machine" is offered only after that failed, and Windows
    then asks for admin permission itself (Cortex never elevates)."""
    if scope not in ("user", "machine"):
        raise ValueError("scope must be user or machine")
    run = (["winget", "install", "--id", "Git.Git", "-e", "--scope", scope, *WINGET]
           if sys.platform == "win32" and targets._which("winget") else None)
    return {"run": run, "source": "winget package Git.Git, from git-scm.com", "size": GIT_SIZE,
            "page": GIT_PAGE, "license": GIT_LICENSE}


ADMIN_GIT = ("Git could not be installed for your Windows account only. It can be installed "
             "for everyone on this computer instead; Windows will ask for admin permission.")


def install_git(job, scope: str = "user", root: Path | None = None) -> dict:
    """winget's Git, then (git on, `root` a vault) the vault's history starts at once: the
    app finds the new git itself, so nobody quits and reopens Cortex. A failed user-scope
    install says so and sets "admin" (the page then offers the machine-scope install)."""
    plan = git_plan(scope)
    if not plan["run"]:
        raise ValueError(f"Install git yourself from {plan['page']}")
    out = actions.run_proc(job, plan["run"])
    if out["cancelled"]:
        return out
    if not out["ok"]:
        if scope == "user":  # the page offers the machine-scope install with its License link
            out |= {"admin": ADMIN_GIT, "license": plan["license"]}
        return out
    if session.git_exe() is None:
        out["lines"].append("Git is installed, but Cortex cannot find it yet. Quit Cortex and "
                            "open it again, then click Use this folder under Vault.")
        return out
    if root is None or not session.git_choice()[0] or not (root / ".cortex").is_dir():
        out["lines"].append("Git is installed.")
        return out
    from cortex import cli
    from cortex.vault import VaultError
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
    """Where winget's per-user installs go (Ollama, Git), for the free-space check."""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home())


def models_dir(runner: str) -> Path:
    """Where a runner keeps its models, for the free-space check."""
    if runner == "ollama":
        return Path(os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama")
    return Path.home() / ".lmstudio"


def install_ollama(job) -> dict:
    plan = install_plan()
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
    from cortex import localllm
    if not isinstance(name, str) or len(name) > 100 or not _OLLAMA_NAME.fullmatch(name):
        raise ValueError("Not an Ollama model name: use one like qwen3.5:4b.")
    if localllm._cloud(name):
        raise ValueError("That is a cloud model; Cortex downloads local models only.")
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
        raise ValueError(f"Cortex could not get the size of {name} from ollama.com. Check your "
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
    from cortex import taint
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
