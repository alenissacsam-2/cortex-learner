"""Titles with no ASCII letter (Hindi, Chinese, Malayalam, Cyrillic) become pages: page_slug
gives them a stable t-<hex> id, where slugify alone refused them or kept only their digits."""

from datetime import UTC, date, datetime, timedelta

import pytest
from conftest import run

from keepfold.catalog import page_slug, slugify
from keepfold.events import local_day
from keepfold.vault import ID_RE

pytestmark = pytest.mark.usefixtures("home")

TITLES = ["गणित", "线性代数", "ഗണിതം", "Линейная алгебра"]


@pytest.mark.parametrize("title", ["Binary search", "C++ & C#", "Two Sum II", "Café crème",
                                   "1984", "2024-11-01", "Kafka 3"])
def test_a_title_that_works_today_keeps_its_id(title):
    assert page_slug(title) == slugify(title) != ""


def test_non_latin_ids_are_valid_stable_and_distinct():
    ids = [page_slug(t) for t in [*TITLES, "Лекция 3", "Тема 3"]]
    assert all(i.startswith("t-") and ID_RE.fullmatch(i) for i in ids)
    assert len(set(ids)) == len(ids)  # 'Лекция 3' and 'Тема 3' no longer both become '3'
    assert page_slug("  गणित ") == page_slug("गणित")
    assert page_slug("ЛЕКЦИЯ 3") == page_slug("лекция   3")
    assert page_slug("!!! ---") == page_slug("") == ""


def test_keepfold_add_takes_non_latin_titles(root):
    async def body(call):
        await call("keepfold_start")
        for t in TITLES:
            out = await call("keepfold_add", {"kind": "note", "title": t})
            assert out["id"] == page_slug(t) and ID_RE.fullmatch(out["id"]), out
            c = await call("keepfold_add", {"kind": "concept", "title": t + " 2",
                                            "parent": "dsa.arrays"})
            assert c["id"] == page_slug(t + " 2"), c
        dup = await call("keepfold_add", {"kind": "note", "title": "गणित"})
        assert dup["error"] == "duplicate_suggested" and page_slug("गणित") in dup["did_you_mean"]
        cdup = await call("keepfold_add", {"kind": "concept", "title": "गणित 2",
                                           "parent": "dsa.arrays"})
        assert cdup["error"] == "duplicate_suggested"
        other = await call("keepfold_add", {"kind": "note", "title": "भौतिकी"})
        assert other["id"] == page_slug("भौतिकी") != page_slug("गणित")
        a = await call("keepfold_add", {"kind": "note", "title": "Лекция 3"})
        b = await call("keepfold_add", {"kind": "note", "title": "Тема 3"})
        assert a["id"] != b["id"] and "3" not in (a["id"], b["id"]), (a, b)
        ascii_ = await call("keepfold_add", {"kind": "note", "title": "Binary search"})
        assert ascii_["id"] == "binary-search"
        bad = await call("keepfold_add", {"kind": "note", "title": "!!! ---"})
        assert bad["error"] == "invalid_argument" and "letter or digit" in bad["message"]

    run(root, body)
    assert (root / "notes" / f"{page_slug('गणित')}.md").is_file()


def test_add_a_date_takes_a_hindi_name(root_committed, home):
    from test_app import _serve, call

    (root_committed / ".keepfold").mkdir(exist_ok=True)
    srv, app = _serve(root_committed)
    try:
        day = (date.fromisoformat(local_day(datetime.now(UTC))) + timedelta(days=9)).isoformat()
        code, out, _, _ = call(app, "POST", "/api/home/date",
                               {"what": "गणित परीक्षा", "when": day, "kind": "exam"})
        assert code == 200 and out["title"] == "गणित परीक्षा", out
        assert out["id"] == page_slug("गणित परीक्षा")
    finally:
        srv.shutdown()
        srv.server_close()
