import os
import shutil
import time
from pathlib import Path

import pytest

from cortex import vault as vault_mod
from cortex.vault import Vault, VaultError, is_private, scan_secrets

FIXTURE = Path(__file__).parent / "fixtures" / "vault_mini"


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    return Vault(root)


def code(excinfo):
    return excinfo.value.code


@pytest.mark.parametrize("bad", ["", "Upper", "-lead", "a/b", "a\\b", "..", "../me/profile",
                                 "x" * 82, "a b", "a_b"])
def test_invalid_ids_rejected(v, bad):
    with pytest.raises(VaultError) as e:
        v.path_for("concept", bad)
    assert code(e) == "invalid_id"


def test_traversal_rejected(v):
    with pytest.raises(VaultError) as e:
        v.read_path(Path("..") / "outside.md")
    assert code(e) == "invalid_id"
    with pytest.raises(VaultError) as e:
        v.write_text(v.root / "concepts" / ".." / ".." / "x.md", "hi")
    assert code(e) == "invalid_id"


def test_unknown_kind_and_id(v):
    with pytest.raises(VaultError) as e:
        v.path_for("planet", "x")
    assert code(e) == "invalid_argument"
    with pytest.raises(VaultError) as e:
        v.read("concept", "nope")
    assert code(e) == "unknown_id"


def test_read_and_private(v):
    note = v.read("concept", "two-pointers")
    assert note.fields["title"] == "Two pointers"
    assert note.body.startswith("# Two pointers\n")
    assert not is_private(note)
    assert is_private(v.read("concept", "secret-diary"))
    brief = v.read_path(Path("BRIEF.md"))
    assert brief.body.startswith("## Brief") and not brief.fields


def test_iter_notes_skips_unparsable(v, caplog):
    (v.root / "concepts" / "broken.md").write_text("---\ntitle: [unclosed\n---\nx\n")
    (v.root / "concepts" / "BadName.md").write_text("---\nid: x\n---\n")
    ids = [n.id for n in v.iter_notes("concept")]
    assert ids == ["arrays-basics", "secret-diary", "two-pointers"]
    assert "broken.md" in caplog.text and "BadName.md" in caplog.text
    assert {n.kind for n in v.iter_notes()} >= {"concept", "problem", "track", "me"}


def test_set_field_keeps_other_bytes(v):
    path = v.path_for("assessment", "acme-oa-2026")
    before = path.read_bytes()
    v.set_field("assessment", "acme-oa-2026", "outcome", "pass")
    after = path.read_bytes()
    assert after == before.replace(b'outcome: "pending"', b'outcome: "pass"')


@pytest.mark.parametrize("bad", ["next friday", "20261130", "2026-11-31", "2026-11-30x"])
def test_set_field_refuses_a_bad_assessment_date(v, bad):
    """B10: an unreadable date silently dropped the test off the exam clock."""
    before = v.path_for("assessment", "acme-oa-2026").read_bytes()
    with pytest.raises(VaultError, match="date"):
        v.set_field("assessment", "acme-oa-2026", "date", bad)
    assert v.path_for("assessment", "acme-oa-2026").read_bytes() == before
    v.set_field("assessment", "acme-oa-2026", "date", "2026-11-30")


def racer(monkeypatch, name: str, line: bytes) -> None:
    """The next write to `name` finds `line` appended by another editor (Obsidian) first."""
    real, fired = Vault._atomic_write, []

    def edit_first(self, path, text, expect=None):
        if Path(path).name == name and not fired:
            fired.append(1)
            with open(path, "ab") as f:
                f.write(line)
        return real(self, path, text, expect)

    monkeypatch.setattr(Vault, "_atomic_write", edit_first)


def test_update_text_keeps_an_edit_made_meanwhile(v, monkeypatch):
    """B13: read-modify-write of a page Obsidian may edit rereads instead of overwriting."""
    page = v.root / "inbox.md"
    page.write_bytes(b"# Inbox\n")
    racer(monkeypatch, "inbox.md", b"- typed in Obsidian\n")
    assert v.update_text(page, lambda t: t + "- mine\n") is True
    assert page.read_bytes() == b"# Inbox\n- typed in Obsidian\n- mine\n"
    new = v.root / "me" / "fresh.md"
    assert v.update_text(new, lambda t: t + "## A\n", default="# Fresh\n") is True
    assert new.read_bytes() == b"# Fresh\n## A\n"
    assert v.update_text(new, lambda t: t) is False
    with pytest.raises(VaultError, match="key or token"):
        v.update_text(page, lambda t: t + "- sk-ant-api03-" + "x" * 90 + "\n")
    with pytest.raises(VaultError):
        v.update_text(v.root / ".." / "out.md", lambda t: "x")


