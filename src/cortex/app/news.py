"""What's new: the card Status shows once after an update. The text ships in
assets/whats_new.json (version -> {items, do}); the last version shown is `app.whats_new_seen`
in ~/.cortex/config.yaml, never in the vault."""

from __future__ import annotations

import json
from pathlib import Path

from cortex import update
from cortex.app import status

NEWS = Path(__file__).resolve().parents[1] / "assets" / "whats_new.json"


def _v(s) -> tuple[int, ...] | None:
    try:
        return update.parse_version(s)
    except update.UpdateError:
        return None


def _entries() -> dict:
    return json.loads(NEWS.read_text(encoding="utf-8"))


def _seen() -> tuple[int, ...] | None:
    app = status.config().get("app")
    return _v(app.get("whats_new_seen")) if isinstance(app, dict) else None


def record(version: str | None = None) -> None:
    """Remember `version` (the running one by default) as shown."""
    from cortex import taint
    seen = version or update.current()
    taint.write_config(lambda cfg: {"app": {
        **(cfg["app"] if isinstance(cfg.get("app"), dict) else {}), "whats_new_seen": seen}})


# the last version without this card: an existing user with no record updated from it or earlier
BEFORE_NEWS = "0.2.1"


def start() -> None:
    """On app start, with no readable record: a fresh install (no vault set yet) records this
    version silently; an existing user came from BEFORE_NEWS or earlier, so sees what is new."""
    if _seen() is None and _v(update.current()):
        record(BEFORE_NEWS if status.config().get("vault") else None)


def card(current: str | None = None) -> dict:
    """{"show": the entries after the last seen up to the running version, newest first,
    "current": this version's entry or None}. Nothing remembered shows nothing."""
    cur = current or update.current()
    cv, seen, news = _v(cur), _seen(), _entries()
    keyed = sorted(((_v(k), k) for k in news if _v(k)), reverse=True)
    show = [{"version": k, **news[k]} for v, k in keyed
            if cv and seen and update.newer(v, seen) and not update.newer(v, cv)]
    return {"show": show, "current": {"version": cur, **news[cur]} if cur in news else None}
