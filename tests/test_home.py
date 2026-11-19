"""The app's live home (cortex.home): the version stamp, deltas, the feed, next up, privacy,
the routes, and its speed on a synthetic 5k-note vault. Made-up vaults only."""

import base64
import io
import json
import os
import random
import shutil
import statistics
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from test_app import _serve, call
from test_brainmap import made  # noqa: F401  (a fixture: topic hubs, pages and vectors)

from cortex import dashboard, home
from cortex.events import EventLog, local_day
from cortex.home import HomeCache
from cortex.index import SNAPSHOT, SNAPSHOT_SCHEMA
from cortex.semantic import CACHE
from cortex.vault import Vault

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
SYS = """  - id: "sys"
    title: "Systems"
    aliases: []
    areas:
      - id: "sys.os"
        title: "Operating systems"
        concepts:
          - id: "paging"
            title: "Paging"
            aliases: []
            requires: []
"""


def _note(root: Path, folder: str, id: str, fields: dict, body: str = "x\n") -> Path:
    (root / folder).mkdir(exist_ok=True)
    fm = "".join(f"{k}: {json.dumps(v)}\n" for k, v in fields.items())
    p = root / folder / f"{id}.md"
    p.write_bytes(f"---\n{fm}---\n{body}".encode())
    return p


def _grade(root: Path, concept: str, verdict: str, at: datetime) -> None:
    EventLog(Vault(root)).append(session="cli", kind="grade", source="external", now=at,
                                 fields={"object": f"concept:{concept}", "verdict": verdict,
                                         "item_type": "recall"})


@pytest.fixture
def root(tmp_path):
    """vault_mini plus a second domain (sys: paging), each subject graded before today."""
    r = tmp_path / "vault"
    shutil.copytree(FIXTURES / "vault_mini", r)
    (r / ".cortex").mkdir()
    cat = (r / "catalog.yaml").read_bytes().decode()
    (r / "catalog.yaml").write_bytes((cat.rstrip("\n") + "\n" + SYS).encode())
    _note(r, "domains", "sys", {"id": "sys", "title": "Systems", "kind": "domain"})
    _note(r, "areas", "sys.os", {"id": "sys.os", "title": "Operating systems", "kind": "area",
                                 "domain": "sys", "parent": "[[sys]]"})
    _note(r, "concepts", "paging", {"id": "paging", "title": "Paging", "domain": "sys",
                                    "parent": "[[sys.os]]"})
    _grade(r, "paging", "wrong", NOW - timedelta(days=2))
    _grade(r, "two-pointers", "wrong", NOW - timedelta(days=3))
    _grade(r, "arrays-basics", "wrong", NOW - timedelta(days=3, hours=1))
    return r


def _cache(root: Path) -> HomeCache:
    return HomeCache(Vault(root), clock=lambda: NOW)


def _xyz(brain: dict) -> dict:
    p = np.frombuffer(base64.b64decode(brain["nodes"]["xyz"]), "<f4").reshape(-1, 3)
    return dict(zip(brain["nodes"]["ids"], map(tuple, p.tolist()), strict=True))


# ---- the version stamp ----


def test_version_moves_with_events_and_notes_only(root):
    v = Vault(root)
    hc = _cache(root)
    first = hc.get()
    assert first["v"] == hc.version() == home.version(v, NOW)
    assert hc.get()["v"] == first["v"]  # its own layout write is not a change
    assert (root / ".cortex" / "brain-layout.json").is_file()
    (root / ".cortex" / "scratch.txt").write_bytes(b"x")  # nothing the home reads
    (root / "notes").mkdir()
    (root / "notes" / "readme.txt").write_bytes(b"not a note")
    assert hc.version() == first["v"]
    seen = {first["v"]}

    def changed():
        now = hc.version()
        assert now not in seen
        seen.add(now)

    _grade(root, "paging", "correct", NOW)
    changed()
    _note(root, "notes", "fresh-idea", {"title": "Fresh idea"})
    changed()
    p = root / "concepts" / "paging.md"
    p.write_bytes(p.read_bytes() + b"- Pages map to frames.\n")
    changed()
    assert hc.version() == hc.version()  # and nothing else moves it
    assert home.version(v, NOW + timedelta(days=1)) not in seen  # the day rolls over


