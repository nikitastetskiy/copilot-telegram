"""
@spec-handoff
@interface inject_text_via_other(window_id: str, screen: dict, text: str) -> bool
@behavior
  - Reads options and selected_index from screen dict
  - Computes delta = find_other_option(options) - (selected_index or 0)
  - delta > 0: sends that many "key code 125" (down) keystrokes in one nav_body
  - delta < 0: sends |delta| "key code 126" (up) keystrokes in one nav_body
  - delta == 0: sends no navigation arrows
  - nav_body always ends with "key code 36" (Return) to enter Other sub-mode
  - Calls time.sleep(0.6) after navigation to let widget redraw
  - Types ASCII text via _send_bytes, non-ASCII via _keystroke keystroke "..."
  - Calls time.sleep(0.15) before final Return
  - Final Return is _keystroke(window_id, "key code 36")
  - If no Other option in options: falls back to _send_bytes(window_id, text + "\\r")
@edge-cases
  - selected_index=None treated as 0 (via `selected_index or 0`)
  - _keystroke returning False on nav step → returns False (no text typed)
@see copilot_watcher.py inject_text_via_other (~line 916)
"""

from __future__ import annotations

import time
import pytest
import copilot_watcher as cw


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def other_watcher(monkeypatch):
    """Patch _keystroke, _send_bytes, and time.sleep; return (module, calls)."""
    calls: dict[str, list] = {"keystroke": [], "send_bytes": [], "sleep": []}

    def fake_keystroke(window_id, body):
        calls["keystroke"].append((window_id, body))
        return True

    def fake_send_bytes(window_id, payload):
        calls["send_bytes"].append((window_id, payload))
        return True

    def fake_sleep(seconds):
        calls["sleep"].append(seconds)

    monkeypatch.setattr(cw, "_keystroke", fake_keystroke)
    monkeypatch.setattr(cw, "_send_bytes", fake_send_bytes)
    monkeypatch.setattr(time, "sleep", fake_sleep)
    return cw, calls


# ---------------------------------------------------------------------------
# H1-1  delta > 0: other_idx=2, selected_index=0 → 2 downs then Return
# ---------------------------------------------------------------------------

def test_inject_other_delta_down(other_watcher):
    """other_idx=2, selected=0 → nav_body has 2 'key code 125' (down) + Return."""
    mod, calls = other_watcher
    options = [("1", "A"), ("2", "B"), ("3", "Other (type your answer)")]
    screen = {"options": options, "selected_index": 0}

    result = mod.inject_text_via_other("win1", screen, "hello")

    assert result is True

    # First keystroke call: navigation block
    nav_body = calls["keystroke"][0][1]
    assert calls["keystroke"][0][0] == "win1"
    assert nav_body.count("key code 125") == 2, \
        f"expected 2 downs in nav_body, got {nav_body.count('key code 125')}"
    assert "key code 126" not in nav_body, "no ups expected for delta=+2"
    assert "key code 36" in nav_body, "nav_body must end with Return to enter Other"

    # Text should be sent via _send_bytes for ASCII
    assert calls["send_bytes"] == [("win1", "hello")], \
        "ASCII text must go through _send_bytes"

    # Final keystroke: bare Return to submit
    final_ks = calls["keystroke"][-1]
    assert final_ks[1] == "key code 36", "last keystroke must be bare Return"

    # time.sleep called twice: 0.6 (sub-mode settle) then 0.15 (before final Return)
    assert calls["sleep"] == [0.6, 0.15], \
        f"expected sleeps [0.6, 0.15], got {calls['sleep']}"


# ---------------------------------------------------------------------------
# H1-2  delta < 0: other_idx=1, selected_index=2 → 1 up then Return
# ---------------------------------------------------------------------------

def test_inject_other_delta_up(other_watcher):
    """other_idx=1, selected=2 → nav_body has 1 'key code 126' (up) + Return."""
    mod, calls = other_watcher
    options = [("1", "A"), ("2", "Other (type your answer)"), ("3", "B")]
    screen = {"options": options, "selected_index": 2}

    result = mod.inject_text_via_other("win1", screen, "reply")

    assert result is True

    nav_body = calls["keystroke"][0][1]
    assert nav_body.count("key code 126") == 1, \
        f"expected 1 up in nav_body, got {nav_body.count('key code 126')}"
    assert "key code 125" not in nav_body, "no downs expected for delta=-1"
    assert "key code 36" in nav_body


# ---------------------------------------------------------------------------
# H1-3  delta == 0: other_idx=1, selected_index=1 → no arrows, just Return
# ---------------------------------------------------------------------------

def test_inject_other_same_position(other_watcher):
    """other_idx=1, selected=1 → nav_body has no arrows, only Return."""
    mod, calls = other_watcher
    options = [("1", "A"), ("2", "Other (type your answer)")]
    screen = {"options": options, "selected_index": 1}

    result = mod.inject_text_via_other("win1", screen, "same")

    assert result is True

    nav_body = calls["keystroke"][0][1]
    assert "key code 125" not in nav_body, "no down arrows for delta=0"
    assert "key code 126" not in nav_body, "no up arrows for delta=0"
    assert "key code 36" in nav_body, "nav_body must still contain Return"


# ---------------------------------------------------------------------------
# H1-4  selected_index=None treated as 0
# ---------------------------------------------------------------------------

def test_inject_other_selected_none_uses_zero(other_watcher):
    """selected_index=None → treats as 0; other_idx=2 → delta=2 → 2 downs."""
    mod, calls = other_watcher
    options = [("1", "A"), ("2", "B"), ("3", "Other (type your answer)")]
    screen = {"options": options, "selected_index": None}

    result = mod.inject_text_via_other("win1", screen, "text")

    assert result is True

    nav_body = calls["keystroke"][0][1]
    assert nav_body.count("key code 125") == 2, \
        f"None selected_index must be treated as 0; expected 2 downs, got {nav_body.count('key code 125')}"
    assert "key code 126" not in nav_body


# ---------------------------------------------------------------------------
# H1-5  No Other option → falls back to _send_bytes with text + "\r"
# ---------------------------------------------------------------------------

def test_inject_other_no_other_in_options_falls_back_to_send_bytes(other_watcher):
    """Options with no Other entry → immediate _send_bytes fallback with 'text\\r'."""
    mod, calls = other_watcher
    options = [("1", "A"), ("2", "B")]
    screen = {"options": options, "selected_index": 0}

    result = mod.inject_text_via_other("win1", screen, "hello")

    assert result is True
    # _keystroke must NOT be called — function returns before navigation
    assert calls["keystroke"] == [], \
        "_keystroke must not be called when no Other option exists"
    # _send_bytes must be called with text + "\r"
    assert calls["send_bytes"] == [("win1", "hello\r")], \
        f"expected _send_bytes with 'hello\\r', got {calls['send_bytes']}"
