"""Sources B, voice memos (voice_memos): a fake transcriber stands in for faster-whisper, so no
model and no faster-whisper are needed. Every fixture is synthetic and built in tmp_path."""

import os
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cortex import importers, life_sources, sources, voice_memos
from cortex.app import actions, server, status
from cortex.cli import app
from cortex.vault import Vault, VaultError

runner = CliRunner()
L = sources.Audience.LOCAL


def seg(start, text, no_speech=0.1, logprob=-0.3):
    return SimpleNamespace(start=start, end=start + 5, text=" " + text,
                           no_speech_prob=no_speech, avg_logprob=logprob)


MEMO = [seg(0, "Remember to revise two pointers before Friday."), seg(10, "Ask Asha about it."),
        seg(36, "Then the graph lecture notes."), seg(70, "That is all.")]
SCRIPTS = {  # file name -> (seconds, segments)
    "20241103_101530.m4a": (80, MEMO),
    "noisy.mp3": (60, [seg(0, "Thanks for watching!", no_speech=0.9),
                       seg(5, "garbled mumble here", logprob=-1.6),
                       seg(10, "I need a new idea for the project."),
                       *[seg(15 + i, "thank you so much") for i in range(6)],
                       seg(40, "Done for today.")]),
    "silence.wav": (30, [seg(0, "you")]),
    "long.ogg": (3 * 3600, []),
}


@pytest.fixture
def fake(monkeypatch):
    """The fake transcriber; `calls` lists (file name, prompt) per transcription."""
    calls = []

    def transcribe(path, prompt):
        calls.append((path.name, prompt))
        raw = path.read_bytes()  # "script:<name>|<version>": new audio bytes, the same memo
        return SCRIPTS[raw[7:].split(b"|")[0].decode() if raw.startswith(b"script:")
                       else path.name]
    monkeypatch.setattr(voice_memos, "load_transcriber", lambda: transcribe)
    return calls


def _memos(tmp_path):
    d = tmp_path / "Recordings"
    for name in SCRIPTS:
        (d / "Phone").mkdir(parents=True, exist_ok=True)
        (d / "Phone" / name).write_bytes(b"audio " + name.encode())
    (d / "notes.txt").write_text("not audio")
    (d / ".trash").mkdir()
    (d / ".trash" / "old.m4a").write_bytes(b"hidden")
    return d


def _pages(v):
    return {p.stem: p.read_text("utf-8") for p in sorted((v.root / "resources").glob("voice-*.md"))}


def test_segments_in_page_out(tmp_path, root_plain, fake):
    d = _memos(tmp_path)
    v = Vault(root_plain)
    src = life_sources.SOURCE_OF["voice-memos"](d)
    assert src.default_tier is L
    r = sources.run(v, src, L)
    assert r["imported"] == 2 and r["private"] == 2
    assert r["reasons"] == {"not audio": 1, "no speech": 1, "over 2 hours": 1, "unreadable": 0}
    pages = _pages(v)
    page = pages["voice-2024-11-03-remember-to-revise-two-pointers-before"]
    assert "private: true" in page and "Source: Voice memos · Date: 2024-11-03" in page
    assert "When: 2024-11-03\nSummary: Remember to revise two pointers before Friday.\n" in page
    assert ("[00:00] Remember to revise two pointers before Friday. Ask Asha about it.\n\n"
            "[00:36] Then the graph lecture notes.\n\n[01:10] That is all.") in page
    # the prompt spells the owner's terms (concept titles), never a note's text
    prompt = dict(fake)["20241103_101530.m4a"]
    assert "Two pointers" in prompt and prompt == voice_memos.spelling(v)
    assert not list(root_plain.rglob("*.m4a")) and not list(root_plain.rglob("*.mp3"))
    assert "old.m4a" not in {n for n, _ in fake}  # a hidden folder is never read


def test_silence_and_hallucination_filters(tmp_path, root_plain, fake):
    v = Vault(root_plain)
    sources.run(v, voice_memos.voice_source(_memos(tmp_path)), L)
    (page,) = [t for k, t in _pages(v).items() if "new-idea" in k]
    assert "Thanks for watching" not in page and "garbled" not in page
    body = page.split("\nSummary: ", 1)[1]  # the frontmatter's summary repeats line one
    assert body.count("thank you so much") == 1 and "Done for today." in body
    words = [(w, 0) for w in "a b c a b c a b c a b c d".split()]
    assert [w for w, _ in voice_memos.unloop(words)] == ["a", "b", "c", "d"]
    three = [(w, 0) for w in "a b c a b c a b c d".split()]  # 3 times is not a loop
    assert voice_memos.unloop(three) == three
    assert voice_memos.transcript([seg(0, "hi there")]) == ""  # under 3 words


