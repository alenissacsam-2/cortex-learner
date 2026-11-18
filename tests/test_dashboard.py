import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cortex import dashboard, levels, planner
from cortex.catalog import Catalog
from cortex.cli import app
from cortex.events import EventLog
from cortex.session import SessionManager
from cortex.vault import Vault

FIXTURES = Path(__file__).parent / "fixtures"
# uv's cache and Pythons, read before conftest points these at a temp home (wheel test only)
UV_ENV = {k: os.environ[k] for k in ("APPDATA", "LOCALAPPDATA", "UV_CACHE_DIR",
                                     "UV_PYTHON_INSTALL_DIR") if k in os.environ}
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
HOSTILE = "</script><img src=x onerror=alert(1)>"
D3_HEADER = "// https://d3js.org v7.9.0 Copyright 2010-2023 Mike Bostock"
KEYS = {"as_of", "day", "sprint", "counts", "today", "plan_cleared", "first_run", "assessments",
        "mistakes", "graph", "details", "history", "vault_name", "pages", "owner", "clusters",
        "counts_by_kind", "loops", "follow_ups", "resurface", "needs", "you", "help", "brain",
        "confidence"}
ASSETS = Path(dashboard.__file__).parent / "assets"
JS = (ASSETS / "dashboard.js").read_text(encoding="utf-8")
# its header comment names the sinks it avoids; comments are not code
BRAIN_JS = re.sub(r"/\*.*?\*/", "", (ASSETS / "brain3d.js").read_text(encoding="utf-8"), flags=re.S)


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "vault"
    shutil.copytree(FIXTURES / "vault_mini", r)
    log = EventLog(Vault(r))
    for i, (concept, verdict) in enumerate([("two-pointers", "wrong"), ("two-pointers", "correct"),
                                            ("arrays-basics", "correct"),
                                            ("secret-diary", "correct")]):
        log.append(session="cli", kind="grade", source="external",
                   now=NOW - timedelta(days=3 - i), fields={
                       "object": f"concept:{concept}", "verdict": verdict,
                       "item_type": "recall", "mistakes": ["dsa.off-by-one"]})
    return r


