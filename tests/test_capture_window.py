"""E7 — verify WatchTarget caching: capture_window uses pre-resolved cg_id
and never re-queries the title."""
import subprocess
from unittest.mock import patch

import copilot_watcher as cw


def test_capture_window_uses_cached_cg_id(monkeypatch, tmp_path):
    """When cg_id is set, capture_window must call `screencapture -l{cg_id}`
    and must NOT call `_quartz_window_id` (no live title resolution)."""
    target = cw.WatchTarget(as_window_id="123", cg_id=42, window_name="anything")
    out_path = str(tmp_path / "screenshot.png")

    # Track invocations
    quartz_calls = []
    monkeypatch.setattr(cw, "_quartz_window_id",
                        lambda name: quartz_calls.append(name) or 999)

    captured_args = {}
    def fake_run(args, **kw):
        captured_args["args"] = args
        # touch the file so capture_window sees it exists
        with open(args[-1], "wb") as f:
            f.write(b"\x89PNG")
        class R: returncode = 0
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)

    ok = cw.capture_window(target, out_path)
    assert ok is True
    assert "-l42" in captured_args["args"]
    assert quartz_calls == []  # no live title resolution


def test_capture_window_falls_back_to_bounds_when_cg_id_none(monkeypatch, tmp_path):
    target = cw.WatchTarget(as_window_id="123", cg_id=None, window_name="x")
    out_path = str(tmp_path / "shot.png")

    monkeypatch.setattr(cw, "_window_bounds", lambda wid: "0,0,800,600")

    captured = {}
    def fake_run(args, **kw):
        captured["args"] = args
        with open(args[-1], "wb") as f:
            f.write(b"\x89PNG")
        class R: returncode = 0
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert cw.capture_window(target, out_path) is True
    assert "-R0,0,800,600" in captured["args"]
