"""Ask this file (C1): a PDF, .docx, .txt or .md the person attaches in the app's Chat.

The file lives only in the app server's memory (`Store`): a random id per file, at most
MAX_FILES at once (the oldest drops), gone on Remove, when the chat is cleared or after IDLE_S
unused. PDF and .docx are parsed in more_sources' sandboxed child (`_Parser`), never in the
server; .txt and .md are decoded as the notes import does. The text is cut into chunks of about
CHUNK_MIN-CHUNK_MAX characters on paragraph boundaries, each knowing its page (a PDF) or part
(the rest); past MAX_CHUNKS the rest of the file is left out and `cut` says where it stopped.

`rank` scores the chunks against a question with the search model Chat uses (bge-small through
`semantic`, embedded once per file) or, without it, a small in-memory BM25; `chat.reply` puts the
matching ones ahead of the vault's passages. The file's text is untrusted data: it reaches the
model wrapped as notes are (`ask.prompt`), and only a local model ever reads it.

Nothing is written until Save to your brain (`save`): the documents import of this one file
(`more_sources.doc_source`), at that source's tier, deduped by the import's own check."""

from __future__ import annotations

import math
import re
import secrets
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from cortex import taint
from cortex.index import STOPWORDS

if TYPE_CHECKING:
    import numpy as np

    from cortex.vault import Vault

# numpy, semantic and the importers are imported where used: the app's server makes a Store at
# start, and must not pay for them then
SUFFIXES = (".pdf", ".docx", ".txt", ".md")
# the documents import's own caps (importers.MAX_ENTRY, MAX_BYTES): 200 MB for a PDF or
# .docx, 5 MB for a note
LIMIT = {".pdf": 200 * 2**20, ".docx": 200 * 2**20, ".txt": 5 * 2**20, ".md": 5 * 2**20}
MAX_FILES = 3
IDLE_S = 2 * 3600
CHUNK_MIN, CHUNK_MAX, MAX_CHUNKS = 800, 1200, 2000
LEAD = 3  # file passages at most in one prompt, ahead of the vault's
SHOWN = 3  # passages shown when no local model is running
NO_MODEL = "No local model is running, so here are the passages that match."
LOCAL_ONLY = "Files are answered on this computer only."
NO_MATCH = "No passage in the file matches these words."
ORIGIN = "imported from an attached file"  # its From line: never the person's own words
SCAN = "This PDF has no text layer (it is a scan). Cortex can't read scans yet."  # not Windows
NO_OCR = ("This PDF is a scan, and Windows has no text-recognition language installed, so "
          "Cortex can't read it.")
WHY = {"encrypted": "This file is password-protected. Remove the password, then attach it "
                    "again.",
       "parse timed out": "Reading this file took too long, so Cortex stopped. Try a smaller "
                          "file.",
       "unreadable": "Cortex could not read this file; it may be damaged. Open it, save a copy, "
                     "and attach that.",
       "not UTF-8 text": "This file is not UTF-8 text. Save it as UTF-8, then attach it again.",
       "empty": "This file has no text to read."}
_WORD = re.compile(r"\w+")


@dataclass(eq=False)
class File:
    name: str
    unit: str  # "page" (a PDF) or "part"
    count: int  # pages in the PDF, or parts (chunks) in the file
    chunks: list  # [(page or part number, text)]
    cut: int  # 0, or the last page or part read when the file was cut at MAX_CHUNKS
    size: int
    mtime: float
    data: bytes  # .txt and .md: the bytes the notes import reads; else b""
    parsed: tuple | None  # PDF and .docx: the child's parse, as the documents import takes it
    scan: bool = False  # C2: a PDF read with Windows OCR; its line says "(read from a scan)"
    id: str = ""
    used: float = 0.0
    vecs: np.ndarray | None = None
    tf: list | None = None  # BM25's term counts per chunk, made on the first keyword search
    saved: dict | None = None  # {title, path} once Save to your brain made (or found) its page
    told: bool = False  # LOCAL_ONLY was said for it

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "unit": self.unit, "count": self.count,
                "cut": self.cut, "saved": self.saved, "scan": self.scan}


def _paras(text: str):
    """Paragraphs of at most CHUNK_MAX characters: blank-line blocks, a long one by its lines,
    a long line cut at a space."""
    for block in re.split(r"\n\s*\n", text):
        for p in [block] if len(block) <= CHUNK_MAX else block.split("\n"):
            p = p.strip()
            while len(p) > CHUNK_MAX:
                cut = p.rfind(" ", CHUNK_MIN, CHUNK_MAX)
                cut = cut if cut > 0 else CHUNK_MAX
                yield p[:cut].rstrip()
                p = p[cut:].strip()
            if p:
                yield p


