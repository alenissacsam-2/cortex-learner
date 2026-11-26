"""The vault's git is the owner's choice: `git: {enabled, private_browsing}` in
~/.keepfold/config.yaml. Off: no repo made, no commit, undo explains, doctor passes, an existing
.git is left alone. private_browsing on: Keepfold's own browse/private/ ignore line goes, private
pages are committed, doctor passes; back off: the line returns and the pages are untracked.
Temp vaults and an isolated HOME only."""

from datetime import UTC
from pathlib import Path

import pytest
from conftest import git
from test_app import app as app  # noqa: F401 (fixture)
from test_app import call
from test_app import vault as vault  # noqa: F401 (fixture)
from test_browse import Embed, chromium, hubbed, m
from test_browse_sites import OWNER
from test_forget_more import ROWS, priv
from typer.testing import CliRunner

from keepfold import cli, doctor, session, taint
from keepfold.browse import build, forget, sources
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")
PRIV = Path("browse") / "private"
SID = "s_01JD0000000000000000000000"


def choose(**git):
    taint.write_config({"git": git})


def page(root, text="hello"):
    p = root / PRIV / "browse-private-2026-01-01.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nid: browse-private-2026-01-01\nprivate: true\n---\n{text}\n",
                 encoding="utf-8")
    return p


def note(root, text="x"):
    p = root / "notes" / "n.md"
    p.parent.mkdir(exist_ok=True)
    p.write_text(text + "\n", encoding="utf-8")
    return p


def head(root):
    return git(root, "rev-parse", "HEAD").strip()


def doctor_git(root):
    return [doctor.check_git(root), doctor.check_git_identity(root),
            doctor.check_private_browse_git(root)]


def test_settings_default_to_todays_behaviour():
    assert session.git_choice({}) == (True, False)
    assert session.git_choice({"git": "off"}) == (True, False)
    assert session.git_choice({"git": {"enabled": "no", "private_browsing": 1}}) == (True, False)
    # private browsing only counts with git on: off, the ignore line stays the safe default
    assert session.git_choice({"git": {"enabled": False, "private_browsing": True}}) == (
        False, False)
    assert session.git_choice({"git": {"enabled": True, "private_browsing": True}}) == (
        True, True)


# ---- git off ------------------------------------------------------------------------------

def test_git_off_init_makes_no_repo_and_doctor_passes(tmp_path):
    choose(enabled=False)
    v = tmp_path / "v"
    r = CliRunner().invoke(cli.app, ["init", str(v)])
    assert r.exit_code == 0, r.output
    assert "git (off in settings)" in r.output
    assert not (v / ".git").exists()
    checks = doctor_git(v)
    assert all(c[0] == "PASS" for c in checks), checks
    assert "off in your settings" in checks[0][2]


def test_git_off_a_session_commits_nothing_and_leaves_git_alone(root):
    before = head(root)
    choose(enabled=False)
    assert session._commit(Vault(root), "keepfold: s", [note(root)]) is None
    assert head(root) == before and (root / ".git").is_dir()
    assert all(c[0] == "PASS" for c in doctor_git(root))
    choose(enabled=True)  # turned back on: commits carry on from the same history
    assert session._commit(Vault(root), "keepfold: s", [root / "notes" / "n.md"])
    assert git(root, "rev-parse", "HEAD~1").strip() == before


def test_git_off_undo_explains_how_to_turn_it_on(root):
    choose(enabled=False)
    r = CliRunner().invoke(cli.app, ["undo", SID, "--vault", str(root)])
    assert r.exit_code == 1
    assert "off in your settings" in r.output and "git: {enabled: true}" in r.output


def test_git_off_untrack_leaves_the_repo_alone(root):
    page(root)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "an old vault")
    before = head(root)
    choose(enabled=False)
    assert not forget.untrack_private(Vault(root))
    assert head(root) == before and git(root, "ls-files", PRIV.as_posix())


def test_git_off_doctor_still_warns_about_the_old_repos_remote_and_history(root):
    """Off with a .git left: identity is PASS by choice, but a remote and private pages in
    history still WARN with the same fixes (read-only checks)."""
    page(root)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "an old vault")
    git(root, "remote", "add", "origin", "https://example.invalid/v.git")
    choose(enabled=False)
    remote, identity, private = doctor_git(root)
    assert remote[0] == "WARN" and "origin" in remote[2] and "off in your settings" in remote[2]
    assert "git remote remove" in remote[3]
    assert identity[0] == "PASS" and "off in your settings" in identity[2]
    assert private[0] == "WARN" and "1 commit in the vault's git history" in private[2]
    assert "git filter-repo" in private[3]
    git(root, "remote", "remove", "origin")
    assert doctor.check_git(root)[0] == "PASS"


# ---- private browsing kept in git ---------------------------------------------------------

