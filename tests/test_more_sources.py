"""Docs, repos and highlights imports (v9 item 14): page shape, skip reasons, dedupe or update
on re-import, and a repo page never holding code. Fixtures are built in tmp_path: a PDF with
pypdf, a .docx with zipfile, a git repo under a test identity."""

import io
import os
import subprocess
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cortex import importers, more_sources, semantic, sources
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


def test_folder_rules_match_whole_segments_and_fold_case_on_windows(monkeypatch):
    """RR-37: a rule names whole folders; hidden and tool folders are always left out."""
    sf = more_sources.skip_folder
    assert sf("School/Archive", ("Archive",)) and sf("Archive/x", ("/Archive/",))
    assert not sf("School/Archived", ("Archive",)) and not sf("MyArchive", ("Archive",))
    assert sf("a/School/Old/b", ("School/Old",)) and not sf("School/b/Old", ("School/Old",))
    for d in ("node_modules", "x/.git", ".venv", "src/__pycache__", "a/.obsidian/b"):
        assert sf(d)
    assert not sf("") and not sf("node_modules_old") and not sf("git")
    monkeypatch.setattr(more_sources, "FOLD_CASE", True)
    assert sf("School/ARCHIVE", ("archive",)) and sf("Node_Modules")
    monkeypatch.setattr(more_sources, "FOLD_CASE", False)
    assert not sf("School/ARCHIVE", ("archive",)) and not sf("Node_Modules")


def test_docs_skip_tool_folders_rules_and_shortcuts(root_plain, tmp_path, monkeypatch):
    d = tmp_path / "docs"
    for rel in ("node_modules/pkg/readme.md", ".git/notes.md", "Archive/old.md",
                "Archived/kept.md", "top.md"):
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(f"# {rel}\n\nText of {rel}.\n", encoding="utf-8")
    for name in ("Report.gdoc", "Site.url", "Thesis.lnk"):
        (d / name).write_text("{}", encoding="utf-8")
    seen = []
    real = more_sources.skip_folder
    monkeypatch.setattr(more_sources, "skip_folder",
                        lambda rel, rules=(): seen.append(rel) or real(rel, rules))
    r = sources.run(Vault(root_plain), more_sources.docs_source(d, skip=("Archive",)), C)
    assert r["imported"] == 2 and r["reasons"]["a shortcut, not the file"] == 3
    assert r["read"] == 5  # pruned folders are never listed, so never counted
    assert not any(s.startswith("node_modules/") for s in seen)  # pruned, not walked


def test_a_parse_that_hangs_or_crashes_is_a_skip(tmp_path, monkeypatch):
    """RR-37: the child is killed after PARSE_TIMEOUT; a crashed child is "unreadable"."""
    d = tmp_path / "docs"
    d.mkdir()
    (d / "a.pdf").write_bytes(_pdf("first"))
    (d / "b.pdf").write_bytes(_pdf("second"))
    monkeypatch.setattr(more_sources, "PARSE_TIMEOUT", 1.0)
    monkeypatch.setattr(more_sources, "_CHILD", [sys.executable, "-c",
                                                  "import time; time.sleep(60)"])
    assert list(more_sources.docs_source(d).items()) == ["parse timed out"] * 2
    monkeypatch.setattr(more_sources, "_CHILD", [sys.executable, "-c", "pass"])
    assert list(more_sources.docs_source(d).items()) == ["unreadable"] * 2


