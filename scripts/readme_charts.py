"""Draw README.md's charts from docs/benchmarks/results.json (written by readme_numbers.py).

Each chart is a static SVG in a light and a dark version (docs/images/charts/<name>-light.svg
and -dark.svg): no script, no external font or URL, a system font stack. The colours are the
dataviz reference palette's first three categorical slots, validated for GitHub's surfaces
(#ffffff light, #0d1117 dark); the background is left transparent so the page shows through.
Then README.md's measured blocks (between `<!-- numbers:NAME -->` and `<!-- /numbers:NAME -->`)
are rewritten from the same results: each chart's <picture> and its table, so a new run of
readme_numbers.py and this script updates every number in the README.

    uv run python scripts/readme_charts.py [--results docs/benchmarks/results.json] [--no-readme]
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from xml.sax.saxutils import escape

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "docs" / "benchmarks" / "results.json"
OUT = REPO / "docs" / "images" / "charts"
FONT = 'system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif'
THEMES = {
    "light": {"series": ("#2a78d6", "#eb6834", "#1baf7a"), "other": "#c3c2b7",
              "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
              "grid": "#e1e0d9", "axis": "#c3c2b7", "on": ("#ffffff", "#ffffff", "#0b0b0b"),
              "surface": "#ffffff"},
    "dark": {"series": ("#3987e5", "#d95926", "#199e70"), "other": "#52514e",
             "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781",
             "grid": "#2c2c2a", "axis": "#383835", "on": ("#ffffff", "#ffffff", "#f0efec"),
             "surface": "#0d1117"},
}
W = 720
BLOCKS = ("machine", "startup", "semantic", "chat-data", "chat-test", "browse", "tests")
TIMED = ("index", "vectors", "browse", "chat_data")  # results.json sections that are timings
PENDING = ("*Not measured yet.* Run `uv run python scripts/readme_numbers.py && "
           "uv run python scripts/readme_charts.py` on a quiet machine to fill this in.")
QUERY_KINDS = {"exact_id": "An exact name", "lexical": "Plain words", "synonym": "Other words",
               "multi_gold": "Several pages", "conflicting_history": "A changed history",
               "stale_negative": "An outdated fact", "unanswerable": "Nothing to find"}
CHAT_CALLS = {"site_list all days": "Every site I visited", "site_list one day": "Sites on one day",
              "site_list one browser": "Sites in one browser", "digest a day": "Summarize my day",
              "digest a week": "Summarize my week", "study plan": "What should I study today?",
              "study due": "What's due?", "study weak": "Where am I weak?",
              "study progress": "How am I doing?",
              "reply (site question)": "A whole Chat reply (site question)"}


def secs(v: float) -> str:
    """0.0123 -> '12 ms', 3.21 -> '3.2 s'; under 10 ms for the 2-decimal scripts' 0.00."""
    if v <= 0:
        return "under 10 ms"
    return f"{v * 1000:.0f} ms" if v < 1 else f"{v:.1f} s"


def nice_ticks(top: float, n: int = 5) -> list[float]:
    """0, step, ..., >= top with a 1, 2, 2.5 or 5 step and at most n + 1 ticks."""
    if top <= 0:
        return [0.0, 1.0]
    raw = top / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return [round(i * step, 10) for i in range(math.ceil(top / step - 1e-9) + 1)]


def tick_label(v: float, unit: str) -> str:
    if unit == "ms":
        return f"{v:,.0f}"
    return f"{v:,.0f}" if v == int(v) else f"{v:g}"


def text(x, y, s, t, size=12, fill="ink", anchor="start", weight=400) -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{t.get(fill, fill)}" '
            f'text-anchor="{anchor}" font-weight="{weight}">{escape(str(s))}</text>')


def svg(t, h: int, title: str, desc: str, body: list[str]) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}" '
            f'viewBox="0 0 {W} {h}" role="img" aria-labelledby="t d" '
            f'font-family=\'{FONT}\'>\n<title id="t">{escape(title)}</title>\n'
            f'<desc id="d">{escape(desc)}</desc>\n' + "\n".join(body) + "\n</svg>\n")


def header(t, title: str, sub: str) -> list[str]:
    return [text(0, 20, title, t, 15, "ink", weight=600), text(0, 40, sub, t, 12, "ink2")]


