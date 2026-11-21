"""R3-4: Review as a daily instrument: Today's 5 (teaching rules, pairs, repeated corrections,
then lines 2+ chats proposed; at most 5 a day, answered cards counted), a single inference
leaving the view after 14 days (inbox.md keeps it), Approve all on repeated corrections only,
and the page's keys (J/K focus, Y, N, S; never while typing)."""

import shutil

import pytest
from conftest import run
from test_app_js import HARNESS, _run
from test_review import RULE, ago, app, today  # noqa: F401  (app is a fixture)
from test_review_conflicts import chat, corrections_vault, prefs

from cortex import review
from cortex.app import server

pytestmark = pytest.mark.usefixtures("home")


def propose(root, text, times=1):
    """`text` proposed for me/preferences ## Coding by `times` chats (one server each)."""
    async def body(call):
        await call("cortex_patch", {"id": "me/preferences", "op": "append_to_section",
                                    "target": "Coding", "text": f"- {text}"})

    for _ in range(times):
        run(root, body)


def test_todays_five_order_sightings_and_the_daily_cap(root, app):  # noqa: F811
    prefs(root, "Never use tabs for indentation", "Always use tabs for indentation")
    corrections_vault(root)
    propose(root, "Likes short answers", times=2)
    propose(root, "Reads on trains")  # one chat only: a single inference

    async def rule(call):
        await call("cortex_patch", RULE)

    run(root, rule)
    view = app.review()
    by = {p["text"]: p for p in view["pending"]}
    assert by["Reads on trains"]["single"] and not by["Likes short answers"]["single"]
    assert [(t["type"], t["key"]) for t in view["today"]] == [
        ("pending", by["Quiz me before you explain"]["key"]),
        ("conflict", view["conflicts"][0]["key"]), ("rule", view["rules"][0]["key"]),
        ("pending", by["Likes short answers"]["key"])]  # never the single inference
    assert view["today_left"] == 5 and view["count"] == 5  # every board still lists it
    view = app.review_approve({"key": by["Quiz me before you explain"]["key"]})
    assert view["today_left"] == 4 and len(view["today"]) == 3
    view = app.review_conflict({"key": view["conflicts"][0]["key"], "keep": "both"})
    view = app.review_rule_dismiss({"key": view["rules"][0]["key"]})
    view = app.review_dismiss({"key": view["today"][0]["key"]})
    assert view["today_left"] == 1 and view["today"] == []
    propose(root, "Codes at night", times=2)
    propose(root, "Uses a standing desk", times=2)
    (t,) = app.review()["today"]  # two would be offered: one is left for today
    view = app.review_dismiss({"key": t["key"]})
    assert view["today_left"] == 0 and view["today"] == []  # done for today
    assert [p["text"] for p in view["pending"] if not p["single"]] != []  # still on the board


def test_a_single_inference_leaves_the_view_after_14_days(root, app):  # noqa: F811
    propose(root, "Reads on trains")
    propose(root, "Likes short answers", times=2)

    async def rule(call):
        await call("cortex_patch", RULE)

    run(root, rule)
    inbox = root / "inbox.md"
    inbox.write_text(inbox.read_text("utf-8").replace(today(), ago(review.SINGLE_DAYS + 1)),
                     encoding="utf-8")
    texts = [p["text"] for p in app.review()["pending"]]
    assert "Reads on trains" not in texts  # out of the view ...
    assert "Reads on trains" in inbox.read_text("utf-8")  # ... not out of the inbox
    assert "Likes short answers" in texts and "Quiz me before you explain" in texts


def test_approve_all_only_on_repeated_corrections(root, app):  # noqa: F811
    corrections_vault(root)
    for n, turns in enumerate((["Never answer in French"], ["Stop answering in French"])):
        chat(root, f"c-fr{n}", turns)
    rules = app.review()["rules"]
    assert len(rules) == 2
    with pytest.raises(server.NotFound):  # an unknown key writes nothing
        app.review_rule_approve_all({"keys": [rules[0]["key"], "nope"]})
    assert not (root / "me" / "core.md").exists()
    with pytest.raises(ValueError):
        app.review_rule_approve_all({"keys": "all"})
    view = app.review_rule_approve_all({"keys": [x["key"] for x in rules]})
    assert view["rules"] == []
    # RR-25: a standing rule goes to me/core, any other correction to me/preferences
    assert all(x["text"] in (root / "me" / f"{x['page']}.md").read_text("utf-8")
               for x in rules)


