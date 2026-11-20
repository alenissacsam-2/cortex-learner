"""`cortex browse forget` and the settings refilter, second review: a category also covers
the site table, local-model verdicts and URL-path words; a national domain takes its search
lines; the private index is redone with no day held; a browse-wide lock keeps a forget from
being undone by an import or a deepen pass; browse/private never wedges the vault's git.
Synthetic History and temp vaults only."""

import json
import os
import socket
from dataclasses import replace
from datetime import UTC
from pathlib import Path

import pytest
from conftest import git
from test_browse import LINK, Embed, chromium, hubbed, m
from test_browse_sites import OWNER
from typer.testing import CliRunner

from cortex import cli, localllm, session
from cortex.browse import build, forget, sites, sources
from cortex.vault import Vault, VaultError

ROWS = [
    (m(0), "https://lichess.org/study/abc", "Chess openings for beginners", 300),
    (m(3), "https://www.amazon.in/dp/B0X", "Wireless EARBUDZ blue", 200),
    (m(4), "https://www.amazon.in/s?k=earbudz+cheap", "Amazon.in : earbudz cheap", 0,
     LINK, "earbudz cheap"),
    (m(30), "https://blog.example.org/post", "Living quietly with my condition", 300),
    (m(40), "https://www.reddit.com/r/endometriosis/comments/abc/x", "Anyone else feel this way",
     200),
]
PRIV = Path("browse") / "private"


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


def fake_local(labels, summary=None):
    r = localllm.Runner("ollama", "http://127.0.0.1:9", "fake")

    def classify(text, labels_, *, timeout=20, budget=None):
        got = labels.get(text, "ordinary")
        return None if got is None else (got, 0.9)
    r.classify = classify

    def classify_many(texts, labels_, *, timeout=60, budget=None):  # a batch: line by line
        return {i: got[0] for i, t in enumerate(texts) if (got := classify(t, labels_))}
    r.classify_many = classify_many
    r.summarise = lambda *a, **k: summary
    return r


def priv(r) -> str:
    return "".join(p.read_text(encoding="utf-8")
                   for p in (r / PRIV).glob("browse-private-2*.md")).lower()


# ---- 1: a category is the site table's, the model's and the URL's too ---------------------

def test_forget_a_site_category_takes_its_day_page_lines(vault, hist):
    imp(vault, hist)
    assert "- wireless earbudz blue · amazon.in" in priv(vault)
    assert "### other: blue earbudz wireless" in priv(vault)  # a heading made of its titles
    forget.purge(Vault(vault), forget.Rule(cats=frozenset({"shopping"})), settings=OWNER)
    p = priv(vault)
    assert "earbudz" not in p and "amazon" not in p
    assert "chess openings for beginners" in p


def test_forget_a_category_follows_model_verdicts_and_url_paths(vault, hist):
    imp(vault, hist, runner=fake_local({"Living quietly with my condition": "health"}))
    p = priv(vault)
    assert "living quietly" in p and "anyone else feel this way" in p
    forget.purge(Vault(vault), forget.Rule(cats=frozenset({"health"})), settings=OWNER)
    p = priv(vault)
    assert "living quietly" not in p and "anyone else feel this way" not in p
    assert "chess openings for beginners" in p


def test_refilter_takes_an_archived_record_the_model_labelled(vault, hist):
    arch = vault / build.ARCHIVE
    arch.mkdir(parents=True)
    a = arch / "inbox-2026-11-09.jsonl"
    a.write_text(json.dumps({"url": "https://quiet.example.net/a",
                             "title": "Living quietly with my condition",
                             "start": m(90).isoformat(), "active_ms": 1000}) + "\n",
                 encoding="utf-8")
    labels = {"Living quietly with my condition": "health"}
    imp(vault, hist, runner=fake_local(labels))
    imp(vault, hist, replace(OWNER, skip_categories=("adult", "health")),
        runner=fake_local(labels))
    assert not (a.is_file() and "living quietly" in a.read_text(encoding="utf-8").lower())


# ---- 3: a national domain's searches ------------------------------------------------------