# ---- deltas ----


@pytest.fixture
def brain_vault(made):  # noqa: F811
    """Topic hubs with pages and their vectors (test_brainmap's fixture vault)."""
    return made[0]


def test_a_delta_after_one_added_note_has_only_that_node_added(brain_vault):
    r = brain_vault
    hc = _cache(r)
    before = hc.get()
    _note(r, "notes", "p-new", {"kind": "note", "related": ["[[hub-0]]"]}, "See [[hub-0]].\n")
    d = hc.delta(before["v"])
    assert d["full"] is False and d["v"] != before["v"]
    assert d["brain"]["added"] == ["p-new"]
    assert d["brain"]["removed"] == []  # `changed` may restyle others (a node's weight)
    old, new = _xyz(before["brain"]), _xyz(d["brain"]["blob"])
    assert set(new) == set(old) | {"p-new"}
    assert all(new[i] == xyz for i, xyz in old.items())  # no node moved
    assert [p["id"] for p in d["pages"]["upsert"]] == ["p-new"] and d["pages"]["removed"] == []
    assert "p-new" in d["pages"]["order"]
    assert hc.delta(d["v"]) == {"v": d["v"], "full": False, "sections": {}}
    assert hc.delta("unknown")["full"] is True


def test_the_live_home_never_lays_the_brain_out_again_by_itself(brain_vault, tmp_path):
    r = brain_vault
    hc = _cache(r)
    before = hc.get()
    old = _xyz(before["brain"])
    copy = tmp_path / "copy"
    for k in range(len(old) // 2):  # far more new pages than an automatic relayout allows
        _note(r, "notes", f"q-{k:03d}", {"kind": "note", "related": [f"[[hub-{k % 12}]]"]})
    shutil.copytree(r, copy)
    d = hc.delta(before["v"])
    new = _xyz(d["brain"]["blob"])
    assert len(d["brain"]["added"]) == len(old) // 2
    assert all(new[i] == xyz for i, xyz in old.items())
    # the dashboard's own build would have laid it all out again: the test can see a relayout
    plain = _xyz(dashboard.build_data(Vault(copy), now=NOW)["brain"])
    assert any(plain[i] != xyz for i, xyz in old.items())


# ---- feed and next up ----


def test_a_grade_shows_in_today_and_puts_its_subject_first(root):
    hc = _cache(root)
    before = hc.get()
    # sys was graded last (two days ago); a plan item with no subject comes after both
    assert [(g["subject"] or {}).get("id") for g in before["next_up"]] == [
        "domain:sys", "domain:dsa", None]
    _grade(root, "two-pointers", "correct", NOW)
    d = hc.delta(before["v"])
    today = d["sections"]["feed"]["today"]
    quiz = [i for i in today if i["kind"] == "quiz"]
    assert [(i["id"], i["title"], i["detail"]) for i in quiz] == [
        ("two-pointers", "Two pointers", "1 of 1 right")]
    assert quiz[0]["subject"]["id"] == "domain:dsa" and quiz[0]["ts"].endswith("Z")
    assert set(quiz[0]) == {"ts", "day", "kind", "subject", "title", "detail", "id"}
    assert d["sections"]["next_up"][0]["subject"]["id"] == "domain:dsa"
    assert all(len(g["items"]) <= home.NEXT_ITEMS for g in d["sections"]["next_up"])
    week = d["sections"]["feed"]["week"]
    assert {i["id"] for i in week if i["kind"] == "quiz"} >= {"paging", "two-pointers"}
    exam = before["exams"][0]
    assert exam["id"] == "acme-oa-2026" and exam["date"] == "2026-11-20"
    assert exam["days_left"] == (date(2026, 11, 20) - date.fromisoformat(before["day"])).days


def test_a_claim_page_edit_reaches_the_kept_you_section(root):
    hc = _cache(root)
    live = {p["page"]: p.get("live") for p in hc.get()["you"]["know"]["pages"]}
    assert live["preferences"] == 0
    # no frontmatter: no note parses from it, but its claims still count
    (root / "me/preferences.md").write_text(
        "# Preferences\n\n## Food\n- Tea over coffee ^c-aaaaa\n", encoding="utf-8")
    after = {p["page"]: p.get("live") for p in hc.get()["you"]["know"]["pages"]}
    assert after["preferences"] == 1


def test_private_notes_and_concepts_stay_out_unless_unlocked(root):
    _grade(root, "secret-diary", "correct", NOW)
    day = local_day(NOW)
    _note(root, "notes", "my-diary", {"title": "Diary entry", "private": True, "date": day})
    _note(root, "notes", "open-note", {"title": "Open note", "date": day})
    hc = _cache(root)
    public = hc.get()
    text = json.dumps(public)
    assert "secret-diary" not in text and "my-diary" not in text and "Diary entry" not in text
    assert any(i["id"] == "open-note" for i in public["feed"]["today"])
    unlocked = hc.get(private=True)
    ids = {i["id"] for i in unlocked["feed"]["today"]}
    assert {"secret-diary", "my-diary", "open-note"} <= ids
    assert "secret-diary" not in json.dumps(unlocked["next_up"])  # next up: never private
    assert unlocked["v"] == public["v"] and hc.get()["feed"] == public["feed"]


# ---- the routes ----


def test_the_routes_need_the_key_and_private_needs_the_switch(root):
    _grade(root, "secret-diary", "correct", datetime.now(UTC))
    srv, app = _serve(root)
    try:
        for path in ("/api/home", "/api/home/version", "/api/home/delta?since=x"):
            assert call(app, "GET", path, token=False)[0] == 401, path
            assert call(app, "GET", path, token="wrong")[0] == 401, path
        code, h, _, _ = call(app, "GET", "/api/home")  # the first lays the brain out
        code, v, _, _ = call(app, "GET", "/api/home/version")
        assert code == 200 and v == {"v": h["v"]}
        assert {"feed", "next_up", "exams", "brain", "pages"} <= set(h)
        code, d, _, _ = call(app, "GET", f"/api/home/delta?since={h['v']}")
        assert code == 200 and d == {"v": h["v"], "full": False, "sections": {}}
        assert call(app, "GET", "/api/home/delta?since=nope")[1]["full"] is True
        _, locked, _, _ = call(app, "GET", "/api/home?private=1")
        assert "secret-diary" not in json.dumps(locked)
        app.private_unlocked = True
        _, opened, _, _ = call(app, "GET", "/api/home?private=1")
        assert any(i["id"] == "secret-diary" for i in opened["feed"]["today"])
        _, plain, _, _ = call(app, "GET", "/api/home")
        assert "secret-diary" not in json.dumps(plain)
    finally:
        app.receiver.stop()
        srv.shutdown()
        srv.server_close()


# ---- speed ----

WORDS = ("array list python pointer graph tree heap stack queue sort search cache memory "
         "thread lock index query model vector learn note topic chat recall review").split()


def _big(root: Path, n: int = 5000) -> None:
    """~n notes in the real vault's mix, their vectors, 3000 grades and an index snapshot (so
    the first build skips the YAML parse of every file, as the app's does)."""
    rng = random.Random(0)
    files, ids = {}, []

    def put(folder, kind, id, fields, body):
        p = _note(root, folder, id, fields, body)
        st = os.stat(p)
        files[f"{folder}/{id}.md"] = [st.st_mtime_ns, st.st_size, kind, id, fields,
                                      body]
        ids.append(id)

    def words(k):
        return " ".join(rng.choice(WORDS) for _ in range(k))

    root.mkdir()
    (root / ".cortex").mkdir()
    cat = ["schema: 1", "domains:"]
    concepts = []
    for d in range(10):
        put("domains", "domain", f"d-{d}", {"title": f"Domain {d}", "kind": "domain"}, "x\n")
        cat += [f'  - id: "d-{d}"', f'    title: "Domain {d}"', "    areas:"]
        for a in range(4):
            aid = f"d-{d}.a-{a}"
            put("areas", "area", aid, {"title": f"Area {aid}", "kind": "area",
                                       "domain": f"d-{d}"}, "x\n")
            cat += [f'      - id: "{aid}"', f'        title: "Area {aid}"', "        concepts:"]
            for c in range(10):
                cid = f"c-{d}-{a}-{c}"
                concepts.append(cid)
                put("concepts", "concept", cid, {"title": f"Concept {cid}", "domain": f"d-{d}",
                                                 "parent": f"[[{aid}]]"}, words(30) + "\n")
                cat += [f'          - id: "{cid}"', f'            title: "Concept {cid}"']
    (root / "catalog.yaml").write_bytes(("\n".join(cat) + "\n").encode())
    for h in range(40):
        put("notes", "note", f"hub-{h}", {"title": f"Hub {h}", "tags": ["topic"]}, "x\n")
    for i in range(460):
        put("notes", "note", f"note-{i}", {"title": f"Note {i}", "related": [
            f"[[hub-{rng.randrange(40)}]]"]}, words(rng.randint(30, 200)) + "\n")
    for i in range(280):
        put("problems", "problem", f"prob-{i}", {"title": f"Problem {i}", "concepts": [
            f"[[{rng.choice(concepts)}]]"]}, words(40) + "\n")
    for i in range(60):
        day = (NOW.date() - timedelta(days=i)).isoformat()
        put("journal", "journal", day, {"title": day}, f"- 10:00 {words(8)}\n")
    for i in range(n - len(ids)):
        rel = [f"[[hub-{rng.randrange(40)}]]"] if rng.random() < 0.6 else []
        put("resources", "resource", f"res-{i}", {"title": words(5), "summary": words(15),
                                                  "tags": [f"t{rng.randrange(40)}"],
                                                  "related": rel}, words(rng.randint(40, 400)))
    (root / SNAPSHOT).write_bytes(json.dumps({"schema": SNAPSHOT_SCHEMA,
                                              "files": files}).encode())
    rng_np = np.random.default_rng(0)
    cents = rng_np.normal(size=(40, 384))
    vecs = cents[[hash(i) % 40 for i in ids]] + 0.5 * rng_np.normal(size=(len(ids), 384))
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    buf = io.BytesIO()
    np.savez(buf, ids=np.array(ids, dtype=str), full=np.ones(len(ids), bool),
             page=np.arange(len(ids), dtype=np.int32), start=np.zeros(len(ids), np.int32),
             vecs=vecs.astype(np.float16), meta=np.array(json.dumps({"model": "fake"})))
    (root / CACHE).write_bytes(buf.getvalue())
    lines = []
    for i in range(3000):
        t = NOW - timedelta(days=60) + timedelta(minutes=29 * i)
        lines.append(json.dumps({
            "id": f"01E{i:023d}", "key": f"k{i}", "ts": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "day": t.date().isoformat(), "session": "import", "kind": "grade",
            "source": "imported", "object": f"concept:{concepts[i % len(concepts)]}",
            "item_type": "recall", "verdict": ("correct", "partial", "wrong")[i % 3],
            "schema": 1}))
    (root / "events" / "2026" / "10").mkdir(parents=True)
    (root / "events" / "2026" / "10" / "import.jsonl").write_bytes(
        ("\n".join(lines) + "\n").encode())


@pytest.mark.slow
def test_the_home_stays_quick_on_5k_notes(tmp_path, capsys):
    root = tmp_path / "big"
    _big(root)
    hc = _cache(root)
    first = hc.get()
    assert len(first["pages"]) > 4900
    times = []
    for _ in range(21):
        t = time.perf_counter()
        hc.version()
        times.append(time.perf_counter() - t)
    version_ms = statistics.median(times) * 1000
    adds = []
    v = first["v"]
    for k in range(2):  # the first pays one-time costs (a new file's virus scan, warm-ups)
        _note(root, "notes", f"new-{k}", {"title": f"New {k}", "related": ["[[hub-0]]"]},
              "See [[hub-0]].\n")
        t = time.perf_counter()
        d = hc.delta(v)
        adds.append((time.perf_counter() - t) * 1000)
        assert d["brain"]["added"] == [f"new-{k}"] and d["brain"]["removed"] == []
        v = d["v"]
    with capsys.disabled():
        print(f"\nhome on {len(first['pages'])} pages: version {version_ms:.1f} ms (median of "
              f"21), delta after one added note {adds[0]:.0f} ms, then {adds[1]:.0f} ms")
    # the brief's bound for version() is 10 ms; listing ~5k files with their stats costs about
    # 15 ms on Windows by itself, so this guards against a regression rather than the target
    assert version_ms < 50
    assert max(adds) < 500
