
from typer.testing import CliRunner

from keepfold import doctor
from keepfold.cli import ME_PAGES, app
from keepfold.contract import scope_contract

TEXT = """# Contract

- core rule

## Domain overrides

<!-- ### <domain id> -->

### dsa
dsa rule

### ml
ml rule

## After
tail
"""


def test_scope_matches_and_drops():
    out = scope_contract(TEXT, {"dsa"})
    assert "dsa rule" in out and "ml rule" not in out and "tail" in out and "core rule" in out
    assert "## Domain overrides" in out
    both = scope_contract(TEXT, {"dsa", "ml"})
    assert "dsa rule" in both and "ml rule" in both
    none = scope_contract(TEXT, set())
    assert "Domain overrides" not in none and "rule\n" in none and "tail" in none


def test_scope_without_overrides_is_unchanged():
    t = "# C\n\n- a\n\n### dsa\nx\n"
    assert scope_contract(t, set()) == t


def test_init_writes_me_pages_once(tmp_path, home):
    root = tmp_path / "v"
    (root / "me").mkdir(parents=True)
    (root / "me/goals.md").write_text("mine\n", encoding="utf-8")
    r = CliRunner().invoke(app, ["init", str(root)])
    assert r.exit_code == 0, r.output
    assert (root / "me/goals.md").read_text(encoding="utf-8") == "mine\n"
    for rel in ME_PAGES:
        assert (root / rel).is_file()
    assert "## Energy and routines" in (root / "me/personality.md").read_text(encoding="utf-8")
    assert "## Domain overrides" in (root / "me/teaching-contract.md").read_text(encoding="utf-8")
    (root / "me/setup.md").write_text("edited\n", encoding="utf-8")
    assert CliRunner().invoke(app, ["init", str(root)]).exit_code == 0
    assert (root / "me/setup.md").read_text(encoding="utf-8") == "edited\n"


def test_doctor_contract_size(tmp_path):
    assert doctor.check_contract_size(tmp_path)[0] == "PASS"
    (tmp_path / "me").mkdir()
    (tmp_path / "me/teaching-contract.md").write_text("x" * 4800, encoding="utf-8")
    assert doctor.check_contract_size(tmp_path)[0] == "PASS"  # same cap as keepfold_start
    (tmp_path / "me/teaching-contract.md").write_text("x" * 4801, encoding="utf-8")
    assert doctor.check_contract_size(tmp_path)[0] == "WARN"
