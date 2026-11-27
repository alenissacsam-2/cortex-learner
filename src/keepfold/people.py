"""v5 PHASE4 V4 people pages: `people/<slug>.md`, private by default, read by exact id.

`vault.is_private` treats a `person` note as private unless its frontmatter says
`private: false`, so the index, recall, vectors, the dashboard, `index.md` and lint never see
private ones; `keepfold_read(id="people/<slug>")` and `keepfold_as_me` reach every page through
`get`. A page with `private: false` is also found by search under its slug.
"""

from __future__ import annotations

import json
import re

from keepfold import relink
from keepfold.catalog import page_slug
from keepfold.vault import Vault, VaultError, invalid

PREFIX = "people/"
TIERS = ("close", "work", "public")
TAG_RE = re.compile(r"^[a-z0-9-]{1,40}$")
VOICE_FILES = ("voice/samples.jsonl", "voice/drafts.jsonl")  # rows carry `audience`
BODY = "# {title}\n\n## Who they are to me\n\n## How I write to them\n"


def get(vault: Vault, id: str) -> dict | None:
    """{id, title, relation, tier, body} for the exact id `people/<slug>`, else None."""
    if not isinstance(id, str) or not id.startswith(PREFIX):
        return None
    try:
        n = vault.read("person", id[len(PREFIX):])
    except VaultError:  # unknown, invalid or unparsable
        return None
    tier = str(n.fields.get("tier") or "work")
    return {"id": id, "title": str(n.fields.get("title") or n.id),
            "relation": str(n.fields.get("relation") or ""),
            "tier": tier if tier in TIERS else "work", "body": n.body.strip()}


def add(vault: Vault, title: str, fields: dict) -> str:
    """Create `people/<slug>.md` (private unless fields.private is false); returns its id
    `people/<slug>`."""
    extra = set(fields) - {"relation", "tier", "private"}
    if extra:
        raise invalid(f"Fields not allowed on a person: {', '.join(sorted(extra))}; allowed: "
                      "relation, tier, private.")
    private = fields.get("private", True)
    if not isinstance(private, bool):
        raise invalid("private must be true or false.")
    relation = fields.get("relation")
    if not isinstance(relation, str) or not TAG_RE.fullmatch(relation):
        raise invalid("A person needs fields.relation: a short tag like professor, friend, family "
                      "or colleague ([a-z0-9-], at most 40).")
    tier = fields.get("tier", "work")
    if tier not in TIERS:
        raise invalid(f"tier must be one of {', '.join(TIERS)}.")
    slug = page_slug(title)[:81].rstrip("-.")
    if not slug:
        raise invalid("The title needs at least one letter or digit.")
    vault.create_note("person", slug, {"title": title, "kind": "person", "relation": relation,
                                       "tier": tier, "private": private},
                      BODY.format(title=" ".join(title.split())))
    relink.apply(vault, relink.people_fixes(vault))  # hub, its profile link, the page's Up
    return PREFIX + slug


def forget(vault: Vault, id: str) -> dict:
    """Delete the page and the voice samples and drafts addressed to it, and every copy of
    its name and id Keepfold keeps (keepfold.forget). Git history keeps the old file (PHASE4
    decision 5; the receipt says how to remove it). Returns the receipt."""
    from keepfold import forget as forget_

    if get(vault, id) is None:
        raise VaultError("unknown_id", f"No people page '{id}'; use the exact id people/<slug>.")
    return forget_.page(vault, id)


def drop_voice(vault: Vault, id: str) -> dict:
    """The voice samples and drafts addressed to people page `id`, deleted: their counts."""
    out = {}
    with vault.lock():
        for rel, name in zip(VOICE_FILES, ("samples", "drafts"), strict=True):
            path = vault.root / rel
            if not path.is_file():
                out[name] = 0
                continue
            keep, gone = [], 0
            for line in path.read_bytes().decode("utf-8").splitlines(keepends=True):
                try:
                    row = json.loads(line)
                except ValueError:
                    row = None
                if isinstance(row, dict) and row.get("audience") == id:
                    gone += 1
                else:
                    keep.append(line)
            if gone:
                vault.write_text(path, "".join(keep))
            out[name] = gone
    return out
