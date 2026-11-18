"""The site pages, the profile and `cortex browse deepen` (browse-brain.md §3a), on synthetic
History only. The core rule: nothing from a hidden category ever reaches a file outside
browse/private/."""

import json
import time
from dataclasses import replace
from datetime import UTC, timedelta

import pytest
from test_browse import LINK, Embed, chromium, hubbed, m

from cortex import browse_serve as bs
from cortex import localllm
from cortex.browse import Settings, build, privacy, sites, sources
from cortex.browse.build import Item
from cortex.vault import Vault, VaultError

# the owner's settings: Claude may see everything but adult, which is kept private
OWNER = Settings(adult="private", hidden_from_claude=("adult",), visible_to_claude=True)
HIDDEN_MARKERS = ("pornhub", "adulttitle", "adultterm", "adultvid", "browse-private")
ROWS = [
    (m(0), "https://lichess.org/study/abc", "Chess openings for beginners", 300),
    (m(5), "https://www.pornhub.com/view_video.php?viewkey=zz", "ADULTTITLE clip", 600),
    (m(20), "https://www.google.com/search?q=porn+adultterm", "porn adultterm - Google", 0,
     LINK, "porn adultterm"),
    (m(22), "https://lichess.org/study/def", "Chess middlegame plans", 300),  # led by the search
    (m(30), "https://www.youtube.com/watch?v=abcdefghijk", "nsfw ADULTVID - YouTube", 200),
    (m(35), "https://www.youtube.com/watch?v=bbcdefghijk", "Chess endgame - YouTube", 400),
    (m(40), "https://www.google.com/search?q=chess+tactics", "chess tactics - Google", 0,
     LINK, "chess tactics"),
    (m(41), "https://www.chess.com/lessons/1", "Chess tactics lesson", 300),
    (m(50), "https://www.webmd.com/flu", "Flu HEALTHMARKER", 120),
    (m(60), "https://ana.github.io/blog/post", "Ana's blog post", 60),
    (m(70), "https://medium.com/@Bob/a-story", "Bob writes", 60),
]


@pytest.fixture
def vault(root):
    return hubbed(root)


@pytest.fixture
def hist(tmp_path):
    local, roaming = tmp_path / "local", tmp_path / "roaming"
    chromium(local / "Google" / "Chrome" / "User Data" / "Default" / "History", ROWS)
    return local, roaming


def imp(r, hist, settings=OWNER, **kw):
    kw.setdefault("embed", Embed())
    return build.run(Vault(r), sources.discover(*hist), settings, tz=UTC, now=m(0, 2), **kw)


def visible(r):
    return {p.relative_to(r).as_posix(): p.read_bytes() for p in (r / "browse").rglob("*")
            if p.is_file() and not p.is_relative_to(r / "browse" / "private")}


def hidden(r):
    return {p.relative_to(r).as_posix(): p.read_bytes()
            for p in (r / "browse" / "private").rglob("*") if p.is_file()}


def assert_clean(r, markers=HIDDEN_MARKERS):
    for rel, data in visible(r).items():
        low = data.lower()
        for w in markers:
            assert w.encode() not in low, (rel, w)


def test_a_hidden_category_never_reaches_a_visible_file(vault, hist):
    rep = imp(vault, hist)
    assert rep["sites_visible"] and rep["sites_hidden"]
    assert_clean(vault)
    v = visible(vault)
    chess = v["browse/sites/gaming/browse-lichess.org.md"].decode()
    assert "Chess openings for beginners" in chess and "chess tactics" not in chess
    assert "## Searches that led here" not in chess  # only the hidden search led there
    assert "[[browse-chess.com]]" in chess or "[[browse-youtube.com]]" in chess  # co-visits
    yt = v["browse/sites/video/browse-youtube.com.md"].decode()
    assert "Chess endgame" in yt and "visits: 1\n" in yt  # the hidden video is not counted
    cc = v["browse/sites/gaming/browse-chess.com.md"].decode()
    assert "## Searches that led here\n- chess tactics · 1" in cc
    assert "browse/sites/health/browse-webmd.com.md" in v  # health is not hidden for the owner
    profile = v["browse/browse-profile.md"].decode()
    assert "## Top sites" in profile and "adult" not in profile
    assert "lichess.org,gaming,2," in v["browse/sites.csv"].decode()
    h = hidden(vault)
    priv = b"".join(h.values()).decode()
    for w in ("ADULTTITLE", "porn adultterm", "ADULTVID"):
        assert w in priv
    pp = h["browse/private/browse-private-profile.md"].decode()
    assert "## Hidden categories" in pp and "### adult" in pp and "- porn adultterm · 1" in pp
    assert "[[browse-private-pornhub.com]]" in pp
    assert "private: true" in h["browse/private/sites/adult/browse-private-youtube.com.md"]\
        .decode()


