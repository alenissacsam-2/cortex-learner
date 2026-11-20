"""Concept catalog: domain -> area -> concept, name resolution and dedup (SPEC §0 U2/U3, §4).

`catalog.yaml` is the machine-readable mirror of domains/, areas/ and concepts/. Every write
goes through `cortex.vault` (lock + atomic replace).
"""

from __future__ import annotations

import copy
import datetime as dt
import difflib
import io
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from cortex.events import local_day
from cortex.index import _edge_targets
from cortex.vault import ID_RE, Vault, VaultError, _to_yaml, is_private
from cortex.vault import _yaml as _vault_yaml

CATALOG = "catalog.yaml"
INBOX = "inbox.md"
DUP_RATIO = 0.85
CHILD_KEY = {"domain": "areas", "area": "concepts"}
PARENT_KIND = {"area": "domain", "concept": "area"}
KINDS = ("domain", "area", "concept")
SYNC_KEYS = ("adopted", "updated", "materialised", "skipped")
SYNC_STAMP = ".cortex/catalog-sync"
# path -> ((mtime_ns, size), data): in-process only. Readers share the data; every writer
# starts from a deep copy (_reload), so the cached tree is never mutated.
_LOADED: dict[Path, tuple[tuple[int, int], CommentedMap]] = {}


@dataclass
class Entry:
    id: str
    kind: str  # 'domain' | 'area' | 'concept'
    title: str
    aliases: list = field(default_factory=list)
    parent: str | None = None
    domain: str = ""
    requires: list = field(default_factory=list)
    areas: list = field(default_factory=list)  # concept only: extra areas beyond `parent`


def slugify(title: str) -> str:
    """Title to an id-safe slug (no dots: dots separate domain and area). '' if nothing is left."""
    s = str(title).replace("+", " plus ").replace("#", " sharp ").replace("&", " and ")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:81].rstrip("-")


def _yaml() -> YAML:
    y = _vault_yaml()
    y.indent(mapping=2, sequence=4, offset=2)  # the vault's catalog layout; round-trips exactly
    return y


def _strs(value) -> list[str]:
    return [str(v) for v in value or []]


