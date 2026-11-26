# Memory of a long-lived Keepfold process

`uv run python scripts/memory_probe.py --notes 1000 --visits 10000`: one process takes the steps
a long-lived MCP server (`keepfold mcp`) and the app take, on a synthetic vault in a temp folder
with a temp HOME (1,000 made-up pages, one in five long, 2,704 chunks; a browse import of 10,000
made-up visits, 369 browse pages, 1,104 chunks). The models are the real ones from
`~/.keepfold/models` (bge-small-en-v1.5 and ms-marco-MiniLM-L-6-v2); nothing else of `~/.keepfold`
is read and nothing is downloaded.

Machine: 8 cores (16 logical), 23.7 GB RAM, Windows 11. Other work kept the CPU near 100%
during every run, so the seconds are noisy; the memory numbers are not.

`ws` is the working set, `private` the private bytes (commit), both in MB after the step.

| step | before ws | before private | after ws | after private |
|---|---:|---:|---:|---:|
| python started | 21 | 13 | 21 | 12 |
| import keepfold (mcp_server, semantic, home) | 55 | 522 | 54 | 522 |
| MCP core open (catalog + index) | 63 | 530 | 63 | 529 |
| embedder loaded | 234 | 686 | 233 | 686 |
| semantic index built (2,704 chunks) | 1,867 | 2,839 | 256 | 708 |
| reranker loaded | 1,964 | 2,936 | 351 | 803 |
| first recall | 2,129 | 3,236 | 355 | 839 |
| 12 more recalls | 2,300 | 3,502 | 356 | 841 |
| background copy: private browse vectors (1,104 chunks) | 2,799 | 4,160 | 480 | 964 |
| private browse search | 2,800 | 4,162 | 481 | 966 |
| app home (build_data, feed, caches), public and private | 2,834 | 4,182 | 512 | 984 |
| idle, models in memory | 2,834 | 4,182 | 512 | 984 |
| idle 10 minutes: models dropped | (kept) | (kept) | **186** | **657** |
| C heap trimmed (`_heapmin`) | 2,831 | 4,179 | 186 | 657 |
| first search after idle (both models loaded again) | 2,832 | 4,179 | 400 | 873 |

The peak working set during the index build: 1,875 MB before, and it stayed; 1,140 MB after,
for the length of a batch, then back to 256 MB. A second "after" run, with another test suite
keeping the CPU at about 75%, matched within 3 MB at every step (idle 511 MB, 185 MB once the
models were dropped).

## Where it went

**onnxruntime's CPU memory arena: about 2.3 GB of the 2.8 GB.** Each model session keeps its
own arena, and the arena keeps the largest batch it ever ran for the life of the session (it
grows in power-of-two steps, so it holds more than one batch needs). The index build (batches
of 64 long heads, as `_vectors` without a budget, `topics.head_stack` and `embed_all` run them;
attention grows with the square of the tokens) left the embedder's arena at about 1,610 MB, the
reranker's 30 passages per recall about 340 MB, and the background copy's private browse
vectors (16 chunks of 1,000 characters) about 380 MB beside the copy's own 120 MB. Measured
alone (fastembed, one model):
after one batch of 64 texts of ~1,000 characters the process stays at 618 MB working set with
the arena, 217 MB without it.

The models themselves are small: bge-small adds 171 MB, the reranker 95 MB, the background
copy about 120 MB.

## The fixes (src/keepfold/semantic.py)

1. **No CPU arena** (`ARENA = False`, passed as fastembed's `enable_cpu_mem_arena` to the
   embedder, the background copy and the reranker). A run frees its buffers at its end. Idle
   with every model loaded: 2,834 MB → 512 MB working set, 4,182 MB → 984 MB private.
