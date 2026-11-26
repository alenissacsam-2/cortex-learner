"""In-process parse caches (vault notes, catalog.yaml): invalidated by (mtime_ns, size)."""

import os
import shutil
from pathlib import Path

import pytest

from keepfold.catalog import Catalog
from keepfold.vault import Vault

FIXTURE = Path(__file__).parent / "fixtures" / "vault_mini"


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    return Vault(root)


def _rewrite(path: Path, old: str, new: str) -> None:
    """Edit the file outside Keepfold and move its mtime on, as an editor save would."""
    st = path.stat()
    path.write_bytes(path.read_bytes().replace(old.encode(), new.encode()))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_note_cache_sees_edits(v):
    path = v.path_for("concept", "arrays-basics")
    assert v.read("concept", "arrays-basics").fields["title"] == "Array basics"
    _rewrite(path, "Array basics", "Array bases!")  # same size: only the mtime differs
    assert v.read("concept", "arrays-basics").fields["title"] == "Array bases!"
    _rewrite(path, "Array bases!", "Arrays, longer title")
    assert [n.fields["title"] for n in v.iter_notes("concept") if n.id == "arrays-basics"] == \
        ["Arrays, longer title"]


def test_note_cache_hands_out_copies(v):
    v.read("concept", "arrays-basics").fields["title"] = "scribbled"
    assert v.read("concept", "arrays-basics").fields["title"] == "Array basics"


def test_catalog_cache_sees_edits_and_writers_keep_it_clean(v):
    reader = Catalog.load(v)
    _rewrite(v.root / "catalog.yaml", '"Array basics"', '"Array bases!"')
    assert Catalog.load(v).entries["arrays-basics"].title == "Array bases!"
    shared = Catalog.load(v)
    Catalog.load(v).alias("arrays-basics", "zzalias")  # a writer edits a private copy
    assert "zzalias" not in str(shared._data) and "zzalias" not in str(reader._data)
    assert "zzalias" in Catalog.load(v).entries["arrays-basics"].aliases