def bar_path(x: float, y: float, w: float, h: float, r: float) -> str:
    """A bar square at its left (the baseline) with rounded right corners."""
    r = max(0.0, min(r, w, h / 2))
    return (f"M{x:.1f},{y:.1f}h{w - r:.1f}a{r},{r} 0 0 1 {r},{r}v{h - 2 * r:.1f}"
            f"a{r},{r} 0 0 1 {-r},{r}h{-(w - r):.1f}z")


# ---- a line chart of seconds against a size -----------------------------------------------

def line_chart(t, title, sub, xs, xlabel, series, unit, desc) -> str:
    """`series`: [(name, [seconds per x])], at most three (slots 1-3)."""
    scale = 1000 if unit == "ms" else 1
    left, right, top, bottom = 56, 230, 64, 52
    h = 360
    pw, ph = W - left - right, h - top - bottom
    ticks = nice_ticks(max(v * scale for _, vs in series for v in vs))
    ymax, xmax = ticks[-1], max(xs)

    def px(x):
        return left + pw * x / xmax

    def py(v):
        return top + ph - ph * v * scale / ymax
    body = header(t, title, sub)
    for v in ticks:
        y = top + ph - ph * v / ymax
        body.append(f'<line x1="{left}" x2="{left + pw}" y1="{y:.1f}" y2="{y:.1f}" '
                    f'stroke="{t["grid" if v else "axis"]}" stroke-width="1"/>')
        body.append(text(left - 8, y + 4, tick_label(v, unit), t, 11, "muted", "end"))
    body.append(text(left - 8, top - 12, unit, t, 11, "muted", "end"))
    for x in xs:
        body.append(text(px(x), top + ph + 18, f"{x:,}", t, 11, "muted", "middle"))
    body.append(text(left + pw / 2, h - 10, xlabel, t, 12, "ink2", "middle"))
    ends = []
    for i, (name, vs) in enumerate(series):
        c = t["series"][i]
        pts = " ".join(f"{px(x):.1f},{py(v):.1f}" for x, v in zip(xs, vs, strict=True))
        body.append(f'<polyline points="{pts}" fill="none" stroke="{c}" stroke-width="2" '
                    f'stroke-linejoin="round" stroke-linecap="round"/>')
        for x, v in zip(xs, vs, strict=True):
            body.append(f'<circle cx="{px(x):.1f}" cy="{py(v):.1f}" r="4" fill="{c}" '
                        f'stroke="{t["surface"]}" stroke-width="2"><title>{escape(name)}, '
                        f'{x:,}: {secs(v)}</title></circle>')
        ends.append([py(vs[-1]), py(vs[-1]), name, vs[-1], c])
    ends.sort()  # end labels apart by at least 30 px (two lines each), with a leader if moved
    for a, b in zip(ends, ends[1:], strict=False):
        b[1] = max(b[1], a[1] + 30)
    for k in range(len(ends) - 2, -1, -1):  # then pull up any pushed past the plot's bottom
        ends[k][1] = min(ends[k][1], ends[k + 1][1] - 30)
    if ends and ends[-1][1] > top + ph:
        shift = ends[-1][1] - (top + ph)
        for e in ends:
            e[1] -= shift
    lx = left + pw + 14
    for y0, y, name, v, c in ends:
        if abs(y - y0) > 1:
            body.append(f'<line x1="{left + pw + 5}" y1="{y0:.1f}" x2="{lx - 3}" y2="{y - 4:.1f}" '
                        f'stroke="{t["muted"]}" stroke-width="1"/>')
        body.append(f'<line x1="{lx}" x2="{lx + 14}" y1="{y - 4:.1f}" y2="{y - 4:.1f}" '
                    f'stroke="{c}" stroke-width="2" stroke-linecap="round"/>')
        body.append(text(lx + 20, y, name, t, 12, "ink"))
        body.append(text(lx + 20, y + 15, f"{secs(v)} at {xs[-1]:,}", t, 12, "ink2"))
    return svg(t, h, title, desc, body)


# ---- horizontal bars --------------------------------------------------------------------------

