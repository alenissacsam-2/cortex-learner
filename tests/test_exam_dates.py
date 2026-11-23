"""R31: Home's Add a date (POST /api/home/date, through cortex_add's own code path) and the
board's calendar suggestions (dashboard.exam_suggestions, a fold over the calendar month pages
and the assessment pages). Made-up vaults only."""

from datetime import UTC, date, datetime, timedelta

import pytest
from test_app import _serve, call
from test_home import _note

from cortex import dashboard, planner
from cortex.events import EventLog, local_day
from cortex.home import HomeCache
from cortex.vault import Vault

TODAY = date.fromisoformat(local_day(datetime.now(UTC)))


def _day(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


@pytest.fixture
def app(root_committed, home):
    (root_committed / ".cortex").mkdir(exist_ok=True)
    srv, a = _serve(root_committed)
    yield a
    srv.shutdown()
    srv.server_close()


def _calendar(root, nid: str, lines: list[str], **fields) -> None:
    _note(root, "resources", nid, {"title": nid, "kind": "resource", "resource_type": "other",
                                   **fields}, "## Events\n" + "".join(f"{x}\n" for x in lines))


def test_add_a_date_writes_an_assessment_page_the_planner_sees(app):
    v = Vault(app.root)
    before = {e["id"] for e in EventLog(v).iter_events()}
    code, out, _, _ = call(app, "POST", "/api/home/date",
                           {"what": "  DSA exam ", "when": _day(19), "kind": "exam"})
    assert code == 200 and out["title"] == "DSA exam" and out["date"] == _day(19)
    page = v.read("assessment", out["id"])
    assert page.fields["title"] == "DSA exam" and page.fields["kind"] == "exam"
    assert (page.fields["date"], page.fields["outcome"]) == (_day(19), "pending")
    got = next(a for a in planner.assessments(v) if a["id"] == out["id"])
    assert (got["kind"], got["date"], got["outcome"]) == ("exam", _day(19), "pending")
    home = HomeCache(v).get()
    row = next(a for a in home["exams"] if a["id"] == out["id"])
    assert row["title"] == "DSA exam" and row["days_left"] == 19
    new = [e for e in EventLog(v).iter_events() if e["id"] not in before]
    # cortex_add's own change in a session of the app's, closed: `cortex undo` takes it back
    assert [e["kind"] for e in new] == ["open", "change", "close"]
    assert new[0]["payload"]["client"] == "cortex-app"
    assert new[1]["payload"]["change"] == {"tool": "add", "id": out["id"], "kind": "assessment",
                                           "op": "create", "target": None}
    # a deadline, and the same name on another day is a page of its own
    code, out2, _, _ = call(app, "POST", "/api/home/date",
                            {"what": "DSA exam", "when": _day(40), "kind": "deadline"})
    assert code == 200 and out2["id"] != out["id"]
    assert v.read("assessment", out2["id"]).fields["kind"] == "deadline"


@pytest.mark.parametrize(("body", "said"), [
    ({"what": "DSA exam", "when": _day(-1)}, "passed"),
    ({"what": "   ", "when": _day(3)}, "name"),
    ({"what": "x" * 121, "when": _day(3)}, "120"),
    ({"what": "DSA exam", "when": "2026-02-30"}, "Pick a date"),
    ({"what": "DSA exam", "when": None}, "Pick a date"),
    ({"what": "DSA exam", "when": _day(3), "kind": "quiz"}, "exam or deadline"),
])
def test_add_a_date_refuses_and_writes_nothing(app, body, said):
    v = Vault(app.root)
    had = sorted(p.name for p in v.note_paths("assessment"))
    code, out, _, _ = call(app, "POST", "/api/home/date", body)
    assert code == 400 and said in out["error"]
    assert sorted(p.name for p in v.note_paths("assessment")) == had


def test_the_same_name_on_the_same_day_is_refused(app):
    body = {"what": "OS quiz", "when": _day(2), "kind": "exam"}
    assert call(app, "POST", "/api/home/date", body)[0] == 200
    code, out, _, _ = call(app, "POST", "/api/home/date", {**body, "what": "os QUIZ"})
    assert code == 400 and "already" in out["error"]


def test_the_name_is_plain_text_on_the_board(app):
    what = "<b>DSA</b> [notes](http://e.test/x) exam"
    assert call(app, "POST", "/api/home/date", {"what": what, "when": _day(5)})[0] == 200
    row = next(a for a in HomeCache(Vault(app.root)).get()["exams"] if a["date"] == _day(5))
    assert row["title"] == "DSA notes exam"  # no tag, no link (the JS sets it as text)


def test_suggestions_are_exam_like_calendar_events_not_yet_added(app):
    root, v = app.root, Vault(app.root)
    _calendar(root, "calendar-2099-01", [
        f"- {_day(3)} 09:00 · DSA Exam · Room 4", f"- {_day(3)} 10:00 · Lunch with Sam",
        f"- {_day(5)} all day · OS assignment deadline", f"- {_day(-2)} all day · Old test",
        f"- {_day(61)} all day · Far exam", f"- {_day(7)} 18:00 (Asia/Kolkata) · Mock OA",
        f"- {_day(9)} all day · Maths quiz"])
    _calendar(root, "calendar-private-2099-01", [f"- {_day(1)} all day · Therapy test"],
              private=True)
    got = HomeCache(v).get()["exam_suggestions"]
    assert got == [
        {"day": _day(3), "title": "DSA Exam", "page": "calendar-2099-01", "kind": "exam"},
        {"day": _day(5), "title": "OS assignment deadline", "page": "calendar-2099-01",
         "kind": "deadline"},
        {"day": _day(7), "title": "Mock OA", "page": "calendar-2099-01", "kind": "exam"}]
    assert "Therapy" not in str(got)  # a private calendar page is never read
    assert call(app, "POST", "/api/home/date",
                {"what": "DSA Exam", "when": _day(3), "kind": "exam"})[0] == 200
    titles = [r["title"] for r in HomeCache(v).get()["exam_suggestions"]]
    assert titles == ["OS assignment deadline", "Mock OA", "Maths quiz"]  # the added one went


def test_suggestions_skip_a_page_under_a_private_subject(root_committed):
    _calendar(root_committed, "calendar-2099-02", [f"- {_day(4)} all day · Physics exam"])
    notes = {"assessment": [], "resource": list(Vault(root_committed).iter_notes("resource"))}
    assert dashboard.exam_suggestions(notes, TODAY, set())[0]["title"] == "Physics exam"
    assert dashboard.exam_suggestions(notes, TODAY, {"calendar-2099-02"}) == []
