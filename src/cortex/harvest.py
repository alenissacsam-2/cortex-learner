"""Harvest the imported chat pages into the wiki (docs/design/v6/arena/harvest-verdict.md).

No LLM here: an attended agent session asks `cortex_chats(action="harvest")` for a bundle,
writes notes, decisions, hub lines and about-me proposals itself, then logs `harvested`.
A bundle packs whole topic hubs (a chat belongs to its first hub; unfiled chats come last, one
unit per month) up to chats.PART_CHARS of the user's own redacted turns, best hub first: the
hub's member centroid against its nearest concept head. Same-day, same-app copies are folded
under the longest page. Progress is a fold over live `harvested` events, so a void or
`cortex undo` re-queues chats; nothing else is stored and no imported page is edited.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections import defaultdict

import numpy as np

from cortex import chats, claims, topics
from cortex.chat_import import MINE
from cortex.events import live
from cortex.index import GLYPHS, _wrap, link_targets
from cortex.reflect import unconfirmed
from cortex.render import plain
from cortex.semantic import is_chat
from cortex.vault import ID_RE, VaultError, is_private

DUPE_COS = 0.97  # REVIEW-1005 §1.5: same-day re-logs, not cross-app copies
CONCEPT_COS = 0.68  # above the everyday hubs' p90 (0.64) to their nearest concept head
CONCEPTS_MAX = 5
QUIET_WEIGHT = 0.4  # replaces semantic.CHAT_WEIGHT (0.6) for a harvested chat
KNOW_CHARS = 1200
CHATS_MAX, PAGES_MAX, FOUND_MAX = 80, 50, 50  # per bundle / harvested event
OUTCOMES = ("harvested", "empty", "skipped")
KNOW = "What I explored"  # the user's questions show interest, not knowledge
OLD_KNOW = "What I know"  # hand- or LLM-written on older hubs: read, never written or renamed
KEEP_DAYS = 7
ME = "**Me:**"


def user_turns(body: str) -> list[str] | None:
    """The text of each `**Me:**` turn of an imported chat page, up to the page app's own
    speaker line (`**Gemini:**` on a Gemini page). None when a turn's end cannot be found (no
    `Source:` app, no turn, or a `**Me:**` with no reply before the next one or the end): the
    caller then serves the title only, never a guess that could hold assistant text. A page
    whose Timeline line says MINE (chat_import's) holds no reply: a turn ends at the next
    `### ` or `**Me:**` line, or the end."""
    app = topics.page_source(body)
    if not app:
        return None
    mine = MINE in (topics.TL_RE.search(body) or [""])[0]
    them, out, cur = f"**{app}:**", [], None
    for ln in body.splitlines():
        if mine and cur is not None and ln.startswith(("### ", ME)):
            out.append("\n".join(cur).strip())
            cur = None
        if ln.startswith(ME):
            if cur is not None:
                return None
            cur = [ln[len(ME):]]
        elif ln.startswith(them):
            if cur is not None:
                out.append("\n".join(cur).strip())
            cur = None
        elif cur is not None:
            cur.append(ln)
    if mine and cur is not None:
        out.append("\n".join(cur).strip())
        cur = None
    return None if cur is not None or not out else out


def fold(events) -> tuple[set[str], set[str], dict[str, str]]:
    """(chats done, hubs skipped, {copy: keeper}) over the live `harvested` log events."""
    done, skipped, dupes = set(), set(), {}
    for e in live(events):
        p = e.get("payload")
        if e.get("kind") != "log" or not isinstance(p, dict) or p.get("kind") != "harvested":
            continue
        if p.get("outcome") == "skipped":
            skipped.add(p.get("hub"))
        else:
            done.update(p.get("chats") or ())
            dupes.update(p.get("dupes") or {})
    return done, skipped, dupes


def quiet(events) -> dict[str, float]:
    """Recall weights for semantic's `quiet`: 0 hides a folded copy, QUIET_WEIGHT a harvested
    (or empty) chat."""
    done, _, dupes = fold(events)
    return {**dict.fromkeys(done, QUIET_WEIGHT), **dict.fromkeys(dupes, 0.0)}


def _hub_sims(index, t, hv) -> tuple[list[str], list[str], np.ndarray]:
    """(hub ids, concept ids, hub x concept cosine): member chunk-0 centroid to concept head."""
    mem = topics.members(index, t)
    hubs = [h for h in sorted(mem) if any(p in hv for p in mem[h])]
    cids = sorted(i for i, n in index._notes.items() if n["kind"] == "concept" and i in hv)
    if not hubs or not cids:
        return hubs, cids, np.zeros((len(hubs), len(cids)), np.float32)
    cent = np.stack([np.sum([hv[p] for p in mem[h] if p in hv], axis=0) for h in hubs])
    cent /= np.maximum(np.linalg.norm(cent, axis=1, keepdims=True), 1e-9)
    return hubs, cids, cent @ np.stack([hv[c] for c in cids]).T


def private_hub(vault, id: str, notes) -> bool:
    """True for a topic hub on disk that the index leaves out: a private one (review r4 L3)."""
    if id in notes or not isinstance(id, str) or not ID_RE.fullmatch(id) \
            or not vault.path_for("note", id).is_file():
        return False
    try:
        n = vault.read("note", id)
    except VaultError:  # unreadable frontmatter: the index skipped it for that, not privacy
        return False
    tags = n.fields.get("tags")
    return is_private(n) and isinstance(tags, list) and "topic" in tags


def queue(index, t, stack, done, skipped, within: str | None = None) -> list[dict]:
    """Units to harvest, best first: [{unit, chats (newest first), newest, score, concepts
    [(id, cos)]}]. A hub's score is its centroid's best concept cosine (then its newest chat);
    unfiled chats come last as `month:YYYY-MM` units, newest month first. Done chats, chats
    under a skipped hub, chats whose `Topics:` line names a private hub and, with `within`
    (a hub), chats outside its subtree are left out."""
    hubs, cids, sims = _hub_sims(index, t, topics.heads(stack))
    row = {h: k for k, h in enumerate(hubs)}
    units: dict[str, list[tuple[str, str]]] = defaultdict(list)
    private: dict[str, bool] = {}  # a hub id named on a Topics: line -> private on disk
    for i, n in index._notes.items():
        if i in done or not is_chat(n):
            continue
        line = topics.TOP_RE.search(n["body"])
        if line and any(private.setdefault(h, private_hub(index.vault, h, index._notes))
                        for h in link_targets(line.group(0))):
            continue
        home = next(iter(topics.page_hubs(n, t.parent)), None)
        path = t.path(home) if home else []
        if skipped.intersection(path) or (within and within not in path):
            continue
        day = topics.page_date(i, n["fields"], n["body"])
        units[home or f"month:{day[:7]}"].append((day, i))
    out = []
    for u, rows in units.items():
        rows.sort(reverse=True)
        s = sims[row[u]] if u in row else np.zeros(0)
        top = [(cids[j], float(s[j])) for j in np.argsort(-s, kind="stable")[:CONCEPTS_MAX]
               if s[j] >= CONCEPT_COS]
        out.append({"unit": u, "chats": [i for _, i in rows], "newest": rows[0][0],
                    "score": float(s.max()) if len(s) else 0.0, "concepts": top})
    month = [x for x in out if x["unit"].startswith("month:")]
    return (sorted((x for x in out if x not in month), reverse=True,
                   key=lambda x: (round(x["score"], 2), x["newest"], x["unit"]))
            + sorted(month, key=lambda x: x["unit"], reverse=True))


def _norm_turns(body: str) -> set[str] | None:
    turns = user_turns(body)
    return None if turns is None else {" ".join(x.split()).lower() for x in turns}


def dupes(index, hv, ids) -> dict[str, str]:
    """{copy: keeper} among `ids`: the same day and app, chunk-0 cosine >= DUPE_COS, and every
    user turn of the copy also in the keeper (chunk 0 sees only the first 1,500 characters, so
    two chats that open alike and then diverge stay apart). The longest page keeps."""
    notes = index._notes
    info = {i: (topics.page_date(i, notes[i]["fields"], notes[i]["body"]),
                topics.page_source(notes[i]["body"]), _norm_turns(notes[i]["body"]))
            for i in ids}
    out: dict[str, str] = {}
    keepers: list[str] = []
    for i in sorted(ids, key=lambda i: (-len(notes[i]["body"]), i)):
        day, app, turns = info[i]
        k = next((k for k in keepers if day and turns is not None and info[k][:2] == (day, app)
                  and info[k][2] is not None and turns <= info[k][2] and i in hv and k in hv
                  and float(hv[i] @ hv[k]) >= DUPE_COS), None)
        if k is None:
            keepers.append(i)
        else:
            out[i] = k
    return out


def _line(turn: str) -> str:
    s = "Me: " + " ".join(chats.redact(turn).split())
    return s if len(s) <= chats.LINE_CHARS else s[:chats.LINE_CHARS - 1] + "…"


def _block(index, i: str, n: int, copies: list[str]) -> tuple[str, dict]:
    """One chat's text block and its row in the bundle's `chats`."""
    note = index._notes[i]
    day, app = topics.page_date(i, note["fields"], note["body"]), topics.page_source(note["body"])
    turns = user_turns(note["body"])
    head = f"[{n}] {day} {app} [[{i}]]" + (f" (×{len(copies) + 1} same day)" if copies else "")
    row = {"id": i, "date": day, "source": app, "turns": len(turns or ()), "dupes": copies}
    if turns is None:  # Y: refuse rather than guess where the user's turn ends
        row["title_only"] = True
        return f"{head} (turns unreadable: title only)\nTitle: {chats.redact(note['title'])}", row
    return "\n".join([head, *(_line(x) for x in turns if x.strip())]), row


def _know(index, h: str) -> str:
    """The tail of a hub's `## What I know` and `## What I explored`, wrapped; '' without
    either."""
    lines = index._notes[h]["body"].splitlines(keepends=True)
    secs = [s for name in (OLD_KNOW, KNOW) if (s := topics._find(lines, (name,)))]
    text = "\n".join("".join(lines[a:b]).strip() for a, b in secs).strip()
    return _wrap(f"know:{h}", text[-KNOW_CHARS:]) if text else ""


def bundle(index, t, stack, done, skipped, within: str | None = None,
           stamp: str = "") -> dict | None:
    """The next bundle, or None when nothing is left: whole units in queue order while their
    text fits chats.PART_CHARS and CHATS_MAX ids; a first unit too big alone is cut by date
    and the rest comes next time. Returns {bundle, hubs [{id, title, path, know, concepts}],
    chats [{id, hub, date, source, turns, dupes, title_only?}], text (wrapped), progress}.
    `bundle` hashes `stamp` (the serve time) with the ids, so a key never outlives a void."""
    hv = topics.heads(stack)
    units = queue(index, t, stack, done, skipped, within)
    if not units:
        return None
    hubs, rows, text, size, count = [], [], [], 0, 0
    for u in units:
        h = u["unit"]
        month = h.startswith("month:")
        head = f"## Unfiled chats, {h[6:] or 'undated'}" if month else f"## {h}: {_path(t, h)}"
        folded = dupes(index, hv, u["chats"])
        blocks = []
        for i in u["chats"]:
            if i not in folded:
                copies = sorted(c for c, k in folded.items() if k == i)
                b, r = _block(index, i, len(rows) + len(blocks) + 1, copies)
                blocks.append((b, {**r, "hub": None if month else h}))
        need = len(head) + 1 + sum(len(b) + 1 for b, _ in blocks)
        ids = sum(1 + len(r["dupes"]) for _, r in blocks)
        if size + need > chats.PART_CHARS or count + ids > CHATS_MAX:
            if rows:
                break
            blocks = _page(blocks, chats.PART_CHARS - len(head) - 1)
        text += [head, *(b for b, _ in blocks)]
        rows += [r for _, r in blocks]
        size += need
        count += ids
        if not month:
            hubs.append({"id": h, "title": plain(t.title[h], 80),
                         "path": plain(_path(t, h), 200),
                         "know": _know(index, h),
                         "concepts": [{"id": c, "title": plain(index._notes[c]["title"], 80),
                                       "state": GLYPHS[index._state(c)]}
                                      for c, _ in u["concepts"]]})
        if len(blocks) < len(u["chats"]) - len(folded):
            break  # a paged unit fills the bundle
    ids = [i for r in rows for i in (r["id"], *r["dupes"])]
    left = sum(len(u["chats"]) for u in units)
    total = sum(1 for n in index._notes.values() if is_chat(n))
    return {"bundle": hashlib.sha256(f"{stamp}|{' '.join(ids)}".encode()).hexdigest()[:16],
            "hubs": hubs, "chats": rows, "text": _wrap("harvest", "\n".join(text)),
            "progress": {"chats": total, "harvested": len(done & set(index._notes)),
                         "left": left}}


def _path(t, h: str) -> str:
    return " › ".join(t.title[x] for x in t.path(h) if x != topics.ROOT)


def _page(blocks: list, cap: int) -> list:
    """The blocks (newest first) that fit `cap` characters and CHATS_MAX ids; at least the
    first, cut to fit."""
    out, size, ids = [], 0, 0
    for b, r in blocks:
        n = 1 + len(r["dupes"])
        if size + len(b) + 1 > cap or ids + n > CHATS_MAX:
            if not out:
                out.append((b[:cap - 2] + "…", r))
            break
        out.append((b, r))
        size, ids = size + len(b) + 1, ids + n
    return out


def status(index, t, stack, events, today: str, since: str | None = None) -> dict:
    """The `cortex harvest status` numbers, read-only. Placement chats sit in a hub with a concept
    at CONCEPT_COS (None without `stack`, the vectors); a hub is bridged when it links a concept
    page (index edges); the yes-rate counts live me/ lines citing a [[chat-…]] against
    `## Not me` ones; `waiting` is the about-me backlog; the keep share is the share of pages
    that harvested events at least KEEP_DAYS old name and that still exist; `since` (a day)
    limits the event-based numbers (keep share, empty rate per app, runs)."""
    events = list(events)
    done, skipped, _ = fold(events)
    evs = [e for e in live(events) if e.get("kind") == "log"
           and isinstance(p := e.get("payload"), dict) and p.get("kind") == "harvested"
           and (since is None or str(e.get("day", "")) >= since)]
    ids = {i for i, n in index._notes.items() if is_chat(n)}
    concepts = {i for i, n in index._notes.items() if n["kind"] == "concept"}
    bridged = {s for s, d in index.db.execute("SELECT src, dst FROM edges")
               if s in t.parent and d in concepts}
    out: dict = {"chats": len(ids), "harvested": len(done & ids), "placement": None,
                 "hubs": len(t.parent), "bridged_hubs": len(bridged),
                 "waiting": len(unconfirmed(index.vault, today)[0])}
    if stack is not None:
        units = [u for u in queue(index, t, stack, set(), skipped) if u["concepts"]]
        place = {i for u in units for i in u["chats"]}
        out["placement"] = {"hubs": len(units), "chats": len(place),
                            "harvested": len(place & done),
                            "coverage": round(len(place & done) / len(place), 3) if place else 0.0,
                            "bridged": len(bridged & {u["unit"] for u in units})}
    cited = [r for r in claims.rows(index.vault) if "[[chat-" in r["text"]]
    yes, no = (sum(r["status"] == s for r in cited) for s in ("live", "not_me"))
    out["yes_rate"] = {"yes": yes, "no": no,
                       "rate": round(yes / (yes + no), 3) if yes + no else None}
    cut = (dt.date.fromisoformat(today) - dt.timedelta(days=KEEP_DAYS)).isoformat()
    old = [p for e in evs if str(e.get("day", "")) <= cut for p in e["payload"].get("pages") or ()]
    kept = sum(p in index._notes for p in old)
    out["keep"] = {"pages": len(old), "kept": kept,
                   "share": round(kept / len(old), 3) if old else None}
    apps: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    runs: dict[str, dict] = {}
    for e in evs:
        p, sid = e["payload"], str(e.get("session"))
        for c in p.get("chats") or ():
            n = index._notes.get(c)
            a = apps[topics.page_source(n["body"]) if n else "?"]
            a[0] += 1
            a[1] += p.get("outcome") == "empty"
        r = runs.setdefault(sid, {"session": sid, "day": e.get("day"), "bundles": 0, "chats": 0,
                                  "pages": [], "undo": f"cortex undo {sid}"})
        r["bundles"] += 1
        r["chats"] += len(p.get("chats") or ())
        r["pages"] += [x for x in p.get("pages") or () if x not in r["pages"]]
    out["empty"] = {a: {"chats": c, "empty": k, "rate": round(k / c, 3)}
                    for a, (c, k) in sorted(apps.items())}
    out["runs"] = list(runs.values())
    return out