def test_nothing_is_visible_when_visible_to_claude_is_off(vault, hist):
    imp(vault, hist, replace(OWNER, visible_to_claude=False))
    assert visible(vault) == {}
    assert "browse/private/sites/gaming/browse-private-lichess.org.md" in hidden(vault)


def test_p2_hiding_later_removes_what_was_visible(vault, hist):
    """Built with health visible, then health hidden, then visibility off: each rebuild leaves
    no visible file holding what is now hidden (privacy review P2)."""
    imp(vault, hist)
    assert any(b"HEALTHMARKER" in d for d in visible(vault).values())
    assert (vault / "browse" / "browse-2026-11-09.md").is_file()
    rep = imp(vault, hist, replace(OWNER, hidden_from_claude=("adult", "health")))
    assert rep["sites_removed"] >= 1
    assert_clean(vault, (*HIDDEN_MARKERS, "healthmarker", "webmd"))
    assert "browse/private/sites/health/browse-private-webmd.com.md" in hidden(vault)
    imp(vault, hist, replace(OWNER, visible_to_claude=False))
    assert visible(vault) == {}


def test_p2_a_public_day_page_the_browser_forgot_goes_when_rules_change(vault, hist):
    imp(vault, hist)
    old = vault / "browse" / "browse-2026-01-05.md"
    old.write_text((vault / "browse" / "browse-2026-11-09.md").read_text(encoding="utf-8"),
                   encoding="utf-8")
    imp(vault, hist)  # the same rules: a page older than the history stays
    assert old.is_file()
    imp(vault, hist, replace(OWNER, hidden_from_claude=("adult", "health")))
    assert not old.exists()


def test_skip_categories_are_never_stored(vault, hist):
    s = replace(OWNER, skip_categories=("adult",))
    imp(vault, hist, s)
    every = b"".join(p.read_bytes() for p in vault.rglob("*")
                     if p.is_file() and ".git" not in p.parts).lower()
    for w in ("adulttitle", "adultterm", "adultvid", "pornhub"):
        assert w.encode() not in every, w
    # a category skipped later is purged from the stored counts too
    imp(vault, hist, replace(s, skip_categories=("adult", "health")), since="2026-11-09")
    days = (vault / sites.DAYS).read_text(encoding="utf-8")
    assert "webmd" not in days and "|health|" not in days


def test_multi_tenant_sites_and_slugs():
    assert sites.site_of("ana.github.io", "/blog") == "ana.github.io"
    assert sites.site_of("x.ana.github.io") == "ana.github.io"
    assert sites.site_of("medium.com", "/@Bob/story") == "medium.com/@bob"
    assert sites.site_of("medium.com", "/topic/x") == "medium.com"
    assert sites.site_of("bob.substack.com", "/p/x") == "bob.substack.com"
    assert sites.site_of("my-app.vercel.app") == "my-app.vercel.app"
    assert sites.site_of("team.notion.site") == "team.notion.site"
    assert sites.site_of("en.m.wikipedia.org") == "wikipedia.org"
    assert sites.site_of("www.bbc.co.uk") == "bbc.co.uk"
    assert sites.site_of("docs.google.com") == "docs.google.com"
    assert sites.page_id("medium.com/@bob", False) == "browse-medium.com-bob"
    assert sites.page_id("ana.github.io", True) == "browse-private-ana.github.io"
    long = sites.page_id("a" * 120 + ".github.io", False)
    assert len(long) <= 81 and long.startswith("browse-a")
    assert sites.site_category("github.com", {}) == "programming"
    assert sites.site_category("webmd.com", {"webmd.com": {"category": "news"}}) == "health"
    assert sites.site_category("x.example", {"x.example": {"category": "food"}}) == "food"
    assert sites.site_category("x.example", {"x.example": {"category": "bogus"}}) == "other"


