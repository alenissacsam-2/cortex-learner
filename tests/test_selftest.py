"""`keepfold selftest`: every module imports and every package data file reads (the frozen apps'
smoke test, release-apps.yml)."""

import re
from pathlib import Path

from typer.testing import CliRunner

from keepfold import selftest
from keepfold.cli import app

runner = CliRunner()
SRC = Path(selftest.__file__).parent


def test_selftest_passes_from_source(home):
    r = runner.invoke(app, ["selftest"])
    assert r.exit_code == 0, r.output
    n, m = map(int, re.fullmatch(r"selftest ok (\d+) modules (\d+) files\n", r.output).groups())
    on_disk = [p for p in SRC.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    assert n == sum(p.suffix == ".py" for p in on_disk) - 1  # keepfold/__init__.py is the root
    assert m == sum(p.suffix not in (".py", ".pyc") for p in on_disk) > 0
    assert not any(home.iterdir())  # it opens no home and no vault


class Fake:
    """A resource tree with one file listed that cannot be read (a file the freeze missed)."""

    def __init__(self, name, children=()):
        self.name, self.children = name, children

    def iterdir(self):
        return iter(self.children)

    def is_dir(self):
        return bool(self.children)

    def read_bytes(self):
        raise FileNotFoundError(2, "No such file", self.name)


def test_selftest_reports_missing_data_file(home, monkeypatch):
    tree = Fake("keepfold", [Fake("assets", [Fake("app.js")])])
    monkeypatch.setattr(selftest, "files", lambda package: tree)
    r = runner.invoke(app, ["selftest"])
    assert r.exit_code == 1
    assert "selftest failed: data file keepfold/assets/app.js" in r.output
    assert "selftest ok" not in r.output
