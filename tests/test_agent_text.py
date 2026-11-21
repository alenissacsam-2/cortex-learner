import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cortex import agent_text, mcp_server, profiles
from cortex.cli import app

pytestmark = pytest.mark.usefixtures("home")
FIXTURES = Path(__file__).parent / "fixtures"


def test_every_listed_source_still_exists():
    """A renamed or deleted constant must fail here, not vanish from `cortex agent-text`."""
    srcs = agent_text.sources()
    labels = {label for label, _, _ in srcs}
    assert all(text.strip() for _, _, text in srcs)
    assert "mcp_server.INSTRUCTIONS" in labels
    assert {f"mcp_server.DESCRIPTIONS[{k}]" for k in mcp_server.DESCRIPTIONS} <= labels
    assert {f"mcp_server.{k}" for k in agent_text.HINTS} <= labels  # getattr would hide none
    assert {"mcp_server.ASK_NEXT", "mcp_server.ERROR_NEXT", "mcp_server.START_NEXT"} <= labels
    assert {f"prime_hook.{k}" for k in agent_text.HOOK_TEMPLATES} <= labels
    assert agent_text.REPO.joinpath(*agent_text.SKILL).is_file()
    assert "/".join(agent_text.SKILL) in labels
    assert "packs.rule_block (no vault)" in labels


def test_sizes_per_profile_follow_the_channels():
    srcs = agent_text.sources()
    got = dict(agent_text.sizes(profiles.DEFAULT, srcs))
    mcp = sum(len(t) for _, c, t in srcs if c == "mcp")
    assert got["unknown"] == got["lmstudio"] == mcp
    assert got["claude"] > mcp and got["cursor"] > mcp and got["claude"] != got["cursor"]
    assert agent_text.channels("Visual Studio Code") == {"mcp", "rules"}


def test_cli_prints_texts_and_sizes_with_or_without_a_vault(tmp_path):
    r = CliRunner().invoke(app, ["agent-text"])
    assert r.exit_code == 0, r.output
    assert "== mcp_server.INSTRUCTIONS [mcp] (" in r.output
    assert "== chars per app profile ==" in r.output and "\nunknown: " in r.output
    root = tmp_path / "vault"
    shutil.copytree(FIXTURES / "vault_mini", root)
    r = CliRunner().invoke(app, ["agent-text", "--vault", str(root)])
    assert r.exit_code == 0, r.output
    assert "\ncursor: " in r.output
