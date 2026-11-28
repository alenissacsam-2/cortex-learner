"""Three `keepfold import` sources on `sources.Source` (v9 item 14): documents, the owner's git
repos and reading highlights. Each page is short, dated and says what it is in its first line,
so the local Chat (a small model reading a few ~700-character passages) can answer from it.

- docs: one page per .pdf/.docx (pypdf; .docx through zipfile + its word/document.xml): the
  title (PDF metadata, else the file name), the date (PDF creation date, else the file's), the
  full text up to DOC_FULL_CHARS (R17; a PDF's pages under `## Page N` headings, `_paged`).
  Keyed by its first DOC_CHARS characters (all a page held before R17) with `updates`, so a
  re-import refills a page imported clipped, unless the owner edited it (`keeps_edits`).
  .md/.txt go through importers.note_item, keyed as notes are, so a
  note imported before is a duplicate here. Encrypted, scanned (no text) and oversized files are
  skipped with the reason. C2: a scanned PDF's pages are read with the OS's own offline OCR
  (Windows OCR, macOS Vision, Tesseract on Linux) in the parse child (`_pdf_pages`), and the
  page ends with `ocr_line` (OCR_LINE on Windows).
- repos: one page per git repo one or two folders down (or the folder itself): the README's
  start, the stack from manifest file names, and from `git log --author=<that repo's
  user.email>` the commit count, first and last dates and the last SUBJECTS subjects. Code,
  diffs and other files' contents are never read; `drops_code` turns a README's code blocks
  into "[code left out]". Keyed by the repo's path with `updates`, so a re-import rewrites it.
  Of the last MINED subjects, the decision-like ones (`decision_word`) are listed on the page;
  the app's Review offers each as an unconfirmed decision (`decision_proposals`).
- highlights: Kindle `My Clippings.txt` and Readwise CSV make one page per book (one per book
  and month past MANY highlights), keyed by book with `updates`; a Pocket CSV (saved articles,
  no highlights) makes month pages. The format is read from the content, not the file name.
"""

from __future__ import annotations

import calendar
import csv
import functools
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from keepfold import importers, life_sources, sources
from keepfold._proc import NO_WINDOW
from keepfold.vault import VaultError, is_private

DOC_SUFFIXES = (".pdf", ".docx")
DOC_CHARS, README_CHARS, SUBJECTS, MANY = 1500, 800, 20, 50
# R17: a document page keeps its full text up to this (about 250 dense pages), so embedding it
# and opening it in Obsidian stay quick; DOC_CHARS is now only the dedupe key (what the import
# kept before R17, so a page imported then is found and refilled)
DOC_FULL_CHARS = 500_000
KEPT = "Keepfold kept the first {} of {} pages."  # a PDF page past DOC_FULL_CHARS or the OCR limits
# the docs import's lines that are never a page's summary: its page headings and KEPT's line
NOT_SUMMARY = re.compile(r"## Page \d+|(?:Keepfold|Keepfold) kept the first \d+ of \d+ pages\.")
MINED, SHOWN_DECISIONS = 200, 5  # subjects mined per repo; decision cards shown at a time
DECISIONS = "Decisions in my commits"  # the repo page's section of decision-like subjects
# RR-9's narrow set: bare remove, drop and rename are left out (too noisy)
_DECIDES = re.compile(
    r"\b(decided|decide|decision|chose|choose|switch(?:ed)? to|migrated? to|adopt(?:ed)?|"
    r"deprecated?|replaced? .{1,60}? with|instead of|in favou?r of|default to|moved? to|ADR)\b",
    re.I)
_NOT_DECISION = re.compile(
    r"^\W*(?:merge|bump|release|wip|fixup|typo|lint|format|revert)(?:s|ed|ing|ting)?\b", re.I)
_HIT = re.compile(r"^- (\d{4}-\d\d-\d\d) ([0-9a-f]{12}) \(([^)]+)\): (.+)$")
DOC_SKIPS = ("not a document", "encrypted", "scanned or no text", "over 200 MB", "unreadable",
             "over 5 MB", "not UTF-8 text", "a shortcut, not the file", "parse timed out")
