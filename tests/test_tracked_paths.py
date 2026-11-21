"""RR-11: the code repo never tracks the owner's private data or the local-only design
history. `git ls-files` lists names only, so this never reads those paths' contents; it skips
when git or the repo is not there (an sdist, a zip download)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from cortex._proc import NO_WINDOW

ROOT = Path(__file__).resolve().parent.parent


def _forbidden(path: str) -> bool:
    p = path.replace("\\", "/")
    return (p.startswith(("mydata/", "docs/design/")) or "learnervault" in p.lower()
            or p == "tests/.private_patterns")


def test_no_private_path_is_tracked():
    git = shutil.which("git")
    if not git:
        pytest.skip("git is not installed")
    out = subprocess.run([git, "-C", str(ROOT), "ls-files", "-z"], capture_output=True,
                         creationflags=NO_WINDOW)
    if out.returncode:
        pytest.skip("not a git checkout")
    tracked = out.stdout.decode("utf-8", "replace").split("\0")
    bad = [p for p in tracked if p and _forbidden(p)]
    assert not bad, f"private paths are tracked (git rm --cached them): {bad[:10]}"


def test_the_rule_catches_each_kind():
    assert all(map(_forbidden, ["mydata/x.json", "docs/design/a.md", "D/LearnerVault/n.md",
                                "x/learnervault.zip", "tests/.private_patterns"]))
    assert not any(map(_forbidden, ["docs/GUIDE.md", "tests/fixtures/vault_mini/a.md",
                                    "src/mydata.py"]))
