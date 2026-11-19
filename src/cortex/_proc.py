"""Shared subprocess flags. The installed app (`cortex-app.exe`) has no console, and on Windows
every console child of such a process opens its own visible window unless it is started with
CREATE_NO_WINDOW: pass `creationflags=NO_WINDOW` to every subprocess (0 elsewhere)."""

import os
import subprocess
import urllib.request

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


def proxy_env(base: dict | None = None) -> dict:
    """`base` (default os.environ) plus the system proxy (the Windows registry, macOS settings)
    as HTTP(S)_PROXY/NO_PROXY when neither is set, for downloaders that read only the env (uv,
    hf_xet, ollama). shortcut: a PAC file (automatic proxy script) is not read; a network that
    needs one still needs HTTPS_PROXY set by hand."""
    env = dict(os.environ if base is None else base)
    if not any(env.get(k) for k in _PROXY_VARS):
        for scheme, url in urllib.request.getproxies().items():
            if scheme in ("http", "https", "no"):
                env[f"{scheme.upper()}_PROXY"] = url
    return env