# RR-37: folders never walked (any hidden folder too), and shortcut files that only point at a
# document somewhere else (a cloud doc, a link) and hold none of its text
SKIP_DIRS = frozenset({"node_modules", ".git", ".venv", "__pycache__"})
POINTERS = (".url", ".lnk", ".gdoc", ".gsheet", ".gslides")
FOLD_CASE = os.name == "nt"  # Windows folder names ignore case (tests patch it)
PARSE_TIMEOUT = 30.0  # seconds one PDF or .docx may take in the child before it is a skip
# C2 limits on Windows OCR of scanned pages (`_pdf_pages`): at most OCR_PAGES pages per file, and
# no page started past OCR_BUDGET seconds into the file, so a long scan returns the pages read
# so far well inside PARSE_TIMEOUT (unchanged) instead of being killed with nothing
OCR_PAGES, OCR_BUDGET = 100, 20.0
OCR_LINE = "Read with Windows OCR: check names and numbers."  # an OCR'd document page's mark
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_OLE = b"\xd0\xcf\x11\xe0"  # a password-protected .docx is an OLE container, not a zip
# manifest file name -> stack; only names are looked at, never their contents
STACKS = {"pyproject.toml": "Python", "setup.py": "Python", "requirements.txt": "Python",
          "package.json": "JavaScript", "tsconfig.json": "TypeScript", "deno.json": "Deno",
          "Cargo.toml": "Rust", "go.mod": "Go", "pom.xml": "Java", "build.gradle": "Java",
          "build.gradle.kts": "Kotlin", "Gemfile": "Ruby", "composer.json": "PHP",
          "CMakeLists.txt": "C/C++", "pubspec.yaml": "Dart", "Package.swift": "Swift",
          "mix.exs": "Elixir", "stack.yaml": "Haskell", "build.sbt": "Scala"}
STACK_SUFFIXES = {".csproj": "C#", ".sln": ".NET", ".fsproj": "F#"}
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
# Kindle's "Added on Monday, 3 March 2025 10:00:00" or "Added on Monday, March 3, 2025 …"
_KDATE = re.compile(r"(?:(\d{1,2}) ([A-Za-z]+)|([A-Za-z]+) (\d{1,2}),) (\d{4})")


def _day(ts: float) -> str:
    """A timestamp's Keepfold day (local, 04:00 rollover), as importers.note_item dates notes."""
    return importers._day(ts)


def _clip(text: str, n: int) -> str:
    """The first ~n characters, cut at a space."""
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + " …"


def _origin(path: Path, name: str) -> str:
    return str(path / name) if path.is_dir() else str(path)


# ---- docs ----

def _pdf(data: bytes) -> tuple[str, str, list[str], int, int | None] | str:
    """`_pdf_pages` read up to DOC_FULL_CHARS: the documents import's PDF parse."""
    return _pdf_pages(data, DOC_FULL_CHARS)


def _paged(pages: list[str], total: int) -> tuple[str, str]:
    """(a PDF page's text, the text as one line): each page with text on one line under a
    `## Page N` heading (an empty page is skipped, the numbers stay true), whole pages up to
    DOC_FULL_CHARS (a first page longer than that is clipped), and KEPT's line first when pages
    were left out here or by `_pdf_pages` (its char or OCR limits)."""
    flat = [(n, " ".join(p.split())) for n, p in enumerate(pages, 1)]
    flat = [(n, p) for n, p in flat if p]
    parts, size, kept = [], 0, len(pages)
    for n, p in flat:
        part = f"## Page {n}\n{p}"
        if size + len(part) > DOC_FULL_CHARS:
            kept = n - 1 if parts else n
            parts = parts or [_clip(part, DOC_FULL_CHARS)]
            break
        parts.append(part)
        size += len(part) + 2
    head = [KEPT.format(kept, total)] if kept < total else []
    return "\n\n".join(head + parts), " ".join(p for _, p in flat)


@functools.cache
def _ocr_engine():
    """C2: this computer's offline OCR as (kind, what it needs), or None: ("windows", Windows'
    OcrEngine in the person's own languages), ("vision", None) on macOS when pyobjc's Vision
    imports, ("tesseract", "eng+hin") on Linux (`_tesseract_langs`). The parse child OCRs; the
    server asks only after a page was OCR'd, to name the engine (`ocr_line`). Nothing leaves
    the computer."""
    if sys.platform == "win32":
        try:
            from winrt.windows.media.ocr import OcrEngine
            engine = OcrEngine.try_create_from_user_profile_languages()
        except Exception:  # noqa: BLE001 - an import or WinRT failure: no OCR, as before C2
            return None
        return None if engine is None else ("windows", engine)
    if sys.platform == "darwin":
        try:
            import Vision  # noqa: F401
        except Exception:  # noqa: BLE001 - pyobjc missing or broken: no OCR
            return None
        return ("vision", None)
    if sys.platform.startswith("linux"):
        langs = _tesseract_langs()
        return ("tesseract", langs) if langs else None
    return None


