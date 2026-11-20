"""Docs, repos and highlights imports (v9 item 14): page shape, skip reasons, dedupe or update
on re-import, and a repo page never holding code. Fixtures are built in tmp_path: a PDF with
pypdf, a .docx with zipfile, a git repo under a test identity."""

import io
import os
import subprocess
import zipfile
from pathlib import Path

from typer.testing import CliRunner

from cortex import importers, more_sources, sources
from cortex.cli import app
from cortex.vault import Vault

runner = CliRunner()
C = sources.Audience.CLOUD


def _page(v: Vault, nid: str) -> str:
    return (v.root / "resources" / f"{nid}.md").read_bytes().decode("utf-8")


def _pdf(text: str = "", title: str = "", encrypt: str = "") -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    if text:
        font = w._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica")}))
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        s = DecodedStreamObject()
        s.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = w._add_object(s)
    meta = {"/CreationDate": "D:20250304120000Z"}
    if title:
        meta["/Title"] = title
    w.add_metadata(meta)
    if encrypt:
        w.encrypt(encrypt, algorithm="RC4-128")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _docx(paras: list[str], title: str = "") -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paras)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document {w}><w:body>{body}</w:body></w:document>")
        if title:
            z.writestr("docProps/core.xml", '<cp:coreProperties xmlns:cp="x" xmlns:dc='
                       f'"http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title>'
                       "</cp:coreProperties>")
    return buf.getvalue()


def _docs_folder(d: Path) -> Path:
    d.mkdir()
    (d / "thesis.pdf").write_bytes(_pdf("My thesis is about graph learning", "Graph Thesis"))
    (d / "plan.docx").write_bytes(_docx(["Study plan for spring", "Week one: linear algebra"],
                                        "Spring Plan"))
    (d / "notes.md").write_bytes(b"# Ideas\n\nA note I wrote.\n")
    (d / "locked.pdf").write_bytes(_pdf("secret plans", encrypt="pw"))
    (d / "scan.pdf").write_bytes(_pdf())
    (d / "word-locked.docx").write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 60)
    (d / "photo.png").write_bytes(b"\x89PNG")
    return d


def test_docs_pages_skips_and_dedupe(root_plain, tmp_path):
    v, d = Vault(root_plain), _docs_folder(tmp_path / "docs")
    src = more_sources.docs_source(d)
    r = sources.run(v, src, C)
    assert r["imported"] == 3 and r["reasons"]["encrypted"] == 2
    assert r["reasons"]["scanned or no text"] == 1 and r["reasons"]["not a document"] == 1
    pdf = _page(v, "docs-graph-thesis")
    assert 'title: "Graph Thesis"' in pdf and 'date: "2025-03-04"' in pdf
    assert "Source: Documents · Date: 2025-03-04 · From: " + str(d / "thesis.pdf") in pdf
    assert "My thesis is about graph learning" in pdf
    docx = _page(v, "docs-spring-plan")
    assert "Study plan for spring\nWeek one: linear algebra" in docx
    assert "secret plans" not in "".join(
        p.read_text("utf-8") for p in (root_plain / "resources").glob("docs-*.md"))
    again = sources.run(v, more_sources.docs_source(d), C)
    assert again["imported"] == 0 and again["duplicate"] == 3


def test_docs_reuse_an_earlier_notes_import(root_plain, tmp_path):
    v, d = Vault(root_plain), _docs_folder(tmp_path / "docs")
    importers.import_notes(v, d)
    r = sources.run(v, more_sources.docs_source(d), C)
    assert r["duplicate"] == 1 and r["imported"] == 2  # notes.md is already a notes page


def test_long_documents_are_clipped(root_plain, tmp_path):
    d = tmp_path / "long"
    d.mkdir()
    (d / "big.docx").write_bytes(_docx([f"paragraph number {i} " * 5 for i in range(200)]))
    it = next(i for i in more_sources.docs_source(d).items() if not isinstance(i, str))
    assert len(it.text) <= more_sources.DOC_CHARS + 2 and it.text.endswith(" …")