def hbars(t, title, sub, rows, desc, label_w=250) -> str:
    """One series: [(label, seconds)], a bar each, value at the tip."""
    left, top, row = label_w, 64, 30
    h = top + row * len(rows) + 30
    pw = W - left - 90
    unit = "ms" if max(v for _, v in rows) < 1 else "s"
    scale = 1000 if unit == "ms" else 1
    ticks = nice_ticks(max(v * scale for _, v in rows))
    xmax = ticks[-1]
    body = header(t, title, sub)
    for v in ticks:
        x = left + pw * v / xmax
        body.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top - 6}" y2="{h - 26}" '
                    f'stroke="{t["grid" if v else "axis"]}" stroke-width="1"/>')
        body.append(text(x, h - 10, f"{tick_label(v, unit)}" + (f" {unit}" if v == xmax else ""),
                         t, 11, "muted", "middle"))
    for i, (name, v) in enumerate(rows):
        y = top + i * row
        w = pw * v * scale / xmax
        body.append(text(left - 10, y + 16, name, t, 12, "ink", "end"))
        if w > 0.5:
            body.append(f'<path d="{bar_path(left, y + 4, w, 16, 4)}" fill="{t["series"][0]}">'
                        f'<title>{escape(name)}: {secs(v)}</title></path>')
        body.append(text(left + w + 6, y + 16, secs(v), t, 12, "ink2"))
    return svg(t, h, title, desc, body)


def stacked(t, title, sub, rows, names, desc, label_w=170) -> str:
    """rows: [(label, [count per part])], parts coloured slot 1, slot 2, then gray."""
    left, top, row = label_w, 92, 32
    h = top + row * len(rows) + 30
    pw = W - left - 150
    ticks = nice_ticks(max(sum(c) for _, c in rows))
    xmax = ticks[-1]
    fills = (t["series"][0], t["series"][1], t["other"])
    body = header(t, title, sub)
    lx = 0  # legend: a swatch and a name per part, in bar order
    for name, f in zip(names, fills, strict=True):
        body.append(f'<rect x="{lx}" y="56" width="12" height="12" rx="2" fill="{f}"/>')
        body.append(text(lx + 18, 66, name, t, 12, "ink2"))
        lx += 34 + 7 * len(name)
    for v in ticks:
        x = left + pw * v / xmax
        body.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top - 6}" y2="{h - 26}" '
                    f'stroke="{t["grid" if v else "axis"]}" stroke-width="1"/>')
        body.append(text(x, h - 10, tick_label(v, "n"), t, 11, "muted", "middle"))
    for i, (label, counts) in enumerate(rows):
        y = top + i * row
        body.append(text(left - 10, y + 17, label, t, 12, "ink", "end"))
        x, last = left, max((k for k, c in enumerate(counts) if c), default=-1)
        for k, c in enumerate(counts):
            if not c:
                continue
            w = pw * c / xmax - (0 if k == last else 2)  # a 2 px gap between parts
            body.append(f'<path d="{bar_path(x, y + 4, w, 18, 4 if k == last else 0)}" '
                        f'fill="{fills[k]}"><title>{escape(label)}: {c} '
                        f'{escape(names[k].lower())}</title></path>')
            if w >= 18:
                body.append(text(x + w / 2, y + 17, c, t, 11, t["on"][k], "middle"))
            x += w + (0 if k == last else 2)
        body.append(text(x + 8, y + 17, f"{counts[0]} of {counts[0] + counts[1]} run passed"
                         if counts[0] + counts[1] else "needs a local model", t, 12, "ink2"))
    return svg(t, h, title, desc, body)


# ---- the charts -------------------------------------------------------------------------------

def line_desc(xs: list, series: list, what: str) -> str:
    """Every point in words, for the SVG's description."""
    return "Line chart. " + "; ".join(
        f"{n}: " + ", ".join(f"{x:,} {what} {secs(v)}" for x, v in zip(xs, vs, strict=True))
        for n, vs in series)


def charts(r: dict, t: dict) -> dict[str, str]:
    """{name: svg text} in theme `t`, for every chart whose section results.json has."""
    out = {}
    if "index" in r:
        out["startup"] = startup_chart(r, t)
    if "vectors" in r:
        out["semantic"] = semantic_chart(r, t)
    if "chat_data" in r:
        out["chat-data"] = chat_data_chart(r, t)
    if "chat_test" in r:
        out["chat-test"] = chat_test_chart(r, t)
    return out


def startup_chart(r: dict, t: dict) -> str:
    pts = r["index"]["points"]
    xs = [p["notes"] for p in pts]
    s = [("Cold index build", [p["cold_build_s"] for p in pts]),
         ("Index from its snapshot", [p["snapshot_build_s"] for p in pts]),
         ("MCP server ready (later starts)", [p["mcp_next_start_s"] for p in pts])]
    return line_chart(
        t, "Index and MCP start time by vault size",
        f"Median of {r['runs']} runs, seconds; synthetic vaults (scripts/bench_index.py)",
        xs, "Notes in the vault", s, "s",
        line_desc(xs, s, "notes"))


