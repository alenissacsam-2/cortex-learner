"""import-chats for every export source: detection, parsing, dedupe, errors, redaction."""

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from cortex import chats
from cortex.vault import VaultError

EXPORTS = Path(__file__).parent / "fixtures" / "exports"
GPT = "chatgpt/6700aaaa-1111-2222-3333-444455556666"


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_CHATS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("CORTEX_CHAT_EXPORT_DIR", str(tmp_path / "exported"))
    return tmp_path / "exported"


def _zip(tmp_path, members: dict[str, str]) -> Path:
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as f:
        for name, text in members.items():
            f.writestr(name, text)
    return z


def _rows():
    return {c["id"]: c for c in chats.list_chats(set(), limit=50)}


def test_chatgpt_zip_follows_the_current_branch_and_redacts(out, tmp_path):
    z = _zip(tmp_path, {"conversations.json":
                        (EXPORTS / "chatgpt_conversations.json").read_text("utf-8"),
                        "chat.html": "<html></html>"})
    assert chats.import_export(z) == ("chatgpt", 1)  # the empty one is skipped
    row = _rows()[GPT]
    assert row["source"] == "chatgpt" and row["started"].startswith("2025-10-01T09:00:00")
    assert chats.read_parts(GPT, "user") == [
        "User: Explain Dijkstra. My key is [redacted]\nUser: And with negative edges?"]
    (part,) = chats.read_parts(GPT)
    assert "Assistant: It grows a shortest-path tree." in part
    stored = (out / f"{GPT}.jsonl").read_text("utf-8")
    for gone in ("abandoned", "TOOL OUTPUT", "sk-chatgpt"):
        assert gone not in stored  # other branch, tool turn, secret (redacted on import)


def test_gemini_takeout_groups_by_conversation_or_session(out, tmp_path):
    z = _zip(tmp_path, {"Takeout/My Activity/Gemini Apps/MyActivity.json":
                        (EXPORTS / "gemini_MyActivity.json").read_text("utf-8")})
    assert chats.import_export(z) == ("gemini", 3)
    assert sorted(p.stem for p in (out / "gemini").glob("*.jsonl")) == [
        "2026-10-01-2000", "2026-10-02-0900", "abc123def456"]
    assert _rows()["gemini/2026-10-02-0900"]["source"] == "gemini"  # only it has 2+ user turns
    assert chats.read_parts("gemini/2026-10-02-0900") == [
        "User: How does BFS work? token [redacted]\nAssistant: Level by level.\n"
        "User: And for weighted graphs?\nAssistant: Use Dijkstra & a heap."]
    assert chats.read_parts("gemini/2026-10-01-2000") == ["User: Was ist ein Graph?"]


def test_perplexity_markdown_file_and_folder(out, tmp_path):
    md = EXPORTS / "perplexity_thread.md"
    assert chats.import_export(md) == ("perplexity", 1)
    (cid,) = _rows()
    (part,) = chats.read_parts(cid)
    lines = part.splitlines()
    assert lines[0] == "User: What is a minimum spanning tree?"
    assert lines[1].startswith("Assistant: A minimum spanning tree connects")
    assert "Kruskal and Prim" in lines[1] and lines[2] == "User: Which is faster on dense graphs?"
    for gone in ("[^1_1]", "example.com", "⁂", "<img", "<div"):
        assert gone not in part
    folder = tmp_path / "threads"
    folder.mkdir()
    shutil.copy(md, folder / "renamed thread.md")
    assert chats.import_export(folder) == ("perplexity", 1)
    assert list(_rows()) == [cid]  # same thread, same id: no duplicate