def test_forget_a_national_domain_takes_its_search_lines(vault, hist):
    imp(vault, hist)
    assert "earbudz cheap · amazon" in priv(vault)
    forget.purge(Vault(vault), forget.Rule(hosts=("amazon.in",)), settings=OWNER)
    assert "earbudz" not in priv(vault)


@pytest.mark.parametrize("host,goes", [("google.co.in", True), ("google.com", True),
                                       ("www.google.co.in", True), ("lichess.org", False),
                                       ("notgoogle.com", False)])
def test_a_search_line_matches_any_domain_of_its_engine(host, goes):
    line = "- 09:04 · plain words · google"
    assert forget._line_goes(forget.Rule(hosts=(host,)), "## Searches", line) is goes


# ---- 2: one browse-wide lock --------------------------------------------------------------

@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(sites, "WAIT_S", 0.2)
    monkeypatch.setattr(forget, "FORGET_WAIT_S", 0.2)


def test_forget_refuses_during_a_deepen_pass_and_runs_after(vault, hist, quick):
    imp(vault, hist)
    r = localllm.Runner("ollama", "http://127.0.0.1:9", "fake")
    refused = []

    def site(s, titles, labels, cands, topics, budget=None):
        if not refused:
            with pytest.raises(VaultError) as e:
                forget.purge(Vault(vault), forget.Rule(hosts=("lichess.org",)), settings=OWNER)
            refused.append(str(e.value))
        return {"category": "gaming", "description": f"About {s}", "related": [], "topics": []}
    r.site = site
    sites.deepen(Vault(vault), r, None, OWNER)
    assert refused == [sites.BUSY]
    assert "lichess" in priv(vault)  # nothing half-forgotten
    forget.purge(Vault(vault), forget.Rule(hosts=("lichess.org",)), settings=OWNER)
    assert not list((vault / "browse").rglob("*lichess*"))
    assert "lichess" not in (vault / sites.CACHE).read_text(encoding="utf-8")


def test_an_import_waits_for_the_lock_and_the_cli_forget_says_so(vault, hist, home, quick):
    imp(vault, hist)
    with sites.busy(Vault(vault)):
        with pytest.raises(VaultError):
            imp(vault, hist)
        out = CliRunner().invoke(cli.app, ["browse", "forget", "--site", "lichess.org",
                                           "--vault", str(vault)])
        assert out.exit_code == 1 and sites.BUSY in out.output
    assert not (vault / sites.LOCK).exists()
    assert "lichess" in priv(vault)


def test_a_crashed_holder_never_leaves_the_lock_stuck(vault, hist, quick):
    imp(vault, hist)
    lock = vault / sites.LOCK
    lock.write_text(f"4194300 1.0 {socket.gethostname()}", encoding="utf-8")  # a dead pid
    forget.purge(Vault(vault), forget.Rule(hosts=("lichess.org",)), settings=OWNER)
    assert "lichess" not in priv(vault) and not lock.exists()
    lock.write_text(f"{os.getpid()} 9999999999.0 {socket.gethostname()}", encoding="utf-8")
    with pytest.raises(VaultError):  # a live holder is never stolen from
        forget.purge(Vault(vault), forget.Rule(hosts=("chess.com",)), settings=OWNER)


# ---- 4: browse/private never wedges the vault's git ---------------------------------------

def _page(vault, text="hello"):
    page = vault / PRIV / "browse-private-2026-01-01.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"---\nid: browse-private-2026-01-01\nprivate: true\n---\n{text}\n",
                    encoding="utf-8")
    return page


def test_untrack_a_staged_private_page_and_commits_go_on(root):
    v = Vault(root)
    page = _page(root)
    git(root, "add", PRIV.as_posix())  # staged, never committed (a commit failed after `add`)
    forget.untrack_private(v)
    assert git(root, "ls-files", PRIV.as_posix()) == "" and page.is_file()
    note = root / "notes" / "n.md"
    note.parent.mkdir(exist_ok=True)
    note.write_text("x\n", encoding="utf-8")
    assert session._commit(v, "cortex: s", [note], before="other changes")
    assert git(root, "ls-tree", "-r", "--name-only", "HEAD", PRIV.as_posix()) == ""
    assert "notes/n.md" in git(root, "ls-files", "notes")


