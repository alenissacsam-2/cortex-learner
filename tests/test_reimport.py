"""RR-18: a re-imported notes folder updates in place. A changed note rewrites its page (found
by its `From:` origin) while the page body is as the import wrote it (the header's `body:` sha);
a hand-edited page keeps its text, the new version comes in as `-2` and the app's Review shows a
card. Files gone from the folder are counted, never deleted. Older versions of a file rank
x STALE_WEIGHT in recall (a read-time fold over the header lines)."""

import pytest

from cortex import importers, semantic, sources
from cortex.app import server
from cortex.catalog import Catalog
from cortex.index import Index
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")


def _notes(d, **files):
    d.mkdir(exist_ok=True)
    for name, text in files.items():
        (d / f"{name}.md").write_text(text, encoding="utf-8")
    return d


def _page(root, nid):
    return (root / "resources" / f"{nid}.md").read_text("utf-8")


def test_a_changed_note_rewrites_its_untouched_page(root_plain, tmp_path):
    v, d = Vault(root_plain), _notes(tmp_path / "n", Plan="First plan.\n", Keep="Same.\n")
    importers.import_notes(v, d)
    assert sources.untouched(_page(root_plain, "notes-plan")) is True
    v._edit_fields("resource", "notes-plan", {"tags": ["mine"]})  # frontmatter is not the body
    (d / "Plan.md").write_text("Second plan.\n", encoding="utf-8")
    r = importers.import_notes(v, d)
    assert (r["new"], r["updated"], r["same"], r["gone"]) == (0, 1, 1, 0)
    page = _page(root_plain, "notes-plan")
    assert "Second plan." in page and "First plan." not in page and 'tags: ["mine"]' in page
    assert sources.untouched(page) is True
    assert not (root_plain / "resources/notes-plan-2.md").exists()
    assert importers.import_notes(v, d)["updated"] == 0  # nothing changed: nothing rewritten


def test_a_hand_edited_page_keeps_its_text_and_gets_a_review_card(root_plain, tmp_path):
    (root_plain / ".cortex").mkdir(exist_ok=True)
    v, d = Vault(root_plain), _notes(tmp_path / "n", Plan="First plan.\n")
    importers.import_notes(v, d)
    v.update_text(root_plain / "resources/notes-plan.md", lambda t: t + "My own line.\n")
    assert sources.untouched(_page(root_plain, "notes-plan")) is False
    (d / "Plan.md").write_text("Second plan.\n", encoding="utf-8")
    r = importers.import_notes(v, d)
    assert (r["new"], r["updated"]) == (1, 0)
    assert "My own line." in _page(root_plain, "notes-plan")
    assert "Second plan." in _page(root_plain, "notes-plan-2")
    app = server.App(root_plain)
    cards = app.review()["imports"]
    assert [(c["page"], c["new"], c["title"], c["private"]) for c in cards] == [
        ("notes-plan", "notes-plan-2", "Plan", False)]
    assert app.review()["more"] >= 1
    (d / "Plan.md").write_text("Third plan.\n", encoding="utf-8")  # the -2 page is untouched
    assert importers.import_notes(v, d)["updated"] == 1
    assert "Third plan." in _page(root_plain, "notes-plan-2")
    app.review_import_dismiss({"key": cards[0]["key"]})
    assert app.review()["imports"] == []
    with pytest.raises(server.NotFound):
        app.review_import_dismiss({"key": "nope"})


def test_a_crlf_note_counts_as_untouched(root_plain, tmp_path):
    """Audit 21 Nov I7: a note saved with Windows line endings is not taken as hand-edited:
    a changed one rewrites its page, with no -2 page and no Review card."""
    v, d = Vault(root_plain), tmp_path / "n"
    d.mkdir()
    (d / "Plan.md").write_bytes(b"First plan.\r\nStep two.\r\n")
    importers.import_notes(v, d)
    assert sources.untouched(_page(root_plain, "notes-plan")) is True
    (d / "Plan.md").write_bytes(b"Second plan.\r\nStep two.\r\n")
    r = importers.import_notes(v, d)
    assert (r["new"], r["updated"]) == (0, 1)
    assert "Second plan." in _page(root_plain, "notes-plan")
    assert sources.edited_pages(v, []) == []


def test_a_page_imported_before_body_shas_is_not_overwritten(root_plain, tmp_path):
    v, d = Vault(root_plain), _notes(tmp_path / "n", Plan="First plan.\n")
    importers.import_notes(v, d)
    p = root_plain / "resources/notes-plan.md"
    v.update_text(p, lambda t: sources.HEADER.sub(
        lambda m: m.group(0).replace(f" · body: {m.group(4)}", ""), t))
    assert sources.untouched(p.read_text("utf-8")) is None
    (d / "Plan.md").write_text("Second plan.\n", encoding="utf-8")
    assert importers.import_notes(v, d)["new"] == 1  # unknown: a new page, as before RR-18
    assert sources.edited_pages(v, []) == []  # but no card: it may not be edited