def test_perplexity_account_export_json(out, tmp_path):
    """The real export shape: user_data_export_*.zip holding conversations-<stamp>.json (a dict
    with a conversations list) and an .xlsx."""
    z = _zip(tmp_path, {"conversations-20260901_100000-abcd1234.json":
                        (EXPORTS / "perplexity_conversations.json").read_text("utf-8"),
                        "user-data-20260901_100000-abcd1234.xlsx": "not a sheet"})
    assert chats.import_export(z, dry_run=True) == ("perplexity", 2, 0)
    assert chats.import_export(z) == ("perplexity", 2)
    cid = "perplexity/aaaa1111-2222-3333-4444-555566667777"
    assert chats.read_parts(cid) == [
        "User: What is a trie?\nAssistant: A prefix tree for strings.\n"  # [1] stripped
        "User: Lookup cost? my key [redacted]"]  # the empty answer is dropped
    assert _rows()[cid]["source"] == "perplexity"
    assert chats.import_export(z, source="perplexity") == ("perplexity", 2)  # forced: same


def test_perplexity_threads_with_the_same_first_question_both_stay(out, tmp_path):
    folder = tmp_path / "threads"
    folder.mkdir()
    (folder / "a.md").write_text("# What is a heap?\n\nALPHA answer\n", "utf-8")
    (folder / "b.md").write_text("# What is a heap?\n\nBETA answer\n", "utf-8")
    (folder / "copy of a.md").write_text("# What is a heap?\n\nALPHA answer\n", "utf-8")
    assert chats.import_export(folder) == ("perplexity", 2)  # the copy is the same thread
    text = "".join(p.read_text("utf-8") for p in (out / "perplexity").glob("*.jsonl"))
    assert len(list((out / "perplexity").glob("*.jsonl"))) == 2
    assert "ALPHA" in text and "BETA" in text
    assert chats.import_export(folder, dry_run=True) == ("perplexity", 0, 2)


def test_forced_gemini_refuses_search_and_chrome_activity(out, tmp_path):
    with pytest.raises(VaultError) as e:
        chats.import_export(EXPORTS / "takeout_MyActivity.json", source="gemini")
    assert "Gemini" in str(e.value) and "google-activity" in str(e.value)
    assert not out.exists()


def test_dedupe_on_source_and_id(out, tmp_path):
    conv = json.loads((EXPORTS / "chatgpt_conversations.json").read_text("utf-8"))[0]
    f = tmp_path / "conversations.json"
    f.write_text(json.dumps([conv]), "utf-8")
    assert chats.import_export(f) == ("chatgpt", 1)
    assert chats.import_export(f) == ("chatgpt", 1)
    claude = [{"uuid": conv["id"], "updated_at": "2026-10-01T10:00:00Z", "chat_messages": [
        {"sender": "human", "text": "Same id, other service", "created_at": "2026-10-01"},
        {"sender": "human", "text": "Second turn", "created_at": "2026-10-01"}]}]
    f.write_text(json.dumps(claude), "utf-8")
    assert chats.import_export(f) == ("claude", 1)
    ids = [c["id"] for c in chats.list_chats(set(), limit=50)]
    assert sorted(ids) == [GPT, f"claude-ai/{conv['id']}"]


def test_force_source_when_detection_fails(out, tmp_path):
    conv = json.loads((EXPORTS / "chatgpt_conversations.json").read_text("utf-8"))[0]
    f = tmp_path / "odd.json"
    f.write_text(json.dumps([{"note": "first item has no known keys"}, conv]), "utf-8")
    with pytest.raises(VaultError) as e:
        chats.import_export(f)
    assert e.value.code == "invalid_argument" and "--source" in str(e.value)
    assert chats.import_export(f, source="chatgpt") == ("chatgpt", 1)
    with pytest.raises(VaultError) as e:
        chats.import_export(f, source="bard")
    assert "chatgpt" in str(e.value)


@pytest.mark.parametrize("name, body, expect", [
    ("broken.json", "[{not json", "Cannot read"),
    ("dict.json", '{"a": 1}', "list"),
    ("MyActivity.html", "<html>Gemini Apps</html>", "JSON"),
])
def test_malformed_files_name_what_was_expected(out, tmp_path, name, body, expect):
    f = tmp_path / name
    f.write_text(body, "utf-8")
    with pytest.raises(VaultError) as e:
        chats.import_export(f)
    assert expect in str(e.value)


