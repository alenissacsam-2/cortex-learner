"""The app's Tonight tab (Gemini round 3 "Nightly Closeout"): GET /api/tonight's boards on a
synthetic vault (the pulse's hours across the 04:00 rollover, what AI apps saved today without
private pages or app clicks, tomorrow's due concepts), the session Undo, the evening landing
rule, and the page's rendering with Review's Today's 5 answered by Y."""

import shutil
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import git, run
from test_app_js import _run
from test_review_today import KEYS_JS
from ulid import ULID

from keepfold import tonight
from keepfold.app import server
from keepfold.events import EventLog
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

DAY = datetime(2026, 11, 20)  # a local date; every time below is local wall time


def at(hours: int, minutes: int = 0) -> datetime:
    """DAY at hours:minutes local time (past 24: the next calendar day), aware."""
    return (DAY + timedelta(hours=hours, minutes=minutes)).astimezone()


def sid(when: datetime) -> str:
    return f"s_{ULID.from_datetime(when)}"


def change(log, s, when, note, **c):
    log.append(session=s, kind="change", source="tutor", now=when,
               fields={"payload": {"note": note, "change": {"tool": "patch", **c}}})


def synthetic(root):
    """An AI app's session (Claude Code), an app click and the CLI on DAY's Keepfold day."""
    log = EventLog(Vault(root))
    agent, click = sid(at(20)), sid(at(21))
    log.append(session=agent, kind="open", source="tutor", now=at(20),
               fields={"payload": {"pid": 1, "client": "Claude Code"}})
    change(log, agent, at(20, 6), "added", id="two-pointers", kind="concept", op="create")
    change(log, agent, at(20, 12), "patched", id="secret-diary", kind="concept", op="append")
    change(log, agent, at(20, 18), "patched", id="profile", kind="me", op="append_to_section",
           target="Facts")
    log.append(session=click, kind="open", source="tutor", now=at(21),
               fields={"payload": {"pid": 1}})
    change(log, click, at(21, 6), "patched", id="arrays-basics", kind="concept", op="check",
           confirm="clicked")
    change(log, "cli", at(9), "patched", id="arrays-basics", kind="concept", op="set")
    for h, m in ((3, 59), (4, 0), (27, 59), (28, 0)):  # 03:59 and 04:00, DAY and the next
        log.append(session="cli", kind="log", source="self", now=at(h, m),
                   fields={"payload": {"kind": "touch", "concept": "two-pointers"}})
    return log, agent, click


def test_the_view_on_a_synthetic_vault(root, monkeypatch):
    log, agent, _ = synthetic(root)
    nxt = (DAY + timedelta(days=1)).date().isoformat()
    states = {"two-pointers": SimpleNamespace(next_due=nxt, state="learning"),
              "secret-diary": SimpleNamespace(next_due=nxt, state="learning"),  # private
              "arrays-basics": SimpleNamespace(
                  next_due=(DAY + timedelta(days=2)).date().isoformat(), state="solid")}
    monkeypatch.setattr(tonight.planner, "current_states", lambda *a, **k: (states, {}))
    v = tonight.view(Vault(root), list(log.iter_events()), at(22, 30))
    assert v["day"] == DAY.date().isoformat() and v["hour"] == 18  # 22:00-23:00
    assert len(v["pulse"]) == 24 and v["pulse"][0]["from"] == "04:00"
    assert v["pulse"][23] == {"from": "03:00", "to": "04:00", "n": 1, "level": 1}
    n = [x["n"] for x in v["pulse"]]
    # 04:00 is hour 0, the next day's 03:59 hour 23; DAY's 03:59 and the next 04:00 are not today
    assert n[0] == 1 and n[5] == 1 and n[16] == 3 and n[17] == 1 and sum(n) == 7
    assert v["counts"] == {"added": 1, "changed": 4, "chat_turns": 0, "recorded": 3}
    assert {s["source"]: s["n"] for s in v["sources"]} == {
        "Claude Code": 3, "Keepfold CLI": 3, "Keepfold app": 1}
    (g,) = v["learned"]  # the app click is not an AI app's
    assert (g["session"], g["client"]) == (agent, "Claude Code")
    assert [(r["id"], r["time"]) for r in g["rows"]] == [("me/profile", "20:18"),
                                                         ("two-pointers", "20:06")]
    assert "secret-diary" not in str(v)  # the private concept is never named
    assert v["tomorrow"] == [{"id": "two-pointers", "title": "Two pointers",
                              "subject": "Data structures and algorithms"}]
    assert v["studies"] is True  # vault_mini has concepts


def test_the_view_says_whether_the_person_studies(tmp_path):
    root = tmp_path / "v"
    root.mkdir()
    (root / "catalog.yaml").write_text("schema: 1\ndomains: []\n", encoding="utf-8")
    assert tonight.view(Vault(root), [], at(22))["studies"] is False


