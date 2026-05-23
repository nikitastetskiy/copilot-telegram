"""
@spec-handoff
@interface parse_screen(history: str) -> dict
@behavior
  - Returns {"kind": "idle"} when no widget pipes exist in the active bottom region
  - Returns {"kind": "selection", "options": list[tuple[str,str]], "selected_index": int|None,
             "question": str|None} when a Copilot CLI selection widget is detected
  - Returns {"kind": "text_input", "question": str|None} when an ask_user text widget
    with "to submit" footer is detected inside a box border
  - Returns {"kind": "yes_no", "question": str|None} when a [y/N] / [Y/n] prompt is detected
  - selected_index is 0-based; None when no indicator (●/❯) is found
  - options is a list of ("1", "label"), ("2", "label"), ... tuples (1-based value strings)
  - Strips real ANSI escape sequences (SGR, DEC private mode, OSC) before parsing
@edge-cases
  - DEC private mode sequences (\x1b[?25l/h) must be stripped — Bug #2 (fixed in E4)
  - [y/N] inside a multi-column table cell must NOT trigger yes_no — Bug #3 (fixed in E6)
  - ASCII box borders (+---+) with bilateral | pipes still detect selection — Bug B4
  - Narrative prose mentioning "to submit" / "to navigate" without a real box stays idle
@see copilot_watcher.py:parse_screen (~line 552)
"""

import pytest

from tests.conftest import load_fixture
from copilot_watcher import parse_screen, _strip_box, _is_box_border


# ── Helper ─────────────────────────────────────────────────────────────────────

def _parse(fixture_name: str) -> dict:
    """Load a fixture and run parse_screen on it. No live I/O."""
    return parse_screen(load_fixture(fixture_name))


# ── Parametrized matrix: kind + selected_index for every fixture ───────────────
#
# This is the primary coverage gate: every fixture file must be classified
# correctly by parse_screen.  Rows that depend on un-landed bug fixes are
# still written as straight assertions so they fail red until the fix ships.
#
# Expected failures (red phase):
#   • false_positive_yn_in_table.txt  → Bug #3 (E6 will fix)
#   • dirty_ansi_with_dec_private.txt → Bug #2 (E4 will fix)

@pytest.mark.parametrize("fixture_name, expected_kind, expected_selected", [
    # ── Idle: shell prompt with no pipes ───────────────────────────────────────
    ("idle_shell_prompt.txt",          "idle",      None),
    # ── Idle: agent narrative prose — no pipes → quick-reject ─────────────────
    ("idle_agent_narrative.txt",       "idle",      None),
    # ── Idle: narrative rendered inside │ pipes but no box border ─────────────
    ("idle_narrative_in_box.txt",      "idle",      None),
    # ── Idle: table with pipe characters + widget-like keywords, no widget ─────
    ("false_positive_narrative.txt",   "idle",      None),
    # ── Regression: Bug #3 — [y/N] in table cell must not trigger yes_no (RCA fix) ─
    # Fixed in E6: bilateral-pipe guard added to the yes_no branch.
    # Pre-fix parser returned kind="yes_no" for any line matching [y/N].
    ("false_positive_yn_in_table.txt", "idle",      None),
    # ── Selection: Style A (●/○ dots), three options ───────────────────────────
    ("selection_dots_3opts_default0.txt", "selection", 0),
    ("selection_dots_3opts_default1.txt", "selection", 1),  # Bug #5 scenario
    ("selection_dots_3opts_default2.txt", "selection", 2),
    # ── Selection: Style B (❯ numbered), five options ─────────────────────────
    ("selection_numbered_5opts.txt",   "selection", 2),
    # ── Selection: Style A + "Other" option ───────────────────────────────────
    ("selection_with_other.txt",       "selection", 0),
    # ── Selection: ASCII +---+ border (BOX_BORDER_RE ASCII extension, landed E8) ──
    ("selection_ascii_border.txt",     "selection", 0),
    # ── Text input ────────────────────────────────────────────────────────────
    ("text_input.txt",                 "text_input", None),
    # ── Yes/No: real bilateral-pipe [y/N] prompt ──────────────────────────────
    ("yes_no.txt",                     "yes_no",    None),
    # ── Regression: Bug #2 — DEC private mode ANSI must be stripped (RCA fix) ────
    # Fixed in E4: ANSI_RE expanded to cover DEC private mode sequences (\x1b[?...).
    # Pre-fix: \x1b[?25l left "[?25l" at line start, breaking bilateral pipe guard.
    ("dirty_ansi_with_dec_private.txt", "selection", 0),
])
def test_parse_screen_kind_and_selected(fixture_name, expected_kind, expected_selected):
    screen = _parse(fixture_name)
    assert screen["kind"] == expected_kind, (
        f"{fixture_name}: expected kind={expected_kind!r}, got {screen['kind']!r}"
    )
    if expected_selected is not None:
        assert screen.get("selected_index") == expected_selected, (
            f"{fixture_name}: expected selected_index={expected_selected}, "
            f"got {screen.get('selected_index')!r}"
        )


