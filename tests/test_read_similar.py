"""RR-23: keepfold_read's `similar`, from head vectors already in memory only."""

import numpy as np
import pytest
from conftest import note

from keepfold import semantic
from keepfold.mcp_server import READ_SIMILAR, Keepfold
from keepfold.vault import Vault, VaultError

pytestmark = pytest.mark.usefixtures("home")


def unit(*xs):
    v = np.array([*xs, *[0.0] * (8 - len(xs))], np.float32)
    return v / np.linalg.norm(v)


def test_similar_lists_close_unlinked_public_pages(root, monkeypatch):
    note(root, "notes/a.md", "title: Alpha\n", "# Alpha\n\nSee [[b]].")
    note(root, "notes/b.md", "title: Beta\n", "# Beta\n")
    for id in ("c", "d", "e", "f"):
        note(root, f"notes/{id}.md", f'title: "{id.upper()} <b>x</b>"\n', f"# {id}\n")
    note(root, "notes/p.md", "title: Private twin\nprivate: true\n", "# p\n")
    c = Keepfold(Vault(root))
    assert "similar" not in c.keepfold_read("a")  # nothing in memory: never loads a model
    assert semantic._embedder is None

    vecs = {"a": unit(1), "b": unit(1), "p": unit(1), "c": unit(1, 0.1), "d": unit(1, 0.2),
            "e": unit(1, 0.3), "f": unit(0, 1)}  # f: far below MIN_COS
    cache = {i: (None, True, np.array([0, 5], np.int32), np.stack([v, unit(0, 0, 1)]))
             for i, v in vecs.items()}
    monkeypatch.setattr(semantic, "_MEM", {c.vault.root: (None, cache, None)})
    got = c.keepfold_read("a")["similar"]
    assert [s["id"] for s in got] == ["c", "d", "e"] and len(got) == READ_SIMILAR
    assert got[0]["title"] == "C x"  # plain text: no markup
    assert "a" not in [s["id"] for s in c.keepfold_read("b").get("similar", [])]  # linked
    assert "p" not in [s["id"] for s in c.keepfold_read("c")["similar"]]  # private
    assert "similar" not in c.keepfold_read("f")
    with pytest.raises(VaultError):
        c.keepfold_read("p")
