"""
@spec-handoff
@interface main(argv: list[str]) -> int
@behavior
  - --version / -V → prints "copilot_watcher <ver>" to stdout, returns 0
  - --help / -h    → prints usage, returns 0
  - --dump <path>  → does NOT require BOT_TOKEN/CHAT_ID, proceeds to window
                     selection then writes cleaned history to <path>, returns 0
                     (Bug B1 fix: credential gate bypassed for --dump)
  - no args, no credentials → credential gate fires, returns 2
@edge-cases
  - --version works even when BOT_TOKEN / CHAT_ID are unset
  - --dump bypasses credential gate entirely (Bug B1 fix)
@see copilot_watcher.py main (~line 1349)
"""

from __future__ import annotations

import sys
import time
import types

import pytest
import copilot_watcher as cw


# ---------------------------------------------------------------------------
# F2-1  --version exits cleanly without credentials
# ---------------------------------------------------------------------------

def test_version_flag_exits_clean(monkeypatch, capsys):
    """--version returns 0 and prints the version string, regardless of creds."""
    monkeypatch.setattr(cw, "BOT_TOKEN", "")
    monkeypatch.setattr(cw, "CHAT_ID", "")

    rc = cw.main(["copilot_watcher.py", "--version"])

    assert rc == 0, f"--version must return 0, got {rc}"
    out = capsys.readouterr().out
    assert "copilot_watcher" in out, f"version string missing from stdout: {out!r}"
    assert cw.__version__ in out, f"version number missing from stdout: {out!r}"


# ---------------------------------------------------------------------------
# F2-2  --dump without creds proceeds past credential gate (RED until Kou fix)
# ---------------------------------------------------------------------------

def test_dump_without_creds_proceeds_past_gate(monkeypatch, tmp_path):
    """--dump must NOT return 2 (cred error) even when BOT_TOKEN/CHAT_ID unset.

    Regression: --dump must not be blocked by missing BOT_TOKEN/CHAT_ID (Bug B1 fix).
    --dump bypasses the credential check entirely (only non-dump mode needs Telegram creds).
    """
    monkeypatch.setattr(cw, "BOT_TOKEN", "")
    monkeypatch.setattr(cw, "CHAT_ID", "")
    monkeypatch.setattr(cw, "TARGET_WINDOW_NAME", "fake-window")
    monkeypatch.setattr(time, "sleep", lambda _: None)

    # Provide a fake window list so pick_from_arg succeeds without live Terminal
    monkeypatch.setattr(
        cw, "list_terminal_windows",
        lambda: [("99", "fake-window")]
    )
    # get_window_history returns None → main returns 1 (window-gone error),
    # which is the "reached window-interaction code" proof.
    monkeypatch.setattr(cw, "get_window_history", lambda _: None)

    dump_out = str(tmp_path / "out.txt")
    rc = cw.main(["copilot_watcher.py", "--dump", dump_out])

    assert rc != 2, (
        "--dump with no creds must not return 2 (credential gate error). "
        f"Got rc={rc}. (Regression: Bug B1 fix — credential gate must not block --dump.)"
    )


# ---------------------------------------------------------------------------
# F2-3  No args + no creds → credential gate returns 2
# ---------------------------------------------------------------------------

def test_no_args_no_creds_returns_2(monkeypatch, capsys):
    """With no --dump and no credentials, main() must return 2."""
    monkeypatch.setattr(cw, "BOT_TOKEN", "")
    monkeypatch.setattr(cw, "CHAT_ID", "")
    # Ensure TARGET_WINDOW_NAME doesn't accidentally drive window selection
    monkeypatch.setattr(cw, "TARGET_WINDOW_NAME", "")

    rc = cw.main(["copilot_watcher.py"])

    assert rc == 2, (
        "Without credentials and without --dump, main() must return 2. "
        f"Got rc={rc}."
    )
    err = capsys.readouterr().err
    assert "BOT_TOKEN" in err or "CHAT_ID" in err, (
        f"Stderr must mention the missing credentials, got: {err!r}"
    )


# ---------------------------------------------------------------------------
# L-NEW-1  SCREENSHOT_PATH env override
# ---------------------------------------------------------------------------

def test_screenshot_path_env_override(monkeypatch, tmp_path):
    """SCREENSHOT_PATH env var overrides the default ~/.copilot_prompt.png path.

    The module reads SCREENSHOT_PATH at import time via os.environ.get(), so we
    must reload the module after setting the env var.  A try/finally reload
    restores the module's original path, preventing test pollution.
    """
    import importlib
    import copilot_watcher

    custom = str(tmp_path / "custom.png")
    monkeypatch.setenv("SCREENSHOT_PATH", custom)
    try:
        importlib.reload(copilot_watcher)
        assert copilot_watcher.SCREENSHOT_PATH == custom, (
            f"Expected SCREENSHOT_PATH={custom!r}, got {copilot_watcher.SCREENSHOT_PATH!r}"
        )
    finally:
        # Remove the env override and reload to restore the default, so
        # subsequent tests that import copilot_watcher get the original value.
        monkeypatch.delenv("SCREENSHOT_PATH", raising=False)
        importlib.reload(copilot_watcher)
