"""Per-agent profiles (plan v9 item 8): which context scopes (cortex.access.SCOPES) each MCP
client may get. Owner settings in `.cortex/profiles.json`, written only by the Cortex app
(`/api/profiles`), never from MCP input; mcp_server.Cortex._scopes reads them and the gate in
cortex.access enforces them. A client matches the first profile whose `match` is in its name
(letters and digits only, any case); a client no profile matches gets `unknown`.
"""

from __future__ import annotations

import copy
import json
import logging
import re

from cortex import access
from cortex.access import SCOPES
from cortex.vault import Vault, is_private

PATH = ".cortex/profiles.json"
CODING = ["core", "coding", "projects", "environment"]  # SCOPES order, as check() stores it
DEFAULT = {
    "profiles": [
        {"match": "claude", "scopes": ["all"]},  # Claude Desktop, Claude Code: as before
        {"match": "lmstudio", "scopes": ["all"]},  # a local model may see everything
        {"match": "cursor", "scopes": CODING},
        {"match": "visualstudiocode", "scopes": CODING},  # VS Code (Copilot)'s client name
        {"match": "copilot", "scopes": CODING},
        {"match": "codex", "scopes": CODING},
    ],
    "unknown": ["core"],  # fail closed: an app no profile names gets the pinned core only
}
MAX_PROFILES = 30


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _scopes(v, where: str) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(s, str) and s in SCOPES for s in v):
        raise ValueError(f"{where}: scopes must be a list of {', '.join(SCOPES)}")
    return [s for s in SCOPES if s in v]  # SCOPES order, no duplicates


def check(data) -> dict:
    """`data` as stored: {"profiles": [{"match", "scopes"}], "unknown": scopes}; ValueError
    when it is not that shape."""
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), list) \
            or len(data["profiles"]) > MAX_PROFILES:
        raise ValueError(f"profiles must be a list of at most {MAX_PROFILES}")
    out = []
    for p in data["profiles"]:
        m = p.get("match") if isinstance(p, dict) else None
        if not isinstance(m, str) or not norm(m) or len(m) > 60:
            raise ValueError("each profile needs a client name to match (letters or digits)")
        out.append({"match": m.strip(), "scopes": _scopes(p.get("scopes"), m)})
    return {"profiles": out, "unknown": _scopes(data.get("unknown"), "unknown")}


def load(vault: Vault) -> dict:
    """The owner's profiles, else DEFAULT (no file yet, or one Cortex cannot read)."""
    p = vault.root / PATH
    try:
        return check(json.loads(p.read_bytes())) if p.is_file() else copy.deepcopy(DEFAULT)
    except (OSError, ValueError) as e:
        logging.getLogger(__name__).warning("%s ignored, using the defaults: %s", PATH, e)
        return copy.deepcopy(DEFAULT)


def save(vault: Vault, data) -> dict:
    """Check and store the profiles (the app's own click only); returns them as stored."""
    data = check(data)
    with vault.lock():
        vault.write_text(vault.root / PATH, json.dumps(data, indent=2) + "\n")
    return data


def scopes_for(data: dict, client: str) -> frozenset[str] | None:
    """The scopes of the first profile matching `client`, else `unknown`; None for all."""
    name = norm(client)
    scopes = next((p["scopes"] for p in data["profiles"] if norm(p["match"]) in name),
                  data["unknown"]) if name else data["unknown"]
    return None if "all" in scopes else frozenset(scopes)


def withheld(vault: Vault, scopes) -> set[str]:
    """Bare ids of the pages an MCP client with `scopes` (None: everything) may not get from
    the me/ pages and the personal kinds: every private me/ page, always (Claude and cloud
    apps never see private notes); with a profile, also the me/ pages outside its scopes
    (access.scope_page) and journal and people pages unless it has `personal` (scope_kind).
    One rule for the MCP server and the Claude Code prime hook."""
    out = {n.id for n in vault.iter_notes("me") if is_private(n)}
    if scopes is not None:
        out |= {p.stem for p in vault.note_paths("me") if not access.scope_page(scopes, p.stem)}
        out |= {p.stem for k in access.KIND_SCOPE if not access.scope_kind(scopes, k)
                for p in vault.note_paths(k)}
    return out