def chunk(pages: list[str]) -> list[tuple[int, str]]:
    """[(page number, text)]: each page's paragraphs joined up to CHUNK_MAX characters; a chunk
    never crosses a page."""
    out = []
    for n, page in enumerate(pages, 1):
        cur = ""
        for p in _paras(page.replace("\r\n", "\n")):
            if cur and len(cur) + 1 + len(p) > CHUNK_MAX:
                out.append((n, cur))
                cur = ""
            cur = f"{cur}\n{p}" if cur else p
        if cur:
            out.append((n, cur))
    return out


def safe_name(name) -> str:
    """The file name to show and cite: its last part, no control characters, at most 120."""
    name = PurePosixPath(str(name or "").replace("\\", "/")).name
    return re.sub(r"[\x00-\x1f\x7f]", "", name)[:120].strip() or "the file"


def read(path: Path, name: str) -> File:
    """Parse the uploaded file at `path` (the person's name for it: `name`); ValueError says
    what went wrong, in one sentence for the page."""
    from cortex import more_sources, semantic
    name = safe_name(name)
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in SUFFIXES:
        raise ValueError("Attach a .pdf, .docx, .txt or .md file.")
    st = path.stat()
    if st.st_size > LIMIT[suffix]:
        raise ValueError(f"This file is over {LIMIT[suffix] // 2**20} MB, the most Cortex reads "
                         f"from one {suffix} file.")
    raw = path.read_bytes()
    data, parsed, total, ocr = b"", None, 0, 0
    if suffix in (".pdf", ".docx"):
        parse = more_sources._Parser()
        try:
            got = parse("pdfpages" if suffix == ".pdf" else "docx", raw)
        finally:
            parse.close()
        if isinstance(got, str):
            raise ValueError(WHY.get(got, WHY["unreadable"]))
        parsed = tuple(got)  # Save hands it to the import as the child's parse (more_sources._pdf)
        if suffix == ".pdf":
            _, _, pages, total, ocr = got
        else:
            pages = [got[2]]
        if not "".join(pages).strip():
            # C2: ocr None is a PDF with no text and no OCR in the child (as before C2); a number
            # means Windows OCR ran and read nothing
            raise ValueError(SCAN if suffix == ".pdf" and sys.platform != "win32" else
                             NO_OCR if suffix == ".pdf" and ocr is None else WHY["empty"])
    else:
        try:
            text = raw.decode("utf-8-sig") if b"\0" not in raw else None
        except UnicodeDecodeError:
            text = None
        if text is None:
            raise ValueError(WHY["not UTF-8 text"])
        if not text.strip():
            raise ValueError(WHY["empty"])
        data, pages = raw, [text]
    chunks = chunk(pages)
    if suffix != ".pdf":
        chunks = [(i, t) for i, (_, t) in enumerate(chunks, 1)]
        total = len(chunks)
    cut = 0
    if len(chunks) > MAX_CHUNKS:
        chunks = chunks[:MAX_CHUNKS]
        cut = chunks[-1][0]
    elif suffix == ".pdf" and len(pages) < total:  # the child stopped at PAGES_CHARS
        cut = len(pages)
    f = File(name, "page" if suffix == ".pdf" else "part", total, chunks, cut, st.st_size,
             st.st_mtime, data, parsed, scan=bool(ocr))
    if (embed := semantic.load_embedder()) is not None:
        f.vecs = semantic.embed_all(embed, [t for _, t in chunks])
    return f


