"""R12 `cortex eval agent`: Layer A (no model) per case, so a regression names its case; Layer
B's pass^3 on a fake runner; the command's lines and JSON."""

import json

import pytest
from typer.testing import CliRunner

from cortex import agent_eval
from cortex.cli import app

pytestmark = pytest.mark.usefixtures("home")

SPEC = agent_eval.spec()


def _param(case):
    known = case.get("known_failure")
    return pytest.param(case, id=case["id"], marks=[pytest.mark.xfail(
        strict=True, reason=known)] if known else [])


@pytest.mark.parametrize("case", [_param(c) for c in SPEC["cases"]])
def test_layer_a(case, tmp_path):
    text, why = agent_eval.case_a(case, SPEC["base"], tmp_path / "v")
    assert not why, f"{case['id']} {case['category']} ({case['client']}): {why}\n---\n{text}"


def test_a_check_bites(tmp_path):
    """Without its mute, A08's muted core line reaches the rules block and the pack."""
    case = next(c for c in SPEC["cases"] if c["id"] == "A08")
    case = {**case, "vault": {**case["vault"], "mute": []}}
    _, why = agent_eval.case_a(case, SPEC["base"], tmp_path / "v")
    assert why == ["served: 'emoji'"]


def test_cases_are_well_formed():
    ids = [c["id"] for c in SPEC["cases"]]
    assert ids == [f"A{n:02}" for n in range(1, 12)]
    for c in SPEC["cases"]:
        assert set(c["layer_a"]) == {"must_serve", "forbid"}
        assert set(c["layer_b"]) == {"regex_all", "regex_none", "must_not_leak"}
        assert c["serve"] and c["prompt"] and c["client"] and c["category"]


class Fake:
    """A runner that answers from a list, in turn, and records what it was sent."""
    model = "fake-2b"

    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    def chat(self, system, user, **k):
        self.sent.append((system, user))
        return self.replies.pop(0) if self.replies else None


def test_layer_b_is_pass_cubed_on_the_served_text(monkeypatch):
    one = SPEC["cases"][1]  # A02: Result and ?, never .unwrap()
    monkeypatch.setitem(SPEC, "cases", [one])
    monkeypatch.setattr(agent_eval, "spec", lambda: SPEC)
    good = "```rust\nfn read() -> Result<Config, Error> { let s = fs::read_to_string(p)?; }\n```"
    bad = good + "\nlet n = s.parse::<u32>().unwrap();"
    fake = Fake([good, good, good])
    r = agent_eval.run(model=True, runner=fake)
    assert r["cases"][0]["b"] == 3 and r["b_pass"] == 1 and r["model"] == "fake-2b"
    system, user = fake.sent[0]
    assert user == one["prompt"] and "Never use .unwrap() in Rust" in system
    r = agent_eval.run(model=True, runner=Fake([good, bad, good]))
    row = r["cases"][0]
    assert row["b"] == 2 and r["b_pass"] == 0
    assert row["b_why"] == [f"matched {one['layer_b']['regex_none'][0]!r}"]
    assert agent_eval.check_b(None, one) == ["no answer"]
    assert "B 2/3" in agent_eval.summary(r) and "Layer B: 0/1 pass^3 on fake-2b" in \
        agent_eval.summary(r)


def test_command_runs_layer_a_only_and_never_looks_for_a_model(monkeypatch):
    from cortex import chat_eval

    monkeypatch.setattr(chat_eval, "find_model", lambda: pytest.fail("looked for a model"))
    res = CliRunner().invoke(app, ["eval", "agent"])
    assert res.exit_code == 0, res.output
    lines = res.output.splitlines()
    assert [ln.split()[0] for ln in lines[:11]] == [c["id"] for c in SPEC["cases"]]
    assert all("B not run" in ln for ln in lines[:11])
    assert "Layer B: not run (no --model)" in res.output
    res = CliRunner().invoke(app, ["eval", "agent", "--json"])
    r = json.loads(res.output)
    assert len(r["cases"]) == 11 and r["b_pass"] is None and r["runs"] == 0


def test_command_with_model_and_none_loaded_says_why(monkeypatch):
    from cortex import chat_eval

    monkeypatch.setattr(chat_eval, "find_model", lambda: (None, "no local model loaded"))
    res = CliRunner().invoke(app, ["eval", "agent", "--model"])
    assert "Layer B: not run (no local model loaded)" in res.output