class Catalog:
    def __init__(self, vault: Vault, data: CommentedMap | None = None):
        self.vault = vault
        self._data = data if data is not None else CommentedMap([("schema", 1),
                                                                 ("domains", CommentedSeq())])
        self.entries: dict[str, Entry] = {}
        self.adopted = False  # set by add()
        self._index()

    # ---- loading --------------------------------------------------------------

    @property
    def path(self):
        return self.vault.root / CATALOG

    @classmethod
    def load(cls, vault: Vault) -> Catalog:
        path = vault.root / CATALOG
        if not path.is_file():
            return cls(vault)
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
        hit = _LOADED.get(path)
        if hit is None or hit[0] != key:
            try:
                data = _yaml().load(path.read_bytes().decode("utf-8"))
            except Exception as e:
                raise VaultError("invalid_argument", f"{CATALOG} cannot be parsed: {e}") from e
            if data is None:
                return cls(vault)
            if not isinstance(data, CommentedMap):
                raise VaultError("invalid_argument", f"{CATALOG} is not a mapping.")
            data.setdefault("domains", CommentedSeq())
            hit = _LOADED[path] = (key, data)
        return cls(vault, hit[1])

    def _index(self) -> None:
        self.entries = {}
        for d in self._data.get("domains") or []:
            self._put(d, "domain", None, str(d["id"]))
            for a in d.get("areas") or []:
                self._put(a, "area", str(d["id"]), str(d["id"]))
                for c in a.get("concepts") or []:
                    self._put(c, "concept", str(a["id"]), str(d["id"]))

    def _put(self, node, kind, parent, domain) -> None:
        id = str(node["id"])
        # a domain's `areas` is its child list; only a concept's `areas` holds extra parents
        self.entries[id] = Entry(id, kind, str(node.get("title") or id), _strs(node.get("aliases")),
                                 parent, domain, _strs(node.get("requires")),
                                 _strs(node.get("areas")) if kind == "concept" else [])

    def _reload(self) -> None:
        """Fresh, private data from disk; every writer calls this before it edits."""
        self._data = copy.deepcopy(Catalog.load(self.vault)._data)
        self._index()

    def _save(self) -> None:
        buf = io.StringIO()
        _yaml().dump(self._data, buf)
        self.vault.write_text(self.path, buf.getvalue())
        self._index()

    def _node(self, id: str) -> tuple[CommentedSeq, CommentedMap]:
        """The (containing list, mapping) of `id` in the raw catalog data."""
        for d in self._data["domains"]:
            if str(d["id"]) == id:
                return self._data["domains"], d
            for a in d.get("areas") or []:
                if str(a["id"]) == id:
                    return d["areas"], a
                for c in a.get("concepts") or []:
                    if str(c["id"]) == id:
                        return a["concepts"], c
        raise VaultError("unknown_id", f"'{id}' is not in {CATALOG}.")

    # ---- queries --------------------------------------------------------------

    def _names(self, kinds, skip=()) -> list[tuple[str, str]]:
        """(casefolded name, id) for every id, title and alias of the given kinds, but none of
        the ids in `skip`."""
        out = []
        for e in self.entries.values():
            if e.kind in kinds and e.id not in skip:
                out += [(n.casefold(), e.id) for n in (e.id, e.title, *e.aliases)]
        return out

    def resolve(self, name: str, kinds=("concept",), skip=()) -> str:
        """The id `name` names. `skip`: ids treated as absent (an access-gated caller's hidden
        ids), so a hidden id fails exactly like an unknown one and is never suggested."""
        name = str(name).strip()
        e = self.entries.get(name)
        if e and e.kind in kinds and name not in skip:
            return name
        names = self._names(kinds, skip)
        key = name.casefold()
        # shortcut: a name shared by two entries resolves to the first in catalog order.
        for n, id in names:
            if n == key:
                return id
        slug = slugify(name)
        if slug:
            for n, id in names:
                if slugify(n) == slug:
                    return id
        code = "unknown_concept" if "concept" in kinds else "unknown_id"
        suggestions = self.suggest(name, kinds, skip=skip)
        hint = f"did you mean {', '.join(suggestions)}?" if suggestions else \
            "add it with cortex_add or check the spelling."
        raise VaultError(code, f"No {'/'.join(kinds)} named '{name}'; {hint}", suggestions)

    def suggest(self, name: str, kinds=("concept",), n: int = 3, skip=()) -> list[str]:
        names = self._names(kinds, skip)
        out: list[str] = []
        for hit in difflib.get_close_matches(str(name).casefold(), [m for m, _ in names], n=n * 4):
            for m, id in names:
                if m == hit and id not in out:
                    out.append(id)
        return out[:n]

    def area_hint(self) -> str:
        """Where a new concept can go: up to 8 non-private area ids, the id format, and the
        calls that make a domain and an area. Walks only domain and area notes (privacy)."""
        private = {n.id for k in ("domain", "area") for n in self.vault.iter_notes(k)
                   if is_private(n)}
        areas = [e.id for e in self.entries.values()
                 if e.kind == "area" and not {e.id, e.parent, e.domain} & private][:8]
        return (f"Existing areas: {', '.join(areas)}. " if areas else "No areas yet. ") + \
            "An area id is <domain>.<area>, e.g. dbms.database-design; a new one takes " \
            'cortex_add(kind="domain", title="DBMS") and then cortex_add(kind="area", ' \
            'title="Database design", parent="dbms"); then add the concept with ' \
            "parent=<area id>."

    def children(self, id: str) -> list[str]:
        return [e.id for e in self.entries.values() if e.parent == id]

    def _near_duplicates(self, kind: str, names: list[str], parent: str | None) -> list[str]:
        out = []
        for e in self.entries.values():
            # area ids carry their domain, so "Basics" may exist once per domain
            if e.kind != kind or (kind == "area" and e.parent != parent):
                continue
            for mine in names:
                if any(difflib.SequenceMatcher(None, mine.casefold(), o.casefold()).ratio()
                       >= DUP_RATIO or slugify(mine) == slugify(o) for o in (e.title, *e.aliases)):
                    out.append(e.id)
                    break
        return out

    # ---- writes ---------------------------------------------------------------

    def add(self, kind: str, title: str, parent: str | None = None, aliases=(), requires=(),
            force: bool = False, areas=()) -> str:
        if kind not in KINDS:
            raise VaultError("invalid_argument", "kind must be domain, area or concept.")
        if areas and kind != "concept":
            raise VaultError("invalid_argument", "Only a concept takes areas.")
        title = str(title).strip()
        aliases = [str(a).strip() for a in aliases if str(a).strip()]
        slug = slugify(title)
        if not slug:
            raise VaultError("invalid_argument", "The title needs at least one letter or digit.")
        with self.vault.lock():
            self._reload()
            # a concept note made by hand with no catalog entry: adopt it from its frontmatter
            adopt = kind == "concept" and slug not in self.entries and \
                self.vault.path_for(kind, slug).is_file()
            note_reqs, note_areas = [], []
            if adopt:
                f = self.vault.read(kind, slug).fields
                note_areas = _edge_targets(f.get("areas") or [])
                parent = (_edge_targets(f.get("parent")) or [parent])[0]
                title = str(f.get("title") or title)
                mine = f.get("aliases") or []
                mine = _strs(mine if isinstance(mine, list) else [mine])
                aliases = list(dict.fromkeys([*mine, *aliases]))
                note_reqs = _edge_targets(f.get("requires") or [])
            if kind == "domain":
                if parent is not None:
                    raise VaultError("invalid_argument", "A domain has no parent.")
            elif not parent:
                raise VaultError("invalid_argument", f"A {kind} needs a parent "
                                 f"{PARENT_KIND[kind]}." +
                                 (" " + self.area_hint() if kind == "concept" else ""))
            else:
                parent = self.resolve(parent, kinds=(PARENT_KIND[kind],))
            domain = self.entries[parent].domain if parent else None
            reqs = [self.resolve(r) for r in requires]
            for r in note_reqs:  # a hand-written requires may name a concept not added yet
                try:
                    reqs.append(self.resolve(r))
                except VaultError as e:
                    if e.code != "unknown_concept":
                        raise
            reqs = list(dict.fromkeys(reqs))
            areas = [self.resolve(a, ("area",)) for a in areas]
            areas += [a for a in note_areas if a in self.entries
                      and self.entries[a].kind == "area"]  # a hand-written area may be unknown
            areas = [a for a in dict.fromkeys(areas) if a != parent]
            prefix = f"{parent}." if kind == "area" else ""
            base = (prefix + slug)[:81].rstrip("-.")
            # adopting registers the existing note as is, so near-duplicates are not checked
            dups = [] if adopt else self._near_duplicates(kind, [title, *aliases], parent)
            if base in self.entries and base not in dups:
                dups.append(base)
            if dups and not force:
                raise VaultError("duplicate_suggested", f"'{title}' looks like "
                                 f"{', '.join(dups)}; use the existing one or pass force=true.",
                                 dups[:3])
            id, n = base, 2
            while id in self.entries:  # force on a taken slug: number it
                id = f"{base[:78].rstrip('-.')}-{n}"
                n += 1
            if not ID_RE.fullmatch(id):
                raise VaultError("invalid_id", f"'{id}' is not a valid id; change the title.")

            e = Entry(id, kind, title, aliases, parent, domain or "", reqs, areas)
            if not adopt:
                self._materialise(e)
            self._attach(_node_of(e), parent)
            self._save()
        self.adopted = adopt  # read by callers to say "adopted" instead of "added"
        return id

    def _attach(self, node: CommentedMap, parent: str | None) -> None:
        if parent is None:
            self._data["domains"].append(node)
            return
        pnode = self._node(parent)[1]
        key = CHILD_KEY[self.entries[parent].kind]
        if pnode.get(key) is None:
            pnode[key] = CommentedSeq()
        pnode[key].append(node)

    def _materialise(self, e: Entry) -> None:
        """Create the note for entry `e` (create_note refuses to overwrite one)."""
        fields: dict = {"title": e.title}
        if e.kind == "domain":
            fields.update(kind="domain", aliases=e.aliases)
        elif e.kind == "area":
            fields.update(kind="area", domain=e.domain, parent=f"[[{e.parent}]]")
            if e.aliases:
                fields["aliases"] = e.aliases
        else:
            fields.update(parent=f"[[{e.parent}]]", domain=e.domain, aliases=e.aliases,
                          requires=[f"[[{r}]]" for r in e.requires], mistakes=[])
            if e.areas:
                fields["areas"] = [f"[[{a}]]" for a in e.areas]
        self.vault.create_note(e.kind, e.id, fields, f"# {e.title}\n\n## Notes\n")

    def _sync(self) -> dict[str, list[str]]:
        """Make the catalog agree with the domain, area and concept notes; never deletes."""
        self._reload()
        report: dict[str, list[str]] = {k: [] for k in SYNC_KEYS}
        notes = {}
        for kind in KINDS:
            for note in self.vault.iter_notes(kind):
                if "merged_into" not in note.fields:  # merge took it out of the catalog
                    notes[note.id] = (kind, note.fields)
        changed, adopted = False, {}
        for kind in KINDS:  # parents first, so a hand-made area can hold a hand-made concept
            for id, (k, f) in notes.items():
                if k != kind:
                    continue
                e = self.entries.get(id)
                if e and e.kind != kind:
                    report["skipped"].append(f"{id}: the catalog has it as a {e.kind}")
                    continue
                parent = None
                if kind != "domain":
                    parent = (_edge_targets(f.get("parent")) or [None])[0]
                    if parent is None and e:
                        parent = e.parent
                    elif parent is None and kind == "area":  # areas/<domain>.<slug>.md
                        parent = str(f.get("domain") or id.partition(".")[0])
                    want = PARENT_KIND[kind]
                    if parent not in self.entries or self.entries[parent].kind != want:
                        report["skipped"].append(f"{id}: parent {parent or '(none)'} is not "
                                                 f"a known {want}")
                        continue
                title = str(f.get("title") or (e.title if e else id))
                areas = []
                if kind == "concept":  # frontmatter wins: no `areas` means none
                    areas = [a for a in dict.fromkeys(_edge_targets(f.get("areas") or []))
                             if a != parent and getattr(self.entries.get(a), "kind", "") == "area"]
                if e is None:
                    mine = f.get("aliases") or []
                    mine = _strs(mine if isinstance(mine, list) else [mine])
                    domain = self.entries[parent].domain if parent else id
                    node = _node_of(Entry(id, kind, title, mine, parent, domain, [], areas))
                    self._attach(node, parent)
                    adopted[id] = (node, f)
                    report["adopted"].append(id)
                elif (e.title, e.parent, e.areas) != (title, parent, areas):
                    seq, node = self._node(id)
                    node["title"] = _to_yaml(title)
                    if areas or "areas" in node:
                        node["areas"] = _to_yaml(areas)
                    if parent != e.parent:
                        seq.remove(node)
                        self._attach(node, parent)
                    report["updated"].append(id)
                else:
                    continue
                changed = True
            if changed:
                self._index()  # once per kind: the next kind looks up these as parents
        for id, (node, f) in adopted.items():  # requires last: they may name later adoptions
            if "requires" not in node:  # only concepts have requires
                continue
            reqs = []
            for r in _edge_targets(f.get("requires") or []):
                try:
                    reqs.append(self.resolve(r))
                except VaultError as err:
                    if err.code != "unknown_concept":
                        raise
            node["requires"] = _to_yaml([r for r in dict.fromkeys(reqs) if r != id])
        for e in list(self.entries.values()):
            try:
                if e.id in notes or self.vault.path_for(e.kind, e.id).is_file():
                    continue  # a note that does not parse is left for doctor, never replaced
                self._materialise(e)
            except VaultError as err:
                report["skipped"].append(f"{e.id}: {err}")
                continue
            report["materialised"].append(e.id)
        if changed:
            self._save()
        return report

    def alias(self, id: str, alias: str) -> None:
        alias = str(alias).strip()
        if not alias:
            raise VaultError("invalid_argument", "The alias is empty.")
        with self.vault.lock():
            self._reload()
            id = self.resolve(id, KINDS)
            e = self.entries[id]
            owner = next((i for n, i in self._names(KINDS) if n == alias.casefold()), None)
            if owner == id:
                return
            if owner:
                raise VaultError("invalid_argument", f"'{alias}' already names '{owner}'; "
                                 "merge them instead.")
            node = self._node(id)[1]
            node["aliases"] = _to_yaml([*e.aliases, alias])
            self._save()
            self._note_aliases(e.kind, id, [*e.aliases, alias])

    def _note_aliases(self, kind: str, id: str, aliases: list[str]) -> None:
        try:
            note = self.vault.read(kind, id)
        except VaultError:
            return  # catalog-only entry: nothing to mirror
        if "aliases" in note.fields:
            self.vault._edit_fields(kind, id, {"aliases": aliases})

    def merge(self, src: str, dst: str) -> None:
        """Fold concept `src` into `dst`. The caller appends the remap event."""
        with self.vault.lock():
            self._reload()
            src, dst = self.resolve(src), self.resolve(dst)
            if src == dst:
                raise VaultError("invalid_argument", "Cannot merge a concept into itself.")
            s, d = self.entries[src], self.entries[dst]
            seen = {d.title.casefold(), *(a.casefold() for a in d.aliases)}
            aliases = list(d.aliases)
            for n in (s.title, *s.aliases):
                if n.casefold() not in seen:
                    seen.add(n.casefold())
                    aliases.append(n)
            self._node(dst)[1]["aliases"] = _to_yaml(aliases)
            seq, node = self._node(src)
            seq.remove(node)
            for e in self.entries.values():  # keep requires pointing at live ids
                if src in e.requires and e.id != src:
                    new = []
                    for r in (dst if r == src else r for r in e.requires):
                        if r != e.id and r not in new:
                            new.append(r)
                    self._node(e.id)[1]["requires"] = _to_yaml(new)
            self._save()
            self._note_aliases("concept", dst, aliases)
            try:
                self.vault.read("concept", src)
            except VaultError:
                return
            self.vault._edit_fields("concept", src, {"merged_into": f"[[{dst}]]"})