def _git(repo: Path, *args: str, email: str = "me@test.invalid") -> None:
    env = {**os.environ, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": email}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def _repo(parent: Path, name: str) -> Path:
    repo = parent / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "me@test.invalid")
    (repo / "README.md").write_bytes(
        b"# Planner\n\nA study planner I built.\n\n```python\nTOP_SECRET_README_CODE = 1\n```\n")
    (repo / "pyproject.toml").write_bytes(b"[project]\nname = 'SHOULD_NOT_APPEAR'\n")
    (repo / "package.json").write_bytes(b"{}")
    (repo / "app.py").write_bytes(b"API_KEY = 'zz-PRIVATE-CODE-STRING-42'\ndef main(): pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "First commit of the planner")
    (repo / "app.py").write_bytes(b"API_KEY = 'zz-PRIVATE-CODE-STRING-43'\n")
    _git(repo, "commit", "-qam", "Someone else's fix", email="other@test.invalid")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "Add weekly view")
    return repo


def test_repos_page_shape_no_code_and_update(root_plain, tmp_path):
    v, projects = Vault(root_plain), tmp_path / "projects"
    repo = _repo(projects / "work", "planner")  # two folders down
    (projects / "empty").mkdir()
    other = projects / "theirs"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "config", "user.email", "me@test.invalid")
    _git(other, "commit", "-q", "--allow-empty", "-m", "not mine", email="x@test.invalid")
    assert more_sources.find_repos(projects) == [other, repo]
    assert importers.detect(projects) == ["repos"]
    r = sources.run(v, more_sources.repos_source(projects), C)
    assert r["imported"] == 1 and r["reasons"]["no commits by you"] == 1
    page = _page(v, "repo-planner")
    assert "Git repository planner (JavaScript, Python): 2 commits by me" in page
    assert "## README\n# Planner\n\nA study planner I built." in page
    assert "- " in page and "Add weekly view" in page and "First commit of the planner" in page
    assert "Someone else's fix" not in page
    for code in ("zz-PRIVATE-CODE-STRING", "TOP_SECRET_README_CODE", "SHOULD_NOT_APPEAR",
                 "def main"):
        assert code not in page
    assert sources.CODE_LEFT_OUT in page
    again = sources.run(v, more_sources.repos_source(projects), C)
    assert again["imported"] == 0 and again["duplicate"] == 1 and again["updated"] == 0
    _git(repo, "commit", "-q", "--allow-empty", "-m", "Add monthly view")
    dry = sources.run(v, more_sources.repos_source(projects), C, dry_run=True)
    assert dry["updated"] == 1 and "monthly" not in _page(v, "repo-planner")
    upd = sources.run(v, more_sources.repos_source(projects), C)
    assert upd["updated"] == 1 and upd["imported"] == 0
    assert "Add monthly view" in _page(v, "repo-planner")
    assert len(list((root_plain / "resources").glob("repo-*.md"))) == 1


def test_repo_without_user_email_is_skipped(root_plain, tmp_path, monkeypatch):
    repo = _repo(tmp_path, "solo")
    _git(repo, "config", "--unset", "user.email")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    assert more_sources.repo_item(repo) == "no git user.email"


CLIPPINGS = (
    "﻿Deep Work (Cal Newport)\r\n- Your Highlight on page 12 | Location 180-182 | Added on "
    "Monday, 3 March 2025 10:00:00\r\n\r\nClarity about what matters provides clarity about "
    "what does not.\r\n==========\r\n"
    "﻿Deep Work (Cal Newport)\r\n- Your Note on page 12 | Added on Tuesday, March 4, 2025 "
    "9:00:00 AM\r\n\r\nApply this to study blocks\r\n==========\r\n"
    "﻿Deep Work (Cal Newport)\r\n- Your Bookmark on page 20 | Added on Tuesday, March 4, "
    "2025 9:05:00 AM\r\n\r\n\r\n==========\r\n"
    "Atomic Habits (James Clear)\r\n- Your Highlight at location 50 | Added on Friday, 1 November"
    " 2024 08:00:00\r\n\r\nYou do not rise to the level of your goals.\r\n==========\r\n")
