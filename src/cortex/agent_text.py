"""RR-36: every fixed string an agent can see, with its size, and the total per app profile
(`cortex agent-text`), so a prompt-budget change shows up in one place. Only what the code and
the skill ship: server instructions, tool descriptions, `next` hints, SKILL.md, the prime hook's
templates and the rule-file block; vault text (claims, pages) is never part of it.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKILL = ("skills", "cortex", "SKILL.md")  # a checkout only: the wheel does not ship the skill
HOOK_TEMPLATES = ("HEAD", "NEXT", "TAGGED", "NUDGE")  # cortex.prime_hook's output
HINTS = ("OFFER", "MOCK_BRIEF", "ASK_FOCUS")  # mcp_server `next` text not named *_NEXT
# The channels that reach an app (by profiles.norm name): MCP reaches every client; the Claude
# Code plugin (skill and hooks) only Claude; Connect's rule file Cursor, VS Code and Codex.
PLUGIN_APPS = ("claude",)
RULE_APPS = ("cursor", "visualstudiocode", "copilot", "codex")


def _text(v) -> str | None:
    if isinstance(v, str):
        return v
    if isinstance(v, dict) and v and all(isinstance(x, str) for x in v.values()):
        return "\n".join(f"{k}: {x}" for k, x in v.items())
    return None


def sources() -> list[tuple[str, str, str]]:
    """(label, channel mcp|plugin|rules, text) of every fixed agent-facing string."""
    from cortex import mcp_server, packs, prime_hook

    out = [("mcp_server.INSTRUCTIONS", "mcp", mcp_server.INSTRUCTIONS)]
    out += [(f"mcp_server.DESCRIPTIONS[{k}]", "mcp", v)
            for k, v in mcp_server.DESCRIPTIONS.items()]
    out += [(f"mcp_server.{k}", "mcp", t) for k, v in sorted(vars(mcp_server).items())
            if k.isupper() and (k.endswith("_NEXT") or k in HINTS) and (t := _text(v))]
    if (p := REPO.joinpath(*SKILL)).is_file():
        out.append(("/".join(SKILL), "plugin", p.read_text(encoding="utf-8")))
    out += [(f"prime_hook.{k}", "plugin", getattr(prime_hook, k)) for k in HOOK_TEMPLATES]
    out.append(("packs.rule_block (no vault)", "rules", packs.rule_block(None)))
    return out


def channels(app: str) -> set[str]:
    from cortex.profiles import norm

    n = norm(app)
    return {"mcp"} | ({"plugin"} if any(a in n for a in PLUGIN_APPS) else set()) \
        | ({"rules"} if any(a in n for a in RULE_APPS) else set())


def sizes(data: dict, srcs: list[tuple[str, str, str]]) -> list[tuple[str, int]]:
    """(profile match or 'unknown', chars of fixed text that app can see) per profile."""
    names = [p["match"] for p in data["profiles"]] + ["unknown"]
    return [(n, sum(len(t) for _, c, t in srcs if c in channels(n))) for n in names]
