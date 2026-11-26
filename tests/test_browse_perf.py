"""The browse import on a heavy synthetic profile (~60k visits, docs/design/v8/PLAN.md task 6):
seconds per step and calls to a fake local model that answers in ~50 ms. Synthetic History
only; the model is `localllm.Runner` with its HTTP post replaced, so nothing leaves the test.
The first import is the app's job (titles in batches, no summaries); then a run with nothing
new and one with 50 new visits, as the live import every 10 minutes. Run with `-s` to see
the table."""

import json
import math
import re
import sqlite3
import time
from collections import Counter
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from test_browse import hubbed

from keepfold import localllm
from keepfold.browse import Settings, build, forget, sites, sources, store, titles
from keepfold.vault import Vault

LINK = 0x30000000
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
WORDS = ("chess", "graphs", "cooking", "guitar", "python", "history", "physics", "news")


def chrome_raw(t: datetime) -> int:
    return int((t - sources.EPOCH).total_seconds() * 1e6) + sources.CHROME_OFFSET_US


def heavy_history(path, n: int = 60_000, n_sites: int = 2_500, days: int = 90,
                  start_id: int = 1, end: datetime = NOW):
    """A Chromium History of `n` visits over `days` days: ~20k distinct titles, a Google search
    every 15th visit (3k distinct terms), every 100th visit an adult site."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE IF NOT EXISTS urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
        "CREATE TABLE IF NOT EXISTS visits(id INTEGER PRIMARY KEY, url INTEGER, "
        "visit_time INTEGER, visit_duration INTEGER, transition INTEGER);"
        "CREATE TABLE IF NOT EXISTS keyword_search_terms(keyword_id INTEGER, url_id INTEGER, "
        "term TEXT, normalized_term TEXT);"
        "CREATE INDEX IF NOT EXISTS kst_url ON keyword_search_terms(url_id);")  # as Chrome has
    t0 = end - timedelta(days=days)
    step = days * 86400 / n
    urls, visits, terms = [], [], []
    for i in range(n):
        vid = start_id + i
        t = t0 + timedelta(seconds=i * step)
        w = WORDS[i % len(WORDS)]
        if i % 100 == 0:
            url, title = f"https://www.pornhub.com/view_video.php?viewkey={i}", f"clip {i}"
        elif i % 15 == 0:
            term = f"how to {w} {i % 3000}"
            url, title = (f"https://www.google.com/search?q={term.replace(' ', '+')}",
                          f"{term} - Google Search")
            terms.append((1, vid, term, term))
        else:
            s, p = (i * 7) % n_sites, i % 8
            url, title = f"https://www.site{s}.com/a/{p}", f"{w} article {p} on site {s}"
        urls.append((vid, url, title))
        visits.append((vid, vid, chrome_raw(t), 0, LINK))
    con.executemany("INSERT INTO urls VALUES (?, ?, ?)", urls)
    con.executemany("INSERT INTO visits VALUES (?, ?, ?, ?, ?)", visits)
    con.executemany("INSERT INTO keyword_search_terms VALUES (?, ?, ?, ?)", terms)
    con.commit()
    con.close()
    return path


class FakeModel:
    """`localllm.Runner._post` replaced: ~`delay` s per call, a label (or a numbered list of
    labels) for a classify prompt, a sentence otherwise. Counts calls by kind."""

    def __init__(self, delay: float = 0.05):
        self.delay, self.calls = delay, Counter()

    def post(self, url, body, timeout):
        time.sleep(self.delay)
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        if "numbered" in system:
            self.calls["classify_batch"] += 1
            n = len(re.findall(r"^\d+\. ", user, re.M))
            text = json.dumps({str(k + 1): "ordinary" for k in range(n)})
        elif "Allowed labels" in system:
            self.calls["classify"] += 1
            text = json.dumps({"label": "ordinary", "confidence": 0.9})
        elif "describe one website" in system:
            self.calls["site"] += 1
            text = "{}"
        else:
            self.calls["summarise"] += 1
            text = "Reading about things."
        return {"message": {"role": "assistant", "content": text}}


class Embed:
    """A fake embedder: 8 axes by word; counts the texts it was given and sleeps `per_text`
    s for each (the real model embeds roughly a thousand short titles a second on a CPU)."""

    def __init__(self, per_text: float = 0.001):
        self.texts, self.per_text = 0, per_text

    def __call__(self, texts):
        self.texts += len(texts)
        time.sleep(self.per_text * len(texts))
        out = np.zeros((len(texts), len(WORDS)), dtype=np.float32)
        for k, t in enumerate(texts):
            for n, w in enumerate(WORDS):
                if w in t.lower():
                    out[k, n] = 1
        out[:, 0] += 1e-3
        return out


STEPS = [(sources.Since, "__enter__"), (store, "ingest"), (store, "items"), (build, "_ask"),
         (titles, "check"), (build, "hubs"),
         (build, "place"), (build, "write_day"), (build, "write_index"), (build, "signals"),
         (sites, "record"), (sites, "write"), (forget, "purge")]


def profile(monkeypatch, run):
    """Time `run()` per step (the module functions in STEPS); {step: seconds, "total": s}."""
    took: Counter = Counter()
    for mod, name in STEPS:
        fn = getattr(mod, name)

        def timed(*a, _fn=fn, _name=name, **k):
            t = time.perf_counter()
            try:
                return _fn(*a, **k)
            finally:
                took[_name] += time.perf_counter() - t
        monkeypatch.setattr(mod, name, timed)
    t = time.perf_counter()
    report = run()
    took["total"] = time.perf_counter() - t
    return report, took


def table(title, took, model, embed):
    lines = [f"{title}: {took['total']:.1f} s; model calls {dict(model.calls)}; "
             f"texts embedded {embed.texts}"]
    lines += [f"  {k:12s} {v:7.2f} s" for k, v in took.items() if k != "total"]
    print("\n".join(lines))


@pytest.mark.slow
def test_heavy_profile(root, tmp_path, monkeypatch):
    hubbed(root)
    local = tmp_path / "local"
    hist = heavy_history(local / "Google" / "Chrome" / "User Data" / "Default" / "History")
    model = FakeModel()
    monkeypatch.setattr(localllm.Runner, "_post", lambda r, url, body, timeout:
                        model.post(url, body, timeout))
    runner = localllm.Runner("ollama", "http://127.0.0.1:1", "qwen3.5:2b")
    settings = Settings(visible_to_claude=True, hidden_from_claude=("adult",))
    v = Vault(root)
    srcs = sources.discover(local, tmp_path / "roaming")
    embed = Embed()
    store.migrate(v, settings=settings)  # as the app's job: no site page yet, the store holds them
    rep, took = profile(monkeypatch, lambda: build.run(
        v, srcs, settings, embed=embed, runner=runner, budget=localllm.Budget(300), tz=UTC,
        now=NOW, summaries=False))
    table("first import", took, model, embed)
    assert rep["sources"] == 1
    # every title the model saw went in batches of titles.BATCH, none one at a time
    assert model.calls["classify"] == 0 and model.calls["summarise"] == 0
    batches = math.ceil(rep["vet_asked"] / titles.BATCH)
    assert rep["vet_calls"] == model.calls["classify_batch"] == batches
    calls1 = sum(model.calls.values())
    rep, took = profile(monkeypatch, lambda: build.run(
        v, srcs, settings, embed=embed, runner=runner, budget=localllm.Budget(300), tz=UTC,
        now=NOW, summaries=False))
    table("nothing new", took, model, embed)
    assert sum(model.calls.values()) == calls1 and not rep.get("visits_new")
    assert took["total"] < 2.0, took  # the History file is not even copied
    heavy_history(hist, n=50, start_id=100_000, days=9 / 1440, end=NOW + timedelta(minutes=10))
    rep, took = profile(monkeypatch, lambda: build.run(
        v, srcs, settings, embed=embed, runner=runner, budget=localllm.Budget(300), tz=UTC,
        now=NOW + timedelta(minutes=10), summaries=False))
    table("50 new visits", took, model, embed)
    assert rep["visits_new"] == 50
    # only the changed day's titles go to the model, and only its pages are made again
    assert model.calls["classify_batch"] - calls1 <= math.ceil(50 / titles.BATCH)
    assert rep["public_written"] == rep["private_written"] == 1  # the one day, not all 90
