"""A me/ page saved as ANSI (cp1252) or UTF-16, or with a YAML typo in its header, is skipped
like an absent page: prime, context and patch still answer and the app's Home, Review, Tonight
and Connect still load."""

import pytest
from conftest import run

pytestmark = pytest.mark.usefixtures("home")

BODY = "# Preferences\n\n## Study\n- Short drills, café at night.\n"
BAD = {
    "cp1252": ('---\nid: "preferences"\ntitle: "Preferences"\n---\n' + BODY).encode("cp1252"),
    "utf16": ('---\nid: "preferences"\ntitle: "Preferences"\n---\n' + BODY).encode("utf-16"),
    "yaml_typo": ('---\nid: "preferences"\ntitle: Preferences: mine\n---\n' + BODY).encode(),
}


@pytest.fixture(params=sorted(BAD))
def bad_page(request):
    return BAD[request.param]


def test_mcp_tools_answer_with_an_unreadable_me_page(root, bad_page):
    for page in ("preferences", "profile"):  # profile: prime reads it through prime.me
        (root / "me" / f"{page}.md").write_bytes(bad_page)

    async def body(call):
        out = await call("keepfold_prime", {"prompt": "help me study two pointers"})
        assert "error" not in out, out
        out = await call("keepfold_context", {"topic": "pointers"})
        assert "error" not in out, out
        await call("keepfold_start")
        out = await call("keepfold_patch", {"id": "sprint-test", "op": "check_item",
                                            "target": "Day 2", "text": "Drill set A"})
        assert out.get("changed") is True, out

    run(root, body)


def test_app_pages_load_with_an_unreadable_me_page(root_committed, bad_page):
    from test_app import _serve, call

    (root_committed / ".keepfold").mkdir(exist_ok=True)
    for page in ("preferences", "profile"):
        (root_committed / "me" / f"{page}.md").write_bytes(bad_page)
    srv, app = _serve(root_committed)
    try:
        for path in ("/api/home", "/api/review", "/api/tonight", "/api/connect"):
            code, out, _, _ = call(app, "GET", path)
            assert code == 200, (path, out)
    finally:
        srv.shutdown()
        srv.server_close()