def test_a_commit_leaves_out_a_private_page_still_in_the_index(root):
    v = Vault(root)
    (root / ".gitignore").write_text(".cortex/*\n" + forget.PRIVATE_IGNORE + "\n",
                                     encoding="utf-8")
    git(root, "add", ".gitignore")
    git(root, "commit", "-q", "-m", "ignore")
    _page(root)
    git(root, "add", "-f", PRIV.as_posix())
    note = root / "notes" / "n.md"
    note.parent.mkdir(exist_ok=True)
    note.write_text("x\n", encoding="utf-8")
    assert session._commit(v, "cortex: everything")
    assert git(root, "ls-tree", "-r", "--name-only", "HEAD", PRIV.as_posix()) == ""
    assert "notes/n.md" in git(root, "ls-tree", "-r", "--name-only", "HEAD")


def test_undo_never_reverts_or_deletes_a_current_private_page(root, home):
    sid = "s_01JD0000000000000000000000"
    page = _page(root, "old")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "an old vault with a private page")
    page.write_text(page.read_text(encoding="utf-8").replace("old", "session"),
                    encoding="utf-8")
    note = root / "concepts" / "arrays-basics.md"
    note.write_text(note.read_text(encoding="utf-8") + "- session edit\n", encoding="utf-8")
    git(root, "commit", "-qam", f"cortex: session {sid} learn")
    assert forget.untrack_private(Vault(root))
    git(root, "add", ".gitignore")
    git(root, "commit", "-q", "-m", "ignore")
    page.write_text(page.read_text(encoding="utf-8").replace("session", "current"),
                    encoding="utf-8")
    out = CliRunner().invoke(cli.app, ["undo", sid, "--vault", str(root)])
    assert out.exit_code == 0 and "reverted" in out.output, out.output
    assert page.is_file() and "current" in page.read_text(encoding="utf-8")
    assert "- session edit" not in note.read_text(encoding="utf-8")
    assert git(root, "ls-tree", "-r", "--name-only", "HEAD", PRIV.as_posix()) == ""


# ---- 5 and 6 ----------------------------------------------------------------------------

def test_refilter_with_no_held_day_redoes_the_private_index(vault, hist, tmp_path):
    imp(vault, hist, replace(OWNER, visible_to_claude=False))
    pidx = vault / PRIV / "browse-private-index.md"
    assert "lichess" in pidx.read_text(encoding="utf-8")
    empty = tmp_path / "local2"
    chromium(empty / "Google" / "Chrome" / "User Data" / "Default" / "History", [])
    build.run(Vault(vault), sources.discover(empty, tmp_path / "r2"),
              replace(OWNER, visible_to_claude=False, deny=("lichess.org",)), tz=UTC,
              now=m(0, 2), embed=Embed())
    assert "lichess" not in pidx.read_text(encoding="utf-8")


def test_forget_drops_torn_site_day_copies(vault, hist):
    imp(vault, hist)
    torn = vault / ".cortex" / "browse" / "site-days.json.bad-20260101T000000Z"
    torn.write_text('{"2026-11-09": {"lichess.org||0": ', encoding="utf-8")
    forget.purge(Vault(vault), forget.Rule(hosts=("lichess.org",)), settings=OWNER)
    assert not torn.exists()


def test_forget_drops_a_model_summary_fed_by_a_title_not_shown(vault, hist):
    imp(vault, hist, runner=fake_local({}, summary="MODELSUMMARY of the day"))
    p = vault / PRIV / "browse-private-2026-11-09.md"
    text = p.read_text(encoding="utf-8")
    assert "MODELSUMMARY" in text and "browse_llm" in text
    # as if the lichess lines were past the shown cut ("- and N more")
    p.write_text("\n".join(ln for ln in text.splitlines() if "lichess" not in ln.lower()
                           and "chess openings" not in ln.lower()) + "\n", encoding="utf-8")
    forget.purge(Vault(vault), forget.Rule(hosts=("lichess.org",)), settings=OWNER)
    text = p.read_text(encoding="utf-8")
    assert "MODELSUMMARY" not in text and "browse_llm" not in text
