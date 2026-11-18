"""I3 browse build <-> privacy <-> sites <-> vectors (docs/design/v6/TEST-PLAN.md §3), on a
synthetic History only: across a settings matrix, no visible file holds a hidden host, title
or search term; dropped rows are stored nowhere; the private vectors hold the private pages and
the main vectors do not; a re-import changes no byte; turning visibility off removes the public
day pages and voids the browse touches (Q12)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from conftest import events, run
from test_browse import Embed, hubbed

from cortex import semantic
from cortex.browse import Settings, build, sites, sources, vectors
from cortex.events import live
from cortex.vault import Vault
from integration.builders import fake_profile_root

pytestmark = pytest.mark.usefixtures("home")

D0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)  # a Tuesday: three days in one week


def history_rows(d0: datetime = D0) -> list[tuple]:
    """Cooking on three days (a visible hub: one touch), plus one row of each kind the filter
    must hide or drop. Every private word starts with "zq" so a grep cannot miss it."""
    rows = [(d0 + timedelta(days=d, minutes=k), f"https://food.example.com/pasta-{d}-{k}",
             f"Pasta recipe cooking day {d} part {k}") for d in range(3) for k in range(3)]
    t = d0 + timedelta(minutes=30)
    return rows + [
        (t, "https://www.pornhub.com/view_video.php?viewkey=zqadultkey", "ZQADULTTITLE clip"),
        (t + timedelta(minutes=1), "https://www.google.com/search?q=porn+zqadultterm",
         "porn zqadultterm - Google Search", "porn zqadultterm"),
        (t + timedelta(minutes=2), "https://www.webmd.com/zqhealthpage", "ZQHEALTHTITLE flu"),
        (t + timedelta(minutes=3), "https://www.google.com/search?q=zqhealthterm+symptoms",
         "zqhealthterm symptoms - Google Search", "zqhealthterm symptoms"),
        (t + timedelta(minutes=4), "https://zerodha.com/zqmoneypage", "ZQMONEYTITLE holdings"),
        (t + timedelta(minutes=5), "https://www.hdfcbank.com/zqbankpage", "ZQBANKTITLE"),
        (t + timedelta(minutes=6), "https://accounts.google.com/signin/zqsignin",
         "ZQSIGNINTITLE"),
        (t + timedelta(minutes=7), "https://example.com/cb?token=ZQTOKENVALUE",
         "Callback ZQTOKENTITLE"),
    ]


# what each category leaves behind if it leaks: hosts, title and term markers (lower case)
MARKERS = {"adult": ("pornhub", "zqadult"), "health": ("webmd", "zqhealth"),
           "money": ("zerodha", "zqmoney")}
DROPPED = ("hdfcbank", "zqbank", "zqsignin", "zqtoken", "accounts.google")

OFF = Settings()  # the code default: nothing visible, adult skipped
OWNER = Settings(adult="private", hidden_from_claude=("adult",), visible_to_claude=True)
PLAN = Settings(visible_to_claude=True, hidden_from_claude=("health", "money"),
                skip_categories=("adult",))


def hidden_markers(s: Settings) -> tuple[str, ...]:
    """Markers that must not reach a visible file under `s`: every non-cloud category's."""
    from cortex.browse import privacy

    return DROPPED + tuple(w for c, ws in MARKERS.items()
                           if privacy.audience(c, s) != "cloud" for w in ws)


def skipped_markers(s: Settings) -> tuple[str, ...]:
    """Markers that must be in no file at all under `s`."""
    return DROPPED + tuple(w for c in s.skip_categories for w in MARKERS.get(c, ()))


def files(root, *, private: bool | None = None) -> dict[str, bytes]:
    """Every file under browse/: private=True only browse/private/, False only outside it."""
    out = {}
    for p in sorted((root / "browse").rglob("*")):
        if not p.is_file():
            continue
        inside = p.is_relative_to(root / "browse" / "private")
        if private is None or inside == private:
            out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


