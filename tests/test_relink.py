"""`keepfold relink` and the Obsidian graph links new writes carry (synthetic vaults only)."""

import json
from pathlib import Path

from typer.testing import CliRunner

from keepfold import people, relink
from keepfold.cli import app
from keepfold.index import _edge_targets
from keepfold.vault import Vault


def _w(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _r(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def _vault(root: Path) -> Vault:
    _w(root, "journal/2026-11-01.md", '---\nid: "2026-11-01"\nkind: "journal"\n---\n## Log\n'
       "- digest a month\n")
    _w(root, "journal/2026-11-14.md", '---\nid: "2026-11-14"\nkind: "journal"\n---\n## Log\n'
       "- 10:00 hi\n")
    _w(root, "notes/my-topic-map.md", '---\nid: "my-topic-map"\nkind: "note"\n---\n# Map\n')
    _w(root, "sessions/2026-11-14-home-abcdefgh.md", "# Session s1\n\n- Mode: home\n")
    _w(root, "me/profile.md", "# Profile\n\n## Who\n- Sam\n")
    _w(root, "people/ravi.md", '---\nid: "ravi"\nkind: "person"\nprivate: false\n---\n# Ravi\n')
    _w(root, "people/asha.md", '---\nid: "asha"\nkind: "person"\n---\n# Asha\n')
    _w(root, "notes/sql-joins.md", '---\nid: "sql-joins"\nkind: "note"\n---\n# Joins\n')
    _w(root, "resources/r1.md", '---\nid: "r1"\nkind: "resource"\nrelated: ["sql-joins", '
       '"gone-note", "[[my-topic-map]]"]\n---\n# R1\n\nSee [[Concepts/SQL Joins|joins]], '
       "[[idfc-research-2026]], df[['a', 'b']] and `x[[0]]`.\n\n```\ny = z[[1]]\n```\n")
    return Vault(root)


def test_dry_run_plans_and_writes_nothing(tmp_path):
    v = _vault(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.md")}
    out = relink.run(v, dry_run=True)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.md")} == before
    assert not (tmp_path / "notes" / "people.md").exists()
    assert out["changed"] == {"session_up": 1, "journal_up": 2, "people_hub": 1,
                              "people_up": 2, "profile_hub": 1, "links_fixed": 1,
                              "frontmatter_links": 1}
    assert sum(out["orphans_before"].values()) > 0 and sum(out["orphans_after"].values()) == 0
    rows = {r["target"]: (r["class"], r["to"]) for r in out["unresolved"]}
    assert rows == {"Concepts/SQL Joins": ("near", "sql-joins"),
                    "idfc-research-2026": ("missing", None), "'a', 'b'": ("junk", None)}


def test_apply_is_idempotent_and_keeps_code_and_privacy(tmp_path):
    v = _vault(tmp_path)
    relink.run(v, dry_run=False)
    assert "Up: [[2026-11-14]]" in _r(tmp_path, "sessions/2026-11-14-home-abcdefgh.md")
    assert _r(tmp_path, "journal/2026-11-14.md").split("---\n")[2].startswith(
        "Up: [[2026-11-01]]\n\n## Log\n")
    assert "Up: [[my-topic-map]]" in _r(tmp_path, "journal/2026-11-01.md")
    assert _r(tmp_path, "me/profile.md") == "# Profile\n\nPeople: [[people]]\n\n## Who\n- Sam\n"
    hub = _r(tmp_path, "notes/people.md")
    assert "- [[people/ravi]]" in hub and "asha" not in hub  # private: not even its link
    assert "Up: [[people]]" in _r(tmp_path, "people/asha.md")
    r1 = _r(tmp_path, "resources/r1.md")
    assert "[[sql-joins|joins]]" in r1 and "[[idfc-research-2026]]" in r1
    assert "df[\\[\\['a', 'b']]" not in r1 and "df\\[\\['a', 'b']]" in r1
    assert "`x[[0]]`" in r1 and "y = z[[1]]" in r1  # code is never touched
    related = v.read("resource", "r1").fields["related"]
    assert list(related) == ["[[sql-joins]]", "gone-note", "[[my-topic-map]]"]
    assert _edge_targets(related) == ["sql-joins", "gone-note", "my-topic-map"]
    snap = {p: p.read_bytes() for p in tmp_path.rglob("*.md")}
    again = relink.run(v, dry_run=False)
    assert not again["changed"]
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.md")} == snap


def test_new_journal_and_people_pages_carry_links(tmp_path):
    v = Vault(tmp_path)
    v.append_journal("2026-11-01", "09:00", "first")  # a month page with no topic map: no Up
    assert "Up:" not in _r(tmp_path, "journal/2026-11-01.md")
    v.append_journal("2026-11-02", "09:00", "second")
    v.append_journal("2026-11-02", "10:00", "third")
    text = _r(tmp_path, "journal/2026-11-02.md")
    assert text.endswith("Up: [[2026-11-01]]\n\n## Log\n- 09:00 second\n- 10:00 third\n")
    _w(tmp_path, "me/profile.md", "# Profile\n")
    pid = people.add(v, "Ravi", {"relation": "friend", "private": False})
    people.add(v, "Asha", {"relation": "friend"})
    assert "[[people]]" in _r(tmp_path, "me/profile.md")
    assert "Up: [[people]]" in _r(tmp_path, "people/asha.md")
    assert "asha" not in _r(tmp_path, "notes/people.md")
    people.forget(v, pid)
    assert "ravi" not in _r(tmp_path, "notes/people.md")


def test_report_never_names_private_pages_or_their_targets(tmp_path):
    v = _vault(tmp_path)
    _w(tmp_path, "notes/secret.md", '---\nid: "secret"\nprivate: true\n---\n[[Secret Target]]\n')
    _w(tmp_path, "people/asha.md", '---\nid: "asha"\nkind: "person"\n---\n[[Asha Target]]\n')
    _w(tmp_path, "browse/private/2026-11-14.md", "# Day\n[[Browse Target]]\n")
    _w(tmp_path, "raw/bad-fm.md", '---\nprivate: [true\n---\n[[Broken Target]]\n')
    text = "\n".join(relink.report_lines(relink.run(v, dry_run=True)))
    for leak in ("Secret Target", "secret.md", "Asha Target", "asha", "Browse Target",
                 "browse/private", "Broken Target", "bad-fm"):
        assert leak not in text
    assert "idfc-research-2026" in text and "resources/r1.md" in text  # public rows stay


def test_broken_yaml_in_notes_does_not_crash(tmp_path):
    v = _vault(tmp_path)
    _w(tmp_path, "notes/bad.md", '---\nid: "bad"\nrelated: [x\n---\nbody\n')
    relink.run(v, dry_run=True)  # used to raise ruamel's ParserError


def test_with_up_shapes():
    assert relink.with_up("# T", "x") == "# T\n\nUp: [[x]]\n"
    assert relink.with_up("# T\n- a\n", "x") == "# T\n\nUp: [[x]]\n\n- a\n"
    assert relink.with_up("see [[X|y]]\n", "x") == "see [[X|y]]\n"  # already linked
    assert relink.with_up("---\nid: a\n---\nbody\n", "x") == "---\nid: a\n---\nUp: [[x]]\n\nbody\n"


def test_cli_dry_run(tmp_path):
    _vault(tmp_path)
    r = CliRunner().invoke(app, ["relink", "--dry-run", "--vault", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "orphans:" in r.output and "junk" in r.output and "nothing written" in r.output
    assert not (tmp_path / "notes" / "people.md").exists()


# ---- study domains <-> topic hubs ---------------------------------------------------------

def _hub(root: Path, id: str, title: str, parent: str, extra: str = "", body: str = "") -> None:
    _w(root, f"notes/{id}.md", f'---\nid: "{id}"\ntitle: "{title}"\nkind: "note"\n'
       f'tags: ["topic"]\nrelated: ["[[{parent}]]"]\n{extra}---\n# {title}\n\n{body}')


def _domain(root: Path, id: str, title: str, aliases=()) -> None:
    _w(root, f"domains/{id}.md", f'---\nid: "{id}"\ntitle: "{title}"\nkind: "domain"\n'
       f"aliases: {json.dumps(list(aliases))}\n---\n# {title}\n\n## Notes\n")


def _study(root: Path) -> Vault:
    """vault_mini (domain dsa, alias DSA) plus os, sql and dbms domains and a hub tree."""
    _w(root, "notes/my-topic-map.md", '---\nid: "my-topic-map"\ntitle: "My topic map"\n'
       'kind: "note"\ntags: ["topic"]\nrelated: []\n---\n# My topic map\n')
    _hub(root, "topic-software", "Software", "my-topic-map")
    _hub(root, "topic-software-dsa", "DSA", "topic-software",
         body="Hand text.\n\n## Conversations\n- old line\n")
    _hub(root, "topic-software-dsa-graphs", "Graphs", "topic-software-dsa")
    _hub(root, "topic-software-sql", "SQL", "topic-software")
    _hub(root, "topic-software-db", "Databases theory", "topic-software")  # a dbms alias
    _hub(root, "topic-software-db-dbms", "DBMS internals", "topic-software-db",
         'aliases: ["DBMS"]\n')  # matches too, but its parent is more general
    _hub(root, "topic-science-cosmos", "Cosmos", "my-topic-map")
    _hub(root, "topic-private-sql", "SQL", "topic-software", "private: true\n")
    _domain(root, "os", "Operating systems", ["OS"])
    _domain(root, "sql", "SQL")
    _domain(root, "dbms", "Database management systems", ["DBMS", "databases theory"])
    return Vault(root)


def test_domain_hub_matching_rule():
    def f(title, aliases=(), related=()):
        return {"title": title, "aliases": list(aliases), "related": list(related)}

    fields = {"domain:os": f("Operating systems", ["OS"]),
              "domain:dsa": f("Data structures and algorithms", ["DSA"]),
              "domain:system-design": f("System design"),
              "domain:genai": f("GenAI & ML"),
              "hub:topic-science-cosmos": f("Cosmos"),
              "hub:topic-life-photos": f("Photos"),
              "hub:topic-software-dsa": f("DSA"),  # by title: "dsa" alone is too short
              "hub:topic-software-system-design": f("Designing systems"),
              "hub:topic-ai-genai-and-ml": f("x"),
              "hub:topic-web-python": f("Snakes")}  # one segment of 5+ characters
    fields["domain:python"] = f("Python")
    assert relink.domain_hubs(fields) == [
        ("dsa", "topic-software-dsa"), ("genai", "topic-ai-genai-and-ml"),
        ("python", "topic-web-python"),
        ("system-design", "topic-software-system-design")]  # os never matches cosmos/photos
    many = {"domain:xxxxx": f("X"), **{f"hub:topic-a{i}-xxxxx": f("t") for i in range(5)}}
    assert len(relink.domain_hubs(many)) == 3  # at most three hubs per domain


def test_short_trailing_id_segments_do_not_match():
    def f(title, aliases=()):
        return {"title": title, "aliases": list(aliases)}

    fields = {"domain:data": f("Data"), "domain:apis": f("APIs"),
              "domain:genai-ml": f("Generative AI", ["ML"]),
              "hub:topic-gis-qgis-workflows-setup-data": f("QGIS setup data"),
              "hub:topic-blockchain-crypto-data-apis": f("Crypto data APIs"),
              "hub:topic-maths-math-for-ml": f("Math for machine learning")}
    assert relink.domain_hubs(fields) == []


def test_domains_join_hubs_idempotently_and_survive_a_hub_rebuild(root_plain):
    from keepfold import topics
    from keepfold.catalog import Catalog
    from keepfold.index import Index

    v = _study(root_plain)
    before = {p: p.read_bytes() for p in root_plain.rglob("*.md")}
    dry = relink.run(v, dry_run=True)
    assert {p: p.read_bytes() for p in root_plain.rglob("*.md")} == before
    assert dry["domain_hubs"] == [("dbms", "topic-software-db"), ("dsa", "topic-software-dsa"),
                                  ("sql", "topic-software-sql")]
    assert dry["changed"]["domain_topics"] == dry["changed"]["hub_domains"] == 3
    assert "  [[dsa]] <-> [[topic-software-dsa]]" in relink.report_lines(dry)

    relink.run(v, dry_run=False)
    assert _r(root_plain, "domains/dsa.md").endswith(
        "## Notes\n\n## Topics\n- [[topic-software-dsa]]\n")
    assert "## Topics\n- [[topic-software-db]]\n" in _r(root_plain, "domains/dbms.md")
    assert "Topics" not in _r(root_plain, "domains/os.md")
    hub = _r(root_plain, "notes/topic-software-dsa.md")
    assert "Hand text.\n\n## Study domains\n- [[dsa]]\n\n## Conversations\n" in hub
    assert "dbms" not in _r(root_plain, "notes/topic-software-db-dbms.md").split("---")[2]
    assert "[[sql]]" not in _r(root_plain, "notes/topic-private-sql.md")
    snap = {p: p.read_bytes() for p in root_plain.rglob("*.md")}
    again = relink.run(v, dry_run=False)
    assert not again["changed"]["domain_topics"] and not again["changed"]["hub_domains"]
    assert {p: p.read_bytes() for p in root_plain.rglob("*.md")} == snap

    done = topics.build(v, Index(v, Catalog.load(v)))
    assert not done["unsafe"]
    hub = _r(root_plain, "notes/topic-software-dsa.md")
    assert "old line" not in hub  # the rebuild did regenerate this hub
    assert "## Study domains\n- [[dsa]]\n" in hub