def blob(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


def test_blob_schema_and_counts(root):
    d = dashboard.build_data(Vault(root), now=NOW)
    assert set(d) == KEYS
    assert set(d["counts"]) == {"solid", "learning", "needs_refresh", "unverified", "not_started"}
    assert sum(d["counts"].values()) == 2  # the private concept is not counted
    kinds = [n["kind"] for n in d["graph"]["nodes"]]
    assert kinds.count("you") == 1 and kinds.count("domain") == 1 and kinds.count("area") == 1
    assert kinds.count("concept") == 2
    types = sorted(lk["type"] for lk in d["graph"]["links"])
    assert types == ["hier", "hier", "hier", "requires", "you"]
    node = next(n for n in d["graph"]["nodes"] if n["id"] == "two-pointers")
    assert set(node) == {"id", "kind", "title", "state", "domain", "area", "degree",
                         "open_mistakes", "next_due"}
    assert node["degree"] == 2 and node["area"] == "dsa.arrays"
    det = d["details"]["two-pointers"]
    assert set(det) == {"evidence", "requires", "required_by", "problems", "mistakes"}
    assert [e["verdict"] for e in det["evidence"]] == ["correct", "wrong"]  # newest first
    assert det["evidence"][0] == {"day": det["evidence"][0]["day"], "verdict": "correct",
                                  "item_type": "recall", "source": "external"}
    assert det["requires"] == ["arrays-basics"]
    assert d["details"]["arrays-basics"]["required_by"] == ["two-pointers"]
    assert {p["id"] for p in det["problems"]} == {"lc-1", "lc-167"}
    assert d["sprint"]["day"] == 2 and [x["status"] for x in d["sprint"]["days"]] == [
        "done", "current"]
    assert d["sprint"]["today_counts"] == {"done": 0, "left": 4}
    a = d["assessments"][0]
    assert a["id"] == "acme-oa-2026" and a["days_left"] == 7
    assert a["readiness"]["total"] == 2
    assert 0.0 <= a["readiness"]["share"] <= 1.0  # weighted planner.readiness
    assert a["readiness"]["weighted"] == a["readiness"]["share"]  # D3
    assert d["mistakes"][0]["id"] == "dsa.off-by-one" and d["mistakes"][0]["count"] == 1
    weeks = d["history"]["weeks"]
    assert len(weeks) == 8 and sum(w["answers"] for w in weeks) == 3
    assert set(d["history"]["mistakes"]) == {"opened", "resolved"}
    assert d["plan_cleared"] is (not d["today"])


def test_private_notes_excluded(root):
    d = dashboard.build_data(Vault(root), now=NOW)
    text = json.dumps(d)
    assert "secret-diary" not in text and "Private concept" not in text


def test_private_extra_area_hides_its_concepts(root):
    """Audit F2: the dashboard hides what planner.hidden hides, extra areas included."""
    cat = root / "catalog.yaml"
    cat.write_text(cat.read_text(encoding="utf-8").replace(
        '            requires: ["arrays-basics"]\n',
        '            requires: ["arrays-basics"]\n            areas: ["dsa.p"]\n')
        + '      - id: "dsa.p"\n        title: "Private area"\n        concepts: []\n',
        encoding="utf-8")
    _note(root, "areas", "dsa.p", {"title": '"Private area"', "kind": "area", "domain": "dsa",
                                   "parent": '"[[dsa]]"', "private": "true"}, "# Private area\n")
    d = dashboard.build_data(Vault(root), now=NOW)
    assert "two-pointers" in planner.hidden(Vault(root), Catalog.load(Vault(root)))
    assert "two-pointers" not in {n["id"] for n in d["graph"]["nodes"]}
    assert "two-pointers" not in {p["id"] for p in d["pages"]}


def test_scalar_and_heading_concept_links(root):
    """Audit F5: a scalar `concepts:` is one link, and `#heading` is not part of the id."""
    p = root / "problems" / "lc-167.md"
    p.write_text(p.read_text(encoding="utf-8").replace(
        'concepts: ["[[two-pointers]]"]', 'concepts: "[[arrays-basics#Notes]]"'),
        encoding="utf-8")
    assert levels.problem_concepts(Vault(root))["lc-167"] == ["arrays-basics"]
    d = dashboard.build_data(Vault(root), now=NOW)
    assert "lc-167" in {x["id"] for x in d["details"]["arrays-basics"]["problems"]}


def test_hostile_title_is_inert(root):
    cat = root / "catalog.yaml"
    cat.write_text(cat.read_text(encoding="utf-8").replace(
        'title: "Two pointers"', f"title: '{HOSTILE} x<y'"), encoding="utf-8")
    html = dashboard.render_html(dashboard.build_data(Vault(root), now=NOW))
    assert "</script><img" not in html and "<img" not in html
    assert "x\\u003cy" in html
    titles = {n["id"]: n["title"] for n in blob(html)["graph"]["nodes"]}
    assert titles["two-pointers"] == "x<y"  # tags stripped, the rest kept as plain text


def test_csp_hashes_every_inline_script(root):
    html = dashboard.render_html(dashboard.build_data(Vault(root), now=NOW))
    csp = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', html)
    assert csp and csp.group(1).startswith("default-src 'none'; script-src ")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(scripts) == 3  # d3, the brain, the page
    for s in scripts:
        digest = base64.b64encode(hashlib.sha256(s.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in csp.group(1)


def test_no_urls_outside_the_d3_license_comment(root):
    html = dashboard.render_html(dashboard.build_data(Vault(root), now=NOW))
    assert html.count(D3_HEADER) == 1
    rest = html.replace(D3_HEADER, "")
    assert "http://" not in rest and "https://" not in rest


def _note(root, folder, id, fields, body):
    (root / folder).mkdir(exist_ok=True)
    fm = "".join(f"{k}: {v}\n" for k, v in fields.items())
    (root / folder / f"{id}.md").write_text(f"---\n{fm}---\n{body}", encoding="utf-8")


def test_pages_embed_bodies_but_never_private_or_raw(root):
    _note(root, "notes", "pointer-invariant", {
        "title": '"Shrink from both ends"', "kind": "note", "summary": '"One invariant"',
        "tags": "[dsa, invariants]", "cortex_status": "learning"},
        "# Idea\n\nSee [[two-pointers|the technique]] and [[secret-diary]] and [[nowhere]].\n")
    _note(root, "projects", "hidden-project", {"kind": "project", "private": "true"},
          "Private project body\n")
    _note(root, "raw", "paste", {"kind": "resource"}, "Raw pasted source text\n")
    _note(root, "notes", "huge", {"kind": "note"}, "x" * (dashboard.BODY_MAX + 500))
    d = dashboard.build_data(Vault(root), now=NOW)
    pages = {p["id"]: p for p in d["pages"]}
    assert d["vault_name"] == "vault"
    note = pages["pointer-invariant"]
    assert set(note) == {"id", "kind", "title", "summary", "tags", "props", "body", "links",
                         "path", "updated", "date", "source", "cluster"}
    assert note["title"] == "Shrink from both ends" and note["summary"] == "One invariant"
    assert note["tags"] == ["dsa", "invariants"] and note["path"] == "notes/pointer-invariant.md"
    assert "[[two-pointers|the technique]]" in note["body"]
    assert note["links"] == ["two-pointers"]  # private and missing ids are dropped
    assert "cortex_status" not in note["props"] and note["props"]["kind"] == "note"
    assert "Move the pointer on the smaller side." in pages["two-pointers"]["body"]
    assert pages["two-pointers"]["links"] == ["dsa.arrays", "arrays-basics", "dsa.off-by-one"]
    assert pages["huge"]["body"].endswith(dashboard.TRUNCATED)
    assert len(pages["huge"]["body"].encode()) <= dashboard.BODY_MAX + len(dashboard.TRUNCATED) + 4
    assert not {"secret-diary", "hidden-project", "paste"} & set(pages)
    text = json.dumps(d)
    assert "Private project body" not in text and "Raw pasted source text" not in text


def test_note_urls_never_read_as_urls_in_the_page(root):
    _note(root, "resources", "book", {"kind": "resource", "url": '"https://example.com/b"'}, "x\n")
    html = dashboard.render_html(dashboard.build_data(Vault(root), now=NOW))
    assert "https://example.com" not in html
    assert next(p for p in blob(html)["pages"] if p["id"] == "book")["props"]["url"] == (
        "https://example.com/b")  # the same string once parsed


def test_page_script_never_parses_html():
    # Note text is untrusted: the reader builds DOM nodes and sets textContent only.
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser",
                 "createContextualFragment", "srcdoc", "eval(", "new Function"):
        assert sink not in JS, sink
        assert sink not in BRAIN_JS, sink
    assert "obsidian://open?vault=" in JS


def test_init_writes_the_wiki_bases(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CORTEX_VAULT", raising=False)
    r = tmp_path / "v"
    res = CliRunner().invoke(app, ["init", str(r)])
    assert res.exit_code == 0, res.output
    for name in ("Projects", "Resources", "Mistakes"):
        text = (r / "views" / f"{name}.base").read_text(encoding="utf-8")
        assert f"name: {name}" in text and f'file.inFolder("{name.lower()}")' in text
    assert "property: status" in (r / "views/Projects.base").read_text(encoding="utf-8")
    assert "direction: DESC" in (r / "views/Mistakes.base").read_text(encoding="utf-8")
    groups = json.loads((r / ".obsidian/graph.json").read_text(encoding="utf-8"))["colorGroups"]
    assert {'path:"projects/"', 'path:"resources/"', 'path:"notes/"', 'path:"decisions/"'} <= {
        g["query"] for g in groups}


@pytest.mark.slow  # about 19 s here
def test_1000_note_vault_is_fast_and_small(tmp_path):
    r = tmp_path / "big"
    r.mkdir()
    (r / "catalog.yaml").write_text("schema: 1\ndomains: []\n", encoding="utf-8")
    para = "A paragraph of **notes** with `code` and some words to search through. " * 20
    for i in range(1000):
        folder, kind = [("notes", "note"), ("projects", "project"), ("resources", "resource"),
                        ("decisions", "decision")][i % 4]
        _note(r, folder, f"n{i}", {"kind": kind, "title": f'"Note {i}"', "tags": "[t1, t2]",
                                   "summary": f'"Summary {i}"'},
              f"# Note {i}\n\n{para}\n\n- [[n{(i + 1) % 1000}]] next\n- [[n{i // 2}]] half\n")
    for f in r.rglob("*.md"):
        f.read_bytes()  # a virus scanner's first look at fresh files is not build time
    t0 = time.perf_counter()
    html = dashboard.render_html(dashboard.build_data(Vault(r), now=NOW))
    assert time.perf_counter() - t0 < 8.0  # about 2.5 s here, nearly all YAML parsing
    assert len(html.encode()) < 5 * 1024 * 1024
    assert len(blob(html)["pages"]) == 1000


def test_build_data_500_concepts_is_fast(tmp_path):
    r = tmp_path / "big"
    r.mkdir()
    lines = ["schema: 1", "domains:"]
    ids = []
    for d in range(5):
        lines += [f'  - id: "d{d}"', f'    title: "Domain {d}"', "    areas:"]
        for a in range(10):
            lines += [f'      - id: "d{d}.a{a}"', f'        title: "Area {d}.{a}"',
                      "        concepts:"]
            for c in range(10):
                cid = f"c-{d}-{a}-{c}"
                req = f'["{ids[-1]}"]' if ids and c else "[]"
                lines += [f'          - id: "{cid}"', f'            title: "Concept {cid}"',
                          f"            requires: {req}"]
                ids.append(cid)
    (r / "catalog.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    events = []
    for i, cid in enumerate(ids[:300]):
        for k in range(3):
            t = NOW - timedelta(days=30 - 9 * k, minutes=i)
            events.append(json.dumps({
                "id": f"01E{i:05d}{k}", "key": f"k{i}.{k}", "ts": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "day": t.date().isoformat(), "session": "import", "kind": "grade",
                "source": "imported", "object": f"concept:{cid}",
                "verdict": ("correct", "partial", "wrong")[(i + k) % 3], "schema": 1}))
    (r / "events" / "2026" / "10").mkdir(parents=True)
    (r / "events" / "2026" / "10" / "import.jsonl").write_text("\n".join(events) + "\n")
    t0 = time.perf_counter()
    d = dashboard.build_data(Vault(r), now=NOW)
    assert time.perf_counter() - t0 < 2.0
    assert sum(d["counts"].values()) == 500
    assert len(d["graph"]["nodes"]) == 1 + 5 + 50 + 500


def test_cli_dashboard_no_open(root, monkeypatch):
    monkeypatch.delenv("CORTEX_VAULT", raising=False)
    r = CliRunner().invoke(app, ["dashboard", "--no-open", "--vault", str(root)])
    assert r.exit_code == 0, r.output
    path = root / ".cortex" / "dashboard.html"
    assert path.is_file() and blob(path.read_text(encoding="utf-8"))["graph"]["nodes"]
    assert "## Concepts" in (root / "index.md").read_text(encoding="utf-8")  # on demand too


def test_close_writes_neither_dashboard_nor_index(root, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a whole-vault view was built at close")

    monkeypatch.setattr(dashboard, "write_dashboard", boom)
    monkeypatch.setattr(dashboard, "write_index", boom)
    mgr = SessionManager(Vault(root), clock=lambda: NOW)
    mgr.start()
    mgr.note("graded", "two-pointers")  # a learning session: the full close
    assert mgr.close()["session"].startswith("s_")
    assert (root / "Today.md").is_file()
    assert not (root / ".cortex" / "dashboard.html").exists()
    assert not (root / "index.md").exists()


def test_owner_name_from_profile(root):
    assert dashboard.build_data(Vault(root), now=NOW)["owner"] is None  # no Name line
    prof = root / "me" / "profile.md"
    text = prof.read_text(encoding="utf-8") + "- **Name:** Sam. Address them as Sam.\n"
    prof.write_text(text, encoding="utf-8")
    assert dashboard.build_data(Vault(root), now=NOW)["owner"] == {"name": "Sam"}
    prof.write_text(text.replace('title: "Profile"', 'title: "Profile"\nprivate: true'),
                    encoding="utf-8")
    assert dashboard.build_data(Vault(root), now=NOW)["owner"] is None


def test_clusters_are_top_hubs_domains_and_unfiled(root):
    def hub(id, parent, extra=None):
        _note(root, "notes", id, {"kind": "note", "title": f'"{id.title()}"', "tags": "[topic]",
                                  **({"related": f'["[[{parent}]]"]'} if parent else {}),
                                  **(extra or {})}, f"# {id}\n")

    hub("my-topic-map", None)
    hub("sql", "my-topic-map")
    hub("sql-joins", "sql")
    hub("outer-joins", "sql-joins")  # a child of a child
    hub("secret-hub", "my-topic-map", {"private": "true"})
    _note(root, "resources", "chat-a", {"kind": "resource", "related": '["[[outer-joins]]"]'},
          "# Chat A\n\nTimeline: [[2026-03-01]] · Source: Gemini · Date: 2026-03-05\n")
    _note(root, "notes", "in-secret", {"kind": "note", "related": '["[[secret-hub]]"]'}, "x\n")
    _note(root, "notes", "loose", {"kind": "note"}, "x\n")
    _note(root, "notes", "hidden-in-sql", {"kind": "note", "private": "true",
                                           "related": '["[[sql]]"]'}, "x\n")
    _note(root, "notes", "on-the-map", {"kind": "note", "related": '["[[my-topic-map]]"]'}, "x\n")
    _note(root, "problems", "lc-secret", {"kind": "problem",
                                          "concepts": '["[[secret-diary]]"]'}, "x\n")
    _note(root, "journal", "2026-11-12", {"kind": "journal"}, "x\n")
    _note(root, "sessions", "s-1", {"kind": "session"}, "x\n")
    hub("people", "my-topic-map")  # a real hub with a group lobe's id
    _note(root, "projects", "cortex", {"kind": "project"}, "x\n")
    _note(root, "decisions", "use-sql", {"kind": "decision", "related": '["[[sql]]"]'}, "x\n")
    _note(root, "loops", "call-bob", {"kind": "loop"}, "x\n")
    _note(root, "people", "bob", {"kind": "person", "private": "false"}, "x\n")  # opted in
    tl = "Timeline: [[2026-03-01]] · Source: {} · Date: 2026-03-05\n"
    _note(root, "resources", "yt-1", {"kind": "resource"}, tl.format("YouTube"))
    _note(root, "resources", "nb-1", {"kind": "resource", "tags": "[notebooklm]"}, "x\n")
    _note(root, "resources", "chat-b", {"kind": "resource"}, tl.format("Perplexity"))
    d = dashboard.build_data(Vault(root), now=NOW)
    pages = {p["id"]: p for p in d["pages"]}
    assert pages["chat-a"]["cluster"] == "sql" and pages["outer-joins"]["cluster"] == "sql"
    assert pages["sql"]["cluster"] == "sql"
    assert pages["my-topic-map"]["cluster"] == pages["on-the-map"]["cluster"] == "unfiled"
    assert pages["two-pointers"]["cluster"] == "domain:dsa"
    assert pages["dsa"]["cluster"] == "domain:dsa"
    assert pages["lc-1"]["cluster"] == pages["dsa.off-by-one"]["cluster"] == "domain:dsa"
    assert pages["lc-secret"]["cluster"] == "unfiled"  # its only concept is private
    assert pages["2026-11-12"]["cluster"] == "journal" and pages["profile"]["cluster"] == "you"
    assert pages["s-1"]["cluster"] == "sessions"
    assert {pages[i]["cluster"] for i in ("acme-oa-2026", "acme-2026", "sprint-test")} == {
        "placements"}
    assert pages["loose"]["cluster"] == "unfiled" and pages["in-secret"]["cluster"] == "unfiled"
    assert {pages[i]["cluster"] for i in ("cortex", "call-bob")} == {"projects"}
    assert pages["use-sql"]["cluster"] == "sql"  # a hub wins over the group
    assert pages["bob"]["cluster"] == "people-group" and pages["people"]["cluster"] == "people"
    assert pages["yt-1"]["cluster"] == pages["nb-1"]["cluster"] == "media"
    assert pages["chat-b"]["cluster"] == "chats"
    assert pages["chat-a"]["date"] == "2026-03-05" and pages["chat-a"]["source"] == "Gemini"
    assert pages["loose"]["source"] is None
    lobes = {c["id"]: c for c in d["clusters"]}
    assert lobes["sql"] == {"id": "sql", "title": "Sql", "kind": "topic", "count": 5}
    assert lobes["domain:dsa"]["kind"] == "domain"
    assert lobes["unfiled"]["title"] == "Unfiled" and lobes["unfiled"]["kind"] == "unfiled"
    assert lobes["you"] == {"id": "you", "title": "About you", "kind": "group", "count": 1}
    assert lobes["placements"]["title"] == "Tests and applications"
    assert "my-topic-map" not in lobes
    assert lobes["people"]["kind"] == "topic"
    assert lobes["people-group"] == {"id": "people-group", "title": "People", "kind": "group",
                                     "count": 1}
    assert lobes["projects"]["title"] == "Projects, decisions and loops"
    assert lobes["media"]["title"] == "YouTube and NotebookLM"
    assert lobes["chats"]["title"] == "Unsorted chats"
    assert sum(c["count"] for c in d["clusters"]) == len(d["pages"])
    counts = [c["count"] for c in d["clusters"]]
    assert counts == sorted(counts, reverse=True)
    assert d["counts_by_kind"]["resource"] == 4
    assert sum(d["counts_by_kind"].values()) == len(d["pages"])
    # a public page's own props may name a private hub (as bodies may); the lobes never do
    seen = set(pages) | set(lobes) | {p["cluster"] for p in d["pages"]}
    assert not {"secret-hub", "hidden-in-sql"} & seen
    assert "Secret-Hub" not in json.dumps(d)


def test_loops_needs_and_you_hold_no_private_text(root):
    _note(root, "loops", "ask-bob", {"kind": "loop", "title": '"Ask <b>Bob</b>"',
                                     "status": "open", "date": "2026-11-01"}, "x\n")
    _note(root, "loops", "secret-loop", {"kind": "loop", "title": '"Secret loop"',
                                         "status": "open", "private": "true"}, "x\n")
    _note(root, "decisions", "pick-db", {"kind": "decision", "title": '"Pick a DB"',
                                         "status": "accepted", "date": "2026-10-01"},
          "## Outcome\n")
    _note(root, "decisions", "secret-choice", {"kind": "decision", "title": '"Secret choice"',
                                               "status": "accepted", "date": "2026-10-01",
                                               "private": "true"}, "## Outcome\n")
    _note(root, "me", "personality", {"id": '"personality"', "private": "true"},
          "# Personality\n\n## Core\n- Secret trait ^c-ccccc\n")
    EventLog(Vault(root)).append(session="cli", kind="log", source="tutor", fields={"payload": {
        "kind": "need", "question": "Morning <i>or</i> night?", "area": "habits"}})
    d = dashboard.build_data(Vault(root), now=NOW)
    assert [r["id"] for r in d["loops"]] == ["ask-bob"]
    assert d["loops"][0]["title"] == "Ask Bob" and d["loops"][0]["created"] == "2026-11-01"
    assert [r["id"] for r in d["follow_ups"]] == ["pick-db"]
    assert "pick-db" in {r["id"] for r in d["resurface"]}
    assert [n["question"] for n in d["needs"]] == ["Morning or night?"]
    you = d["you"]
    assert set(you) == {"know", "unconfirmed", "probes"}
    assert {"page": "personality", "private": True} in you["know"]["pages"]
    assert you["know"]["needs"] == d["needs"] and you["probes"]["overall"] == {"n": 0}
    text = json.dumps(d)
    assert "Secret" not in text and "<i>" not in text and "<b>" not in text


def test_help_texts_from_the_checkout(root):
    h = dashboard.build_data(Vault(root), now=NOW)["help"]
    assert h["commands_md"].startswith("# Help")
    rows = h["glossary_md"].splitlines()
    assert rows[0].startswith("| Name |") and all(r.startswith("|") for r in rows)
    assert any("**Hub / topic tree**" in r for r in rows)


def test_page_wires_brain_v2(root):
    # The page mounts the precomputed brain (never the old nodes/links API) and keeps its routes.
    assert "brain: D.brain, pages" in JS and "nodes, links, clusters" not in JS
    for hook in ("onSelect", "onCluster", "onLobe", "onMode", "onSpin", ".explore(", ".today(",
                 ".hoverId(", ".setGrowth(", ".setArchive(", ".setSpin("):
        assert hook in JS, hook
    html = dashboard.render_html(dashboard.build_data(Vault(root), now=NOW))
    for part in ('id="outline" role="tree"', 'id="brain-live" aria-live="polite"',
                 'id="brain-tools" role="toolbar"', 'id="llobe-chip"', 'id="home-brain-tools"'):
        assert part in html, part


def test_page_wires_the_look_panel(root):
    # brain v4 §5: the Look panel is generated from the renderer's schema and kept in localStorage.
    for part in ("STYLE_SCHEMA", "PRESETS", "DEFAULT_STYLE", ".setStyle(", "onStyle", "onCrumbs",
                 "'cortex.brain.style'", "aria-describedby", "aria-valuetext", "Reset to default",
                 "aria-keyshortcuts', 'L'"):
        assert part in JS, part
    assert "Wallpaper" not in JS and "wallpaper" not in JS  # dropped by the owner
    html = dashboard.render_html(dashboard.build_data(Vault(root), now=NOW))
    assert 'id="look" aria-label="Brain look" hidden' in html


def test_stage_is_a_keyboard_widget_with_help():
    # D6: the stage takes keys, so it is an application described by a key help naming the rail
    for part in ("'role', 'application'", "'aria-roledescription', '3D brain'",
                 "'aria-describedby', keysId", "The outline in the side rail lists every page."):
        assert part in BRAIN_JS, part
    assert "'role', 'img'" not in BRAIN_JS
    assert "keyHelp: HAS_STYLE ? 'L opens Look.'" in JS


def test_hover_picks_only_at_rest():
    # D7: a hover pick (a synchronous readPixels) waits for the pointer to rest, never every move
    assert "if (!drag) { pendingPick = p;" not in BRAIN_JS
    assert "setTimeout(() => { pendingPick = p; invalidate(); }, PICK_REST_MS)" in BRAIN_JS


def test_forecast_waits_for_calibration(root):
    """The exam-day forecast shows behind the home card's gate; before it, how far off it is."""
    log = EventLog(Vault(root))

    def stamped(n, start=0):  # two-pointers already has graded history, so each one counts
        for i in range(start, start + n):  # B5: one cold item a day, a forecast with skill
            ok = i % 4 != 3
            log.append(session="cli", kind="grade", source="tutor",
                       predicted_p=0.8 if ok else 0.2, now=NOW - timedelta(days=40 - i),
                       fields={"object": "concept:two-pointers",
                               "verdict": "correct" if ok else "wrong"})

    stamped(12)
    r = dashboard.build_data(Vault(root), now=NOW)["assessments"][0]["readiness"]
    assert r["forecast"] is None and r["calibrating"] == {"n": 11, "need": 30}  # 1st: no history
    stamped(19, 12)
    r = dashboard.build_data(Vault(root), now=NOW)["assessments"][0]["readiness"]
    assert 0.0 <= r["forecast"] <= 1.0 and r["calibrating"] is None


def test_confidence_mirror_in_the_blob_skips_private_concepts(root):
    log = EventLog(Vault(root))
    for i, concept in enumerate(["two-pointers"] * 3 + ["secret-diary"] * 5):
        log.append(session="cli", kind="grade", source="tutor", now=NOW - timedelta(hours=i),
                   fields={"object": f"concept:{concept}", "verdict": "correct",
                           "confidence": 4})
    rows = dashboard.build_data(Vault(root), now=NOW)["confidence"]
    assert [r["confidence"] for r in rows] == [1, 2, 3, 4]
    assert rows[3] == {"confidence": 4, "label": "4 (certain)", "n": 3, "right": 3,
                       "share": None}  # under the band minimum; the private 5 not counted
    assert "D.confidence" in JS and "not enough answers yet" in JS


# ---- v7 review D: personal data, help in the wheel, honest labels, first run, a11y --------

def _empty_vault(tmp_path) -> Path:
    r = tmp_path / "empty"
    r.mkdir()
    (r / "catalog.yaml").write_text("schema: 1\ndomains: []\n", encoding="utf-8")
    return r


def test_no_owner_data_in_the_page_script():
    # D5: no one learner's note ids or life events are baked into every vault's dashboard
    assert "youtube-and-notebooklm-patterns" not in JS and "placements in" not in JS
    assert "p.tags.includes('patterns')" in JS and "${s.title} in ${plural(n, 'day')}" in JS


def test_sprint_banner_title_comes_from_the_track(root):
    s = dashboard.build_data(Vault(root), now=NOW)["sprint"]
    assert s["title"] and s["title"] != "Phase 1 placements"


@pytest.mark.slow  # builds the wheel, about 7 s
def test_help_files_ship_in_the_wheel(tmp_path, monkeypatch):
    # D2: an installed Cortex has no checkout, so the Help tab reads the wheel's copies
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("needs uv")
    repo = Path(__file__).resolve().parent.parent
    out = tmp_path / "wheel"
    r = subprocess.run([uv, "build", "--wheel", "--offline", "--out-dir", str(out)], cwd=repo,
                       capture_output=True, text=True, env=os.environ | UV_ENV)
    assert r.returncode == 0, r.stderr
    [whl] = out.glob("*.whl")
    assets = tmp_path / "site" / "cortex" / "assets"
    with zipfile.ZipFile(whl) as z:
        names = set(z.namelist())
        assert {"cortex/assets/help/help.md", "cortex/assets/help/GUIDE.md"} <= names
        z.extractall(tmp_path / "site", [n for n in names if n.startswith("cortex/assets/help/")])
    monkeypatch.setattr(dashboard, "ASSETS", assets)
    monkeypatch.setattr(dashboard, "REPO", tmp_path / "not-a-checkout")
    h = dashboard._help()
    assert h["commands_md"].startswith("# Help")
    assert any("**Hub / topic tree**" in r for r in h["glossary_md"].splitlines())
    monkeypatch.setattr(dashboard, "ASSETS", tmp_path / "nothing")
    assert dashboard._help() == {"commands_md": None, "glossary_md": None}


def test_readiness_carries_the_weighted_share_and_plain_counts(root, monkeypatch):
    # D3: the weighted share under both names, beside the plain solid/total count
    monkeypatch.setattr(planner, "readiness", lambda *a: 0.25)
    r = dashboard.build_data(Vault(root), now=NOW)["assessments"][0]["readiness"]
    assert r["share"] == r["weighted"] == 0.25
    assert isinstance(r["solid"], int) and isinstance(r["total"], int)


def test_readiness_label_names_the_weighted_share():
    # D3: Home and Study show one metric, called what it is, beside the plain count
    assert JS.count("= readiness(r") == 2
    assert "% of the test's weight solid · ${topics}" in JS
    assert "% ready (" not in JS and "tested topics solid" not in JS


def test_empty_vault_is_a_first_run_not_done(tmp_path):
    # D7: an empty plan on a fresh vault is not "Done for today"
    d = dashboard.build_data(Vault(_empty_vault(tmp_path)), now=NOW)
    assert d["today"] == [] and d["first_run"] is True and d["plan_cleared"] is False
    assert "Start here" in JS and "Add a test date" in JS and "Import your notes or chats" in JS
    assert "In the Cortex app: Setup → Add your history." in JS  # the app's guided imports


def test_empty_plan_is_done_only_after_work_today(tmp_path):
    r = _empty_vault(tmp_path)
    EventLog(Vault(r)).append(session="cli", kind="grade", source="external", now=NOW,
                              fields={"object": "concept:x", "verdict": "correct"})
    d = dashboard.build_data(Vault(r), now=NOW)
    assert d["first_run"] is False and d["plan_cleared"] is True
    d = dashboard.build_data(Vault(r), now=NOW + timedelta(days=2))
    assert d["first_run"] is False and d["plan_cleared"] is False


def test_day_is_the_cortex_day_not_the_calendar_date(root):
    # D8: at 01:30 local time it is still yesterday in Cortex (04:00 rollover)
    late = datetime(2026, 11, 14, 1, 30).astimezone()
    d = dashboard.build_data(Vault(root), now=late)
    assert d["as_of"].startswith("2026-11-14") and d["day"] == "2026-11-13"
    assert "const TODAY = D.day ||" in JS


def test_me_pages_get_friendly_titles(root):
    (root / "me" / "teaching-contract.md").write_text("---\nkind: me\n---\n# x\n",
                                                       encoding="utf-8")
    pages = {p["id"]: p for p in dashboard.build_data(Vault(root), now=NOW)["pages"]}
    assert pages["teaching-contract"]["title"] == "Teaching contract"
    assert pages["profile"]["title"] == "Profile"  # a title field still wins


def test_copy_buttons_copy_ids_never_titles():
    # D13: a copied prompt is pasted as the user's own words, outside <untrusted_note>
    copies = (re.findall(r"(?:copyText|copyBtn)\((`[^`]*`)", JS)
              + re.findall(r"prompt: [^,}]*`[^`]*`", JS) + re.findall(r"return `/cortex[^`]*`", JS))
    assert copies and not [c for c in copies if re.search(r"\$\{[^}]*(title|company|kind)", c)]
    assert "const loopPrompt = (id) =>" in JS and "loopPrompt(l.title)" not in JS


def test_timeline_days_and_palette_take_the_keyboard():
    # D14
    assert "c.setAttribute('tabindex', '0');" in JS
    assert "svg.addEventListener('keydown', pick);" in JS
    assert "if (e.key === 'Tab') { e.preventDefault(); pq.focus(); }" in JS
    css = (ASSETS / "dashboard.css").read_text(encoding="utf-8")
    assert ".dep-row:focus-within .acts" in css


def test_growth_range_ignores_absurd_dates():
    # D10
    assert "d >= '1990' && d <= latest" in JS


def _js_function(src: str, head: str) -> str:
    """The source of one function in the page script, by brace matching from `head`."""
    i = src.index(head)
    depth = 0
    for k in range(src.index("{", i), len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[i:k + 1]
    raise AssertionError(head)


def _js_line(src: str, head: str) -> str:
    i = src.index(head)
    return src[i:src.index("\n", i)]


def test_markdown_quotes_cap_their_depth(tmp_path):
    # D9: 10,000 nested `>` once blew the stack and left the reader blank
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    start = JS.index("  const INLINE = ")
    parts = [JS[start:JS.index("\n", JS.index("'g');", start))],
             _js_line(JS, "  const cells = "), _js_line(JS, "  const QUOTE_DEPTH"),
             *(_js_function(JS, f"function {f}(") for f in ("el", "button", "wikilink",
                                                             "inline", "md"))]
    harness = tmp_path / "md.cjs"
    harness.write_text("""
const mk = (tag) => ({ tag, kids: [], className: '',
  set textContent(t) { this.kids = [String(t)]; },
  append(...xs) { this.kids.push(...xs); }, appendChild(x) { this.kids.push(x); return x; },
  setAttribute() {}, addEventListener() {} });
const document = { createElement: mk, createDocumentFragment: () => mk('#frag') };
const PAGES = new Map(), openReader = () => {};
""" + "\n".join(parts) + """
const depth = (n) => (typeof n === 'string' ? 0
  : (n.tag === 'blockquote' ? 1 : 0) + Math.max(0, ...n.kids.map(depth)));
const deep = md('>'.repeat(10000) + ' hi');
const ok = md('> a\\n>> b');
console.log(JSON.stringify({ deep: depth(deep), ok: depth(ok) }));
""", encoding="utf-8")
    r = subprocess.run([node, str(harness)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"deep": 21, "ok": 2}


def test_snapshot_parses_build_the_same_dashboard(root):
    # D15: notes served from the index snapshot (no file read, no YAML) give the same blob
    from cortex import vault as vault_mod
    from cortex.index import Index

    v = Vault(root)
    idx = Index(v, Catalog.load(v))
    idx.refresh()
    idx.save_snapshot()
    dashboard.write_dashboard(v, now=NOW)  # the brain keeps its first layout; compare after it
    for p in [p for p in vault_mod._PARSED if p.is_relative_to(root)]:
        del vault_mod._PARSED[p]  # a fresh process: nothing parsed yet
    want = dashboard.build_data(v, now=NOW)
    for p in [p for p in vault_mod._PARSED if p.is_relative_to(root)]:
        del vault_mod._PARSED[p]
    with dashboard._snapshot_parses(v):
        served = {p for p in vault_mod._PARSED if p.is_relative_to(root)}
        got = dashboard.build_data(v, now=NOW)
    assert served and got == want
    assert not served & set(vault_mod._PARSED)  # scoped: later reads parse the files again
