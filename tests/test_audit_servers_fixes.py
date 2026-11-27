"""Audit 27 Nov (servers): a private raw source is matched by file, not by path spelling
(finding 3)."""

import os

import pytest
from conftest import note

from keepfold.mcp_server import Keepfold
from keepfold.vault import Vault, VaultError

pytestmark = pytest.mark.usefixtures("home")


def test_a_private_raw_source_is_refused_under_another_path_to_the_same_file(root):
    """macOS's APFS is case-insensitive while Path == is not, so raw/DIARY.md reached
    raw/diary.md's text. A hard link is that case on every OS: another path, the same file."""
    note(root, "resources/diary-src.md", 'title: "Diary"\nprivate: true\n'
         'source: "raw/diary.md"\n', "# Diary")
    (root / "raw").mkdir(exist_ok=True)
    (root / "raw/diary.md").write_text("my diary", encoding="utf-8")
    os.link(root / "raw/diary.md", root / "raw/other-name.md")
    (root / "raw/open.md").write_text("open text", encoding="utf-8")
    c = Keepfold(Vault(root))
    for rel in ("raw/diary.md", "raw/other-name.md"):
        with pytest.raises(VaultError) as e:
            c.keepfold_read(rel)
        assert e.value.code == "unknown_id"
    assert "open text" in c.keepfold_read("raw/open.md")["text"]

