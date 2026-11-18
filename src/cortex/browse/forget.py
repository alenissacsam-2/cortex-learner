"""Re-filtering and forgetting stored browsing (v7 review C2).

The browsers forget old history, so a rule only reaches everything Cortex stored when every
store is filtered again: the private day pages on disk, the per-day site counts
(`site-days.json`), the extension inbox and its archive, the local model's site cache
(`sites.json`) and the two vector caches. `purge` takes out what a `Rule` matches:
`Rule.of(settings)` after a settings change (skip categories, deny and allow lists; run by
`build.run` when the fingerprint changed) and `Rule(hosts=...)` / `Rule(cats=...)` for
`cortex browse forget --site/--category`. Writes go through `cortex.vault`; counts only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlsplit

from cortex.browse import privacy, sites
from cortex.browse.privacy import Settings
from cortex.vault import Vault, VaultError

log = logging.getLogger(__name__)
ARCHIVE = ".cortex/browse/inbox-archive"  # the consumed extension inbox (review C5)
# a private day page's search line names its engine, not its host
ENGINE_HOSTS = {"google": "google.com", "bing": "bing.com", "duckduckgo": "duckduckgo.com",
                "brave": "search.brave.com", "youtube": "youtube.com",
                "perplexity": "perplexity.ai", "amazon": "amazon.com", "github": "github.com"}
_DUR = re.compile(r"^(?:\d+ h)?\s*(?:\d+ min)?$")
_PDAY = re.compile(r"^browse-private-\d{4}-\d{2}-\d{2}\.md$")


@dataclass(frozen=True)
class Rule:
    """What goes: a host under `hosts` (a domain covers its subdomains) or the settings'
    deny/allow lists, and text or a site in a category of `cats`."""
    hosts: tuple[str, ...] = ()
    cats: frozenset[str] = frozenset()
    settings: Settings | None = None

    @classmethod
    def of(cls, settings: Settings) -> Rule:
        return cls(cats=frozenset(settings.skip_categories), settings=settings)

    def host(self, host: str) -> bool:
        host = (host or "").lower().rstrip(".").removeprefix("www.")
        s = self.settings
        return bool(host) and (privacy.in_domains(host, self.hosts) or (s is not None and (
            privacy.in_domains(host, s.deny)
            or bool(s.allow and not privacy.in_domains(host, s.allow)))))

    def text(self, host: str, *texts: str) -> bool:
        if self.host(host):
            return True
        text = " ".join(t for t in texts if t).replace("\\", "")
        return bool(self.cats) and privacy.category(host or "", "/", text)[0] in self.cats

    def site(self, site: str, cat: str, llm: dict) -> bool:
        return (self.host(sites.host_of(site)) or cat in self.cats
                or (bool(self.cats) and sites.site_category(site, llm) in self.cats))

    def record(self, r: dict) -> bool:
        """An extension inbox record: True when it goes."""
        url, title = str(r.get("url") or ""), str(r.get("title") or "")
        channel = str(r.get("channel") or "")
        try:
            host = urlsplit(url).hostname or ""
        except ValueError:
            return True
        if self.text(host, title, channel):
            return True
        return self.settings is not None and privacy.filter_record(
            url, title, settings=self.settings, channel=channel) is None


def _days(vault: Vault, rule: Rule, llm: dict, gone_titles: set[str]) -> Counter:
    """Take the rule's keys and rows out of site-days.json."""
    c: Counter = Counter()
    days, bad = sites.load_days(vault)
    if bad:
        c["site_days_set_aside"] += 1
    for keys in days.values():
        if not isinstance(keys, dict):
            continue
        for key in list(keys):
            site, cat, _ = sites._split(key)
            a = keys[key]
            if rule.site(site, cat, llm) or not isinstance(a, dict):
                for t, *_ in ((a or {}).get("p") or {}).values() if isinstance(a, dict) else ():
                    gone_titles.add(sites._h(t))
                del keys[key]
                c["site_keys"] += 1
                continue
            for url, row in list((a.get("p") or {}).items()):
                if rule.text(urlsplit(url).hostname or "", row[0]):
                    del a["p"][url]
                    gone_titles.add(sites._h(row[0]))
                    a["n"], a["s"] = max(0, a["n"] - row[1]), max(0, a["s"] - row[2])
                    c["site_rows"] += 1
            for t in [t for t in (a.get("q") or {}) if rule.text("", t)]:
                a["n"] = max(0, a["n"] - a["q"].pop(t))
                c["site_rows"] += 1
            for terms in (a.get("r") or {}).values():
                for t in [t for t in terms if rule.text("", t)]:
                    del terms[t]
                    c["site_rows"] += 1
            if a["n"] <= 0:
                del keys[key]
                c["site_keys"] += 1
    for d in list(days):
        keys = days[d]
        if not isinstance(keys, dict) or not keys:
            del days[d]
            continue
        for a in keys.values():  # a reference to a key gone from that day goes too
            for f in ("r", "co"):
                ref = a.get(f)
                if isinstance(ref, dict):
                    for k in [k for k in ref if k not in keys or not ref[k]]:
                        del ref[k]
    if c["site_keys"] or c["site_rows"]:
        sites._save(vault, sites.DAYS, days)
    return c


