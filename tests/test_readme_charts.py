"""scripts/readme_charts.py turns a tiny results.json into valid, self-contained SVGs."""

import importlib.util
import re
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "readme_charts.py"


def _charts():
    spec = importlib.util.spec_from_file_location("readme_charts", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _results() -> dict:
    kinds = ("exact_id", "lexical", "synonym", "unanswerable")
    return {
        "runs": 3, "generated": "2026-11-22T21:00:00+05:30", "git_sha": "abc1234",
        "machine": {"cpu": "Test CPU", "logical_cores": 8, "os": "TestOS", "python": "3.12.0"},
        "load": {"cpu_percent_median": 90.0},
        "browse": {"visits": 5000, "first_import_s": 12.0, "nothing_new_s": 0.1,
                   "live_read_50_s": 1.5},
        "tests": {"collected": 1234, "command": "uv run pytest --collect-only -q"},
        "index": {"points": [{"notes": n, "cold_build_s": n / 1000, "snapshot_build_s": 0.1,
                              "mcp_first_start_s": 0.9, "mcp_next_start_s": 0.5}
                             for n in (1000, 2500)]},
        "vectors": {"points": [{"pages": n, "query_s": 0.01, "cold_start_s": 0.2,
                                "add_page_s": 0.05, "chunks": 10 * n}
                               for n in (1000, 2000)]},
        "chat_data": {"calls": {"site_list all days": {"first_s": 0.3, "after_s": 0.0},
                                "study due": {"first_s": 0.1, "after_s": 0.02}}},
        "chat_test": {"passed": 3, "ran": 3, "failed": 0, "needs_model": 4,
                      "by_query_kind": {k: {"pass": i % 2, "fail": 0, "needs_model": 1}
                                        for i, k in enumerate(kinds)}},
    }


def test_charts_are_valid_svg_with_no_script_or_external_url(tmp_path):
    paths = _charts().write(_results(), tmp_path)
    names = sorted(p.name for p in paths)
    assert len(names) == 8 and "startup-dark.svg" in names and "chat-test-light.svg" in names
    for p in paths:
        text = p.read_text(encoding="utf-8")
        root = ET.fromstring(text)  # well-formed XML
        assert root.tag == "{http://www.w3.org/2000/svg}svg"
        assert root.find("{http://www.w3.org/2000/svg}title").text  # an accessible name
        assert "<script" not in text.lower() and "href" not in text.lower()
        assert "@import" not in text and "url(" not in text
        urls = set(re.findall(r"https?://[^\s\"'<>]+", text))
        assert urls <= {"http://www.w3.org/2000/svg"}, (p.name, urls)


def test_readme_blocks_are_filled_between_their_markers_only():
    mod = _charts()
    readme = ("intro\n<!-- numbers:tests -->\nold\n<!-- /numbers:tests -->\nmiddle\n"
              "<!-- numbers:startup -->\n<!-- /numbers:startup -->\nend\n")  # empty block
    out = mod.fill(readme, mod.blocks(_results()))
    assert out.startswith("intro\n") and "\nmiddle\n" in out and out.endswith("end\n")
    assert "1,234 automated tests" in out and "\nold\n" not in out
    assert 'srcset="docs/images/charts/startup-dark.svg"' in out and "| 2,500 | 2.5 s |" in out
    assert mod.fill(out, mod.blocks(_results())) == out  # a second run changes nothing


def test_a_partial_run_leaves_missing_sections_out(tmp_path):
    mod = _charts()
    r = {k: v for k, v in _results().items() if k not in mod.TIMED}  # a run stopped early
    assert sorted(p.name for p in mod.write(r, tmp_path)) == ["chat-test-dark.svg",
                                                               "chat-test-light.svg"]
    b = mod.blocks(r)
    assert b["startup"] == b["browse"] == mod.PENDING and "1,234 automated tests" in b["tests"]
    assert "median" not in b["machine"] and "Not measured yet" in b["machine"]
