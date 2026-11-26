"""`keepfold` for the E2E tests, run as `sys.executable -I _launch.py <args>`: FakeEmbed as the
embedder (no model download), no browser opened, and Ollama's fixed port 11434 redirected to
the one the test picked (KEEPFOLD_E2E_OLLAMA, else refused). No src/ change is needed."""

import os
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/, for conftest.FakeEmbed

from conftest import FakeEmbed  # noqa: E402

from keepfold import localllm, semantic  # noqa: E402
from keepfold.app import server  # noqa: E402
from keepfold.cli import app  # noqa: E402

OLLAMA = "http://127.0.0.1:11434"

webbrowser.open = lambda *a, **k: True
server._open = lambda *a, **k: True  # a distinct object: _open_path would os.startfile otherwise
semantic._embedder = FakeEmbed()
_real_request = localllm._request


def _request(url, body, timeout):
    if url.startswith(OLLAMA):
        if not (fake := os.environ.get("KEEPFOLD_E2E_OLLAMA")):
            raise OSError("no Ollama in the E2E tests")
        url = fake + url[len(OLLAMA):]
    return _real_request(url, body, timeout)


localllm._request = _request

if __name__ == "__main__":
    app()
