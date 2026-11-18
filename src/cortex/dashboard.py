"""The offline dashboard (SPEC §0 U2, §10; docs/design/dashboard.md).

One self-contained `.cortex/dashboard.html`: the data blob, d3 v7 and the page script are
inlined, and the CSP allows only those scripts by hash. Note text reaches the page as plain
strings (links, images and tags stripped) and the page script inserts it with `textContent`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import string
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from cortex import (
    brainmap,
    know,
    learning_eval,
    levels,
    loops,
    needs,
    planner,
    probes,
    reflect,
    topics,
)
from cortex.catalog import Catalog
from cortex.events import EventLog, local_day
from cortex.index import _edge_targets, link_targets
from cortex.render import index_md, plain
from cortex.vault import CHECKED_RE, HEADING_RE, TOOL_PROPS, UNCHECKED_RE, Vault, is_private

ASSETS = Path(__file__).parent / "assets"
YOU = "@you"  # not a valid note id, so it never collides with one
EVIDENCE_SHOWN, WEEKS, MISTAKE_DAYS = 10, 8, 30
ITEM_TYPE = {"problem_status": "problem", "self_report": "self report"}
_DAY_RE = re.compile(r"^Day (\d+)$")
_SLOT = re.compile(r"\{\{(\w+)\}\}")
BODY_MAX = 20 * 1024
TRUNCATED = "\n\n*… truncated here; open the note in Obsidian for the rest.*\n"
SHOWN_PROPS = 30  # list items and characters below are capped too, to keep the blob small
REPO = Path(__file__).resolve().parents[2]  # the checkout; the help files are absent when installed
GLOSSARY = "The names, in plain words"  # the GUIDE heading after its number
_NAME_RE = re.compile(r"\*\*Name:\*\*\s*(\S+)")
UNFILED = "unfiled"
GROUP_TITLES = {"journal": "Journal", "you": "About you", "sessions": "Study sessions",
                "placements": "Tests and applications",
                "projects": "Projects, decisions and loops", "people": "People",
                "media": "YouTube and NotebookLM", "chats": "Unsorted chats",
                "browsing": "Browsing"}
GROUPS = {"journal": "journal", "me": "you", "session": "sessions",  # page kind -> group lobe
          "browse": "browsing",  # browse-brain.md §4; private browse pages are never read
          **dict.fromkeys(("assessment", "application", "track"), "placements")}
NO_HUB = {"project": "projects", "decision": "projects", "loop": "projects", "person": "people"}
MEDIA, CHATS = {"youtube", "notebooklm"}, {"claude", "gemini", "perplexity"}


def _title(note) -> str:
    return plain(note.fields.get("title") or note.id) or note.id


def _sprint(track) -> dict | None:
    if track is None:
        return None
    cur = track.fields.get("current_day")
    cur = cur if isinstance(cur, int) and not isinstance(cur, bool) else None
    nums, done, left, inside, level = [], 0, 0, False, 0
    for line in track.body.splitlines():
        m = HEADING_RE.match(line)
        if m:
            d = _DAY_RE.match(m.group(2))
            if d:
                nums.append(int(d.group(1)))
            if inside and len(m.group(1)) <= level:
                inside = False
            if cur is not None and m.group(2) == f"Day {cur}":
                inside, level = True, len(m.group(1))
            continue
        if inside:
            done += bool(CHECKED_RE.match(line))
            left += bool(UNCHECKED_RE.match(line))
    last = max(nums + [cur or 0], default=0)
    days = [{"n": n, "label": f"Day {n}",
             "status": "upcoming" if cur is None or n > cur else "current" if n == cur
             else "done"} for n in range(1, last + 1)]
    pd = planner._day(track.fields.get("placement_date"))
    return {"day": cur, "days": days, "placement_date": pd.isoformat() if pd else None,
            "today_counts": {"done": done, "left": left}}


def build_data(vault: Vault, *, now: datetime, relayout: bool = False) -> dict:
    """The dashboard blob (docs/design/dashboard.md "Data blob"); private notes never enter it.
    `relayout` lays the brain out from scratch (brainmap)."""
    catalog = Catalog.load(vault)
    day = local_day(now)
    today_d = date.fromisoformat(day)
    events = list(EventLog(vault).iter_events())
    notes = {k: list(vault.iter_notes(k)) for k in Vault.KIND_DIRS}  # parsing dominates; once
    pc = levels.problem_concepts(vault)
    states, mstates = levels.fold(events, day, pc, planner.exam_concepts(vault, catalog, day))
    private = {n.id for ns in notes.values() for n in ns if is_private(n)}
    hidden = planner.hidden(vault, catalog, private)
    entries = [e for e in catalog.entries.values() if e.id not in hidden]
    concepts = [e for e in entries if e.kind == "concept"]
    visible = {e.id for e in concepts}
    shown = {e.id for e in entries}

    def state(c):
        return states[c].state if c in states else "not_started"

    tally = Counter(state(e.id) for e in concepts)
    counts = {s: tally[s] for s in ("solid", "learning", "needs_refresh", "unverified",
                                    "not_started")}

    items = [i for i in planner.today(vault, catalog, states, now=now, events=events)
             if i["id"] not in hidden]
    today = [{"kind": i["kind"], "id": i["id"], "title": plain(i["title"]),
              "state": i["state"], "why": plain(i["why"])} for i in items]

    scores = {n.id: n.fields.get("score") for n in notes["assessment"]}
    pairs = learning_eval.history_pairs(events, pc, learning_eval.MIN_PAIRS)
    calibrated = pairs >= learning_eval.MIN_PAIRS

    def assessment(a):
        d = planner._day(a["date"]) if a["date"] else None
        tested = [c for c in planner._weights(catalog, a["tests"]) if c in visible]
        doms = {catalog.entries[c].domain for c in tested}
        score = scores.get(a["id"])
        return {"id": a["id"], "company": plain(a["company"] or "") or None,
                "kind": plain(a["kind"] or "") or None, "date": d.isoformat() if d else None,
                "days_left": (d - today_d).days if d else None,
                "outcome": a["outcome"] or "pending",
                "score": plain(str(score)) if score is not None else None,
                "readiness": {"solid": sum(state(c) == "solid" for c in tested),
                              "total": len(tested),
                              "share": planner.readiness(vault, catalog, states, a),
                              # a forecast beside the Solid share, only for a day still ahead
                              # and once calibrated, as on the home card; until then, why not
                              "forecast": planner.recall_on_day(vault, catalog, states, a, now)
                              if d and d >= today_d and calibrated else None,
                              "calibrating": None if calibrated or not d or d < today_d
                              else {"n": pairs, "need": learning_eval.MIN_PAIRS}},
                "domains": sorted(catalog.entries[x].title for x in doms if x in catalog.entries)}

    rows = [assessment(a) for a in planner.assessments(vault) if a["id"] not in private]
    coming = [a for a in rows if a["outcome"] == "pending"
              and (a["days_left"] is None or a["days_left"] >= 0)]
    coming.sort(key=lambda a: (a["days_left"] is None, a["days_left"] or 0))

    mtitle = {n.id: _title(n) for n in notes["mistake"]}
    mistakes = sorted(({"id": m.id, "title": mtitle.get(m.id, m.id), "count": m.count,
                        "last_day": m.last_day, "status": m.status}
                       for m in mstates.values() if m.id not in private),
                      key=lambda m: (m["status"] != "open", -_ord(m["last_day"]), m["id"]))

    # graph
    nodes = [{"id": YOU, "kind": "you", "title": "You", "state": None, "domain": None,
              "area": None, "degree": 0, "open_mistakes": 0, "next_due": None}]
    links = []
    for e in entries:
        st = states.get(e.id)
        nodes.append({"id": e.id, "kind": e.kind, "title": plain(e.title) or e.id,
                      "state": state(e.id) if e.kind == "concept" else None,
                      "domain": e.domain, "area": e.parent if e.kind == "concept" else
                      e.id if e.kind == "area" else None, "degree": 0,
                      "open_mistakes": len([m for m in st.open_mistakes if m not in private])
                      if st else 0, "next_due": st.next_due if st else None})
        links.append({"s": YOU if e.kind == "domain" else e.parent, "t": e.id,
                      "type": "you" if e.kind == "domain" else "hier"})
        links += [{"s": e.id, "t": r, "type": "requires"} for r in e.requires
                  if r in visible and r != e.id]
        # extra areas are thin non-tree links; the parent edge above stays the tree
        links += [{"s": a, "t": e.id, "type": "area"} for a in e.areas if a in shown]
    degree = Counter([lk["s"] for lk in links] + [lk["t"] for lk in links])
    for n in nodes:
        n["degree"] = degree[n["id"]]

    # concept details
    asks = {e["id"]: e for e in events if e.get("kind") == "ask"}
    trail: dict[str, list[dict]] = {}
    answers = Counter()
    for it in levels._items(events, pc):  # void and remap applied, as in the levels fold
        ev = it.ev
        if ev.get("kind") == "grade" and it.concept not in hidden:
            answers[ev.get("day")] += 1
        if it.concept in visible and it.verdict is not None:
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            kind = (ev.get("item_type") or asks.get(ev.get("ref"), {}).get("item_type")
                    or ITEM_TYPE.get(payload.get("kind")))
            trail.setdefault(it.concept, []).append({
                "day": ev.get("day"), "verdict": it.verdict,
                "item_type": plain(kind) if kind else None,
                "source": plain(ev.get("source") or "") or None})
    required_by: dict[str, list[str]] = {}
    for lk in links:
        if lk["type"] == "requires":
            required_by.setdefault(lk["t"], []).append(lk["s"])
    problems: dict[str, list[dict]] = {}
    for p in notes["problem"]:
        if p.id in private:
            continue
        row = {"id": p.id, "title": _title(p), "status": plain(p.fields.get("status") or "")
               or None}
        for c in dict.fromkeys(pc.get(p.id, [])):
            if c in visible:
                problems.setdefault(c, []).append(row)
    details = {}
    for e in concepts:
        st = states.get(e.id)
        d = {"evidence": trail.get(e.id, [])[-EVIDENCE_SHOWN:][::-1],
             "requires": [r for r in e.requires if r in visible and r != e.id],
             "required_by": required_by.get(e.id, []),
             "problems": problems.get(e.id, []),
             "mistakes": [m for m in st.open_mistakes if m not in private] if st else []}
        if any(d.values()):
            details[e.id] = d

    # history
    monday = today_d - timedelta(days=today_d.weekday())
    weeks = []
    for i in range(WEEKS - 1, -1, -1):
        start = monday - timedelta(weeks=i)
        week = {(start + timedelta(days=j)).isoformat() for j in range(7)}
        weeks.append({"start": start.isoformat(),
                      "answers": sum(n for d, n in answers.items() if d in week)})
    cut = (today_d - timedelta(days=MISTAKE_DAYS)).isoformat()
    _, before = levels.fold([e for e in events if str(e.get("day", "")) <= cut], cut, pc,
                            planner.exam_concepts(vault, catalog, cut))

    def moved(status):
        return sum(1 for m in mstates.values() if m.id not in private and m.status == status
                   and getattr(before.get(m.id), "status", None) != status)

    pages, clusters = _pages(vault, notes, hidden, catalog, pc)
    parsed = _Parsed(vault.root, notes)  # loops and know read notes through this: parsed once
    loop_notes = {n.id: n for n in notes["loop"]}
    open_needs = [_plained(r, "question", "area", "task")
                  | {"options": [plain(o) for o in r["options"]]
                     if isinstance(r["options"], list) else []}
                  for r in needs.open(events, day)]
    known = know.report(parsed, events, day)
    known["needs"] = open_needs
    for pg in known["pages"]:
        if "contested" in pg:
            pg["contested"] = [plain(t, 200) for t in pg["contested"]]
    profile = next((n for n in notes["me"] if n.id == "profile" and not is_private(n)), None)
    m = _NAME_RE.search(profile.body) if profile else None
    name = plain(m.group(1).strip(string.punctuation)) if m else ""

    return {
        "as_of": now.astimezone().isoformat(timespec="seconds"),
        "sprint": _sprint(next((t for t in notes["track"] if t.id not in private), None)),
        "counts": counts,
        "today": today,
        "plan_cleared": not today,
        "assessments": coming,
        "mistakes": mistakes,
        "graph": {"nodes": nodes, "links": links},
        "vault_name": vault.root.name,
        "pages": pages,
        "details": details,
        "history": {"weeks": weeks, "assessments": rows,
                    "mistakes": {"opened": moved("open"), "resolved": moved("resolved")}},
        # council item 6: how often each stated confidence (1-4) was right; display-only
        "confidence": learning_eval.confidence_mirror(events, pc, hidden),
        "owner": {"name": name} if name else None,
        "clusters": clusters,
        "brain": brainmap.build(vault, pages, clusters, relayout=relayout),
        "counts_by_kind": dict(Counter(p["kind"] for p in pages)),
        "loops": [_plained(r, "title", "summary", "loop_type")
                  | {"created": loops.created(loop_notes[r["id"]]).isoformat()}
                  for r in loops.loops(parsed)],
        "follow_ups": [_plained(r, "title") for r in loops.follow_ups(parsed, day)],
        "resurface": [_plained(r, "title", "summary")
                      for r in loops.resurface(parsed, day, limit=5, events=events)],
        "needs": open_needs,
        "you": {"know": known, "unconfirmed": reflect.unconfirmed(vault, day)[0],
                "probes": probes.summary(events)},
        "help": _help(),
    }


class _Parsed(SimpleNamespace):
    """A read-only stand-in for the vault that serves the notes build_data already parsed."""

    def __init__(self, root: Path, notes: dict):
        super().__init__(root=root, notes=notes)

    def iter_notes(self, kind: str | None = None):
        return iter(self.notes[kind] if kind else [n for ns in self.notes.values() for n in ns])


def _plained(row: dict, *keys: str) -> dict:
    return row | {k: plain(row[k], 300) for k in keys if row.get(k) is not None}


def _help() -> dict:
    """The `/cortex help` reference and the GUIDE glossary table (trusted repo files)."""
    cmds, guide = REPO / "skills" / "cortex" / "modes" / "help.md", REPO / "docs" / "GUIDE.md"
    text = guide.read_text(encoding="utf-8") if guide.is_file() else ""
    # match the heading by name, so renumbering the guide's sections can't lose the glossary
    head = next((ln[3:].strip() for ln in text.splitlines()
                 if ln.startswith("## ") and ln.rstrip().endswith(GLOSSARY)), None)
    rows = [ln for ln in loops.section_text(text, head).splitlines()
            if ln.startswith("|")] if head else []
    return {"commands_md": cmds.read_text(encoding="utf-8") if cmds.is_file() else None,
            "glossary_md": "\n".join(rows) or None}


def _lobes(keep: dict, catalog: Catalog, hidden: set, pc: dict
           ) -> tuple[dict[str, str], list[dict]]:
    """Page id -> its brain lobe id, and the lobes [{id, title, kind, count}], biggest first.
    A lobe is a top hub (no parent, or a child of the topic map; never the map itself) under its
    hub id, a catalog domain as `domain:<id>`, a GROUPS lobe, or `unfiled`. A hub sits in its
    top hub's lobe; a concept, area or domain page in its domain's; a problem or mistake in the
    domain of its first visible concept; journal, me, session, placement and browse pages in
    their group; any other page in the lobe of its first hub under a top hub, else (projects,
    decisions, loops, people, media and chat resources) in a group; the rest are unfiled.
    A group lobe id that a hub already uses gets a `-group` suffix."""
    idx = {i: {"kind": n.kind, "title": _title(n), "fields": n.fields} for i, n in keep.items()}
    t = topics.tree(SimpleNamespace(_notes=idx))  # tree reads only `_notes`; no search index

    def top(h):
        p = t.path(h)
        return p[1] if p[0] == topics.ROOT and len(p) > 1 else p[0]

    def domain(i, fields=None):
        e = catalog.entries.get(i)
        d = (e.domain if e else "") or str((fields or {}).get("domain") or "")
        return f"domain:{d}" if d in catalog.entries and not {d, i} & hidden else None

    gid = {g: g + "-group" if g in t.parent else g for g in GROUP_TITLES}

    def no_hub(i, k, f):
        if k != "resource":
            return NO_HUB.get(k)
        src = topics.page_source(keep[i].body).lower()
        tags = f.get("tags")
        tags = tags if isinstance(tags, list) else str(tags or "").replace(",", " ").split()
        if src in MEDIA or MEDIA & {str(x).lstrip("#").lower() for x in tags}:
            return "media"
        return "chats" if src in CHATS else None

    lobe = {}
    for i, n in idx.items():
        k, f = n["kind"], n["fields"]
        if i in t.parent:
            lobe[i] = top(i)
        elif k in ("concept", "area", "domain"):
            lobe[i] = domain(i, f)
        elif k in ("problem", "mistake"):
            cs = pc.get(i, []) if k == "problem" else _edge_targets(f.get("concepts") or [])
            lobe[i] = next(filter(None, map(domain, cs)), None)
        elif k in GROUPS:
            lobe[i] = gid[GROUPS[k]]
        else:
            lobe[i] = next((x for x in map(top, topics.page_hubs(n, t.parent))
                            if x != topics.ROOT), None) or gid.get(no_hub(i, k, f))
        if lobe[i] in (None, topics.ROOT):
            lobe[i] = UNFILED

    titles = {gid[g]: title for g, title in GROUP_TITLES.items()}

    def row(c, k):
        if c == UNFILED:
            return {"id": c, "title": "Unfiled", "kind": UNFILED, "count": k}
        if c in titles and c not in t.parent:
            return {"id": c, "title": titles[c], "kind": "group", "count": k}
        if c.startswith("domain:"):
            return {"id": c, "title": plain(catalog.entries[c[7:]].title) or c[7:],
                    "kind": "domain", "count": k}
        return {"id": c, "title": idx[c]["title"], "kind": "topic", "count": k}

    count = Counter(lobe.values())
    return lobe, [row(c, k) for c, k in sorted(count.items(), key=lambda x: (-x[1], x[0]))]


def _prop(v):
    if isinstance(v, list):
        return [str(x)[:300] for x in v[:SHOWN_PROPS]]
    return str(v)[:300]


def _pages(vault: Vault, notes: dict, hidden: set, catalog: Catalog, pc: dict
           ) -> tuple[list[dict], list[dict]]:
    """Every non-private note for the reader view (phase4-wiki §8; `raw/` is not a kind), and
    the brain lobes (`_lobes`)."""
    keep = {}
    for ns in notes.values():
        for n in ns:
            if n.id not in hidden and not is_private(n):
                keep.setdefault(n.id, n)  # an id clash keeps the first kind, as wikilinks do
    lobe, clusters = _lobes(keep, catalog, hidden, pc)
    pages = []
    for n in keep.values():
        f = n.fields
        tags = f.get("tags")
        tags = tags if isinstance(tags, list) else str(tags or "").replace(",", " ").split()
        body = n.body
        if len(body.encode()) > BODY_MAX:
            body = body.encode()[:BODY_MAX].decode(errors="ignore") + TRUNCATED
        text = " ".join(str(v) for v in f.values()) + n.body
        pages.append({
            "id": n.id, "kind": n.kind, "title": _title(n),
            "summary": plain(f.get("summary") or "", 300) or None,
            "tags": [str(t).lstrip("#") for t in tags if str(t).strip("#")][:SHOWN_PROPS],
            "props": {str(k): _prop(v) for k, v in f.items()
                      if k not in TOOL_PROPS | {"id", "title", "summary", "private"}},
            "body": body,
            "links": [x for x in dict.fromkeys(link_targets(text))
                      if x in keep and x != n.id],
            "path": n.path.relative_to(vault.root).as_posix(),
            "updated": date.fromtimestamp(n.path.stat().st_mtime).isoformat(),
            "date": topics.page_date(n.id, f, n.body) or None,
            "source": plain(topics.page_source(n.body)) or None,
            "cluster": lobe[n.id]})
    return pages, clusters


def _ord(day) -> int:
    return date.fromisoformat(day).toordinal() if day else 0


def _sha(script: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() + "'"


def render_html(data: dict) -> str:
    """One self-contained page: the template with the CSS, data blob and scripts inlined."""
    read = lambda name: (ASSETS / name).read_text(encoding="utf-8")  # noqa: E731
    lic = read("LICENSE-d3.txt").strip()
    # The XML namespace strings are not fetched; `\/` keeps them from reading as URLs.
    d3 = f"/*! d3 v7.9.0 | ISC License\n{lic}\n*/\n" + read("d3.v7.min.js").replace(
        '"http://www.w3.org', '"http:\\/\\/www.w3.org')
    brain, app = read("brain3d.js"), read("dashboard.js")
    csp = (f"default-src 'none'; script-src {_sha(d3)} {_sha(brain)} {_sha(app)}; "
           "style-src 'unsafe-inline'; img-src data:")
    # `:\/\/` is the same JSON string; note URLs then never read as fetchable URLs in the page
    blob = (json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace("://", ":\\/\\/"))
    slots = {"CSP": csp, "CSS": read("dashboard.css"), "DATA": blob, "D3": d3, "BRAIN": brain,
             "APP": app}
    return _SLOT.sub(lambda m: slots[m.group(1)], read("dashboard.html.tmpl"))


def write_dashboard(vault: Vault, *, now: datetime, relayout: bool = False) -> Path:
    """Regenerate `<vault>/.cortex/dashboard.html`."""
    return vault.write_text(vault.root / ".cortex" / "dashboard.html",
                            render_html(build_data(vault, now=now, relayout=relayout)))


def write_index(vault: Vault, *, now: datetime) -> Path:
    """Regenerate `<vault>/index.md`, the one-file text map of every non-private page. Like
    the dashboard it is built on demand (`cortex dashboard`), never at session close."""
    catalog = Catalog.load(vault)
    after, _ = planner.current_states(vault, local_day(now), catalog)
    private = planner.hidden(vault, catalog)
    notes = [n for n in vault.iter_notes() if not is_private(n) and n.id not in private]
    return vault.write_text(vault.root / "index.md", index_md(
        notes, catalog.entries, {c: s.state for c, s in after.items()}))
