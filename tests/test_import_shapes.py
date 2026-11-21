"""RR-38: `cortex doctor`'s import shapes: per source, page-length quartiles and the share of
pages that are mostly links or numbers only; numbers and known labels only, never page text."""

from cortex import doctor
from cortex.vault import Vault


def _page(v: Vault, nid: str, head: str, body: str) -> None:
    v.create_note("resource", nid, {"title": nid, "kind": "resource"},
                  f"# {nid}\n\n{head} · Date: 2026-01-02 · sha: {'0' * 16}\n\n{body}\n")


def test_shapes_per_source_numbers_only(root_plain):
    v = Vault(root_plain)
    assert doctor.check_import_shapes(root_plain) == [
        ("PASS", "Import shapes", "no imported pages", "")]
    for k, n in enumerate((10, 20, 30, 40, 50)):
        _page(v, f"notes-{k}", "Source: Notes", "secret words " + "x" * (n - 13))
    _page(v, "notes-links", "Source: Notes", "https://example.com/a/very/long/link/1\n"
          "https://example.com/a/very/long/link/2")
    _page(v, "notes-nums", "Source: Notes", "12.5 13.75 2026 11 21 99 100")
    v.create_note("resource", "chat-1", {"title": "c", "kind": "resource"},
                  "# c\n\nTimeline: · Source: Claude · Date: 2026-01-02 · My turns only\n\n"
                  "**Me:** hello there\n")
    _page(v, "odd", "Source: My private diary label", "text")
    rows = doctor.check_import_shapes(root_plain)
    assert [r[1] for r in rows] == ["Import shapes: Notes", "Import shapes: Claude",
                                    "Import shapes: other"]
    assert rows[0][2] == ("7 pages, length quartiles 28 / 30 / 40 characters, mostly links "
                          "14%, numbers only 14%")
    shown = " ".join(" ".join(r) for r in rows)
    assert "secret" not in shown and "diary" not in shown and "hello" not in shown