def grep(blobs: dict[str, bytes], markers) -> list[tuple[str, str]]:
    return [(name, w) for name, data in blobs.items() for w in markers
            if w.encode() in data.lower()]


def every_file(root) -> dict[str, bytes]:
    """The whole vault, .cortex and .git included."""
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def local(home):
    return fake_profile_root(home / "LOCALAPPDATA", {"chrome": history_rows()})


@pytest.fixture
def vault(root):
    return hubbed(root)


def imp(root, local, settings, **kw):
    return build.run(Vault(root), sources.discover(local, local / "none"), settings,
                     embed=Embed(), tz=UTC, now=D0 + timedelta(days=3), **kw)


@pytest.mark.parametrize("settings", [OFF, OWNER, PLAN], ids=["off", "owner", "plan"])
def test_no_visible_file_holds_hidden_rows(vault, local, settings):
    rep = imp(vault, local, settings)
    assert rep["private"] and rep.get("dropped", 0) >= 3, rep
    public = files(vault, private=False)
    assert not grep(public, hidden_markers(settings)), grep(public, hidden_markers(settings))
    assert not grep(every_file(vault), skipped_markers(settings))
    private = b"".join(files(vault, private=True).values()).lower()
    for c, ws in MARKERS.items():  # a hidden (not skipped) category is kept, privately
        if c not in settings.skip_categories:
            assert all(w.encode() in private for w in ws), (c, settings)
    if settings.visible_to_claude:
        assert "browse/browse-2026-11-10.md" in public
        assert b"pasta recipe cooking" in b"".join(public.values()).lower()
    else:
        assert public == {}


def test_private_folder_only_when_something_is_private(vault, local):
    everything_skipped = Settings(visible_to_claude=True, hidden_from_claude=(),
                                  skip_categories=tuple(MARKERS))
    imp(vault, local, everything_skipped)
    leaked = grep(every_file(vault), skipped_markers(everything_skipped))
    assert not leaked, leaked
    private = files(vault, private=True)
    # what remains private is only the visible rows' archive: never a skipped row
    assert not grep(private, (*DROPPED, *sum(MARKERS.values(), ())))


def test_private_vectors_hold_hidden_pages_the_main_ones_do_not(vault, local, monkeypatch):
    monkeypatch.setattr(semantic, "_embedder", Embed())
    imp(vault, local, PLAN)
    sites.update_vectors(Vault(vault))
    priv = vectors._read(Vault(vault))
    assert any(p.startswith("browse/private/") for p in priv)
    assert any("webmd" in p for p in priv) and not any("pornhub" in p for p in priv)

    async def body(call):
        await call("cortex_recall", {"query": "pasta recipe cooking"})

    run(vault, body)
    with np.load(vault / semantic.CACHE, allow_pickle=False) as z:
        stored = " ".join(str(z[k]) for k in z.files).lower()
    assert "browse-" in stored  # the public browse pages were embedded
    assert "browse-private" not in stored and not grep({"v3": stored.encode()},
                                                        hidden_markers(PLAN))


def test_a_second_import_changes_no_byte(vault, local):
    imp(vault, local, PLAN)
    first = files(vault)
    rep = imp(vault, local, PLAN, since="2026-11-09")  # rebuild every day all the same
    assert files(vault) == first, rep


def test_turning_visibility_off_removes_public_pages_and_voids_touches(vault, local):
    imp(vault, local, OWNER)
    assert list((vault / "browse").glob("browse-*.md"))
    touches = [e for e in live(events(vault)) if str(e.get("key", "")).startswith(
        "browse:touch:")]
    assert touches, "three cooking days earn a touch"
    rep = imp(vault, local, replace(OWNER, visible_to_claude=False))
    assert rep["browse_voided"] == len(touches)
    assert not list((vault / "browse").glob("browse-*.md"))
    assert files(vault, private=False) == {}
    assert not [e for e in live(events(vault)) if str(e.get("key", "")).startswith(
        "browse:touch:")]
    assert files(vault, private=True)  # the private archive stays
