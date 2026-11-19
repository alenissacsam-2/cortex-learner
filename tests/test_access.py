"""SPEC-privacy-gate §5 Matrix: every viewer x audience x kind x switch/flag against may_see,
plus audience_of cases that check the helpers delegate to the existing rules."""

from itertools import product
from pathlib import Path

import pytest
from ruamel.yaml.comments import CommentedMap

from cortex.access import (
    KINDS,
    VIEWERS,
    Audience,
    audience_of_browse,
    audience_of_note,
    audience_of_study,
    may_see,
)
from cortex.browse.privacy import Settings
from cortex.vault import Note

S, L, C = Audience.SKIP, Audience.LOCAL, Audience.CLOUD
# The spec's §2 table, written out. A cell is True/False, a condition, or {kind: condition}.
# Conditions: "private_flag+tty" (both), "cloud_opt_in", "sw:<switch>" (Chat's switch on).
MATRIX = {
    "mcp":         {S: False, L: False,              C: True},
    "dashboard":   {S: False, L: False,              C: True},
    "page_public": {S: False, L: False,              C: True},
    "today_md":    {S: False, L: False,              C: True},
    "start_card":  {S: False, L: False,              C: True},
    "terminal":    {S: False, L: "private_flag+tty", C: True},
    "chat_local":  {S: False, L: "sw:private",
                    C: {"note": "sw:notes", "chat": "sw:chats", "browse": "sw:browsing",
                        "study": "sw:notes"}},
    "cloud_model": {S: False, L: False,              C: "cloud_opt_in"},
}
SWITCHES = ("notes", "chats", "browsing", "private")
SWITCH_SETS = [None, *(dict(zip(SWITCHES, bits, strict=True))
                       for bits in product((False, True), repeat=4))]


def _expected(cell, kind, switches, private_flag, tty, cloud_opt_in) -> bool:
    cell = cell[kind] if isinstance(cell, dict) else cell
    if isinstance(cell, bool):
        return cell
    if cell == "private_flag+tty":
        return private_flag and tty
    if cell == "cloud_opt_in":
        return cloud_opt_in
    return bool((switches or {}).get(cell.removeprefix("sw:")))


def test_matrix_covers_every_viewer_and_audience():
    assert set(MATRIX) == set(VIEWERS)
    assert all(set(row) == set(Audience) for row in MATRIX.values())


@pytest.mark.parametrize("viewer", VIEWERS)
def test_may_see_matrix(viewer):
    """Every audience, kind, switch set and flag; flags a viewer does not use never widen."""
    for audience, kind, sw, pf, tty, opt in product(Audience, KINDS, SWITCH_SETS,
                                                   (False, True), (False, True), (False, True)):
        want = _expected(MATRIX[viewer][audience], kind, sw, pf, tty, opt)
        got = may_see(viewer, audience, kind, sw, private_flag=pf, tty=tty, cloud_opt_in=opt)
        assert got is want, (viewer, audience, kind, sw, pf, tty, opt)
        assert may_see(viewer, audience.value, kind, sw, private_flag=pf, tty=tty,
                       cloud_opt_in=opt) is want  # browse.privacy's plain strings work too


def test_terminal_needs_both_private_flag_and_tty():
    assert not may_see("terminal", L, "note", private_flag=True)  # piped: no TTY
    assert not may_see("terminal", L, "note", tty=True)
    assert may_see("terminal", L, "note", private_flag=True, tty=True)


def test_chat_local_without_switches_sees_nothing():
    assert not any(may_see("chat_local", a, k) for a in Audience for k in KINDS)


@pytest.mark.parametrize("args", [("claude", C, "note"), ("mcp", "public", "note"),
                                  ("mcp", C, "email")])
def test_unknown_names_raise(args):
    with pytest.raises(ValueError):
        may_see(*args)


def _note(kind="note", id="n1", **fields) -> Note:
    return Note(Path(f"{id}.md"), kind, id, CommentedMap(fields), "")


@pytest.mark.parametrize(("note", "hidden", "want"), [
    (_note(), None, C),
    (_note(private=True), None, L),
    (_note(private="yes"), None, L),
    (_note(private="true"), None, L),
    (_note(private="On"), None, L),
    (_note(private=1), None, L),
    (_note(private=False), None, C),
    (_note(private="no"), None, C),
    (_note("person", "p1"), None, L),  # people pages are private by default
    (_note("person", "p1", private=False), None, C),
    (_note(id="c1"), {"c1"}, L),  # under a planner.hidden subject
    (_note(id="c2"), {"c1"}, C),
])
def test_audience_of_note(note, hidden, want):
    assert audience_of_note(note, hidden) is want


SET = Settings(skip_categories=("adult",), hidden_from_claude=("health",),
               visible_to_claude=True, private=("secret.example",))


@pytest.mark.parametrize(("category", "settings", "kw", "want"), [
    ("adult", SET, {}, S),
    ("health", SET, {}, L),
    ("money", SET, {}, C),
    (None, SET, {}, C),
    ("money", SET, {"host": "www.secret.example"}, L),  # a private_domains host
    ("adult", SET, {"host": "secret.example"}, S),  # skip wins over a private host
    ("money", SET, {"private": True}, L),  # Kept.private / Kept.local
    (None, Settings(), {}, L),  # code defaults: nothing reaches the cloud
    ("health", Settings(), {}, L),
    ("adult", Settings(), {}, S),  # adult is skipped by default
    ("adult", Settings(adult="private", skip_categories=()), {}, L),
])
def test_audience_of_browse(category, settings, kw, want):
    assert audience_of_browse(category, settings, **kw) is want


def test_audience_of_study():
    assert audience_of_study(["c1"], set()) is C
    assert audience_of_study(["c1", "c2"], {"c2"}) is L
    assert audience_of_study(["c1"], set(), note_private=True) is L
    assert audience_of_study([], {"c1"}) is C
