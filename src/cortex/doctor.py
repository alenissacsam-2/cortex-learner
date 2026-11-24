"""Environment checks for `cortex doctor` (SPEC §7): each returns (status, name, detail, fix)."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

from cortex import claims, share
from cortex.app import targets
from cortex.clients import claude_desktop_config_paths
from cortex.contract import MAX_CHARS as CONTRACT_MAX_CHARS
from cortex.events import EventLog
from cortex.session import _git, git_choice, git_exe
from cortex.vault import CHAT_ARCHIVE, PRIVATE_BROWSE_DIR, Vault


def connects() -> list[str]:
    """The "Where Cortex connects" section: a heading, then one line per destination off this
    PC (cortex.egress)."""
    from cortex import egress
    return ["Where Cortex connects:", *(f"  {s}" for s in egress.summary())]


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


def _old_repo(root: Path) -> bool:
    """git is off but the vault still has a repo git can read: its remotes and history still
    matter for privacy, so those read-only checks still run."""
    return (root / ".git").exists() and git_exe() is not None


def _remote_warn(name: str, remotes: list[str], off: bool) -> tuple[str, str, str, str]:
    return ("WARN", name, ("git is off in your settings, but the vault's repo still has "
                           if off else "") + f"remotes: {', '.join(remotes)}",
            "Remove them with `git remote remove <name>`; a push would publish private "
            "people pages and voice samples. The vault stays local.")


def check_git(root: Path) -> tuple[str, str, str, str]:
    name = "Vault git: no remote, push blocked"
    root = Path(root).resolve()
    if not git_choice()[0]:
        if _old_repo(root) and (remotes := _git(root, "remote").stdout.split()):
            return _remote_warn(name, remotes, off=True)
        return ("PASS", name, "git is off in your settings (your choice): Cortex keeps no "
                "history of the vault and `cortex undo` is off", "")
    if not (root / ".git").is_dir() and git_exe() is None:
        return ("WARN", name, "git is not installed, so the vault has no history and undo does "
                "nothing", "Install git: in the Cortex app, Status → Git → Install Git "
                "(Windows; or run `winget install --id Git.Git -e --scope user`); else get it "
                "from git-scm.com. The app then starts the vault's history by itself; from a "
                "terminal, run `cortex init <vault>` after the install.")
    if not (root / ".git").is_dir():
        return "FAIL", name, "the vault has no git repo", "Run `cortex init <vault>`."
    if not (root / ".git" / "hooks" / "pre-push").is_file():
        return "FAIL", name, "no pre-push hook", "Run `cortex init <vault>` to add it."
    remotes = _git(root, "remote").stdout.split()
    if remotes:
        return _remote_warn(name, remotes, off=False)
    return "PASS", name, "no remote; pre-push hook present", ""


def check_private_browse_git(root: Path) -> tuple[str, str, str, str]:
    """v7 C3: private browse pages must not be in the vault's git. Cortex untracks them on the
    next browse import, but never rewrites history; this says how."""
    from cortex.browse import forget

    name = "Private browsing out of git"
    root = Path(root)
    git_on, kept = git_choice()
    if not git_on:
        if _old_repo(root) and (n := forget.private_in_history(root)):
            return _history_warn(name, n)
        return "PASS", name, "git is off in your settings (see Vault git)", ""
    if kept:
        return "PASS", name, "private browsing pages are kept in git by your choice", ""
    if not (root / ".git").exists():
        return "PASS", name, "no repo (see Vault git)", ""
    if _git(root, "ls-files", "--", PRIVATE_BROWSE_DIR).stdout.strip():
        return ("WARN", name, "browse/private/ is still tracked by git",
                "Run `cortex browse import` once (it untracks the folder), or run "
                "`git rm -r --cached browse/private` in the vault and commit.")
    if _git(root, "check-ignore", "-q", PRIVATE_BROWSE_DIR + "/x.md").returncode:
        if not (root / PRIVATE_BROWSE_DIR).exists():  # an older vault, nothing private yet
            return ("PASS", name, "no private pages yet; the .gitignore line is added before "
                    "the first one", "")
        return ("WARN", name, "browse/private/ is not in the vault's .gitignore",
                f"Run `cortex init {root}` or `cortex browse import`; either adds the line.")
    n = forget.private_in_history(root)
    if n:
        return _history_warn(name, n)
    return "PASS", name, "browse/private/ is ignored and in no commit", ""


def check_chat_archive_git(root: Path) -> tuple[str, str, str, str]:
    """v9 C5: imported chat pages sit in archive/chats/, which the vault's .gitignore keeps out
    of git."""
    name = "Chat archive out of git"
    root = Path(root)
    if not (root / ".git").exists():
        return "PASS", name, "no repo (see Vault git)", ""
    if _git(root, "check-ignore", "-q", CHAT_ARCHIVE + "/x.md").returncode:
        return ("WARN", name, f"{CHAT_ARCHIVE}/ is not in the vault's .gitignore",
                f"Run `cortex init {root}` or `cortex chats archive`; either adds the line.")
    if _git(root, "ls-files", "--", CHAT_ARCHIVE).stdout.strip():
        return ("WARN", name, f"{CHAT_ARCHIVE}/ is still tracked by git",
                f"Run `git rm -r --cached {CHAT_ARCHIVE}` in the vault and commit.")
    return "PASS", name, f"{CHAT_ARCHIVE}/ is ignored", ""


def check_browse_legacy(root: Path) -> tuple[str, str, str, str]:
    """F3: the browse files from before the store (`browse.legacy`), not read in yet."""
    from cortex.browse import legacy
    from cortex.vault import Vault
    name = "Old browse files"
    left = legacy.pending(Vault(root))
    if not left:
        return "PASS", name, "none left to read in", ""
    return ("WARN", name, f"{len(left)} not read into the browsing store yet: "
            + ", ".join(Path(r).name for r in left),
            "Run `cortex browse import` (or open the app): it reads them in and renames each "
            "to <name>.migrated.")


def _history_warn(name: str, n: int) -> tuple[str, str, str, str]:
    return ("WARN", name, f"{n} commit{'s' * (n != 1)} in the vault's git history hold "
            "private browse pages",
            "They are no longer committed, but old commits keep them. To remove them, "
            "back up the vault, then in it run `git filter-repo --path browse/private "
            "--invert-paths` (or `git filter-branch --index-filter \"git rm -r -q --cached "
            "--ignore-unmatch browse/private\" -- --all`), then `git reflog expire "
            "--expire=now --all` and `git gc --prune=now`. Cortex never rewrites history "
            "itself.")


def check_git_identity(root: Path) -> tuple[str, str, str, str]:
    """N11: with no git identity every session commit fails silently, so `cortex undo` has
    nothing to revert."""
    name = "Vault git identity"
    if not git_choice()[0]:
        return "PASS", name, "git is off in your settings (see Vault git)", ""
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
    on = share.killed(Vault(root))
    return "PASS", "Kill switch", ("on: act-as-me and the reader role "
                                   "are refused (`cortex kill --off`)" if on else "off"), ""


def check_exclude() -> tuple[str, str, str, str]:
    """Folders where Claude Code gets no priming and no Cortex tools (a state)."""
    from cortex.taint import exclude_list

    folders = exclude_list()
    return "PASS", "Excluded folders", "; ".join(folders) if folders else "none", ""


def check_me_pages(root: Path) -> tuple[str, str, str, str]:
    """Pages added by later versions (e.g. me/voice.md) on an older vault."""
    from cortex.cli import ME_PAGES  # cli imports doctor at module level

    missing = [rel for rel in ME_PAGES if not (Path(root) / rel).exists()]
    if missing:
        return ("WARN", "me/ pages", "missing: " + ", ".join(missing),
                f"Run `cortex init {root}`; it adds missing pages and changes nothing else.")
    return "PASS", "me/ pages", f"{len(ME_PAGES)} present", ""


SYNC_COPY_FIX = "Open both, keep what you want in the original, delete the copy."


def check_sync_copies(root: Path) -> tuple[str, str, str, str]:
    """WARN on a sync tool's conflict copies (sync_copies.find): a copied page is not read
    (its edit lives only there); the event log reads each event once, copies included."""
    from cortex import sync_copies

    name = "Sync conflict copies"
    found = [c for c, _ in sync_copies.find(root)]
    if not found:
        return "PASS", name, "none", ""
    more = f" and {len(found) - 5} more" if len(found) > 5 else ""
    return ("WARN", name, f"{len(found)} found: {', '.join(found[:5])}{more}",
            f"{SYNC_COPY_FIX} Cortex does not read a copied page, so an edit made on the "
            "other device is only in the copy.")


def check_id_clashes(root: Path) -> tuple[str, str, str, str]:
    name = "Note ids unique across kinds"
    seen: dict[str, list[str]] = {}
    for kind in Vault.KIND_DIRS:
        for path in sorted(p for d in Vault.dirs(kind) for p in (Path(root) / d).glob("*.md")):
            seen.setdefault(path.stem, []).append(kind)
    clashes = [f"{id} ({', '.join(kinds)})" for id, kinds in seen.items() if len(kinds) > 1]
    if clashes:
        return ("WARN", name, f"{len(clashes)} clash(es): {'; '.join(clashes)}",
                "Rename one note in each pair; the index keeps only one note per id.")
    return "PASS", name, f"{len(seen)} ids", ""


# RR-38: the import header (`Source: <label> · Date:`) or a chat page's Timeline line
_IMPORT_HEAD = re.compile(r"^(?:Timeline: [^\n]*?)?Source: ([^·\n]+?) · Date: [^\n]*$", re.M)
OTHER_SOURCE = "other"  # a label no Cortex importer writes: never printed, it may be page text


def _quartiles(xs: list[int]) -> list[int]:
    s = sorted(xs)
    return [s[round(q * (len(s) - 1))] for q in (0.25, 0.5, 0.75)]


def check_import_shapes(root: Path) -> list[tuple[str, str, str, str]]:
    """RR-38: per import source, its pages' length quartiles (characters below the header
    line) and the share of pages that are mostly links (sources.link_lines over 60% of the
    lines, the quality gate's rule) or numbers only (under 10% letters and at least 30% digits
    of their non-space characters). Numbers only: no page text, and a label Cortex's importers
    do not write shows as OTHER_SOURCE."""
    from cortex import chat_import, importers, sources
    known = {*chat_import.LABELS.values(), *importers.LABELS.values(), "Calendar", "Documents",
             "Git", "Highlights", "Pocket"}
    shapes: dict[str, list[tuple[int, bool, bool]]] = {}
    for p in Vault(root).note_paths("resource"):
        try:
            text = p.read_text("utf-8", errors="replace")
        except OSError:
            continue
        if not (m := _IMPORT_HEAD.search(text, text.find("\n---", 3) + 1)):
            continue
        body = text[m.end():]
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        chars = [c for c in body if not c.isspace()]
        links = bool(lines) and sources.link_lines(lines) > 0.6 * len(lines)
        numbers = bool(chars) and sum(c.isalpha() for c in chars) < 0.1 * len(chars) \
            and sum(c.isdigit() for c in chars) >= 0.3 * len(chars)
        label = m.group(1).strip() if m.group(1).strip() in known else OTHER_SOURCE
        shapes.setdefault(label, []).append((len(body.strip()), links, numbers))
    if not shapes:
        return [("PASS", "Import shapes", "no imported pages", "")]
    out = []
    for label, rows in sorted(shapes.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        q, n = _quartiles([r[0] for r in rows]), len(rows)
        out.append(("PASS", f"Import shapes: {label}", (
            f"{n:,} page{'s' * (n != 1)}, length quartiles {q[0]:,} / {q[1]:,} / {q[2]:,} "
            f"characters, mostly links {sum(r[1] for r in rows) / n:.0%}, numbers only "
            f"{sum(r[2] for r in rows) / n:.0%}"), ""))
    return out


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


def _has_cortex(path: Path) -> bool:
    """`path`'s mcpServers hold a Cortex server."""
    servers = _read_json(path).get("mcpServers")
    return isinstance(servers, dict) and any(_is_cortex_server(e) for e in servers.values())


