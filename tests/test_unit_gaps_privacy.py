"""TEST-PLAN §2 items 1-5 (privacy first): app imports and uploads, connector writers, the
browse filters and page bodies, site keys, and the small privacy gates in harvest, topics,
relink, claims, voice and export_context. Every subprocess, runner and browser is faked."""

import json
import random
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import FakeEmbed, note

from cortex import semantic
from cortex.app import actions, server, status, targets
from cortex.browse import build, privacy, sites
from cortex.browse.privacy import Kept
from cortex.vault import Vault, VaultError

pytestmark = pytest.mark.usefixtures("home")
MARKERS = ("TITLEMARKER", "URLMARKER", "SEARCHMARKER", "markerdomain")


@pytest.fixture(autouse=True)
def no_process(monkeypatch):
    monkeypatch.setattr(actions, "_popen", lambda *a, **k: pytest.fail("no process in tests"))
    monkeypatch.setattr(targets, "_run", lambda *a, **k: (False, "faked"))
    monkeypatch.setattr(targets, "_which", lambda c: None)


# ---- 1. app/actions: uploads and import jobs --------------------------------------------------


@pytest.fixture
def app(root):
    (root / ".cortex").mkdir(exist_ok=True)
    return server.App(root)


def _upload(app, name="upload-x.json") -> tuple[str, Path]:
    d = actions.tmp_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"[]")
    app.uploads["u1"] = p
    return "u1", p


def _wait(app) -> server.Job:
    for _ in range(400):
        if not app.jobs.cur.running:
            return app.jobs.cur
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_tmp_dir_is_under_the_cortex_home(home):
    assert actions.tmp_dir() == home / ".cortex" / "tmp"


@pytest.mark.parametrize("outcome", ["ok", "fail"])
def test_import_run_deletes_the_upload_on_success_and_failure(app, monkeypatch, outcome):
    uid, p = _upload(app)

    def fn(job, source, path, root):
        assert path == p
        if outcome == "fail":
            raise ValueError("bad export")
        return {"ok": True}
    monkeypatch.setattr(actions, "import_run", fn)
    app.import_run({"source": "chatgpt", "upload": uid})
    job = _wait(app)
    assert (job.error is None) == (outcome == "ok")
    assert not p.exists() and uid not in app.uploads


def test_import_preview_keeps_the_upload_unless_it_fails(app, monkeypatch):
    uid, p = _upload(app)
    monkeypatch.setattr(actions, "import_preview", lambda job, s, path, r: {"ok": True})
    app.import_preview({"source": "chatgpt", "upload": uid})
    _wait(app)
    assert p.exists() and app.uploads[uid] == p  # kept for the import that follows


def test_cancelled_google_import_deletes_the_upload_and_reports_counts_only(app, monkeypatch):
    """Cancel mid-import: build.run's next embed call raises Cancelled, the job says so with no
    title, URL or search in it, and the upload is gone."""
    from cortex.browse import sources, takeout

    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    monkeypatch.setattr(sources, "discover", lambda *a, **k: [])
    monkeypatch.setattr(takeout, "records", lambda path: ["row"])
    started = []

    def fake_run(v, srcs, settings, *, embed, progress, **kw):
        progress("chrome/Default", 3, 2, None)
        started.append(1)
        while not app.jobs.cur.cancelled.is_set():
            time.sleep(0.01)
        embed(["TITLEMARKER URLMARKER"])
        raise AssertionError("embed must raise Cancelled")
    monkeypatch.setattr(build, "run", fake_run)
    uid, p = _upload(app, "upload-y-MyActivity.json")
    app.import_run({"source": "google-activity", "upload": uid})
    for _ in range(200):
        if started:
            break
        time.sleep(0.01)
    app.cancel({})
    job = _wait(app)
    assert job.error is None and job.result["cancelled"] and not job.result["ok"]
    assert not p.exists() and uid not in app.uploads
    seen = json.dumps(job.view()) + json.dumps(job.lines)
    assert not any(m in seen for m in MARKERS)


def test_google_import_error_is_reported_by_type_only(app, monkeypatch):
    uid, p = _upload(app)

    def boom(job, source, path, root):
        raise RuntimeError("TITLEMARKER at https://markerdomain.example/URLMARKER")
    monkeypatch.setattr(actions, "import_run", boom)
    app.import_run({"source": "google-activity", "upload": uid})
    job = _wait(app)
    assert job.error == "RuntimeError" and not p.exists()