def test_set_field_crlf_body_identical(v):
    path = v.path_for("problem", "lc-1")
    crlf = path.read_bytes().replace(b"\n", b"\r\n")
    path.write_bytes(crlf)
    v.set_field("problem", "lc-1", "status", "solved")
    assert path.read_bytes() == crlf.replace(b'status: "attempted"', b'status: "solved"')


def test_set_field_new_field_and_int(v):
    v.set_field("track", "sprint-test", "current_day", 3)
    v.set_field("problem", "lc-167", "solved_on", "2026-11-13")
    assert b"current_day: 3\n" in v.path_for("track", "sprint-test").read_bytes()
    text = v.path_for("problem", "lc-167").read_text()
    assert 'status: "todo"\nsolved_on: "2026-11-13"\n---\n' in text


@pytest.mark.parametrize("kind,id,field,value", [
    ("concept", "two-pointers", "title", "x"),
    ("problem", "lc-1", "url", "https://x"),
    ("me", "profile", "title", "x"),
    ("problem", "lc-1", "status", "done"),  # not in the enum
    ("problem", "lc-1", "status", ["solved"]),
    ("concept", "two-pointers", "cortex_status", "solid"),  # computed by Cortex
    ("concept", "two-pointers", "cortex_next", "2026-11-15"),
    ("concept", "two-pointers", "merged_into", "[[arrays-basics]]"),
])
def test_set_field_rejects(v, kind, id, field, value):
    with pytest.raises(VaultError) as e:
        v.set_field(kind, id, field, value)
    assert code(e) == "invalid_argument"


def test_set_field_refuses_tool_props_with_reason(v):
    with pytest.raises(VaultError, match="computed by Cortex"):
        v.set_field("concept", "two-pointers", "cortex_status", "solid")


def test_set_tool_props(v):
    path = v.path_for("concept", "two-pointers")
    v.set_tool_props("concept", "two-pointers", "learning", "2026-11-15")
    assert 'cortex_status: "learning"\ncortex_next: "2026-11-15"\n---' in path.read_text()
    v.set_tool_props("concept", "two-pointers", "solid", None)
    text = path.read_text()
    assert 'cortex_status: "solid"' in text and "cortex_next" not in text


def test_append_to_section(v):
    path = v.path_for("mistake", "dsa.off-by-one")
    v.append_to_section("mistake", "dsa.off-by-one", "Pattern", "Also at the start.")
    assert "one short when the end index is inclusive.\nAlso at the start.\n\n## Fix" in (
        path.read_text())
    v.append_to_section("concept", "arrays-basics", "## Notes", "- first")
    assert v.read("concept", "arrays-basics").body.endswith("## Notes\n- first\n")


def test_append_to_me_note(v):
    v.append_to_section("me", "profile", "Brief", "- Prefers C++.")
    assert v.read("me", "profile").body.endswith("- Test learner, synthetic.\n- Prefers C++.\n")


def test_append_missing_heading(v):
    before = v.path_for("problem", "lc-1").read_bytes()
    with pytest.raises(VaultError) as e:
        v.append_to_section("problem", "lc-1", "Solutions", "x")
    assert code(e) == "invalid_argument"
    assert v.path_for("problem", "lc-1").read_bytes() == before


def test_check_item_ticks_exactly_one(v):
    path = v.path_for("track", "sprint-test")
    before = path.read_text().splitlines()
    assert v.check_item("track", "sprint-test", "Day 2", "[[lc-167]]") is True
    after = path.read_text().splitlines()
    diff = [(a, b) for a, b in zip(before, after, strict=True) if a != b]
    assert diff == [("- [ ] [[lc-167]] Two Sum II", "- [x] [[lc-167]] Two Sum II")]
    assert "- [ ] [[lc-167]] Two Sum II (owed copy)" in after  # other section untouched
    assert v.check_item("track", "sprint-test", "Day 2", "[[lc-167]]") is False  # idempotent


