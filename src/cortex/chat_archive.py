"""C5: imported AI chat pages live in archive/chats/ (vault.CHAT_ARCHIVE), a folder the vault's
.gitignore keeps out of git, so they are in no new commit and no push. They stay `resource`
pages (Vault.EXTRA_DIRS): iter_notes, the index, find, recall, Chat and the brain map read them
as before, a private one stays private, and ids (so `[[id]]` links) never change.

`move` takes the chat pages out of resources/ into the archive, through Vault.move (lock, temp
file, os.replace). A chat page is what chat_import calls one: a resource whose Timeline line
names its `Source:` app (topics.page_source, semantic.is_chat), or a page the automatic import
wrote (its `chat_saved` ledger, `auto: true`). In a git repo (git on in settings) their old
paths leave git's index (`git rm --cached`) in a commit of their own; history is never
rewritten. `undo` moves every page in the archive back and commits them.

`auto` runs the move once from the app's chat-import pass; MARKER records it. `--undo` clears
MARKER and sets `app.chats_archive: false`: the move never runs again, and new chat pages go to
resources/ until `cortex chats archive` turns it back on.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cortex import taint, topics
from cortex.vault import CHAT_ARCHIVE, ID_RE, Vault, VaultError

MARKER = ".cortex/chats-archived"
IGNORE = CHAT_ARCHIVE + "/"
MAIN = Vault.KIND_DIRS["resource"]
MESSAGE = "cortex: chat pages leave git (moved to archive/chats/)"
UNDO_MESSAGE = "cortex: chat pages back in resources/"


def enabled() -> bool:
    """`app.chats_archive` in ~/.cortex/config.yaml: on unless set to false (by `--undo`)."""
    a = taint.config().get("app")
    return not (isinstance(a, dict) and a.get("chats_archive") is False)


def folder() -> str | None:
    """Where chat_import writes a new chat page: CHAT_ARCHIVE, or None (resources/)."""
    return CHAT_ARCHIVE if enabled() else None


def _set(on: bool) -> None:
    taint.write_config(lambda cfg: {"app": {
        **(cfg["app"] if isinstance(cfg.get("app"), dict) else {}), "chats_archive": on}})


def ensure_ignored(vault: Vault) -> bool:
    """Add the archive's .gitignore line when missing (an older vault), where there is a repo or
    a .gitignore. True when added."""
    ignore = vault.root / ".gitignore"
    if not ((vault.root / ".git").exists() or ignore.is_file()):
        return False
    old = ignore.read_bytes().decode("utf-8") if ignore.is_file() else ""
    if IGNORE in old.splitlines():
        return False
    vault.write_text(ignore, old + ("" if old.endswith("\n") or not old else "\n")
                     + IGNORE + "\n")
    return True


def pages(vault: Vault, undo: bool = False) -> list[str]:
    """Ids of the pages `move` moves: the chat pages in resources/, or (`undo`) every page in
    the archive."""
    if undo:
        d = vault.root / CHAT_ARCHIVE
        return sorted(p.stem for p in d.glob("*.md") if ID_RE.fullmatch(p.stem)) \
            if d.is_dir() else []
    from cortex.chat_import import _ledger, _pid  # lazy: chat_import imports this module
    from cortex.events import EventLog

    auto = {_pid(e) for e in _ledger(EventLog(vault).iter_events()).values() if e.get("auto")}
    main = vault.root / MAIN
    return sorted(n.id for n in vault.iter_notes("resource") if n.path.parent == main
                  and (n.id in auto or topics.page_source(n.body)))


def move(vault: Vault, *, undo: bool = False, dry_run: bool = False) -> dict:
    """Move the chat pages (module doc). {"ids": moved (or, dry, to move), "kept": ids left
    where they are (the other folder holds a different page of that id, or the file is open
    in another program), "git": a line saying what git did ('' for nothing)}.
    VaultError("busy") while a chat import runs."""
    from cortex import chat_import

    if dry_run:
        return {"ids": pages(vault, undo), "kept": [], "git": ""}
    src, dst = (CHAT_ARCHIVE, MAIN) if undo else (MAIN, CHAT_ARCHIVE)
    with vault.job_lock(chat_import.LOCK, 5, chat_import.BUSY):  # no import writes meanwhile
        if not undo:
            ensure_ignored(vault)  # before the first page lands in the archive
        moved, kept = [], []
        for i in pages(vault, undo):
            try:
                vault.move(vault.root / src / f"{i}.md", vault.root / dst / f"{i}.md")
                moved.append(i)
            except VaultError:
                kept.append(i)
        git = _git(vault, moved, undo)
        if undo:
            vault.remove(vault.root / MARKER)
        else:
            vault.write_text(vault.root / MARKER,
                             datetime.now(UTC).isoformat(timespec="seconds") + "\n")
    _set(not undo)
    return {"ids": moved, "kept": kept, "git": git}


def auto(vault: Vault) -> dict | None:
    """The app's one-time move: None when it ran before (MARKER), or `--undo` turned it off."""
    if (vault.root / MARKER).exists() or not enabled():
        return None
    return move(vault)


def _git(vault: Vault, ids: list[str], undo: bool) -> str:
    """Archive: `git rm --cached` of the moved pages' tracked old paths, committed alone (what
    else is staged stays staged). Undo: the pages committed back. Only when the vault is a git
    repo and git is on in settings."""
    from cortex import session

    root = vault.root
    if not ids or not (root / ".git").exists():
        return ""
    if not session.git_choice()[0]:
        return "git is off in your settings: the vault's repo was left alone."
    if busy := session.git_busy(root):
        return f"git was left alone ({busy}); finish that in git, then commit the change."
    if undo:
        sha = session._commit(vault, UNDO_MESSAGE, [root / MAIN / f"{i}.md" for i in ids])
        return f"git: committed them back into the vault's repo ({sha[:7]})." if sha else ""
    with vault.lock():
        tracked = set(session._gitz(root, "ls-files", "-z", "--", MAIN).stdout.split("\0"))
        rels = [r for i in ids if (r := f"{MAIN}/{i}.md") in tracked]
        if not rels:
            return "git: none of them was tracked in the vault's repo."
        spec = "\0".join(rels)
        for args in (("rm", "--cached", "-q", "--ignore-unmatch"), ("commit", "-q", "-m", MESSAGE)):
            r = session._gitz(root, *args, "--pathspec-from-file=-", "--pathspec-file-nul",
                              input=spec)
            if r.returncode:
                return f"git {args[0]} failed: {r.stderr.strip()[:200]}"
    n = len(rels)
    return (f"git: ran `git rm --cached` on {n} page{'s' * (n != 1)} in the vault's repo and "
            f"committed it; older commits still hold them (history is never rewritten).")