def test_an_incremental_rebuild_writes_nothing(vault, hist):
    imp(vault, hist)
    before = {**visible(vault), **hidden(vault)}
    rep = imp(vault, hist, since="2026-11-09")
    assert rep["sites_written"] == 0 and rep["sites_removed"] == 0 and rep["sites_same"] > 0
    assert {**visible(vault), **hidden(vault)} == before


class FakeLLM:
    def __init__(self, answer=None, cost=1.0):
        self.asked, self.cost, self.answer = [], cost, answer

    def site(self, name, titles, labels, candidates, topics, *, budget=None):
        self.asked.append((name, list(titles), list(candidates)))
        if budget is not None:
            budget.spend(self.cost)
        if self.answer:
            return self.answer(name, candidates)
        return {"category": "programming", "description": f"About {name} [link](http://x)",
                "related": candidates[:2] + ["not-a-candidate.com"], "topics": ["chess hub"]}


def test_deepen_respects_the_budget_and_resumes_from_the_cache(vault, hist):
    imp(vault, hist)
    llm = FakeLLM()
    r = sites.deepen(Vault(vault), llm, localllm.Budget(2.5), OWNER, tz=UTC)
    assert r["asked"] == 3 and r["left"] > 0
    first = [a[0] for a in llm.asked]
    cache = json.loads((vault / sites.CACHE).read_text(encoding="utf-8"))["llm"]
    assert set(first) <= set(cache)
    again = FakeLLM()
    r2 = sites.deepen(Vault(vault), again, localllm.Budget(100), OWNER, tz=UTC)
    assert not {a[0] for a in again.asked} & set(first)  # resumed, nothing asked twice
    assert r2["left"] == 0
    done = FakeLLM()
    assert sites.deepen(Vault(vault), done, localllm.Budget(100), OWNER, tz=UTC)["asked"] == 0
    # a visible page's call heard only visible titles and visible candidate sites
    for name, titles, cands in llm.asked + again.asked:
        if not any("ADULT" in t for t in titles):  # a visible page
            assert not any("pornhub" in c for c in cands), name
    chess = (vault / "browse/sites/gaming/browse-lichess.org.md").read_text(encoding="utf-8")
    assert "About lichess.org" in chess and "http" not in chess.split("---", 2)[2]
    assert "[[chess]]" in chess and "not-a-candidate" not in chess
    assert_clean(vault)


def test_deepen_never_moves_a_site_out_of_a_sensitive_category(vault, hist):
    imp(vault, hist)
    llm = FakeLLM(lambda name, c: {"category": "news" if name == "webmd.com" else "adult"
                                   if name == "ana.github.io" else "bogus",
                                   "description": "x", "related": [], "topics": []})
    sites.deepen(Vault(vault), llm, localllm.Budget(100), OWNER, tz=UTC)
    v, h = visible(vault), hidden(vault)
    assert "browse/sites/health/browse-webmd.com.md" in v  # still health
    # moved INTO adult: the whole site is hidden now
    assert not any("ana.github.io" in k for k in v)
    assert "browse/private/sites/adult/browse-private-ana.github.io.md" in h
    assert "browse/sites/other/browse-medium.com-bob.md" in v  # "bogus" is not a category
    assert_clean(vault, (*HIDDEN_MARKERS, "ana.github"))


