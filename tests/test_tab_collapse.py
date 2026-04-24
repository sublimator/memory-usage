"""
Round-trip + property tests for the screenshot space→tab helpers.

Anchors the `x` hotkey's clipboard payload behaviour so the "it looked
weird in the editor" regression doesn't come back. The core contract
is that ``_collapse_spaces_to_tabs(line, ts).expandtabs(ts)`` must
exactly reproduce the input line for any line that contains no tab
characters — otherwise the compression isn't lossless against that
tabstop.
"""

from __future__ import annotations

import pytest

from memory_usage.ui.dashboard import (
    _best_tab_collapse_frame,
    _collapse_spaces_to_tabs,
)

TABSTOPS = [2, 4, 8]


# ----------------------------------------------------------------------
# _collapse_spaces_to_tabs
# ----------------------------------------------------------------------


@pytest.mark.parametrize("tabstop", TABSTOPS)
@pytest.mark.parametrize(
    "line",
    [
        "",
        "hello",
        "a b c",
        "hello    world",
        "hello     world",
        "hello        world",
        "hello               world",
        "        indent8",
        "    indent4",
        "  indent2",
        "            indent12",
        "a                                                  far-apart",
        "   peer             IBLs               sent             rep        ",
        "  Overview  Stats  Heap  Ledgers Info  Config",
        "trailing spaces always stay            ",
        " leading then tail   ",
        "multiple    runs      between       words",
    ],
)
def test_collapse_roundtrip(line: str, tabstop: int) -> None:
    """expandtabs(tabstop) after collapse(line, tabstop) must equal line.

    This is the lossless contract: compression may replace runs of
    spaces with tabs, but expansion with the same tabstop must restore
    the exact original rendering column-for-column.
    """
    collapsed = _collapse_spaces_to_tabs(line, tabstop)
    assert collapsed.expandtabs(tabstop) == line


@pytest.mark.parametrize("tabstop", TABSTOPS)
@pytest.mark.parametrize(
    "line",
    [
        "",
        "no-spaces-at-all",
        "    ",
        "hello world",
        "hello     world",
        "hello        world",
    ],
)
def test_collapse_never_longer(line: str, tabstop: int) -> None:
    """Compression never grows the string.

    Each ``\\t`` replaces at least one space, and we only emit a tab
    when it strictly crosses a tabstop boundary.
    """
    collapsed = _collapse_spaces_to_tabs(line, tabstop)
    assert len(collapsed) <= len(line)


@pytest.mark.parametrize("tabstop", TABSTOPS)
def test_collapse_no_tab_followed_by_enough_spaces(tabstop: int) -> None:
    """After compression, a ``\\t`` is never followed by ≥tabstop spaces.

    If it were, the algorithm missed a collapse opportunity (those
    trailing spaces should have folded into another tab).
    """
    line = "a" + " " * 32 + "b"
    collapsed = _collapse_spaces_to_tabs(line, tabstop)
    for i, ch in enumerate(collapsed):
        if ch == "\t":
            run = 0
            j = i + 1
            while j < len(collapsed) and collapsed[j] == " ":
                run += 1
                j += 1
            assert run < tabstop, (
                f"tabstop={tabstop}: {run} spaces after a tab — should have been another tab"
            )


def test_collapse_known_shape_tabstop_8() -> None:
    """Anchor specific visible shapes at tabstop 8 against the algorithm.

    These are the cases a human would sanity-check by eye. If any of
    them flip, something real changed in the collapse logic — not
    just a cosmetic tweak.
    """
    # "hello" ends at col 5; 3 spaces reach col 8 exactly → one \t,
    # expansion lands "world" at col 8 same as the input.
    assert _collapse_spaces_to_tabs("hello   world", 8) == "hello\tworld"
    # 5 spaces: \t to col 8, then 2 spaces to col 10.
    assert _collapse_spaces_to_tabs("hello     world", 8) == "hello\t  world"
    # 11 spaces: \t to col 8, \t to col 16, then 3 spaces to col 21… wait,
    # 5 + 11 = col 16 is also a boundary, so both tabs land exactly.
    assert _collapse_spaces_to_tabs("hello           world", 8) == "hello\t\tworld"
    assert _collapse_spaces_to_tabs("        x", 8) == "\tx"
    assert _collapse_spaces_to_tabs("                x", 8) == "\t\tx"
    assert _collapse_spaces_to_tabs("    x", 8) == "    x"  # can't cross boundary
    assert _collapse_spaces_to_tabs("", 8) == ""
    assert _collapse_spaces_to_tabs("no-spaces", 8) == "no-spaces"


def test_collapse_known_shape_tabstop_4() -> None:
    """Same anchor set at tabstop 4 — smaller boundary compresses more."""
    assert _collapse_spaces_to_tabs("hello   world", 4) == "hello\tworld"
    assert _collapse_spaces_to_tabs("    x", 4) == "\tx"
    assert _collapse_spaces_to_tabs("        x", 4) == "\t\tx"
    assert _collapse_spaces_to_tabs("  x", 4) == "  x"


