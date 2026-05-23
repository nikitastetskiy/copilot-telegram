"""
@spec-handoff
@interface _clean_ansi(text: str) -> str
@behavior
  - Removes all ANSI/VT escape sequences from text
  - DEC private-mode CSI (ESC[?25l, ESC[?25h) → stripped
  - OSC title sequences (ESC]0;...BEL) → stripped
  - 2-char keypad escapes (ESC=, ESC>) → stripped
  - SGR colour codes (ESC[32m, ESC[0m) → stripped
  - Cursor save/restore + cursor-position CSI (ESC7, ESC[H, ESC8) → stripped
  - Remaining control characters replaced by space (CTRL_RE)
@edge-cases
  - No ESC byte must survive in the cleaned output
  - No '?25l', ']0;', '[?25h' residue must remain
@see copilot_watcher.py _clean_ansi (~line 577)
"""

from __future__ import annotations

import pytest
from copilot_watcher import _clean_ansi


@pytest.mark.parametrize("inp,expected", [
    # DEC private mode cursor hide/show — Bug #2 regression (Sui T1)
    (
        "\x1b[?25l\x1b[0m│ ❯ 1. foo │\x1b[0m\x1b[?25h",
        "│ ❯ 1. foo │",
    ),
    # OSC title sequence (ESC ] 0 ; ... BEL)
    (
        "\x1b]0;Terminal — copilot\x07line",
        "line",
    ),
    # 2-char keypad mode escapes (ESC= enter-keypad-mode, ESC> exit)
    (
        "\x1b=\x1b>foo",
        "foo",
    ),
    # SGR colour codes
    (
        "\x1b[32mhello\x1b[0m",
        "hello",
    ),
    # Cursor save (ESC 7), cursor-position CSI (ESC[H), cursor restore (ESC 8)
    (
        "\x1b7\x1b[Hsaved\x1b8",
        "saved",
    ),
])
def test_clean_ansi_strips_sequence(inp, expected):
    """_clean_ansi must remove the given escape sequence, leaving only text."""
    result = _clean_ansi(inp)
    assert "\x1b" not in result, \
        f"ESC byte survived cleaning: {result!r}"
    assert "?25l" not in result, \
        f"'?25l' residue in cleaned output: {result!r}"
    assert "]0;" not in result, \
        f"']0;' residue in cleaned output: {result!r}"
    assert result == expected, \
        f"_clean_ansi({inp!r})\n  got:      {result!r}\n  expected: {expected!r}"
