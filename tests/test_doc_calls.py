"""P1 call lint: every `keepfold_<tool>(...)` snippet the model reads (the skill and its modes,
INSTRUCTIONS, tool descriptions, string literals in mcp_server.py) must match the real input
schemas. The MCP SDK silently drops an unknown argument, so a wrong name in a doc never errors.

Rules: the tool exists; every `name=` is a real parameter; a quoted (or bare word) value of an
enum parameter is in the enum. A positional argument is either a bare parameter name, the
shorthand for "pass it" (`keepfold_chats(action="read", id, part)`, `id?` when optional), or a
value in first place, standing for the tool's first parameter (`keepfold_read("x")`). Snippets
with no parens or only a `...` placeholder are prose and skipped."""

import ast
import re
from pathlib import Path

import pytest

from keepfold import mcp_server
from keepfold.mcp_server import build_server
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "keepfold"
CALL = re.compile(r"\bkeepfold_([a-z_]+)\(")
WORD = re.compile(r"[a-z_]+")
PAIRS = {"(": ")", "[": "]", "{": "}", "<": ">"}  # <placeholder, with commas>
ALLOW: set[tuple[str, str]] = set()  # (source, snippet): keep tiny, one-line reason each


def _strings(path: Path) -> list[str]:
    """String literals in a module, docstrings excluded (they are not model-facing)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and n.body and isinstance(n.body[0], ast.Expr)}
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):  # an f-string: its literal parts, `…` per field
            out.append("".join(v.value if isinstance(v, ast.Constant) else "…" for v in n.values))
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            out.append(n.value)
    return out


def sources() -> list[tuple[str, str]]:
    files = [SKILL / "SKILL.md", *sorted((SKILL / "modes").glob("*.md"))]
    return ([(str(p.relative_to(ROOT)), p.read_text(encoding="utf-8")) for p in files]
            + [("INSTRUCTIONS", mcp_server.INSTRUCTIONS)]
            + [(f"DESCRIPTIONS[{k}]", v) for k, v in mcp_server.DESCRIPTIONS.items()]
            + [("mcp_server.py", s) for s in _strings(ROOT / "src" / "keepfold" / "mcp_server.py")])


def _split(text: str) -> list[str] | None:
    """Top-level arguments of the call whose `(` ended just before `text`; None if unclosed."""
    args, depth, quote, cur = [], 0, None, ""
    for ch in text[:600]:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'`" and not (ch == "'" and cur[-1:].isalnum()):  # prime's ids: no quote
            quote = ch
        elif ch in PAIRS:
            depth += 1
        elif ch in PAIRS.values():
            if depth == 0:
                return [*args, cur.strip()] if cur.strip() or args else []
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
            continue
        cur += ch
    return None


def calls(srcs):
    for src, text in srcs:
        for m in CALL.finditer(text):
            args = _split(text[m.end():])
            if args is None or args in (["..."], ["…"]):
                continue
            yield src, f"keepfold_{m.group(1)}({', '.join(args)})", m.group(1), args


def _enum(prop: dict) -> list | None:
    for p in (prop, *prop.get("anyOf", ())):
        if "enum" in p:
            return p["enum"]
    return None


def lint(tools: dict, srcs) -> tuple[list[str], int]:
    """(findings, snippets seen) for `srcs` [(source, text)] against `tools` {name: properties}."""
    bad, seen = [], 0
    for src, snippet, tool, args in calls(srcs):
        seen += 1
        if (src, snippet) in ALLOW:
            continue
        props = tools.get(f"keepfold_{tool}")
        if props is None:
            bad.append(f"{src}: {snippet}: no tool keepfold_{tool}")
            continue
        for i, arg in enumerate(args):
            name, eq, value = arg.partition("=")
            if arg in ("...", "…"):
                continue
            if not eq or not re.fullmatch(r"\s*\w+\s*", name):  # positional
                if not (arg.rstrip("?") in props or i == 0 and props):
                    bad.append(f"{src}: {snippet}: positional argument {arg!r} (name it)")
                continue
            name, value = name.strip(), value.strip()
            if name not in props:
                bad.append(f"{src}: {snippet}: no parameter {name!r}")
                continue
            enum = _enum(props[name])
            lit = value[1:-1] if value[:1] in "\"'" and value[-1:] == value[:1] else value
            if enum and WORD.fullmatch(lit) and lit not in enum:
                bad.append(f"{src}: {snippet}: {name}={lit!r} not in {enum}")
    return bad, seen


@pytest.fixture
def tools(root):
    return {t.name: t.parameters.get("properties", {})
            for t in build_server(Vault(root))._tool_manager.list_tools()}


def test_doc_calls_match_schemas(tools):
    bad, seen = lint(tools, sources())
    assert seen > 50  # the extractor still finds the docs' calls
    assert not bad, "\n".join(bad)


def test_lint_catches_the_dogfood_mistakes(tools):
    srcs = [("a", 'keepfold_add(kind="note", title="T", summary="s", note_type="fleeting")'),
            ("b", "then keepfold_patch(page, reject, target=<the line>)."),
            ("c", "keepfold_log(kind='touch', fair=true) and keepfold_start(mode=\"mocks\")"),
            ("d", "keepfold_lint() and keepfold_find (prose) and keepfold_grade(...)"),
            ("e", 'keepfold_read("my-topic-map"), keepfold_chats(action="read", id, part?)')]
    bad, seen = lint(tools, srcs)
    assert seen == 7 and len(bad) == 6, bad
    assert [b.split(":")[0] for b in bad] == ["a", "a", "b", "c", "c", "d"]