def test_check_item_ambiguous_or_missing(v):
    for text in ("[[two-pointers]]", "no such item"):
        with pytest.raises(VaultError) as e:
            v.check_item("track", "sprint-test", "Day 2", text)
        assert code(e) == "invalid_argument"


def test_create_note(v):
    path = v.create_note("problem", "lc-15", {"title": "3Sum", "concepts": ["[[two-pointers]]"],
                                               "status": "todo"}, "# 3Sum\n\n## Notes\n")
    assert path.read_bytes() == (b'---\nid: "lc-15"\ntitle: "3Sum"\n'
                                 b'concepts: ["[[two-pointers]]"]\nstatus: "todo"\n---\n'
                                 b"# 3Sum\n\n## Notes\n")
    assert v.read("problem", "lc-15").fields["concepts"] == ["[[two-pointers]]"]


def test_create_note_refuses_overwrite(v):
    before = v.path_for("problem", "lc-1").read_bytes()
    with pytest.raises(VaultError) as e:
        v.create_note("problem", "lc-1", {"title": "x"}, "# x\n")
    assert code(e) == "invalid_argument"
    assert v.path_for("problem", "lc-1").read_bytes() == before


@pytest.mark.parametrize("secret", [
    "sk-ant-api03-" + "a" * 30,
    "ghp_" + "A" * 36,
    "github_pat_" + "B" * 30,
    "AKIAIOSFODNN7EXAMPLE",
    "-----BEGIN RSA PRIVATE KEY-----",
    "api_key = abcdef123456789",
])
def test_secret_rejected(v, secret):
    before = v.path_for("problem", "lc-1").read_bytes()
    with pytest.raises(VaultError) as e:
        v.append_to_section("problem", "lc-1", "Notes", f"note {secret}")
    assert code(e) == "secret_detected"
    with pytest.raises(VaultError) as e:
        v.create_note("problem", "lc-99", {"title": secret}, "")
    assert code(e) == "secret_detected"
    assert v.path_for("problem", "lc-1").read_bytes() == before
    assert not v.path_for("problem", "lc-99").exists()
    with pytest.raises(VaultError) as e:
        v.write_text(v.root / "Today.md", secret)
    assert code(e) == "secret_detected"
    v.write_text(v.root / ".cortex" / "dashboard.html", secret)  # git-ignored view: not scanned


