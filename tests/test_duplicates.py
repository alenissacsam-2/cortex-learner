"""`cortex concept duplicates` (v6 agenda 8): read-only duplicate-pair suggestions."""

from conftest import FakeEmbed, git, note
from typer.testing import CliRunner

from cortex import duplicates, semantic
from cortex.catalog import Catalog
from cortex.cli import app
from cortex.vault import Vault


def _add(root, title, **kw):
    return Catalog.load(Vault(root)).add("concept", title, "dsa.arrays", force=True, **kw)


def test_finds_known_pair_skips_unrelated_and_writes_nothing(root_committed):
    root = root_committed
    _add(root, "Two pointer")
    note(root, "problems/lc-2.md", 'id: "lc-2"\ntitle: "Pair sum"\n'
         'concepts: ["[[two-pointer]]", "[[two-pointers]]"]\n', "# Pair sum")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "setup")
    head, before = git(root, "rev-parse", "HEAD"), {p: p.read_bytes() for p in root.rglob("*")
                                                    if p.is_file() and ".git" not in p.parts}

    r = CliRunner().invoke(app, ["concept", "duplicates", "--vault", str(root)])
    assert r.exit_code == 0, r.output
    assert "run: cortex concept merge two-pointer two-pointers" in r.output  # more problems wins
    assert "same area dsa.arrays" in r.output and "1 shared problem" in r.output
    assert "arrays-basics" not in r.output  # same area, shared problem, but different names
    assert "secret-diary" not in r.output and "Private concept" not in r.output
    assert "embedding cosine" not in r.output  # no model: no semantic evidence, no download

    assert git(root, "rev-parse", "HEAD") == head
    assert git(root, "status", "--porcelain") == ""
    assert {p: p.read_bytes() for p in root.rglob("*")
            if p.is_file() and ".git" not in p.parts} == before


def test_private_concept_is_excluded(root_committed):
    root = root_committed
    id = _add(root, "The two pointers")
    assert any(id in (p.src, p.dst) for p in duplicates.find(Vault(root))[0])
    p = root / f"concepts/{id}.md"
    p.write_text(p.read_text(encoding="utf-8").replace("---\n", "---\nprivate: true\n", 1),
                 encoding="utf-8")
    pairs, total, compared = duplicates.find(Vault(root))
    assert all(id not in (p.src, p.dst) for p in pairs) and total == 0 and compared == 2


def test_embedding_cosine_only_with_an_embedder(root_committed, monkeypatch):
    root = root_committed
    _add(root, "Automobile")
    _add(root, "Car")
    assert duplicates.find(Vault(root))[1] == 0  # names alone do not match
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    pairs, _, _ = duplicates.find(Vault(root), semantic.load_embedder())
    assert {pairs[0].src, pairs[0].dst} == {"automobile", "car"}
    assert "embedding cosine 1.00" in pairs[0].evidence


def test_safe_strips_links_html_and_control_chars():
    assert duplicates.safe("[Two](http://x) <b>p</b>\x1b[31m") == "Two p[31m"


def test_loose_name_match_alone_is_not_a_pair(root_committed):
    root = root_committed
    a = _add(root, "Matrix traversal")
    b = _add(root, "Grid traversal")  # name ratio 0.80: a judged false pair
    assert not any({p.src, p.dst} == {a, b} for p in duplicates.find(Vault(root))[0])
