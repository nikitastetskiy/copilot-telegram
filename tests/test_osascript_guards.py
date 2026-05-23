"""
@spec-handoff
@interface
  _window_bounds(as_window_id: str) -> Optional[str]
  get_window_history(as_window_id: str) -> Optional[str]
  _send_bytes(target_window_id: str, payload: str) -> bool
  _keystroke(target_window_id: str, applescript_body: str) -> bool
@behavior
  - Each function calls int(window_id) as the FIRST operation before any
    AppleScript template is constructed, so a non-integer string raises
    ValueError before osascript/subprocess is ever invoked.
  - With a valid integer window id, _send_bytes reaches subprocess.run.
@edge-cases
  - Non-integer string → ValueError (int() conversion), no subprocess fired
  - Float string ("1.5") → ValueError, no subprocess fired
  - Empty string ("") → ValueError, no subprocess fired
@see copilot_watcher.py _window_bounds:309, get_window_history:369,
     _send_bytes:410, _keystroke:430
"""

# Code inspection note: all four functions use `int(window_id)` — NOT
# isinstance() — so the concrete exception is always ValueError for any
# non-integer string.  pytest.raises((ValueError, TypeError)) is kept as the
# outer contract in case a future refactor switches to isinstance + TypeError;
# tighten to ValueError if that never happens.

import pytest
import copilot_watcher as cw


# ---------------------------------------------------------------------------
# Guard tests — no subprocess mock needed: ValueError fires before osascript
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn_name,args", [
    ("_window_bounds",     ("not_an_int",)),
    ("get_window_history", ("not_an_int",)),
    ("_send_bytes",        ("not_an_int", "payload")),
    ("_keystroke",         ("not_an_int", "key code 36")),
])
def test_osascript_fn_raises_on_non_integer_window_id(fn_name, args):
    """Non-integer window id raises ValueError before any AppleScript is built.

    int() conversion is the first statement in each function (guard comment
    at lines 309, 369, 410, 430 of copilot_watcher.py).  No subprocess is
    spawned, no osascript is invoked — the guard is purely in-process.
    """
    fn = getattr(cw, fn_name)
    with pytest.raises((ValueError, TypeError)):
        fn(*args)


@pytest.mark.parametrize("fn_name,args", [
    ("_window_bounds",     ("1.5",)),
    ("get_window_history", ("",)),
    ("_send_bytes",        ("abc123", "payload")),
    ("_keystroke",         ("not_an_int", "keystroke return")),
])
def test_osascript_fn_raises_on_non_integer_variants(fn_name, args):
    """Float strings, empty strings, and alphanumeric IDs all raise before osascript."""
    fn = getattr(cw, fn_name)
    with pytest.raises((ValueError, TypeError)):
        fn(*args)


# ---------------------------------------------------------------------------
# Positive-path test — valid int reaches subprocess.run
# ---------------------------------------------------------------------------

def test_send_bytes_with_valid_int_window_id_calls_osascript(monkeypatch):
    """With a valid integer window id, _send_bytes proceeds to subprocess.run.

    This verifies the guard does NOT block legitimate calls — only the
    non-integer injection path is rejected.
    """
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = b""
            stderr = b""
        return R()

    monkeypatch.setattr(cw.subprocess, "run", fake_run)
    cw._send_bytes(12345, "x")
    assert calls, "subprocess.run should have been called with a valid int window id"
