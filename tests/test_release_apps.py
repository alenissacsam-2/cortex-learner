"""release-apps.yml and its size budget, checked offline: the workflow parses, runs on a published
release and by hand for a tag, has the Mac and Ubuntu jobs, may only write release files, and pins
every action to a commit as the other workflows do; packaging/sizes.json and check_sizes.py."""

import json
import re
import sys
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
FLOW = WORKFLOWS / "release-apps.yml"
PINNED = re.compile(r"^\s*(?:- )?uses: ([\w.-]+/[\w.-]+)@([0-9a-f]{40}) # v\d+(?:\.\d+)*$")
sys.path.insert(0, str(ROOT / "packaging"))
import check_sizes  # noqa: E402


def _flow() -> dict:
    return YAML(typ="safe").load(FLOW.read_text(encoding="utf-8"))


def test_triggers_jobs_and_permissions():
    flow = _flow()
    assert flow["on"]["release"] == {"types": ["published"]}
    assert flow["on"]["workflow_dispatch"]["inputs"]["tag"]["required"] is True
    assert flow["permissions"] == {"contents": "write"}
    jobs = flow["jobs"]
    assert sorted(j["runs-on"] for j in jobs.values()) == ["macos-26", "ubuntu-24.04"]
    assert all("permissions" not in j for j in jobs.values())  # nothing widens it per job
    for j in jobs.values():
        steps = [s.get("run", "") for s in j["steps"]]
        assert any("gh release upload" in s for s in steps)
        assert any("smoke_cli.sh" in s for s in steps) and any("smoke_gui.sh" in s for s in steps)
        assert any("check_sizes.py" in s for s in steps)
        # the dispatch input reaches scripts only through $TAG, never pasted into a script
        assert not any("inputs." in s for s in steps)


def test_every_action_is_pinned_like_the_other_workflows():
    seen: dict[str, set] = {}
    for f in WORKFLOWS.glob("*.yml"):
        for line in f.read_text(encoding="utf-8").splitlines():
            if "uses:" in line:
                m = PINNED.match(line)
                assert m, f"{f.name}: not pinned to a commit with its version: {line.strip()}"
                seen.setdefault(m[1], set()).add(m[2])
    assert {a: s for a, s in seen.items() if len(s) > 1} == {}  # one commit per action
    uses = re.findall(r"uses: ([\w.-]+/[\w.-]+)@", FLOW.read_text(encoding="utf-8"))
    assert "actions/checkout" in uses and "actions/upload-artifact" in uses


def test_sizes_json_has_a_baseline_slot_per_release_file():
    sizes = json.loads((ROOT / "packaging" / "sizes.json").read_text(encoding="utf-8"))
    assert {k for k in sizes if not k.startswith("_")} == {
        "Keepfold-<v>-macos-arm64.dmg", "Keepfold-<v>-macos-arm64.zip",
        "keepfold_<v>_amd64.deb", "keepfold-<v>-linux-x86_64.tar.gz"}
    assert all(v is None or (isinstance(v, int) and v > 0)
               for k, v in sizes.items() if not k.startswith("_"))


def test_size_budget(tmp_path):
    def f(name, size):
        p = tmp_path / name
        p.write_bytes(b"x" * size)
        return str(p)
    sizes = {"a-<v>.zip": 100, "b-<v>.deb": None}
    out = check_sizes.check("1.2.3", [f("a-1.2.3.zip", 125), f("b-1.2.3.deb", 999)], sizes)
    assert out[0].startswith("ok") and out[1].startswith("skip")  # +25% exactly passes
    out = check_sizes.check("1.2.3", [f("a-1.2.3.zip", 126), f("c-1.2.3.tar.gz", 1)], sizes)
    assert out[0].startswith("FAIL") and "+26%" in out[0]
    assert out[1].startswith("FAIL") and "c-<v>.tar.gz" in out[1]  # a file with no key
