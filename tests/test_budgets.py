"""Budget guard rails (v6 CONSENSUS item 2, r1-systems P5): the hook entry points stay light
and closing a session on a mid-size vault stays well inside the 30 s SessionEnd hook."""

import json
import os
import subprocess
import sys
import time

import pytest
from conftest import git

from keepfold import session as session_mod
from keepfold import vault as vault_mod
from keepfold.vault import Vault

# Runs `keepfold <argv1>` as the console script does and reports, even past prime-hook's
# os._exit, every top-level third-party module and every keepfold module it imported.
PROBE = """
import json, os, sys
real_exit = os._exit
def report():
    third = sorted({m.split('.')[0] for m in sys.modules} - set(sys.stdlib_module_names))
    mine = sorted(m for m in sys.modules if m.startswith('keepfold'))
    sys.stderr.write('\\nIMPORTS ' + json.dumps({'third': third, 'keepfold': mine}) + '\\n')
os._exit = lambda c: (report(), real_exit(c))
sys.argv = ['keepfold', sys.argv[1]]
from keepfold import taint
taint.main()
report()
"""
# Heavy imports that would cost seconds on every prompt or tool call. Today both paths also
# import ruamel (taint.config reads the pointer with it; r1-systems P2) and the prime path
# imports fsrs; those are allowed here until P2 lands.
HEAVY = {"mcp", "numpy", "fastembed", "onnxruntime", "typer", "tokenizers"}
HEAVY_KEEPFOLD = {"keepfold.mcp_server", "keepfold.semantic", "keepfold.cli"}


@pytest.fixture
def root(root_committed):
    return root_committed


@pytest.mark.parametrize("cmd, payload", [
    ("prime-hook", {"session_id": "c1", "hook_event_name": "UserPromptSubmit",
                    "prompt": "How do two pointers work?"}),
    ("taint", {"session_id": "c1", "tool_name": "WebFetch"}),
])
def test_hook_import_budget(root, cmd, payload):
    r = subprocess.run([sys.executable, "-c", PROBE, cmd], input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", timeout=120,
                       env={**os.environ, "KEEPFOLD_VAULT": str(root)})
    line = next(x for x in r.stderr.splitlines() if x.startswith("IMPORTS "))
    got = json.loads(line.removeprefix("IMPORTS "))
    # the path really ran: prime loaded its module, taint reached the vault write
    assert {"prime-hook": "keepfold.prime_hook", "taint": "keepfold.vault"}[cmd] in got["keepfold"]
    assert not HEAVY & set(got["third"]), got["third"]
    assert not HEAVY_KEEPFOLD & set(got["keepfold"]), got["keepfold"]


def test_a_tagged_prompt_saves_without_the_sdk(root):
    """QA 22 Nov: a //tag took the hook from ~0.9 s to ~4 s; the mcp SDK import (~1.6 s) and
    an unused index build were most of it. mcp_server comes in, the SDK does not."""
    payload = {"session_id": "c1", "hook_event_name": "UserPromptSubmit",
               "prompt": "Explain heaps\n//pref: Replies in British English"}
    r = subprocess.run([sys.executable, "-c", PROBE, "prime-hook"], input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", timeout=120,
                       env={**os.environ, "KEEPFOLD_VAULT": str(root)})
    line = next(x for x in r.stderr.splitlines() if x.startswith("IMPORTS "))
    got = json.loads(line.removeprefix("IMPORTS "))
    assert "keepfold.mcp_server" in got["keepfold"] and "Keepfold saved" in r.stdout
    assert not HEAVY & set(got["third"]), got["third"]
    assert "keepfold.semantic" not in got["keepfold"]


# No slow marker in this repo, so the vault stays small enough for a ~10 s test. Since close
# stopped rebuilding the dashboard and index.md (v6 item 5) it takes ~1.4 s here and at 5,000
# pages alike (mostly git), down from ~4 s here and 25-29 s at 5,000.
PAGES = 500


def page(i: int) -> str:
    body = f"Page {i} text about topic t{i % 40}, linking [[r-{(i + 7) % PAGES}]].\n" * 25
    return (f'---\ntitle: "Page {i}"\ntags: ["t{i % 40}"]\nrelated: ["[[r-{i + 1}]]"]\n---\n'
            f"# Page {i}\n\n{body}")


@pytest.mark.slow  # about 10 s here
@pytest.mark.perf  # a 5 s timing bound: it fails when other work loads every core
def test_close_budget(root, monkeypatch):
    (root / "resources").mkdir()
    for i in range(PAGES):
        (root / "resources" / f"r-{i}.md").write_text(page(i), encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "pages")
    mgr = session_mod.SessionManager(Vault(root))
    mgr.start("learn")
    mgr.note("logged")  # a learning log, so the close is the full (not the quiet) pipeline
    # Windows' virus scan holds the first open of each new file (~6 ms each, half the time
    # here); read each once so the bound times Keepfold, not the scanner. Real pages are old.
    for p in root.rglob("*.md"):
        p.read_bytes()
    monkeypatch.setattr(vault_mod, "_PARSED", {})  # a cold parse, as in the SessionEnd sweep
    scanned = []  # v6 T7: the commit-time secret scan reads only the files being committed
    real_pattern = session_mod.secret_pattern
    monkeypatch.setattr(session_mod, "secret_pattern",
                        lambda text: scanned.append(text) or real_pattern(text))
    t0 = time.perf_counter()
    out = mgr.close(summary="budget")
    took = time.perf_counter() - t0
    assert out["commit"], out
    assert 0 < len(scanned) < 20, len(scanned)  # the close's own few files, not the 500 pages
    assert took < 5, f"closing one session took {took:.1f} s on {PAGES} pages"