KEYS_JS = HARNESS[:HARNESS.index("const out = {};")] + r"""
const out = { errors };
const press = (key, target) => (winL.keydown || []).forEach((fn) => fn({ key, target: target || document.activeElement || body, preventDefault() {} }));
const todayCards = () => $('review').childNodes[0].querySelectorAll('.rcard');
(async () => {
  try {
    vm.runInThisContext(fs.readFileSync(appPath, 'utf8'), { filename: appPath });
    await tick();
    nav.querySelectorAll('button').find((b) => b.dataset.s === 'review').fire('click');
    await tick();
    out.today = todayCards().map((c) => [c.getAttribute('aria-label'), c.getAttribute('role'), c.getAttribute('tabindex')]);
    out.shortcuts = todayCards()[0].querySelectorAll('button').map((b) => [b.textContent, b.getAttribute('aria-keyshortcuts')]);
    out.allRules = $('review').querySelectorAll('button').filter((b) => b.textContent === 'Approve all').length;
    press('j'); out.f1 = document.activeElement.getAttribute('aria-label');
    press('j'); out.f2 = document.activeElement.getAttribute('aria-label');
    press('k'); out.f3 = [document.activeElement.getAttribute('aria-label'), document.activeElement.getAttribute('tabindex')];
    const typing = new Element('input');
    press('y', typing);  // typing in a field: nothing happens
    await tick();
    out.typed = fetched.filter((f) => f.method === 'POST').length;
    DATA.routes['/api/review/approve'] = DATA.after;
    press('y');
    await tick();
    out.posted = fetched.filter((f) => f.method === 'POST').map((f) => [f.path, f.body]);
    out.refocus = document.activeElement.getAttribute('aria-label');
    press('n');
    await tick();
    out.posted2 = fetched.filter((f) => f.method === 'POST').map((f) => [f.path, f.body]).slice(1);
    press('s');
    await tick();
    out.afterSkip = todayCards().map((c) => c.getAttribute('aria-label'));
  } catch (e) { errors.push(String(e && e.stack || e)); }
  out.sinks = sinks;
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""  # noqa: E501  (JavaScript)


def test_the_keys_and_todays_five_on_the_page(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    pend = {"key": "k1", "page": "teaching-contract", "section": "Rules", "text": "Quiz me",
            "since": "2026-11-20", "single": False}
    pair = {"key": "p1", "why": "update",
            "left": {"page": "preferences", "id": "c-bbbbb", "text": "Uses Vim"},
            "right": {"page": "preferences", "id": "c-ccccc", "text": "Uses Helix"}}
    rules = [{"key": f"r{n}", "text": f"Stop {n}", "chats": 2} for n in (1, 2)]
    view = {"pending": [pend], "expired": [], "stale": [], "conflicts": [pair], "rules": rules,
            "decisions": [], "forget": [], "count": 4, "today_left": 5,
            "today": [{"type": "pending", "key": "k1"}, {"type": "conflict", "key": "p1"},
                      {"type": "rule", "key": "r1"}]}
    after = {**view, "pending": [], "count": 3, "today_left": 4, "today": view["today"][1:]}
    status = {"first_run": False, "rows": [], "vault": "V", "land": "status",
              "update": {"current": "0.1.0", "mode": "installer"}}
    out = _run(node, tmp_path, "keys", KEYS_JS, {
        "routes": {"/api/review": view, "/api/status": status,
                   "/api/review/conflict": {**after, "conflicts": [], "today": after["today"][1:]}},
        "after": after, "stream": []})
    assert out["errors"] == [] and out["sinks"] == [], out["errors"]
    assert out["today"] == [["Quiz me", "group", "0"], ["Uses Vim", "group", "-1"],
                            ["You told 2 chats: Stop 1", "group", "-1"]]
    assert out["shortcuts"] == [["Approve", "Y"], ["Dismiss", "N"]]
    assert out["allRules"] == 1
    assert (out["f1"], out["f2"], out["f3"]) == ("Quiz me", "Uses Vim", ["Quiz me", "0"])
    assert out["typed"] == 0
    assert out["posted"] == [["/api/review/approve", {"key": "k1"}]]
    assert out["refocus"] == "Uses Vim"  # the next card takes the answered one's place
    assert out["posted2"] == [["/api/review/conflict", {"key": "p1", "keep": "both"}]]
    assert out["afterSkip"] == []  # S: the last Today card is skipped for this page
