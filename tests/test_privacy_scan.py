"""Shipped files carry no machine- or person-specific data: no real user folder, drive path,
private vault path, Claude Store package id or email address."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# every shipped path; tests/fixtures hold made-up addresses on purpose, so they stay out
SCOPE = ["src", "plugin", "skills", "scripts", "extension", "installer", "docs", "README.md",
         "CLAUDE.md", "filemap.md", "pyproject.toml", ".claude-plugin"]
PATTERNS = {
    "D: drive path": re.compile(r"\bD:[\\/]"),
    "private vault path": re.compile(r"([A-Za-z]:|Users)[\\/]+([^<\s\\/]+[\\/]+)?LearnerVault"),
    "email address": re.compile(
        r"[\w.+-]+@(?!example\.(com|org|invalid)\b)[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
}
# a maintainer's own names, user folder and app ids: one regex per line in a git-ignored file,
# so the public repo never spells them out
LOCAL = ROOT / "tests" / ".private_patterns"
if LOCAL.is_file():
    PATTERNS |= {f"local pattern {i}": re.compile(line.strip())
                 for i, line in enumerate(LOCAL.read_text(encoding="utf-8").splitlines(), 1)
                 if line.strip() and not line.startswith("#")}


def tracked() -> list[Path]:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("needs git and a checkout")
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", *SCOPE],
                         capture_output=True, text=True, check=True).stdout
    return [ROOT / p for p in out.split("\0") if p]


def test_shipped_files_have_no_private_data():
    hits = []
    for path in tracked():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary assets
        for name, rx in PATTERNS.items():
            for m in rx.finditer(text):
                hits.append(f"{path.relative_to(ROOT)}: {name}: {m.group(0)}")
    assert hits == []


def test_patterns_catch_examples():
    bad = [r"D:\LearnerVault", r"C:\Users\bob\LearnerVault", "someone@gmail.com"]
    ok = ["~/LearnerVault", r"C:\Users\<you>\LearnerVault", "test@example.invalid",
          "cortex@cortex", "git+https://github.com/<owner>/<repo>@<tag>", "galenic"]
    assert all(any(rx.search(s) for rx in PATTERNS.values()) for s in bad)
    assert not any(rx.search(s) for rx in PATTERNS.values() for s in ok)


def test_private_folders_not_tracked():
    tracked()  # skips without git
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--", "mydata", "dist",
                          "docs/design"],
                         capture_output=True, text=True, check=True).stdout
    assert out == ""
