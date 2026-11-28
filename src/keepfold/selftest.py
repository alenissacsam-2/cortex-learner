"""`keepfold selftest` (hidden): import every module of the package and read every package data
file, the way a frozen app (PyInstaller) loads them lazily. A frozen build's smoke test runs it to
catch what the freeze missed; it opens no vault, no home and no network."""

from __future__ import annotations

import importlib
import pkgutil
import sys
from importlib.resources import files
from importlib.resources.abc import Traversable

# A module that imports a platform's own packages at import time, and the sys.platform it needs.
# Empty by rule: pyobjc, gi, winreg and winrt imports stay lazy inside functions, so every module
# imports on every OS; an entry here is the exception, e.g. {"keepfold.app.mac_x": "darwin"}.
PLATFORM_ONLY: dict[str, str] = {}


class Failed(Exception):
    """The first module or data file that failed, by name."""


def modules(package: str = "keepfold") -> int:
    """Import every module under `package`; the number imported."""
    def fail(name: str) -> None:  # walk_packages could not import a package to look inside it
        raise Failed(f"module {name}: {sys.exc_info()[1]!r}")

    n = 0
    root = importlib.import_module(package)
    for m in pkgutil.walk_packages(root.__path__, package + ".", onerror=fail):
        if PLATFORM_ONLY.get(m.name, sys.platform) != sys.platform:
            continue
        try:
            importlib.import_module(m.name)
        except (Exception, SystemExit) as e:  # a module must never exit on import either
            raise Failed(f"module {m.name}: {e!r}") from e
        n += 1
    return n


def data_files(package: str = "keepfold") -> int:
    """Read every non-Python file under `package` through importlib.resources; the number read."""
    def walk(node: Traversable, path: str) -> int:
        n = 0
        for child in node.iterdir():
            name = f"{path}/{child.name}"
            if child.is_dir():
                if child.name != "__pycache__":
                    n += walk(child, name)
            elif not child.name.endswith((".py", ".pyc")):
                try:
                    child.read_bytes()
                except OSError as e:
                    raise Failed(f"data file {name}: {e!r}") from e
                n += 1
        return n

    return walk(files(package), package)


def run() -> str:
    """The success line; raises Failed on the first failure."""
    return f"selftest ok {modules()} modules {data_files()} files"
