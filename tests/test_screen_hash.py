"""
@spec-handoff
@interface screen_hash(screen: dict, window_id: str) -> str
@behavior
  - Returns an MD5 hex string
  - Consumes: window_id, screen["kind"], screen["question"], screen["options"]
  - Does NOT consume selected_index: user navigating the menu must not
    produce a new hash (avoids re-sending the same prompt)
  - Two screens identical except for selected_index hash to the same value
  - Two screens differing in options hash to different values
@edge-cases
  - options=None / missing → treated as empty sequence
  - question=None / missing → treated as ""
@see copilot_watcher.py screen_hash (~line 809)
"""

from __future__ import annotations

from copilot_watcher import screen_hash


def test_screen_hash_excludes_selected_index():
    """Two screens identical except selected_index must produce the same hash.

    Copilot CLI re-renders every keypress with a new selected_index.
    Treating that as a new prompt would re-send the Telegram message on every
    arrow key — screen_hash intentionally omits selected_index.
    """
    s1 = {
        "kind": "selection",
        "options": [("1", "A"), ("2", "B")],
        "selected_index": 0,
        "title": "T",
        "body": "B",
    }
    s2 = {
        "kind": "selection",
        "options": [("1", "A"), ("2", "B")],
        "selected_index": 2,
        "title": "T",
        "body": "B",
    }
    assert screen_hash(s1, "win1") == screen_hash(s2, "win1"), (
        "screen_hash must be identical when only selected_index differs"
    )


def test_screen_hash_differs_on_options_change():
    """Two screens with different option labels must produce different hashes."""
    s1 = {
        "kind": "selection",
        "options": [("1", "A")],
        "selected_index": None,
        "title": "T",
        "body": "B",
    }
    s2 = {
        "kind": "selection",
        "options": [("1", "B")],
        "selected_index": None,
        "title": "T",
        "body": "B",
    }
    assert screen_hash(s1, "win1") != screen_hash(s2, "win1"), (
        "screen_hash must differ when option labels differ"
    )