2. **Idle unload** (`IDLE_UNLOAD = 600` s): a thread checks every minute and drops each model
   unused for 10 minutes (the embedder, the background copy, the reranker); its next call loads
   it again. A call in progress holds its model, so it is never dropped mid-call. This bought
   another 326 MB: 512 → 186 MB working set, 984 → 657 MB private. Dropping frees the memory
   at once; `gc.collect()` and the C runtime's `_heapmin` freed nothing more, so neither is
   called.

Reload, measured after the drop: the embedder 0.4 s (its first load is 4 s, most of it the
fastembed import), the reranker under 0.3 s; the first recall after idle took 2.0 s against
1.7-1.9 s for a warm one on the busy machine.

## Speed without the arena

Interleaved in one process, both sessions alive, so the machine's load hits both alike
(medians):

| | arena on | arena off |
|---|---:|---:|
| query embedding (one short query) | 11.3 ms | 9.7 ms |
| rerank of 30 passages (a recall's rerank) | 825 ms | 817 ms |
| bulk embedding, 64 per batch, all cores | 18.2/s | 15.6/s |
| bulk embedding, 16 per batch, all cores | 17.9/s | 13.8/s |
| bulk embedding, 8 per batch, all cores | 20.5/s | 12.0/s |
| bulk embedding, 64 per batch, 8 threads (the background copy) | 16.5/s | 15.3/s |
| bulk embedding, 16 per batch, 8 threads | 17.4/s | 13.8/s |

A search is no slower (the query embedding and the rerank are within noise). Bulk embedding is
7-40% slower without the arena, as every run allocates its buffers afresh: the probe's full
index build took 262 s against 209 s before. Builds are background work at low priority, or
`keepfold model build` once; a short-lived build process could turn the arena back on (set
`semantic.ARENA = True` before the model loads), since its arena dies with it.

## M2: the idle app, hidden or minimised

`uv run python scripts/memory_probe.py --app 60 [--hidden] --notes 300 --visits 3000` runs the
desktop app on the made-up vault's Home and measures its six WebView2 processes for 60 s per
window state. `--launch 13 [--hidden]` starts the app as the installer does (`launch.main`) on
that vault, indexed with the real models, and prints the Python process's memory and the loaded
search models each minute. Both use a temp HOME, a free port and a fake tray; neither writes the
Run key nor runs LM Studio's CLI.

**The cause of the CPU in the tray.** Closing the window hid it with WinForms' `Form.Hide`,
which leaves WebView2's `IsVisible` true. The page stayed `visible`, so Home's 3D brain (its
animation frames) and its 3 s poll ran on in the tray at 21-24% of a core: about 11% in the GPU
process and 16% in the renderer. A minimised window was already reported hidden.

**The fixes.**
1. `desktop.py` hides the WebView2 control first, while the form still shows, and then the
   form (`_hide`). WebView2 then sets `IsVisible` false and the page gets `visibilitychange`
   (hidden). Showing shows the form first, then the control (`_show`).
2. A minute later (`SLEEP_S`), still hidden, the page sleeps: `CoreWebView2.TrySuspendAsync()`
   (`_sleep`). A window started hidden at login sleeps a minute after start. WebView2 pauses
   the page's timers and lets Windows trim its processes, and wakes the page when the control
   shows again. `MemoryUsageTargetLevel` is not set: suspending sets it to Low already, and
   Microsoft's docs advise against mixing the two. On pywebview's other backends,
   `win.hide()` and `win.show()` run as before.
3. In `app.js`, the job poll waits while the page is hidden and the 15 s check skips. When the
   page shows again, it refreshes once. Home (`dashboard.js`, `brain3d.js`) already paused its
   poll and the brain while hidden; it never was hidden in the tray until now.

WebView2's six processes, 60 s per state (57 s before):

| window | before CPU | before ws MB | page | after CPU | after ws MB | page |
|---|---:|---:|---|---:|---:|---|
| shown (Home) | 26% | 465 | visible | 30-41% | 467 | visible |
| hidden to the tray | 21-24% | 434 | visible | 0.3% | 413 | hidden |
| still in the tray a minute later (asleep) | 21-24% | 434 | visible | 0.1% | 25 | hidden |
| shown again (time to show) | 25% (0.06 s) | 440 | visible | 30% (0.03 s) | 195 | visible |
| minimised | 0.2-0.9% | 426 | hidden | 0.2% | 204 | hidden |
| started hidden (at login), after a minute | 1.0% | 385 | hidden | 1.3% | 23 | hidden |

The CPU while shown is the brain drawing, which is unchanged. It moved between 24% and 41% from
run to run with the machine's load. The WebView2 processes' private bytes stay about 216 MB
while asleep: suspending trims their working set, not their commit.

The Python process of an idle app, measured with `--launch 13`, ran with no job, the window
hidden or shown, for 13 minutes. No search model was loaded at any minute. Its working set was
164-172 MB. Its private bytes were 633 MB with 16 OpenBLAS threads (as 0.4.2 runs) and 250 MB
with the cap below.

**OpenBLAS.** `taint.cap_blas()` sets `OPENBLAS_NUM_THREADS=4` unless the user set it or
`OMP_NUM_THREADS`, which OpenBLAS also reads. It runs before numpy loads, first thing in each
entry point:
- `keepfold` (`taint.main`): the MCP server and every CLI command;
- the app (`launch.main`);
- the receiver (`launch.receiver`).

The processes they start inherit it. The private bytes numpy commits at import, by thread count
(the working set rises by about 12 MB in every case):

| OpenBLAS threads | 16 (the default here) | 8 | 4 | 2 | 1 |
|---|---:|---:|---:|---:|---:|
| private MB | 492 | 235 | 107 | 42 | 10 |

The probe above, run on 300 made-up notes (811 chunks), once with 16 threads and once with
the cap. The models run on onnxruntime's own threads, not OpenBLAS, so the speeds stay within
noise:

| | 16 threads | 4 threads (the cap) |
|---|---:|---:|
| private bytes after `import keepfold` | 523 MB | 137 MB |
| private bytes, idle with every model loaded | 965 MB | 579 MB |
| private bytes, idle with the models dropped | 639 MB | 255 MB |
| working set, idle with the models dropped | 164 MB | 158 MB |
| recall (12 of them, median) | 1,862 ms | 1,652 ms |
| query embedding (median) | 12.9 ms | 13.3 ms |
| index build (811 chunks) | 118 s | 104 s |

**What M1 misses on a busy PC (not changed here).** The owner's installed 0.4.2 app had a
726 MB working set and 1,184 MB of private bytes 15 minutes after it started. The idle probe
shows that the app loads no model when nothing happens. On a PC in use, three things keep the
memory high:
- **OpenBLAS** accounts for about 490 MB of the private bytes. The cap above fixes it.
- **The live browse import** runs every 10 minutes, the first time a minute after start. It
  calls `semantic.load_embedder()` for its embed function, and that call loads the main
  embedder (+169 MB working set) even though the import's embedding goes to the background
  copy. The copy adds 115 MB when there are new visits. Imports run every 10 minutes and
  `IDLE_UNLOAD` is also 10 minutes, so the copy is rarely dropped.
- **The safety net** runs every 5 minutes while new chat pages wait. It uses the main embedder
  whenever that is loaded (`safety_net.background_tools`), and each use restarts the idle
  clock. While the owner chats with AI apps, the embedder is never dropped.

Measured in one process: 47 MB working set, then 216 MB after `load_embedder()`, 331 MB after
background embedding, and 108 MB after the drop.

Possible fixes, all outside M2's files (`semantic.py`, `browse_serve.py`, `safety_net.py`):
- `load_embedder()` returns the function without loading the model, since `_Held` loads it on
  first use anyway;
- background uses inside `semantic.background()`, which the net would also use, no longer
  restart the idle clock;
- or a shorter `IDLE_UNLOAD` in the app.

## M3: an idle Keepfold gives its memory back

The owner's installed 0.4.2, 15 minutes after start on a PC in use: the app's Python process
had a 726 MB working set and 1,184 MB of private bytes, and each idle `keepfold mcp` had 295-323
MB. M3 closes the two gaps M2 found in the app and makes an idle MCP connection small.

Two probe runs, both on made-up data with a temp HOME and the real models:
- `--launch 25 --busy --scale 0.2 --notes 300` starts the app as the installer does. It makes
  one person's search at the start. Every minute it adds 40 visits to a made-up Chrome
  history, and every fifth minute a chat page. The browse import, the chats import, the
  safety net and the idle check run on their schedules. `--scale 0.2` shortens every schedule
  and `IDLE_UNLOAD` five times, so 25 minutes take 5. Times are shown at full scale.
- `--mcp 15 --scale 0.2 --notes 300` starts `keepfold mcp` over stdio as an AI app does:
  `initialize`, then one `keepfold_prime` and one `keepfold_recall` (first calls, timed), then 15
  idle minutes, then the same two calls again.

Both print the working set and private bytes each minute, and which models are loaded and for
how long they have gone unused.

"Before" is main at 19191f1. "After" is this change. The runs used `--notes 300 --visits 3000`
and a hidden window, on 16 logical cores, with no test suite running. Times are full-scale
minutes. "Embedder" is the main model and "copy" is the background copy.

**The app on a PC in use** (`--launch 25 --busy`), working set and private bytes in MB:

| min | before ws | priv | models | after ws | priv | models |
|---:|---:|---:|---|---:|---:|---|
| 1 | 338 | 407 | embedder | 339 | 408 | embedder |
| 2 | 466 | 535 | embedder, copy | 467 | 535 | embedder, copy |
| 4 | 468 | 537 | embedder, copy | 500 | 587 | embedder, copy (vectors) |
| 7 | 868 | 975 | embedder, copy | 654 | 786 | embedder, copy (vectors) |
| 10 | 474 | 544 | embedder, copy | 365 | 435 | embedder |
| 12 | 473 | 544 | embedder, copy | 47 | 326 | none |
| 14 | 476 | 547 | embedder, copy | 49 | 327 | none |
| 16 | 478 | 549 | embedder, copy | 51 | 327 | none |
| 20 | 481 | 551 | embedder, copy | 100 | 318 | none (an import ran) |
| 25 | 473 | 545 | embedder, copy | 109 | 323 | none |

**Before:** the safety net's uses every 5 minutes kept the embedder loaded for the whole run.
The copy was dropped only once, at minute 13, and the next import loaded it again.

**After:** the embedder goes 10 minutes after the person's search (minute 12), although the net
used it at minutes 2 and 7. From then on the net runs without an embedder and picks no hub by
vectors, as M1 intended after a drop. The copy goes when each import ends.

Each run also depends on whether the PC was idle. In the after run it was idle from minute 4, so
the import's private-vectors pass ran for its budget and held the copy until minute 9. In the
before run it never ran.

The after run came before review's scorer stopped restarting the clock (see below). In that
run, its first pass, at minute 1, used the embedder too, which moved the drop about a minute
later.

A first after run, which trimmed only on an idle drop, stayed at 132-136 MB with no model
after its minute-14 import. The end of a background run now trims too.

**One idle `keepfold mcp`** (`--mcp 15`), in MB:

| min | before ws | priv | after ws | priv | models |
|---:|---:|---:|---:|---:|---|
| 1-11 | 360 | 425 | 360 | 425 | embedder, reranker |
| 12-14 | 153 | 217 | 2-3 | 224 | none (dropped at 10 idle minutes) |
| 16, after a prime and a recall | 269 | 335 | 159 | 335 | embedder |

The idle check drops both models between 10 and 11 idle minutes. Before, the working set stayed
at 153 MB after that. Now the trim empties it, and the next calls fault back what they touch.
Private bytes are the same, since they still hold the imports.

**First calls over stdio** (seconds), two runs each:

| call | before | after |
|---|---|---|
| initialize | 6.40, 5.75 | 5.88, 5.70 |
| `keepfold_prime`, first (waits for the warm-up's load) | 4.31, 4.23 | 4.38, 4.22 |
| `keepfold_recall`, first | 0.11, 0.13 | 0.10, 0.09 |
| `keepfold_prime`, after the idle drop (loads again) | 0.52 | 0.52 |
| `keepfold_recall`, after the idle drop | 0.11 | 0.10 |

The first prime is 0.7% slower on the mean, which is within noise. The trim costs no visible
time after idle, because that call loads the model again anyway.

**The rule: only a person's use keeps a model.** Background work still uses the models, but it
never keeps one loaded:
- **The browse import loads nothing to get its embed function.** Inside
  `semantic.background()`, `load_embedder()` returns the function when the download is whole,
  by the same rule as `doctor.search_model_ready` (the `.complete` marker, else no
  `*.incomplete` file). It never loads a model. The first embed loads the capped copy, which
  is the model background work uses anyway. When the outermost `background()` ends, the copy
  is dropped, since it served only that work. `_embedder`, which prime and review read as
  "this process has the model", is set only by a load outside `background()`. A background
  import therefore never makes them load the model.
- **A background use never restarts the idle clock.** `_Held.use(touch=False)` leaves the
  clock where a person's last use set it. A model no person used goes at the next idle check,
  even while background work runs every few minutes.
- **The safety net and review's scorer never load the main embedder by themselves.**
  `safety_net.background_tools()` and `review._warm` get `semantic.loaded_embedder()`, which
  returns an embed function only while the main model is in memory. That function does not restart the clock.
  If the model is dropped between that check and the embed, the call loads it again, and the
  next idle check drops it.
- **The failure path is the same as before.** Before M3, a model whose files did not load
  (corrupt or half downloaded) made `load_embedder()` return None, and the import ran without
  it. Now a half download is still None, by the readiness rule. A whole download that does not
  load fails at the first embed with `semantic.NotLoaded`, a `VaultError` with the `not_set_up`
  message. `browse_serve.run_import` then runs `build.run` again without a model. Its days
  stay dirty until their pages are saved, so the second run makes them, with no topics and no
  vectors. The private vectors' pass already reports any failure and goes on.
  `test_a_broken_model_leaves_the_background_import_as_without_one` covers both cases.

**What an idle `keepfold mcp` holds.**
- **The imports.** They take about 127 MB of working set before any model loads, and they stay
  for the life of the process:
  - the MCP SDK brings starlette, uvicorn, sse_starlette and cryptography;
  - fastembed brings onnxruntime, tokenizers, PIL, huggingface_hub, requests, tqdm, mmh3 and
    loguru;
  - numpy comes in through `keepfold.cli`, by way of dashboard and brainmap.
- **The embedder.** The warm-up after `notifications/initialized` loads it, so the first prime
  is quick and orders by cosine. It stays: dropping it would slow the first call.
- **The core's catalog and index, and the vector matrices kept for queries**
  (`semantic._MEM`). Dropping `_MEM` would change what `keepfold_read` lists as similar pages.

The M1 idle check drops the embedder after `IDLE_UNLOAD`. On Windows a process keeps the pages
it touched in its working set after the memory is freed. So when the last model goes, the
process now empties its working set. That happens at an idle drop (`semantic.idle_pass`) or
at the end of a background run that leaves no model (`background()`). Both call
`_trim_if_none`, then `_trim`, which calls `K32EmptyWorkingSet`. `TRIM = False` turns it off. The pages go to the standby list, where
other programs can take them, and the next call faults back only the pages it touches. Private
bytes do not change, and no tool's output or server-stamped field changes.

**Review's scorer follows the same rule.** `review._warm` serves the app's about-me tidy and
Review. It used to take `_embedder` as meaning the model was loaded. So once a person's use had
set it, the scorer loaded the embedder again after an idle drop and restarted its clock. Now it
also uses `semantic.loaded_embedder()`. With no model in memory, it scores by words (Jaccard),
as it already did when no model was set up.

## What is left (not changed here)

- **The index build's transient peak**, about 900 MB above the loaded model at a batch of 64
  long heads, lasts while the batch runs. Smaller batches for long texts would lower it, at
  some cost in speed without the arena (the table above).
- **The app's WebView2 window** is separate processes, not Python. Since M2 its page sleeps a
  minute after the window goes to the tray, which leaves a 25 MB working set. Its private bytes
  stay about 216 MB. Only closing the web view while the window is hidden would free them, and
  the next show would then load the page again.
- **What stays after the drop** (186 MB working set): Python and the imported modules, the
  loaded onnxruntime and tokenizer libraries, the MCP core's catalog and index, the vector
  matrices kept for queries (`semantic._MEM`, `browse.vectors._MEM`: 4 bytes × 384 per chunk)
  and the app home's caches. tracemalloc (`--trace`) counted 74 MB of Python allocations at
  idle: 22 MB by the imports, 20 MB on loading the embedder (fastembed, tokenizers), 5 MB by
  the index build, 21 MB by the app home; nothing big was left from a build to release.
  (In that run the steps were slow enough that the idle thread itself dropped the embedder and
  the reranker while the background copy worked, 82 MB, and the next search loaded them again.)

## F1: the per-call refresh of find, read and recall

On test_home's made-up 5k-note vault (quiet PC, median of 15), a warm `keepfold_find` took 576 ms
before (the refresh 558 ms: `current_states` 248 ms, the index's stat pass 256 ms) and 90 ms after
(the refresh 72 ms: `current_states` 9 ms from the cached fold, the scan 53 ms from `os.scandir`'s
listing); `tests/test_replay_cache.py` holds it under 0.3 s.

## R30d: the parse caches (`events._PARSED`, `vault._PARSED`)

Both caches keep every file they parsed for the life of the process; an outside review proposed
an LRU of 512 entries. Measured first (with F1 in), on test_home's made-up 5k-note vault plus
2,000 made-up session files (6 to 18 events each, grades with a 40-word answer and 30-word
feedback, `taught` logs, a 50-word close summary): 2,001 event files, 27,138 events, 16.6 MB of
JSONL; 5,000 pages, 6.3 MB of Markdown. Every event file read through `EventLog.iter_events`
and every page parsed through `Vault.iter_notes` (more than a server ever parses: the index
starts from its snapshot), then the results dropped:

| cache | entries | Python allocations held (tracemalloc) |
|---|---:|---:|
| `events._PARSED` | 2,001 files | 65.3 MB |
| `vault._PARSED` | 5,000 pages | 32.8 MB |
| both | | **98 MB** |

The process's working set rose 79 → 171 MB and its private bytes 545 → 636 MB with both full
(tracemalloc off). A warm `keepfold_find` on that vault took 245 ms (median of 7) with another
test suite keeping the CPU busy; F1's budget is 0.3 s.

**Decision: no bound.** Both together stay under the 150 MB line at this size, and a bound
below the working set would make each fold parse the log again: `levels.replay` and the
planner read every event file on each call, so an LRU of 512 files would re-read 1,500 files
per call at 2,000 sessions. The cache grows with the vault, about 33 KB per session file and
6.5 KB per page; a vault several times this size is the point to bound it by bytes, never below
the event files the folds read.

**quick_check (R29b).** `keepfold.quarantine.check` runs `PRAGMA quick_check` once per store per
process, at its first open. On the 5k-page vectors (one 384-dim chunk a page, 5.3 MB) it took
20 ms; on the same pages with 6 chunks each (25.8 MB) 130 ms.
