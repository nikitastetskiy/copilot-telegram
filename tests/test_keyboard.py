"""
@spec-handoff
@interface build_keyboard(screen: dict) -> Optional[dict]
@behavior
  - kind="idle"       → None  (no widget active)
  - kind="text_input" → None  (user replies in chat; no buttons needed)
  - kind="yes_no"     → {"inline_keyboard": [[Yes/y], [No/n]]}
  - kind="selection"  → one row per non-Other option; selected row prefixed "● ";
                        labels > 60 chars are truncated to <=60 with trailing "…"
  - kind="selection", all options are Other → None (no rows)
@edge-cases
  - options=[]                  → None
  - only "Other (…)" options   → None (all hidden)
  - multiple "Other" variants   → ALL hidden (find_all_other_options)
  - selected_index=None         → no "● " prefix on any row
@see copilot_watcher.py build_keyboard (~line 767)
@see copilot_watcher.py find_other_option (~line 725)
@see copilot_watcher.py find_all_other_options (~line 739)
"""

import pytest
from copilot_watcher import build_keyboard, find_other_option, find_all_other_options


# ===========================================================================
# build_keyboard — kind guard tests
# ===========================================================================

def test_idle_no_keyboard():
    """kind='idle' → None; no widget is active."""
    assert build_keyboard({"kind": "idle"}) is None


def test_text_input_no_keyboard():
    """kind='text_input' → None; user types in chat, no inline buttons."""
    assert build_keyboard({"kind": "text_input"}) is None


def test_unknown_kind_no_keyboard():
    """Unknown kind falls through to None — future-proof against new screen types."""
    assert build_keyboard({"kind": "something_new"}) is None


# ===========================================================================
# build_keyboard — yes_no
# ===========================================================================

def test_yes_no_keyboard_structure():
    """yes_no → exactly 2 rows: Yes (callback 'y') and No (callback 'n')."""
    kb = build_keyboard({"kind": "yes_no"})
    assert kb is not None, "expected a keyboard for yes_no screen"
    rows = kb["inline_keyboard"]
    assert len(rows) == 2, f"expected 2 rows, got {len(rows)}"
    callbacks = [row[0]["callback_data"] for row in rows]
    assert "y" in callbacks, "missing 'y' callback"
    assert "n" in callbacks, "missing 'n' callback"
    labels = {row[0]["callback_data"]: row[0]["text"] for row in rows}
    assert labels["y"] == "Yes"
    assert labels["n"] == "No"


# ===========================================================================
# build_keyboard — selection
# ===========================================================================

def test_selection_basic_three_options():
    """3 plain options → 3 rows; callback_data matches value strings."""
    screen = {
        "kind": "selection",
        "options": [("1", "Alpha"), ("2", "Beta"), ("3", "Gamma")],
        "selected_index": None,
    }
    kb = build_keyboard(screen)
    assert kb is not None
    rows = kb["inline_keyboard"]
    assert len(rows) == 3, f"expected 3 rows, got {len(rows)}"
    callbacks = [row[0]["callback_data"] for row in rows]
    assert callbacks == ["1", "2", "3"]


def test_selection_selected_marker():
    """selected_index=1 → 2nd row text starts with '● '; others do not."""
    screen = {
        "kind": "selection",
        "options": [("1", "Alpha"), ("2", "Beta"), ("3", "Gamma")],
        "selected_index": 1,
    }
    kb = build_keyboard(screen)
    rows = kb["inline_keyboard"]
    assert len(rows) == 3
    texts = [row[0]["text"] for row in rows]
    assert texts[1].startswith("● "), f"expected '● ' prefix on row 1, got '{texts[1]}'"
    assert not texts[0].startswith("● "), "row 0 must not be marked selected"
    assert not texts[2].startswith("● "), "row 2 must not be marked selected"


