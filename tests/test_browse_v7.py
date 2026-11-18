"""v7 review C (data, privacy, local models), on synthetic History and temp vaults only:
C1 search terms and uncategorised titles stay local until a local-model verdict clears them,
C2 a settings change re-filters everything stored and `cortex browse forget`, C3 private pages
out of the vault's git, C5/C16 the extension inbox archive, C12 the private search signature,
C14 macOS and Linux browser paths."""

import json
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path

import pytest
from conftest import FakeEmbed, git
from test_browse import LINK, Embed, chromium, hubbed, m
from test_browse_sites import OWNER, hidden, visible
from typer.testing import CliRunner

from cortex import cli, cloudllm, doctor, localllm, semantic, session
from cortex.browse import build, forget, privacy, sites, sources, vectors
from cortex.catalog import Catalog
from cortex.index import Index
from cortex.vault import Vault

ROWS = [
    (m(0), "https://lichess.org/study/abc", "Chess openings for beginners", 300),
    (m(5), "https://www.google.com/search?q=pcos+treatment", "pcos treatment - Google", 0,
     LINK, "pcos treatment"),
    (m(6), "https://www.google.com/search?q=ways+to+stop+existing", "ways to stop existing", 0,
     LINK, "ways to stop existing"),
    (m(7), "https://www.google.com/search?q=personal+loan+emi", "personal loan emi", 0,
     LINK, "personal loan emi"),
    (m(20), "https://www.google.com/search?q=chess+tactics", "chess tactics - Google", 0,
     LINK, "chess tactics"),
    (m(21), "https://www.chess.com/lessons/1", "Chess tactics lesson", 300),
    (m(30), "https://blog.example.org/post", "Living quietly with my condition", 300),
    (m(50), "https://www.webmd.com/flu", "Flu HEALTHMARKER", 120),
]
SECRET = ("pcos", "stop existing", "loan emi")


@pytest.fixture
def vault(root):
    return hubbed(root)


@pytest.fixture
def hist(tmp_path):
    local = tmp_path / "local"
    chromium(local / "Google" / "Chrome" / "User Data" / "Default" / "History", ROWS)
    return local, tmp_path / "roaming"


def imp(r, hist, settings=OWNER, **kw):
    kw.setdefault("embed", Embed())
    return build.run(Vault(r), sources.discover(*hist), settings, tz=UTC, now=m(0, 2), **kw)


def fake_local(labels: dict[str, str | None]):
    """A real `localllm.Runner` (so `type(r) is Runner`) whose model calls are stubbed."""
    r = localllm.Runner("ollama", "http://127.0.0.1:9", "fake")
    r.asked = []

    def classify(text, labels_, *, timeout=20, budget=None):
        r.asked.append(text)
        got = labels.get(text, "ordinary")
        return None if got is None else (got, 0.9)
    r.classify = classify
    r.summarise = lambda *a, **k: None
    return r


def visible_text(r) -> str:
    return b"".join(visible(r).values()).decode("utf-8").lower()


# ---- C1 ----------------------------------------------------------------------------------

@pytest.mark.parametrize("text,cat", [("pcos treatment", "health"),
                                      ("ways to stop existing", "selfharm"),
                                      ("personal loan emi", "money"),
                                      ("I dont want to live anymore", "selfharm"),
                                      ("home loan interest rates", "money")])
def test_c1_wider_word_lists_catch_the_review_examples(text, cat):
    assert privacy.category("www.google.com", "/search", text, text)[0] == cat


@pytest.mark.parametrize("text", ["kill process linux", "binary tree depth",
                                  "depth first search", "EMI filter design",
                                  "dead code elimination", "acid base titration",
                                  "career counselling", "getting high accuracy",
                                  "Parkinson's law project", "lean startup"])
def test_c1_ordinary_study_terms_are_not_caught(text):
    assert privacy.category("www.google.com", "/search", text, text)[0] is None


def test_c1_sensitive_searches_never_reach_a_visible_page_or_the_index(vault, hist):
    imp(vault, hist)
    text = visible_text(vault)
    for w in SECRET:
        assert w not in text, w
    idx = Index(Vault(vault), Catalog.load(Vault(vault)))
    assert idx.find("stop existing") == [] and idx.find("pcos") == []
    priv = b"".join(hidden(vault).values()).decode().lower()
    assert all(w in priv for w in SECRET)  # still on the private pages


