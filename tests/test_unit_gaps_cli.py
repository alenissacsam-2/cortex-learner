"""TEST-PLAN §2 items 8-9: one CliRunner smoke per `cortex` command on vault_mini (exit 0, no
traceback, no private text in the output), then the item-9 functions that matter most.
Nothing here needs the network, a real model or a real browser profile."""

import json

import pytest
from conftest import FakeEmbed, git, note
from typer.testing import CliRunner

from cortex import semantic
from cortex.cli import app

pytestmark = pytest.mark.usefixtures("home")
PRIV = "PRIVMARKER"  # only in private pages: no command may print it


@pytest.fixture
def vault(root, home, monkeypatch):
    """vault_mini with private text in a private concept, a private note and a private
    browse page; the home config points at it; a fake embedder; no browser found."""
    from cortex.browse import sources

    p = root / "concepts" / "secret-diary.md"
    p.write_text(p.read_text(encoding="utf-8") + f"\n{PRIV} diary line\n", encoding="utf-8")
    note(root, "notes/private-thoughts.md",
         'id: "private-thoughts"\ntitle: "Thoughts"\nprivate: true\ntags: [note]\n',
         f"# Thoughts\n\n{PRIV} about arrays\n")
    note(root, "browse/private/browse-private-2026-11-01.md",
         'id: "browse-private-2026-11-01"\ntitle: "Browsing 2026-11-01 (private)"\n'
         "private: true\n", f"# Browsing\n\n- {PRIV} search · google\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "private pages")
    (home / ".cortex").mkdir(exist_ok=True)
    (home / ".cortex" / "config.yaml").write_text(f"vault: {json.dumps(str(root))}\n",
                                                  encoding="utf-8")
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    real = sources.discover  # the selftest passes its own temp folders; nothing else is read
    monkeypatch.setattr(sources, "discover", lambda local=None, roaming=None: (
        real(local, roaming) if local is not None else []))
    return root


# (args, exit codes allowed). {V} is the vault, {T} a fresh temp folder. Left out: `mcp`,
# `app` and `browse serve` (they serve until stopped), `model download` (network), `ask`,
# `llm start` and `llm test` (a real local model).
CASES = [
    (["init", "{T}/new"], {0}),
    (["doctor", "--vault", "{V}"], {0, 1}),  # 1: the fixture repo has no remote or push hook
    (["setup", "--dry-run", "--yes", "--vault", "{V}"], {0}),
    (["setup", "--print-mcp-config", "--vault", "{V}"], {0}),
    (["lint", "--vault", "{V}"], {0}),
    (["relink", "--dry-run", "--vault", "{V}"], {0}),
    (["reflect", "--vault", "{V}"], {0}),
    (["probes", "--vault", "{V}"], {0}),
    (["needs", "--vault", "{V}"], {0}),
    (["chats", "--vault", "{V}"], {0}),
    (["claims", "--vault", "{V}"], {0}),
    (["claims", "--unasked", "--vault", "{V}"], {0}),
    (["claims", "--summary", "--vault", "{V}"], {0}),
    (["saved", "--vault", "{V}"], {0}),
    (["know", "--vault", "{V}"], {0}),
    (["brief", "--vault", "{V}"], {0}),
    (["today", "--vault", "{V}"], {0}),
    (["dashboard", "--no-open", "--vault", "{V}"], {0}),
    (["replay", "--dry-run", "--vault", "{V}"], {0}),
    (["backup", "--to", "{T}/bak", "--vault", "{V}"], {0}),
    (["access", "--vault", "{V}"], {0}),
    (["recall", "arrays", "--vault", "{V}"], {0}),
    (["log", "result", "two-pointers", "pass", "--vault", "{V}"], {0}),
    (["log", "solved", "lc-1", "--vault", "{V}"], {0}),
    (["log", "assessment", "acme-oa-2026", "--outcome", "pass", "--vault", "{V}"], {0}),
    (["log", "taught", "arrays-basics", "--vault", "{V}"], {0}),
    (["concept", "add", "Sliding window", "--parent", "dsa.arrays", "--vault", "{V}"], {0}),
    (["concept", "alias", "two-pointers", "twin pointers", "--vault", "{V}"], {0}),
    (["concept", "duplicates", "--vault", "{V}"], {0}),
    (["catalog", "sync", "--vault", "{V}"], {0}),
    (["session", "sweep", "--vault", "{V}"], {0}),
    (["session", "close", "--vault", "{V}"], {0}),
    (["model", "build", "--vault", "{V}"], {0}),
    (["model", "status", "--vault", "{V}"], {0}),
    (["eval", "recall", "--vault", "{V}"], {0}),
    (["eval", "learning", "--vault", "{V}"], {0}),
    (["eval", "all", "--vault", "{V}"], {0}),
    (["topics", "status", "--vault", "{V}"], {0}),
    (["topics", "attach", "--dry-run", "--vault", "{V}"], {0}),
    (["topics", "build", "--dry-run", "--vault", "{V}"], {0}),
    (["topics", "eval", "--vault", "{V}"], {0}),
    (["harvest", "status", "--vault", "{V}"], {0}),
    (["export", "context", "--vault", "{V}"], {0}),
    (["exclude", "add", "{T}/ex"], {0}),
    (["exclude", "list"], {0}),
    (["browse", "selftest"], {0}),
    (["browse", "scan"], {0}),
    (["browse", "import", "--dry-run", "--vault", "{V}"], {0}),
    (["browse", "status", "--vault", "{V}"], {0}),
    (["llm", "status"], {0}),
    (["sample", "remove", "--vault", "{V}"], {0}),
]


@pytest.mark.parametrize("args,codes", CASES, ids=[" ".join(a[:2]) for a, _ in CASES])
def test_every_command_runs_clean_and_prints_no_private_text(vault, tmp_path, args, codes):
    (tmp_path / "ex").mkdir()
    argv = [a.replace("{V}", str(vault)).replace("{T}", str(tmp_path)) for a in args]
    r = CliRunner().invoke(app, argv)
    out = r.output + (r.stderr if r.stderr_bytes is not None else "")
    assert r.exit_code in codes, (r.exit_code, out, r.exception)
    assert r.exception is None or isinstance(r.exception, SystemExit), repr(r.exception)
    assert "Traceback" not in out
    assert PRIV not in out


def test_exclude_add_list_remove_round_trip(vault, tmp_path):
    ex = tmp_path / "ex"
    ex.mkdir()
    for cmd in ("add", "remove"):
        assert CliRunner().invoke(app, ["exclude", cmd, str(ex)]).exit_code == 0
        listed = CliRunner().invoke(app, ["exclude", "list"]).output
        assert (str(ex) in listed) == (cmd == "add"), listed


# ---- item 9: the functions that matter most ----------------------------------------------


def test_dashboard_index_and_html_leave_private_pages_out(vault):
    from datetime import UTC, datetime

    from cortex import dashboard
    from cortex.vault import Vault
    assert CliRunner().invoke(app, ["dashboard", "--no-open", "--vault", str(vault)]
                              ).exit_code == 0
    index = (vault / "index.md").read_text(encoding="utf-8")
    html = dashboard.snapshot(Vault(vault), now=datetime.now(UTC))  # Export snapshot
    assert "[[two-pointers]]" in index  # the public pages are there
    for text in (index, html):
        assert PRIV not in text and "private-thoughts" not in text
    assert "secret-diary" not in index


def test_recall_at_forgets_over_time():
    from datetime import UTC, datetime, timedelta

    from cortex.levels import ConceptState, recall_at

    t0 = datetime(2026, 11, 1, 9, tzinfo=UTC)
    assert recall_at(None, t0) == 0.0
    assert recall_at(ConceptState("c"), t0) == 0.0  # never reviewed: nothing to forecast
    st = ConceptState("c", stability=5.0, last_review=t0)
    ps = [recall_at(st, t0 + timedelta(days=d)) for d in (0, 1, 5, 30)]
    assert ps[0] == pytest.approx(1.0) and ps == sorted(ps, reverse=True) and ps[-1] > 0
    naive = (t0 + timedelta(days=5)).replace(tzinfo=None)  # a naive time counts as UTC
    assert recall_at(st, naive) == pytest.approx(ps[2])


def test_best_chunks_picks_each_pages_best_chunk_and_its_offset():
    import numpy as np

    page = np.array([0, 0, 0, 1, 2, 2])
    start = np.array([0, 100, 200, 0, 0, 50])
    sims = np.array([0.1, 0.9, 0.9, 0.3, 0.5, 0.2], np.float32)
    best, at = semantic.best_chunks(page, start, sims, 3)
    assert best.tolist() == pytest.approx([0.9, 0.3, 0.5])
    assert at.tolist() == [100, 0, 0]  # a tie keeps the first chunk
    best, at = semantic.best_chunks(page[:0], start[:0], sims[:0], 0)
    assert len(best) == len(at) == 0


def test_changed_lines_never_names_a_person_and_caps():
    from cortex import render

    person = {"kind": "person", "id": "asha", "tool": "patch", "op": "set_field"}
    lines = render.changed_lines([person, person])
    assert lines == ["- changed a people page ×2"] and "asha" not in "".join(lines)
    many = [{"kind": "note", "id": f"n{k}", "tool": "add"} for k in range(40)]
    lines = render.changed_lines(many)
    assert len(lines) == render.CHANGED_SHOWN + 1 and lines[-1] == "+25 more"
    evil = render.changed_lines([{"kind": "note", "id": "x", "tool": "patch", "op": "set_field",
                                  "target": "<img src=x>[a](http://evil.example)"}])[0]
    assert "<" not in evil and "evil.example" not in evil


def test_letter_card_line_escapes_untrusted_text():
    from cortex import letters

    line = letters.card_line({"written": "2026-11-03",
                              "text": "Keep going ![x](http://t.example/p.png) <b>now</b>"})
    assert line.startswith("A note from you (written 3 Nov):")
    assert "t.example" not in line and "<b>" not in line and "Keep going" in line


def test_p0s_takes_the_latest_live_baseline():
    from cortex import surprisal

    def ev(i, claim, p0):
        return {"id": i, "kind": "log", "payload": {"kind": "baseline", "claim": claim, "p0": p0}}

    evs = [ev("a", "c1", 0.5), ev("b", "c1", 0.8), ev("c", "c2", 0.25),
           {"id": "v", "kind": "void", "ref": "c"}]
    assert surprisal.p0s(evs) == {"c1": 0.8}
    assert surprisal.bits(evs) == {"c1": pytest.approx(0.3219, abs=1e-3)}