def _tesseract_langs() -> str | None:
    """Tesseract's installed languages but osd (script detection) as "eng+hin", or None (no
    tesseract, or no language). Tests replace this."""
    out = _run_ocr(["tesseract", "--list-langs"], b"")
    langs = [ln.strip() for ln in (out or "").splitlines()[1:] if ln.strip() not in ("", "osd")]
    return "+".join(langs) or None


def _run_ocr(args: list[str], data: bytes) -> str | None:
    """An OCR program's stdout with `data` on stdin, or None when it is missing, fails or runs
    past 60 s. Tests replace this: they never run a real tesseract."""
    try:
        got = subprocess.run(args, input=data, capture_output=True, timeout=60,
                             creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    return got.stdout.decode("utf-8", "replace") if got.returncode == 0 else None


def ocr_line(kind: str) -> str:
    """The mark on a page read by the `kind` engine (an `_ocr_engine` kind)."""
    return {"vision": "Read with macOS text recognition: check names and numbers.",
            "tesseract": "Read with Tesseract OCR: check names and numbers."}.get(kind, OCR_LINE)


def _ocr(engine, page) -> str:
    """C2: the text OCR reads in `page`'s images, a line per line read, in reading order; ""
    when the page fails, so that one page is skipped, not the file. `engine` is
    `_ocr_engine`'s (kind, what it needs); Windows is the kind otherwise."""
    import asyncio
    try:
        images = [im.image for im in page.images]
        if engine[0] == "vision":
            return _vision_text(images)
        if engine[0] == "tesseract":
            return _tesseract_text(images, engine[1])
        return asyncio.run(_recognize(engine[1], images))
    except Exception:  # noqa: BLE001 - an image pypdf or the OCR cannot decode, a WinRT error
        return ""


def _png(im) -> bytes:
    out = io.BytesIO()
    im.convert("RGB").save(out, "PNG")  # a mode PNG holds (CMYK, palette...), lossless
    return out.getvalue()


def _tesseract_text(images, langs: str) -> str:
    """Tesseract's lines for each image, blank lines left out; an image it fails on reads
    nothing."""
    lines = []
    for im in images:
        out = _run_ocr(["tesseract", "stdin", "stdout", "-l", langs], _png(im)) or ""
        lines += [ln.strip() for ln in out.splitlines() if ln.strip()]
    return "\n".join(lines)


def _vision_text(images) -> str:
    """macOS Vision's text in each image, a line per observation, the language found by
    itself. Checked on a Mac (P3), not in the tests."""
    import Vision
    from Foundation import NSData
    lines = []
    for im in images:
        data = _png(im)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setAutomaticallyDetectsLanguage_(True)
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(
            NSData.dataWithBytes_length_(data, len(data)), None)
        ok, err = handler.performRequests_error_([req], None)
        if not ok:
            raise OSError(str(err))
        for obs in req.results() or []:
            if top := obs.topCandidates_(1):
                lines.append(top[0].string())
    return "\n".join(lines)


async def _recognize(engine, images) -> str:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import Buffer, InMemoryRandomAccessStream
    most, lines = OcrEngine.max_image_dimension, []
    for im in images:
        im = im.convert("RGB")  # a fresh copy in a mode BMP holds (CMYK, 1-bit, palette...)
        im.thumbnail((most, most))  # only ever scales down, keeping the aspect
        out = io.BytesIO()
        im.save(out, "BMP")  # uncompressed: the quickest for Windows to decode
        data = out.getvalue()
        buf = Buffer(len(data))  # in memory only: nothing is written to disk
        buf.length = len(data)
        memoryview(buf)[:] = data
        stream = InMemoryRandomAccessStream()
        await stream.write_async(buf)
        stream.seek(0)
        bmp = await (await BitmapDecoder.create_async(stream)).get_software_bitmap_async()
        lines += [ln.text for ln in (await engine.recognize_async(bmp)).lines]
    return "\n".join(lines)


def _pdf_pages(data: bytes, limit: int) -> tuple[str, str, list[str], int, int | None] | str:
    """(title, YYYY-MM-DD or "", each page's text up to about `limit` characters in all, the
    page count, ocr) or a skip reason. The app's Chat reads an attached PDF this way (askfile).

    C2: a page with no text but with images (a scan) is read with Windows OCR (`_ocr`); a page
    with some text is never OCR'd. `ocr` is how many pages OCR read text from, or None when no
    page has text and this computer has no OCR (`_ocr_engine`): then nothing changes from
    before C2. Limits: at most OCR_PAGES pages are OCR'd per file, and none is started past
    OCR_BUDGET seconds; at either the reading stops there, so `pages` is shorter than the page
    count (askfile says where it stopped)."""
    from pypdf import PdfReader
    start = time.monotonic()
    try:
        rd = PdfReader(io.BytesIO(data))
        if rd.is_encrypted:
            try:  # many "encrypted" PDFs open with an empty password
                if not rd.decrypt(""):
                    return "encrypted"
            except Exception:  # noqa: BLE001 - AES needs a crypto library pypdf may lack
                return "encrypted"
        pages, n, tried, read = [], 0, 0, 0
        for page in rd.pages:
            text = page.extract_text() or ""
            if not text.strip() and _has_images(page) and (engine := _ocr_engine()) is not None:
                if tried >= OCR_PAGES or time.monotonic() - start > OCR_BUDGET:
                    break
                tried += 1
                text = _ocr(engine, page)
                read += bool(text.strip())
            pages.append(text)
            n += 1 + len(pages[-1])
            if n > limit:
                break
        total = len(rd.pages)
        meta = rd.metadata
        title = str(meta.title or "").strip() if meta else ""
        try:
            made = meta.creation_date.date().isoformat() if meta and meta.creation_date else ""
        except Exception:  # noqa: BLE001 - a malformed date string
            made = ""
    except Exception:  # noqa: BLE001 - pypdf raises many types on a damaged file
        return "unreadable"
    # the engine is only asked for when no page has text, so a text PDF never loads it
    ocr = read if any(p.strip() for p in pages) or _ocr_engine() is not None else None
    return title, made, pages, total, ocr


def _has_images(page) -> bool:
    try:
        return len(page.images) > 0
    except Exception:  # noqa: BLE001 - a damaged resource: no OCR, the page as before C2
        return False


def _docx(data: bytes) -> tuple[str, str, str] | str:
    if data[:4] == _OLE:
        return "encrypted"
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        info = z.getinfo("word/document.xml")
        if info.file_size > importers.MAX_ENTRY:
            return "over 200 MB"
        doc = ElementTree.fromstring(z.read(info))
        paras = (" ".join("".join(t.text or "" for t in p.iter(_W + "t")).split())
                 for p in doc.iter(_W + "p"))
        title = ""
        if "docProps/core.xml" in z.namelist():
            t = ElementTree.fromstring(z.read("docProps/core.xml")).find(_DC + "title")
            title = (t.text or "").strip() if t is not None else ""
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, OSError):
        return "unreadable"
    return title, "", "\n".join(p for p in paras if p)