def test_c1_search_terms_are_local_without_a_local_model(vault, hist):
    imp(vault, hist)
    text = visible_text(vault)
    assert "chess tactics lesson" in text  # an ordinary title stays visible (word lists only)
    assert "searches that led here" not in text and "chess tactics ·" not in text
    days = (vault / sites.DAYS).read_text(encoding="utf-8")
    assert "chess tactics" in days  # stored for the private pages, never shown


def test_c1_a_local_ordinary_verdict_clears_a_search(vault, hist):
    r = fake_local({"Living quietly with my condition": "health"})
    imp(vault, hist, runner=r)
    assert "chess tactics" in r.asked and "pcos treatment" not in r.asked  # lists came first
    cc = visible(vault)["browse/sites/gaming/browse-chess.com.md"].decode()
    assert "## Searches that led here\n- chess tactics · 1" in cc
    text = visible_text(vault)
    assert "living quietly" not in text  # the local model said health: hidden
    assert "living quietly" in b"".join(hidden(vault).values()).decode().lower()
    assert all(w not in text for w in SECRET)


def test_c1_no_verdict_keeps_a_title_local_and_verdicts_are_cached(vault, hist):
    r = fake_local({"Living quietly with my condition": None, "chess tactics": None})
    imp(vault, hist, runner=r)
    text = visible_text(vault)
    assert "living quietly" not in text and "chess tactics ·" not in text
    cache = (vault / sites.VERDICTS).read_text(encoding="utf-8")
    assert "chess" not in cache.lower() and len(cache) > 2  # hashes, never the text
    again = fake_local({})
    imp(vault, hist, runner=again, since="2026-11-09")
    assert "Chess openings for beginners" not in again.asked  # cached "ordinary"
    assert "Living quietly with my condition" in again.asked  # asked again: no verdict yet


def test_c1_the_cloud_runner_is_never_the_classifier(vault, hist, monkeypatch):
    cloud = cloudllm.CloudRunner("openai", "https://api.example.com/v1", "m")

    def boom(*a, **k):
        raise AssertionError("a cloud runner classified browsing")
    monkeypatch.setattr(cloudllm.CloudRunner, "classify", boom)
    monkeypatch.setattr(cloudllm.CloudRunner, "summarise", lambda *a, **k: None)
    assert build.local_only(cloud) is None
    imp(vault, hist, runner=cloud)
    assert "chess tactics ·" not in visible_text(vault)  # no verdict: still local
    off = localllm.Runner("ollama", "http://example.com:11434", "m")
    assert build.local_only(off) is None  # not loopback


def test_c1_a_sensitive_verdict_on_a_search_follows_its_category(vault, hist):
    r = fake_local({"chess tactics": "betting"})
    imp(vault, hist, replace(OWNER, skip_categories=("adult", "betting")), runner=r)
    every = b"".join(p.read_bytes() for p in vault.rglob("*")
                     if p.is_file() and ".git" not in p.parts).lower()
    assert b"chess tactics -" not in every and b'"chess tactics"' not in every


# ---- C2 ----------------------------------------------------------------------------------

PRIV = Path("browse") / "private"


def every_byte(r) -> bytes:
    return b"".join(p.read_bytes() for p in Path(r).rglob("*")
                    if p.is_file() and ".git" not in p.parts).lower()


def forgotten_day(vault, day="2026-01-05"):
    """A day the browsers no longer hold: its private page and site counts stay on disk."""
    src = vault / PRIV / "browse-private-2026-11-09.md"
    (src.parent / f"browse-private-{day}.md").write_text(
        src.read_text(encoding="utf-8").replace("2026-11-09", day), encoding="utf-8")
    days = json.loads((vault / sites.DAYS).read_text(encoding="utf-8"))
    days[day] = days["2026-11-09"]
    (vault / sites.DAYS).write_text(json.dumps(days), encoding="utf-8")


@pytest.fixture
def fake_embed(monkeypatch):
    f = FakeEmbed()
    monkeypatch.setattr(semantic, "_embedder", f)
    return f


def test_c2_a_new_skip_category_reaches_every_store(vault, hist, fake_embed):
    imp(vault, hist)
    forgotten_day(vault)
    vectors.update(Vault(vault))
    assert b"healthmarker" in every_byte(vault) and b"webmd" in every_byte(vault)
    rep = imp(vault, hist, replace(OWNER, skip_categories=("adult", "health")))
    assert rep["refiltered_day_pages"] >= 1 and rep["refiltered_site_keys"] >= 1
    every = every_byte(vault)
    for w in (b"healthmarker", b"webmd", b"pcos"):
        assert w not in every, w
    old = (vault / PRIV / "browse-private-2026-01-05.md").read_text(encoding="utf-8")
    assert "Chess openings for beginners" in old  # the rest of a forgotten day stays
    assert set(vectors._read(Vault(vault))) <= {p.relative_to(vault).as_posix()
                                                 for p in (vault / "browse").rglob("*.md")}