def test_cancelled_is_not_an_exception_a_broad_except_could_swallow():
    assert issubclass(actions.Cancelled, KeyboardInterrupt)
    assert not issubclass(actions.Cancelled, Exception)


def test_history_import_summary_holds_counts_and_dates_only(root, monkeypatch):
    from cortex.browse import sources

    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    monkeypatch.setattr(sources, "discover", lambda *a, **k: [
        sources.Source("chrome", "Default", root / "History")])
    monkeypatch.setattr(build, "run", lambda *a, **k: {
        "private_written": 2, "kept": 1, "private": 4, "sources": 1, "days_from": "2026-11-01",
        "titles": ["TITLEMARKER"], "error": "URLMARKER", "last": "SEARCHMARKER"})
    job = server.Job("import", "browse", True)
    out = actions.history_import(job, root, {"chrome/Default"})
    assert out["ok"] and out["counts"]["days_written"] == 2
    assert out["counts"]["first_day"] == "2026-11-01"
    assert not any(m in json.dumps(out) + json.dumps(job.lines) for m in MARKERS)
    with pytest.raises(ValueError, match="Tick at least one"):
        actions.history_import(job, root, {"firefox/x"})


def test_history_import_without_the_search_model_stops_first(root, monkeypatch):
    from cortex.browse import sources

    monkeypatch.setattr(sources, "discover", lambda *a, **k: [
        sources.Source("chrome", "Default", root / "History")])
    monkeypatch.setattr(build, "run", lambda *a, **k: pytest.fail("must not import"))
    out = actions.history_import(server.Job("i", "b", True), root, {"chrome/Default"})
    assert out == {"ok": False, "need": "embed", "lines": out["lines"]}


def test_browse_process_answer_holds_counts_only(root, monkeypatch):
    from cortex import browse_serve

    monkeypatch.setattr(browse_serve, "run_import", lambda v, srcs=None: {
        "kept": 3, "model": False, "days_from": "2026-10-01", "title": "TITLEMARKER",
        "url": "https://markerdomain.example/URLMARKER", "search": "SEARCHMARKER"})
    out = actions.browse_process(server.Job("p", "browse", False), root)
    assert out["counts"] == {"kept": 3, "model": False, "days_from": "2026-10-01"}


def test_status_safe_drops_every_free_text_value():
    got = status.safe({"n": 1, "f": 0.5, "b": True, "day": "2026-01-02", "t": "TITLEMARKER",
                       "list": ["x"], "none": None, "d": {"a": 1}})
    assert got == {"n": 1, "f": 0.5, "b": True, "day": "2026-01-02"}
    assert status.safe(["not", "a", "dict"]) == {}


# ---- 2. app/targets: connector files ---------------------------------------------------------

ENTRY = {"command": "cortex", "args": ["mcp", "--vault", "C:\\v"]}


def _baks(p: Path) -> list[Path]:
    return sorted(p.parent.glob(p.name + ".cortex-*.bak"))


