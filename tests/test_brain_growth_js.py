"""The growth moment's rules in brain3d.js (`KeepfoldBrain.GROWTH`), run under node: which pages
count as new against the viewer's `seenAt`, a storage that fails, hidden pages never counted,
the label text. The moment itself and Today are driven in a real browser by
test_dashboard_js.py. Skipped without node."""

import json
import shutil
import subprocess

import pytest

from keepfold import dashboard

BRAIN_JS = dashboard.ASSETS / "brain3d.js"

HARNESS = r"""
globalThis.window = globalThis;
require(process.argv[2]);
const G = window.KeepfoldBrain.GROWTH, out = {};
const added = { a: '2026-11-20T09:00:00.000Z', b: '2026-11-20T10:00:00.000Z',
  c: '2026-11-20T11:00:00.000Z', d: 7 };
out.first = G.pending(added, null, null, 'NOW');
out.none = G.pending({}, null, null, '2026-11-20T08:00:00.000Z');
out.after = G.pending(added, '2026-11-20T09:00:00.000Z');
out.all = G.pending(added, '2026-11-19T00:00:00.000Z');
out.seenAll = G.pending(added, '2026-11-20T11:00:00.000Z', null, 'NOW');
out.hidden = G.pending(added, '2026-11-19T00:00:00.000Z', (id) => id !== 'b');
const broken = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('full'); } };
out.readBroken = G.read(broken, 'k');
G.write(broken, 'k', 'v');  // must not throw
const mem = new Map();
const ok = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, v) };
out.readEmpty = G.read(ok, 'k');
G.write(ok, 'k', 'x1');
out.readBack = G.read(ok, 'k');
out.readNull = G.read(null, 'k');
const lobe = (k, n, title) => ({ k, n, title });
out.one = G.labels([lobe(4, 2, 'Graphs')]);
out.three = G.labels([lobe(1, 1, 'A'), lobe(2, 3, 'B'), lobe(0, 1, 'C')]);
out.many = G.labels([lobe(1, 1, 'A'), lobe(2, 3, 'B'), lobe(3, 1, 'C'), lobe(4, 1200, 'D')]);
out.since = G.since(added, Date.parse('2026-11-20T10:00:00.000Z'), new Set(['c']));
console.log(JSON.stringify(out));
"""


def test_growth_rules(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node")
    (tmp_path / "h.cjs").write_text(HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(tmp_path / "h.cjs"), str(BRAIN_JS)], capture_output=True,
                       text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    # nothing seen yet: nothing plays, the newest stamp is remembered (or now, with no stamps)
    assert o["first"] == {"ids": [], "newest": "2026-11-20T11:00:00.000Z"}
    assert o["none"] == {"ids": [], "newest": "2026-11-20T08:00:00.000Z"}
    # newer than seenAt, oldest first; a bad stamp never counts
    assert o["after"] == {"ids": ["b", "c"], "newest": "2026-11-20T11:00:00.000Z"}
    assert o["all"]["ids"] == ["a", "b", "c"]
    assert o["seenAll"] == {"ids": [], "newest": "2026-11-20T11:00:00.000Z"}
    # a page this view hides is never counted, though seenAt moves past it
    assert o["hidden"] == {"ids": ["a", "c"], "newest": "2026-11-20T11:00:00.000Z"}
    # storage that throws or is missing reads as nothing and writes without an error
    assert o["readBroken"] is None and o["readEmpty"] is None and o["readNull"] is None
    assert o["readBack"] == "x1"
    # labels: one per lobe up to 3, most first; more lobes: one count of places
    assert o["one"] == [{"k": 4, "text": "+2 · Graphs"}]
    assert [x["text"] for x in o["three"]] == ["+3 · B", "+1 · C", "+1 · A"]  # ties: lobe order
    assert o["many"] == [{"k": -1, "text": "+1,205 in 4 places"}]
    assert o["since"] == ["b"]  # today's faint glow: from midnight, less the pages chosen