def test_c2_a_denied_domain_leaves_forgotten_days_and_the_cache(vault, hist):
    imp(vault, hist)
    forgotten_day(vault)
    cache = sites.load_cache(Vault(vault))
    cache["llm"]["lichess.org"] = {"category": "gaming", "v": {
        "desc": "A chess site", "fed": [], "related": [], "topics": []}}
    cache["llm"]["chess.com"] = {"v": {"desc": "x", "fed": [], "related": ["lichess.org"],
                                       "topics": []}}
    sites._save(Vault(vault), sites.CACHE, cache)
    imp(vault, hist, replace(OWNER, deny=("lichess.org",)))
    every = every_byte(vault)
    assert b"lichess" not in every and b"chess openings for beginners" not in every
    assert b"chess tactics lesson" in every


def test_c2_the_same_settings_do_not_refilter(vault, hist):
    imp(vault, hist)
    again = imp(vault, hist)
    assert not any(v for k, v in again.items() if k.startswith("refiltered"))


def test_c2_forget_a_site_from_the_cli(vault, hist, home, fake_embed):
    imp(vault, hist)
    forgotten_day(vault)
    vectors.update(Vault(vault))
    arch = vault / build.ARCHIVE
    arch.mkdir(parents=True)
    (arch / "inbox-2026-11-09.jsonl").write_text(json.dumps(
        {"url": "https://lichess.org/x", "title": "Lichess from the extension",
         "start": m(90).isoformat(), "active_ms": 1000}) + "\n" + json.dumps(
        {"url": "https://example.net/y", "title": "Kept", "start": m(91).isoformat(),
         "active_ms": 1000}) + "\n", encoding="utf-8")
    out = CliRunner().invoke(cli.app, ["browse", "forget", "--site", "lichess.org",
                                       "--vault", str(vault)])
    assert out.exit_code == 0, out.output
    assert "forgot lichess.org:" in out.output and "deny_domains" in out.output
    assert "chess openings" not in out.output.lower()  # counts only
    every = every_byte(vault)
    assert b"lichess" not in every and b"chess openings for beginners" not in every
    assert b'"kept"' in every and b"chess tactics lesson" in every
    assert all("lichess" not in p for p in vectors._read(Vault(vault)))


def test_c2_forget_a_category(vault, hist, home):
    imp(vault, hist)
    forgotten_day(vault)
    out = CliRunner().invoke(cli.app, ["browse", "forget", "--category", "health",
                                       "--vault", str(vault)])
    assert out.exit_code == 0, out.output
    every = every_byte(vault)
    assert b"healthmarker" not in every and b"webmd" not in every and b"pcos" not in every
    assert b"chess openings for beginners" in every
    for bad in (["--category", "cats"], ["--site", "not a domain"], [],
                ["--site", "a.com", "--category", "health"]):
        r = CliRunner().invoke(cli.app, ["browse", "forget", *bad, "--vault", str(vault)])
        assert r.exit_code == 1, bad


# ---- C3 ----------------------------------------------------------------------------------

def test_c3_tracked_private_pages_leave_git_once_and_history_is_warned(vault, hist):
    old = vault / PRIV / "browse-private-2026-01-01.md"
    old.parent.mkdir(parents=True)
    old.write_text("---\nid: browse-private-2026-01-01\nprivate: true\n---\nsecret\n",
                   encoding="utf-8")
    git(vault, "add", "-A")
    git(vault, "commit", "-q", "-m", "an old commit with a private page")
    (vault / "notes" / "staged.md").write_text("mine\n", encoding="utf-8")
    git(vault, "add", "notes/staged.md")  # the owner's own staged change is left alone
    imp(vault, hist)
    assert forget.PRIVATE_IGNORE in (vault / ".gitignore").read_text(
        encoding="utf-8").splitlines()
    assert git(vault, "ls-files", PRIV.as_posix()) == ""
    assert git(vault, "log", "-1", "--format=%s") == forget.UNTRACK_MESSAGE + "\n"
    shown = git(vault, "show", "--name-only", "--format=", "HEAD").split()
    assert shown == [(PRIV / "browse-private-2026-01-01.md").as_posix()]
    assert "A  notes/staged.md" in git(vault, "status", "--porcelain")
    assert PRIV.as_posix() not in git(vault, "status", "--porcelain", "--untracked-files=all")
    assert old.is_file()
    status, _, detail, fix = doctor.check_private_browse_git(vault)
    # the old commit and the one that took the page out both hold it
    assert status == "WARN" and "2 commits" in detail and "filter-repo" in fix
    assert not forget.untrack_private(Vault(vault))  # once only


