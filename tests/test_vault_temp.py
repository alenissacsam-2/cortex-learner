"""A vault in the system temp folder warns (Dashboard and vault, Status) and `_init` never lets
one replace the vault the owner already uses. "Temp" is a tmp_path subfolder, never the real one."""

import os
import tempfile
from pathlib import Path

import pytest

from keepfold import cli, taint
from keepfold.app import server, status
from keepfold.vault import in_temp_folder


@pytest.fixture
def systemp(tmp_path, monkeypatch):
    t = tmp_path / "systemp"
    t.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(t))
    return t


def test_in_temp_folder_compares_path_parts(systemp):
    assert in_temp_folder(systemp / "vault") and in_temp_folder(systemp)
    assert not in_temp_folder(systemp.parent / "systemp2" / "vault")  # not a string prefix
    assert not in_temp_folder(systemp.parent / "vault")
    if os.name == "nt":
        assert in_temp_folder(str(systemp / "Vault").upper())


def test_init_keeps_the_real_vault_when_a_temp_folder_asks(tmp_path, systemp):
    real, temp = tmp_path / "real", systemp / "demo"
    cli._init(real)  # no vault yet: the first setup, whatever it is
    assert Path(taint.config()["vault"]) == real
    _, _, warned = cli._init(temp, default=True)
    assert Path(taint.config()["vault"]) == real
    assert warned and warned[0].startswith(f"Kept your vault {real}")
    assert "Pick a folder" in warned[0]  # outside Temp (Windows), in your home folder (else)


def test_init_first_setup_in_a_temp_folder_becomes_the_default(systemp):
    cli._init(systemp / "first", default=True)
    assert Path(taint.config()["vault"]) == systemp / "first"


def test_init_moves_between_two_ordinary_folders_as_before(tmp_path, systemp):
    a, b = tmp_path / "a", tmp_path / "b"
    cli._init(a)
    _, _, warned = cli._init(b, default=True)
    assert Path(taint.config()["vault"]) == b and not warned
    cli._init(systemp / "t", default=True)  # a temp vault replaces an ordinary one only if asked
    assert Path(taint.config()["vault"]) == b


def test_vault_and_status_flag_a_temp_vault(tmp_path, systemp):
    ok, temp = tmp_path / "real", systemp / "demo"
    for r in (ok, temp):
        cli._init(r)
    assert server.App(ok).vault()["temp"] is False
    assert server.App(temp).vault()["temp"] is True
    row = next(r for r in status.rows(ok) if r["id"] == "vault")
    assert row["state"] == "ok"
    row = next(r for r in status.rows(temp) if r["id"] == "vault")
    assert row["state"] == "warn" and row["detail"] == status.TEMP_VAULT
    assert row["action"] == {"go": "vault", "label": "Change folder"}