def semantic_chart(r: dict, t: dict) -> str:
    pts = r["vectors"]["points"]
    xs = [p["pages"] for p in pts]
    s = [("Search by meaning (warm)", [p["query_s"] for p in pts]),
         ("First search after a start", [p["cold_start_s"] for p in pts]),
         ("Add one page", [p["add_page_s"] for p in pts])]
    unit = "ms" if max(v for _, vs in s for v in vs) < 1 else "s"
    return line_chart(
        t, "Semantic search time by vault size",
        f"Median of {r['runs']} runs, {'milliseconds' if unit == 'ms' else 'seconds'}; "
        "about 10 chunks a page (scripts/bench_scale.py vectors)",
        xs, "Pages in the vault", s, unit,
        line_desc(xs, s, "pages"))


def chat_data_chart(r: dict, t: dict) -> str:
    calls = r["chat_data"]["calls"]
    rows = [(label, calls[k]["after_s"]) for k, label in CHAT_CALLS.items() if k in calls]
    return hbars(
        t, "Chat's answers from your own data",
        f"Median of {r['runs']} runs, after the first call; heavy synthetic vault "
        "(scripts/bench_chat_data.py)", rows,
        "Bar chart. " + "; ".join(f"{n}: {secs(v)}" for n, v in rows))


def chat_test_chart(r: dict, t: dict) -> str:
    ct = r["chat_test"]
    ev = ct["by_query_kind"]
    rows = [(label, [ev[k]["pass"], ev[k]["fail"], ev[k]["needs_model"]])
            for k, label in QUERY_KINDS.items() if k in ev]
    return stacked(
        t, "The 40-question Chat test, with no model",
        f"{ct['passed']} of {ct['ran']} questions Keepfold answers from its own data passed; "
        f"{ct['needs_model']} need a local model", rows, ("Pass", "Fail", "Needs a local model"),
        "Stacked bar chart by the kind of search a question needs. " + "; ".join(
            f"{n}: {c[0]} pass, {c[1]} fail, {c[2]} need a local model" for n, c in rows))


def write(r: dict, out: Path = OUT) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for mode, t in THEMES.items():
        for name, body in charts(r, t).items():
            p = out / f"{name}-{mode}.svg"
            p.write_text(body, encoding="utf-8", newline="\n")
            paths.append(p)
    return paths


# ---- README.md's measured blocks --------------------------------------------------------------

def picture(name: str, alt: str) -> str:
    base = "docs/images/charts/" + name
    return (f'<picture><source media="(prefers-color-scheme: dark)" srcset="{base}-dark.svg">'
            f'<img src="{base}-light.svg" alt="{escape(alt, {chr(34): "&quot;"})}"></picture>')


