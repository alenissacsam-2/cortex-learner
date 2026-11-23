"""`cortex doctor --security` (cortex.redteam): the five probes PASS on this code, and each one
really detects its hole: with the one guard it depends on turned off, that probe and only that
probe FAILs. The run builds its own made-up vaults in a temp folder it deletes."""

import os
import re
import sqlite3
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cortex import claims, cli, dashboard, prime, redteam, review, tags, vecstore
from cortex.browse import store

IDS = [p[0] for p in redteam.PROBES]


def _statuses(probes) -> dict:
    return {p.id: p.status for p in probes}


def test_every_probe_passes_and_the_temp_folder_and_env_are_left_as_found(monkeypatch):
    made = []
    real = redteam.tempfile.mkdtemp
    monkeypatch.setattr(redteam.tempfile, "mkdtemp",
                        lambda **k: made.append(real(**k)) or made[-1])
    before = {k: os.environ.get(k) for k in redteam._ENV}
    probes = redteam.run()
    assert [p.line() for p in probes] == [f"PASS  {i} {n}" for i, n, _, _ in redteam.PROBES]
    assert len(made) == 1 and not Path(made[0]).exists()
    assert {k: os.environ.get(k) for k in redteam._ENV} == before


def _secure_delete_off(monkeypatch):
    def off(real):
        def connect(*a, **k):
            con = real(*a, **k)
            con.execute("PRAGMA secure_delete=OFF")
            return con
        return connect
    monkeypatch.setattr(vecstore, "_open", off(vecstore._open))
    monkeypatch.setattr(store, "connect", off(store.connect))


HOLES = {  # probe -> the one guard turned off
    "P1": lambda mp: mp.setattr(claims, "_IMAGE", re.compile(r"(?!x)x")),
    "P1-card": lambda mp: mp.setattr(dashboard, "plain", lambda text, limit=120: str(text)),
    "P2": lambda mp: mp.setattr(tags, "promo", lambda text: False),
    "P3": lambda mp: mp.setattr(prime, "withheld_playbooks", lambda vault, ids, evs: set()),
    "P4": lambda mp: mp.setattr(review, "outside_lines", lambda vault, events: []),
    "P5": _secure_delete_off,
}


@pytest.mark.slow
@pytest.mark.parametrize("hole", HOLES)
def test_each_probe_fails_when_its_guard_is_off(hole, monkeypatch):
    HOLES[hole](monkeypatch)
    probes = redteam.run()
    probe = hole.split("-")[0]
    assert _statuses(probes) == {i: "FAIL" if i == probe else "PASS" for i in IDS}
    bad = next(p for p in probes if p.id == probe)
    assert bad.why and "probe setup" not in bad.why and "crashed" not in bad.why, bad.why


def test_secure_delete_is_what_p5_checks(tmp_path):
    """The P5 negative's premise: without secure_delete a deleted row's bytes stay on disk."""
    p = tmp_path / "x.db"
    con = sqlite3.connect(p)
    con.execute("PRAGMA secure_delete=OFF")
    con.execute("CREATE TABLE t(v TEXT)")
    con.execute("INSERT INTO t VALUES ('CANARY-SELF-TEST')")
    con.commit()
    con.execute("DELETE FROM t")
    con.commit()
    con.close()
    assert b"CANARY-SELF-TEST" in p.read_bytes()


def test_doctor_security_prints_a_line_per_probe_and_exits_1_on_a_fail(monkeypatch):
    probes = [redteam.Probe(i, n, a, ok=i != "P3", why=None if i != "P3" else "a guard")
              for i, n, a, _ in redteam.PROBES]
    monkeypatch.setattr(redteam, "run", lambda now=None: probes)
    r = CliRunner().invoke(cli.app, ["doctor", "--security"])
    assert r.exit_code == 1, r.output
    assert "PASS  P1 image exfiltration\n" in r.output
    assert "FAIL  P3 MemoryGraft playbook: a guard\n" in r.output
    assert r.output.rstrip().endswith("4 of 5 attacks stopped; 1 got through.")
    for p in probes:
        p.ok = True
    r = CliRunner().invoke(cli.app, ["doctor", "--security"])
    assert r.exit_code == 0 and r.output.rstrip().endswith("5 of 5 attacks stopped.")
    assert "Python" not in r.output  # not the plain doctor's checks


@pytest.mark.perf
def test_the_red_team_takes_under_ten_seconds():
    redteam.run()  # imports warm
    t = time.monotonic()
    assert all(p.ok for p in redteam.run())
    assert time.monotonic() - t < 10