def scan_pdf(pages: list[str], size: tuple[int, int] = (400, 120)) -> bytes:
    """C2: a scanned PDF made with Pillow: each page one image of its words, no text layer."""
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("arial.ttf", size[0] // 28)
    except OSError:
        font = ImageFont.load_default(size[0] // 28)
    ims = []
    for words in pages:
        im = Image.new("RGB", size, "white")
        ImageDraw.Draw(im).text((size[0] // 12, size[1] // 10), words, fill="black", font=font)
        ims.append(im)
    buf = io.BytesIO()
    ims[0].save(buf, "PDF", resolution=200, save_all=True, append_images=ims[1:])
    return buf.getvalue()


@pytest.fixture
def ocr(monkeypatch):
    """Windows OCR mocked in this process: each page OCR'd reads "read page <n>"."""
    seen = []
    monkeypatch.setattr(more_sources, "_ocr_engine", lambda: "engine")

    def fake(engine, page):
        seen.append(len(page.images))
        return f"read page {len(seen)}"
    monkeypatch.setattr(more_sources, "_ocr", fake)
    return seen


def _with_text(scan: bytes, text: str) -> bytes:
    """`scan`'s first page with `text` laid over it: a page with text and an image."""
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(scan)))
    w.pages[0].merge_page(PdfReader(io.BytesIO(_pdf(text))).pages[0])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_only_pages_with_no_text_and_an_image_are_ocrd(ocr):
    assert more_sources._pdf_pages(scan_pdf(["Heaps"]), 10**6)[2:] == (["read page 1"], 1, 1)
    mixed = _with_text(scan_pdf(["Heaps"]), "Typed words")
    _, _, pages, total, n = more_sources._pdf_pages(mixed, 10**6)
    assert "Typed words" in pages[0] and (total, n) == (1, 0) and ocr == [1]  # not OCR'd
    # a blank page has no image: nothing to OCR, and it is not a scan
    assert more_sources._pdf_pages(_pdf(), 10**6)[2:] == ([""], 1, 0) and ocr == [1]


def test_ocr_reads_at_most_ocr_pages_and_stops_past_its_time(ocr, monkeypatch):
    assert more_sources.OCR_PAGES == 100 and more_sources.OCR_BUDGET < more_sources.PARSE_TIMEOUT
    monkeypatch.setattr(more_sources, "OCR_PAGES", 2)
    got = more_sources._pdf_pages(scan_pdf(["a", "b", "c"]), 10**6)
    assert got[2:] == (["read page 1", "read page 2"], 3, 2)  # read up to page 2 of 3
    monkeypatch.setattr(more_sources, "OCR_BUDGET", -1.0)
    assert more_sources._pdf_pages(scan_pdf(["a"]), 10**6)[2:] == ([], 1, 0)


def test_one_page_that_fails_ocr_is_skipped_not_the_file(monkeypatch):
    monkeypatch.setattr(more_sources, "_ocr_engine", lambda: "engine")
    calls = []

    async def recognize(engine, images):
        calls.append(len(images))
        if len(calls) == 2:
            raise OSError("WinRT could not decode it")
        return f"line {len(calls)}"
    monkeypatch.setattr(more_sources, "_recognize", recognize)
    got = more_sources._pdf_pages(scan_pdf(["a", "b", "c"]), 10**6)
    assert got[2:] == (["line 1", "", "line 3"], 3, 2) and calls == [1, 1, 1]


def test_without_ocr_a_scan_is_skipped_as_before(root_plain, monkeypatch):
    monkeypatch.setattr(more_sources, "_ocr_engine", lambda: None)
    data = scan_pdf(["Heaps"])
    assert more_sources._pdf_pages(data, 10**6)[2:] == ([""], 1, None)
    src = more_sources.doc_source("scan.pdf", len(data), 0.0, lambda *a, **k: data,
                                  lambda kind, d: more_sources._pdf(d))
    assert list(src.items()) == ["scanned or no text"]


def test_an_ocrd_document_page_says_so(root_plain, ocr):
    data, v = scan_pdf(["Heaps", "Tries"]), Vault(root_plain)
    src = more_sources.doc_source("lecture-scan.pdf", len(data), 0.0, lambda *a, **k: data,
                                  lambda kind, d: more_sources._pdf(d))
    assert sources.run(v, src, C)["imported"] == 1
    page = _page(v, "docs-lecture-scan")
    assert f"## Page 1\nread page 1\n\n## Page 2\nread page 2\n\n{more_sources.OCR_LINE}" in page
    assert 'summary: "read page 1' in page  # the mark is last, so it is not the summary
    text = sources.run(v, more_sources.doc_source(
        "typed.pdf", 9, 0.0, lambda *a, **k: _pdf("Typed"), lambda kind, d: more_sources._pdf(d)),
        C)
    assert text["imported"] == 1 and more_sources.OCR_LINE not in _page(v, "docs-typed")


@pytest.mark.slow
def test_windows_ocr_reads_a_scan_in_the_child(tmp_path):
    """The real engine, through the parse child; skipped off Windows or with no OCR language."""
    if sys.platform != "win32" or more_sources._ocr_engine() is None:
        pytest.skip("no Windows OCR on this computer")
    d = tmp_path / "docs"
    d.mkdir()
    (d / "scan.pdf").write_bytes(scan_pdf(["Photosynthesis makes glucose", "Mitochondria 1234"],
                                          size=(1700, 2200)))
    it = next(iter(more_sources.docs_source(d).items()))
    assert not isinstance(it, str), it
    assert it.text.endswith(more_sources.OCR_LINE)
    assert "Photosynthesis makes glucose" in it.text and "Mitochondria 1234" in it.text


def test_docs_reuse_an_earlier_notes_import(root_plain, tmp_path):
    v, d = Vault(root_plain), _docs_folder(tmp_path / "docs")
    importers.import_notes(v, d)
    r = sources.run(v, more_sources.docs_source(d), C)
    assert r["duplicate"] == 1 and r["imported"] == 2  # notes.md is already a notes page


def test_long_documents_are_whole(root_plain, tmp_path):
    """R17: a .docx keeps every paragraph, as it is; its key is the 1,500 characters it kept
    before."""
    d = tmp_path / "long"
    d.mkdir()
    (d / "big.docx").write_bytes(_docx([f"paragraph number {i} " * 5 for i in range(200)]))
    it = next(i for i in more_sources.docs_source(d).items() if not isinstance(i, str))
    assert len(it.text) > 10 * more_sources.DOC_CHARS and "\nparagraph number 199" in it.text
    assert it.key == more_sources._clip(it.text, more_sources.DOC_CHARS)


WORDS = "heaps keep the smallest key on top "


def _pdfs(pages: list[str]) -> bytes:
    """A PDF of `pages`, each one line of text ("" is a page with none)."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    w = PdfWriter()
    font = w._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica")}))
    for text in pages:
        page = w.add_blank_page(612, 792)
        if text:
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
            s = DecodedStreamObject()
            s.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
            page[NameObject("/Contents")] = w._add_object(s)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _book(n: int = 3, empty: int = 2) -> list[str]:
    """`n` pages of about 1,100 characters, page `empty` with no text."""
    return ["" if i == empty else f"Chapter {i} " + WORDS * 30 for i in range(1, n + 1)]


def _doc_run(v: Vault, data: bytes, name: str = "book.pdf") -> dict:
    def parse(kind, d):
        return more_sources._pdf(d) if kind == "pdf" else more_sources._docx(d)
    return sources.run(v, more_sources.doc_source(name, len(data), 0.0, lambda *a, **k: data,
                                                  parse), C)


def test_a_pdf_keeps_every_page_under_its_page_heading(root_plain):
    v = Vault(root_plain)
    assert _doc_run(v, _pdfs(_book()))["imported"] == 1
    page = _page(v, "docs-book")
    assert "\n\n## Page 1\nChapter 1 heaps" in page and "## Page 2" not in page  # no text
    assert "\n\n## Page 3\nChapter 3 heaps" in page
    assert page.split("## Page 1")[1].count("smallest key") == 60
    assert 'summary: "Chapter 1 heaps keep' in page and "Cortex kept" not in page
    assert "Source: Documents · Date: " in page and "\n\nWhen: " in page  # the header as before
    note = v.read_path(v.root / "resources" / "docs-book.md")
    chunks = semantic._chunks({"title": "book", "kind": "resource", "fields": note.fields,
                               "body": note.body})
    assert len(chunks) > 1 and chunks[-1][1].startswith("book › ## Page 3 · ")


def test_past_the_cap_whole_pages_are_kept_and_the_page_says_so(root_plain, monkeypatch):
    monkeypatch.setattr(more_sources, "DOC_FULL_CHARS", 2500)
    v = Vault(root_plain)
    assert _doc_run(v, _pdfs(_book(4, empty=0)))["imported"] == 1
    page = _page(v, "docs-book")
    assert "\n\nWhen: " + more_sources._day(0.0) + "\n\nCortex kept the first 2 of 4 pages.\n\n"\
        "## Page 1\nChapter 1" in page and "## Page 2\nChapter 2" in page
    assert "Chapter 3" not in page and 'summary: "Chapter 1 heaps' in page
    paged = more_sources._paged
    assert paged(["a", " ", "b  c"], 5) == (
        "Cortex kept the first 3 of 5 pages.\n\n## Page 1\na\n\n## Page 3\nb c", "a b c")
    one, _ = paged(["x " * 2000], 1)  # one page over the cap alone: clipped, never dropped
    assert one.startswith("## Page 1\nx x") and len(one) <= 2502 and "Cortex kept" not in one


def test_a_long_scan_says_how_many_pages_ocr_read(root_plain, ocr, monkeypatch):
    monkeypatch.setattr(more_sources, "OCR_PAGES", 1)
    v = Vault(root_plain)
    data = scan_pdf(["a", "b"])
    assert _doc_run(v, data, "scan.pdf")["imported"] == 1
    page = _page(v, "docs-scan")
    assert "Cortex kept the first 1 of 2 pages.\n\n## Page 1\nread page 1\n\n" in page
    assert page.endswith(more_sources.OCR_LINE + "\n") and 'summary: "read page 1"' in page


def _old_import(v: Vault, data: bytes, name: str) -> None:
    """The documents import as it was before R17: the first DOC_CHARS characters on one line
    (a .docx: its paragraphs), no page headings and no `body:` mark."""
    got = more_sources._pdf(data) if name.endswith(".pdf") else more_sources._docx(data)
    text = " ".join(" ".join(got[2]).split()) if name.endswith(".pdf") else got[2]
    it = sources.Item(got[0] or Path(name).stem, got[1] or more_sources._day(0.0),
                      more_sources._clip(text, more_sources.DOC_CHARS), name)
    sources.run(v, sources.Source("docs", "Documents", "folder", lambda: iter([it]),
                                  skips=more_sources.DOC_SKIPS), C)


def test_a_page_imported_clipped_is_refilled_once(root_plain):
    v, data = Vault(root_plain), _pdfs(_book(4))
    _old_import(v, data, "book.pdf")
    old = _page(v, "docs-book")
    assert "Chapter 4" not in old and " · body: " not in old and "## Page" not in old
    v._edit_fields("resource", "docs-book", {"tags": ["mine"]})
    r = _doc_run(v, data)
    assert (r["imported"], r["updated"], r["duplicate"]) == (0, 1, 0)
    page = _page(v, "docs-book")
    assert "## Page 4\nChapter 4" in page and 'tags: ["mine"]' in page  # the owner's field
    assert 'summary: "Chapter 1 heaps' in page and " · body: " in page
    assert old.split(" · sha: ")[1][:16] == page.split(" · sha: ")[1][:16]  # the same key
    assert sorted(p.name for p in (v.root / "resources").glob("docs-*")) == ["docs-book.md"]
    again = _doc_run(v, data)
    assert (again["imported"], again["updated"], again["duplicate"]) == (0, 0, 1)
    # a short .docx imported before is the same text: a duplicate, its page untouched
    short = _docx(["Study plan for spring", "Week one"])
    _old_import(v, short, "plan.docx")
    before = _page(v, "docs-plan")
    r = _doc_run(v, short, "plan.docx")
    assert (r["updated"], r["duplicate"]) == (0, 1) and _page(v, "docs-plan") == before


def test_another_file_with_the_same_opening_never_rewrites_the_page(root_plain):
    """Two files whose first DOC_CHARS characters match share a key: the second is a duplicate,
    as before R17, and the first file's page keeps its own text."""
    v = Vault(root_plain)
    assert _doc_run(v, _pdfs(_book(4)), "book.pdf")["imported"] == 1
    before = _page(v, "docs-book")
    # pages 1 and 3 hold the key; page 4 differs
    r = _doc_run(v, _pdfs(_book(4)[:3] + ["Chapter 4 something else entirely"]), "copy.pdf")
    assert (r["imported"], r["updated"], r["duplicate"]) == (0, 0, 1)
    assert _page(v, "docs-book") == before
    assert sorted(p.name for p in (v.root / "resources").glob("docs-*")) == ["docs-book.md"]


def _append(v: Vault, nid: str, line: str) -> None:
    p = v.root / "resources" / f"{nid}.md"
    p.write_bytes(p.read_bytes() + line.encode())


def test_an_owner_edited_page_is_left_as_it_is(root_plain):
    v, data = Vault(root_plain), _pdfs(_book())
    _old_import(v, data, "book.pdf")
    _append(v, "docs-book", "My own note.\n")
    before = _page(v, "docs-book")
    r = _doc_run(v, data)
    assert r["updated"] == 0 and r["reasons"][sources.EDITED] == 1
    assert _page(v, "docs-book") == before
    assert "skipped 1 (1 you edited the page; it was left as it is)" in \
        importers.report_line("Documents", r)
    # a page with a `body:` mark: edited, then the file grows; the edit stays
    assert _doc_run(v, _pdfs(["Lecture " + WORDS * 50]), "lecture.pdf")["imported"] == 1
    _append(v, "docs-lecture", "Mine.\n")
    edited = _page(v, "docs-lecture")
    r = _doc_run(v, _pdfs(["Lecture " + WORDS * 50, "A new page"]), "lecture.pdf")
    assert r["updated"] == 0 and r["reasons"][sources.EDITED] == 1
    assert _page(v, "docs-lecture") == edited


def test_other_updating_sources_still_rewrite_and_summarise_as_before(root_plain):
    """Only the docs import keeps edits and skips its page headings in the summary."""
    v = Vault(root_plain)

    def src(text):
        return sources.Source("repos", "Repos", "folder", lambda: iter(
            [sources.Item("Planner", "2025-01-01", text, key="planner")]), updates=True)
    sources.run(v, src("## Page 1\nfirst words"), C)
    assert 'summary: "Page 1"' in _page(v, "repos-planner")
    _append(v, "repos-planner", "My edit.\n")
    r = sources.run(v, src("## Page 1\nsecond words"), C)
    assert r["updated"] == 1 and sources.EDITED not in r["reasons"]
    page = _page(v, "repos-planner")
    assert "second words" in page and "My edit." not in page


@pytest.mark.perf
def test_a_250_page_pdf_parses_well_inside_the_timeout():
    """R17: the docs import reads up to DOC_FULL_CHARS (about 250 dense pages) in the child,
    measured at about 5 s, well inside PARSE_TIMEOUT."""
    import time
    data = _pdfs([f"Page {i} " + WORDS * 57 for i in range(1, 251)])  # ~2,000 characters each
    parse = more_sources._Parser()
    try:
        t = time.perf_counter()
        got = parse("pdf", data)
        took = time.perf_counter() - t
    finally:
        parse.close()
    assert not isinstance(got, str) and took < more_sources.PARSE_TIMEOUT / 3, took
    text, _ = more_sources._paged(list(got[2]), got[3])
    head, pages = text.split("\n\n", 1)  # KEPT's line, then whole pages up to the cap
    assert head.startswith("Cortex kept the first 24") and head.endswith(" of 250 pages.")
    assert 490_000 < len(pages) <= more_sources.DOC_FULL_CHARS


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


def test_an_updated_page_stays_private(root_plain, tmp_path):
    """A page imported private, or marked private by hand, keeps `private: true` when a
    re-import at the normal tier rewrites it."""
    v, projects = Vault(root_plain), tmp_path / "projects"
    repo = _repo(projects, "planner")
    _repo(projects, "notebook")
    sources.run(v, more_sources.repos_source(projects), sources.Audience.LOCAL)
    sources.run(v, more_sources.repos_source(projects), C)  # unchanged: nothing rewritten
    assert "private: true" in _page(v, "repo-planner")
    nb = root_plain / "resources" / "repo-notebook.md"
    nb.write_bytes(_page(v, "repo-notebook").replace("private: true\n", "").encode())
    assert "private: true" not in _page(v, "repo-notebook")
    sources.run(v, more_sources.repos_source(projects), C)
    nb.write_bytes(_page(v, "repo-notebook").replace("---\n#", "private: true\n---\n#", 1)
                   .encode())  # marked private by hand
    (repo / "README.md").write_bytes(b"# Planner\n\nNow with a calendar view.\n")
    (projects / "notebook" / "README.md").write_bytes(b"# Notebook\n\nChanged too.\n")
    r = sources.run(v, more_sources.repos_source(projects), C)
    assert r["updated"] == 2 and r["private"] == 2
    for nid, text in (("repo-planner", "Now with a calendar view."),
                      ("repo-notebook", "Changed too.")):
        page = _page(v, nid)
        assert text in page and "private: true" in page


@pytest.mark.parametrize("subject, word", [
    ("Switch to uv for installs", "switch to"), ("Switched to SQLite FTS5", "switched to"),
    ("Migrated to the mcp 2.x SDK", "migrated to"), ("Adopt ruff", "adopt"),
    ("Deprecate the old dashboard", "deprecate"),
    ("Replace pickle with JSON in the cache", "replace … with"),
    ("Use a heap instead of a sorted list", "instead of"),
    ("Drop Flask in favour of FastAPI", "in favour of"), ("ADR 3: one vault per user", "adr"),
    ("Decided: no cloud sync", "decided"), ("Moved to pnpm", "moved to"),
    ("Default to the normal tier", "default to"), ("Chose FSRS over SM-2", "chose"),
    ("Merge branch 'switch to uv'", None), ("Bump deps; migrate to 3.12", None),
    ("Revert \"Switch to uv\"", None), ("WIP: adopt ruff", None), ("fixup! decided", None),
    ("Formatting: instead of tabs", None), ("Remove the old CLI", None),
    ("Rename module", None), ("Add weekly view", None),
    ("Wipe the cache; switch to v2", "switch to"),
])
def test_decision_word(subject, word):
    assert more_sources.decision_word(subject) == word


def _decisions_repo(parent: Path) -> Path:
    repo = _repo(parent, "planner")
    for day, msg in (("2026-01-05", "Switch to uv for installs"),
                     ("2026-02-10", "Merge branch 'adopt-ruff'"),
                     ("2026-03-15", "Replace pickle with JSON in the cache"),
                     ("2026-04-20", "Tune the quiz timer")):
        _git(repo, "commit", "-q", "--allow-empty", f"--date={day}T10:00:00", "-m", msg)
    _git(repo, "commit", "-q", "--allow-empty", "--date=2026-05-01T10:00:00", "-m",
         "Adopt ruff for lint", email="other@test.invalid")  # not mine
    return repo


def test_repo_decisions_are_listed_and_offered_once(root_plain, tmp_path):
    v, projects = Vault(root_plain), tmp_path / "projects"
    _decisions_repo(projects)
    sources.run(v, more_sources.repos_source(projects), C)
    page = _page(v, "repo-planner")
    section = page.split(f"## {more_sources.DECISIONS}\n")[1]
    assert section.count("\n- ") == 1 and "Merge branch" not in section and "Adopt" not in section
    assert "2026-03-15" in section and "(replace … with): Replace pickle with JSON" in section
    props = more_sources.decision_proposals(v, [])
    assert [(x["date"], x["word"], x["subject"], x["project"], x["private"]) for x in props] == [
        ("2026-03-15", "replace … with", "Replace pickle with JSON in the cache", "planner", False),
        ("2026-01-05", "switch to", "Switch to uv for installs", "planner", False)]
    assert all(len(x["hash"]) == 12 for x in props)
    again = sources.run(v, more_sources.repos_source(projects), C)  # a re-import
    assert again["duplicate"] == 1 and more_sources.decision_proposals(v, []) == props
    done = [{"id": "e1", "kind": "change", "payload": {"change": {
        "tool": "dismiss", "id": "decisions", "hash": props[0]["key"]}}}]
    assert more_sources.decision_proposals(v, done) == props[1:]
    voided = [*done, {"kind": "void", "ref": "e1"}]
    assert more_sources.decision_proposals(v, voided) == props


def test_a_private_repo_offers_private_decisions(root_plain, tmp_path):
    v, projects = Vault(root_plain), tmp_path / "projects"
    _decisions_repo(projects)
    sources.run(v, more_sources.repos_source(projects), sources.Audience.LOCAL)
    assert all(x["private"] for x in more_sources.decision_proposals(v, []))
    sources.run(v, more_sources.repos_source(projects), sources.Audience.SKIP)
    assert len(more_sources.decision_proposals(v, [])) == 2


def test_git_with_no_output_is_a_clear_skip(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "solo")
    real = more_sources._git

    def no_log(r, *args):
        got = real(r, *args)
        return subprocess.CompletedProcess(got.args, 0, None if args[0] == "log" else got.stdout)

    monkeypatch.setattr(more_sources, "_git", no_log)
    assert more_sources.repo_item(repo) == "git could not read it"


@pytest.mark.usefixtures("home")
def test_review_approves_and_dismisses_commit_decisions(root, tmp_path):
    from test_review import changes

    from cortex.app import server
    projects = tmp_path / "projects"
    _decisions_repo(projects)
    sources.run(Vault(root), more_sources.repos_source(projects), sources.Audience.LOCAL)
    (root / ".cortex").mkdir(exist_ok=True)
    app = server.App(root)
    view = app.review()
    first, second = view["decisions"]
    assert view["more"] >= 2 and first["subject"] == "Replace pickle with JSON in the cache"
    view = app.review_decision_approve({"key": first["key"]})
    assert [x["key"] for x in view["decisions"]] == [second["key"]]
    (c,) = changes(root, "decisions")
    assert (c["tool"], c["hash"], c["confirm"]) == ("decide", first["key"], "clicked")
    page = (root / "decisions" / f"{c['target']}.md").read_text("utf-8")
    assert 'title: "Replace pickle with JSON in the cache"' in page and "private: true" in page
    assert 'date: "2026-03-15"' in page and 'status: "accepted"' in page
    due = date.fromisoformat(server.App._today()) + timedelta(days=21)  # from the approve day
    assert f'review_on: "{due.isoformat()}"' in page
    assert "[[repo-planner]]" in page and "planner" in page
    assert app.review_decision_dismiss({"key": second["key"]})["decisions"] == []
    with pytest.raises(server.NotFound):
        app.review_decision_approve({"key": second["key"]})
    sources.run(Vault(root), more_sources.repos_source(projects), sources.Audience.LOCAL)
    assert app.review()["decisions"] == []  # a re-import does not offer them again


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


def test_a_kindle_or_whatsapp_txt_past_the_note_cap_still_imports(root_plain, tmp_path,
                                                                   monkeypatch):
    """Audit 21 Nov I10: My Clippings.txt and WhatsApp exports are .txt but not notes: the
    note cap (MAX_BYTES, made tiny here) does not refuse them."""
    from cortex import life_sources
    monkeypatch.setattr(importers, "MAX_BYTES", 100)
    v = Vault(root_plain)
    clip = tmp_path / "My Clippings.txt"
    clip.write_bytes(CLIPPINGS.encode("utf-8"))
    assert sources.run(v, more_sources.highlights_source(clip), C)["imported"] == 2
    chat = tmp_path / "WhatsApp Chat with Me.txt"
    chat.write_text("13/03/2024, 09:15 - Me: " + "buy milk and bread " * 20 + "\n",
                    encoding="utf-8")
    assert sources.run(v, life_sources.selfchat_source(chat), C)["imported"] == 1


def test_a_book_crossing_many_highlights_keeps_one_page_per_part(root_plain, tmp_path,
                                                                  monkeypatch):
    """Audit 21 Nov I11: a book that grows past MANY highlights splits by month without
    leaving its old whole-book page beside the new ones."""
    monkeypatch.setattr(more_sources, "MANY", 2)
    v = Vault(root_plain)
    clip = tmp_path / "My Clippings.txt"
    clip.write_bytes(CLIPPINGS.encode("utf-8"))
    sources.run(v, more_sources.highlights_source(clip), C)
    clip.write_bytes((CLIPPINGS + "Deep Work (Cal Newport)\r\n- Your Highlight on page 30 | "
                      "Added on Thursday, 10 April 2025 10:00:00\r\n\r\nShallow work is easy."
                      "\r\n==========\r\n").encode("utf-8"))
    r = sources.run(v, more_sources.highlights_source(clip), C)
    pages = sorted(p.stem for p in (root_plain / "resources").glob("highlights-deep-work*"))
    assert pages == ["highlights-deep-work", "highlights-deep-work-2025-04"], pages
    assert (r["imported"], r["updated"]) == (1, 1)
    assert "Shallow work" not in _page(v, "highlights-deep-work")
    assert "Clarity about what matters" in _page(v, "highlights-deep-work")


def test_kindle_and_readwise_pages(root_plain, tmp_path):
    v = Vault(root_plain)
    clip = tmp_path / "My Clippings.txt"
    clip.write_bytes(CLIPPINGS.encode("utf-8"))
    assert importers.detect(clip) == ["highlights"]
    r = sources.run(v, more_sources.highlights_source(clip), C)
    assert r["imported"] == 2 and r["reasons"]["bookmark or empty"] == 1
    page = _page(v, "highlights-deep-work")
    assert 'date: "2025-03-04"' in page
    assert "\n\nWhen: 2025-03-04\nWho: Cal Newport\n\nMy Kindle" in page  # R3-6's fact header
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