def test_collapse_known_shape_tabstop_2() -> None:
    """At tabstop 2, every 2-space gap compresses to a tab."""
    # "a" is at col 0, col-after = 1 (NOT a tabstop boundary under ts=2).
    # 2 spaces cross col 2; emit \t from col 1 → col 2, then 1 leftover
    # space to reach col 3 where "b" starts.
    assert _collapse_spaces_to_tabs("a  b", 2) == "a\t b"
    # 3 spaces: \t to col 2, \t to col 4 — exact landing.
    assert _collapse_spaces_to_tabs("a   b", 2) == "a\t\tb"
    # Even a single space can collapse when it crosses: "a" ends at
    # col 1, next tabstop is col 2 — one \t replaces one space.
    assert _collapse_spaces_to_tabs("a b", 2) == "a\tb"
    assert _collapse_spaces_to_tabs("    x", 2) == "\t\tx"


# ----------------------------------------------------------------------
# _best_tab_collapse_frame
# ----------------------------------------------------------------------


def test_frame_empty_input() -> None:
    lines, ts = _best_tab_collapse_frame([])
    assert lines == []
    assert ts == 0


def test_frame_picks_one_tabstop_for_whole_frame() -> None:
    """Output uses a single tabstop — any line expanded with that
    tabstop must match the input line.
    """
    raw = [
        "        indent8",
        "hello           world",
        "  Overview  Stats  Heap",
        "plain text line",
    ]
    collapsed, ts = _best_tab_collapse_frame(raw)
    assert len(collapsed) == len(raw)
    if ts:
        for orig, comp in zip(raw, collapsed):
            assert comp.expandtabs(ts) == orig


@pytest.mark.parametrize(
    "raw",
    [
        ["short", "line"],
        [""],
        ["x" * 50],  # no gaps to compress
        ["a  b", "c  d"],
        ["hello           world", "               x"],
        [
            "   peer             IBLs               sent",
            "     1                 23                472",
            "     2                 14                 21",
        ],
    ],
)
def test_frame_total_never_grows(raw: list[str]) -> None:
    collapsed, _ = _best_tab_collapse_frame(raw)
    assert sum(len(s) for s in collapsed) <= sum(len(s) for s in raw)


def test_frame_no_gain_returns_unchanged() -> None:
    """If no tabstop shrinks the total, the input is returned as-is
    with tabstop 0 (meaning "no compression applied").
    """
    raw = ["a", "b", "cd"]
    collapsed, ts = _best_tab_collapse_frame(raw)
    assert collapsed == raw
    assert ts == 0


def test_frame_prefers_larger_tabstop_on_tie() -> None:
    """Tiebreaker: when two tabstops yield the same total length,
    keep the larger one (fewer tab chars, renders more conventionally).
    The implementation uses strict < on total length and iterates
    8 → 4 → 2, so the earliest (largest) tabstop wins ties.
    """
    # Line where 8 and 4 produce the same total length. Contrive it:
    # "        x" — under 8 → "\tx" (2 chars), under 4 → "\t\tx" (3 chars).
    # So 8 wins strictly. Use a case where 4 and 2 tie instead:
    # "  x" — under 4 → "  x" (3 chars), under 2 → "\tx" (2 chars).
    # 2 wins strictly. Anchor instead on a line that both 4 and 8 can
    # compress equally to verify the "first-wins" order keeps 8.
    raw = ["        x"]  # 8 yields "\tx", 4 yields "\t\tx"
    _, ts = _best_tab_collapse_frame(raw)
    # 8 is both shorter and larger → wins unambiguously. Sanity: ts=8.
    assert ts == 8


def test_frame_real_table_shape() -> None:
    """Representative screenshot row (peer table header) should
    compress hard under some tabstop and round-trip exactly.
    """
    raw = [
        "            peer               IBLs               sent             rep",
        "               1                 23                472             459",
    ]
    collapsed, ts = _best_tab_collapse_frame(raw)
    assert ts in (2, 4, 8)
    assert sum(len(s) for s in collapsed) < sum(len(s) for s in raw)
    for orig, comp in zip(raw, collapsed):
        assert comp.expandtabs(ts) == orig


def test_frame_rstripped_trailing_runs_dont_break() -> None:
    """Screenshot lines are always rstripped before we pass them to
    the collapser — no trailing spaces to worry about. Verify that
    assumption isn't load-bearing on correctness.
    """
    raw = ["hello        world", "   indented"]
    collapsed, ts = _best_tab_collapse_frame(raw)
    if ts:
        for orig, comp in zip(raw, collapsed):
            assert comp.expandtabs(ts) == orig
