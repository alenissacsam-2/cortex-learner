"""Every way data can leave Cortex, in one place (E1). tests/test_egress.py fails when code under
src/cortex reaches the network from a module that has no entry here, and when an entry's module
no longer does; `cortex doctor --privacy` and the app's Status print `summary()`.

`loopback` is True when nothing leaves this PC (127.0.0.1 only, or nothing sent at all)."""

from __future__ import annotations

from typing import NamedTuple


class Egress(NamedTuple):
    module: str  # path under src/cortex
    dest: str  # host, or a plain name for one the person chooses
    what: str  # what is sent or fetched, in plain words
    when: str  # who starts it
    loopback: bool = False


REGISTRY: tuple[Egress, ...] = (
    Egress("update.py", "github.com",
           "a request for the newest Cortex version (nothing about you, not even your version) "
           "and, when there is a newer one, the installer and its checksum",
           "a minute after the app starts and every 6 hours while it runs, and when you click "
           "Check for updates or Update now"),
    Egress("app/wizard.py", "ollama.com, git-scm.com and LM Studio's catalogue",
           "the name of a local model to look up its size, and the downloads of Ollama, Git "
           "or a local model (through winget, Homebrew, ollama pull or lms get)",
           "only when you click Install, Download or Get in Setup or Local model"),
    Egress("voice_memos.py", "pypi.org and huggingface.co",
           "the faster-whisper package and the speech model for voice memos",
           "only when you run `cortex model download --voice` or click its Download button"),
    Egress("semantic.py", "huggingface.co",
           "the search model and the reranker (about 150 MB, once, into ~/.cortex/models)",
           "only when you run `cortex model download` or click Download search model; "
           "searches never download"),
    Egress("cloudllm.py", "your cloud model's address",
           "titles of browsed pages and their sites, for pages in categories you set to cloud, "
           "with your API key",
           "only when you turn on cloud processing and set a cloud model"),
    Egress("cli.py", "github.com (through Claude Code)",
           "Claude Code's download of the Cortex plugin",
           "only when `cortex setup` asks to install the Claude Code plugin and you say yes"),
    Egress("app/targets.py", "github.com (through Claude Code)",
           "Claude Code's download of the Cortex plugin",
           "only when you connect Claude Code in Connect AI apps"),
    Egress("localllm.py", "localhost (Ollama or LM Studio)",
           "your question and the notes it needs, to the local model on this PC",
           "when you use Chat or a local-model feature", loopback=True),
    Egress("app/server.py", "localhost",
           "the app's own page, and a check whether an app is already running",
           "while the app runs", loopback=True),
    Egress("_proc.py", "nothing (reads this PC's proxy setting)",
           "nothing: the system proxy is read and handed to the downloads above",
           "with those downloads", loopback=True),
)


def summary() -> list[str]:
    """One plain line per destination off this PC."""
    return [f"{e.dest}: {e.what}. {e.when[0].upper()}{e.when[1:]}."
            for e in REGISTRY if not e.loopback]
