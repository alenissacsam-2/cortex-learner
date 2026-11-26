# Contributing to Keepfold

## Report a bug

In the app, open Status, then **Something not working?**, press **Copy details**, then **Report on GitHub** and paste the details into the form. Nothing is sent until you submit the form. Without the app, run `keepfold doctor --report`.

The details hold versions and what is on or off. They leave out your notes, chats, browsing, API keys and file paths. Please keep those out of anything you write too. For a security problem, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## Suggest a feature

Open a [feature request](https://github.com/alenissacsam-2/keepfold/issues/new?template=feature_request.yml) and say what you are trying to do.

## Run Keepfold from source

```
uv sync
uv run keepfold --help
uv run pytest -m "not slow"
uv run ruff check
```

Always run Python through `uv run`, never bare `python` or `pip`.

## The privacy rule

Tests use only the made-up data in `tests/fixtures/`. Never copy a real vault into the repo, use one as a fixture or commit one.

## Docs

[README.md](README.md) (start here), [docs/GUIDE.md](docs/GUIDE.md) (the user guide), [docs/INSTALL.md](docs/INSTALL.md), [docs/PRIVACY.md](docs/PRIVACY.md), [docs/tools.md](docs/tools.md) (tool schemas), and [filemap.md](filemap.md) (every file). The rules for contributors and AI agents are in [CLAUDE.md](CLAUDE.md).
