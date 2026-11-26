"""RR-17b: a golden snapshot of each agent rules file Keepfold writes (Cursor, VS Code, Codex
through `targets.Rules`, Gemini's GEMINI.md block), as a new file holds it with no vault (the
prime line alone; the pinned core comes from the owner's vault and is not part of the shape).
A change to the block's wording or markers then shows up in review.

Regenerate after a deliberate change:
    KEEPFOLD_REGEN_GOLDEN=1 uv run pytest tests/test_agent_rules_golden.py
and read the diff under tests/fixtures/agent_rules/ before committing it."""

import os
from pathlib import Path

import pytest

from keepfold.app import targets

GOLDEN = Path(__file__).parent / "fixtures" / "agent_rules"


def _rule_files(missing: Path) -> dict[str, str]:
    """{target id: the text it writes into a rules file that does not exist yet}."""
    out = {}
    for t in targets.targets():
        if isinstance(t, targets.Rules):
            out[t.id] = targets.block_text(missing, targets.rule_block(None), t.RULE_HEAD)
        elif isinstance(t, targets.Gemini):
            out[t.id] = targets.block_text(missing, t.BLOCK)
    return out


def test_every_rule_block_matches_its_snapshot(tmp_path):
    got = _rule_files(tmp_path / "none.md")
    assert {"cursor", "vscode", "codex", "gemini-cli"} <= set(got)
    if os.environ.get("KEEPFOLD_REGEN_GOLDEN"):
        GOLDEN.mkdir(parents=True, exist_ok=True)
        for old in GOLDEN.glob("*.md"):
            old.unlink()
        for tid, text in got.items():
            (GOLDEN / f"{tid}.md").write_text(text, encoding="utf-8", newline="\n")
        pytest.skip("golden files rewritten; review the diff")
    have = {p.stem: p.read_text(encoding="utf-8") for p in GOLDEN.glob("*.md")}
    assert set(have) == set(got), "a target was added or removed: regenerate (see docstring)"
    for tid, text in got.items():
        assert text == have[tid], f"{tid}'s rules block changed: regenerate (see docstring)"
