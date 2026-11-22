import importlib.util
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_skills", ROOT / "scripts" / "build_skills.py")
bs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bs)


def keys(fields):
    return [f.split(":", 1)[0] for f in fields]


def test_build_writes_both_variants(tmp_path):
    src = ROOT / "skills" / "cortex" / "SKILL.md"
    src_fields, src_body = bs.split(src.read_text(encoding="utf-8"))
    code, zpath = bs.build(src, tmp_path)

    c_fields, c_body = bs.split(code.read_text(encoding="utf-8"))
    assert c_body == src_body
    assert c_fields[: len(src_fields)] == src_fields
    assert "disable-model-invocation: true" in c_fields
    assert "allowed-tools: mcp__plugin_cortex_cortex__*" in c_fields

    with zipfile.ZipFile(zpath) as z:
        names = sorted(z.namelist())
        assert names == ["cortex/", "cortex/SKILL.md", "cortex/modes/",
                         *(f"cortex/modes/{m.name}" for m in sorted(src.parent.glob("modes/*.md")))]
        p_fields, p_body = bs.split(z.read("cortex/SKILL.md").decode("utf-8"))
    assert keys(p_fields) == ["name", "description", "license"]
    assert p_body == src_body
    for m in (src.parent / "modes").glob("*.md"):
        assert (code.parent / "modes" / m.name).read_bytes() == m.read_bytes()


def test_no_gate_left(tmp_path):
    """SPEC §0 U4: Cortex is always on, so nothing shipped may gate it behind /cortex."""
    code, zpath = bs.build(ROOT / "skills" / "cortex" / "SKILL.md", tmp_path)
    with zipfile.ZipFile(zpath) as z:
        bodies = [code.read_text(encoding="utf-8"), z.read("cortex/SKILL.md").decode("utf-8")]
    for text in bodies:
        assert "Outside /cortex" not in text and "do not call `cortex_*`" not in text
    shipped = [(ROOT / "plugin" / ".mcp.json").read_text(encoding="utf-8"),
               (ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"),
               *(p.read_text(encoding="utf-8") for p in (ROOT / "dist").glob("*.json"))]
    for text in shipped:
        assert "--gate" not in text and "UserPromptExpansion" not in text
    assert "SessionEnd" in shipped[1]


def test_modes_have_rows_and_files():
    """Every cortex_start mode has a row in the core table; every referenced modes/ file exists."""
    import re
    import typing

    from cortex.mcp_server import Cortex

    skill = ROOT / "skills" / "cortex"
    core = (skill / "SKILL.md").read_text(encoding="utf-8")
    from mcp.server import mcpserver as sdk

    # Context resolves in mcp_server's globals only after build_server: name it here, so the
    # test passes alone and under -n auto too
    hint = typing.get_type_hints(Cortex.cortex_start, localns={"Context": sdk.Context})["mode"]
    for mode in typing.get_args(hint):
        assert f"| `{mode}` |" in core, mode
    refs = set(re.findall(r"`(modes/[\w-]+\.md)`", core))
    assert refs
    for r in refs:
        assert (skill / r).is_file(), r
    assert len(core.split()) <= 900


def test_help_lists_every_mode_and_command():
    """`/cortex help` (modes/help.md) names every mode of the core table and every visible CLI
    command, so the list people read cannot fall behind."""
    import typer.main

    from cortex.cli import app

    skill = ROOT / "skills" / "cortex"
    core = (skill / "SKILL.md").read_text(encoding="utf-8")
    help_text = (skill / "modes" / "help.md").read_text(encoding="utf-8")
    for mode in re.findall(r"^\| `(\w+)` \|", core, re.M):
        if mode != "home":  # home is plain /cortex
            assert f"/cortex {mode}" in help_text, mode
    cmd = typer.main.get_command(app)
    for name, c in cmd.commands.items():
        if not c.hidden:
            assert f"`{name}" in help_text or f" {name}" in help_text, name