def test_run_deepen_needs_a_runner_and_updates_the_vectors(vault, monkeypatch):
    called = []
    monkeypatch.setattr(bs, "local_runner", lambda budget_s=None: (None, None))
    assert bs.run_deepen(Vault(vault)) is None
    from cortex.browse import vectors

    monkeypatch.setattr(vectors, "update", lambda v, progress=None: called.append(v) or {})
    monkeypatch.setattr(bs, "local_runner",
                        lambda budget_s=None: (FakeLLM(), localllm.Budget(budget_s or 5)))
    lines = []
    r = bs.run_deepen(Vault(vault), say=lines.append, budget_s=3)
    assert r is not None and called and lines
    assert all("://" not in ln for ln in lines)


def test_a_vector_failure_is_reported_not_raised(vault, monkeypatch):
    from cortex.browse import vectors

    def boom(v, progress=None):
        raise RuntimeError("nope")

    monkeypatch.setattr(vectors, "update", boom)
    lines = []
    assert sites.update_vectors(Vault(vault), lines.append) is None
    assert lines == ["private browse search not updated: RuntimeError"]

    def unset(v, progress=None):
        raise VaultError("not_set_up", "Run cortex model download first.")

    monkeypatch.setattr(vectors, "update", unset)
    lines.clear()
    sites.update_vectors(Vault(vault), lines.append)
    assert lines == ["private browse search not updated: Run cortex model download first."]


def test_the_receiver_deepens_once_after_30_idle_minutes(tmp_path, monkeypatch):
    runs = []
    monkeypatch.setattr(bs, "run_deepen", lambda v, say=None: runs.append(1) or {})
    t = [0.0]
    p = bs.Processor(bs.Vault(tmp_path), clock=lambda: t[0], say=lambda s: None)
    t[0] = bs.DEEPEN_IDLE_S - 1
    assert not p.tick()
    t[0] = bs.DEEPEN_IDLE_S
    assert p.tick() and runs == [1]
    t[0] += bs.DEEPEN_IDLE_S * 3
    assert not p.tick() and runs == [1]  # once per idle period
    p.batch(0, False)
    t[0] += bs.DEEPEN_IDLE_S
    assert p.tick() and runs == [1, 1]


def test_privacy_apply_follows_hidden_and_skip():
    s = Settings(hidden_from_claude=("adult",), skip_categories=())
    k = privacy.filter_record("https://www.webmd.com/x", "x", settings=s)
    assert (k.category, k.private) == ("health", False)
    k = privacy.filter_record("https://e.com/a", "Sexy cars of 2026", settings=s)  # a maybe
    assert (k.category, k.maybe, k.private) == ("adult", True, True)
    assert privacy.filter_record("https://e.com/a", "Flu symptoms",
                                 settings=replace(s, skip_categories=("health",))) is None
    p = privacy.filter_record("https://my.diary.net/a", "x",
                              settings=replace(s, private=("diary.net",)))
    assert p.private
    assert Settings().skip_categories == ("adult",)
    assert Settings(adult="private").skip_categories == ()
    assert Settings(sensitive="skip").skip_categories == privacy.lists.CATEGORIES


def _synthetic(n_visits: int, n_sites: int):
    t0 = m(0) - timedelta(days=90)
    out = []
    for i in range(n_visits):
        start = t0 + timedelta(minutes=2 * i)
        site = f"site{i % n_sites}.com"
        cat = "adult" if i % 50 == 0 else None
        k = privacy.Kept(f"https://{site}/p{i % 37}", f"Page {i % 37} on {site}", site, cat,
                         cat is not None, "q" if i % 23 == 0 else None,
                         "google" if i % 23 == 0 else None)
        out.append(Item(start.date().isoformat(), start, 60.0, k))
    return out


