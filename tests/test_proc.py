"""A1: every subprocess the app or the CLI starts passes `creationflags`, so the windowless
`cortex-app.exe` never flashes a console window on Windows (cortex/_proc.py NO_WINDOW). New
calls without it fail here; the allow-list names the ones that are detached or show a window on
purpose."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPAWN = {"run", "Popen", "call", "check_call", "check_output"}
# (file, enclosing function): why it needs no NO_WINDOW
ALLOWED = {
    ("installer/build.py", "main"): "build time, in the developer's terminal",
    # shortcut: localllm._lms gets the flag from the local-chat change; drop this entry once
    # that change is merged
    ("src/cortex/localllm.py", "_lms"): "owned by another change",
}


def _spawns(path: Path):
    """(function name, line) of each subprocess call in `path` without creationflags."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            via = getattr(f.value, "id", "") if isinstance(f, ast.Attribute) else ""
            if (via == "subprocess" and name in SPAWN) or name == "_popen":
                if not any(k.arg == "creationflags" or k.arg is None for k in node.keywords):
                    yield fn.name, node.lineno


def test_every_subprocess_passes_creationflags():
    files = [*(ROOT / "src" / "cortex").rglob("*.py"), *(ROOT / "installer").glob("*.py")]
    bad = []
    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        bad += [f"{rel}:{line} in {fn}()" for fn, line in _spawns(p)
                if (rel, fn) not in ALLOWED]
    assert not bad, "pass creationflags=NO_WINDOW (cortex._proc): " + ", ".join(bad)


def test_the_scan_finds_a_call_without_the_flag(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("import subprocess\n\ndef f():\n    subprocess.run(['x'])\n\n"
                 "def g():\n    subprocess.run(['x'], creationflags=0)\n    _popen(['y'])\n")
    assert list(_spawns(p)) == [("f", 4), ("g", 8)]