def test_chat_turns_from_the_live_logs(root):
    from keepfold import chats
    chats.log_live("01JD0000000000000000000000", [("user", "hi"), ("assistant", "hello")],
                   at(22))
    chats.log_live("01JD0000000000000000000000", [("user", "yesterday")], at(3))
    v = tonight.view(Vault(root), [], at(23))
    assert v["counts"]["chat_turns"] == 1 and v["pulse"][18]["n"] == 1
    assert v["sources"] == [{"source": chats.LIVE, "n": 1}]


def test_levels_by_quartiles():
    assert tonight.levels([0, 0]) == [0, 0]
    assert tonight.levels([0, 1, 2, 3, 9]) == [0, 1, 2, 2, 3]


def test_undo_an_ai_apps_session_then_404(root, monkeypatch):
    async def body(call):
        await call("keepfold_add", {"kind": "concept", "title": "Sliding Window",
                                  "parent": "dsa.arrays"})
        await call("keepfold_end", {"summary": "added a concept"})  # committed: undo-able

    run(root, body)
    app = server.App(root)
    (g,) = app.tonight()["learned"]
    assert g["rows"][0]["id"] == "sliding-window" and g["client"]
    with pytest.raises(server.NotFound):
        app.tonight_undo({"session": "s_01JD0000000000000000000000"})
    app.tonight_undo({"session": g["session"]})
    while app.jobs.running():
        time.sleep(0.05)
    job = app.jobs.view()["job"]
    assert job["error"] is None and job["result"]["ok"], job
    assert "sliding-window" not in (root / "catalog.yaml").read_text("utf-8")
    assert git(root, "log", "-1", "--format=%s").strip() == f"keepfold: undo {g['session']}"
    assert app.tonight()["learned"] == []
    with pytest.raises(server.NotFound):  # already undone
        app.tonight_undo({"session": g["session"]})


def test_undo_of_a_session_still_open_says_so(root):
    async def body(call):  # no keepfold_end: the session is open, nothing committed yet
        await call("keepfold_add", {"kind": "concept", "title": "Sliding Window",
                                  "parent": "dsa.arrays"})

    run(root, body)
    assert not [e for e in EventLog(Vault(root)).iter_events() if e["kind"] == "close"]
    app = server.App(root)
    (g,) = app.tonight()["learned"]  # what it saved is still shown
    with pytest.raises(ValueError, match="still open"):
        app.tonight_undo({"session": g["session"]})
    assert not app.jobs.running() and "sliding-window" in (root / "catalog.yaml").read_text(
        "utf-8")


def test_undo_resets_the_review_keepfold_only_between_clicks(root, monkeypatch):
    async def body(call):
        await call("keepfold_add", {"kind": "concept", "title": "Sliding Window",
                                  "parent": "dsa.arrays"})
        await call("keepfold_end", {"summary": "added a concept"})

    run(root, body)
    app = server.App(root)
    (g,) = app.tonight()["learned"]
    monkeypatch.setattr(server.actions, "capture", lambda *a, **k: {"ok": True})
    app.core = (root, object())
    with app.reviewing:  # a Review click in progress
        app.tonight_undo({"session": g["session"]})
        time.sleep(0.3)
        assert app.core is not None  # never swapped out under the click
    while app.jobs.running():
        time.sleep(0.05)
    assert app.core is None


@pytest.mark.parametrize(("hh", "mm", "night"), [(19, 59, False), (20, 0, True),
                                                 (3, 59, True), (4, 0, False)])
def test_the_app_opens_on_tonight_in_the_evening(root, hh, mm, night):
    (root / ".keepfold").mkdir(exist_ok=True)
    app = server.App(root)
    url = server.start_url(app, datetime(2026, 11, 20, hh, mm))
    assert url == (f"http://127.0.0.1:{app.port}/manage?t={app.token}#tonight" if night
                   else f"http://127.0.0.1:{app.port}/?t={app.token}")


def test_no_vault_opens_home_at_night(tmp_path):
    app = server.App(None)
    assert "#tonight" not in server.start_url(app, datetime(2026, 11, 20, 22))


# the Review keys test (test_review_today.KEYS_JS), on the Tonight section instead
OPEN_REVIEW = """    nav.querySelectorAll('button').find((b) => b.dataset.s === 'review').fire('click');
    await tick();
    out.today = todayCards().map"""  # noqa: E501  (JavaScript)
OPEN_TONIGHT = """    nav.querySelectorAll('button').find((b) => b.dataset.s === 'tonight').fire('click');
    await tick();
    const t = $('tonight');
    out.boards = t.querySelectorAll('h2').map((x) => x.textContent);
    out.cells = t.querySelectorAll('.pcell').map((c) => [c.getAttribute('role'), c.getAttribute('aria-label'), c.className]).filter((x, i) => i < 2 || x[2].includes('now'));
    out.text = t.textContent;
    out.undo = t.querySelectorAll('button').filter((b) => b.textContent === 'Undo').map((b) => b.getAttribute('aria-label'));
    t.querySelectorAll('button').find((b) => b.textContent === 'Undo').fire('click');
    await tick();
    out.undoAsk = [$('confirm-title').textContent, $('confirm-ok').textContent, $('confirm-body').textContent,
      fetched.some((f) => f.path === '/api/tonight/undo')];
    $('confirm-ok').onclick();
    await tick();
    out.undone = fetched.filter((f) => f.path === '/api/tonight/undo').map((f) => f.body);
    out.today = todayCards().map"""  # noqa: E501  (JavaScript)
