"""
@spec-handoff
@interface inject_response(target_window_id: str, user_response: str, selected_index: Optional[int] = None) -> None
@behavior
  - "up"   → exactly one key code 126 keystroke via _keystroke
  - "down"  → exactly one key code 125 keystroke via _keystroke
  - "enter" → exactly one key code 36  keystroke via _keystroke
  - digit n, selected_index=i:
      target_idx = n - 1  (0-based)
      delta = target_idx - selected_index
      delta == 0 → zero navigation keystrokes, then key code 36
      delta >  0 → delta × key code 125 (down), then key code 36
      delta <  0 → |delta| × key code 126 (up),  then key code 36
  - digit n, selected_index=None → legacy saturation: (MAX_OPTIONS+2) ups,
      then (n-1) downs, then key code 36  [explicit backwards-compat fallback]
  - ASCII free text  → _send_bytes(text) then _keystroke(key code 36)
  - non-ASCII free text → _keystroke(keystroke "text") then _keystroke(key code 36)
@edge-cases
  - delta == 0 → only Return keystroke, no arrows
  - selected_index=None → saturation path, not delta path
  - non-ASCII bypasses _send_bytes entirely
@see copilot_watcher.py inject_response (~line 396)
"""

import types

import pytest
import copilot_watcher as cw

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def watcher(monkeypatch):
    """Patch _keystroke and _send_bytes; return (module, calls-dict).

    calls["keystroke"] accumulates tuples (window_id, body).
    calls["send_bytes"] accumulates tuples (window_id, payload).
    """
    calls = {"keystroke": [], "send_bytes": []}

    def fake_keystroke(window_id, body):
        calls["keystroke"].append((window_id, body))
        return True

    def fake_send_bytes(window_id, payload):
        calls["send_bytes"].append((window_id, payload))
        return True

    monkeypatch.setattr(cw, "_keystroke", fake_keystroke)
    monkeypatch.setattr(cw, "_send_bytes", fake_send_bytes)
    return cw, calls


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_SENTINEL = object()

def _run(cw_mod, calls, response, selected_index=_SENTINEL):
    """Call inject_response, forwarding selected_index only when provided.

    Keeping these two call forms separate ensures that 'up'/'down'/'enter' and
    free-text tests exercise the no-selected_index code path cleanly, while
    tests that pass selected_index verify the delta-nav kwarg (RCA Bug #5 fix).
    """
    calls["keystroke"].clear()
    calls["send_bytes"].clear()
    if selected_index is _SENTINEL:
        cw_mod.inject_response("test_win", response)
    else:
        # Verifies inject_response accepts selected_index for delta-nav (RCA Bug #5 fix).
        cw_mod.inject_response("test_win", response, selected_index=selected_index)
    return list(calls["keystroke"]), list(calls["send_bytes"])


# ===========================================================================
# Basic navigation commands
# ===========================================================================

def test_up_command(watcher):
    """'up' sends exactly one key code 126 (up-arrow)."""
    cw_mod, calls = watcher
    keystrokes, _ = _run(cw_mod, calls, "up")
    assert len(keystrokes) == 1
    win_id, body = keystrokes[0]
    assert win_id == "test_win"
    assert "key code 126" in body
    assert "key code 125" not in body


def test_down_command(watcher):
    """'down' sends exactly one key code 125 (down-arrow)."""
    cw_mod, calls = watcher
    keystrokes, _ = _run(cw_mod, calls, "down")
    assert len(keystrokes) == 1
    win_id, body = keystrokes[0]
    assert win_id == "test_win"
    assert "key code 125" in body
    assert "key code 126" not in body


def test_enter_command(watcher):
    """'enter' sends exactly one key code 36 (Return)."""
    cw_mod, calls = watcher
    keystrokes, _ = _run(cw_mod, calls, "enter")
    assert len(keystrokes) == 1
    win_id, body = keystrokes[0]
    assert win_id == "test_win"
    assert "key code 36" in body
    assert "key code 125" not in body
    assert "key code 126" not in body


# ===========================================================================
# Free-text paths
# ===========================================================================

def test_free_text_ascii(watcher):
    """ASCII free text: _send_bytes called with text, then _keystroke with Return."""
    cw_mod, calls = watcher
    keystrokes, sent = _run(cw_mod, calls, "hello world")

    # _send_bytes must have received the literal text
    payloads = [payload for (_, payload) in sent]
    assert any("hello world" in p for p in payloads), \
        "_send_bytes was not called with the text payload"

    # _keystroke must have been called with Return (key code 36)
    bodies = [body for (_, body) in keystrokes]
    assert any("key code 36" in b for b in bodies), \
        "_keystroke was not called with Return after text"

    # Verify ordering: bytes come before the Return keystroke
    assert len(sent) >= 1, "expected at least one _send_bytes call"
    assert len(keystrokes) >= 1, "expected at least one _keystroke call"
    # _send_bytes must happen before the final Return
    # (calls are appended in execution order; just verifying structure here)
    assert "key code 36" in keystrokes[-1][1], \
        "Return must be the last keystroke"


def test_free_text_non_ascii(watcher):
    """Non-ASCII free text falls back to keystroke, never _send_bytes."""
    cw_mod, calls = watcher
    keystrokes, sent = _run(cw_mod, calls, "héllo")

    # _send_bytes must NOT be called — é is non-ASCII
    assert sent == [], "_send_bytes must not be called for non-ASCII text"

    bodies = [body for (_, body) in keystrokes]
    # First keystroke must carry the text (keystroke "héllo")
    assert any("héllo" in b for b in bodies), \
        "text not found in any _keystroke call"
    # Final keystroke must be Return
    assert "key code 36" in keystrokes[-1][1], \
        "Return must be the last keystroke for non-ASCII"


