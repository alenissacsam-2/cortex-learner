"""v5 PHASE4 V5: share tiers. (me/delegation.md, the delegate role, was removed on 20 Nov 2026:
`keepfold init` no longer writes it.)"""

import pytest
from typer.testing import CliRunner

from keepfold import people, share
from keepfold.cli import app
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")


def test_init_writes_no_delegation_page(tmp_path):
    r = CliRunner().invoke(app, ["init", str(tmp_path / "v")])
    assert r.exit_code == 0, r.output
    assert not (tmp_path / "v" / "me" / "delegation.md").exists()


@pytest.mark.parametrize("audience,tier", [
    ("friend", "close"), ("family", "close"), ("partner", "close"), ("professor", "work"),
    ("recruiter", "work"), ("client", "work"), ("public", "public"), ("stranger", "work"),
    ("people/asha", "close"), ("people/rao", "public"), ("people/nobody", "work")])
def test_tier(tmp_path, audience, tier):
    v = Vault(tmp_path)
    people.add(v, "Asha", {"relation": "friend", "tier": "close"})
    people.add(v, "Rao", {"relation": "professor", "tier": "public"})
    assert share.tier(v, audience) == tier


def test_allowed(tmp_path):
    v = Vault(tmp_path)
    (tmp_path / "me").mkdir()
    for page, s in (("goals", "close"), ("profile", "work"), ("setup", "public"),
                    ("preferences", "self"), ("personality", None), ("portrait", "bogus")):
        fm = f"share: {s}\n" if s else ""
        (tmp_path / "me" / f"{page}.md").write_bytes(f"---\n{fm}---\n# {page}\n".encode())
    people.add(v, "Asha", {"relation": "friend", "tier": "close"})
    assert share.allowed(v, "friend") == {"goals", "profile", "setup"}
    assert share.allowed(v, "people/asha") == {"goals", "profile", "setup"}
    assert share.allowed(v, "professor") == {"profile", "setup"}
    assert share.allowed(v, "someone") == {"profile", "setup"}
    assert share.allowed(v, "public") == {"setup"}