def test_private_on_drops_only_keepfolds_line_and_commits_the_pages(root, monkeypatch):
    (root / ".gitignore").write_text(f".keepfold/*\n{forget.PRIVATE_IGNORE}\nmine/\n",
                                     encoding="utf-8")
    git(root, "add", ".gitignore")
    git(root, "commit", "-q", "-m", "ignore")
    choose(enabled=True, private_browsing=True)

    def boom(v):
        raise AssertionError("untrack_private must not run")
    monkeypatch.setattr(forget, "untrack_private", boom)
    p = page(root)
    build.run(Vault(root), [], OWNER, tz=UTC, now=m(0, 2), embed=Embed())  # an import
    assert (root / ".gitignore").read_text(encoding="utf-8") == ".keepfold/*\nmine/\n"
    assert session._commit(Vault(root), "keepfold: session", [p])
    assert p.relative_to(root).as_posix() in git(root, "ls-files", PRIV.as_posix())
    status, _, detail, _ = doctor.check_private_browse_git(root)
    assert status == "PASS" and "by your choice" in detail


def test_private_on_init_does_not_add_the_line(root):
    choose(enabled=True, private_browsing=True)
    r = CliRunner().invoke(cli.app, ["init", str(root)])
    assert r.exit_code == 0, r.output
    lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert forget.PRIVATE_IGNORE not in lines and cli.WORKSPACE_IGNORE in lines


def test_private_back_off_readds_the_line_and_untracks(root):
    choose(enabled=True, private_browsing=True)
    p = page(root)
    assert session._commit(Vault(root), "keepfold: everything")
    assert git(root, "ls-files", PRIV.as_posix())
    choose(enabled=True, private_browsing=False)
    r = CliRunner().invoke(cli.app, ["init", str(root)])
    assert r.exit_code == 0, r.output
    assert forget.PRIVATE_IGNORE in (root / ".gitignore").read_text(encoding="utf-8").split()
    assert forget.sync_private(Vault(root))  # what the next import runs
    assert git(root, "ls-files", PRIV.as_posix()) == "" and p.is_file()
    assert doctor.check_private_browse_git(root)[0] == "WARN"  # old commits still hold it


# ---- the app ------------------------------------------------------------------------------

def test_app_saves_the_choice_and_applies_it(app, vault):  # noqa: F811
    (vault / ".gitignore").write_text(f".keepfold/*\n{forget.PRIVATE_IGNORE}\n", encoding="utf-8")
    assert call(app, "GET", "/api/vault")[1]["git"] == {"enabled": True,
                                                         "private_browsing": False}
    body = {"enabled": True, "private_browsing": True}
    assert call(app, "POST", "/api/vault/git", body, token=False)[0] == 401
    for bad in ({"enabled": "yes", "private_browsing": False}, {"enabled": True}):
        assert call(app, "POST", "/api/vault/git", bad)[0] == 400
    assert session.git_choice() == (True, False)
    assert call(app, "POST", "/api/vault/git", body)[0] == 200
    assert session.git_choice() == (True, True)
    assert forget.PRIVATE_IGNORE not in (vault / ".gitignore").read_text(encoding="utf-8")
    assert call(app, "POST", "/api/vault/git", {"enabled": False,
                                                 "private_browsing": False})[0] == 200
    assert call(app, "GET", "/api/vault")[1]["git"]["enabled"] is False
    assert taint.config()["git"] == {"enabled": False, "private_browsing": False}
    # git off with the private box still ticked: shown as saved, but the ignore line is back
    assert call(app, "POST", "/api/vault/git", {"enabled": False,
                                                 "private_browsing": True})[0] == 200
    assert call(app, "GET", "/api/vault")[1]["git"] == {"enabled": False,
                                                         "private_browsing": True}
    assert session.git_choice() == (False, False)
    assert forget.PRIVATE_IGNORE in (vault / ".gitignore").read_text(encoding="utf-8").split()


def test_private_on_forget_commits_the_pages_without_what_it_forgot(root, tmp_path):
    v = hubbed(root)
    choose(enabled=True, private_browsing=True)
    local = tmp_path / "local"
    chromium(local / "Google" / "Chrome" / "User Data" / "Default" / "History", ROWS)
    build.run(Vault(v), sources.discover(local, tmp_path / "roaming"), OWNER, tz=UTC,
              now=m(0, 2), embed=Embed())
    assert session._commit(Vault(v), "keepfold: import")
    assert "lichess" in priv(v)
    r = CliRunner().invoke(cli.app, ["browse", "forget", "--site", "lichess.org",
                                     "--vault", str(v)])
    assert r.exit_code == 0, r.output
    assert "lichess" not in priv(v)
    assert git(v, "status", "--porcelain", "--", PRIV.as_posix()) == ""  # committed
    tracked = git(v, "ls-files", PRIV.as_posix()).split()
    assert tracked and not any("lichess" in git(v, "show", f"HEAD:{p}").lower()
                               for p in tracked)
    assert "lichess" in git(v, "log", "-p", "--", PRIV.as_posix()).lower()  # old commits keep it
