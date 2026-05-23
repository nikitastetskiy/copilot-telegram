"""
Shared pytest helpers for the copilot-telegram test suite.

This file must:
  - Import cleanly before copilot_watcher is importable.
  - Never import copilot_watcher at module scope.
  - Expose load_fixture() as a plain function (not a pytest fixture) so test
    modules can call it directly without going through pytest's fixture system.
  - Generate the dirty_ansi_with_dec_private.txt fixture at session start so the
    file always contains real ESC bytes (0x1B) regardless of how the repo was
    cloned — some editors strip non-printable bytes on save.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make copilot_watcher importable from test modules.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

# ── Constants ─────────────────────────────────────────────────────────────────

SCREENS_DIR: Path = Path(__file__).parent / "fixtures" / "screens"


# ── Fixture loader ────────────────────────────────────────────────────────────

def load_fixture(name: str) -> str:
    """Load a screen fixture by filename (with or without .txt extension).

    Steps:
      1. Resolve path: SCREENS_DIR / name  (appends .txt if not already present)
      2. Read as UTF-8 bytes decoded to str.
      3. Strip every line whose first non-space character is ``#`` — these are
         human-readable annotation lines embedded in fixture files (e.g.
         ``# FIXTURE: …``, ``# Expected classification: …``, ``# Reason: …``).
         Terminal-content lines never legitimately start with ``#`` in these
         fixtures, so this is safe.  The parser must never see comment lines.
      4. Return the stripped string.

    Raises FileNotFoundError if the fixture does not exist.
    """
    fname = name if name.endswith(".txt") else f"{name}.txt"
    path = SCREENS_DIR / fname
    raw = path.read_bytes().decode("utf-8")
    lines = [ln for ln in raw.splitlines(keepends=True) if not ln.lstrip().startswith("#")]
    return "".join(lines)


# ── Dirty ANSI fixture generator ──────────────────────────────────────────────

def _write_dirty_ansi_fixture() -> None:
    """Write dirty_ansi_with_dec_private.txt with REAL ESC bytes (0x1B).

    Ink re-renders the entire widget on EVERY keypress/cursor-move, wrapping
    EACH LINE with cursor-hide/show and SGR sequences:

        ESC[?25l  ESC[32m  <widget-line>  ESC[0m  ESC[?25h

    With the CURRENT (broken) ANSI_RE:
      • ESC[32m and ESC[0m are removed (standard CSI — matched by the regex).
      • ESC[?25l and ESC[?25h are NOT removed (private-mode CSI — regex misses ?).
      • CTRL_RE then replaces the bare ESC bytes with spaces, leaving " [?25l"
        at the START of every cleaned line.
      • Bilateral pipe check: `stripped.startswith(("│", "|"))` → False (starts
        with "[") → footer_idx = None → parse_screen returns {"kind": "idle"}.
      → This is Bug #2.

    After Kou's fix (E4), the expanded ANSI_RE strips ESC[?25l/h and OSC
    sequences completely.  Each cleaned line starts with "│…" → bilateral
    passes → parse_screen returns kind=selection, 2 options, selected_index=0.
    """
    path = SCREENS_DIR / "dirty_ansi_with_dec_private.txt"
    if path.exists() and path.read_bytes().count(b"\x1b") > 10:
        return  # already present with real ESC bytes — protects against repeated session starts

    ESC = "\x1b"
    BEL = "\x07"
    # Each widget line is individually wrapped, mirroring real Ink output.
    widget_lines = [
        f"{ESC}]0;Terminal — copilot{BEL}",
        f"{ESC}[?25l{ESC}[32m╭───────────────────────────────────╮{ESC}[0m{ESC}[?25h",
        f"{ESC}[?25l{ESC}[32m│ Question                           │{ESC}[0m{ESC}[?25h",
        f"{ESC}[?25l{ESC}[32m│ Continue?                          │{ESC}[0m{ESC}[?25h",
        f"{ESC}[?25l{ESC}[32m│                                    │{ESC}[0m{ESC}[?25h",
        f"{ESC}[?25l{ESC}[32m│ ● 1. Yes                           │{ESC}[0m{ESC}[?25h",
        f"{ESC}[?25l{ESC}[32m│ ○ 2. No                            │{ESC}[0m{ESC}[?25h",
        f"{ESC}[?25l{ESC}[32m│                                    │{ESC}[0m{ESC}[?25h",
        f"{ESC}[?25l{ESC}[32m│ ↑/↓ to navigate · enter to select │{ESC}[0m{ESC}[?25h",
        f"{ESC}[?25l{ESC}[32m╰───────────────────────────────────╯{ESC}[0m{ESC}[?25h",
    ]
    path.write_bytes(("\n".join(widget_lines) + "\n").encode("utf-8"))


@pytest.fixture(scope="session", autouse=True)
def _generate_binary_fixtures() -> None:
    """Session-scoped autouse fixture: writes fixtures that contain raw bytes.

    Called once per test session before any test runs. Safe to call on repeated
    sessions (files are only written when absent).
    """
    SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    _write_dirty_ansi_fixture()
