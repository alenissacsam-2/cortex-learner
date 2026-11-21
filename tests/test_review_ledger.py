"""RR-10: the per-producer accept/reject ledger. Review counts each producer's clicked
answers from change events; one whose rejects exceed its accepts over 20+ decided cards is
paused (its cards are not offered), Status names it with a Resume, and Resume (a change
event, the only thing stored) brings the cards back and starts the count again."""

import shutil

import pytest
from conftest import events, run
from test_app_js import HARNESS, _run
from test_review import PREF, app, changes  # noqa: F401  (app is a fixture)
from test_review_conflicts import corrections_vault
from ulid import ULID

from cortex import review
from cortex.app import server
from cortex.events import EventLog
from cortex.vault import Vault

pytestmark = pytest.mark.usefixtures("home")


def ev(change, session=None, id_=None):
    """A change event as SessionManager.note writes it, one click per session."""
    return {"kind": "change", "id": id_ or f"e{ULID()}", "session": session or f"s_{ULID()}",
            "payload": {"note": "patched", "change": change}}


def click(producer, tool="cortex_patch", **c):
    return ev({"tool": tool, "producer": producer, "confirm": "clicked", **c})


def test_the_ledger_counts_clicks_and_pauses_after_20():
    no = [click("corrections", tool="dismiss", id="me/core") for _ in range(11)]
    yes = [click("corrections") for _ in range(9)]
    led = review.ledger(no[:10] + yes)
    assert led["corrections"] == {"accept": 9, "reject": 10, "paused": False}  # 19 cards
    led = review.ledger(no + yes)
    assert led["corrections"] == {"accept": 9, "reject": 11, "paused": True}
    assert not any(x["paused"] for p, x in led.items() if p != "corrections")
    tie = [click("chats", tool="dismiss") for _ in range(10)] + [click("chats")]
    tie += [click("chats", op="add") for _ in range(9)]
    assert review.ledger(tie)["chats"]["paused"] is False  # 10 to 10: not more rejects
    # one verdict per click: a click's several changes share its session
    s = f"s_{ULID()}"
    assert review.ledger([ev({"tool": "dismiss", "producer": "chats", "confirm": "clicked"}, s),
                          ev({"tool": "dismiss", "producer": "chats", "confirm": "clicked"}, s)]
                         )["chats"]["reject"] == 1
    # a void cancels a click; an unclicked change never counts
    x = click("corrections", tool="dismiss")
    void = {"kind": "void", "id": "v1", "session": "cli", "ref": x["id"]}
    assert review.ledger([x, void, ev({"tool": "dismiss", "producer": "corrections"})]
                         )["corrections"]["reject"] == 0
    # Resume starts the count again
    resumed = no + yes + [ev({"tool": "resume", "producer": "corrections",
                              "confirm": "clicked"})]
    assert review.ledger(resumed)["corrections"] == {"accept": 0, "reject": 0, "paused": False}


def test_legacy_clicks_before_the_stamp():
    old = [ev({"tool": "decide", "op": "reject", "confirm": "clicked"}),
           ev({"tool": "decide", "op": "accept", "confirm": "clicked"}),
           ev({"tool": "dismiss", "id": "forget", "confirm": "clicked"}),
           ev({"tool": "dismiss", "id": "me/preferences", "confirm": "clicked"})]  # unknown
    led = review.ledger(old)
    assert (led["decisions"]["accept"], led["decisions"]["reject"]) == (1, 1)
    assert led["forget"]["reject"] == 1
    assert led["chats"] == {"accept": 0, "reject": 0, "paused": False}


def seed(root, producer, n):
    """`n` clicked dismissals of `producer`'s cards, each its own session."""
    log = EventLog(Vault(root))
    for _ in range(n):
        log.append(session=f"s_{ULID()}", kind="change", source="tutor", fields={"payload": {
            "note": "patched", "change": {"tool": "dismiss", "id": "me/core", "producer": producer,
                                          "confirm": "clicked"}}})


def test_a_paused_producer_leaves_review_and_status_offers_resume(root, app):  # noqa: F811
    corrections_vault(root)
    assert app.review()["rules"] != []
    with pytest.raises(server.NotFound):  # only a paused producer resumes
        app.review_resume({"producer": "corrections"})
    seed(root, "corrections", review.PAUSE_AFTER)
    view = app.review()
    assert view["rules"] == [] and [p["producer"] for p in view["paused"]] == ["corrections"]
    (row,) = [r for r in app.status()["rows"] if r.get("resume")]
    assert row["resume"] == "corrections" and row["value"] == "Corrections you repeat paused"
    assert "20 of the last 20" in row["detail"]
    view = app.review_resume({"producer": "corrections"})
    assert view["rules"] != [] and view["paused"] == []
    assert not [r for r in app.status()["rows"] if r.get("resume")]
    (c,) = [e["payload"]["change"] for e in events(root) if e["kind"] == "change"
            and e["payload"]["change"].get("tool") == "resume"]
    assert c == {"tool": "resume", "id": "review", "kind": "review", "op": "resume",
                 "producer": "corrections", "confirm": "clicked"}


def test_clicks_stamp_their_producer(root, app):  # noqa: F811
    async def body(call):
        await call("cortex_patch", PREF)

    run(root, body)
    (p,) = app.review()["pending"]
    app.review_dismiss({"key": p["key"]})
    (c,) = [c for c in changes(root, "preferences") if c.get("op") == "reject"]
    assert (c["producer"], c["card"]) == ("chats", "pending")
    led = review.ledger(events(root))
    assert led["chats"] == {"accept": 0, "reject": 1, "paused": False}


RESUME_JS = HARNESS[:HARNESS.index("const out = {};")] + r"""
const out = { errors };
(async () => {
  try {
    vm.runInThisContext(fs.readFileSync(appPath, 'utf8'), { filename: appPath });
    await tick();
    const b = $('status').querySelectorAll('button').find((x) => x.textContent === 'Resume');
    out.label = b.getAttribute('aria-label');
    b.fire('click');
    await tick();
    out.posted = fetched.filter((f) => f.method === 'POST').map((f) => [f.path, f.body]);
  } catch (e) { errors.push(String(e && e.stack || e)); }
  out.sinks = sinks;
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""  # noqa: E501  (JavaScript)


def test_the_status_resume_button(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    row = {"id": "review-chats", "label": "Review", "state": "warn",
           "value": "Lines chats suggest paused", "detail": "You dismissed 15 of the last 20",
           "resume": "chats"}
    status = {"first_run": False, "rows": [row], "vault": "V", "land": "status",
              "update": {"current": "0.1.0", "mode": "installer"}}
    out = _run(node, tmp_path, "resume", RESUME_JS, {
        "routes": {"/api/status": status, "/api/review/resume": {"paused": []}}, "stream": []})
    assert out["errors"] == [] and out["sinks"] == [], out["errors"]
    assert out["label"] == "Resume: Lines chats suggest paused"
    assert out["posted"] == [["/api/review/resume", {"producer": "chats"}]]