PAGES_CHARS = 2_400_000  # a "pdfpages" parse stops past this many characters (askfile's cap)


def _parse_child() -> None:
    """The parse child's loop: `<kind> <n>\\n` and n bytes in on stdin, one JSON line out (the
    `_pdf` / `_pdf_pages` / `_docx` result for kind pdf / pdfpages / docx) per document, until
    stdin closes."""
    inp, out = sys.stdin.buffer, sys.stdout
    while head := inp.readline():
        kind, n = head.split()
        data = inp.read(int(n))
        out.write(json.dumps(_pdf(data) if kind == b"pdf" else
                             _pdf_pages(data, PAGES_CHARS) if kind == b"pdfpages" else
                             _docx(data)) + "\n")
        out.flush()


def _child_argv() -> list[str]:
    """The parse child's command line. A frozen Mac/Linux app takes no `-c`, so it starts its
    own CLI (next to `sys.executable` in the onedir folder) with the hidden `_parse-child`."""
    if getattr(sys, "frozen", False):
        from keepfold.app.bundle import CLI
        name = CLI if sys.platform == "darwin" else "keepfold"
        return [str(Path(sys.executable).with_name(name)), "_parse-child"]
    return [sys.executable, "-c", "from keepfold.more_sources import _parse_child; _parse_child()"]


