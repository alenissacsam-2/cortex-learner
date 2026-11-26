"""Release metadata agrees across pyproject, plugin.json and marketplace.json, and the plugin
launches only the `keepfold` command."""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def test_versions_and_names_agree():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"]["version"]
    plugin = load("plugin/.claude-plugin/plugin.json")
    market = load(".claude-plugin/marketplace.json")
    (entry,) = market["plugins"]
    assert plugin["version"] == entry["version"] == version
    assert plugin["name"] == entry["name"] == "keepfold"
    assert entry["source"] == "./plugin"
    for meta in (plugin, entry):
        assert meta["license"] == "MIT" and meta["description"] and meta["keywords"]
        assert meta["author"]["name"]


def test_mcp_registry_entry_matches_the_package():
    """server.json (mcp-publisher) names the PyPI release and runs `keepfold mcp`; the registry
    checks PyPI ownership by the README's mcp-name line, and caps the description at 100."""
    server = load("server.json")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"]["version"]
    (pkg,) = server["packages"]
    assert server["version"] == pkg["version"] == version
    assert (pkg["registryType"], pkg["identifier"], pkg["transport"]) == (
        "pypi", "keepfold", {"type": "stdio"})
    assert [a["value"] for a in pkg["packageArguments"]] == ["mcp"]
    assert len(server["description"]) <= 100
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"<!-- mcp-name: {server['name']} -->" in readme


def test_plugin_runs_only_keepfold():
    servers = load("plugin/.mcp.json")["mcpServers"]
    assert {s["command"] for s in servers.values()} == {"keepfold"}
    hooks = load("plugin/hooks/hooks.json")["hooks"]
    commands = [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]
    assert commands and all(c.split()[0] == "keepfold" for c in commands)
    # Claude Code runs hooks in Git Bash, or in Windows PowerShell 5.1 when Git is missing (a
    # new laptop): `||` does not parse there, `; exit 0` works in both and never blocks a prompt
    assert all(c.endswith("; exit 0") and "|" not in c and "&" not in c for c in commands)
    for groups in hooks.values():
        for g in groups:  # the taint matcher excludes only Keepfold and no-outside-text servers
            names = re.findall(r"mcp__\(([^)]*)\)__", g.get("matcher", ""))
            assert all(set(n.split("|")) == {"keepfold", "plugin_keepfold_keepfold",
                                             "ccd_[^_]*", "visualize",
                                             "scheduled-tasks", "savvy-progress"} for n in names)