def test_c3_commits_never_take_ignored_private_pages(vault, hist):
    imp(vault, hist)
    v = Vault(vault)
    page = vault / "notes" / "a-new-note.md"
    page.write_text("x\n", encoding="utf-8")
    private = list((vault / PRIV).rglob("*.md"))
    assert private
    assert session._commit(v, "cortex: test", [page, *private])
    assert git(vault, "ls-files", PRIV.as_posix()) == ""
    assert "notes/a-new-note.md" in git(vault, "ls-files", "notes")


def test_c3_init_writes_and_upgrades_the_gitignore(tmp_path, home):
    assert forget.PRIVATE_IGNORE == cli.PRIVATE_BROWSE_IGNORE
    assert forget.PRIVATE_IGNORE in cli.GITIGNORE.splitlines()
    r = tmp_path / "old"
    r.mkdir()
    (r / ".gitignore").write_text(".cortex/*\n", encoding="utf-8")
    cli._init(r)
    assert forget.PRIVATE_IGNORE in (r / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_c3_doctor_passes_a_clean_vault(root):
    (root / ".gitignore").write_text(f".cortex/*\n{forget.PRIVATE_IGNORE}\n", encoding="utf-8")
    assert doctor.check_private_browse_git(root)[0] == "PASS"
    (root / ".gitignore").write_text(".cortex/*\n", encoding="utf-8")
    assert doctor.check_private_browse_git(root)[0] == "WARN"


# ---- C5 and C16 --------------------------------------------------------------------------

EXT = {"url": "https://lichess.org/extonly", "title": "Chess from the extension",
       "start": m(200).isoformat(), "active_ms": 60_000, "excerpt": "a public excerpt"}


def test_c5_extension_only_visits_survive_a_rebuild(vault, hist):
    d = vault / ".cortex" / "browse"
    d.mkdir(parents=True)
    hiv = {"url": "https://www.webmd.com/x", "title": "HIV symptoms",
           "start": m(201).isoformat(), "active_ms": 1000, "excerpt": "leaked?"}
    (d / "inbox-2026-11-09.jsonl").write_text(
        json.dumps(EXT) + "\n" + json.dumps(hiv) + "\n", encoding="utf-8")
    imp(vault, hist, replace(OWNER, hidden_from_claude=("adult", "health")))
    page = vault / PRIV / "browse-private-2026-11-09.md"
    assert "Chess from the extension" in page.read_text(encoding="utf-8")
    assert not (d / "inbox-2026-11-09.jsonl").exists()
    archived = (vault / build.ARCHIVE / "inbox-2026-11-09.jsonl").read_text(encoding="utf-8")
    assert "a public excerpt" in archived and "leaked?" not in archived  # private: no excerpt
    imp(vault, hist, replace(OWNER, hidden_from_claude=("adult", "health", "money")))  # reset
    assert "Chess from the extension" in page.read_text(encoding="utf-8")
    assert "extonly" in (vault / sites.DAYS).read_text(encoding="utf-8")


def test_c16_every_read_file_is_archived_and_old_ones_pruned(vault, hist):
    d = vault / ".cortex" / "browse"
    d.mkdir(parents=True)
    early = {**EXT, "start": (m(0) - timedelta(days=3)).isoformat()}
    (d / "inbox-2026-11-06.jsonl").write_text(json.dumps(early) + "\n", encoding="utf-8")
    ancient = {**EXT, "start": (m(0) - timedelta(days=300)).isoformat()}
    (vault / build.ARCHIVE).mkdir(parents=True)
    (vault / build.ARCHIVE / "inbox-2026-01-13.jsonl").write_text(
        json.dumps(ancient) + "\n", encoding="utf-8")
    rep = imp(vault, hist, since="2026-11-09")  # the 6th is before the rebuild
    assert not list(d.glob("inbox-*.jsonl"))  # read whole: archived whatever its date
    assert rep["archive_pruned"] >= 1  # older than the browsers' history
    assert not (vault / build.ARCHIVE / "inbox-2026-01-13.jsonl").exists()


# ---- C12 ---------------------------------------------------------------------------------

def test_c12_search_reads_the_pages_only_when_they_changed(vault, hist, fake_embed,
                                                           monkeypatch):
    imp(vault, hist)
    v = Vault(vault)
    vectors.update(v)
    calls = []
    real = vectors._files
    monkeypatch.setattr(vectors, "_files", lambda *a, **k: calls.append(1) or real(*a, **k))
    vectors.search(v, "chess")
    vectors.search(v, "chess")
    assert calls == []  # the signature matched: nothing read or hashed
    p = vault / PRIV / "browse-private-2026-11-09.md"
    p.write_text(p.read_text(encoding="utf-8") + "- one more line\n", encoding="utf-8")
    vectors.search(v, "chess")
    assert calls == [1]


def test_c12_prune_needs_no_model(vault, hist, fake_embed, monkeypatch):
    imp(vault, hist)
    v = Vault(vault)
    vectors.update(v)
    gone = vault / PRIV / "browse-private-2026-11-09.md"
    gone.unlink()
    monkeypatch.setattr(semantic, "_embedder", None)
    monkeypatch.setattr(semantic, "load_embedder", lambda *a, **k: None)
    assert vectors.prune(v) == 1
    assert gone.relative_to(vault).as_posix() not in vectors._read(v)


# ---- C14 ---------------------------------------------------------------------------------

def _history(path: Path, name: str = "History"):
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_bytes(b"x")


def test_c14_macos_and_linux_browser_folders(tmp_path):
    mac = tmp_path / "mac"
    base = mac / "Library" / "Application Support"
    _history(base / "Google" / "Chrome" / "Default")
    _history(base / "Microsoft Edge" / "Profile 1")
    _history(base / "BraveSoftware" / "Brave-Browser" / "Default")
    _history(base / "Firefox" / "Profiles" / "abc.default-release", "places.sqlite")
    got = [s.key for s in sources.discover(home=mac, platform="darwin")]
    assert got == ["chrome/Default", "edge/Profile 1", "brave/Default",
                   "firefox/abc.default-release"]
    lin = tmp_path / "linux"
    _history(lin / ".config" / "google-chrome" / "Default")
    _history(lin / ".config" / "microsoft-edge" / "Default")
    _history(lin / ".config" / "BraveSoftware" / "Brave-Browser" / "Profile 2")
    _history(lin / ".mozilla" / "firefox" / "x.default", "places.sqlite")
    (lin / ".config" / "google-chrome" / "Crash Reports").mkdir()
    got = [s.key for s in sources.discover(home=lin, platform="linux")]
    assert got == ["chrome/Default", "edge/Default", "brave/Profile 2", "firefox/x.default"]
    assert sources.discover(home=tmp_path / "empty", platform="linux") == []


def test_c14_windows_is_unchanged(tmp_path, monkeypatch):
    local, roaming = tmp_path / "L", tmp_path / "R"
    _history(local / "Google" / "Chrome" / "User Data" / "Default")
    _history(roaming / "Mozilla" / "Firefox" / "Profiles" / "p", "places.sqlite")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))
    assert [s.key for s in sources.discover(platform="win32")] == ["chrome/Default",
                                                                    "firefox/p"]
    # folders given: the Windows layout on any platform, as before
    assert [s.key for s in sources.discover(local, roaming, platform="linux")] == [
        "chrome/Default", "firefox/p"]


# --- C9: the cloud allowlist ------------------------------------------------------------------

def test_c9_cloud_allowlist_and_legacy_hidden_list_migrate(monkeypatch):
    load, cats = privacy.load_settings, privacy.lists.CATEGORIES
    s = load({"browse": {"cloud_categories": ["money"]}})
    assert s.hidden_from_claude == tuple(c for c in cats if c != "money")
    assert load({"browse": {"cloud_categories": []}}).hidden_from_claude == cats
    assert load({}).hidden_from_claude == cats  # the default: every category local
    # a v0.1.0 config listed what to hide; it keeps meaning the same
    assert load({"browse": {"hidden_from_claude": ["adult", "health"]}}).hidden_from_claude \
        == ("adult", "health")
    # a category added after v0.1.0 is in neither list, so it stays local (fail closed)
    monkeypatch.setattr(privacy.lists, "CATEGORIES", (*cats, "newcat"))
    assert "newcat" in load({"browse": {"hidden_from_claude": ["adult"]}}).hidden_from_claude
    assert "newcat" in load({"browse": {"cloud_categories": ["money"]}}).hidden_from_claude
