import datetime as dt
import os
import shutil
from pathlib import Path

import pytest

from keepfold import catalog as catalog_mod
from keepfold.catalog import Catalog, inbox_add, slugify
from keepfold.events import local_day
from keepfold.vault import Vault, VaultError

FIXTURE = Path(__file__).parent / "fixtures" / "vault_mini"
REAL = Path(os.environ.get("KEEPFOLD_REAL_VAULT", "/nonexistent-real-vault"))


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    return Vault(root)


@pytest.fixture
def cat(vault):
    return Catalog.load(vault)


def test_load_hierarchy(cat):
    assert cat.entries["dsa"].kind == "domain"
    assert cat.entries["dsa.arrays"].parent == "dsa"
    assert cat.entries["two-pointers"].requires == ["arrays-basics"]
    assert cat.entries["two-pointers"].domain == "dsa"
    assert "two-pointers" in cat.children("dsa.arrays")
    assert cat.children("dsa") == ["dsa.arrays"]


def test_missing_catalog_is_empty(vault):
    (vault.root / "catalog.yaml").unlink()
    assert Catalog.load(vault).entries == {}


@pytest.mark.parametrize("name", ["two-pointers", "Two Pointers", "2 POINTERS",
                                  "two-pointer technique", " two_pointers "])
def test_resolve(cat, name):
    assert cat.resolve(name) == "two-pointers"


def test_resolve_kinds(cat):
    assert cat.resolve("dsa", kinds=("domain",)) == "dsa"
    with pytest.raises(VaultError):
        cat.resolve("dsa")  # not a concept


def test_unknown_did_you_mean(cat):
    with pytest.raises(VaultError) as ei:
        cat.resolve("two pointer")
    assert ei.value.code == "unknown_concept"
    assert ei.value.did_you_mean[0] == "two-pointers"
    assert len(ei.value.did_you_mean) <= 3
    with pytest.raises(VaultError) as ei:
        cat.resolve("quantum chromodynamics")
    assert ei.value.did_you_mean == []


def test_add_concept_writes_catalog_and_note(vault, cat):
    id = cat.add("concept", "Sliding window", parent="Arrays", aliases=["window"],
                 requires=["two pointers"])
    assert id == "sliding-window"
    again = Catalog.load(vault)
    assert again.entries[id].parent == "dsa.arrays"
    assert again.entries[id].requires == ["two-pointers"]
    note = vault.read("concept", id)
    assert note.fields["parent"] == "[[dsa.arrays]]"
    assert note.fields["domain"] == "dsa"
    assert list(note.fields["requires"]) == ["[[two-pointers]]"]
    assert note.body.startswith("# Sliding window")
    text = (vault.root / "catalog.yaml").read_text()
    assert text.startswith("# Synthetic test catalog")  # comment kept
    assert '            aliases: ["window"]\n            requires: ["two-pointers"]\n' in text


def test_add_domain_and_area(vault, cat):
    d = cat.add("domain", "Operating systems", aliases=["OS"])
    a = cat.add("area", "Scheduling", parent="OS")
    c = cat.add("concept", "Round robin", parent=a)
    assert (d, a, c) == ("operating-systems", "operating-systems.scheduling", "round-robin")
    again = Catalog.load(vault)
    assert again.entries[c].domain == d
    assert vault.read("domain", d).fields["kind"] == "domain"
    area = vault.read("area", a).fields
    assert (area["kind"], area["domain"], area["parent"]) == ("area", d, "[[operating-systems]]")


def test_add_unchanged_catalog_round_trips(vault, cat):
    before = (vault.root / "catalog.yaml").read_text()
    cat.add("domain", "Networks")
    after = (vault.root / "catalog.yaml").read_text()
    assert after.startswith(before)


def test_duplicate_and_force(vault, cat):
    with pytest.raises(VaultError) as ei:
        cat.add("concept", "Two-Pointers technique!", parent="dsa.arrays")
    assert ei.value.code == "duplicate_suggested"
    assert ei.value.did_you_mean == ["two-pointers"]
    with pytest.raises(VaultError) as ei:
        cat.add("concept", "Two pointers", parent="dsa.arrays")  # same slug
    assert ei.value.code == "duplicate_suggested"
    assert cat.add("concept", "Two pointers", parent="dsa.arrays", force=True) == \
        "two-pointers-2"
    assert vault.read("concept", "two-pointers-2")


def test_area_same_title_other_domain_is_fine(cat):
    cat.add("domain", "Mathematics")
    assert cat.add("area", "Arrays", parent="mathematics") == "mathematics.arrays"


@pytest.mark.parametrize("kind,parent", [("concept", "nope"), ("concept", "dsa"),
                                         ("area", "dsa.arrays"), ("area", None),
                                         ("domain", "dsa")])
def test_bad_parent(vault, cat, kind, parent):
    with pytest.raises(VaultError):
        cat.add(kind, "Fresh thing", parent=parent)
    assert not (vault.root / "concepts" / "fresh-thing.md").exists()
    assert "fresh-thing" not in Catalog.load(vault).entries


def test_alias(vault, cat):
    cat.alias("two-pointers", "2ptr")
    assert Catalog.load(vault).resolve("2PTR") == "two-pointers"
    assert "2ptr" in vault.read("concept", "two-pointers").fields["aliases"]
    cat.alias("two-pointers", "2ptr")  # idempotent
    assert Catalog.load(vault).entries["two-pointers"].aliases.count("2ptr") == 1
    with pytest.raises(VaultError):
        cat.alias("arrays-basics", "2ptr")  # owned by another concept
    cat.alias("dsa.arrays", "lists")  # area without an aliases key
    assert Catalog.load(vault).resolve("Lists", kinds=("area",)) == "dsa.arrays"