def _fields(line: str) -> list[str]:
    return line[2:].split(" · ")


def _line_goes(rule: Rule, section: str, line: str) -> bool:
    """A private day page line (cortex.browse.build.private_body) the rule matches."""
    if not line.startswith("- ") or line in ("- none",) or line.startswith("- and "):
        return False
    f = _fields(line)
    if section == "## Topics" and len(f) >= 3:
        if f[-1].endswith(" visits") or f[-1].endswith(" visit"):
            f = f[:-1]
        return len(f) >= 3 and rule.text(f[-2], " · ".join(f[:-2]))
    if section == "## Searches" and len(f) >= 3:
        return rule.text(ENGINE_HOSTS.get(f[-1], ""), " · ".join(f[1:-1]))
    if section == "## Videos" and len(f) >= 3:
        return rule.text("youtube.com", " · ".join(f[1:-1]))
    if section in ("## Top sites", "## Sites I return to") and f:
        return rule.text(f[0])  # a sensitive category's own host counts
    return False


def _page(rule: Rule, body: str) -> tuple[str, int]:
    """(the body without the rule's lines, lines taken out). Topic headings left with no line
    go; when anything went, the local model's summary and session lines go too (they were
    made from every title) and the summary is redone from the topic headings."""
    out, section, n = [], "", 0
    for line in body.splitlines():
        if line.startswith("## "):
            section = line
        if _line_goes(rule, section, line):
            n += 1
            continue
        if section == "## Topics" and line.startswith("### ") and " · " in line:
            head, took = line[4:].rsplit(" · ", 1)
            if rule.text(head if "." in head and " " not in head else "", head):
                line, n = f"### Other · {took}", n + 1  # a cluster label made of its titles
        out.append(line)
    if not n:
        return body, 0
    kept, section = [], ""
    for i, line in enumerate(out):  # a "### head · time" with no item under it goes
        if line.startswith("## "):
            section = line
        if section == "## Topics" and line.startswith("### "):
            nxt = next((x for x in out[i + 1:] if x.startswith(("- ", "#"))), "")
            if not nxt.startswith("- "):
                continue
        kept.append(line)
    heads = [ln[4:] for ln in _section(kept, "## Topics") if ln.startswith("### ")]
    summary = ", ".join(f"{h.rsplit(' · ', 1)[0]} ({h.rsplit(' · ', 1)[-1]})"
                        for h in heads[:3] if " · " in h) or "nothing kept"
    from cortex.browse.build import SUMMARY_HEAD

    body = "\n".join(SUMMARY_HEAD + summary if ln.startswith(SUMMARY_HEAD) else ln
                     for ln in _drop_section(kept, "## Sessions"))
    return body, n


def _section(lines: list[str], head: str) -> list[str]:
    out, inside = [], False
    for ln in lines:
        if ln.startswith("## "):
            inside = ln == head
        elif inside:
            out.append(ln)
    return out


