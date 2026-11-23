"""`cortex eval agent` (R12): does each AI app get the right lines about the owner, and none it
must not? Ten cases (assets/agent_eval.json, adapted from Gemini round 5's A01-A10), each a
made-up vault, an AI app's MCP client name and a prompt.

Layer A, model-free: each case's vault is built in a temp folder through cortex.vault, and the
text that client is served on the case's channels (cortex_prime over MCP as that client, the
rules-file block app/targets.py writes for it, a context pack) is checked for the lines it must
get and the ones it must not. The real serving code runs, never a copy of its logic.

Layer B, owner-run only (`--model`): that text and the prompt go to the local model the owner
already loaded (chat_eval.find_model: never loads, unloads or downloads one, never a cloud API),
three times per case; a case passes only if all three replies pass its regexes (pass^3).
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from pathlib import Path

from cortex import claims, taint, xray
from cortex.vault import Vault

SPEC = Path(__file__).parent / "assets" / "agent_eval.json"
RUNS = 3
SYSTEM = ("You are the user's AI assistant. Cortex, the user's memory, gave you this context "
          "about them:\n\n{served}")


def spec() -> dict:
    return json.loads(SPEC.read_text(encoding="utf-8"))


def build(root: Path, *blocks: dict) -> Vault:
    """A new vault at `root` from `blocks` (the spec's `base`, then a case's `vault`), in order:
    pages, lines, an agent's guesses, X-ray mutes (agent_eval.json's meta.vault_block)."""
    root.mkdir(parents=True)
    (root / ".cortex").mkdir()
    v = Vault(root)
    for b in blocks:
        for p in b.get("pages", []):
            v.create_note(p["kind"], p["id"], p.get("fields", {}), p["body"], p.get("folder"))
        for page, head, text in b.get("lines", []):
            v.append_to_section("me", page, head, text)
    guesses = [g for b in blocks for g in b.get("guesses", [])]
    if guesses:
        from cortex.mcp_server import Cortex

        c = Cortex(v)  # in process, no user_confirmed: an AI app's inferred line, tier agent
        c.session = c.sessions.require("patch")  # the ambient session a tool call opens
        for page, head, text in guesses:
            c.cortex_patch(f"me/{page}", "append_to_section", head, text)
    ids = {r["text"]: r["id"] for r in claims.rows(v)}
    for text in (t for b in blocks for t in b.get("mute", [])):
        xray.set_flag(v, "mute", xray.claim(ids[text]), True)
    return v


def served(vault: Vault, case: dict) -> str:
    """What `case["client"]` is served on each of the case's channels, joined."""
    from cortex import packs
    from cortex.app import targets
    from cortex.mcp_server import Cortex

    out = []
    for ch in case["serve"]:
        if ch == "prime":
            c = Cortex(vault)
            c.over_mcp, c.sessions.client = True, case["client"]  # as the MCP handshake names it
            out.append(c.cortex_prime(case["prompt"])["context"])
        elif ch == "rules":
            entry = {"args": ["mcp", "--vault", str(vault.root)]}  # as clients.server_entry
            out.append(targets.rule_block(entry, case["client"]))
        elif ch.startswith("pack:"):
            out.append(packs.pack(vault, ch.removeprefix("pack:")))
        else:
            raise ValueError(f"{case['id']}: unknown channel {ch!r}")
    return "\n\n".join(out)


def check_a(text: str, case: dict) -> list[str]:
    """Layer A's reasons `text` fails `case` ([] passes): a must_serve line missing, a forbid
    one present (substrings, any case)."""
    low, a = text.lower(), case["layer_a"]
    return ([f"not served: {s!r}" for s in a["must_serve"] if s.lower() not in low]
            + [f"served: {s!r}" for s in a["forbid"] if s.lower() in low])


def check_b(reply: str | None, case: dict) -> list[str]:
    """Layer B's reasons one model reply fails `case` ([] passes)."""
    if not reply:
        return ["no answer"]
    b = case["layer_b"]
    return ([f"no match for {r!r}" for r in b["regex_all"] if not re.search(r, reply)]
            + [f"matched {r!r}" for r in b["regex_none"] if re.search(r, reply)]
            + [f"leaked {s!r}" for s in b["must_not_leak"] if s.lower() in reply.lower()])


def case_a(case: dict, base: dict, root: Path) -> tuple[str, list[str]]:
    """(served text, Layer A's reasons) for `case`, its vault built at `root`. The owner's
    config is never read (taint.pinned), so the result is the same on any machine."""
    with taint.pinned({}):
        text = served(build(root, base, case["vault"]), case)
    return text, check_a(text, case)


def run(*, model: bool = False, runner=None, runs: int = RUNS, progress=None) -> dict:
    """Layer A on every case; with `model`, Layer B too on `runner` (default: the loaded local
    model, chat_eval.find_model). `progress(done, total, id)` after each case."""
    sp = spec()
    why_b = "" if model else "no --model"
    if model and runner is None:
        from cortex import chat_eval

        runner, why_b = chat_eval.find_model()
    rows, tmp, t0 = [], Path(tempfile.mkdtemp(prefix="cortex-agent-eval-")), time.monotonic()
    try:
        for n, case in enumerate(sp["cases"], 1):
            text, why = case_a(case, sp["base"], tmp / case["id"])
            known = case.get("known_failure")
            row = {k: case[k] for k in ("id", "category", "client")}
            row |= {"a": "known failure" if why and known else "fail" if why else "pass",
                    "a_why": why, **({"known_failure": known} if known else {}),
                    "b": None, "b_why": []}
            if runner is not None:
                fails = [check_b(runner.chat(SYSTEM.format(served=text), case["prompt"]), case)
                         for _ in range(runs)]
                row["b"] = sum(not f for f in fails)
                row["b_why"] = next((f for f in fails if f), [])
            rows.append(row)
            if progress:
                progress(n, len(sp["cases"]), case["id"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"cases": rows, "runs": runs if runner is not None else 0,
            "model": getattr(runner, "model", None), "b_not_run": why_b if runner is None else "",
            "a_seconds": round(time.monotonic() - t0, 2) if runner is None else None,
            "a_pass": sum(r["a"] == "pass" for r in rows),
            "a_known": sum(r["a"] == "known failure" for r in rows),
            "a_fail": sum(r["a"] == "fail" for r in rows),
            "b_pass": sum(r["b"] == runs for r in rows) if runner is not None else None}


def summary(r: dict) -> str:
    """One line per case (id, category, A, B) and the totals."""
    out = []
    for c in r["cases"]:
        b = f"{c['b']}/{r['runs']}" if c["b"] is not None else "not run"
        why = "; ".join(c["a_why"] + c["b_why"])
        out.append(f"{c['id']}  {c['category']:<26} A {c['a']:<13} B {b}"
                   + (f"  ({why})" if why else ""))
    n = len(r["cases"])
    out.append(f"Layer A: {r['a_pass']}/{n} pass" + (
        f", {r['a_known']} known failure(s)" if r["a_known"] else "") + (
        f", {r['a_fail']} FAIL" if r["a_fail"] else "") + (
        f" in {r['a_seconds']} s" if r["a_seconds"] is not None else ""))
    out.append(f"Layer B: {r['b_pass']}/{n} pass^{r['runs']} on {r['model']}"
               if r["b_pass"] is not None else f"Layer B: not run ({r['b_not_run']})")
    return "\n".join(out)
