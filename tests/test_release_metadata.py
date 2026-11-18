"""Release metadata agrees across pyproject, plugin.json and marketplace.json, and the plugin
launches only the `cortex` command."""

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
    assert plugin["name"] == entry["name"] == "cortex"
    assert entry["source"] == "./plugin"
    for meta in (plugin, entry):
        assert meta["license"] == "MIT" and meta["description"] and meta["keywords"]
        assert meta["author"]["name"]


def test_plugin_runs_only_cortex():
    servers = load("plugin/.mcp.json")["mcpServers"]
    assert {s["command"] for s in servers.values()} == {"cortex"}
    hooks = load("plugin/hooks/hooks.json")["hooks"]
    commands = [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]
    assert commands and all(c.split()[0] == "cortex" for c in commands)
    for groups in hooks.values():
        for g in groups:  # the taint matcher excludes only Cortex and no-outside-text servers
            names = re.findall(r"mcp__\(([^)]*)\)__", g.get("matcher", ""))
            assert all(set(n.split("|")) == {"cortex", "plugin_cortex_cortex",
                                             "ccd_[^_]*", "visualize",
                                             "scheduled-tasks", "savvy-progress"} for n in names)
