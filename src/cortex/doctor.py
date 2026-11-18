"""Environment checks for `cortex doctor` (SPEC §7): each returns (status, name, detail, fix)."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

from cortex import claims, delegation
from cortex.app import targets
from cortex.clients import claude_desktop_config_paths
from cortex.contract import MAX_CHARS as CONTRACT_MAX_CHARS
from cortex.events import EventLog
from cortex.session import _git
from cortex.vault import Vault


def check_python() -> tuple[str, str, str, str]:
    name = "Python and FTS5"
    ver = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info < (3, 12):  # noqa: UP036 (the check is the point)
        return "FAIL", name, f"Python {ver}", "Run cortex through `uv run` (Python 3.12+)."
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.Error as e:
        return "FAIL", name, f"sqlite3 has no FTS5: {e}", "Use the uv-managed Python 3.12."
    return "PASS", name, f"Python {ver}, sqlite {sqlite3.sqlite_version} with FTS5", ""


SYNC_MARKS = {"onedrive": "OneDrive", "dropbox": "Dropbox", "google drive": "Google Drive",
              "googledrive": "Google Drive", "my drive": "Google Drive", "icloud": "iCloud",
              "clouddocs": "iCloud"}  # casefolded path text -> the sync service


def check_onedrive(root: Path) -> tuple[str, str, str, str]:
    """WARN when the vault sits in a cloud-synced folder (OneDrive, Dropbox, Google Drive,
    iCloud): sync breaks the lock and silently copies private pages off the machine."""
    name = "Vault outside cloud sync"
    root = Path(root).resolve()
    text = str(root).casefold()
    synced = next((svc for mark, svc in SYNC_MARKS.items() if mark in text), None)
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        base = os.environ.get(var)
        if base and root.is_relative_to(Path(base).resolve()):
            synced = "OneDrive"
    if synced:
        return ("WARN", name, f"{root} is inside {synced}",
                f"Move the vault out of {synced}; sync can break the lock and atomic writes, "
                "and silently copies private pages and voice samples to the cloud.")
    return "PASS", name, str(root), ""


def check_git(root: Path) -> tuple[str, str, str, str]:
    name = "Vault git: no remote, push blocked"
    root = Path(root).resolve()
    if not (root / ".git").is_dir() and shutil.which("git") is None:
        return ("WARN", name, "git is not installed, so the vault has no history and undo does "
                "nothing", "Install git (Windows: Setup → Vault → Install Git, or `winget "
                "install --id Git.Git -e`; else git-scm.com), restart Cortex, then use the "
                "vault folder again in Setup (or run `cortex init <vault>`).")
    if not (root / ".git").is_dir():
        return "FAIL", name, "the vault has no git repo", "Run `cortex init <vault>`."
    if not (root / ".git" / "hooks" / "pre-push").is_file():
        return "FAIL", name, "no pre-push hook", "Run `cortex init <vault>` to add it."
    remotes = _git(root, "remote").stdout.split()
    if remotes:
        return ("WARN", name, f"remotes: {', '.join(remotes)}",
                "Remove them with `git remote remove <name>`; a push would publish private "
                "people pages and voice samples. The vault stays local.")
    return "PASS", name, "no remote; pre-push hook present", ""


def check_private_browse_git(root: Path) -> tuple[str, str, str, str]:
    """v7 C3: private browse pages must not be in the vault's git. Cortex untracks them on the
    next browse import, but never rewrites history; this says how."""
    from cortex.browse import forget

    name = "Private browsing out of git"
    root = Path(root)
    if not (root / ".git").exists():
        return "PASS", name, "no repo (see Vault git)", ""
    if _git(root, "ls-files", "--", "browse/private").stdout.strip():
        return ("WARN", name, "browse/private/ is still tracked by git",
                "Run `cortex browse import` once (it untracks the folder), or run "
                "`git rm -r --cached browse/private` in the vault and commit.")
    if _git(root, "check-ignore", "-q", "browse/private/x.md").returncode:
        return ("WARN", name, "browse/private/ is not in the vault's .gitignore",
                f"Run `cortex init {root}` or `cortex browse import`; either adds the line.")
    n = forget.private_in_history(root)
    if n:
        return ("WARN", name, f"{n} commit{'s' * (n != 1)} in the vault's git history hold "
                "private browse pages",
                "They are no longer committed, but old commits keep them. To remove them, "
                "back up the vault, then in it run `git filter-repo --path browse/private "
                "--invert-paths` (or `git filter-branch --index-filter \"git rm -r -q --cached "
                "--ignore-unmatch browse/private\" -- --all`), then `git reflog expire "
                "--expire=now --all` and `git gc --prune=now`. Cortex never rewrites history "
                "itself.")
    return "PASS", name, "browse/private/ is ignored and in no commit", ""


def check_git_identity(root: Path) -> tuple[str, str, str, str]:
    """N11: with no git identity every session commit fails silently, so `cortex undo` has
    nothing to revert."""
    name = "Vault git identity"
    if not (Path(root) / ".git").is_dir():
        return "PASS", name, "no repo (see Vault git)", ""
    email = _git(Path(root), "config", "user.email").stdout.strip()
    if not email:
        return ("WARN", name, "git has no user.email here, so session commits fail and "
                "`cortex undo` does nothing",
                f"Run `cortex init {root}`; it sets Cortex <cortex@localhost> for this vault only.")
    return "PASS", name, email, ""


MODEL_READY = ".complete"  # `cortex model download` writes it into models_dir() when it is done


def search_model_ready() -> bool:
    """The search model is whole: the download finished. Older downloads have no marker; they
    count when no file is left half-written (Hugging Face's `*.incomplete`)."""
    from cortex import semantic

    d = semantic.models_dir()
    if (d / MODEL_READY).is_file():
        return True
    return d.is_dir() and any(d.iterdir()) and not any(d.rglob("*.incomplete"))


def installer() -> bool:
    """Cortex runs from a CortexSetup.exe install (no terminal commands for its user)."""
    from cortex import update  # update imports cli, which imports this module

    return update.mode() == "installer"


def app_hint() -> str:
    """How this user opens the app: the Start menu for an installer install, else the command."""
    return "open Cortex from the Start menu" if installer() else "run `cortex app`"


def check_search_model() -> tuple[str, str, str, str]:
    from cortex import semantic

    d = semantic.models_dir()
    if search_model_ready():
        return "PASS", "Search model", str(d), ""
    return ("WARN", "Search model", "not downloaded: recall uses keywords only and a history "
            "import files every page under Other",
            "In the Cortex app (Start menu), Setup → Download search model." if installer() else
            "Run `cortex model download` (about 150 MB, once) or use `cortex app` → Setup.")


def check_on_path() -> tuple[str, str, str, str]:
    """The Claude Code plugin's server and hooks run `cortex` by name; hooks fail silently."""
    if found := shutil.which("cortex"):
        return "PASS", "`cortex` on PATH", found, ""
    return ("WARN", "`cortex` on PATH", "not found: the Claude Code plugin's server and hooks "
            "cannot start it",
            "Sign out of Windows and in again (setup added Cortex to your PATH), or run "
            "CortexSetup.exe again." if installer() else
            "Run `uv tool update-shell`, then open a new terminal.")


def check_ai_apps() -> tuple[str, str, str, str]:
    """N9: WARN when no AI app the app's Connect section lists has a `cortex` entry."""
    name = "AI app connected"
    on = [t.label for t in targets.targets() if t.connected()]
    if on:
        return "PASS", name, ", ".join(on), ""
    return ("WARN", name, "no AI app runs Cortex yet, so nothing reads or records anything",
            "Open Cortex from the Start menu and tick an app under Connect AI apps."
            if installer() else
            "Run `cortex app` and tick an app under Connect, or `cortex setup`.")


def check_local_model() -> tuple[str, str, str, str]:
    from cortex import localllm
    from cortex.taint import config

    name = "Local model"
    conf = config()
    if not localllm.enabled(conf):
        return "PASS", name, "off", ""
    got = localllm.status(conf)  # read-only, like `cortex llm status`
    if got["error"]:  # a non-loopback url or a cloud model
        return "WARN", name, got["error"], "Fix `localllm` in ~/.cortex/config.yaml."
    if (runner := got["runner"]) is None:
        return ("WARN", name, "on, but not running: no local runner answers with the "
                "configured model", "Run `cortex llm start` (or `cortex app` → Setup → Start).")
    return "PASS", name, runner.describe(), ""


def check_kill(root: Path) -> tuple[str, str, str, str]:
    """v5 PHASE4 V5: show the kill switch state (a state, not a fault)."""
    on = delegation.killed(Vault(root))
    return "PASS", "Kill switch", ("on: act-as-me, drafts and the delegate and reader roles "
                                   "are refused (`cortex kill --off`)" if on else "off"), ""


def check_exclude() -> tuple[str, str, str, str]:
    """Folders where Claude Code gets no priming and no Cortex tools (a state)."""
    from cortex.taint import exclude_list

    folders = exclude_list()
    return "PASS", "Excluded folders", "; ".join(folders) if folders else "none", ""


def check_me_pages(root: Path) -> tuple[str, str, str, str]:
    """Pages added by later versions (e.g. me/voice.md, me/delegation.md) on an older vault."""
    from cortex.cli import ME_PAGES  # cli imports doctor at module level

    missing = [rel for rel in ME_PAGES if not (Path(root) / rel).exists()]
    if missing:
        return ("WARN", "me/ pages", "missing: " + ", ".join(missing),
                f"Run `cortex init {root}`; it adds missing pages and changes nothing else.")
    return "PASS", "me/ pages", f"{len(ME_PAGES)} present", ""


def check_id_clashes(root: Path) -> tuple[str, str, str, str]:
    name = "Note ids unique across kinds"
    seen: dict[str, list[str]] = {}
    for kind, folder in Vault.KIND_DIRS.items():
        for path in sorted((Path(root) / folder).glob("*.md")):
            seen.setdefault(path.stem, []).append(kind)
    clashes = [f"{id} ({', '.join(kinds)})" for id, kinds in seen.items() if len(kinds) > 1]
    if clashes:
        return ("WARN", name, f"{len(clashes)} clash(es): {'; '.join(clashes)}",
                "Rename one note in each pair; the index keeps only one note per id.")
    return "PASS", name, f"{len(seen)} ids", ""


def check_claims(root: Path) -> tuple[str, str, str, str]:
    """Live claims per authorship tier (v5 F3); a preview, so doctor records no baseline."""
    v = Vault(root)
    rows = claims.tiers(v, EventLog(v).iter_events())
    return "PASS", "Claim tiers", ", ".join(
        f"{t} {sum(r['tier'] == t for r in rows)}" for t in claims.TIERS), ""


def check_contract_size(root: Path) -> tuple[str, str, str, str]:
    name = "Teaching contract size"
    p = Path(root) / "me" / "teaching-contract.md"
    n = len(p.read_bytes().decode("utf-8")) if p.is_file() else 0  # whole file, before scoping
    if n > CONTRACT_MAX_CHARS:
        return ("WARN", name, f"me/teaching-contract.md is {n} characters (whole file, before "
                f"domain scoping); cortex_start cuts the contract at {CONTRACT_MAX_CHARS} "
                "(about 1,200 tokens)",
                "Trim the core rules or move domain-specific ones under `## Domain overrides`.")
    return "PASS", name, f"{n} characters", ""


def _base(s) -> str:
    """File name of a Windows or POSIX path, whatever OS reads it."""
    return re.split(r"[\\/]", str(s))[-1].casefold()


def _is_cortex_server(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    base = _base
    args = entry.get("args") if isinstance(entry.get("args"), list) else []
    return base(entry.get("command", "")).startswith("cortex") or (
        "mcp" in args and any(base(a).startswith("cortex") for a in args))


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def plugin_enabled(settings: Path | None = None) -> bool:
    """True when ~/.claude/settings.json enables a `cortex@...` Claude Code plugin."""
    settings = settings or Path.home() / ".claude" / "settings.json"
    plugins = _read_json(settings).get("enabledPlugins")
    return isinstance(plugins, dict) and any(
        str(k).startswith("cortex@") and v is True for k, v in plugins.items())


def check_duplicate_servers(settings: Path | None = None,
                            desktop_configs: list[Path] | None = None
                            ) -> tuple[str, str, str, str]:
    """WARN when the Claude Code plugin and a desktop-config entry both register Cortex."""
    name = "One Cortex server per client"
    plugin_on = plugin_enabled(settings)
    desktop = []
    for path in claude_desktop_config_paths() if desktop_configs is None else desktop_configs:
        servers = _read_json(path).get("mcpServers")
        if isinstance(servers, dict) and any(_is_cortex_server(e) for e in servers.values()):
            desktop.append(str(path))
    if plugin_on and desktop:
        return ("WARN", name, "the Claude Code plugin and the desktop config "
                f"({'; '.join(desktop)}) both register Cortex; the desktop app's Code tab loads "
                "both, so every tool and session is duplicated there",
                "Keep both if you use them: Claude Chat needs the desktop entry and the Claude "
                "Code CLI needs the plugin. In the desktop Code tab, expect two copies.")
    return "PASS", name, "plugin" if plugin_on else ("desktop config" if desktop else "none"), ""