def test_selection_selected_index_zero_marks_first_row():
    """Shin M4 — selected_index=0 → first row text starts with '● '.

    selected_index=0 is the default initial position for Copilot CLI prompts.
    This case is distinct from selected_index=None (no cursor known): it must
    place the '● ' marker on row 0, not suppress it.
    """
    screen = {
        "kind": "selection",
        "options": [("1", "Alpha"), ("2", "Beta"), ("3", "Gamma")],
        "selected_index": 0,
    }
    kb = build_keyboard(screen)
    assert kb is not None
    rows = kb["inline_keyboard"]
    texts = [row[0]["text"] for row in rows]
    assert texts[0].startswith("● "), (
        f"selected_index=0 must mark first row with '● ', got '{texts[0]}'"
    )
    assert not texts[1].startswith("● "), "row 1 must not be marked"
    assert not texts[2].startswith("● "), "row 2 must not be marked"



def test_selection_hides_other_type_your_answer():
    """'Other (type your answer)' option is hidden from keyboard."""
    screen = {
        "kind": "selection",
        "options": [
            ("1", "Accept suggestion"),
            ("2", "Reject suggestion"),
            ("3", "Other (type your answer)"),
        ],
        "selected_index": 0,
    }
    kb = build_keyboard(screen)
    assert kb is not None, "expected keyboard with visible options"
    rows = kb["inline_keyboard"]
    assert len(rows) == 2, f"expected 2 rows (Other hidden), got {len(rows)}"
    labels = [row[0]["text"] for row in rows]
    assert all("Other" not in t for t in labels), \
        f"'Other' must not appear in any button label: {labels}"


def test_selection_hides_all_other_variants():
    """Both 'Other (type your answer)' AND 'Other (specify)' are hidden."""
    screen = {
        "kind": "selection",
        "options": [
            ("1", "Accept"),
            ("2", "Other (type your answer)"),
            ("3", "Other (specify)"),
        ],
        "selected_index": None,
    }
    kb = build_keyboard(screen)
    assert kb is not None, "expected 1 visible row"
    rows = kb["inline_keyboard"]
    assert len(rows) == 1, \
        f"expected 1 row (both Other variants hidden), got {len(rows)}"
    assert rows[0][0]["callback_data"] == "1"


def test_selection_empty_options_returns_none():
    """Empty options list → None."""
    kb = build_keyboard({"kind": "selection", "options": [], "selected_index": None})
    assert kb is None


def test_selection_only_other_returns_none():
    """If the only option is 'Other (type your answer)', keyboard is None."""
    screen = {
        "kind": "selection",
        "options": [("1", "Other (type your answer)")],
        "selected_index": None,
    }
    kb = build_keyboard(screen)
    assert kb is None, \
        "expected None when all options are hidden 'Other' entries"


def test_selection_label_truncation():
    """A label longer than 60 chars is truncated to ≤60 chars ending with '…'."""
    long_label = "A" * 100
    screen = {
        "kind": "selection",
        "options": [("1", long_label)],
        "selected_index": None,
    }
    kb = build_keyboard(screen)
    assert kb is not None
    text = kb["inline_keyboard"][0][0]["text"]
    assert len(text) <= 60, f"button text too long ({len(text)} chars): '{text}'"
    assert text.endswith("…"), f"truncated text must end with '…', got '{text[-3:]}'"


# ===========================================================================
# find_other_option helper
# ===========================================================================

def test_find_other_option_returns_last_match():
    """find_other_option returns the 0-based index of the LAST 'Other' option."""
    opts = [
        ("1", "Yes"),
        ("2", "Other (specify)"),
        ("3", "Other (type your answer)"),
    ]
    result = find_other_option(opts)
    assert result == 2, f"expected index 2 (last Other), got {result}"


def test_find_other_option_returns_none_when_absent():
    """find_other_option returns None when no 'Other' option exists."""
    opts = [("1", "Accept"), ("2", "Reject")]
    assert find_other_option(opts) is None


# ===========================================================================
# find_all_other_options helper
# ===========================================================================

def test_find_all_other_options_returns_all_indices():
    """find_all_other_options returns the set of ALL 'Other' option indices."""
    opts = [
        ("1", "Accept"),
        ("2", "Other (type your answer)"),
        ("3", "Other (specify)"),
    ]
    result = find_all_other_options(opts)
    assert result == {1, 2}, f"expected {{1, 2}}, got {result}"


def test_find_all_other_options_empty_when_none():
    """find_all_other_options returns empty set when no Other options present."""
    opts = [("1", "Yes"), ("2", "No")]
    assert find_all_other_options(opts) == set()
