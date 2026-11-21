"""MCP output that shows its evidence and its limits: date and source filters on find and
recall (RR-31), what was searched when nothing (or little) was found (RR-14), the best passage
of the top recall hits and windowed reads (RR-12), prime's coverage line (RR-5)."""

import pytest
from conftest import FakeEmbed, note

from cortex import semantic
from cortex.mcp_server import Cortex
from cortex.vault import Vault, VaultError

pytestmark = pytest.mark.usefixtures("home")


@pytest.fixture
def vault(root):
    note(root, "notes/engines.md", "title: Engines\ndate: 2026-03-01\n",
         "# Engines\n\nAutomobile car motor basics.")
    # as sources.run writes them: a `date:` field and a `Source:` line
    note(root, "resources/repo-motor.md", "title: motor-repo\nkind: resource\ndate: 2025-06-10\n",
         "# motor-repo\n\nSource: Git · Date: 2025-06-10 · Path: x\n\nA car motor simulator.")
    note(root, "resources/notes-car.md", "title: Car note\nkind: resource\ndate: 2026-10-01\n",
         "# Car note\n\nSource: Notes · Date: 2026-10-01\n\nMy car vehicle checklist.")
    note(root, "notes/hidden.md", "title: Hidden\nprivate: true\ndate: 2026-03-02\n",
         "# Hidden\n\ncar motor diary")
    return root


def ids(out):
    return [h["id"] for h in out["hits"]]


def test_find_and_recall_filter_by_day_and_source(vault, monkeypatch):
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    c = Cortex(Vault(vault))
    assert set(ids(c.cortex_find("car"))) >= {"engines", "repo-motor", "notes-car"}
    assert ids(c.cortex_find("car", source="git")) == ["repo-motor"]
    assert ids(c.cortex_find("car", source="mine")) == ["engines"]
    assert ids(c.cortex_find("car", since="2026-01-01", until="2026-06-30")) == ["engines"]
    assert ids(c.cortex_find("car", since="2026-09-01")) == ["notes-car"]
    for tool in (c.cortex_recall, c.cortex_find):
        assert ids(tool("car", source="Notes")) == ["notes-car"]
        assert ids(tool("car", until="2025-12-31")) == ["repo-motor"]
        assert "hidden" not in ids(tool("car", since="2026-03-02"))
        for bad in ({"since": "2026-13-01"}, {"until": "yesterday"}, {"source": " "},
                    {"since": "2026-05-01", "until": "2026-04-01"}):
            with pytest.raises(VaultError) as e:
                tool("car", **bad)
            assert e.value.code == "invalid_argument", bad