# ===========================================================================
# Legacy saturation fallback (no selected_index) — RCA Bug #5 backwards-compat
#
# After E5 added `selected_index: Optional[int] = None`, calling with
# selected_index=None triggers the legacy MAX_OPTIONS+2 ups path.
# ===========================================================================

def test_digit_fallback_without_selected_index(watcher):
    """Backwards-compat: selected_index=None triggers legacy saturation path (RCA Bug #5 fix).

    Callers that pass None explicitly must still receive (MAX_OPTIONS+2) up-arrows
    before Return — the saturation path guarantees the cursor reaches option 1
    regardless of current position.
    """
    cw_mod, calls = watcher
    keystrokes, _ = _run(cw_mod, calls, "1", selected_index=None)

    body = keystrokes[-1][1]  # the single keystroke body
    assert body.count("key code 126") == cw.MAX_OPTIONS + 2, \
        f"expected {cw.MAX_OPTIONS + 2} ups (saturation), got {body.count('key code 126')}"
    assert body.rstrip().endswith("key code 36"), \
        "body must end with Return"


# ===========================================================================
# Delta-navigation (Bug #5 regression fix) — RCA: selected_index param
#
# All cases call inject_response with selected_index=<int>.
# Verifies exact arrow counts for every delta scenario.
# ===========================================================================

# (n_str, selected_index, expected_downs, expected_ups, label)
# delta = (int(n) - 1) - selected_index
# delta > 0 → that many key code 125 (down)
# delta < 0 → |delta| key code 126 (up)
# delta == 0 → no arrows
_DELTA_CASES = [
    ("1", 0, 0, 0, "selected=0 press 1 → delta=0, no arrows"),
    ("1", 1, 0, 1, "selected=1 press 1 → delta=-1, 1 up"),
    ("1", 2, 0, 2, "selected=2 press 1 → delta=-2, 2 ups"),
    ("3", 0, 2, 0, "selected=0 press 3 → delta=+2, 2 downs"),
    ("2", 1, 0, 0, "selected=1 press 2 → delta=0, no arrows"),
    ("3", 2, 0, 0, "selected=2 press 3 → delta=0, no arrows"),
]


@pytest.mark.parametrize(
    "n,selected,expected_downs,expected_ups,label", _DELTA_CASES
)
def test_digit_delta_nav(watcher, n, selected, expected_downs, expected_ups, label):
    """Verifies exact arrow counts per delta (selected_index param, RCA Bug #5 fix).

    delta = (int(n) - 1) - selected_index
    delta > 0 → that many key code 125 (down-arrow) keystrokes
    delta < 0 → |delta| key code 126 (up-arrow) keystrokes
    delta == 0 → no arrow keystrokes, only Return
    """
    cw_mod, calls = watcher
    cw_mod.inject_response("test_win", n, selected_index=selected)

    body = calls["keystroke"][-1][1]
    assert body.count("key code 125") == expected_downs, \
        f"{label}: expected {expected_downs} downs, got {body.count('key code 125')}"
    assert body.count("key code 126") == expected_ups, \
        f"{label}: expected {expected_ups} ups, got {body.count('key code 126')}"
    assert body.rstrip().endswith("key code 36"), \
        f"{label}: body must end with Return (key code 36)"


# ===========================================================================
# Bug A — window-gone infinite spin (E6)
# ===========================================================================

def test_wait_for_telegram_input_exits_on_window_gone(monkeypatch):
    """wait_for_telegram_input must return None immediately when the target
    window disappears (get_window_history returns None), instead of spinning
    forever.  No Telegram network mock needed — the function returns before
    reaching get_updates when history is None.
    """
    monkeypatch.setattr(cw, "get_window_history", lambda _: None)
    # SimpleNamespace is a better sentinel than bare object(): it's inspectable
    # and its repr is more descriptive in failure output.
    result = cw.wait_for_telegram_input(types.SimpleNamespace(), "fake_win_id")
    assert result is None


# ===========================================================================
# Shin M3 — inject_response fallback path (_keystroke fails → _send_bytes)
# ===========================================================================

def test_inject_response_keystroke_failure_falls_back_to_send_bytes(monkeypatch):
    """When _keystroke returns False for a digit response, _send_bytes is
    called with the byte-sequence fallback payload.

    The fallback is a last-resort and may not work (Terminal.app drops ANSI
    escapes via do-script), but the code must attempt it so that at least raw
    bytes arrive.  For the delta case (n=3, selected_index=0 → delta=+2):
      payload = ESC[B × 2 + CR  = '\\x1b[B\\x1b[B\\r'
    """
    calls: dict[str, list] = {"keystroke": [], "send_bytes": []}

    def fake_keystroke_fail(window_id, body):
        calls["keystroke"].append((window_id, body))
        return False  # simulate AppleScript failure

    def fake_send_bytes(window_id, payload):
        calls["send_bytes"].append((window_id, payload))
        return True

    monkeypatch.setattr(cw, "_keystroke", fake_keystroke_fail)
    monkeypatch.setattr(cw, "_send_bytes", fake_send_bytes)

    # n=3, selected_index=0 → delta = (3-1) - 0 = 2 → 2 downs
    cw.inject_response("win1", "3", selected_index=0)

    # _keystroke was attempted (and failed)
    assert len(calls["keystroke"]) >= 1, "_keystroke must be tried first"

    # _send_bytes must be called with the byte-escape fallback
    assert len(calls["send_bytes"]) == 1, \
        f"expected exactly one _send_bytes call, got {len(calls['send_bytes'])}"
    _, payload = calls["send_bytes"][0]
    assert payload == "\x1b[B\x1b[B\r", (
        f"delta=+2 fallback payload must be ESC[B×2+CR, got {payload!r}"
    )