def test_zip_without_an_export_lists_the_expected_files(out, tmp_path):
    z = _zip(tmp_path, {"notes.txt": "hello"})
    with pytest.raises(VaultError) as e:
        chats.import_export(z)
    for want in ("conversations.json", "MyActivity.json", ".md"):
        assert want in str(e.value)
    bad = tmp_path / "bad.zip"
    bad.write_text("not a zip", "utf-8")
    with pytest.raises(VaultError):
        chats.import_export(bad)


def test_empty_export_imports_nothing(out, tmp_path):
    f = tmp_path / "conversations.json"
    f.write_text("[]", "utf-8")
    assert chats.import_export(f, source="chatgpt") == ("chatgpt", 0)


def test_guides_cover_every_source():
    assert set(chats.GUIDES) == {"claude", "chatgpt", "gemini", "perplexity", "google-activity"}
    for g in chats.GUIDES.values():
        assert g["steps"] and g["link"].startswith("https://") and g["wait"] and g["pick"]


def test_dry_run_counts_new_and_already_imported_and_writes_nothing(out, tmp_path):
    f = tmp_path / "conversations.json"
    shutil.copy(EXPORTS / "chatgpt_conversations.json", f)
    assert chats.import_export(f, dry_run=True) == ("chatgpt", 1, 0)
    assert not out.exists()
    assert chats.import_export(f) == ("chatgpt", 1)
    before = {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert chats.import_export(f, dry_run=True) == ("chatgpt", 0, 1)
    assert {p: p.read_bytes() for p in out.rglob("*") if p.is_file()} == before


def test_gemini_html_gives_the_same_rows_as_json(out, tmp_path):
    from cortex.browse import takeout
    page = EXPORTS / "takeout_html" / "Takeout" / "My Activity" / "Gemini Apps" / "MyActivity.html"
    rows, skipped = takeout.html_rows(page.read_bytes())
    js = json.loads((EXPORTS / "gemini_MyActivity.json").read_text("utf-8"))
    for r in js:
        r.pop("titleUrl", None)  # the HTML rows seen carry no chat link; chats split at a gap

    def norm(convs):
        return [(cid, [(role, ts[:19], " ".join(t.split())) for role, ts, t in turns])
                for cid, _, turns in convs]

    assert skipped == 0 and norm(chats._gemini(rows)) == norm(chats._gemini(js))
    z = _zip(tmp_path, {"Takeout/My Activity/Gemini Apps/MyActivity.html":
                        page.read_text("utf-8")})
    assert chats.import_export(z) == ("gemini", 3)
    assert chats.read_parts("gemini/2026-10-02-0900") == [
        "User: How does BFS work? token [redacted]\nAssistant: Level by level.\n"
        "User: And for weighted graphs?\nAssistant: Use Dijkstra & a heap."]


def test_perplexity_json_strips_citation_markers_outside_code():
    text = ("Tries are fast.[1] Lookups too[2][3], see `a[1]` and x [4] stays.\n"
            "```\nb = a[1]\n```\nEnd.[12]")
    assert chats._uncite(text) == ("Tries are fast. Lookups too, see `a[1]` and x [4] stays.\n"
                                   "```\nb = a[1]\n```\nEnd.")
    assert chats._uncite("[1] at the start and a[1234] too long") == \
        "[1] at the start and a[1234] too long"
    assert chats._uncite("open fence\n```\nc[2]") == "open fence\n```\nc[2]"


def _pplx_json(cid, question, answer):
    return json.dumps({"conversations": [{"context_uuid": cid, "updated_at": "2026-09-01",
                                          "entries": [{"query": question, "answer": answer,
                                                       "created_at": "2026-09-01"}]}]})


@pytest.mark.parametrize("md_first", [True, False])
def test_perplexity_thread_from_markdown_and_json_is_stored_once(out, tmp_path, md_first):
    md = tmp_path / "Graphs.md"
    md.write_text("# What is a graph?\n\nNodes and edges, joined.[^1]\n\n"
                  "[^1]: https://example.org\n", "utf-8")
    js = tmp_path / "conversations-1.json"
    js.write_text(_pplx_json("bbbb1111-2222-3333-4444-555566667777", "What is a graph?",
                             "Nodes and edges, joined.[1]"), "utf-8")
    other = tmp_path / "conversations-2.json"
    other.write_text(_pplx_json("cccc1111-2222-3333-4444-555566667777", "What is a graph?",
                                "A different answer."), "utf-8")
    first, second = (md, js) if md_first else (js, md)
    assert chats.import_export(first) == ("perplexity", 1)
    assert chats.import_export(second, dry_run=True) == ("perplexity", 0, 1)
    assert chats.import_export(second) == ("perplexity", 1)
    assert len(list((out / "perplexity").glob("*.jsonl"))) == 1
    assert chats.import_export(other) == ("perplexity", 1)  # another answer: another thread
    assert len(list((out / "perplexity").glob("*.jsonl"))) == 2


def test_perplexity_threads_that_only_open_the_same_way_never_merge(out, tmp_path):
    """G2: a JSON thread and two Markdown threads that all open with "hi" and the same answer
    are three threads; no import overwrites or drops one of them."""
    hello = "Hello! How can I help you today?"
    js = tmp_path / "conversations-1.json"
    js.write_text(json.dumps({"conversations": [{
        "context_uuid": "dddd1111-2222-3333-4444-555566667777", "updated_at": "2026-09-01",
        "entries": [{"query": "hi", "answer": hello, "created_at": "2026-09-01"},
                    {"query": "explain heaps", "answer": "A heap is a tree.",
                     "created_at": "2026-09-01"}]}]}), "utf-8")
    z = _zip(tmp_path, {"dns.md": f"# hi\n\n{hello}\n\n# what is DNS\n\nNames to IPs.\n",
                        "pizza.md": f"# hi\n\n{hello}\n\n# best pizza\n\nNeapolitan.\n"})
    assert chats.import_export(js) == ("perplexity", 1)
    assert chats.import_export(z) == ("perplexity", 2)
    files = list((out / "perplexity").glob("*.jsonl"))
    assert len(files) == 3
    text = "".join(f.read_text("utf-8") for f in files)
    assert all(w in text for w in ("explain heaps", "what is DNS", "best pizza"))
    assert chats.import_export(js) == ("perplexity", 1)  # re-import: still three
    assert len(list((out / "perplexity").glob("*.jsonl"))) == 3


def test_gemini_html_rows_with_an_unreadable_date_are_counted(out, tmp_path):
    from typer.testing import CliRunner

    from cortex.cli import app
    page = (EXPORTS / "takeout_html" / "Takeout" / "My Activity" / "Gemini Apps" /
            "MyActivity.html").read_text("utf-8")
    bad = page.replace("Oct 2, 2026, 2:29:00&#8239;PM IST", "sometime")
    assert bad != page
    z = _zip(tmp_path, {"Takeout/My Activity/Gemini Apps/MyActivity.html": bad})
    got = chats.import_export(z)
    assert got == ("gemini", 3) and got.skipped == 1
    r = CliRunner().invoke(app, ["import", str(z)])
    assert r.exit_code == 0, r.output
    assert "Gemini: 3 conversations imported" in r.output
    assert ("Gemini: read 3, imported 3, private 0, duplicate 0, skipped 1 (1 unreadable date)"
            in r.output)  # the import report


@pytest.mark.parametrize("folder, stem, product, label", [
    ("Mi actividad", "MiActividad", "Apps de Gemini", "Instrucción: "),
    ("Meine Aktivitäten", "MeineAktivitäten", "Gemini-Apps", "Prompt eingegeben: "),
    ("मेरी गतिविधि", "मेरी गतिविधि", "Gemini ऐप्लिकेशन", "प्रॉम्प्ट: "),
    ("マイ アクティビティ", "マイアクティビティ", "Gemini アプリ", "プロンプト："),
])
def test_gemini_takeout_in_another_language(out, tmp_path, folder, stem, product, label):
    """Synthetic: the product and prompt label in the account language give the same chats."""
    text = (EXPORTS / "gemini_MyActivity.json").read_text("utf-8")
    text = text.replace("Gemini Apps", product).replace('"Prompted ', f'"{label}')
    z = _zip(tmp_path, {f"Takeout/{folder}/{product}/{stem}.json": text})
    from cortex import importers
    assert importers.detect(z) == ["chats"]
    assert chats.import_export(z) == ("gemini", 3)
    assert chats.read_parts("gemini/2026-10-02-0900")[0].startswith("User: How does BFS work?")


def _gem(time, prompt, url=None):
    row = {"header": "Gemini Apps", "title": f"Prompted {prompt}", "time": time,
           "products": ["Gemini Apps"], "safeHtmlItem": [{"html": "ok"}]}
    return {**row, "titleUrl": url} if url else row


def _gem_chats(rows):
    return {cid: [t for r, _, t in turns if r == "user"] for cid, _, turns in chats._gemini(rows)}


def test_gemini_rows_without_a_chat_id_split_at_a_30_minute_gap():
    rows = [_gem("2026-10-02T11:00:00Z", "c"),  # 45 min after b: a new chat
            _gem("2026-10-02T10:10:00Z", "b"),  # 10 min after a: the same chat
            _gem("2026-10-02T10:00:00Z", "a"),
            _gem("2026-10-02T10:20:00Z", "x", "https://gemini.google.com/app/abc123def456"),
            _gem("2026-10-02T13:00:00Z", "y", "https://gemini.google.com/app/abc123def456"),
            _gem("not a time", "skipped")]
    assert _gem_chats(rows) == {"abc123def456": ["x", "y"], "2026-10-02-1000": ["a", "b"],
                                "2026-10-02-1100": ["c"]}
    assert list(chats._gemini(rows)) == list(chats._gemini(rows[::-1]))  # stable ids


def _claude_conv(uid, updated, text):
    return {"uuid": uid, "name": "t", "updated_at": updated, "chat_messages": [
        {"sender": "human", "created_at": updated, "content": [{"type": "text", "text": text}]},
        {"sender": "assistant", "created_at": updated,
         "content": [{"type": "text", "text": "ok"}]}]}


def test_split_export_reads_every_conversations_file_and_keeps_the_latest_copy(out, tmp_path):
    """C6: a large export split over conversations-1.json, -2.json... is read whole; one id in
    two files is one conversation, its most recently updated copy wins."""
    a = _claude_conv("aaaa-1111", "2026-10-01T09:00:00Z", "old question")
    a2 = _claude_conv("aaaa-1111", "2026-10-03T09:00:00Z", "newer question")
    b = _claude_conv("bbbb-2222", "2026-10-02T09:00:00Z", "second file question")
    z = _zip(tmp_path, {"conversations-1.json": json.dumps([a]),
                        "conversations-2.json": json.dumps([b, a2]),
                        "users.json": "[]"})
    got = chats.import_export(z)
    assert got == ("claude", 2)
    assert chats.read_parts("claude-ai/bbbb-2222", "user") == ["User: second file question"]
    assert chats.read_parts("claude-ai/aaaa-1111", "user") == ["User: newer question"]
    rep = got.report()
    assert rep["files"] == 2 and rep["read"] == 3 and rep["duplicate"] == 1
    assert rep["imported"] == 2