# ── Option count and label spot-checks ────────────────────────────────────────

def test_selection_3opts_option_count():
    """Selection with 3 options returns exactly 3 (value, label) tuples."""
    screen = _parse("selection_dots_3opts_default0.txt")
    opts = screen["options"]
    assert len(opts) == 3
    labels = [label for _, label in opts]
    assert "Edit and run again" in labels


def test_selection_dots_labels():
    """Label ordering and content match the fixture for the Bug #5 scenario.

    selection_dots_3opts_default1 has selected_index=1 ("Discard changes").
    The old saturating-14-ups navigation would have sent the wrong delta;
    this test pins the parse output that feeds the delta calculation.
    """
    screen = _parse("selection_dots_3opts_default1.txt")
    labels = [label for _, label in screen["options"]]
    assert labels == ["Edit and run again", "Discard changes", "Open in editor"]
    assert screen["selected_index"] == 1, (
        "selected_index must be 1 (Discard changes) for the Bug #5 delta-nav fixture"
    )


def test_selection_numbered_5opts_count():
    """Numbered style with 5 options returns exactly 5."""
    screen = _parse("selection_numbered_5opts.txt")
    assert len(screen["options"]) == 5


def test_selection_numbered_5opts_labels():
    """All 5 option labels from the numbered fixture are parsed correctly."""
    screen = _parse("selection_numbered_5opts.txt")
    labels = [label for _, label in screen["options"]]
    assert labels == [
        "Run all tests",
        "Run failing tests only",
        "Run with coverage",
        "Open test report",
        "Exit",
    ]


def test_selection_with_other_option_present():
    """The 'Other (type your answer)' option is present and not dropped at parse level.

    Hiding Other from the Telegram keyboard is build_keyboard's responsibility,
    not parse_screen's.  parse_screen must preserve it.
    """
    screen = _parse("selection_with_other.txt")
    labels = [label for _, label in screen["options"]]
    assert any("Other" in l for l in labels), (
        "Expected at least one option with 'Other' in label"
    )
    assert len(labels) == 3


def test_selection_ascii_border_detected():
    """ASCII +---+ border with | pipes: classified as selection, 3 options.

    BOX_BORDER_RE includes +, -, = characters (landed in E8), so has_box=True.
    INDICATOR_RE (●/○) also fires and bilateral | pipe guard passes on the footer.
    Both code paths yield kind=selection with 3 options.
    """
    # Regression test for Bug B4 (BOX_BORDER_RE ASCII extension, landed E8).
    screen = _parse("selection_ascii_border.txt")
    assert screen["kind"] == "selection"
    assert len(screen["options"]) == 3


def test_selection_ascii_border_option_labels():
    """ASCII-border fixture option labels parsed correctly."""
    screen = _parse("selection_ascii_border.txt")
    labels = [label for _, label in screen["options"]]
    assert labels == ["Option Alpha", "Option Beta", "Option Gamma"]


# ── Text input ────────────────────────────────────────────────────────────────

def test_text_input_detected():
    """text_input.txt classifies as kind='text_input'."""
    screen = _parse("text_input.txt")
    assert screen["kind"] == "text_input"


def test_text_input_question_nonempty():
    """parse_screen extracts a non-empty question for text_input prompts."""
    screen = _parse("text_input.txt")
    q = screen.get("question")
    assert q is not None and len(str(q).strip()) > 0, (
        f"Expected non-empty question, got {q!r}"
    )


# ── Yes/No ────────────────────────────────────────────────────────────────────

def test_yes_no_detected():
    """yes_no.txt classifies as kind='yes_no' with a non-None question."""
    screen = _parse("yes_no.txt")
    assert screen["kind"] == "yes_no"
    assert screen.get("question") is not None


def test_yes_no_question_contains_yn_pattern():
    """The extracted yes_no question text contains the [y/N] / [Y/n] pattern."""
    screen = _parse("yes_no.txt")
    q = screen.get("question", "")
    has_yn = any(pat in q for pat in ("[y/N]", "[Y/n]", "[y/n]", "[Y/N]"))
    assert has_yn, (
        f"Expected [y/N]-style pattern in yes_no question, got: {q!r}"
    )


