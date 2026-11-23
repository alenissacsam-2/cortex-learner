# Memory of a long-lived Cortex process

`uv run python scripts/memory_probe.py --notes 1000 --visits 10000`: one process takes the steps
a long-lived MCP server (`cortex mcp`) and the app take, on a synthetic vault in a temp folder
with a temp HOME (1,000 made-up pages, one in five long, 2,704 chunks; a browse import of 10,000
made-up visits, 369 browse pages, 1,104 chunks). The models are the real ones from
`~/.cortex/models` (bge-small-en-v1.5 and ms-marco-MiniLM-L-6-v2); nothing else of `~/.cortex`
is read and nothing is downloaded.

Machine: 8 cores (16 logical), 23.7 GB RAM, Windows 11. Other work kept the CPU near 100%
during every run, so the seconds are noisy; the memory numbers are not.

`ws` is the working set, `private` the private bytes (commit), both in MB after the step.

| step | before ws | before private | after ws | after private |
|---|---:|---:|---:|---:|
| python started | 21 | 13 | 21 | 12 |
| import cortex (mcp_server, semantic, home) | 55 | 522 | 54 | 522 |
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

## The fixes (src/cortex/semantic.py)

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
`cortex model build` once; a short-lived build process could turn the arena back on (set
`semantic.ARENA = True` before the model loads), since its arena dies with it.

## What is left (not changed here)

- **numpy's OpenBLAS commits about 490 MB of private bytes at import** (a buffer per thread,
  16 threads here; working set +13 MB). With `OPENBLAS_NUM_THREADS=4` set before numpy loads
  it is 107 MB. It counts against the commit limit, not the working set Task Manager shows by
  default. It has to be set in the process's entry point, before any import of numpy.
- **The index build's transient peak**, about 900 MB above the loaded model at a batch of 64
  long heads, lasts while the batch runs. Smaller batches for long texts would lower it, at
  some cost in speed without the arena (the table above).
- **The app's WebView2 window** is separate processes, not Python: a hidden pywebview window on
  a plain page had 6 `msedgewebview2.exe` children with about 330 MB working set and 150 MB
  private bytes in all, before any of Cortex's own page (the 3D brain draws on the GPU process).
  Nothing in Python changes it; only closing the web view while the window is hidden would.
- **What stays after the drop** (186 MB working set): Python and the imported modules, the
  loaded onnxruntime and tokenizer libraries, the MCP core's catalog and index, the vector
  matrices kept for queries (`semantic._MEM`, `browse.vectors._MEM`: 4 bytes × 384 per chunk)
  and the app home's caches. tracemalloc (`--trace`) counted 74 MB of Python allocations at
  idle: 22 MB by the imports, 20 MB on loading the embedder (fastembed, tokenizers), 5 MB by
  the index build, 21 MB by the app home; nothing big was left from a build to release.
  (In that run the steps were slow enough that the idle thread itself dropped the embedder and
  the reranker while the background copy worked, 82 MB, and the next search loaded them again.)