PAGE_JS = KEYS_JS.replace(OPEN_REVIEW, OPEN_TONIGHT).replace(
    "const todayCards = () => $('review').childNodes[0]",
    "const todayCards = () => $('tonight').childNodes[1]")
assert OPEN_REVIEW in KEYS_JS and "$('tonight').childNodes[1]" in PAGE_JS


@pytest.mark.parametrize("studies", [True, False])
def test_the_tonight_section_and_y_on_a_card(tmp_path, studies):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    pend = {"key": "k1", "page": "teaching-contract", "section": "Rules", "text": "Quiz me",
            "since": "2026-11-20", "single": False}
    pair = {"key": "p1", "why": "update",
            "left": {"page": "preferences", "id": "c-bbbbb", "text": "Uses Vim"},
            "right": {"page": "preferences", "id": "c-ccccc", "text": "Uses Helix"}}
    review = {"pending": [pend], "expired": [], "stale": [], "conflicts": [pair], "rules": [],
              "decisions": [], "forget": [], "count": 2, "today_left": 5, "paused": [],
              "today": [{"type": "pending", "key": "k1"}, {"type": "conflict", "key": "p1"}]}
    after = {**review, "pending": [], "count": 1, "today_left": 4, "today": review["today"][1:]}
    pulse = [{"from": f"{(4 + i) % 24:02d}:00", "to": f"{(5 + i) % 24:02d}:00",
              "n": 6 if i == 10 else 0, "level": 3 if i == 10 else 0} for i in range(24)]
    view = {"day": "2026-11-20", "hour": 10, "pulse": pulse,
            "counts": {"added": 1, "changed": 2, "chat_turns": 3, "recorded": 1},
            "sources": [{"source": "Claude Code", "n": 6}],
            "learned": [{"session": "s_1", "client": "Claude Code", "rows": [
                {"text": "<b>Likes tea</b>", "id": "me/profile", "time": "20:18"}]}],
            "tomorrow": [{"id": "two-pointers", "title": "Two Pointers", "subject": "DSA"}],
            "tomorrow_day": "2026-11-21", "studies": studies, "review": review}
    status = {"first_run": False, "rows": [], "vault": "V", "land": "status",
              "update": {"current": "0.1.0", "mode": "installer"}}
    out = _run(node, tmp_path, "tonight", PAGE_JS, {
        "routes": {"/api/tonight": view, "/api/review": review, "/api/status": status,
                   "/api/tonight/undo": {"job": "Undo", "seq": 1},
                   "/api/job": {"job": None, "seq": 1},
                   "/api/review/conflict": {**after, "conflicts": [], "today": []}},
        "after": after, "stream": []})
    assert out["errors"] == [] and out["sinks"] == [], out["errors"]
    # no Tomorrow board for someone who does not study
    assert out["boards"] == ["Today", "Today’s 5", "What AI apps learned today",
                             *(["Tomorrow"] if studies else [])]
    assert out["cells"] == [["img", "04:00–05:00: 0 events", "pcell l0"],
                            ["img", "05:00–06:00: 0 events", "pcell l0"],
                            ["img", "14:00–15:00: 6 events", "pcell l3 now"]]
    assert ("1 page added · 2 pages changed · 3 chat turns · 1 thing recorded by AI apps"
            in out["text"])
    assert "Claude Code: 6" in out["text"] and "<b>Likes tea</b>" in out["text"]  # as text
    assert studies == ("A 2-minute cold check tomorrow: ask any AI app /keepfold review."
                       in out["text"])
    assert out["undo"] == ["Undo everything Claude Code saved in session s_1"]
    title, ok, ask, posted = out["undoAsk"]  # asked first: nothing posted before the OK
    assert (title, ok, posted) == ("Undo Claude Code’s session?", "Undo", False)
    assert "Claude Code" in ask and "1 change" in ask and "cannot be redone" in ask
    assert out["undone"] == [{"session": "s_1"}]
    assert out["today"] == [["Quiz me", "group", "0"], ["Uses Vim", "group", "-1"]]
    # Y on the focused card answers it through Review's endpoint, as on the Review page
    assert out["posted"][1:] == [["/api/review/approve", {"key": "k1"}]]  # after the Undo
    assert out["refocus"] == "Uses Vim"


def test_the_apps_own_check_is_not_an_ai_app():
    from keepfold import tonight
    events = [{"session": "s_a", "kind": "open", "payload": {"client": "keepfold-app"}},
              {"session": "s_b", "kind": "assessment", "grader": {"client": "keepfold-app"}},
              {"session": "s_c", "kind": "open", "payload": {"client": "claude-ai"}}]
    assert tonight.clients(events) == {"s_c": "claude-ai"}