def _drop_section(lines: list[str], head: str) -> list[str]:
    out, inside = [], False
    for ln in lines:
        if ln.startswith("## "):
            inside = ln == head
        if not inside:
            out.append(ln)
    return out


def _pages(vault: Vault, rule: Rule) -> Counter:
    """Filter every private day page on disk (the visible day pages hold hub links and counts
    only, and the visible site pages are redone from site-days.json)."""
    from cortex.browse.build import PRIVATE_DIR, _page_text

    c: Counter = Counter()
    folder = vault.root / PRIVATE_DIR
    for p in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        if not _PDAY.match(p.name):
            continue
        try:
            note = vault.read_path(p)
        except VaultError:
            continue
        body, n = _page(rule, note.body)
        if not n:
            continue
        fields = {k: v for k, v in note.fields.items() if k != "browse_llm"}
        vault.write_text(p, _page_text(fields, body))
        c["day_pages"] += 1
        c["day_lines"] += n
    return c


def _inbox(vault: Vault, rule: Rule) -> Counter:
    """Filter the extension's inbox files and their archive, record by record; a record the
    settings keep private loses its excerpt."""
    c: Counter = Counter()
    base = vault.root / ".cortex" / "browse"
    files = [*sorted(base.glob("inbox-*.jsonl")), *sorted((vault.root / ARCHIVE).glob("*.jsonl"))]
    with vault.lock():  # the receiver appends under the same lock
        for p in files:
            try:
                lines = p.read_bytes().decode("utf-8", "replace").splitlines()
            except OSError:
                continue
            keep, gone, blanked = [], 0, 0
            for line in lines:
                try:
                    r = json.loads(line)
                except ValueError:
                    keep.append(line)
                    continue
                if not isinstance(r, dict) or rule.record(r):
                    gone += 1
                    continue
                if rule.settings is not None and r.get("excerpt"):
                    k = privacy.filter_record(str(r.get("url")), str(r.get("title") or ""),
                                              settings=rule.settings,
                                              channel=str(r.get("channel") or ""))
                    if k is not None and k.private:
                        r["excerpt"] = ""
                        line = json.dumps(r, ensure_ascii=False)
                        blanked += 1
                keep.append(line)
            c["inbox_excerpts"] += blanked
            if gone or blanked:
                if keep:
                    vault.write_text(p, "\n".join(keep) + "\n")
                else:
                    vault.remove(p)
                c["inbox_records"] += gone
    return c


def _cache(vault: Vault, rule: Rule, gone_titles: set[str]) -> Counter:
    """The local model's site entries: a site the rule matches goes; a description made from
    a title that went goes; a related site that went is unlinked."""
    c: Counter = Counter()
    cache = sites.load_cache(vault)
    llm = cache["llm"]
    gone = {s for s in llm if rule.site(s, "", llm)}
    for s in gone:
        del llm[s]
        c["cache_sites"] += 1
    for entry in llm.values():
        if not isinstance(entry, dict):
            continue
        for side in ("h", "v"):
            e = entry.get(side)
            if not isinstance(e, dict):
                continue
            if set(e.get("fed") or []) & gone_titles:
                del entry[side]
                c["cache_descriptions"] += 1
            elif any(r in gone for r in e.get("related") or []):
                e["related"] = [r for r in e["related"] if r not in gone]
    if c:
        sites._save(vault, sites.CACHE, {"llm": llm, "hashes": cache["hashes"]})
    return c


def prune_vectors(vault: Vault) -> Counter:
    """Drop the vector rows of pages that changed or went, with no model needed: the private
    browse vectors and the main index's cache (a changed page is embedded again later)."""
    from cortex import semantic
    from cortex.browse import vectors
    from cortex.catalog import Catalog
    from cortex.index import Index

    c: Counter = Counter({"vectors": vectors.prune(vault)})
    old = semantic._read(vault.root)
    if old:
        idx = Index(vault, Catalog.load(vault))
        pages = semantic._pages(idx)
        keep = semantic._merge(old, {}, pages)
        if len(keep) < len(old):
            dim = next(iter(old.values()))[3].shape[1]
            semantic._save(idx, pages, keep, semantic._sig(vault.root), dim)
            c["vectors"] += len(old) - len(keep)
    return c