def plugin_enabled(settings: Path | None = None) -> bool:
    """True when ~/.claude/settings.json enables a `cortex@...` Claude Code plugin."""
    settings = settings or Path.home() / ".claude" / "settings.json"
    plugins = _read_json(settings).get("enabledPlugins")
    return isinstance(plugins, dict) and any(
        str(k).startswith("cortex@") and v is True for k, v in plugins.items())


def last_calls(root: Path) -> dict[str, float]:
    """Client name -> the time (epoch s) of its latest call, from the marks the server writes
    (mcp_server.SEEN: one file per client, its mtime the call, rewritten at most once a
    minute)."""
    out = {}
    for p in (Path(root) / ".cortex" / "clients").glob("*.txt"):
        try:
            out[p.read_text(encoding="utf-8").strip() or p.stem] = p.stat().st_mtime
        except OSError:
            continue
    return out


def _when(t: float | None) -> str:
    from datetime import datetime
    return f"last call {datetime.fromtimestamp(t):%Y-%m-%d %H:%M}" if t else \
        "no call recorded yet"


def check_clients(root: Path, settings: Path | None = None,
                  desktop_configs: list[Path] | None = None) -> list[tuple[str, str, str, str]]:
    """Per Claude client: Cortex configured, the Claude Code prime hook, and its last call."""
    calls = last_calls(root)
    seen = {k.casefold(): t for k, t in calls.items()}

    def last(chat: bool) -> float | None:  # the Chat tab names itself claude-ai; Code claude-code
        ts = [t for k, t in seen.items() if "claude" in k and ("claude-code" in k) != chat]
        return max(ts, default=None)

    rows = []
    paths = claude_desktop_config_paths() if desktop_configs is None else desktop_configs
    have = [str(p) for p in paths if _has_cortex(p)]
    name, t = "Claude Desktop (Chat)", last(True)
    if not paths:
        rows.append(("PASS", name, "not installed", ""))
    elif not have:
        rows.append(("WARN", name, f"Cortex is not in {paths[0]}, so its chats are not "
                     "logged", "Tick Claude Desktop under Connect in the Cortex app (or run "
                     "`cortex setup`), then quit Claude Desktop from the tray and open it again."))
    else:
        rows.append(("PASS" if t else "WARN", name, f"Cortex in {'; '.join(have)}; {_when(t)}",
                     "" if t else "Quit Claude Desktop from the tray, open it again and start "
                     "a new chat; if no call shows up, see docs/INSTALL.md (Troubleshooting)."))
    home = Path.home()
    settings = settings or home / ".claude" / "settings.json"
    plugin = plugin_enabled(settings)
    entry = _has_cortex(home / ".claude.json")
    hook = plugin or "prime-hook" in json.dumps(_read_json(settings).get("hooks") or {})
    name, t = "Claude Code", last(False)
    if not (home / ".claude").is_dir() and not (home / ".claude.json").is_file():
        rows.append(("PASS", name, "not installed", ""))
        return rows
    how = "plugin" if plugin else "~/.claude.json" if entry else \
        "the desktop config only (its Code tab)" if have else ""
    detail = (f"Cortex from {how}" if how else "Cortex not configured") + \
        f"; prime hook {'installed' if hook else 'missing'}; {_when(t)}"
    ok = bool(how) and hook and t
    rows.append(("PASS" if ok else "WARN", name, detail, "" if ok else
                 "Tick Claude Code under Connect in the Cortex app: it installs the Cortex "
                 "plugin, which brings the server and the prime hook. Its chats are saved "
                 "from their transcripts either way."
                 if not (plugin and hook) else
                 "Restart Claude Code and start a new chat; if no call shows up, see "
                 "docs/INSTALL.md (Troubleshooting)."))
    return rows


def check_duplicate_servers(settings: Path | None = None,
                            desktop_configs: list[Path] | None = None
                            ) -> tuple[str, str, str, str]:
    """WARN when the Claude Code plugin and a desktop-config entry both register Cortex."""
    name = "One Cortex server per client"
    plugin_on = plugin_enabled(settings)
    desktop = [str(p) for p in (claude_desktop_config_paths() if desktop_configs is None
                                else desktop_configs) if _has_cortex(p)]
    if plugin_on and desktop:
        return ("WARN", name, "the Claude Code plugin and the desktop config "
                f"({'; '.join(desktop)}) both register Cortex; the desktop app's Code tab loads "
                "both, so every tool and session is duplicated there",
                "Keep both if you use them: Claude Chat needs the desktop entry and the Claude "
                "Code CLI needs the plugin. In the desktop Code tab, expect two copies.")
    return "PASS", name, "plugin" if plugin_on else ("desktop config" if desktop else "none"), ""
