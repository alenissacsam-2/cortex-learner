"""Report a problem: the issue forms, the redaction helper, the text Status's Copy details and
`keepfold doctor --report` share (no paths, user name or notes), its GET route and the Status
board's two buttons (app.js under node)."""

import json
import shutil
from pathlib import Path

import pytest
import yaml
from test_app_js import HARNESS, _run, _sweep_routes
from typer.testing import CliRunner

from keepfold import localllm, update
from keepfold.app import actions, server, status, targets
from keepfold.cli import app as cli

ROOT = Path(__file__).parent.parent
FORMS = ROOT / ".github" / "ISSUE_TEMPLATE"
USER = "zorblax"


# ---- the issue forms ---------------------------------------------------------------------

def _form(name):
    return yaml.safe_load((FORMS / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name,label", [("bug_report.yml", "bug"),
                                        ("feature_request.yml", "enhancement")])
def test_issue_forms_follow_github_schema(name, label):
    f = _form(name)
    assert f["name"] and f["description"] and f["labels"] == [label]
    ids = []
    for item in f["body"]:
        assert item["type"] in {"markdown", "textarea", "input", "dropdown", "checkboxes"}
        if item["type"] == "markdown":
            assert item["attributes"]["value"]
            continue
        ids.append(item["id"])
        assert item["attributes"]["label"]
        assert isinstance(item["validations"]["required"], bool)
    assert len(ids) == len(set(ids)) and ids
    if name == "bug_report.yml":
        assert {"what-happened", "expected", "steps", "details", "windows", "install"} <= set(ids)
        assert "never paste" in f["body"][0]["attributes"]["value"]
        assert "Copy details" in f["body"][0]["attributes"]["value"]


def test_issue_config_blocks_blank_issues_and_points_to_security():
    c = _form("config.yml")
    assert c["blank_issues_enabled"] is False
    link = c["contact_links"][0]
    assert link["url"] == f"https://github.com/{update.REPO}/blob/main/SECURITY.md"
    assert link["name"] and link["about"]