class _Parser:
    """RR-37: PDF and .docx parsing in a child process (`_child_argv`), so a hostile file that
    hangs or crashes pypdf is one "parse timed out" (or "unreadable") skip after
    PARSE_TIMEOUT, not a hung import. One child serves the whole walk; a timeout kills it and
    the next document starts a new one."""

    def __init__(self):
        self.proc = None

    def __call__(self, kind: str, data: bytes) -> tuple[str, str, str] | str:
        if self.proc is None:
            self.proc = subprocess.Popen(_child_argv(), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         creationflags=NO_WINDOW)
            self.lines = queue.Queue()
            threading.Thread(target=self._pump, args=(self.proc.stdout, self.lines),
                             daemon=True).start()
        try:
            self.proc.stdin.write(f"{kind} {len(data)}\n".encode() + data)
            self.proc.stdin.flush()
            line = self.lines.get(timeout=PARSE_TIMEOUT)
        except queue.Empty:
            self.close()
            return "parse timed out"
        except OSError:  # the child died while reading
            line = None
        if line is None:
            self.close()
            return "unreadable"
        got = json.loads(line)
        return got if isinstance(got, str) else tuple(got)

    @staticmethod
    def _pump(stream, lines: queue.Queue) -> None:
        try:
            for line in stream:
                lines.put(line)
        except (OSError, ValueError):  # closed by `close` after a kill
            pass
        lines.put(None)  # end of output: the child exited

    def close(self) -> None:
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait()
            for f in (self.proc.stdin, self.proc.stdout):
                f.close()
            self.proc = None


def _fold(name: str) -> str:
    return name.casefold() if FOLD_CASE else name


def skip_folder(rel: str, rules: tuple[str, ...] = ()) -> bool:
    """Whether the folder `rel` (relative to the import, "/"-separated) is left out: any hidden
    folder or SKIP_DIRS name in it, or a `rules` entry ("Archive", "School/Old") matching whole
    folder names in a row, so "Archive" never matches "Archived"; case-folded on Windows."""
    parts = [_fold(p) for p in rel.replace("\\", "/").split("/") if p and p != "."]
    if any(p.startswith(".") or p in SKIP_DIRS for p in parts):
        return True
    for rule in rules:
        seg = [_fold(p) for p in rule.replace("\\", "/").split("/") if p]
        if seg and any(parts[i:i + len(seg)] == seg for i in range(len(parts))):
            return True
    return False


def docs_source(path: Path, skip: tuple[str, ...] = (), files: list | None = None
                ) -> sources.Source:
    """One page per document in the folder (or the one file); see the module docstring. The
    walk leaves out `skip_folder`'s folders (`skip`: more folder rules) and shortcut files.
    `files`: `importers._files` entries already walked (a watched folder's changed ones)."""
    path = Path(path)
    if files is None:
        files = importers._files(path, skip_dir=lambda rel: skip_folder(rel, skip))

    def items():
        parse = _Parser()
        try:
            yield from _doc_items(path, files, skip, parse)
        finally:
            parse.close()

    return _docs(items)


def _docs(items) -> sources.Source:
    """R17: `updates` (keyed by the text the import kept before), so a page imported clipped
    gets its full text, unless the owner edited it (`keeps_edits`)."""
    return sources.Source("docs", "Documents", "folder", items, skips=DOC_SKIPS, updates=True,
                          keeps_edits=True, not_summary=NOT_SUMMARY)


def doc_source(name: str, size: int, mtime: float, read, parse) -> sources.Source:
    """The documents import of one file already read (the app Chat's Save to your brain,
    askfile): `read` gives its bytes, `parse` stands in for the child (`_Parser`'s result)."""
    return _docs(lambda: _doc_items(Path(name), [(name, size, mtime, read)], (), parse))


def _doc_items(path: Path, files: list, skip: tuple[str, ...], parse: _Parser):
    for name, size, mtime, read in files:
        low, folder = name.lower(), PurePosixPath(name).parent.as_posix()
        if PurePosixPath(name).name.startswith(".") or skip_folder(folder, skip):
            yield "not a document"  # a hidden file, or a skipped folder inside a .zip
            continue
        if low.endswith(POINTERS):
            yield "a shortcut, not the file"
            continue
        if not low.endswith(DOC_SUFFIXES + importers.NOTE_SUFFIXES):
            yield "not a document"
            continue
        if low.endswith(importers.NOTE_SUFFIXES):
            yield importers.note_item(name, size, mtime, read)
            continue
        try:
            data = read() if size <= importers.MAX_ENTRY else None
        except VaultError:  # past the whole import's cap
            data = None
        if data is None:
            yield "over 200 MB"
            continue
        got = parse("pdf" if low.endswith(".pdf") else "docx", data)
        if isinstance(got, str):
            yield got
            continue
        if isinstance(got[2], list):  # a PDF: `_pdf_pages`' pages
            title, made, pages, total, ocr = got
            text, flat = _paged(pages, total)
        else:
            (title, made, flat), ocr = got, 0
            text = _clip(flat, DOC_FULL_CHARS)
        if not text.strip():
            yield "scanned or no text"
            continue
        # last: the first body line is the page's summary; the engine is asked only after OCR ran
        mark = "\n\n" + ocr_line((_ocr_engine() or ("windows",))[0]) if ocr else ""
        # the key is the text the import kept before R17, so its page is found and refilled
        yield sources.Item(title or PurePosixPath(name).stem, made or _day(mtime), text + mark,
                           _origin(path, name), key=_clip(flat, DOC_CHARS) + mark)


