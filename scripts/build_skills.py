"""Regenerate the plugin skill and Chat skill zip from skills/cortex/ (SKILL.md, modes/).

Run: uv run python scripts/build_skills.py
"""

import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTABLE = ("name", "description", "license")
CODE_EXTRA = ["disable-model-invocation: true", "allowed-tools: mcp__plugin_cortex_cortex__*"]


def split(text: str) -> tuple[list[str], str]:
    """Return (frontmatter lines, body) of a SKILL.md."""
    # shortcut: assumes one-line `key: value` fields; use ruamel if multi-line values appear.
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md has no frontmatter")
    head, body = text[4:].split("\n---\n", 1)
    return head.splitlines(), body


def render(fields: list[str], body: str) -> str:
    return "---\n" + "\n".join(fields) + "\n---\n" + body


def build(src: Path, out: Path) -> tuple[Path, Path]:
    """Write <out>/plugin/skills/cortex/ (SKILL.md + modes/) and <out>/dist/cortex-skill.zip."""
    fields, body = split(src.read_text(encoding="utf-8"))
    modes = sorted((src.parent / "modes").glob("*.md"))
    code = out / "plugin" / "skills" / "cortex" / "SKILL.md"
    shutil.rmtree(code.parent / "modes", ignore_errors=True)  # drop modes removed from the source
    (code.parent / "modes").mkdir(parents=True, exist_ok=True)
    code.write_text(render(fields + CODE_EXTRA, body), encoding="utf-8", newline="\n")
    for m in modes:
        shutil.copyfile(m, code.parent / "modes" / m.name)

    portable = [f for f in fields if f.split(":", 1)[0].strip() in PORTABLE]
    zpath = out / "dist" / "cortex-skill.zip"
    zpath.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("cortex/", "")
        z.writestr("cortex/SKILL.md", render(portable, body))
        z.writestr("cortex/modes/", "")
        for m in modes:
            z.writestr(f"cortex/modes/{m.name}", m.read_bytes())
    return code, zpath


if __name__ == "__main__":
    for p in build(ROOT / "skills" / "cortex" / "SKILL.md", ROOT):
        print(p)
