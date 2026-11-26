"""Watched folders (R18): a folder the owner adds once (`keepfold watch add`, or the app's Import
step) comes in by itself. The app's periodic pass (server.start's `auto` loop, after the live
browse import: every browse_serve.AUTO_EVERY_S) calls `run`, which re-imports each watched
folder through `sources.run` with its remembered source and tier: documents, notes, git
projects, calendars (a folder of .ics files, or one .ics file's folder: its month pages gain the
new lines only), or a Zotero library (keepfold.zotero).

WATCHED (.keepfold/watched.json) holds [{path, source, tier, last}]; `last` is the line of the last
pass that read something, failed or found the folder gone, and when. FILES
(.keepfold/watch-files.json) holds per folder {entry: signature}: a file's [mtime_ns, size], a
repo's reflog's, a Zotero item's hash (and "" the database's). An unchanged entry costs a stat,
never a read; at most PER_PASS new or changed ones are read per folder per pass, the rest on the
next. Removed files leave FILES; their pages stay.

The walk never follows a link or junction out of the folder, skips hidden and system files and
folders (more_sources.skip_folder's names too), files not on this computer (a cloud
placeholder: reading one downloads it), the vault and anything under it, and lists at most
MAX_LISTED files. The importers' size limits hold: their readers refuse a file over them. A
watched folder that is gone is reported (`missing`), kept, and never an error.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime
from functools import partial
from pathlib import Path

from keepfold import importers, more_sources, sources
from keepfold.vault import Vault, VaultError

WATCHED, FILES = ".keepfold/watched.json", ".keepfold/watch-files.json"
SOURCES = ("docs", "notes", "repos", "calendar", "zotero")
SUFFIXES = {"docs": more_sources.DOC_SUFFIXES + importers.NOTE_SUFFIXES,
            "notes": importers.NOTE_SUFFIXES, "calendar": (".ics",)}
PER_PASS = 200  # new or changed files (repos, Zotero items) read per folder per pass
MAX_LISTED = 100_000  # files of its kinds listed per folder per pass
# hidden, system, offline, and the cloud placeholders that download when read (recall on open
# or on data access: OneDrive's Files On-Demand)
SKIP_ATTRS = 0x2 | 0x4 | 0x1000 | 0x40000 | 0x400000
GONE = "Not found; kept, and checked again on the next pass."


def _json(vault: Vault, rel: str, empty):
    try:
        d = json.loads((vault.root / rel).read_text("utf-8"))
    except (OSError, ValueError):
        return empty
    return d if isinstance(d, type(empty)) else empty


def watched(vault: Vault) -> list[dict]:
    return [w for w in _json(vault, WATCHED, []) if isinstance(w, dict)
            and isinstance(w.get("path"), str) and w.get("source") in SOURCES
            and w.get("tier") in {a.value for a in sources.Audience}]


def _write(vault: Vault, rel: str, data) -> None:
    vault.write_text(vault.root / rel, json.dumps(data, indent=1) + "\n")


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(p))


def _under(p, root: str) -> bool:
    """Whether `p` is the folder `root` (normcased) or inside it."""
    p = _norm(p)
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def _folder(vault: Vault, path, source: str) -> Path:
    """The folder a watch reads: absolute, resolved, there, and not the vault or inside it."""
    if source == "zotero":
        from keepfold import zotero
        folder = zotero.find(path).resolve()
    else:
        p = Path(str(path or "").strip().strip("\"'")).expanduser()
        if not p.is_absolute():
            raise VaultError("invalid_argument", "Give the folder's full path, such as "
                             "C:\\Users\\you\\Documents\\Papers.")
        folder = p.resolve()
        if source == "calendar" and folder.suffix.lower() == ".ics" and folder.is_file():
            folder = folder.parent  # one exported calendar: its folder's .ics files are read
        if not folder.is_dir():
            raise VaultError("invalid_argument", f"{folder} is not a folder.")
    if _under(folder, _norm(vault.root.resolve())):
        raise VaultError("invalid_argument", "That is your vault, or a folder inside it: Keepfold "
                         "has it already.")
    return folder


def _source(source: str, folder: Path, todo: list) -> sources.Source:
    """The import of `todo` (walked entries) from `folder`, as `keepfold import` would make it."""
    if source == "zotero":
        from keepfold import zotero
        return zotero.source(folder, todo)
    if source == "docs":  # keyed by the watched folder and its path in it (`updates`): a
        src = more_sources.docs_source(folder, files=todo)  # changed document rewrites its page
        at = f"docs {_norm(folder)} "
        return replace(src, items=lambda: (
            # a document's origin is its full path, a .md/.txt note's its name in the folder
            replace(it, key=at + Path(folder, it.origin).relative_to(folder).as_posix())
            if isinstance(it, sources.Item) else it for it in src.items()))
    return {"notes": partial(importers.notes_source, files=todo),
            "repos": partial(more_sources.repos_source, repos=todo),
            "calendar": partial(importers.calendar_source, files=todo)}[source](folder)


def add(vault: Vault, path, source: str = "docs", tier: str | None = None,
        said=None) -> dict:
    """Watch `path` (for Zotero, None finds the library) as `source` at `tier` (a CLI word or
    an Audience value; None: the source's last choice, else its default). A given tier is
    remembered for the source and a stricter one reaches its earlier pages (sources.pick_tier;
    its line goes to `said`). Adding a watched folder again replaces its source and tier."""
    if source not in SOURCES:
        raise VaultError("invalid_argument", f"source must be one of {', '.join(SOURCES)}.")
    if tier is not None:
        try:
            sources.tier(tier)
        except ValueError as e:
            raise VaultError("invalid_argument", str(e)) from None
    folder = _folder(vault, path, source)
    t, _ = sources.pick_tier(vault, _source(source, folder, []), tier, save=tier is not None,
                             said=said)
    entry = {"path": str(folder), "source": source, "tier": t.value}
    with vault.lock():
        rows = watched(vault)
        old = next((w for w in rows if _norm(w["path"]) == _norm(folder)), None)
        _write(vault, WATCHED, [w for w in rows if w is not old] + [entry])
        if old and old["source"] != source:  # another source reads other files
            _forget(vault, old["path"])
    return entry


def remove(vault: Vault, path) -> bool:
    """Stop watching `path`; its pages stay. False when it was not watched."""
    with vault.lock():
        rows = watched(vault)
        gone = _norm(Path(str(path).strip().strip("\"'")).expanduser().resolve())
        keep = [w for w in rows if _norm(w["path"]) != gone]
        if len(keep) == len(rows):
            return False
        _write(vault, WATCHED, keep)
        for w in rows:
            if w not in keep:
                _forget(vault, w["path"])
    return True


def _forget(vault: Vault, folder: str) -> None:
    files = _json(vault, FILES, {})
    if files.pop(folder, None) is not None:
        _write(vault, FILES, files)


def _there(w: dict) -> bool:
    p = Path(w["path"])
    if w["source"] == "zotero":
        from keepfold import zotero
        return (p / zotero.DB).is_file()
    return p.is_dir()


def listing(vault: Vault) -> dict:
    """The app's rows: each watch with `missing`, and `tiers` (source -> its last tier choice,
    else the default) for the add form."""
    last = sources.remembered(vault)
    return {"rows": [{**w, "missing": not _there(w)} for w in watched(vault)],
            "tiers": {s: last.get(s, sources.Audience.CLOUD).value for s in SOURCES}}


def _skip(name: str, st) -> bool:
    return name.startswith(".") or bool(getattr(st, "st_file_attributes", 0) & SKIP_ATTRS)


def walk(folder: Path, vault_root: str, suffixes: tuple[str, ...]) -> tuple[dict, bool]:
    """({relative name: ([mtime_ns, size], an importers._files entry)}, whether the walk saw
    everything): files ending in `suffixes` under `folder`, by the rules in the module
    docstring. One stat per entry, from the folder listing."""
    out, whole, stack = {}, True, [str(folder)]
    cut = len(os.path.join(str(folder), ""))
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            whole = False  # an unreadable folder: its files are not gone
            continue
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
                if e.is_symlink() or e.is_junction() or _skip(e.name, st):
                    continue
                if e.is_dir(follow_symlinks=False):
                    if not more_sources.skip_folder(e.name) and not _under(e.path, vault_root):
                        stack.append(e.path)
                elif e.is_file(follow_symlinks=False) and e.name.lower().endswith(suffixes):
                    if len(out) >= MAX_LISTED:
                        return out, False
                    rel = e.path[cut:].replace(os.sep, "/")
                    # its own byte budget: a pass reads up to PER_PASS whole files
                    read = partial(importers._read, rel, st.st_size, partial(open, e.path, "rb"),
                                   [importers.MAX_TOTAL])
                    out[rel] = ([st.st_mtime_ns, st.st_size], (rel, st.st_size, st.st_mtime, read))
            except OSError:
                whole = False
    return out, whole


def _repos(folder: Path, vault_root: str) -> tuple[dict, bool]:
    """{relative path: (its reflog's [mtime_ns, size], the repo)} of `more_sources.find_repos`,
    less a repo reached through a link, junction, hidden or system folder, or the vault."""
    out = {}
    for repo in more_sources.find_repos(folder):
        rel = repo.relative_to(folder)
        steps = [folder.joinpath(*rel.parts[:i + 1]) for i in range(len(rel.parts))]
        try:
            if _under(repo, vault_root) or any(
                    os.path.islink(p) or os.path.isjunction(p) or _skip(p.name, p.lstat())
                    for p in steps):
                continue
        except OSError:
            continue
        git = repo / ".git"  # a commit appends to logs/HEAD
        for p in (git / "logs" / "HEAD", git / "HEAD", git):
            try:
                st = p.stat()
            except OSError:
                continue
            out[rel.as_posix()] = ([st.st_mtime_ns, st.st_size], repo)
            break
    return out, True


def _pass(vault: Vault, w: dict, old: dict) -> dict:
    """One watched folder: its new and changed entries through `sources.run`. {path, read,
    left, line} (`line`: the report, or why nothing was read)."""
    folder, source, out = Path(w["path"]), w["source"], {"path": w["path"], "read": 0, "left": 0}
    if not _there(w):
        return {**out, "missing": True, "line": GONE}
    if w["tier"] == sources.Audience.SKIP.value:
        return out
    quick = None
    vroot = _norm(vault.root.resolve())
    if source == "zotero":
        from keepfold import zotero
        quick = zotero.stat(folder)
        if quick is not None and old.get("") == quick:
            return out  # the database is as last time: nothing is copied
        entries, whole = {k: [s, r] for k, (s, r) in zotero.records(folder).items()}, True
    elif source == "repos":
        entries, whole = _repos(folder, vroot)
    else:
        entries, whole = walk(folder, vroot, SUFFIXES[source])
    todo = sorted(k for k, (sig, _) in entries.items() if old.get(k) != sig)
    batch = todo[:PER_PASS]
    files = dict(old) if not whole else {k: old[k] for k in entries if k in old}
    files.pop("", None)
    if batch:
        src = _source(source, folder, [entries[k][1] for k in batch])
        r = sources.run(vault, src, sources.Audience(w["tier"]))
        r.pop("gone", None)  # a pass reads the changed files only: the others are not gone
        out.update(read=len(batch), left=len(todo) - len(batch),
                   line=importers.report_line(src.label, r)
                   + (f"; {len(todo) - len(batch):,} more on the next pass" if todo[PER_PASS:]
                      else ""))
    files.update({k: entries[k][0] for k in batch})
    if quick is not None and not todo[PER_PASS:]:  # all read: the next pass checks the stat
        files[""] = quick
    if files != old:
        with vault.lock():
            now = _json(vault, FILES, {})
            if any(_norm(x["path"]) == _norm(w["path"]) for x in watched(vault)):  # not removed
                _write(vault, FILES, {**now, w["path"]: files})
    return out


def run(vault: Vault) -> list[dict]:
    """One pass over every watched folder (see the module docstring); a folder that fails is
    reported in its result and tried again on the next pass. The results, and each new line
    is kept as the folder's `last`."""
    old, out = _json(vault, FILES, {}), []
    for w in watched(vault):
        try:
            out.append(_pass(vault, w, old.get(w["path"], {})))
        except (VaultError, OSError, ValueError) as e:
            out.append({"path": w["path"], "read": 0, "left": 0, "line": f"Failed: {e}"})
    lines = {r["path"]: r for r in out if r.get("line")}
    if lines:
        at = datetime.now().isoformat(timespec="seconds")
        with vault.lock():
            rows, changed = watched(vault), False
            for x in rows:
                r = lines.get(x["path"])
                if r and (r["read"] or (x.get("last") or {}).get("line") != r["line"]):
                    x["last"], changed = {"at": at, "line": r["line"]}, True
            if changed:
                _write(vault, WATCHED, rows)
    return out