# ---- repos ----

def _is_repo(d: Path) -> bool:
    return (d / ".git").exists()


def _subdirs(d: Path) -> list[Path]:
    try:
        return sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []


def find_repos(folder: Path) -> list[Path]:
    """The folder itself when it is a repo, else the repos one or two folders down."""
    folder = Path(folder)
    if _is_repo(folder):
        return [folder]
    out = []
    for d in _subdirs(folder):
        out += [d] if _is_repo(d) else [e for e in _subdirs(d) if _is_repo(e)]
    return out


def holds_repos(path: Path) -> bool:
    """For `importers.detect`: a folder (not an Obsidian vault) with repos one or two down."""
    path = Path(path)
    return path.is_dir() and not (path / ".obsidian").exists() and not _is_repo(path) \
        and bool(find_repos(path))


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), "-c", "log.showSignature=false", *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=60, creationflags=NO_WINDOW)


def _stack(names: list[str]) -> list[str]:
    got = [STACKS[n] for n in names if n in STACKS] + \
        [s for n in names for x, s in STACK_SUFFIXES.items() if n.endswith(x)]
    return list(dict.fromkeys(got))


def _readme(repo: Path, names: list[str]) -> str:
    for n in sorted(names):
        if n.lower().startswith("readme") and (repo / n).is_file():
            with (repo / n).open("rb") as f:
                return _clip(f.read(4 * README_CHARS).decode("utf-8-sig", "replace").strip(),
                             README_CHARS)
    return ""


def decision_word(subject: str) -> str | None:
    """The signal word that makes a commit subject look like a decision ("switched to",
    "replace … with"), or None; subjects starting with merge, bump, release, wip, fixup, typo,
    lint, format or revert never are."""
    if _NOT_DECISION.match(subject) or not (m := _DECIDES.search(subject)):
        return None
    w = " ".join(m.group(1).lower().split())
    return re.sub(r"^(replaced?) .* with$", r"\1 … with", w)


def repo_item(repo: Path) -> sources.Item | str:
    """One repo's Item, or why it is skipped. Reads git's log subjects, hashes and dates, the
    root's file names and the README; nothing else. Of the last MINED subjects, those
    `decision_word` picks are listed under `## DECISIONS` (date, hash, word, subject) for the
    Review to offer (`decision_proposals`)."""
    try:
        email = (_git(repo, "config", "user.email").stdout or "").strip()
        if not email:
            return "no git user.email"
        log = _git(repo, "log", "--fixed-strings", f"--author={email}", "--date=short",
                   "--format=%ad%x09%H%x09%s")
    except (OSError, subprocess.SubprocessError):
        return "git could not read it"
    if log.stdout is None:  # captured output is a str; anything else is a broken git run
        return "git could not read it"
    rows = [r for ln in log.stdout.splitlines() if len(r := ln.split("\t", 2)) == 3] \
        if log.returncode == 0 else []
    if not rows:
        return "no commits by you"
    names = sorted(os.listdir(repo), key=str.upper)  # NTFS's order; ext4 lists in hash order
    stack, readme = _stack(names), _readme(repo, names)
    first, last = rows[-1][0], rows[0][0]
    n = len(rows)
    head = (f"Git repository {repo.name}" + (f" ({', '.join(stack)})" if stack else "")
            + f": {n:,} commit{'s' * (n != 1)} by me, from {first} to {last}.")
    hits = [f"- {d} {h[:12]} ({w}): {s}" for d, h, s in rows[:MINED] if (w := decision_word(s))]
    text = head + (f"\n\n## README\n{readme}" if readme else "") + \
        f"\n\n## My last {min(n, SUBJECTS)} commits\n" + \
        "\n".join(f"- {d} {s}" for d, _, s in rows[:SUBJECTS]) + \
        (f"\n\n## {DECISIONS}\n" + "\n".join(hits) if hits else "")
    return sources.Item(repo.name, last, text, str(repo), key=f"repo {repo.resolve()}")