class Store:
    """The attached files, in this process's memory only."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.lock, self.files = clock, threading.Lock(), {}

    def _expire(self) -> None:
        now = self.clock()
        for i in [i for i, f in self.files.items() if now - f.used > IDLE_S]:
            del self.files[i]

    def add(self, f: File) -> File:
        with self.lock:
            self._expire()
            f.id, f.used = secrets.token_hex(8), self.clock()
            self.files[f.id] = f
            while len(self.files) > MAX_FILES:  # the oldest attached drops
                self.files.pop(next(iter(self.files)))
        return f

    def get(self, ids) -> list[File]:
        """The files of `ids` still held, each marked used now; anything else is left out."""
        with self.lock:
            self._expire()
            ids = ids if isinstance(ids, list) else []
            out = [self.files[i] for i in dict.fromkeys(x for x in ids if isinstance(x, str))
                   if i in self.files]
            for f in out:
                f.used = self.clock()
        return out

    def remove(self, id) -> bool:
        with self.lock:
            return self.files.pop(id, None) is not None if isinstance(id, str) else False

    def clear(self) -> None:
        with self.lock:
            self.files.clear()

    def list(self) -> list[dict]:
        with self.lock:
            self._expire()
            return [f.info() for f in self.files.values()]


# ---- ranking ---------------------------------------------------------------------------------

def _terms(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in STOPWORDS]


def _bm25(f: File, terms: list[str], k1: float = 1.2, b: float = 0.75) -> list[float]:
    if f.tf is None:
        f.tf = [Counter(_terms(t)) for _, t in f.chunks]
    df = Counter(w for c in f.tf for w in c)
    n = len(f.tf)
    avg = sum(sum(c.values()) for c in f.tf) / n or 1.0
    out = []
    for c in f.tf:
        size, s = sum(c.values()), 0.0
        for w in set(terms):
            if tf := c.get(w):
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * size / avg))
        out.append(s)
    return out


def rank(files: list[File], question: str) -> list[tuple[float, bool, File, int, str]]:
    """[(score, matches, file, page or part, text)] of every chunk scoring above 0, best first:
    the search model's cosine (`matches` from semantic.MIN_COS up), else BM25 (`matches` when
    any word is shared)."""
    from cortex import semantic
    embed = semantic.load_embedder()
    q = semantic._embed(embed, [semantic.QUERY_PREFIX + question.strip()])[0] \
        if embed is not None else None
    terms = _terms(question)
    out = []
    for f in files:
        if q is not None:
            if f.vecs is None or f.vecs.shape[1] != len(q):  # attached before the model came
                f.vecs = semantic.embed_all(embed, [t for _, t in f.chunks])
            scores = [(float(s), bool(s >= semantic.MIN_COS)) for s in f.vecs @ q]
        else:
            scores = [(s, s > 0) for s in _bm25(f, terms)]
        out += [(s, ok, f, n, t) for (s, ok), (n, t) in zip(scores, f.chunks, strict=True)
                if s > 0]
    return sorted(out, key=lambda x: -x[0])


def label(f: File, n: int) -> str:
    return f"{f.name}, {f.unit} {n}"


def passage(f: File, n: int, text: str) -> dict:
    """A chunk in `ask._passages`' shape, for `ask.prompt` and the sources (kind "file")."""
    return {"path": "", "title": label(f, n), "text": text, "kind": "file", "day": "",
            "origin": ORIGIN, "at": 0, "len": len(text)}


def cloud_set(config: dict | None = None) -> bool:
    """Whether Chat's model setting names a cloud model (localllm refuses it)."""
    from cortex import localllm
    cfg = (taint.config() if config is None else config).get("localllm")
    return isinstance(cfg, dict) and localllm._cloud(str(cfg.get("model") or ""))


# ---- Save to your brain ----------------------------------------------------------------------

def save(vault: Vault, f: File) -> dict:
    """Import `f` as the documents import does (one page, its tier: the last choice or the
    default) and return {title, path} of its page, the one already there when the same text was
    imported before (the import's own dedupe). The caller commits. ValueError when nothing was
    saved, saying why."""
    from cortex import more_sources, sources
    src = more_sources.doc_source(f.name, f.size, f.mtime, lambda *a, **k: f.data,
                                  lambda kind, data: f.parsed)
    t = sources.pick_tier(vault, src)[0]
    if t is sources.Audience.SKIP:
        raise ValueError("Documents are set to Don't save, so nothing was saved. Change that on "
                         "the Documents card under Add your history.")
    items = list(src.items())
    src.items = lambda: iter(items)
    r = sources.run(vault, src, t)
    known = sources._known_hashes(vault)
    for it in items:
        if not isinstance(it, str) and (p := known.get(sources._sha(it.key or it.text))):
            note = vault.read_path(p)
            out = {"title": str(note.fields.get("title") or p.stem),
                   "path": p.relative_to(vault.root).as_posix()}
            f.saved = out
            return out
    why = [*(k for k, v in r["reasons"].items() if v), *r["unreadable"],
           *(x for x in items if isinstance(x, str))]
    raise ValueError("Cortex did not save this file" + (f" ({why[0]})." if why else "."))