@pytest.mark.parametrize("secret, near_miss", [  # SEC-3; built by concatenation, no real keys
    ("sk_live_" + "a1" * 12, "sk_live_ is the Stripe prefix"),
    ("rk_live_" + "Z9" * 12, "rk_live_short"),
    ("xoxb-" + "1234567890-abcdef", "xoxb-short"),
    ("AIza" + "B" * 35, "AIza is a name"),
    ("glpat-" + "c" * 20, "glpat-abc"),
    ("hf_" + "d" * 30, "hf_model and hf_hub_download"),
    ("npm_" + "e1" * 18, "npm_config_cache"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlLXg", "eyJ.eyJ.x"),
    ("https://alice:s3cr3t@db.example.invalid", "https://example.invalid:8080/a@b"),
    ("Password = hunter22", "the password is long"),
    ("const privateKeyHex = '" + "ab" * 32 + "'",
     "tx hash 0x" + "ab" * 32 + " and, much later in the line, where a private key goes"),
])
def test_secret_patterns(secret, near_miss):
    with pytest.raises(VaultError):
        scan_secrets(f"note {secret}")
    scan_secrets(f"note {near_miss}")


def test_secret_scan_allows_plain_text(v):
    v.append_to_section("problem", "lc-1", "Notes", "- used sk-learn and an api key concept")


def test_lock_exclusive(v):
    other = Vault(v.root)
    with v.lock():
        t0 = time.monotonic()
        with pytest.raises(VaultError) as e:
            with other.lock(timeout=0.3):
                pass
        assert code(e) == "vault_locked"
        assert time.monotonic() - t0 >= 0.3
        with v.lock():  # reentrant for the same Vault
            pass
        assert (v.root / ".cortex" / "lock").exists()
    assert not (v.root / ".cortex" / "lock").exists()
    with other.lock(timeout=0.3):
        pass


def test_stale_lock_stolen(v):
    lock = v.root / ".cortex" / "lock"
    lock.parent.mkdir(exist_ok=True)
    lock.write_text(f"999999 {time.time() - 1000:.3f}")  # old and the pid is gone
    with v.lock(timeout=0.3):
        assert lock.read_text().startswith(f"{os.getpid()} ")
    lock.write_text(f"999999 {time.time():.3f}")  # fresh but the pid is gone
    with v.lock(timeout=0.3):
        pass
    assert not lock.exists()
    lock.write_text("garbage")  # no readable pid: the file's age decides
    old = time.time() - vault_mod.LOCK_STALE_S - 10
    os.utime(lock, (old, old))
    with v.lock(timeout=0.3):
        pass


def test_live_holders_lock_is_never_stolen(v, monkeypatch):
    """R2-3: a lock older than LOCK_STALE_S whose holder is alive (a long sites.write) stays."""
    lock = v.root / ".cortex" / "lock"
    lock.parent.mkdir(exist_ok=True)
    lock.write_text(f"{os.getpid()} {time.time() - vault_mod.LOCK_STALE_S - 1:.3f}")
    monkeypatch.setattr(vault_mod, "_pid_started", lambda pid: 0.0)  # started long before
    with pytest.raises(VaultError) as e:
        with v.lock(timeout=0.3):
            pass
    assert code(e) == "vault_locked"
    assert lock.exists()


def test_lock_of_a_reused_pid_is_stolen(v, monkeypatch):
    """G4: after a reboot the lock's pid can belong to a live, newer process; a holder that
    started after the lock was written is not the holder, so the lock is stale."""
    lock = v.root / ".cortex" / "lock"
    lock.parent.mkdir(exist_ok=True)
    ts = time.time() - 3600
    monkeypatch.setattr(vault_mod, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(vault_mod, "_pid_started", lambda pid: ts + 600)  # a newer process
    lock.write_text(f"4242 {ts:.3f}")
    with v.lock(timeout=0.3):
        pass
    assert not lock.exists()
    monkeypatch.setattr(vault_mod, "_pid_started", lambda pid: ts - 60)  # the real holder
    lock.write_text(f"4242 {ts:.3f}")
    with pytest.raises(VaultError):
        with v.lock(timeout=0.3):
            pass
    monkeypatch.setattr(vault_mod, "_pid_started", lambda pid: None)  # unknown: age decides
    lock.write_text(f"4242 {time.time() - 600:.3f}")
    with pytest.raises(VaultError):  # ten minutes is within the bound
        with v.lock(timeout=0.3):
            pass
    lock.write_text(f"4242 {time.time() - 11 * vault_mod.LOCK_STALE_S:.3f}")
    with v.lock(timeout=0.3):
        pass
    assert not lock.exists()


def test_pid_started_reads_this_process():
    started = vault_mod._pid_started(os.getpid())
    if started is not None or os.name == "nt":
        assert time.time() - 86400 * 30 < started <= time.time()


def test_lock_records_pid_and_host(v):
    import socket
    with v.lock():
        pid, _ts, host = (v.root / ".cortex" / "lock").read_text().split(maxsplit=2)
    assert int(pid) == os.getpid() and host == socket.gethostname()


def test_replace_retries_permission_error(v, monkeypatch):
    real = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) <= 2:
            raise PermissionError("locked by another program")
        return real(src, dst)

    monkeypatch.setattr(vault_mod.os, "replace", flaky)
    monkeypatch.setattr(vault_mod.time, "sleep", lambda s: None)
    v.set_field("problem", "lc-1", "status", "solved")
    assert len(calls) == 3
    assert v.read("problem", "lc-1").fields["status"] == "solved"
    assert not list(v.path_for("problem", "lc-1").parent.glob("*.tmp"))


def test_concurrent_edit_rereads(v, monkeypatch):
    path = v.path_for("problem", "lc-1")
    real_write = Vault._atomic_write
    seen = []

    def edit_then_write(self, p, text, expect=None):
        if not seen:  # Obsidian saves the note while Cortex is mid-edit
            raw = path.read_bytes()
            path.write_bytes(raw.replace(b"- Hash map version first.",
                                         b"- Hash map version first.\n- Obsidian edit."))
        seen.append(text)
        return real_write(self, p, text, expect)

    monkeypatch.setattr(Vault, "_atomic_write", edit_then_write)
    v.append_to_section("problem", "lc-1", "Notes", "- Cortex edit.")
    assert len(seen) == 2
    assert path.read_text().endswith("- Obsidian edit.\n- Cortex edit.\n")


def test_id_with_trailing_newline_is_rejected(tmp_path):
    import shutil

    from cortex.vault import Vault, VaultError

    root = tmp_path / "v"
    shutil.copytree(Path(__file__).parent / "fixtures" / "vault_mini", root)
    with pytest.raises(VaultError):
        Vault(root).path_for("concept", "two-pointers\n")


def test_bom_crlf_private_note_keeps_frontmatter_and_bytes(v):
    # T2: a UTF-8 BOM must not hide `private: true`
    path = v.path_for("problem", "lc-1")
    raw = path.read_bytes().replace(b"---\n", b"---\nprivate: true\n", 1)
    raw = b"\xef\xbb\xbf" + raw.replace(b"\n", b"\r\n")
    path.write_bytes(raw)
    assert is_private(v.read("problem", "lc-1"))
    v.set_field("problem", "lc-1", "status", "solved")
    assert path.read_bytes() == raw.replace(b'status: "attempted"', b'status: "solved"')
    assert is_private(v.read("problem", "lc-1"))


def test_replace_always_failing_is_vault_locked(v, monkeypatch):
    def locked(src, dst):
        raise PermissionError("locked by another program")

    monkeypatch.setattr(vault_mod.os, "replace", locked)
    monkeypatch.setattr(vault_mod.time, "sleep", lambda s: None)
    with pytest.raises(VaultError) as e:
        v.set_field("problem", "lc-1", "status", "solved")
    assert code(e) == "vault_locked" and "lc-1.md" in str(e.value)
    assert not list(v.path_for("problem", "lc-1").parent.glob("*.tmp"))


SITE_FM = ('id: "browse-a.com"\ntitle: "a \\"b\\" \\u00e9 \\\\ c \u00e9"\nkind: "browse"\n'
           'visits: 12\nneg: -3\nseen: true\nhidden: false\n')


@pytest.mark.parametrize("fm", [SITE_FM, SITE_FM.replace("\n", "\r\n"), "x: 1\n", ""])
def test_flat_frontmatter_reads_like_ruamel_without_it(fm, monkeypatch):
    """R2-10: ruamel took ~5 ms a page, minutes for 20k site pages; flat key: value lines
    (sites._fm) are read directly, to the same values and types."""
    want = vault_mod._yaml().load(fm) if fm.strip() else {}
    monkeypatch.setattr(vault_mod, "_yaml", lambda: pytest.fail("ruamel used"))
    got = vault_mod._load_fm(fm)
    assert got == want and [type(x) for x in got.values()] == [type(x) for x in want.values()]


@pytest.mark.parametrize("fm", ["tags: [a, b]\n", "a: 1\na: 2\n", "a: 1 # c\n", "a: 007\n",
                                "a: plain\n", 'a: "\\x41"\n', "a: |\n  b\n"])
def test_other_frontmatter_still_goes_through_ruamel(fm):
    real = vault_mod._yaml
    try:
        want = real().load(fm)
    except Exception as e:
        with pytest.raises(type(e)):
            vault_mod._load_fm(fm)
        return
    assert vault_mod._load_fm(fm) == want


def test_iter_notes_leaves_out_browse_site_pages(tmp_path):
    """R2-10: whole-vault walks (dashboard, lint) skip the generated site pages; the index
    still reads them through note_paths."""
    v = Vault(tmp_path)
    for rel in ("browse/2026-11-09.md", "browse/sites/browse-sites.md",
                "browse/sites/news/browse-a.com.md"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'---\nid: "{p.stem}"\nkind: "browse"\n---\nx\n', encoding="utf-8")
    assert {n.id for n in v.iter_notes("browse")} == {"2026-11-09", "browse-sites"}
    assert len(v.note_paths("browse")) == 3