def decision_proposals(vault, events) -> list[dict]:
    """RR-9: up to SHOWN_DECISIONS commit subjects from the repo pages' `## DECISIONS`, newest
    first, as {key, page, project, date, hash, word, subject, private} for the app's Review.
    The key hashes the repo page and the commit, so a re-import never offers one again; left
    out are those the owner approved (a `decide` change) or dismissed (a `dismiss` change), both
    on id `decisions` and holding the key. `private` follows the repo page (an unreadable page
    counts as private), and Approve makes a private page's decision private too."""
    from keepfold import review
    events = list(events)
    voided = review._voided(events)
    done = {c.get("hash") for e in events if e.get("kind") == "change"
            and e.get("id") not in voided and isinstance(c := review._payload(e).get("change"),
                                                          dict)
            and c.get("tool") in ("dismiss", "decide") and c.get("id") == "decisions"}
    out = []
    for p in sorted((vault.root / "resources").glob("repo-*.md")):
        try:
            note = vault.read_path(p)
        except VaultError:
            continue
        if not re.search(r"^Source: Git · ", note.body, re.M):
            continue
        private, project = is_private(note), str(note.fields.get("title") or p.stem)
        section = note.body.partition(f"\n## {DECISIONS}\n")[2].split("\n## ", 1)[0]
        for m in map(_HIT.match, section.splitlines()):
            key = review.text_hash(f"{p.stem} {m.group(2)}") if m else None
            if m and key not in done:
                out.append({"key": key, "page": p.stem, "project": project, "date": m.group(1),
                            "hash": m.group(2), "word": m.group(3), "subject": m.group(4),
                            "private": private})
    out.sort(key=lambda x: (x["date"], x["project"], x["hash"]), reverse=True)
    return out[:SHOWN_DECISIONS]


def repos_source(path: Path, repos: list[Path] | None = None) -> sources.Source:
    """One page per repo `find_repos` finds (or per one of `repos`, a watched folder's changed
    ones); see the module docstring."""
    path = Path(path)
    if not path.is_dir():
        raise VaultError("invalid_argument", f"{path.name} is not a folder of git projects.")
    if repos is None and not (repos := find_repos(path)):
        raise VaultError("invalid_argument", f"Found no git repository in {path.name} or one "
                         "or two folders below it.")
    return sources.Source("repos", "Git", "folder", lambda: map(repo_item, repos),
                          prefix="repo", drops_code=True, updates=True,
                          skips=("no git user.email", "no commits by you",
                                 "git could not read it"))


# ---- highlights ----

def highlight_format(name: str, read) -> str | None:
    """"kindle", "readwise", "pocket" or None, from a .txt or .csv file's first bytes."""
    low = name.lower()
    if not low.endswith((".txt", ".csv")):
        return None
    head = read(4096).decode("utf-8-sig", "replace")
    if low.endswith(".txt"):
        return "kindle" if "\n==========" in head else None
    cols = {c.strip().lower() for c in next(csv.reader([head.split("\n", 1)[0]]), [])}
    return "readwise" if {"highlight", "book title"} <= cols else \
        "pocket" if {"title", "url", "time_added"} <= cols else None


def _kindle_day(meta: str) -> str:
    m = _KDATE.search(meta.split("|")[-1])
    if not m:
        return ""
    day, month = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
    try:
        return datetime(int(m.group(5)), _MONTHS[month.lower()], int(day)).date().isoformat()
    except (KeyError, ValueError):
        return ""


def _kindle(text: str):
    """(title, author, day or "", quote, is a note) per clipping, or a skip reason."""
    for entry in text.split("=========="):
        lines = [x.strip().lstrip("﻿") for x in entry.strip().splitlines()]
        if not any(lines):
            continue
        if len(lines) < 3 or not "".join(lines[2:]).strip():
            yield "bookmark or empty"
            continue
        m = re.match(r"(.*?)\s*\(([^()]*)\)$", lines[0])
        title, author = (m.group(1), m.group(2)) if m and m.group(1) else (lines[0], "")
        yield (title, author, _kindle_day(lines[1]), " ".join(" ".join(lines[2:]).split()),
               "note" in lines[1].lower().split("|")[0])


def _readwise(text: str):
    for row in csv.DictReader(io.StringIO(text)):
        quote = " ".join((row.get("Highlight") or "").split())
        if not quote:
            yield "bookmark or empty"
            continue
        day = (row.get("Highlighted at") or "")[:10]
        note = " ".join((row.get("Note") or "").split())
        yield ((row.get("Book Title") or "").strip() or "(untitled)",
               (row.get("Book Author") or "").strip(),
               day if re.fullmatch(r"\d{4}-\d\d-\d\d", day) else "",
               quote + (f" (my note: {note})" if note else ""), False)