def test_edit_json_keeps_other_keys_backs_up_and_removes(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text('{"theme": "dark", "mcpServers": {"other": {"command": "x"}}}',
                 encoding="utf-8-sig")  # a BOM, as Notepad writes it
    assert targets.edit_json(p, "mcpServers", ENTRY, dry_run=True) == "add"
    assert not _baks(p)  # a dry run writes nothing
    assert targets.edit_json(p, "mcpServers", ENTRY) == "add"
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["theme"] == "dark" and d["mcpServers"]["other"] == {"command": "x"}
    assert d["mcpServers"]["cortex"] == ENTRY and len(_baks(p)) == 1
    assert targets.edit_json(p, "mcpServers", ENTRY) == "none"
    assert targets.edit_json(p, "mcpServers", {**ENTRY, "args": []}) == "update"
    assert targets.edit_json(p, "mcpServers", None) == "remove"
    d = json.loads(p.read_text(encoding="utf-8"))
    assert "cortex" not in d["mcpServers"] and d["mcpServers"]["other"] and d["theme"]
    assert targets.edit_json(p, "mcpServers", None) == "none"


@pytest.mark.parametrize("bad", ['{"a": 1,}', "// c\n{}", "[1]", '{"mcpServers": []}'])
def test_edit_json_leaves_a_file_it_cannot_read_whole_untouched(tmp_path, bad):
    p = tmp_path / "mcp.json"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(ValueError, match="untouched"):
        targets.edit_json(p, "mcpServers", ENTRY)
    assert p.read_text(encoding="utf-8") == bad and not _baks(p)


def test_codex_text_round_trip_keeps_other_tables():
    import tomllib

    old = 'model = "o3"\n\n[mcp_servers.other]\ncommand = "x"\n\n[profiles.a]\nk = 1\n'
    added = targets.codex_text(old, ENTRY)
    d = tomllib.loads(added)
    assert d["mcp_servers"]["cortex"] == ENTRY and d["mcp_servers"]["other"] == {"command": "x"}
    assert d["model"] == "o3" and d["profiles"] == {"a": {"k": 1}}
    gone = tomllib.loads(targets.codex_text(added, None))
    assert gone == tomllib.loads(old)
    with pytest.raises(ValueError, match="untouched"):
        targets.codex_text("[a\n", ENTRY)
    with pytest.raises(ValueError, match="by hand"):  # an inline cortex table is not edited
        targets.codex_text('mcp_servers = { cortex = { command = "y" } }\n', ENTRY)


def test_only_lm_studio_gets_private_and_only_with_the_toggle(home):
    def on(v):
        (home / ".cortex").mkdir(exist_ok=True)
        (home / ".cortex" / "config.yaml").write_text(
            f"browse:\n  private_mcp: {str(v).lower()}\n", encoding="utf-8")

    lms = targets.LMStudio()
    others = [t for t in targets.targets() if not isinstance(t, targets.LMStudio)]
    on(False)
    assert targets._base(lms, "private", ENTRY) == ENTRY
    on(True)
    assert targets._base(lms, "private", ENTRY)["args"][-1] == targets.PRIVATE
    assert targets._base(lms, True, ENTRY) == ENTRY  # a plain tick: no --private
    assert all(targets._base(t, "private", ENTRY) == ENTRY for t in others)


# ---- 3. browse/privacy and browse/build ------------------------------------------------------


def test_in_domains_matches_whole_labels_only():
    assert privacy.in_domains("example.com", ["example.com"])
    assert privacy.in_domains("a.b.example.com", ["example.com"])
    assert not privacy.in_domains("evilexample.com", ["example.com"])
    assert not privacy.in_domains("example.com.evil.net", ["example.com"])
    assert not privacy.in_domains("example.com", [])


def test_clean_title_strips_counts_whitespace_and_caps():
    assert privacy.clean_title("(3) Inbox \n\t  news") == "Inbox news"
    assert privacy.clean_title("(12+) Feed") == "Feed"
    assert privacy.clean_title(None) == ""
    assert len(privacy.clean_title("x" * 5000)) == privacy.TITLE_MAX


def _item(n: int, rng: random.Random) -> build.Item:
    cat = rng.choice([None, *privacy.lists.CATEGORIES])
    priv = rng.random() < 0.5 or cat is not None
    search = f"SEARCHMARKER{n}" if rng.random() < 0.3 else None
    k = Kept(f"https://h{n}.markerdomain.example/URLMARKER{n}", f"TITLEMARKER{n}",
             f"h{n}.markerdomain.example", cat, priv, search, "google" if search else None,
             f"vid{n:05d}" if rng.random() < 0.2 else None)
    return build.Item("2026-11-01", datetime(2026, 11, 1, 9, tzinfo=UTC) + timedelta(minutes=n),
                      60.0 * (n + 1), k)


@pytest.mark.parametrize("seed", range(25))
def test_public_body_never_receives_a_hidden_item(seed):
    """Property: over random categories and private flags, the hub rows behind the public page
    are the same with the hidden items removed, and no item's text reaches the page."""
    rng = random.Random(seed)
    items = [_item(n, rng) for n in range(rng.randint(1, 30))]
    where = {}
    for i in items:
        if rng.random() < 0.7:
            where[(i.k.url, False)] = ("hub", rng.choice(["topic-a", "topic-b"]))
    rows = build._hub_rows(items, where)
    shown = [i for i in items if not i.k.private]
    assert rows == build._hub_rows(shown, where)
    assert all(build.public(i, where) is False for i in items if i.k.private)
    body = build.public_body("2026-11-01", rows, 3, "two topics")
    assert not any(m in body for m in MARKERS)
    pages = sum(p + v for _, _, p, v, _ in rows)
    assert pages <= len({i.k.url for i in shown if not i.k.search})


def test_private_body_escapes_untrusted_titles_and_searches():
    k = Kept("https://a.example/x", "<script>alert(1)</script> [x](http://evil.example)",
             "a.example", search="<img src=x>", engine="google")
    it = build.Item("2026-11-01", datetime(2026, 11, 1, 9, tzinfo=UTC), 120.0, k)
    body = build.private_body("2026-11-01", [it, build.Item(it.day, it.start, 0, Kept(
        "https://g.example/s", "", "g.example", search="<img src=x>", engine="google"))],
        {}, {}, UTC)
    assert "<script" not in body and "<img" not in body and "evil.example" not in body
    assert body.startswith("# Browsing 2026-11-01 (private)")


def test_read_inbox_skips_bad_rows_and_caps_fields(root):
    d = root / ".cortex" / "browse"
    d.mkdir(parents=True)
    rows = [{"url": "https://a.example/", "title": "A", "start": 1_762_000_000_000,
             "active_ms": 5000, "excerpt": "e" * 5000, "channel": "c" * 500},
            {"url": "https://b.example/", "start": "2026-11-01T10:00:00Z", "active_ms": -4},
            {"url": "https://c.example/", "start": "not a time"},
            {"url": 7, "start": 1}, ["not", "a", "dict"]]
    (d / "inbox-1.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n",
                                     encoding="utf-8")
    visits, stats = build.read_inbox(Vault(root))
    assert [v.url for v in visits] == ["https://a.example/", "https://b.example/"]
    assert visits[0].secs == 5.0 and visits[1].secs == 0.0  # a negative time counts as none
    assert len(visits[0].excerpt) == 2000 and len(visits[0].channel) == 200
    assert visits[1].start == datetime(2026, 11, 1, 10, tzinfo=UTC)
    assert list(stats) == ["inbox-1.jsonl"]


# ---- 4. browse/sites -------------------------------------------------------------------------


def test_site_keys_for_multi_tenant_medium_and_idn_hosts():
    assert sites.site_of("ana.github.io") != sites.site_of("bob.github.io")
    assert sites.host_of("medium.com/@bob") == "medium.com"
    assert sites.host_of("ana.github.io") == "ana.github.io"
    a, b = sites.page_id("medium.com/@bob", False), sites.page_id("medium.com", False)
    assert a != b and a.startswith("browse-medium.com-")
    assert sites.page_id("medium.com/@bob", True).startswith("browse-private-")
    # punycode is a plain ASCII host; a raw Unicode host is converted to punycode and kept
    assert sites.site_of("www.xn--bcher-kva.de") == "xn--bcher-kva.de"
    assert privacy.drop_reason("https://bücher.de/x") is None
    long = sites.page_id("a" * 120 + ".example", False)
    assert len(long) <= 81 and long != sites.page_id("a" * 121 + ".example", False)


def test_load_cache_survives_a_bad_file_and_on_disk_lists_only_its_files(root):
    v = Vault(root)
    assert sites.load_cache(v) == {"llm": {}, "hashes": {}}
    p = root / sites.CACHE
    p.parent.mkdir(parents=True, exist_ok=True)
    for bad in ("{not json", "[1, 2]", '{"llm": [1], "hashes": "x"}'):
        p.write_text(bad, encoding="utf-8")
        assert sites.load_cache(v) == {"llm": {}, "hashes": {}}
    p.write_text('{"llm": {"a.com": {"category": "news"}}, "hashes": {}}', encoding="utf-8")
    assert sites.load_cache(v)["llm"] == {"a.com": {"category": "news"}}
    assert sites.on_disk(v) == []
    made = [root / sites.SITES_DIR / "browse-a.com.md", root / sites.HIDDEN_CSV]
    for f in made:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x", encoding="utf-8")
    (root / "browse" / "browse-2026-11-01.md").write_text("x", encoding="utf-8")  # a day page
    assert sorted(sites.on_disk(v)) == sorted(made)


# ---- 5. the small privacy gates --------------------------------------------------------------


def test_private_hub_only_for_a_private_topic_hub_on_disk(root):
    from cortex import harvest

    v = Vault(root)
    note(root, "notes/hub-secret.md", 'id: "hub-secret"\ntitle: "S"\nkind: "note"\n'
         "tags: [topic]\nprivate: true\n", "# S")
    note(root, "notes/hub-open.md", 'id: "hub-open"\ntitle: "O"\nkind: "note"\n'
         "tags: [topic]\n", "# O")
    note(root, "notes/plain-secret.md", 'id: "plain-secret"\ntitle: "P"\nkind: "note"\n'
         "private: true\n", "# P")
    assert harvest.private_hub(v, "hub-secret", {})
    assert not harvest.private_hub(v, "hub-secret", {"hub-secret": {}})  # indexed: not private
    assert not harvest.private_hub(v, "hub-open", {})
    assert not harvest.private_hub(v, "plain-secret", {})  # private, but not a hub
    assert not harvest.private_hub(v, "missing", {})
    assert not harvest.private_hub(v, "../me/profile", {})
    assert not harvest.private_hub(v, None, {})


def test_is_hub_and_page_hubs():
    from cortex import topics

    hub = {"kind": "note", "fields": {"tags": ["topic", "x"]}}
    assert topics.is_hub(hub)
    assert not topics.is_hub({"kind": "concept", "fields": {"tags": ["topic"]}})
    assert not topics.is_hub({"kind": "note", "fields": {"tags": "topic"}})
    page = {"fields": {"related": ["[[a]]", "[[b]]", "[[a]]", "[[secret]]"]}}
    assert topics.page_hubs(page, {"a": None, "b": "a"}) == ["a", "b"]
    assert topics.page_hubs({"fields": {}}, {"a": None}) == []


def test_people_hub_lists_only_public_people(root):
    from cortex import relink

    v = Vault(root)
    assert relink.people_fixes(v) == {}  # nothing until the first people page
    note(root, "people/asha.md", 'id: "asha"\ntitle: "Asha"\n', "# Asha")  # private by default
    note(root, "people/bob.md", 'id: "bob"\ntitle: "Bob"\nprivate: false\n', "# Bob")
    fixes = relink.people_fixes(v)
    hub = dict(fixes[v.path_for("note", relink.HUB)])["people_hub"]("")
    assert "[[people/bob]]" in hub and "asha" not in hub.lower()
    assert v.path_for("me", "profile") in fixes
    done = relink.apply(v, fixes)
    assert done["people_hub"] == 1 and "[[people]]" in (root / "people" / "asha.md").read_text(
        encoding="utf-8")


def test_is_not_me():
    from cortex import claims

    page = "# Profile\n\n## Not me\n- I love  Java\n\n## Facts\n- I like Python\n"
    assert claims.is_not_me("profile", page, "i love java")
    assert not claims.is_not_me("profile", page, "I like Python")


@pytest.mark.parametrize("ok", ["professor", "people/asha", "a-b-1", "people/a.b-c"])
def test_check_audience_accepts_tags_and_people(ok):
    from cortex import voice

    assert voice.check_audience(ok) == ok


@pytest.mark.parametrize("bad", ["", "Professor", "people/../me", "people/", "a b", "x" * 41,
                                 "people/asha/x", None, 3, "tag\n"])
def test_check_audience_refuses_anything_else(bad):
    from cortex import voice

    with pytest.raises(VaultError):
        voice.check_audience(bad)


def test_export_target_refused_in_the_vault_the_repo_and_excluded_folders(root, home, tmp_path):
    from cortex import export_context

    v = Vault(root)
    for inside in (root / "me" / "x.md", root, export_context.REPO / "out.md"):
        with pytest.raises(VaultError, match="Refusing"):
            export_context.check_target(inside, v)
    ex = tmp_path / "shared"
    (home / ".cortex").mkdir(exist_ok=True)
    (home / ".cortex" / "config.yaml").write_text(f"exclude: [{json.dumps(str(ex))}]\n",
                                                  encoding="utf-8")
    with pytest.raises(VaultError, match="excluded"):
        export_context.check_target(ex / "sub" / "ctx.md", v)
    ok = tmp_path / "out" / "ctx.md"
    assert export_context.check_target(ok, v) == ok.resolve()