# ---- redaction ---------------------------------------------------------------------------

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A home whose path holds a fake user name, with a copy of the fixture vault in it, and no
    runner, process or network."""
    home = tmp_path / "Users" / USER
    vault = home / "My Notes"
    shutil.copytree(Path(__file__).parent / "fixtures" / "vault_mini", vault)
    (vault / ".keepfold").mkdir(exist_ok=True)
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setenv("USERNAME", USER)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    def no_runner(*a, **k):
        raise OSError("no runner in tests")

    monkeypatch.setattr(localllm, "_request", no_runner)
    monkeypatch.setattr(localllm, "_lms_exe", lambda cfg: None)
    monkeypatch.setattr(targets, "_which", lambda c: None)
    monkeypatch.setattr(actions, "_popen", lambda *a, **k: pytest.fail("started a process"))
    monkeypatch.setattr(status, "ollama_models", lambda: None)
    monkeypatch.setattr(status, "lms_models", lambda: [])
    status.forget()
    return home, vault


def test_redact_removes_paths_user_name_tokens_and_keys(fake_home):
    home, vault = fake_home
    text = "\n".join([
        f"home {home}", f"vault {vault}", f"fwd {str(vault).replace(chr(92), '/')}/x.md",
        r"drive D:\LearnerVault\notes\a.md and c:/Other Dir/x", "/Users/someone/Docs/a.txt",
        f"user {USER} and {USER.upper()}", "https://x.test/cb?a=1&token=abc123&b=2",
        "key sk-ant-abcdefghijkl123456"])
    out = status.redact(text, vault)
    for gone in (str(home), str(vault), "LearnerVault", "Other Dir", "someone", USER,
                 USER.upper(), "abc123", "abcdefghijkl"):
        assert gone not in out, (gone, out)
    assert out.count("<path>") >= 6 and "<user>" in out and "token=<redacted>" in out
    assert "b=2" in out and "<key>" in out


def test_report_holds_no_path_user_name_or_notes(fake_home):
    home, vault = fake_home
    out = status.report(vault)
    notes = [p.read_text(encoding="utf-8") for p in vault.rglob("*.md")]
    assert notes
    for gone in (str(home), str(vault), USER, "My Notes", "Users"):
        assert gone not in out, (gone, out)
    for n in notes:
        words = [w for w in n.split() if len(w) > 8 and w.isalpha()]
        assert not any(w in out for w in words[:20])
    assert out.startswith(f"Keepfold {update.current()} (")
    assert "Status:\n- Vault: Ready" in out and "- Local model: off" in out
    assert "Optional parts:" in out and "Browsing import: no" in out
    assert "Python 3." in out


def test_report_survives_missing_rows(fake_home, monkeypatch):
    home, vault = fake_home
    monkeypatch.setattr(status, "rows", lambda root: [
        {"id": "vault", "label": "Vault", "state": "ok", "value": "x"}])
    out = status.report(vault)
    assert "- Vault: Ready" in out and "- Search model: off" in out
    assert "- Local model: off" in out and "- Browsing import: no" in out


def test_status_warns_when_an_ai_app_names_the_old_vault(fake_home, monkeypatch):
    """FX4: an app's Keepfold entry still names the old vault folder (stale): the AI apps row
    warns and opens Connect, and the report says it needs attention."""
    from keepfold import clients
    home, vault = fake_home
    monkeypatch.setattr(clients, "mcp_command", lambda: "keepfold")
    (home / ".codeium/windsurf").mkdir(parents=True)
    targets.apply_all({"windsurf": True}, clients.server_entry("keepfold", home / "old vault"))
    apps = next(r for r in status.rows(vault) if r["id"] == "apps")
    assert apps["state"] == "warn" and apps["action"] == {"go": "connect", "label": "Open"}
    assert apps["detail"] == ("An AI app still uses your old vault folder. Open Connect AI apps "
                              "and click Apply.")
    assert "- AI apps: Needs attention" in status.report(vault)
    targets.apply_all({"windsurf": True}, clients.server_entry("keepfold", vault))
    status.forget()
    assert next(r for r in status.rows(vault) if r["id"] == "apps")["state"] == "ok"


def test_route_is_a_read_only_get_and_equals_doctor_report(fake_home):
    home, vault = fake_home
    a = server.App(vault)
    assert "/api/status/report" in a.get
    assert "/api/status/report" not in getattr(a, "post", {})
    before = sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns)
                    for p in vault.rglob("*") if p.is_file())
    text = a.get["/api/status/report"]()["text"]
    assert sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in vault.rglob("*") if p.is_file()) == before
    r = CliRunner().invoke(cli, ["doctor", "--report", "--vault", str(vault)])
    assert r.exit_code == 0, r.output
    assert r.output.strip() == text.strip()
    assert a.status()["issue_url"] == (
        f"https://github.com/{update.REPO}/issues/new?template=bug_report.yml")


# ---- the Status board (app.js) -----------------------------------------------------------

BOARD = HARNESS[:HARNESS.index("const out = {};")] + r"""
const out = { errors };
(async () => {
  try {
    vm.runInThisContext(fs.readFileSync(appPath, 'utf8'), { filename: appPath });
    await tick();
    nav.querySelectorAll('button').find((b) => b.dataset.s === 'status').fire('click');
    await tick();
    const b = $('status').querySelectorAll('.board').find((x) => x.querySelector('h2') && x.querySelector('h2').textContent === 'Something not working?');
    out.board = !!b;
    out.help = b.querySelector('.sub').textContent;
    const copy = b.querySelectorAll('button').find((x) => x.textContent.includes('Copy details'));
    out.copyLabel = copy.textContent;
    const gets = () => fetched.filter((f) => f.path === '/api/status/report').map((f) => f.method || 'GET');
    out.getsBefore = gets();  // fetched with the board, before any click
    copy.fire('click');
    await tick();
    out.copied = clipboard.slice();
    out.gets = gets();
    // a blocked copy: the kept text, read-only and selected, with how to copy it by hand
    Element.prototype.select = function () { this.selected = true; };
    DATA.fail = true;
    copy.fire('click');
    await tick();
    const area = b.querySelector('textarea');
    out.blocked = area && [area.value, area.readonly === true || area.getAttribute('readonly') !== null,
      area.selected === true, document.activeElement === area, b.querySelector('.report-text').querySelector('p').textContent];
    out.blockedToast = $('toast-msg').textContent;
    out.getsAfter = gets().length;
    const a = b.querySelectorAll('a').find((x) => x.textContent === 'Report on GitHub');
    out.href = a.getAttribute('href');
    out.attrs = [a.getAttribute('target'), a.getAttribute('rel')];
  } catch (e) { errors.push(String(e && e.stack || e)); }
  out.sinks = sinks;
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""  # noqa: E501  (JavaScript)


def test_status_board_copies_the_servers_text_and_links_the_issue_form(root_committed, tmp_path,
                                                                       monkeypatch):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    routes = _sweep_routes(root_committed, monkeypatch)
    routes["/api/status/report"] = {"text": "Keepfold 9.9.9 (uv)\nStatus:\n- Vault: Ready"}
    out = _run(node, tmp_path, "report", BOARD, {"routes": routes, "stream": []})
    assert out["errors"] == [] and out["sinks"] == [], out["errors"]
    assert out["board"] and out["copyLabel"] == "Copy details"
    assert out["help"] == ("Copy details, then paste them into the form. Nothing is sent "
                           "until you submit it on GitHub.")
    text = routes["/api/status/report"]["text"]
    assert out["getsBefore"] == ["GET"]  # the click only writes the clipboard
    assert out["copied"] == [text] and out["gets"] == ["GET"]
    assert out["blocked"] == [text, True, True, True, "Select the text and press Ctrl+C."]
    assert "terminal" not in out["blockedToast"] and out["getsAfter"] == 1
    assert out["href"] == f"https://github.com/{update.REPO}/issues/new?template=bug_report.yml"
    assert out["attrs"] == ["_blank", "noopener noreferrer"]
    assert json.dumps(routes["/api/status"]["issue_url"]) == json.dumps(out["href"])