def _book_items(app: str, origin: str, mtime: float, rows) -> list:
    """Items per book (per book and month past MANY highlights), and skip reasons."""
    books, out = defaultdict(list), []
    for r in rows:
        if isinstance(r, str):
            out.append(r)
        else:
            books[r[0], r[1]].append(r[2:])
    for (title, author), hs in books.items():
        groups = defaultdict(list)
        for h in hs:
            groups[h[0][:7] if len(hs) > MANY else ""].append(h)
        for i, (month, g) in enumerate(sorted(groups.items())):
            days = sorted(d for d, *_ in g if d)
            span = f", {days[0]} to {days[-1]}" if days else ""
            name = f"{title} ({month or 'undated'})" if len(groups) > 1 else title
            by = f" by {author}" if author else ""
            head = (f"My {app} highlights from \"{title}\"{by}" + (f" in {month}" if month else "")
                    + f" ({len(g)} highlight{'s' * (len(g) != 1)}{span}).")
            body = "\n".join(f"- {d + ': ' if d else ''}" + (f"My note: {q}" if note else f'"{q}"')
                             for d, q, note in g)
            out.append(sources.Item(name, days[-1] if days else _day(mtime),
                                    f"{head}\n\n{body}", origin,
                                    # the first part keeps the whole book's key: a book that
                                    # grows past MANY rewrites its page, never leaves it stale
                                    key=f"highlights {app} {title} {author} {month * bool(i)}",
                                    who=author))
    return out


def _pocket(text: str):
    for row in csv.DictReader(io.StringIO(text)):
        try:
            day = _day(int(row.get("time_added") or ""))
        except (ValueError, OverflowError, OSError):
            yield "no readable date"
            continue
        title = " ".join((row.get("title") or "").split()) or row.get("url") or "(untitled)"
        tags = (row.get("tags") or "").replace("|", ", ")
        yield sources.Item(title, day, f"{day} saved \"{title}\" ({row.get('url', '')})"
                           + (f", tagged {tags}" if tags else ""))


def highlights_source(path: Path) -> sources.Source:
    """Kindle and Readwise: item pages per book; Pocket alone: month pages of saved articles.
    A Pocket list beside Kindle or Readwise files is skipped (import it on its own)."""
    path = Path(path)
    found = [(f, k) for f in importers._files(path) if (k := highlight_format(f[0], f[3]))]
    if not found:
        raise VaultError("invalid_argument", f"Found no Kindle clippings, Readwise or Pocket "
                         f"export in {path.name}.")
    pocket = all(k == "pocket" for _, k in found)
    skips = ("bookmark or empty", "no readable date", "over 200 MB",
             "a Pocket list (import it on its own)")

    def items():
        for (name, _size, mtime, read), kind in found:
            if kind == "pocket" and not pocket:
                yield skips[3]
                continue
            try:  # My Clippings.txt is a .txt but not a note: an export's cap, not a note's
                text = read(cap=importers.MAX_ENTRY).decode("utf-8-sig", "replace")
            except VaultError:
                yield "over 200 MB"
                continue
            if kind == "pocket":
                yield from _pocket(text)
                continue
            app, rows = ("Kindle", _kindle(text)) if kind == "kindle" else \
                ("Readwise", _readwise(text))
            yield from _book_items(app, _origin(path, name), mtime, rows)

    if pocket:
        return sources.Source("highlights", "Pocket", "file", items, page="month",
                              prefix="pocket", noun="saved articles", skips=skips)
    return sources.Source("highlights", "Highlights", "file", items, updates=True,
                          skips=skips)


def zotero_source(path: Path | None) -> sources.Source:
    """R18: a Zotero library (keepfold.zotero); with no path, the one on this computer."""
    from keepfold import zotero  # it imports this module
    return zotero.source(path)


def anki_source(path: Path) -> sources.Source:
    """R22: Anki decks (keepfold.anki); the caller runs `anki.link` after the import."""
    from keepfold import anki  # loaded only for an Anki import
    return anki.source(path)


SOURCE_OF = {"docs": docs_source, "repos": repos_source, "highlights": highlights_source,
             **life_sources.SOURCE_OF, "zotero": zotero_source,  # Sources B, then R18
             "anki": anki_source}