# ── Regression: Bug #3 — [y/N] bilateral pipe guard (fixed in E6) ─────────────

def test_false_positive_yn_in_table_stays_idle():
    """[y/N] inside a multi-column table cell must NOT trigger yes_no.

    Regression test for Bug #3 (Y/N bilateral gate, fixed in E6).

    The fixture │ Prompt │ Default [y/N] │ Notes │ is a three-column markdown
    table row.  The pre-fix parser had no bilateral-pipe guard on the yes_no
    branch, so YN_RE matched [y/N] in the stripped line and returned
    kind="yes_no" — a false positive.  The E6 bilateral-pipe guard prevents
    this by requiring a single-column │...│ structure for yes_no detection.
    """
    screen = _parse("false_positive_yn_in_table.txt")
    assert screen["kind"] == "idle", (
        f"Bug #3: [y/N] inside a table cell must not trigger yes_no; "
        f"got kind={screen['kind']!r}"
    )


# ── Regression: Bug #2 — DEC private mode ANSI expansion (fixed in E4) ────────

def test_dirty_ansi_with_dec_private_is_selection():
    """DEC private-mode ANSI sequences must be stripped before parsing.

    Regression test for Bug #2 (ANSI_RE expansion, fixed in E4).

    The fixture wraps every Ink widget line with ESC[?25l ... ESC[?25h
    (cursor-hide/show).  The pre-fix ANSI_RE pattern \\x1b\\[[0-9;]*[mKH...]
    did not handle the '?' prefix of private-mode CSI sequences, so they
    survived _clean_ansi.  CTRL_RE then replaced the bare \\x1b with a space,
    leaving "[?25l" at the start of every cleaned line.  The bilateral pipe
    guard on the footer then failed (line starts with '[', not '│') and
    parse_screen fell through to kind="idle".

    E4 expanded ANSI_RE to cover DEC private modes; the fixture now parses
    correctly as kind="selection" with 2 options and selected_index=0.
    """
    screen = _parse("dirty_ansi_with_dec_private.txt")
    assert screen["kind"] == "selection", (
        f"Bug #2: dirty ANSI fixture must parse as 'selection', got {screen['kind']!r}"
    )
    assert screen.get("selected_index") == 0, (
        f"Bug #2: expected selected_index=0, got {screen.get('selected_index')!r}"
    )
    assert len(screen.get("options", [])) == 2, (
        f"Bug #2: expected 2 options (Yes/No), got {screen.get('options')!r}"
    )


# ── Shin L1: direct _is_box_border and _strip_box unit tests ─────────────────


def test_is_box_border_unicode_corners():
    """╭──────╮ is a box border (Unicode box-drawing corners + rule)."""
    assert _is_box_border("╭──────╮") is True


def test_is_box_border_plain_text():
    """Plain text like 'hello' is not a box border."""
    assert _is_box_border("hello") is False


def test_is_box_border_ascii_plus_dashes():
    """+-----+ is a box border — BOX_BORDER_RE includes + and - (E8 landed)."""
    assert _is_box_border("+-----+") is True


def test_is_box_border_horizontal_rule():
    """A bare ───── separator line is a box border."""
    assert _is_box_border("─────") is True


def test_strip_box_removes_leading_and_trailing_pipe():
    """'│ x │' strips both box pipes, leaving the inner content."""
    # Leading │ and following space are removed; trailing │ and preceding space too.
    result = _strip_box("│ x │")
    assert "│" not in result, f"box pipe must be stripped, got {result!r}"
    assert "x" in result, f"inner content 'x' must survive, got {result!r}"


def test_strip_box_leaves_non_pipe_border_lines_unchanged():
    """'╭─╮' and '╰─╯' have no │ prefix/suffix: _strip_box returns them as-is.

    _strip_box only removes leading/trailing │/|/┃.  Detecting box-border
    lines as skippable is _is_box_border's responsibility, not _strip_box's.
    """
    assert _strip_box("╭─╮") == "╭─╮", \
        "_strip_box must not alter lines without leading/trailing │"
    assert _strip_box("╰─╯") == "╰─╯", \
        "_strip_box must not alter lines without leading/trailing │"


def test_strip_box_ascii_pipe():
    """'| hello |' → strips ASCII pipes, leaving inner content."""
    result = _strip_box("| hello |")
    assert "|" not in result, f"ASCII pipe must be stripped, got {result!r}"
    assert "hello" in result, f"'hello' must survive, got {result!r}"