def _node_of(e: Entry) -> CommentedMap:
    """The catalog.yaml mapping for a new entry."""
    node = CommentedMap([("id", _to_yaml(e.id)), ("title", _to_yaml(e.title))])
    if e.kind == "domain":
        node["aliases"] = _to_yaml(e.aliases)
        node["areas"] = CommentedSeq()
    elif e.kind == "area":
        if e.aliases:
            node["aliases"] = _to_yaml(e.aliases)
        node["concepts"] = CommentedSeq()
    else:
        node["aliases"] = _to_yaml(e.aliases)
        node["requires"] = _to_yaml(e.requires)
        if e.areas:
            node["areas"] = _to_yaml(e.areas)
    return node


def _signature(vault: Vault) -> str:
    """File count, newest mtime and total size of catalog.yaml and the catalog notes."""
    # shortcut: misses an edit that keeps the size and backdates the mtime; keep a per-file
    # stat map if that ever matters.
    paths = [vault.root / CATALOG]
    paths += [p for k in KINDS for p in (vault.root / vault.KIND_DIRS[k]).glob("*.md")]
    n = top = size = 0
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue
        n, top, size = n + 1, max(top, st.st_mtime_ns), size + st.st_size
    return f"{n} {top} {size}"


def sync(vault: Vault, if_changed: bool = False) -> dict[str, list[str]]:
    """Make catalog.yaml agree with domains/, areas/ and concepts/ (frontmatter wins).

    Adopts notes without entries, takes titles, parents and areas edited in Obsidian, and
    materialises a note for an entry that has none. Never deletes. With `if_changed`, returns
    at once when no catalog file or note changed since the last sync (a stat scan only)."""
    stamp = vault.root / SYNC_STAMP
    if if_changed:
        try:
            if stamp.read_bytes().decode() == _signature(vault):
                return {k: [] for k in SYNC_KEYS}
        except OSError:
            pass
    with vault.lock():
        report = Catalog.load(vault)._sync()
        vault.write_text(stamp, _signature(vault))
    return report