READWISE = ("Highlight,Book Title,Book Author,Amazon Book ID,Note,Color,Tags,Location Type,"
            "Location,Highlighted at,Document tags\n"
            '"Make it work, then make it right.",Clean Code Notes,Kent Beck,,good one,yellow,,'
            "order,1,2024-06-01 10:00:00+00:00,\n"
            ",Clean Code Notes,Kent Beck,,,,,order,2,2024-06-02 10:00:00+00:00,\n")
POCKET = ("title,url,time_added,cursor,tags,status\n"
          "How FSRS works,https://example.org/fsrs,1730000000,,learning|memory,unread\n"
          "Bad row,https://example.org/x,,,,unread\n")


def test_kindle_and_readwise_pages(root_plain, tmp_path):
    v = Vault(root_plain)
    clip = tmp_path / "My Clippings.txt"
    clip.write_bytes(CLIPPINGS.encode("utf-8"))
    assert importers.detect(clip) == ["highlights"]
    r = sources.run(v, more_sources.highlights_source(clip), C)
    assert r["imported"] == 2 and r["reasons"]["bookmark or empty"] == 1
    page = _page(v, "highlights-deep-work")
    assert 'date: "2025-03-04"' in page
    assert ('My Kindle highlights from "Deep Work" by Cal Newport (2 highlights, 2025-03-03 '
            "to 2025-03-04).") in page
    assert '- 2025-03-03: "Clarity about what matters' in page
    assert "- 2025-03-04: My note: Apply this to study blocks" in page
    again = sources.run(v, more_sources.highlights_source(clip), C)
    assert again["duplicate"] == 2 and again["imported"] == again["updated"] == 0
    clip.write_bytes((CLIPPINGS + "Deep Work (Cal Newport)\r\n- Your Highlight on page 30 | "
                      "Added on Monday, 10 March 2025 10:00:00\r\n\r\nShallow work is easy."
                      "\r\n==========\r\n").encode("utf-8"))
    more = sources.run(v, more_sources.highlights_source(clip), C)
    assert more["updated"] == 1 and more["duplicate"] == 1 and more["imported"] == 0
    assert "Shallow work is easy." in _page(v, "highlights-deep-work")
    rw = tmp_path / "readwise.csv"
    rw.write_bytes(READWISE.encode())
    assert importers.detect(rw) == ["highlights"]
    r = sources.run(v, more_sources.highlights_source(rw), C)
    assert r["imported"] == 1 and r["reasons"]["bookmark or empty"] == 1
    page = _page(v, "highlights-clean-code-notes")
    assert ('- 2024-06-01: "Make it work, then make it right. (my note: good one)"' in page)


def test_pocket_list_is_month_pages(root_plain, tmp_path):
    v, f = Vault(root_plain), tmp_path / "part_000000.csv"
    f.write_bytes(POCKET.encode())
    src = more_sources.highlights_source(f)
    assert src.page == "month" and src.id == "highlights"
    r = sources.run(v, src, C)
    assert r["imported"] == 1 and r["reasons"]["no readable date"] == 1
    page = next((root_plain / "resources").glob("pocket-*.md")).read_text("utf-8")
    assert '- 2024-10-27 saved "How FSRS works" (https://example.org/fsrs), tagged learning, ' \
           "memory" in page
    assert sources.run(v, more_sources.highlights_source(f), C)["duplicate"] == 1


def test_cli_imports_each_new_source(root_plain, tmp_path):
    d = _docs_folder(tmp_path / "docs")
    out = runner.invoke(app, ["import", str(d), "--source", "docs", "--vault",
                              str(root_plain)]).output
    assert "Documents: tier normal" in out and "Documents: read 7, imported 3" in out
    _repo(tmp_path / "projects", "planner")
    out = runner.invoke(app, ["import", str(tmp_path / "projects"), "--vault", str(root_plain),
                              "--tier", "private"]).output
    assert "Git: read 1, imported 1, private 1" in out
    assert "private: true" in _page(Vault(root_plain), "repo-planner")
    clip = tmp_path / "My Clippings.txt"
    clip.write_bytes(CLIPPINGS.encode("utf-8"))
    out = runner.invoke(app, ["import", str(clip), "--dry-run", "--vault",
                              str(root_plain)]).output
    assert "Highlights: read 3, imported 2" in out and "dry run" in out