@pytest.mark.slow  # about 20 s here: 2,000 site pages written through the vault
def test_sixty_thousand_visits_build_in_reasonable_time(tmp_path):
    v = Vault(tmp_path)
    items = _synthetic(60_000, 2_000)
    t = time.perf_counter()
    days = sites.record(v, items, {}, OWNER, UTC, "2000-01-01", None)
    r = sites.write(v, OWNER, UTC, days=days)
    took = time.perf_counter() - t
    assert (r["sites_visible"], r["sites_hidden"]) == (1_960, 40)  # 40 sites: adult only
    t = time.perf_counter()
    r2 = sites.write(v, OWNER, UTC)
    assert r2["sites_written"] == 0
    assert took < 120, took  # measured 17 s on the owner's laptop; slack for slow disks
    assert time.perf_counter() - t < 60


# ---- board 2 review fixes (docs/design/v6/board2) -------------------------------------------

def _item(t, url, title, host):
    return Item(t.date().isoformat(), t, 60.0, privacy.Kept(url, title, host))


def test_r2_1_a_subdomain_hidden_or_denied_later_leaves_the_forgotten_days(tmp_path):
    v = Vault(tmp_path)
    t0 = m(0, -60)
    day = t0.date().isoformat()
    sites.record(v, [_item(t0, "https://portal.uni.edu/grades", "SECRETGRADES portal",
                           "portal.uni.edu"),
                     _item(t0 + timedelta(minutes=5), "https://www.uni.edu/news", "Uni news",
                           "www.uni.edu")], {}, OWNER, UTC, day, day)
    sites.write(v, OWNER, UTC)
    assert any(b"SECRETGRADES" in b for b in visible(tmp_path).values())
    # the browser has forgotten that day: later runs record only newer days
    later = replace(OWNER, private=("portal.uni.edu",))
    sites.record(v, [], {}, later, UTC, "2099-01-01", {"2099-01-01"})
    sites.write(v, later, UTC)
    assert not any(b"SECRETGRADES" in b for b in visible(tmp_path).values())
    assert any(b"SECRETGRADES" in b for b in hidden(tmp_path).values())
    sites.write(v, replace(OWNER, deny=("portal.uni.edu",)), UTC)
    assert not any(b"SECRETGRADES" in b
                   for b in {**visible(tmp_path), **hidden(tmp_path)}.values())


def test_r2_2_the_reconcile_removes_only_pages_cortex_made(vault, hist):
    imp(vault, hist)
    ours = vault / "browse/sites/gaming/browse-lichess.org.md"
    assert ours.is_file()
    own = vault / "browse/sites/learning/my-reading-list.md"
    own2 = vault / "browse/private/sites/other/browse-my-notes.md"
    for p, text in ((own, "# mine\n"), (own2, "---\nid: browse-my-notes\n---\n# my notes\n")):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    r = sites.write(Vault(vault), replace(OWNER, deny=("lichess.org",)), UTC)
    assert not ours.exists() and r["sites_removed"] >= 1
    assert own.read_text(encoding="utf-8") == "# mine\n" and own2.is_file()
    assert r["sites_not_ours"] == 2


def test_r2_7_a_torn_site_days_file_is_set_aside_and_removes_nothing(vault, hist):
    imp(vault, hist)
    before = set(visible(vault)) | set(hidden(vault))
    p = vault / sites.DAYS
    torn = p.read_text(encoding="utf-8")[:60]
    p.write_text(torn, encoding="utf-8")
    lines = []
    r = imp(vault, hist, say=lines.append)
    bad = list(p.parent.glob("site-days.json.bad-*"))
    assert len(bad) == 1 and bad[0].read_text(encoding="utf-8") == torn
    assert r["sites_removed"] == 0 and r["sites_days_bad"] == 1
    assert before <= set(visible(vault)) | set(hidden(vault))  # nothing removed
    assert any("set aside" in ln for ln in lines) and all("://" not in ln for ln in lines)