def test_merge(vault, cat):
    cat.merge("arrays-basics", "two-pointers")
    again = Catalog.load(vault)
    assert "arrays-basics" not in again.entries
    assert again.resolve("Array basics") == "two-pointers"
    assert again.entries["two-pointers"].requires == []  # no self-requirement
    src = vault.read("concept", "arrays-basics")  # note kept
    assert src.fields["merged_into"] == "[[two-pointers]]"
    assert "Array basics" in vault.read("concept", "two-pointers").fields["aliases"]
    with pytest.raises(VaultError):
        cat.merge("two-pointers", "two-pointers")


def test_inbox_add(vault):
    assert inbox_add(vault, "Segment Trees", "from a quiz") == "inbox:segment-trees"
    inbox_add(vault, "Segment Trees", "again")  # no duplicate line
    inbox_add(vault, "Fenwick")
    today = local_day(dt.datetime.now(dt.UTC))
    text = (vault.root / "inbox.md").read_text()
    assert text == (f"# Inbox\n\n- [ ] Segment Trees (from a quiz, {today})\n"
                    f"- [ ] Fenwick ({today})\n")
    (vault.root / "inbox.md").unlink()
    inbox_add(vault, "Tries")
    assert (vault.root / "inbox.md").read_text().endswith(f"- [ ] Tries ({today})\n")


def test_inbox_add_keeps_an_obsidian_edit_made_meanwhile(vault, monkeypatch):
    """B13: the same unchecked read-modify-write of inbox.md as the server's proposals."""
    from test_vault import racer
    inbox_add(vault, "Tries")
    racer(monkeypatch, "inbox.md", b"\n## Mine\n- typed in Obsidian\n")
    inbox_add(vault, "Heaps")
    text = (vault.root / "inbox.md").read_text()
    assert "- typed in Obsidian" in text and text.index("Heaps") < text.index("## Mine")


@pytest.mark.parametrize("title,slug", [
    ("Two Pointers", "two-pointers"), ("  Kadane's algorithm ", "kadane-s-algorithm"),
    ("C++", "c-plus-plus"), ("C#", "c-sharp"), ("Node.js", "node-js"),
    ("Café résumé", "cafe-resume"), ("!!!", ""), ("x" * 100, "x" * 81),
    ("a" * 80 + " b", "a" * 80),
])
def test_slugify(title, slug):
    assert slugify(title) == slug


@pytest.mark.skipif(not (REAL / "catalog.yaml").is_file(), reason="no learner vault")
def test_real_catalog_smoke():
    """Read-only: never write to the learner's vault."""
    cat = Catalog.load(Vault(REAL))
    concepts = [e for e in cat.entries.values() if e.kind == "concept"]
    assert concepts
    for e in concepts:
        assert e.parent in cat.entries and cat.entries[e.parent].kind == "area", e.id
    for e in cat.entries.values():
        if e.kind == "area":
            assert cat.entries[e.parent].kind == "domain", e.id


HAND_NOTE = ("---\ntitle: Prefix sums\nparent: \"[[dsa.arrays]]\"\naliases: [running sum]\n"
             "requires: [\"[[arrays-basics]]\", \"[[not-yet-added]]\"]\n---\n"
             "# Prefix sums\n\nMine.\n")


def test_add_adopts_hand_made_concept(vault, cat):
    path = vault.root / "concepts/prefix-sums.md"
    path.write_bytes(HAND_NOTE.encode())
    id = cat.add("concept", "Prefix sums")  # no parent: the note's frontmatter has it
    assert id == "prefix-sums" and cat.adopted
    e = Catalog.load(vault).entries[id]
    assert (e.parent, e.domain, e.aliases, e.requires) == \
        ("dsa.arrays", "dsa", ["running sum"], ["arrays-basics"])
    assert path.read_bytes().decode() == HAND_NOTE  # the note is untouched
    with pytest.raises(VaultError) as ei:  # adopted once; now it is a normal duplicate
        cat.add("concept", "Prefix sums", parent="dsa.arrays")
    assert ei.value.code == "duplicate_suggested"
    cat.add("concept", "Kadane", parent="dsa.arrays")
    assert not cat.adopted


def test_add_refuses_id_used_by_another_kind(vault, cat):
    with pytest.raises(VaultError) as ei:
        cat.add("concept", "LC 1", parent="dsa.arrays", force=True)  # problems/lc-1.md
    assert ei.value.code == "invalid_id" and "lc-1-concept" in str(ei.value)
    assert not (vault.root / "concepts/lc-1.md").exists()
    assert "lc-1" not in Catalog.load(vault).entries


def test_inbox_add_uses_the_0400_rollover(vault, monkeypatch):  # §15.2 N11
    late = dt.datetime(2026, 11, 14, 2, 30).astimezone()  # 02:30 local: still the 13th

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return late.astimezone(tz)

    monkeypatch.setattr(catalog_mod, "dt", type("dt", (), {"datetime": Frozen, "UTC": dt.UTC}))
    inbox_add(vault, "Heaps", "fleeting")
    assert "- [ ] Heaps (fleeting, 2026-11-13)" in (vault.root / "inbox.md").read_text()
