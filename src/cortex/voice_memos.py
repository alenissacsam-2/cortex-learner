"""Sources B, voice memos (RR-30, v9 item 14 part B): a folder of audio files (a phone's voice
recorder, WhatsApp voice notes copied out) into one private page per memo, transcribed on this
computer by faster-whisper (CPU, int8, model `MODEL`). Never sent anywhere; the audio is never
copied into the vault.

faster-whisper is optional (the `voice` extra): `cortex model download --voice` installs it
(pinned, `PIN`) and downloads the model into ~/.cortex/models, and is the only place either is
fetched. Without them an import refuses with `NEEDS`. faster-whisper decodes through PyAV, whose
wheels bundle FFmpeg, so no ffmpeg on PATH is needed.

Whisper invents text on silence, so: its VAD filter; segments with no_speech_prob over
NO_SPEECH or avg_logprob under MIN_LOGPROB dropped; a phrase looping more than LOOP times in a
row kept once; a memo under MIN_WORDS words skipped. Its prompt (spelling only) is up to
PROMPT_TERMS concept titles and people names, never note text.

A page is `voice-YYYY-MM-DD-<first words>`: the fact header (When: the date in the file name,
else the file's date; Summary: the first sentence), then a paragraph per ~PARA_SECONDS of
speech with [mm:ss] at its start. Keyed by the audio's sha, so an unchanged file is not
transcribed again; a changed file rewrites its page unless the page was edited (RR-18).
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import date, datetime
from pathlib import Path

from cortex import importers, sources
from cortex._proc import NO_WINDOW
from cortex.vault import Vault, VaultError

LABEL = "Voice memos"  # the pages' header label; sources.edited_pages lists them
AUDIO = (".m4a", ".mp3", ".wav", ".ogg", ".opus", ".aac")
MODEL = "base"
SIZE = "about 150 MB"
PIN = "faster-whisper==1.2.1"  # the `voice` extra in pyproject.toml
MAX_SECONDS = 2 * 3600
PARA_SECONDS = 30
NO_SPEECH, MIN_LOGPROB = 0.6, -1.0
MIN_WORDS, LOOP = 3, 3
PROMPT_TERMS = 40
TITLE_WORDS = 6
NEEDS = (f"Voice memos need the local speech model ({SIZE}). Install it with the Voice memos "
         "card in the app, or run `cortex model download --voice`.")
SKIPS = ("not audio", "no speech", "over 2 hours", "unreadable")
_DAY = re.compile(r"(?<!\d)(20\d\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")
_WORD = re.compile(r"[\W_]+")

_model = None
_LOCK = threading.Lock()


def load_transcriber():
    """`transcribe(path, prompt) -> (seconds, segments)`, or None when faster-whisper or its
    model is not installed. Never downloads. A file over MAX_SECONDS comes back with no
    segments, undecoded."""
    global _model
    with _LOCK:
        if _model is None:
            importlib.invalidate_caches()  # `model download --voice` may have run since start
            try:
                from faster_whisper import WhisperModel

                from cortex import semantic
                _model = WhisperModel(MODEL, device="cpu", compute_type="int8",
                                      download_root=str(semantic.models_dir()),
                                      local_files_only=True)
            except Exception:  # noqa: BLE001 - not installed, or the model is not downloaded
                return None
        model = _model

    def transcribe(path: Path, prompt: str):
        import av
        with av.open(str(path)) as f:
            seconds = (f.duration or 0) / av.time_base
        if seconds > MAX_SECONDS:
            return seconds, []
        segs, info = model.transcribe(str(path), vad_filter=True, initial_prompt=prompt or None)
        return info.duration, segs

    return transcribe


def install(say=print) -> None:
    """`cortex model download --voice`: faster-whisper into this Python (uv pip, `PIN`, held to
    the installer's constraints when there are some), then the model into ~/.cortex/models."""
    from cortex import semantic, update
    importlib.invalidate_caches()
    if importlib.util.find_spec("faster_whisper") is None:
        d = update.install_dir()
        uv = str(d / "uv.exe") if d and (d / "uv.exe").is_file() else shutil.which("uv")
        if not uv:
            raise RuntimeError("uv is not on PATH, so faster-whisper cannot be installed. "
                               f"Install uv, or run: uv pip install {PIN}")
        cmd = [uv, "pip", "install", "--python", sys.executable, PIN]
        env = dict(os.environ)
        cwd = None
        if d:  # the installer's pins and cache folder (installer/setup_app.py)
            if (d / "constraints.txt").is_file():
                # relative, run in `d`: uv 0.9.15 splits a -c path on spaces (uv#22290)
                cmd += ["-c", "constraints.txt"]
                cwd = str(d)
            env.update(UV_CACHE_DIR=str(d / "cache"), UV_NATIVE_TLS="1")
        say(f"installing {PIN} ...")
        subprocess.run(cmd, check=True, env=env, cwd=cwd, creationflags=NO_WINDOW)
        importlib.invalidate_caches()
    from faster_whisper import download_model
    say(f"speech model {MODEL}: downloading if missing into {semantic.models_dir()} ...")
    download_model(MODEL, cache_dir=str(semantic.models_dir()))


def recorded(p: Path) -> str:
    """The day a memo was recorded: a date in its file name (20241103, 2024-11-03), else the
    file's modified date."""
    if m := _DAY.search(p.stem):
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            pass
    return datetime.fromtimestamp(p.stat().st_mtime).date().isoformat()


def spelling(v: Vault) -> str:
    """Whisper's prompt: up to PROMPT_TERMS concept titles and people names, so it spells the
    owner's terms; never a note's text."""
    from cortex.catalog import Catalog
    try:
        terms = [e.title for e in Catalog.load(v).entries.values() if e.kind == "concept"]
    except VaultError:
        terms = []
    names = []
    for p in v.note_paths("person"):
        try:
            names.append(str(v.read_path(p).fields.get("title") or p.stem))
        except VaultError:
            continue
    got = terms[:PROMPT_TERMS] + names[:PROMPT_TERMS]
    return ", ".join(got) + "." if got else ""


def unloop(words: list) -> list:
    """`words` ((word, start) pairs) with any 3+ word phrase repeated more than LOOP times in
    a row kept once (Whisper's hallucination loops)."""
    norm = [_WORD.sub("", w.lower()) for w, _ in words]
    out, i = [], 0
    while i < len(words):
        for n in range(3, 13):
            k = 1
            while norm[i + k * n:i + (k + 1) * n] == norm[i:i + n] and i + (k + 1) * n \
                    <= len(words):
                k += 1
            if k > LOOP:
                out += words[i:i + n]
                i += k * n
                break
        else:
            out.append(words[i])
            i += 1
    return out


def _clock(s: float) -> str:
    s = int(s)
    return f"[{s // 60:02d}:{s % 60:02d}]"


def transcript(segments, stop=lambda: False) -> str | None:
    """The kept speech as paragraphs of about PARA_SECONDS, `[mm:ss]` first; "" when under
    MIN_WORDS words are left, None when `stop()` said to stop."""
    words = []
    for s in segments:
        if stop():
            return None
        if s.no_speech_prob > NO_SPEECH or s.avg_logprob < MIN_LOGPROB:
            continue
        words += [(w, s.start) for w in s.text.split()]
    words = unloop(words)
    if len(words) < MIN_WORDS:
        return ""
    paras, t0 = [], None
    for w, start in words:
        if t0 is None or start - t0 >= PARA_SECONDS:
            t0 = start
            paras.append([_clock(start)])
        paras[-1].append(w)
    return "\n\n".join(" ".join(p) for p in paras)


def _hidden(rel: str) -> bool:
    return any(x.startswith(".") for x in rel.split("/"))


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _audio(root: Path, errors: list) -> list[Path]:
    return [p for p in importers._walk(root, _hidden, errors)
            if p.is_file() and not p.name.startswith(".")]


def count(v: Vault, path: Path) -> tuple[int, int]:
    """(audio files, how many are new) in `path`, for the app's Check: nothing transcribed."""
    files = [p for p in _audio(Path(path), []) if p.suffix.lower() in AUDIO]
    known = sources._known_hashes(v)
    new = 0
    for p in files:
        try:
            new += sources._sha(f"voice {_sha(p)}") not in known
        except OSError:
            continue
    return len(files), new


def voice_source(path: Path, job=None) -> sources.Source:
    """A folder of audio files into one page per memo; see the module docstring. `job` (the
    app's) gets a progress line per file, and its Cancel stops between segments."""
    root = Path(path)
    if not root.is_dir():
        raise VaultError("invalid_argument", f"{root.name} is not a folder of voice memos.")
    transcribe = load_transcriber()
    if transcribe is None:
        raise VaultError("invalid_argument", NEEDS)
    stop = (lambda: job.cancelled.is_set()) if job is not None else (lambda: False)

    def items():
        v = src.vault
        known = sources._known_hashes(v) if v is not None else {}
        prompt = spelling(v) if v is not None else ""
        files = _audio(root, src.walk_errors)
        for i, p in enumerate(files, 1):
            if stop():
                return
            if p.suffix.lower() not in AUDIO:
                yield "not audio"
                continue
            try:
                key, day = f"voice {_sha(p)}", recorded(p)
            except (OSError, ValueError, OverflowError):
                yield "unreadable"
                continue
            if sources._sha(key) in known:  # unchanged since its import: not transcribed again
                yield sources.Item(p.stem, day, "", str(p), key=key)
                continue
            if job is not None:
                job.say(f"transcribing {i:,} of {len(files):,}: {p.name}")
            try:
                seconds, segs = transcribe(p, prompt)
                text = "" if seconds > MAX_SECONDS else transcript(segs, stop)
            except Exception:  # noqa: BLE001 - PyAV and CTranslate2 raise many types
                yield "unreadable"
                continue
            if text is None:
                return
            if seconds > MAX_SECONDS:
                yield "over 2 hours"
            elif not text:
                yield "no speech"
            else:
                speech = re.sub(r"\[\d+:\d\d\] ", "", text)
                first = re.split(r"(?<=[.!?])\s", speech, maxsplit=1)[0]
                title = " ".join(speech.split()[:TITLE_WORDS]).rstrip(".,!?;:")
                yield sources.Item(f"{day} {title}", day, text, str(p), key=key, summary=first)

    src = sources.Source("voice-memos", LABEL, "folder", items,
                         default_tier=sources.Audience.LOCAL, prefix="voice", by_origin=True,
                         skips=SKIPS)
    return src