def test_refuses_without_the_speech_model(tmp_path, root_plain, monkeypatch):
    monkeypatch.setattr(voice_memos, "load_transcriber", lambda: None)
    d = _memos(tmp_path)
    with pytest.raises(VaultError, match="cortex model download --voice"):
        voice_memos.voice_source(d)
    r = runner.invoke(app, ["import", str(d), "--source", "voice-memos", "--vault",
                            str(root_plain)])
    assert r.exit_code == 1 and "cortex model download --voice" in r.output
    assert not list((root_plain / "resources").glob("voice-*.md"))


def test_reimport_is_a_no_op_and_an_edited_page_is_kept(tmp_path, root_plain, fake):
    d = _memos(tmp_path)
    v = Vault(root_plain)
    sources.run(v, voice_memos.voice_source(d), L)
    before, n = _pages(v), len(fake)
    r = sources.run(v, voice_memos.voice_source(d), L)
    assert r["imported"] == 0 and r["duplicate"] == 2 and _pages(v) == before
    assert len(fake) == n + 2  # only the two that made no page are transcribed again
    memo = d / "Phone" / "20241103_101530.m4a"
    memo.write_bytes(b"script:20241103_101530.m4a|2")
    r = sources.run(v, voice_memos.voice_source(d), L)
    assert r["updated"] == 1 and r["imported"] == 0 and set(_pages(v)) == set(before)
    (pid,) = [k for k in before if "remember" in k]
    p = root_plain / "resources" / f"{pid}.md"
    p.write_text(p.read_text("utf-8").replace("That is all.", "That is all. My own edit."),
                 encoding="utf-8")
    memo.write_bytes(b"script:20241103_101530.m4a|3")  # changed again: the edit stays
    r = sources.run(v, voice_memos.voice_source(d), L)
    assert r["imported"] == 1 and "My own edit." in p.read_text("utf-8")
    edited = sources.edited_pages(v, [])
    assert [e["page"] for e in edited] == [pid] and edited[0]["private"]


def test_recorded_day_from_name_or_file_date(tmp_path):
    a = tmp_path / "PTT-20240229-WA0001.opus"
    a.write_bytes(b"x")
    assert voice_memos.recorded(a) == "2024-02-29"
    b = tmp_path / "Recording 7.m4a"
    b.write_bytes(b"x")
    os.utime(b, (1714557600, 1714557600))  # 2024-05-01 10:00 UTC
    assert voice_memos.recorded(b)[:7] in ("2024-04", "2024-05")


def test_cli_imports_private_by_default(tmp_path, root_plain, fake):
    r = runner.invoke(app, ["import", str(_memos(tmp_path) / "Phone"), "--vault",
                            str(root_plain)])
    assert r.exit_code == 0, r.output
    assert "Voice memos: tier private (the default" in r.output
    assert len(list((root_plain / "resources").glob("voice-2024-11-03-*.md"))) == 1


def test_detect_and_card_wiring(tmp_path, root_plain):
    d = _memos(tmp_path)
    assert importers.detect(d / "Phone") == ["voice-memos"]
    notes = tmp_path / "my-notes"
    notes.mkdir()
    (notes / "a.md").write_text("# A\n")
    (notes / "talk.m4a").write_bytes(b"x")
    assert importers.detect(notes) == ["notes"]  # a recording among notes is an attachment
    assert importers.SOURCES["voice-memos"] == "voice-memos"
    assert "voice-memos" in importers.GUIDES and "voice-memos" in server.IMPORTS
    assert {"voice-memos"} <= set(actions.TIERED) & set(actions.OWN_CHECK) & set(actions.KIND)
    js = (server.ASSETS / "app.js").read_text(encoding="utf-8")
    assert "'voice-memos'" in js and "/api/setup/voice" in js and "p.need === 'voice'" in js
    (root_plain / "resources").mkdir(exist_ok=True)
    (root_plain / "resources" / "voice-2024-01-01-x.md").write_text("x")
    assert status.imports(root_plain)["voice-memos"]["count"] == 1


def test_model_download_voice_installs_the_pin_then_the_model(home, monkeypatch):
    """`cortex model download --voice` is the one path that installs faster-whisper (uv pip,
    pinned, into this Python) and fetches the model into ~/.cortex/models; faked here."""
    import sys

    from cortex import semantic, update
    ran, got = [], []
    monkeypatch.setattr(voice_memos.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(update, "install_dir", lambda: None)
    monkeypatch.setattr(voice_memos.shutil, "which", lambda name: "uv")
    monkeypatch.setattr(voice_memos.subprocess, "run", lambda cmd, **k: ran.append(cmd))
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(
        download_model=lambda size, cache_dir: got.append((size, cache_dir))))
    r = runner.invoke(app, ["model", "download", "--voice"])
    assert r.exit_code == 0, r.output
    assert ran == [["uv", "pip", "install", "--python", sys.executable, voice_memos.PIN]]
    assert got == [("base", str(semantic.models_dir()))] and str(home) in got[0][1]
    monkeypatch.setattr(voice_memos.shutil, "which", lambda name: None)
    r = runner.invoke(app, ["model", "download", "--voice"])
    assert r.exit_code == 1 and "uv is not on PATH" in r.output