class Router:
    """run_deepen with a fake local and a fake cloud runner; `flip(cur)` changes the settings
    after the first cloud call."""

    def __init__(self, monkeypatch, settings, flip=None):
        from cortex import cloudllm

        self.cur, self.local, self.cloud = [settings], FakeLLM(), FakeLLM()
        self.topics = []
        real = self.cloud.site

        def cloud_site(name, titles, labels, candidates, topics, *, budget=None):
            self.topics.append((name, list(topics)))
            out = real(name, titles, labels, candidates, topics, budget=budget)
            if flip and len(self.cloud.asked) == 1:
                self.cur[0] = flip(self.cur[0])
            return out
        self.cloud.site = cloud_site
        monkeypatch.setattr(bs, "load_settings", lambda config=None: self.cur[0])
        monkeypatch.setattr(bs, "local_runner",
                            lambda budget_s=None: (self.local, localllm.Budget(100)))
        monkeypatch.setattr(cloudllm, "find", lambda *a, **k: self.cloud)
        monkeypatch.setattr(cloudllm, "audit", lambda *a, **k: None)
        monkeypatch.setattr(sites, "update_vectors", lambda v, say=None: None)

    def run(self, vault):
        return bs.run_deepen(Vault(vault), say=lambda s: None)


def test_s3_moving_a_category_to_local_mid_run_stops_the_cloud_for_it(vault, hist,
                                                                       monkeypatch):
    imp(vault, hist)  # health (webmd.com) is visible
    rt = Router(monkeypatch, replace(OWNER, cloud_processing=True),
                flip=lambda s: replace(s, hidden_from_claude=("adult", "health")))
    rt.run(vault)
    cloud = [a[0] for a in rt.cloud.asked]
    assert cloud and cloud[0] != "webmd.com"
    assert "webmd.com" not in cloud and "webmd.com" in [a[0] for a in rt.local.asked]
    assert not any("webmd.com" in c for a in rt.cloud.asked[1:] for c in a[2])


def test_s6_the_cloud_hears_only_the_hubs_linked_to_that_site(vault, hist, monkeypatch):
    imp(vault, hist)
    rt = Router(monkeypatch, replace(OWNER, cloud_processing=True))
    rt.run(vault)
    got = dict(rt.topics)
    assert got["lichess.org"] == ["chess hub"]
    assert got["ana.github.io"] == []
    assert not any("cooking hub" in t for t in got.values())


def test_write_holds_the_vault_lock_in_short_chunks(tmp_path, monkeypatch):
    """R2-3: 20k pages under one lock held it for 114 s; other writers then timed out."""
    from collections import Counter
    v = Vault(tmp_path)
    days = sites.record(v, _synthetic(600, 60), {}, OWNER, UTC, "2000-01-01", None)
    holds, per_hold = [0], Counter()
    real_acquire, real_write = v._acquire_file, v.write_text

    def acquire(timeout):
        holds[0] += 1
        return real_acquire(timeout)

    def write_text(path, text, *a, **k):
        per_hold[holds[0]] += 1
        return real_write(path, text, *a, **k)

    monkeypatch.setattr(sites, "WRITE_CHUNK", 10)
    monkeypatch.setattr(v, "_acquire_file", acquire)
    monkeypatch.setattr(v, "write_text", write_text)
    r = sites.write(v, OWNER, UTC, days=days)
    assert r["sites_written"] > 30
    assert max(per_hold.values()) <= 10


def test_deepen_keeps_only_related_sites_that_share_a_category_or_hub(vault, hist):
    imp(vault, hist)
    padded = FakeLLM(lambda name, c: {"category": "other", "description": "x",
                                      "related": list(c), "topics": []})  # a 2B model's padding
    sites.deepen(Vault(vault), padded, localllm.Budget(100), OWNER, tz=UTC)
    llm = sites.load_cache(Vault(vault))["llm"]
    by = {}
    for q in sites.pages(Vault(vault), OWNER, sites.load_days(Vault(vault))[0], llm).values():
        by.setdefault(q.site, []).append(q)
    kept = 0
    for site, ps in by.items():
        for p in ps:
            rel = (llm.get(site) or {}).get("h" if p.hidden else "v", {}).get("related", [])
            assert len(rel) <= 3
            for s in rel:
                assert any(q.category == p.category or set(q.hubs) & set(p.hubs)
                           for q in by[s]), (site, s)
            kept += len(rel)
    assert kept  # the chess sites still find each other