PRIVATE_IGNORE = "browse/private/"
UNTRACK_MESSAGE = "cortex: stop tracking private browse pages"


def _git_env(top: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    from cortex.session import GIT_TIMEOUT_S

    try:
        return subprocess.run(["git", "--literal-pathspecs", "-C", top, *args],
                              capture_output=True, encoding="utf-8", env=env,
                              timeout=GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 1, "", "timeout")


def untrack_private(vault: Vault) -> bool:
    """C3: browse/private/ stays out of the vault's git. The .gitignore line is added when
    missing (an older vault), and private pages still in the index leave it once: a commit
    made on a scratch index (HEAD less browse/private, so nothing else the owner staged is
    swept in), then the same removal in the real index; the files stay on disk. History is
    never rewritten (`cortex doctor` warns and says how). True when a commit was made."""
    from cortex.session import _gitz, git_busy

    root = vault.root
    ignore = root / ".gitignore"
    if (root / ".git").exists() or ignore.is_file():
        old = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
        if PRIVATE_IGNORE not in old.splitlines():
            vault.write_text(ignore, old + ("" if old.endswith("\n") or not old else "\n")
                             + PRIVATE_IGNORE + "\n")
    tracked = _gitz(root, "ls-files", "-z", "--", "browse/private")
    if tracked.returncode or not tracked.stdout.strip("\0"):
        return False
    with vault.lock():
        if git_busy(root):
            return False
        top = _gitz(root, "rev-parse", "--show-toplevel").stdout.strip()
        spec = _gitz(root, "rev-parse", "--show-prefix").stdout.strip() + "browse/private"
        made = False
        if _gitz(top, "rev-parse", "--verify", "-q", "HEAD").returncode == 0:
            with tempfile.TemporaryDirectory() as d:
                env = {**os.environ, "GIT_INDEX_FILE": os.path.join(d, "index")}
                head = _gitz(top, "rev-parse", "HEAD").stdout.strip()
                steps = [("read-tree", "HEAD"), ("rm", "-r", "-q", "--cached", "--", spec)]
                if any(_git_env(top, *a, env=env).returncode for a in steps):
                    return False
                tree = _git_env(top, "write-tree", env=env)
                new = _git_env(top, "commit-tree", tree.stdout.strip(), "-p", head, "-m",
                               UNTRACK_MESSAGE, env=env)
                if tree.returncode or new.returncode or _gitz(
                        top, "update-ref", "-m", UNTRACK_MESSAGE, "HEAD", new.stdout.strip(),
                        head).returncode:
                    log.warning("browse: private pages not untracked: %s",
                                (new.stderr or tree.stderr).strip()[:200])
                    return False
                made = True
        _gitz(top, "rm", "-r", "-q", "--cached", "--", spec)
    return made


def private_in_history(root) -> int:
    """How many commits of the vault's history hold a browse/private/ file (0: none, or no
    repo)."""
    from cortex.session import _gitz

    r = _gitz(root, "log", "--all", "--format=%h", "--", "browse/private")
    return 0 if r.returncode else len(r.stdout.split())


def purge(vault: Vault, rule: Rule, *, settings: Settings | None = None,
          write: bool = True, say=None) -> Counter:
    """Take what `rule` matches out of every store. `write`: then redo the site pages and the
    browse index (with `settings`, default the saved ones) and prune the vectors; `build.run`
    passes False and does those itself. Counts only."""
    say = say or (lambda s: None)
    gone_titles: set[str] = set()
    llm = sites.load_cache(vault)["llm"]
    c = _days(vault, rule, llm, gone_titles)
    c.update(_pages(vault, rule))
    c.update(_inbox(vault, rule))
    c.update(_cache(vault, rule, gone_titles))
    if write:
        from cortex.browse import build, load_settings

        settings = settings or load_settings()
        c.update({k: v for k, v in sites.write(vault, settings, say=say).items()
                  if k == "sites_removed"})
        build.write_index(vault, visible=settings.visible_to_claude)
        c.update(prune_vectors(vault))
    return +c
