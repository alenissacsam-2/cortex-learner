"""`keepfold eval learning`: how well the logged `predicted_p` forecasts graded items (v6 item 7).

Read-only. Each graded primary item the fold sees (`levels._items`: voids and remaps applied)
is paired with the `predicted_p` the server stamped on its own event, the FSRS retrievability
just before the answer. Outcome = the fold's pass rule: correct at hints ≤ 1. Items with no
stamp (CLI and imported grades, problem_status, old self_reports) are counted, never guessed.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from keepfold import events as _events
from keepfold import levels
from keepfold.events import STUDY_MODES, norm_answer

BINS = 5
MIN_PAIRS = 30  # fewer pairs than this in a row: noise
EPS = 1e-6
SPLITS = ("mode", "grader", "source", "item_type", "confidence", "first", "prior")
SPOT_SHOWN = 3  # model-graded answers a reflect pack offers to keep or flip
# confidence mirror: 10 answers per band (each of the 4 bands is asked)
MIN_BAND = 10
CONFIDENCE_LABELS = {1: "1 (guessing)", 2: "2", 3: "3", 4: "4 (certain)"}


def _mode(mode: str | None) -> str:
    return "ambient" if mode == "ambient" else "study" if mode in STUDY_MODES else mode or "none"


def _grader(it: levels._Item, keys: dict[str, str]) -> str:
    """Who decided the verdict. The server (`Keepfold._graded`) makes a normalised answer_key
    match `correct`; on a mismatch the model's verdict stands. So for a tutor grade whose ask
    had an answer_key: `key-agrees` = the answer matched (correct) or did not and the model said
    wrong; `key-disagrees` = no match, yet the model said correct or partial (leniency or a
    paraphrase). `model` = a tutor grade with no key; other sources by name (external, self)."""
    ev = it.ev
    if ev.get("source") != "tutor":
        return ev.get("source") or "none"
    key, answer = keys.get(ev.get("ref")), ev.get("learner_answer")
    if not isinstance(key, str):
        return "model"
    match = isinstance(answer, str) and norm_answer(answer) == norm_answer(key)
    return "key-agrees" if match or it.verdict == "wrong" else "key-disagrees"


def _spot(e: dict) -> bool:
    return e.get("kind") == "log" and isinstance(e.get("payload"), dict) \
        and e["payload"].get("kind") == "spot_check"


def spot_checks(events: Iterable[dict], limit: int | None = SPOT_SHOWN,
                hide: set[str] = frozenset()) -> list[dict]:
    """v6 idea 10: the `limit` (None: all) newest live tutor grades on a concept whose verdict
    the model gave (no answer_key match) and no spot_check names yet, as {event_id, concept,
    question, answer, verdict}; concepts in `hide` (planner.hidden) are left out. Question and
    answer are untrusted text."""
    live = _events.live(list(events))
    asks = {e["id"]: e for e in live if e.get("kind") == "ask"}
    done = {e["payload"].get("event") for e in live if _spot(e)}
    rows = []
    for e in sorted(live, key=lambda e: (e["ts"], e["id"]), reverse=True):
        obj = str(e.get("object") or "")
        if (e.get("kind") != "grade" or e.get("source") != "tutor" or e["id"] in done
                or not obj.startswith("concept:") or obj.split(":", 1)[1] in hide):
            continue
        ask = asks.get(e.get("ref"), {})
        key, answer = ask.get("answer_key"), e.get("learner_answer")
        if isinstance(key, str) and isinstance(answer, str) \
                and norm_answer(answer) == norm_answer(key):
            continue  # the server's key match decided it, not the model
        rows.append({"event_id": e["id"], "concept": obj.split(":", 1)[1],
                     "question": str(e.get("question") or ask.get("question") or ""),
                     "answer": str(answer or ""), "verdict": e.get("verdict")})
        if len(rows) == limit:
            break
    return rows


def agreement(events: Iterable[dict]) -> dict:
    """How often the user's spot-check verdict matched the model's (`was`)."""
    rows = [e["payload"] for e in _events.live(list(events)) if _spot(e)]
    agree = sum(p.get("verdict") == p.get("was") for p in rows)
    return {"n": len(rows), "agree": agree,
            "rate": round(agree / len(rows), 4) if rows else None}


def _headline(pairs: list[tuple[float, int, float]]) -> dict:
    """pairs of (forecast, pass 0/1, partial-credit score)."""
    n = len(pairs)
    if not n:
        return {"n": 0}
    base = sum(y for _, y, _ in pairs) / n
    brier = sum((p - y) ** 2 for p, y, _ in pairs) / n
    ref = base * (1 - base)  # Brier of always forecasting the base rate
    loss = -sum(y * math.log(max(p, EPS)) + (1 - y) * math.log(max(1 - p, EPS))
                for p, y, _ in pairs) / n
    bins = reliability(pairs)
    return {"n": n, "base_rate": round(base, 4), "brier": round(brier, 4),
            "brier_base": round(ref, 4), "skill": round(1 - brier / ref, 4) if ref else None,
            "log_loss": round(loss, 4),
            "gap": round(sum(b["n"] * abs(b["p"] - b["pass"]) for b in bins if b["n"]) / n, 4),
            "brier_partial": round(sum((p - s) ** 2 for p, _, s in pairs) / n, 4),
            "few": n < MIN_PAIRS}


def reliability(pairs: list[tuple[float, int, float]]) -> list[dict]:
    """5 equal-width bins: mean forecast, observed pass rate, n."""
    rows = [{"lo": i / BINS, "hi": (i + 1) / BINS, "n": 0, "p": 0.0, "pass": 0.0}
            for i in range(BINS)]
    for p, y, _ in pairs:
        row = rows[min(int(p * BINS), BINS - 1)]
        row["n"], row["p"], row["pass"] = row["n"] + 1, row["p"] + p, row["pass"] + y
    for row in rows:
        if row["n"]:
            row["p"], row["pass"] = round(row["p"] / row["n"], 4), round(row["pass"] / row["n"], 4)
    return rows


def _stamped(p) -> bool:
    return not isinstance(p, bool) and isinstance(p, (int, float)) and 0 <= p <= 1


def cold_pairs(events: Iterable[dict], problem_concepts: dict | None = None,
               ) -> list[tuple[float, int, float]]:
    """The pairs that check the exam-day forecast (B5): stamped graded items on a concept
    already graded or credited, cold (the first on the concept that local day; a warm re-test
    after teaching passes almost surely, so it says nothing about recall across a gap)."""
    pairs, seen = [], set()
    for it in levels._items(events, problem_concepts):
        if it.verdict is None or it.concept is None or it.ev.get("kind") == "ask":
            continue
        p = it.ev.get("predicted_p")
        if it.concept in seen and not it.warm and _stamped(p):
            pairs.append((float(p), int(it.verdict == "correct" and it.hints <= 1),
                          levels._score(it)))
        seen.add(it.concept)
        if it.verdict == "correct":
            seen.update(it.also)
    return pairs


def calibrated(events: Iterable[dict], problem_concepts: dict | None = None,
               ) -> tuple[int, bool]:
    """The exam-day forecast gate: (cold pairs, open). Open needs MIN_PAIRS cold pairs whose
    forecasts beat always forecasting the base rate (Brier skill > 0)."""
    pairs = cold_pairs(events, problem_concepts)
    skill = _headline(pairs).get("skill")
    return len(pairs), len(pairs) >= MIN_PAIRS and skill is not None and skill > 0


def report(events: Iterable[dict], problem_concepts: dict | None = None,
           since: str | None = None) -> dict:
    """The calibration report over graded primary items on days ≥ `since`."""
    events = list(events)
    keys = {e["id"]: e.get("answer_key") for e in _events.live(events) if e.get("kind") == "ask"}
    splits: dict[str, dict[str, list]] = {s: {} for s in SPLITS}
    pairs, skipped, disagree = [], 0, {}
    run: dict[tuple, bool] = {}  # (concept, day) -> the cold block is still running
    seen: set[str] = set()  # concepts with a graded item (or `also` credit) so far
    for it in levels._items(events, problem_concepts):
        ev = it.ev
        if it.verdict is None or it.concept is None or ev.get("kind") == "ask":
            continue
        # cold as the fold rates a day (`levels._cold_block`): the first graded item with
        # nothing on the concept earlier that day, and every one after it until a teaching
        k = (it.concept, ev.get("day"))
        cold = run[k] = run.get(k, not it.warm) and not it.primed
        prior = "history" if it.concept in seen else "first review"  # first: the 0.5 default
        seen.add(it.concept)
        if it.verdict == "correct":
            seen.update(it.also)
        if since and (ev.get("day") or "") < since:
            continue
        if mv := ev.get("model_verdict"):
            pair_name = f"{mv} -> {it.verdict}"
            disagree[pair_name] = disagree.get(pair_name, 0) + 1
        p = ev.get("predicted_p")
        if not _stamped(p):
            skipped += 1
            continue
        pair = (float(p), int(it.verdict == "correct" and it.hints <= 1), levels._score(it))
        pairs.append(pair)
        for split, value in (("mode", _mode(ev.get("mode"))), ("grader", _grader(it, keys)),
                             ("source", ev.get("source") or "none"),
                             ("item_type", it.item_type or "recall"),
                             ("confidence", "given" if it.confidence is not None else "none"),
                             ("first", "cold" if cold else "later same day"),
                             ("prior", prior)):
            splits[split].setdefault(value, []).append(pair)
    return {**_headline(pairs), "skipped_no_p": skipped, "since": since,
            "bins": reliability(pairs),
            "splits": {s: {k: _headline(v) for k, v in sorted(rows.items())}
                       for s, rows in splits.items()},
            "model_verdict": {"n": sum(disagree.values()), "by": dict(sorted(disagree.items()))},
            "spot_check": agreement(events)}


def confidence_mirror(events: Iterable[dict], problem_concepts: dict | None = None,
                      hide: set[str] = frozenset()) -> list[dict]:
    """Council item 6, display-only (never scheduling): per confidence 1-4 the graded primary
    answers that carried it, as [{confidence, label, n, right, share}]; right = the fold's pass
    rule (correct at hints <= 1; partial is not right). `share` is None below MIN_BAND answers.
    Concepts in `hide` (planner.hidden) are left out."""
    rows = {c: [0, 0] for c in CONFIDENCE_LABELS}
    for it in levels._items(events, problem_concepts):
        if (it.verdict is None or it.ev.get("kind") == "ask" or it.concept in hide
                or isinstance(it.confidence, bool) or it.confidence not in rows):
            continue
        rows[it.confidence][0] += 1
        rows[it.confidence][1] += it.verdict == "correct" and it.hints <= 1
    return [{"confidence": c, "label": CONFIDENCE_LABELS[c], "n": n, "right": right,
             "share": round(right / n, 4) if n >= MIN_BAND else None}
            for c, (n, right) in rows.items()]


def mirror_lines(rows: list[dict]) -> list[str]:
    """The mirror as plain lines; [] when no answer carried a confidence."""
    if not any(r["n"] for r in rows):
        return []
    return [f"When you said {r['label']}, you were right {r['right']} of {r['n']} times"
            if r["share"] is not None else
            f"When you said {r['label']}: not enough answers yet ({r['n']})" for r in rows]


def _row(name: str, h: dict) -> str:
    if not h.get("n"):
        return f"{name:<14} {0:>5}"
    skill = "-" if h["skill"] is None else f"{h['skill']:+.3f}"
    return (f"{name:<14} {h['n']:>5} {h['base_rate']:>6.3f} {h['brier']:>6.3f} "
            f"{h['brier_base']:>6.3f} {skill:>7} {h['log_loss']:>7.3f} {h['gap']:>6.3f} "
            f"{h['brier_partial']:>7.3f}" + ("  (too few)" if h["few"] else ""))


def text(r: dict) -> str:
    """The report as a compact plain-text table."""
    out = [f"learning calibration{' since ' + r['since'] if r['since'] else ''}: "
           f"{r['n']} graded item(s) paired with predicted_p, "
           f"{r['skipped_no_p']} skipped (no predicted_p)",
           "pass = correct at hints <= 1; skill = 1 - Brier/base Brier; gap = binned |forecast"
           " - pass|; Bpart = Brier with partial = 0.5",
           f"{'':<14} {'n':>5} {'pass':>6} {'Brier':>6} {'base':>6} {'skill':>7} "
           f"{'logloss':>7} {'gap':>6} {'Bpart':>7}",
           _row("all", r)]
    if (sc := r.get("spot_check") or {}).get("n"):
        out.append(f"grader spot-check: user agreed with {sc['agree']} of {sc['n']} "
                   f"model verdict(s) ({sc['rate']:.0%})")
    if not r["n"]:
        return "\n".join(out)
    out.append(_row("cold only", r["splits"]["first"].get("cold", {"n": 0})))
    out += ["", "reliability   forecast   pass      n"]
    out += [f"{b['lo']:.1f}-{b['hi']:.1f}       {b['p']:>6.3f} {b['pass']:>6.3f} {b['n']:>6}"
            for b in r["bins"] if b["n"]]
    for split, rows in r["splits"].items():
        out += ["", f"by {split}"] + [_row(k, h) for k, h in rows.items()]
    mv = r["model_verdict"]
    out += ["", f"model_verdict disagreements (model -> final): {mv['n']}"
            + (" (" + ", ".join(f"{k}: {n}" for k, n in mv["by"].items()) + ")"
               if mv["n"] else "")]
    return "\n".join(out)
