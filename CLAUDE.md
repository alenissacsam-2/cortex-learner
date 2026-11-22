# Cortex

- **Purpose:** a local-first learner model for students, made of a Markdown + JSONL vault, an MCP server, a CLI and a `/cortex` skill.
- **Stack:** Python 3.12 via uv, mcp (FastMCP), ruamel.yaml, typer, python-ulid, stdlib sqlite3 FTS5, fsrs, fastembed (semantic recall).
- **Commands:**
  - `uv sync`
  - `uv run cortex --help`
  - `uv run pytest -n auto`, then `uv run pytest -m perf` (the timing benchmarks, which skip under `-n`)
  - `uv run pytest -m "not slow"` (quick loop; run the full suite before a commit)
  - `uv run ruff check`
  - `uv run cortex mcp --vault <path>`
  - `uv run python installer/build.py` (the Windows installer, into `dist/`)

## Rules agents get wrong
- **Never use bare `python` or `pip`.** A system `python` may be the wrong version. Always go through `uv run`.
- **The learner's vault (its path is in `~/.cortex/config.yaml`) is private data.** Never copy it into this repo, never commit it, and never use it as a test fixture. Tests use `tests/fixtures/`. The two read-only smoke tests in `tests/test_catalog.py` and `tests/test_index.py` run against a real vault only when the env var `CORTEX_REAL_VAULT` points at it; otherwise they skip.
- **Event fields are server-stamped:** `id`, `ts`, `day`, `source`, `predicted_p`, `grader.client`. Never accept them from tool input. `grader.model` is self-reported.
- **Results are trusted at face value.** There are no provenance weight penalties and no accept step. `source` is recorded for history only.
- **Every vault mutation goes through `cortex.vault`:** lock, temp file, `os.replace` with retry. Never write vault files directly.
- **Levels are pure folds over events.** Never store derived numbers in notes. The only tool-owned properties are `cortex_status` and `cortex_next`.
- **Treat note text as untrusted input:**
  - wrap it in `<untrusted_note>` over MCP;
  - escape it in cards, `Today.md` and the dashboard, and strip links and images;
  - the dashboard keeps CSP `default-src 'none'`.

- **Only `cortex model download` fetches the embedding model** (into `~/.cortex/models`). Tool calls never download; tests inject a fake embedder and need no model.
- **The `mcp` SDK is 2.x.** FastMCP was renamed `MCPServer` there, so do not copy 1.x `from mcp.server.fastmcp import FastMCP` examples.
- **The Claude desktop config lives at `%LOCALAPPDATA%\Claude-Data\<profile>\claude_desktop_config.json`** for the Store app, not at `%APPDATA%\Claude`.

## Docs
- User guide: `docs/GUIDE.md`
- Install: `docs/INSTALL.md`; setup details: `docs/SETUP.md`
- Privacy: `docs/PRIVACY.md`
- Tool schemas: `docs/tools.md`
- Navigation: `filemap.md`