def inbox_add(vault: Vault, name: str, context: str = "") -> str:
    """Park an unknown concept name in inbox.md; returns its 'inbox:<slug>' object id."""
    name = " ".join(str(name).split())
    slug = slugify(name)
    if not slug:
        raise VaultError("invalid_argument", "The name needs at least one letter or digit.")
    context = " ".join(str(context).split())
    today = local_day(dt.datetime.now(dt.UTC))  # the 04:00 rollover, like the journal
    line = f"- [ ] {name} ({context}, {today})" if context else f"- [ ] {name} ({today})"

    def add(text: str) -> str:  # B13: reruns on the reread page if the owner edited it
        lines = text.rstrip("\n").split("\n")
        if any(ln.startswith(f"- [ ] {name} (") for ln in lines):
            return text
        # above the first `## ` section: lines appended at the end landed under
        # `## About me (unconfirmed)` and read as claims waiting for the user's yes
        cut = next((i for i, ln in enumerate(lines) if ln.startswith("## ")), len(lines))
        head, tail = lines[:cut], lines[cut:]
        while len(head) > 1 and not head[-1].strip():
            head.pop()
        head += ([] if head[-1].startswith("- [") else [""]) + [line]  # blank after prose
        return "\n".join(head + ([""] + tail if tail else [])) + "\n"

    vault.update_text(vault.root / INBOX, add, "# Inbox\n")
    return f"inbox:{slug}"


def resolve_or_inbox(vault: Vault, catalog: Catalog, name: str, context: str = "",
                     skip=()) -> str:
    """'concept:<id>' for a known name; otherwise park it in inbox.md and return 'inbox:<slug>'.
    `skip` as in Catalog.resolve: a hidden name is parked like an unknown one."""
    try:
        return f"concept:{catalog.resolve(name, skip=skip)}"
    except VaultError as e:
        if e.code != "unknown_concept":
            raise
        return inbox_add(vault, name, context)