def test_a_page_starts_with_its_fact_header(root_plain, tmp_path):  # R3-6
    v = Vault(root_plain)
    d = _notes(tmp_path / "n", Plan="---\nsummary: The Q3 launch plan\n---\nFirst plan.\n",
               Bare="No frontmatter.\n")
    importers.import_notes(v, d)
    page = _page(root_plain, "notes-plan")
    head = page.split("· sha: ", 1)[1].split("\n", 1)[1]  # the body under the header line
    assert head.startswith("\nWhen: 20") and "\nSummary: The Q3 launch plan\n\nFirst plan." \
        in head
    assert sources.HEADER.search(page) and sources.untouched(page) is True
    assert "Who:" not in page and "Summary:" not in _page(root_plain, "notes-bare")  # unknown


def test_a_page_from_before_fact_headers_is_rewritten_in_place(root_plain, tmp_path):  # R3-6
    v, d = Vault(root_plain), _notes(tmp_path / "n", Plan="First plan.\n")
    importers.import_notes(v, d)
    p = root_plain / "resources/notes-plan.md"

    def old(t):  # the page as an import before R3-6 wrote it: no facts, body sha of the body
        t = "".join(ln for ln in t.splitlines(keepends=True) if not ln.startswith("When: "))
        t = t.replace("sha: " + t.split("· sha: ")[1][:16] + "\n\n\n", "sha: "
                      + t.split("· sha: ")[1][:16] + "\n\n")
        return sources.HEADER.sub(lambda m: m.group(0).replace(
            m.group(4), sources._sha("First plan.")), t)
    v.update_text(p, old)
    assert "When:" not in p.read_text("utf-8") and sources.untouched(p.read_text("utf-8"))
    assert importers.import_notes(v, d)["updated"] == 0  # an unchanged file: a no-op
    assert "When:" not in p.read_text("utf-8")
    (d / "Plan.md").write_text("Second plan.\n", encoding="utf-8")
    r = importers.import_notes(v, d)
    assert (r["new"], r["updated"]) == (0, 1)  # in place, not a -2 page
    assert "When: " in p.read_text("utf-8") and sources.untouched(p.read_text("utf-8"))
    assert not (root_plain / "resources/notes-plan-2.md").exists()


def test_files_gone_from_the_folder_are_counted_not_deleted(root_plain, tmp_path):
    v, d = Vault(root_plain), _notes(tmp_path / "n", A="Alpha note.\n", B="Beta note.\n")
    importers.import_notes(v, d)
    (d / "B.md").unlink()
    r = importers.import_notes(v, d)
    assert r["gone"] == 1 and (root_plain / "resources/notes-b.md").is_file()
    assert "1 earlier page not in the folder (kept)" in importers.report_line("Notes", r)
    src = importers.notes_source(d)
    src.walk_errors.append(PermissionError("a folder could not be read"))
    assert "gone" not in sources.run(v, src)  # an incomplete walk counts nothing as gone


def test_an_unreadable_subfolder_is_a_walk_error(tmp_path, monkeypatch):
    d = _notes(tmp_path / "n", A="Alpha.\n")
    (d / "sub").mkdir()
    real = importers.os.walk

    def walk(top, onerror=None):
        onerror(PermissionError(13, "denied", str(top / "sub")))
        yield from real(top)

    monkeypatch.setattr(importers.os, "walk", walk)
    assert len(importers.notes_source(d).walk_errors) == 1


def test_older_versions_rank_lower_in_recall(root_plain, tmp_path):
    v = Vault(root_plain)
    for nid, day in (("notes-plan", "2026-01-02"), ("notes-plan-2", "2026-03-04"),
                     ("notes-other", "2026-01-02")):
        title = "Other" if nid == "notes-other" else "Plan"
        origin = "Other.md" if nid == "notes-other" else "Plan.md"
        v.create_note("resource", nid, {"title": title, "kind": "resource"},
                      f"# {title}\n\nSource: Notes · Date: {day} · From: {origin} · "
                      f"sha: {'0' * 16}\n\nGraph search notes.\n")
    idx = Index(v, Catalog.load(v))
    older = semantic.older_versions(idx)
    assert older == {"notes-plan"}
    f = semantic._factors(idx, "notes-plan", False, None, older=older)
    assert f == {"older_version": semantic.STALE_WEIGHT}
    hits = semantic.recall(idx, "graph search notes", None)
    assert [h["id"] for h in hits].index("notes-plan-2") < \
        [h["id"] for h in hits].index("notes-plan")