def table(head: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    return "\n".join(lines + ["| " + " | ".join(str(c) for c in row) + " |" for row in rows])


def blocks(r: dict) -> dict[str, str]:
    """{name: markdown} for each `<!-- numbers:name -->` block of README.md."""
    m = r["machine"]
    busy = r.get("load", {}).get("cpu_percent_median")
    timed = [k for k in TIMED if k in r]
    out = {name: PENDING for name in BLOCKS}
    out["machine"] = (
        f"Measured on {r['generated'][:10]} at commit `{r['git_sha']}`, on {m['cpu']} "
        f"({m['logical_cores']} logical cores), {m['os']}, Python {m['python']}"
        + (f"; each time is the median of {r['runs']} runs." if timed else ".")
        + (f" The processor was {busy:.0f}% busy before the runs (median of the samples; other "
           "programs were running), so these times are likely slower than on an idle machine."
           if timed and busy is not None and busy >= 50 else "")
        + ("" if len(timed) == len(TIMED) else
           " A section that says *Not measured yet* has no numbers in this run."))
    if "index" in r:
        out.update(startup_block(r))
    if "vectors" in r:
        out.update(semantic_block(r))
    if "chat_data" in r:
        out.update(chat_data_block(r))
    if "chat_test" in r:
        out.update(chat_test_block(r))
    if "browse" in r:
        out.update(browse_block(r))
    if "tests" in r:
        out["tests"] = (f"{r['tests']['collected']:,} automated tests "
                        f"(`{r['tests']['command']}`).")
    return out


def startup_block(r: dict) -> dict[str, str]:
    out = {}
    pts = r["index"]["points"]
    out["startup"] = picture("startup", "Index and MCP start time by vault size: " + "; ".join(
        f"{p['notes']:,} notes: cold build {secs(p['cold_build_s'])}, from the snapshot "
        f"{secs(p['snapshot_build_s'])}, MCP server ready {secs(p['mcp_next_start_s'])}"
        for p in pts)) + "\n\n" + table(
        ["Notes", "Cold index build", "Index from its snapshot", "MCP ready, first start",
         "MCP ready, later starts"],
        [[f"{p['notes']:,}", secs(p["cold_build_s"]), secs(p["snapshot_build_s"]),
          secs(p["mcp_first_start_s"]), secs(p["mcp_next_start_s"])] for p in pts])
    return out


def semantic_block(r: dict) -> dict[str, str]:
    out = {}
    pts = r["vectors"]["points"]
    out["semantic"] = picture("semantic", "Semantic search time by vault size: " + "; ".join(
        f"{p['pages']:,} pages: search {secs(p['query_s'])}, first search after a start "
        f"{secs(p['cold_start_s'])}, add one page {secs(p['add_page_s'])}" for p in pts)) + (
        "\n\n" + table(["Pages", "Chunks", "Search by meaning (warm)", "First search after a start",
                        "Add one page"],
                       [[f"{p['pages']:,}", f"{p['chunks']:,}", secs(p["query_s"]),
                         secs(p["cold_start_s"]), secs(p["add_page_s"])] for p in pts]))
    return out


def chat_data_block(r: dict) -> dict[str, str]:
    out = {}
    calls = r["chat_data"]["calls"]
    rows = [(label, calls[k]) for k, label in CHAT_CALLS.items() if k in calls]
    out["chat-data"] = picture("chat-data", "Chat's answers from your own data: " + "; ".join(
        f"{n} {secs(c['after_s'])}" for n, c in rows)) + "\n\n" + table(
        ["Question", "First call after an import", "Later calls"],
        [[n, secs(c["first_s"]), secs(c["after_s"])] for n, c in rows])
    return out


def chat_test_block(r: dict) -> dict[str, str]:
    out = {}
    ct = r["chat_test"]
    ev = ct["by_query_kind"]
    rows2 = [(label, ev[k]) for k, label in QUERY_KINDS.items() if k in ev]
    out["chat-test"] = picture("chat-test", f"The Chat test with no model: {ct['passed']} of "
                               f"{ct['ran']} data questions passed, {ct['needs_model']} need a "
                               "local model. " + "; ".join(
                                   f"{n}: {c['pass']} pass, {c['fail']} fail, {c['needs_model']} "
                                   "need a model" for n, c in rows2)) + "\n\n" + table(
        ["The search a question needs", "Pass", "Fail", "Needs a local model"],
        [[n, c["pass"], c["fail"], c["needs_model"]] for n, c in rows2]
        + [["**All 40**", f"**{ct['passed']}**", f"**{ct['failed']}**",
            f"**{ct['needs_model']}**"]])
    return out


def browse_block(r: dict) -> dict[str, str]:
    b = r["browse"]
    return {"browse": table(
        ["Browsing history (synthetic, 5,000 sites over a year)", "Time"],
        [[f"First import of {b['visits']:,} visits", secs(b["first_import_s"])],
         ["The next import with nothing new", secs(b["nothing_new_s"])],
         ["A live read of 50 new visits (the app does this every 10 minutes)",
          secs(b["live_read_50_s"])]])}


def fill(text: str, parts: dict[str, str]) -> str:
    """Replace each `<!-- numbers:k -->...<!-- /numbers:k -->` body with parts[k]."""
    for k, body in parts.items():
        k = re.escape(k)
        text = re.sub(rf"(<!-- numbers:{k} -->\n)(?:.*?\n)?(<!-- /numbers:{k} -->)",
                      lambda mo, b=body: mo[1] + b + "\n" + mo[2], text, flags=re.S)
    return text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, default=RESULTS)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--readme", type=Path, default=REPO / "README.md")
    ap.add_argument("--no-readme", action="store_true", help="draw the charts only")
    args = ap.parse_args()
    r = json.loads(args.results.read_text(encoding="utf-8"))
    for p in write(r, args.out):
        print(p.relative_to(REPO) if p.is_relative_to(REPO) else p)
    if not args.no_readme:
        old = args.readme.read_text(encoding="utf-8")
        new = fill(old, blocks(r))
        if new != old:
            args.readme.write_text(new, encoding="utf-8", newline="\n")
        print(f"{args.readme.name}: {'updated' if new != old else 'unchanged'}")


if __name__ == "__main__":
    main()
