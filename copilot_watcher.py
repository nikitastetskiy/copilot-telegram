"""
copilot_watcher.py — Watch a Terminal.app window running GitHub Copilot CLI
and forward its interactive prompts to Telegram with labeled inline buttons.

Design notes (why some things look the way they do):

* **Bytes go to a specific window, not whatever is focused.** Responses are
  injected via Terminal.app's native `do script ... in window id N without
  entering`, which writes raw bytes to the tty of *that* window. This avoids
  the historical bug where keystrokes sent via System Events would land in
  whichever Terminal window happened to be frontmost.

* **No clipboard pollution.** We never call `pbcopy`. Every byte (including
  escape sequences for arrow keys) is built from `(ASCII character N)` in
  AppleScript, so special characters survive without quoting hacks.

* **Configuration via environment.** Secrets are never hardcoded. If a `.env`
  file exists alongside this script we load it; otherwise we fall back to
  shell environment variables. Required vars are validated at startup.

Required env:
    BOT_TOKEN              Telegram bot token from @BotFather
    CHAT_ID                Your Telegram chat (user) id

Optional env:
    TARGET_WINDOW_NAME     Default window-name filter for the picker
    SCREENSHOT_PATH        Where to write the prompt screenshot
                           (default: ~/.copilot_prompt.png)
    LOG_LEVEL              DEBUG / INFO / WARNING (default: INFO)
"""

from __future__ import annotations

__version__ = "0.1.0"

import hashlib
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests


# ── WatchTarget ───────────────────────────────────────────────────────────────

@dataclass
class WatchTarget:
    """Cached identifiers for the Terminal window we're watching.

    Captured once at startup (in ``monitor_screen``) so screenshot capture and
    keystroke injection never re-resolve from a title that may have changed
    while Copilot CLI runs.  ``cg_id`` is None when Quartz isn't available;
    the capture path then falls back to AppleScript bounds.
    """
    as_window_id: str        # AppleScript ``window id N``
    cg_id: Optional[int]     # CoreGraphics window number for ``screencapture -l``
    window_name: str         # original title at startup (diagnostic only)


# ── Config ────────────────────────────────────────────────────────────────────

def _load_dotenv(path: Path) -> None:
    """Tiny `.env` loader so we don't pull in python-dotenv as a dependency."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        # shell env wins over .env so users can override per-invocation
        os.environ.setdefault(key, value)


_load_dotenv(Path(__file__).resolve().parent / ".env")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
TARGET_WINDOW_NAME = os.environ.get("TARGET_WINDOW_NAME", "").strip()
SCREENSHOT_PATH = os.environ.get("SCREENSHOT_PATH", os.path.expanduser("~/.copilot_prompt.png"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("copilot_watcher")


# ── Constants ─────────────────────────────────────────────────────────────────

# Selection trigger → cooldown so a single prompt isn't re-sent every loop.
COOLDOWN_SECONDS = 20
POLL_INTERVAL_SECONDS = 2
POLL_INTERVAL_IDLE_SECONDS = 5     # bumped interval after sustained idleness
IDLE_CYCLES_BEFORE_SLOWDOWN = 10   # ~20s of consecutive idle before slowing poll
HISTORY_TAIL_LINES = 80            # AppleScript fetches this many trailing lines
TAIL_SCAN_LINES = 50               # how many trailing lines parse_screen inspects
ACTIVE_WIDGET_LINES = 20           # bottom-of-screen window where a "live" widget lives
SETTLE_DELAY_SECONDS = 6          # let terminal redraw after we inject (longer so user can read output)
TG_TEXT_MAX = 180                 # truncate long strings shown in Telegram captions
MAX_OPTIONS = 12
KEY_DELAY = 0.04  # 40 ms keystroke pacing — see Sui RCA Bug #4
UNPARSED_DUMP_DIR = os.path.expanduser("~")  # where unparsed-but-suspect screens are dumped
UNPARSED_DUMP_COOLDOWN = 60       # don't dump the same screen twice within this window

# Comprehensive ANSI/VT escape-sequence matcher.
# DEC private modes (?25l, ?2004h), OSC title sequences, and 2-char escapes
# must be stripped or Ink's output corrupts the bilateral-pipe widget guard.
ANSI_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-9;?!>]*[A-Za-z]"                    # CSI: standard + private (?25l, ?2004h, >4m…)
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"          # OSC: ESC ] … BEL or ST
    r"|[PX\^_][^\x1b]*\x1b\\"                  # DCS / SOS / PM / APC
    r"|[0-9A-Za-z=><]"                         # 2-char: ESC= ESC> ESC( etc.
    r")"
)
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Box-drawing characters Copilot CLI uses to frame interactive widgets.
# Stripping them lets us treat boxed content as plain lines.
BOX_CHARS_RE = re.compile(r"[│|┃┆┊╎╽╿║]")
BOX_BORDER_RE = re.compile(r"^[\s╭╮╰╯─━═┄┅┈┉╌╍+\-=]+$")

# Copilot CLI uses several <SelectInput> styles. We recognize them all:
#
# Style A (permission prompts, model picker, from the Ink bundle):
#   ●  U+25CF  filled circle, highlighted item
#   ○  U+25CB  empty circle,  non-highlighted item
# Style B (boxed `ask_user`-style menu — selected line only):
#   ❯  U+276F  arrow, ONLY on the currently highlighted line; others are blank
#   >          ASCII fallback some terminals downgrade to
# Style C (plain numbered options, no per-line indicator):
#   "  1. label", "  2. label", … — selection signalled by other means (color)
INDICATOR_RE = re.compile(r"^[ \t]*([●○❯>])[ \t]+(\S.*)$")
# Numbered options optionally prefixed by ❯ or >. Captures (marker, n, label).
NUMBERED_OPT_RE = re.compile(r"^[ \t]*([❯>])?[ \t]*(\d+)[.\)][ \t]+(\S.*?)[ \t]*$")

# Hints that the CLI prints below every interactive component.
# Footer must include the "↑/↓" navigation hint (or its ASCII equivalent
# rendered by terminals that downgrade Unicode arrows), or an explicit
# "enter to select/confirm" cue. This rejects agent prose that mentions
# widget-like phrases without the actual Ink-rendered widget arrows.
FOOTER_SELECT_RE = re.compile(
    r"(?:↑\s*/\s*↓|up\s*/\s*down)\s+to\s+(?:navigate|move)"
    r"|enter\s+to\s+(?:select|confirm|go\s+back)",
    re.IGNORECASE,
)
# Requires "enter" or "↵" prefix so free prose like "press Return to submit"
# doesn't accidentally match the text-input footer detector.
FOOTER_TEXT_RE = re.compile(
    r"enter\s+to\s+submit|↵\s+to\s+submit",
    re.IGNORECASE,
)
YN_RE = re.compile(r"[\[(](?:y/n|y/N|Y/n)[\])]", re.IGNORECASE)


# ── Generic AppleScript helper ────────────────────────────────────────────────

def osascript(script: str, timeout: float = 8.0) -> tuple[bool, str, str]:
    """Run AppleScript. Returns (ok, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", f"osascript timed out after {timeout}s"
    except Exception as e:
        return False, "", f"osascript failed: {e!r}"


# ── Telegram client (single Session, retries, single place to log errors) ─────

class Telegram:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self.base = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()

    def send_message(self, text: str) -> None:
        try:
            r = self.session.post(
                f"{self.base}/sendMessage",
                data={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=15,
            )
            if r.status_code != 200:
                log.warning("sendMessage %s: %s", r.status_code, r.text)
        except Exception as e:
            log.warning("sendMessage failed: %s", e)

    def send_photo(self, image_path: str, caption: str, keyboard: Optional[dict]) -> None:
        if not os.path.exists(image_path):
            log.warning("Screenshot not found at %s — sending text only", image_path)
            self.send_message(f"<b>Warning</b> — could not capture screenshot.\n\n{caption}")
            return
        data: dict[str, str] = {
            "chat_id": self.chat_id,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if keyboard:
            import json
            data["reply_markup"] = json.dumps(keyboard)
        try:
            with open(image_path, "rb") as fh:
                r = self.session.post(
                    f"{self.base}/sendPhoto",
                    files={"photo": ("screenshot.png", fh, "image/png")},
                    data=data,
                    timeout=30,
                )
            if r.status_code != 200:
                log.warning("sendPhoto %s: %s", r.status_code, r.text)
                self.send_message(
                    f"<b>Warning</b> — photo upload failed ({r.status_code}).\n\n{caption}"
                )
        except Exception as e:
            log.warning("sendPhoto failed: %s", e)
            self.send_message(f"<b>Error</b> sending photo: <code>{e}</code>\n\n{caption}")

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self.session.post(
                f"{self.base}/answerCallbackQuery",
                data={"callback_query_id": callback_id, "text": text},
                timeout=5,
            )
        except Exception:
            pass

    def get_updates(self, offset: int, timeout: int = 5) -> list[dict]:
        try:
            r = self.session.get(
                f"{self.base}/getUpdates",
                params={"offset": offset, "timeout": timeout},
                timeout=timeout + 3,
            )
            return r.json().get("result", []) or []
        except Exception as e:
            log.debug("getUpdates failed: %s", e)
            return []

    def latest_update_id(self) -> int:
        try:
            r = self.session.get(f"{self.base}/getUpdates", timeout=15).json()
            results = r.get("result") or []
            return results[-1]["update_id"] if results else 0
        except Exception:
            return 0


# ── Terminal window helpers ───────────────────────────────────────────────────

def _quartz_window_id(window_name: str) -> Optional[int]:
    """Look up the CoreGraphics window id for `screencapture -l`.

    Prefers importing Quartz directly so we don't pay the cost of spawning a
    subprocess; falls back to a subprocess only if pyobjc isn't available in
    the current interpreter.
    """
    if not window_name:
        return None
    try:
        from Quartz import (  # type: ignore
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListOptionOnScreenOnly,
        )
        for w in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID):
            if w.get("kCGWindowOwnerName") == "Terminal" and window_name in (w.get("kCGWindowName") or ""):
                return int(w["kCGWindowNumber"])
        return None
    except ImportError:
        pass

    # Fallback: spawn a subprocess that has Quartz.
    code = (
        "from Quartz import CGWindowListCopyWindowInfo, "
        "kCGWindowListOptionOnScreenOnly, kCGNullWindowID\n"
        f"name = {window_name!r}\n"
        "for w in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID):\n"
        "    if w.get('kCGWindowOwnerName') == 'Terminal' and name in (w.get('kCGWindowName') or ''):\n"
        "        print(w['kCGWindowNumber']); break\n"
    )
    try:
        r = subprocess.run(["python3", "-c", code], capture_output=True, text=True, timeout=5)
        val = r.stdout.strip()
        return int(val) if val.isdigit() else None
    except Exception:
        return None


def _window_bounds(as_window_id: str) -> Optional[str]:
    """Return 'x,y,w,h' for `screencapture -R`, or None."""
    as_window_id = int(as_window_id)  # defensive: non-integer raises ValueError, preventing AppleScript injection
    script = f'''
    tell application "Terminal"
        set b to bounds of window id {as_window_id}
        set x to (item 1 of b) as text
        set y to (item 2 of b) as text
        set w to ((item 3 of b) - (item 1 of b)) as text
        set h to ((item 4 of b) - (item 2 of b)) as text
        return x & "," & y & "," & w & "," & h
    end tell
    '''
    ok, out, _err = osascript(script, timeout=5)
    if ok and out.count(",") == 3:
        return out
    return None


def capture_window(target: WatchTarget, path: str) -> bool:
    """Screenshot the target window using its pre-resolved identifiers.

    Never re-resolves the CG window id — caller must pass a pre-built
    WatchTarget constructed at startup.  Uses ``screencapture -l{cg_id}``
    when available, else falls back to AppleScript bounds.  Never falls back
    to full-screen capture (we don't want unrelated content leaking to
    Telegram).
    """
    if target.cg_id is not None:
        try:
            subprocess.run(
                ["screencapture", "-x", f"-l{target.cg_id}", path],
                check=True,
                timeout=10,
            )
            if os.path.exists(path):
                return True
        except Exception as e:
            log.debug("screencapture -l failed: %s", e)

    bounds = _window_bounds(target.as_window_id)
    if bounds:
        try:
            subprocess.run(
                ["screencapture", "-x", f"-R{bounds}", path],
                check=True,
                timeout=10,
            )
            if os.path.exists(path):
                return True
        except Exception as e:
            log.debug("screencapture -R failed: %s", e)

    log.warning(
        "could not screenshot window %r (as_window_id=%s, cg_id=%s)",
        target.window_name, target.as_window_id, target.cg_id,
    )
    return False


def get_window_history(as_window_id: str) -> Optional[str]:
    """Return the tail of every tab's history for the given window, or None."""
    as_window_id = int(as_window_id)  # defensive: non-integer raises ValueError, preventing AppleScript injection
    script = f'''
    tell application "Terminal"
        set outputText to ""
        try
            set winID to {as_window_id}
            repeat with j from 1 to count of tabs of window id winID
                set hist to (history of tab j of window id winID) as text
                set lineList to paragraphs of hist
                set lineCount to count of lineList
                set startLine to lineCount - ({HISTORY_TAIL_LINES} - 1)
                if startLine < 1 then set startLine to 1
                set recentLines to ""
                repeat with k from startLine to lineCount
                    set recentLines to recentLines & (item k of lineList) & linefeed
                end repeat
                set outputText to outputText & "||WIN:{as_window_id}||" & recentLines & linefeed
            end repeat
        on error errMsg
            set outputText to "ERROR:" & errMsg
        end try
        return outputText
    end tell
    '''
    ok, out, _err = osascript(script)
    if not ok or not out or "ERROR:" in out:
        return None
    return out


# ── Byte-level injection into a specific Terminal window ──────────────────────

def _send_bytes(target_window_id: str, payload: str) -> bool:
    """Write raw bytes straight to the tty of the given Terminal window via
    `do script ... in window id N without entering`. Each byte is encoded as
    `(ASCII character N)` so escape sequences and special characters survive.

    This is what fixes the "pasted into the wrong terminal" bug — the bytes
    only ever reach the target window, regardless of focus, and we never use
    the system clipboard.
    """
    target_window_id = int(target_window_id)  # defensive: non-integer raises ValueError, preventing AppleScript injection
    if not payload:
        return True
    parts = " & ".join(f"(ASCII character {ord(ch)})" for ch in payload)
    script = f'''
    tell application "Terminal"
        do script {parts} in window id {target_window_id} without entering
    end tell
    '''
    ok, _out, err = osascript(script)
    if not ok:
        log.debug("do-script send failed: %s", err)
    return ok


def _keystroke(target_window_id: str, applescript_body: str) -> bool:
    """Send key codes to a specific Terminal window via System Events.
    Brings the window to the front first (small visual flash, unavoidable
    for System Events), then waits for focus to settle before injecting.
    Never touches the clipboard. Returns True on success."""
    target_window_id = int(target_window_id)  # defensive: non-integer raises ValueError, preventing AppleScript injection
    script = f'''
    tell application "Terminal"
        activate
        set index of window id {target_window_id} to 1
        set frontmost of window id {target_window_id} to true
    end tell
    delay 0.3
    tell application "System Events"
        tell process "Terminal"
            {applescript_body}
        end tell
    end tell
    '''
    ok, _out, err = osascript(script)
    if not ok:
        log.warning("keystroke send failed: %s", err)
    return ok


def inject_response(target_window_id: str, user_response: str,
                    selected_index: Optional[int] = None) -> None:
    """Convert a Telegram callback / message into bytes sent to the target
    window's tty.

    * ``up`` / ``down`` / ``enter`` → arrow key + Return via System Events
      (Terminal.app's `do script` silently drops ANSI escapes for arrow
      keys, so we use OS-level key codes instead).
    * digit 1–N, selected_index=i → delta navigation: compute
      delta = (n-1) - selected_index, send |delta| downs (delta>0) or ups
      (delta<0), then Return. When selected_index is None, falls back to the
      legacy saturation path: (MAX_OPTIONS+2) ups then (n-1) downs (may
      mis-navigate if Ink's SelectInput wraps instead of clamping).
    * Free-text response → typed verbatim through `do script` then Return.
      `do script` works fine for plain text and avoids the focus flash;
      falls back to System Events on failure.
    """
    resp = user_response.strip()

    def _spaced(keys: list[str]) -> str:
        """Interleave a ``delay`` between each key code so the foreground
        Ink app sees discrete events rather than a single burst."""
        if not keys:
            return ""
        out = [keys[0]]
        for k in keys[1:]:
            out.append(f"delay {KEY_DELAY}")
            out.append(k)
        return "\n".join(out)

    # Navigation responses → keystrokes (the only path that actually delivers
    # arrow-key escape sequences to the foreground process).
    if resp == "up":
        body = "key code 126"
    elif resp == "down":
        body = "key code 125"
    elif resp == "enter":
        body = "key code 36"
    elif resp.isdigit():
        n = int(resp)
        target_idx = n - 1   # 0-based

        if selected_index is not None:
            delta = target_idx - selected_index
            if delta > 0:
                nav_keys = ["key code 125"] * delta      # downs only
            elif delta < 0:
                nav_keys = ["key code 126"] * abs(delta) # ups only
            else:
                nav_keys = []                             # already on target
            log.debug(
                "inject_response: delta nav selected_index=%d → target_idx=%d, delta=%d",
                selected_index, target_idx, delta,
            )
        else:
            # Fallback: no selected_index available — use old saturation path.
            # DEPRECATED: prefer passing selected_index from monitor_screen.
            log.warning(
                "inject_response: selected_index not provided; using saturation fallback "
                "(may be wrong if SelectInput wraps)"
            )
            nav_keys = ["key code 126"] * (MAX_OPTIONS + 2) + ["key code 125"] * target_idx

        if nav_keys:
            body = _spaced(nav_keys) + f"\ndelay {KEY_DELAY * 4}\nkey code 36"
        else:
            body = f"delay {KEY_DELAY * 4}\nkey code 36"
    else:
        # Free-text: type the payload then commit with a REAL Return
        # keystroke. Sending byte `\r` alone via `do script` doesn't
        # reliably submit Copilot CLI's text-input widget — it accepts
        # only the OS-level Return keypress as a final commit. Same
        # pattern used by inject_text_via_other.
        is_ascii = all(ord(c) < 128 for c in resp)
        typed_ok = False
        if is_ascii:
            typed_ok = _send_bytes(target_window_id, resp)  # text only, no \r
        if not typed_ok:
            escaped = resp.replace("\\", "\\\\").replace('"', '\\"')
            typed_ok = _keystroke(target_window_id, f'keystroke "{escaped}"')
        if typed_ok:
            time.sleep(0.15)
            _keystroke(target_window_id, "key code 36")
        return

    if _keystroke(target_window_id, body):
        return

    # Last-resort byte injection (likely to fail for arrow keys, but try).
    log.warning(
        "keystroke path failed; byte-fallback is a no-op for arrow keys "
        "(Terminal.app drops ANSI escape sequences sent via do script). "
        "Response may not have been delivered."
    )
    if resp == "up":
        payload = "\x1b[A"
    elif resp == "down":
        payload = "\x1b[B"
    elif resp == "enter":
        payload = "\r"
    elif resp.isdigit():
        n = int(resp)
        if selected_index is not None:
            target_idx = n - 1
            delta = target_idx - selected_index
            if delta > 0:
                payload = "\x1b[B" * delta + "\r"
            elif delta < 0:
                payload = "\x1b[A" * abs(delta) + "\r"
            else:
                payload = "\r"
        else:
            payload = ("\x1b[A" * (MAX_OPTIONS + 2)) + ("\x1b[B" * (n - 1)) + "\r"
    else:
        payload = resp + "\r"
    _send_bytes(target_window_id, payload)


# ── Screen parser ─────────────────────────────────────────────────────────────
#
# Copilot CLI menus are always rendered as Ink <SelectInput> components with
# `●`/`○`/`❯` indicator lines plus a hints footer (`enter: to select`,
# `enter: to submit`, …). Detecting that visual structure is far more reliable
# than guessing question wordings — GitHub can rename any prompt and we still
# work. The `Screen` dataclass-shape returned here captures *exactly* what we
# need to render the right Telegram keyboard.

def _clean_ansi(text: str) -> str:
    return CTRL_RE.sub(" ", ANSI_RE.sub("", text))


def _find_question_above(lines: list[str], start_idx: int, max_lines: int = 3) -> Optional[str]:
    """Collect up to `max_lines` consecutive non-empty *content* lines scanning
    upward from `start_idx - 1`. Skips:
      * blank lines (until something is collected)
      * box borders / horizontal separators (───, ╭──╮, ╰──╯)
      * the literal "Question" header Copilot CLI's ask_user widget prints
    Stops at the first blank line *after* collecting something, so for
    Copilot's permission prompts we capture both the question and the command
    preview, while a plain header like `"Select Model"` returns just that one
    line."""
    collected: list[str] = []
    for j in range(start_idx - 1, -1, -1):
        raw = lines[j]
        stripped = raw.strip()
        if not stripped:
            if collected:
                break
            continue
        # Skip horizontal-rule and box borders silently.
        if _is_box_border(raw):
            continue
        # Skip the "Question" widget header (case-insensitive, exact word).
        if stripped.lower() == "question":
            continue
        collected.append(stripped)
        if len(collected) >= max_lines:
            break
    if not collected:
        return None
    collected.reverse()
    return " — ".join(collected)[:300]


def _strip_box(line: str) -> str:
    """Remove leading/trailing box-drawing pipes so `│ ❯ 1. label │` → `❯ 1. label`."""
    # Strip outermost pipes only; keep inner punctuation alone.
    stripped = line.rstrip()
    # Remove trailing pipe + trailing whitespace from inside
    if stripped.endswith(("│", "|", "┃")):
        stripped = stripped[:-1].rstrip()
    # Remove leading pipe + following whitespace
    lstripped = stripped.lstrip()
    if lstripped.startswith(("│", "|", "┃")):
        return lstripped[1:]
    return stripped


def _is_box_border(line: str) -> bool:
    """True for `╭───╮`, `╰───╯`, `─────` separator lines, blank-ish lines."""
    s = line.strip()
    if not s:
        return False
    return bool(BOX_BORDER_RE.match(s))


def parse_screen(history: str) -> dict:
    """Classify the most recent screen state. Returns one of:

        {"kind": "selection", "question": str|None,
         "options": [(value, label), ...], "selected_index": int|None}

        {"kind": "text_input", "question": str|None}

        {"kind": "yes_no",    "question": str|None}

        {"kind": "idle"}

    Robust to two distinct Copilot CLI rendering styles:
      * "Ink dots" — every option line has ●/○ (permission, model picker).
      * "Boxed ask_user" — options are numbered, only the selected one has ❯,
        and the whole widget is wrapped in `╭─╮ │ … │ ╰─╯` box drawing.
    """
    cleaned = _clean_ansi(history)
    raw_lines = cleaned.splitlines()

    # ── Lightweight quick-reject ─────────────────────────────────────────
    # Real Copilot widgets always render `│` (U+2502) box pipes in the
    # bottom rows. If there's no pipe character anywhere in the active
    # region, this can't be a widget — skip the heavy regex passes
    # entirely. This shaves the per-cycle work in the common idle case
    # (which is most of the time when no widget is shown).
    quick_tail = raw_lines[-ACTIVE_WIDGET_LINES:] if raw_lines else []
    if not any("│" in ln or "|" in ln for ln in quick_tail):
        return {"kind": "idle"}

    # Normalize: strip box pipes from every line so the parser logic below
    # doesn't have to special-case them. We keep border/separator lines
    # detectable via _is_box_border.
    all_lines = [_strip_box(ln) for ln in raw_lines]
    tail = all_lines[-TAIL_SCAN_LINES:]
    raw_tail_full = raw_lines[-TAIL_SCAN_LINES:]  # same indices as `tail`
    if not tail:
        return {"kind": "idle"}

    # Locate the most recent hints footer. That's the anchor for everything.
    # Restrict the search to the active widget region (bottom-of-screen) so
    # that quoted strings like "↑/↓ to select" in scrollback don't confuse us.
    # CRITICAL: the footer must be wrapped by box-pipe characters on BOTH
    # sides (e.g. `│ ↑/↓ to select · enter to confirm │`). Plain agent
    # narrative that mentions "to submit" or "to select" doesn't have that
    # bilateral box wrapping, so it stays idle. This is the single strongest
    # signal that we're looking at a real Ink-rendered widget vs prose that
    # happens to quote widget-like phrases.
    footer_idx = None
    footer_kind: Optional[str] = None
    footer_scan_start = max(0, len(tail) - ACTIVE_WIDGET_LINES)
    for i in range(len(tail) - 1, footer_scan_start - 1, -1):
        line = tail[i]
        raw_line = raw_tail_full[i] if i < len(raw_tail_full) else line
        # The raw line (before _strip_box) must start AND end with a box
        # pipe so we know this is inside a widget rectangle.
        s = raw_line.strip()
        if not (s.startswith(("│", "|")) and s.endswith(("│", "|"))):
            continue
        if FOOTER_SELECT_RE.search(line):
            footer_idx, footer_kind = i, "select"
            break
        if FOOTER_TEXT_RE.search(line):
            footer_idx, footer_kind = i, "text"
            break

    # Secondary signal: a horizontal box border (╭─╮ or ╰─╯) in the active
    # region. Still required as a sanity check, but the bilateral-pipe gate
    # on the footer above is what actually rejects agent narrative.
    has_box = any(_is_box_border(ln) for ln in raw_tail_full)

    # 1) Selection menu: numbered or indicator option lines above a "select" footer.
    if footer_idx is not None and footer_kind == "select":
        # raw_opts entries are (selected: bool, label: str)
        raw_opts: list[tuple[bool, str]] = []
        topmost_opt_idx: Optional[int] = None
        i = footer_idx - 1
        blanks = 0
        while i >= 0 and len(raw_opts) < MAX_OPTIONS:
            line = tail[i]
            stripped = line.strip()

            # Skip box borders and horizontal separators silently.
            if _is_box_border(line):
                i -= 1
                continue

            if not stripped:
                blanks += 1
                # One blank between options/header is allowed; two ends the block.
                if blanks >= 2 or raw_opts:
                    break
                i -= 1
                continue
            blanks = 0

            # Style B (more specific): numbered option with optional ❯ marker.
            # Tried BEFORE the generic indicator regex so that `❯ 1. label`
            # extracts a clean `label` instead of `1. label`. Only trusted
            # when a box border is present in the active region — otherwise
            # plain agent text containing `1.` `2.` lists would falsely
            # parse as a live widget.
            m2 = NUMBERED_OPT_RE.match(line) if has_box else None
            if m2:
                selected = m2.group(1) in ("❯", ">")
                raw_opts.append((selected, m2.group(3).strip()))
                topmost_opt_idx = i
                i -= 1
                continue

            # Style A: per-line indicator (●/○/❯/>).
            m = INDICATOR_RE.match(line)
            if m:
                selected = m.group(1) in ("●", "❯")
                raw_opts.append((selected, m.group(2).strip()))
                topmost_opt_idx = i
                i -= 1
                continue

            # Non-option non-blank non-border line: end of option block.
            break

        # Guard: if no option lines were found above the footer, this is not
        # a live widget — likely quoted help text. raw_opts being empty handles
        # this (satisfies RFC §1.2 B3 "at least one option above footer").
        if raw_opts and topmost_opt_idx is not None:
            raw_opts.reverse()
            options = [(str(idx + 1), label) for idx, (_sel, label) in enumerate(raw_opts)]
            selected_index = next(
                (idx for idx, (sel, _) in enumerate(raw_opts) if sel),
                None,
            )
            question = _find_question_above(tail, topmost_opt_idx)
            return {
                "kind": "selection",
                "question": question,
                "options": options,
                "selected_index": selected_index,
            }

    # 2) Text input: "to submit" footer with no indicator lines above it.
    # Like the selection branch, gated on a box border being present —
    # without it, agent narrative containing "to submit" (e.g. "...press
    # Return to submit.") would falsely trigger a text_input prompt.
    if footer_idx is not None and footer_kind == "text" and has_box:
        question = _find_question_above(tail, footer_idx)
        return {"kind": "text_input", "question": question}

    # 3) Plain Y/N shell prompt (not all CLI prompts come from Copilot itself).
    # Scoped to the active widget region so that `[y/N]` strings appearing in
    # quoted agent-output text further up in scrollback don't false-trigger.
    yn_start = max(0, len(tail) - ACTIVE_WIDGET_LINES)
    for i in range(len(tail) - 1, yn_start - 1, -1):
        line = tail[i]
        # Bilateral pipe guard (same rule as selection / text_input branches):
        # the raw line must start AND end with a box pipe so that `[y/N]`
        # in plain prose never fires here.
        raw_line = raw_tail_full[i] if i < len(raw_tail_full) else line
        s = raw_line.strip()
        if not (s.startswith(("│", "|")) and s.endswith(("│", "|"))):
            continue
        # Reject multi-column table rows: after _strip_box removes the outer
        # pipes, any remaining │/| in `line` means it is a table cell row, not
        # a simple yes/no prompt.  e.g. "│ Prompt │ Default [y/N] │ Notes │"
        # strips to " Prompt │ Default [y/N] │ Notes " — inner pipes present.
        if "│" in line or "|" in line:
            continue
        if YN_RE.search(line):
            return {"kind": "yes_no", "question": line.strip()[:200]}

    return {"kind": "idle"}


def screen_hash(screen: dict, window_id: str) -> str:
    """Stable hash of a non-idle screen for cooldown deduplication.

    Crucially does *not* include `selected_index`: the user navigating the
    menu shouldn't be treated as a new prompt.
    """
    parts = [window_id, screen.get("kind", ""), screen.get("question") or ""]
    for value, label in screen.get("options") or ():
        parts.append(f"{value}\x1f{label}")
    return hashlib.md5("\x1e".join(parts).encode()).hexdigest()


# Regex matching the "Other (type your answer)" / "Other (specify)" option that
# Copilot CLI's ask_user widget auto-appends to free-form-capable prompts.
OTHER_OPTION_RE = re.compile(r"^Other\b(?:\s*\(.*\))?\s*$", re.IGNORECASE)


def find_other_option(options: list[tuple[str, str]]) -> Optional[int]:
    """Return the 0-based index of the LAST 'Other (type your answer)' option,
    or None if none exist. Copilot CLI's widget auto-appends its own "Other"
    as the final option — when the user's own prompt also contains an
    "Other"-labeled option, only the last (Copilot-appended) one actually
    triggers text-input sub-mode. We pick the last match for navigation.
    """
    last = None
    for idx, (_value, label) in enumerate(options):
        if OTHER_OPTION_RE.match(label.strip()):
            last = idx
    return last


def find_all_other_options(options: list[tuple[str, str]]) -> set[int]:
    """Return the set of all 0-based indices whose label matches the "Other
    (type your answer)" pattern. Used by `build_keyboard` to hide every
    such option from the Telegram button row (the user replies in chat
    instead). Without this, a user-supplied "Other" option leaks through
    when Copilot also appends its own."""
    return {
        idx
        for idx, (_value, label) in enumerate(options)
        if OTHER_OPTION_RE.match(label.strip())
    }


# ── Keyboard rendering ────────────────────────────────────────────────────────

def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ellipsize(text: str, limit: int = TG_TEXT_MAX) -> str:
    """Trim long strings for Telegram captions, appending '...' when cut."""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 3)] + "..."


def build_keyboard(screen: dict) -> Optional[dict]:
    """Build an inline keyboard that mirrors exactly what the terminal shows.

    Every keyboard we render comes from a successfully parsed screen, so we
    never produce "ghost" buttons that don't correspond to anything visible.
    There is no manual `↑↓⏎` fallback row — the only non-button affordance is
    free-text reply, which is the natural fallback when the CLI is asking
    the user to *type* something.
    """
    kind = screen.get("kind")

    if kind == "text_input":
        # No buttons for text_input — Copilot's text widget rejects empty
        # submissions, so a "Submit empty" affordance is misleading. The
        # caption tells the user to reply in chat; the chat handler will
        # type+Return into the widget.
        return None

    if kind == "yes_no":
        return {"inline_keyboard": [
            [{"text": "Yes", "callback_data": "y"}],
            [{"text": "No", "callback_data": "n"}],
        ]}

    if kind == "selection":
        options = screen.get("options") or []
        selected = screen.get("selected_index")
        hidden_indices = find_all_other_options(options)
        rows: list[list[dict]] = []
        for idx, (value, label) in enumerate(options):
            # Hide every "Other (type your answer)" entry — Copilot CLI
            # auto-appends its own, and any matching user-supplied option
            # is indistinguishable in Telegram. Free-text routing goes
            # through whichever one is last (see find_other_option).
            if idx in hidden_indices:
                continue
            prefix = "● " if idx == selected else ""
            text = f"{prefix}{value}. {label}" if value.isdigit() else f"{prefix}{label}"
            if len(text) > 60:
                text = text[:57] + "…"
            rows.append([{"text": text, "callback_data": value}])
        return {"inline_keyboard": rows} if rows else None

    return None


# ── Telegram input loop ───────────────────────────────────────────────────────

def inject_text_via_other(window_id: str, screen: dict, text: str) -> bool:
    """Free-text reply on a selection screen: navigate to the "Other (type
    your answer)" option, press Return to enter text-input sub-mode, wait
    for the terminal to redraw, then type the text and submit. Returns True
    if the whole sequence dispatched (does not verify on-screen result).

    Uses delta navigation from screen["selected_index"] to the "Other" option
    index (found via find_other_option). Falls back to index 0 when
    selected_index is unknown.
    """
    options = screen.get("options") or []
    if find_other_option(options) is None:
        log.info("free-text reply but screen has no 'Other' option; typing as-is")
        return _send_bytes(window_id, text + "\r")

    other_idx = find_other_option(options)   # 0-based index of "Other" option
    current_idx = screen.get("selected_index") or 0

    # Step 1: delta-navigate to the "Other" option, then press Return to enter
    # text-input sub-mode. We use the same per-keystroke delay as inject_response
    # so Ink processes the events individually.
    delta = other_idx - current_idx
    if delta > 0:
        nav_keys = [f"key code 125\ndelay {KEY_DELAY}"] * delta   # downs
    elif delta < 0:
        nav_keys = [f"key code 126\ndelay {KEY_DELAY}"] * abs(delta)   # ups
    else:
        nav_keys = []   # already on Other

    if nav_keys:
        nav_body = "\n".join(nav_keys) + f"\ndelay {KEY_DELAY * 4}\nkey code 36"
    else:
        nav_body = f"delay {KEY_DELAY * 4}\nkey code 36"

    log.info(
        "free-text reply → delta nav to Other (idx=%d, from=%d, delta=%d) then Return",
        other_idx, current_idx, delta,
    )
    # NOTE: We no longer use (MAX_OPTIONS+2) downs because Ink's SelectInput
    # does NOT clamp at the bottom for all versions — it wraps in v4+.
    # Delta navigation from selected_index is always correct.
    if not _keystroke(window_id, nav_body):
        log.warning("could not navigate to Other; aborting free-text inject")
        return False

    # Step 2: let Copilot CLI's widget redraw into text-input sub-mode.
    time.sleep(0.6)

    # Step 3: type the text. For ASCII-only payloads, `do script ... without
    # entering` injects fast. For non-ASCII (any byte > 127), AppleScript's
    # `ASCII character N` builder maps through macRoman and corrupts the
    # glyph (e.g. ñ→Ò), so we route to System Events keystrokes which
    # handle UTF-8 correctly.
    #
    # Either way the final Return MUST be a real `key code 36` — Copilot
    # CLI's text-input widget doesn't always treat raw byte 13 (\r) as a
    # submit, but the OS-level Return keystroke is unambiguous.
    is_ascii = all(ord(c) < 128 for c in text)
    typed_ok = False
    if is_ascii:
        typed_ok = _send_bytes(window_id, text)  # text only, no \r
    if not typed_ok:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        typed_ok = _keystroke(window_id, f'keystroke "{escaped}"')
    if not typed_ok:
        return False
    # Brief settle, then a real Return keystroke to submit.
    time.sleep(0.15)
    return _keystroke(window_id, "key code 36")


def wait_for_telegram_input(tg: Telegram, window_id: str) -> Optional[tuple[str, str]]:
    """Wait for a Telegram tap or text reply. Returns ``(response, source)``
    where source is ``"callback"`` (button tap) or ``"text"`` (chat message),
    or ``None`` if the user already answered locally in the terminal (screen
    went idle). Source matters because text replies on a selection screen
    need a two-step inject (navigate to "Other" then type), while callbacks
    are just key codes.
    """
    # Lazily initialized on the first live poll so that window-gone / idle
    # early exits never touch the Telegram object (and tests can pass a bare
    # object() without a latest_update_id stub).
    last_update_id: Optional[int] = None

    while True:
        history = get_window_history(window_id)
        if history is None:
            log.warning("wait_for_telegram_input: window gone (history=None); aborting wait")
            return None
        if parse_screen(history).get("kind") == "idle":
            return None  # answered on the terminal directly

        if last_update_id is None:
            last_update_id = tg.latest_update_id()
        for update in tg.get_updates(offset=last_update_id + 1, timeout=5):
            last_update_id = update["update_id"]

            if "callback_query" in update:
                cq = update["callback_query"]
                value = cq.get("data", "")
                tg.answer_callback(cq["id"], f"↪ {value}")
                return (value, "callback")

            msg = update.get("message") or {}
            text = msg.get("text")
            if text:
                return (text.strip(), "text")


# ── Main loop ─────────────────────────────────────────────────────────────────

def monitor_screen(tg: Telegram, window_id: str, window_name: str) -> None:
    short_name = window_name.split(" — ")[0].strip()
    target = WatchTarget(
        as_window_id=window_id,
        cg_id=_quartz_window_id(window_name),
        window_name=window_name,
    )
    if target.cg_id is None:
        log.warning(
            "Quartz CG window id not resolved for %r — falling back to "
            "AppleScript bounds for screenshots (less stable if window moves).",
            window_name,
        )
    tg.send_message(f"<b>copilot_watcher</b> — <code>{short_name}</code>")

    cycle = 0
    last_hash: Optional[str] = None
    last_trigger_time = 0.0
    # Question-keyed cooldown — survives subtle re-render hash drift after
    # injection. Keyed on (kind, question, options-tuple) so that two
    # back-to-back text_input prompts (which might both have empty
    # questions) don't get mistakenly collapsed onto each other.
    last_answered_key: Optional[tuple] = None
    last_answered_time = 0.0
    ANSWERED_COOLDOWN = 30.0

    def _identity_key(s: dict) -> tuple:
        return (
            s.get("kind", ""),
            s.get("question") or "",
            tuple((v, l) for v, l in (s.get("options") or ())),
        )

    # Cache the most recent (history → parsed-screen) so consecutive polls
    # with unchanged history skip the parse entirely. The AppleScript fetch
    # itself is the biggest per-cycle cost; this just makes idle stretches
    # essentially free CPU-wise.
    last_history: Optional[str] = None
    last_screen: dict = {"kind": "idle"}
    idle_streak = 0  # consecutive idle cycles → adaptive poll slowdown

    while True:
        cycle += 1
        history = get_window_history(window_id)

        if history is None:
            log.info("[%d] window gone, retrying...", cycle)
            time.sleep(3)
            continue

        # Skip re-parsing if history hasn't changed since last poll —
        # common when the terminal is idle (no new bytes written).
        if history == last_history:
            screen = last_screen
        else:
            screen = parse_screen(history)
            last_history = history
            last_screen = screen
        kind = screen["kind"]
        if kind == "idle":
            idle_streak += 1
            # Run _maybe_dump_unparsed on every idle cycle; the function throttles
            # per-content via _LAST_UNPARSED to avoid disk spam.
            _maybe_dump_unparsed(history)

            if cycle % max(1, int(30 / POLL_INTERVAL_SECONDS)) == 0:
                log.info("[%d] idle (heartbeat)", cycle)
            else:
                log.debug("[%d] idle", cycle)
            if log.isEnabledFor(logging.DEBUG):
                tail = _clean_ansi(history).splitlines()[-5:]
                for ln in tail:
                    log.debug("    tail │ %s", ln)
            # Adaptive polling: after sustained idle, back off to the
            # slower interval to reduce osascript subprocess churn.
            interval = (
                POLL_INTERVAL_IDLE_SECONDS
                if idle_streak >= IDLE_CYCLES_BEFORE_SLOWDOWN
                else POLL_INTERVAL_SECONDS
            )
            time.sleep(interval)
            continue

        idle_streak = 0  # any non-idle screen resets to fast polling

        question = screen.get("question")
        ctx_hash = screen_hash(screen, window_id)
        ctx_key = _identity_key(screen)
        now = time.time()

        # Two layers of cooldown:
        #   1. Hash-based (catches identical re-renders).
        #   2. Identity-keyed (kind+question+options): survives subtle
        #      hash drift on the SAME logical prompt, but lets a new
        #      prompt through even if its question text is empty/similar.
        if ctx_hash == last_hash and (now - last_trigger_time) < COOLDOWN_SECONDS:
            log.debug("[%d] cooldown (hash) — skipping", cycle)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        if last_answered_key is not None and ctx_key == last_answered_key and (now - last_answered_time) < ANSWERED_COOLDOWN:
            log.debug("[%d] cooldown (just answered same prompt) — skipping", cycle)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        last_hash = ctx_hash
        last_trigger_time = now

        log.info(
            "[%d] prompt kind=%s question=%r options=%d",
            cycle, kind, question, len(screen.get("options") or []),
        )

        capture_window(target, SCREENSHOT_PATH)

        # Build a readable, well-spaced caption. Sections are separated by
        # blank lines so the prompt question stands out from the metadata
        # and the action hint.
        parts = ["<b>Copilot needs your input</b>"]
        if question:
            parts.append(f"<i>{_html_escape(_ellipsize(question))}</i>")
        parts.append(f"<code>{short_name}</code>")
        if kind == "text_input":
            parts.append("<i>Type your answer in the chat</i>")
        elif kind == "selection" and find_other_option(screen.get("options") or []) is not None:
            # We hide the "Other (type your answer)" button; tell the user
            # they can still type a custom answer by replying in chat.
            parts.append("<i>Tap a button, or reply in chat to type a custom answer</i>")
        caption = "\n\n".join(parts)

        keyboard = build_keyboard(screen)
        # try/finally guarantees the screenshot file is removed even if
        # send_photo raises (network blip, Telegram outage, etc.) — otherwise
        # the screenshot file lingers until the next successful cycle.
        try:
            tg.send_photo(SCREENSHOT_PATH, caption, keyboard)
        finally:
            _safe_unlink(SCREENSHOT_PATH)

        result = wait_for_telegram_input(tg, window_id)

        if result is None:
            log.info("[%d] answered locally, skipping injection", cycle)
            user_response = "local"
        else:
            user_response, source = result
            log.info("[%d] response (%s): %r", cycle, source, user_response)
            if source == "text" and kind == "selection":
                # Free-text reply on a selection screen → route through "Other".
                inject_text_via_other(window_id, screen, user_response)
            else:
                inject_response(window_id, user_response,
                                selected_index=screen.get("selected_index"))

        # Remember the prompt we just answered so subtle re-renders of the
        # same screen don't re-trigger us. We key on the full identity tuple
        # (kind, question, options) so distinct prompts with overlapping
        # question text (or both empty) aren't mistakenly collapsed.
        last_answered_key = ctx_key
        last_answered_time = time.time()

        # Confirmation screenshot after the terminal settles.
        time.sleep(SETTLE_DELAY_SECONDS)
        if capture_window(target, SCREENSHOT_PATH):
            try:
                tg.send_photo(SCREENSHOT_PATH, f"<b>Result</b>\n\n<code>{_html_escape(_ellipsize(user_response, 80))}</code>", None)
            finally:
                _safe_unlink(SCREENSHOT_PATH)

        log.info("[%d] done, resuming", cycle)


def _safe_unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# Tracks the last screen we auto-dumped + when, so we don't write the same
# unparsed screen to disk every 2 seconds. Keyed by hash(cleaned tail).
# Capped at MAX_UNPARSED_TRACKED entries — oldest evicted on overflow so the
# dict can't grow unboundedly across a long-running session.
_LAST_UNPARSED: dict[str, float] = {}
MAX_UNPARSED_TRACKED = 128
# Auto-prune dump files older than this many seconds at startup of each
# _maybe_dump_unparsed call (cheap stat-based scan).
UNPARSED_DUMP_TTL_SECONDS = 24 * 3600


def _prune_old_unparsed_files() -> None:
    """Best-effort cleanup of stale `copilot_watcher_unparsed_*.txt` files in
    UNPARSED_DUMP_DIR. Files older than UNPARSED_DUMP_TTL_SECONDS are removed
    so the home directory doesn't accumulate dumps across long-running sessions."""
    try:
        cutoff = time.time() - UNPARSED_DUMP_TTL_SECONDS
        with os.scandir(UNPARSED_DUMP_DIR) as it:
            for entry in it:
                if not entry.name.startswith("copilot_watcher_unparsed_"):
                    continue
                try:
                    if entry.stat().st_mtime < cutoff:
                        os.unlink(entry.path)
                except OSError:
                    pass
    except OSError:
        pass


def _maybe_dump_unparsed(history: str) -> None:
    """If the tail contains a footer hint in the **active widget region** (the
    bottom ~20 lines, where a live Copilot widget lives) but the parser still
    returned idle, save the cleaned tail to ``UNPARSED_DUMP_DIR`` so the user
    can share it. Throttled per-content to avoid spam.

    Crucially, we restrict the footer-hint search to the very bottom of the
    screen so that quoted strings like "↑/↓ to select" appearing in agent-
    output history don't trigger spurious dumps.
    """
    cleaned = _clean_ansi(history)
    bottom_lines = cleaned.splitlines()[-ACTIVE_WIDGET_LINES:]
    bottom_text = "\n".join(bottom_lines)

    has_footer = (
        FOOTER_SELECT_RE.search(bottom_text)
        or FOOTER_TEXT_RE.search(bottom_text)
    )
    if not has_footer:
        return

    # Dump the larger tail so the snapshot is actually useful for diagnosis.
    tail_text = "\n".join(cleaned.splitlines()[-TAIL_SCAN_LINES:])
    key = hashlib.md5(tail_text.encode()).hexdigest()[:12]
    now = time.time()
    if now - _LAST_UNPARSED.get(key, 0.0) < UNPARSED_DUMP_COOLDOWN:
        return

    # Evict the oldest entry if we'd exceed the cap (bounds memory growth).
    if len(_LAST_UNPARSED) >= MAX_UNPARSED_TRACKED and key not in _LAST_UNPARSED:
        oldest = min(_LAST_UNPARSED, key=_LAST_UNPARSED.get)
        _LAST_UNPARSED.pop(oldest, None)
    _LAST_UNPARSED[key] = now

    # Prune stale dump files (cheap; runs only when we're about to write one).
    _prune_old_unparsed_files()

    try:
        os.makedirs(UNPARSED_DUMP_DIR, exist_ok=True)
        path = os.path.join(UNPARSED_DUMP_DIR, f"copilot_watcher_unparsed_{key}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(tail_text)
        log.warning(
            "Detected footer hint in active region but parser returned idle. Dumped → %s",
            path,
        )
    except OSError as e:
        log.debug("could not dump unparsed screen: %s", e)


# ── Window picker ─────────────────────────────────────────────────────────────

def list_terminal_windows() -> list[tuple[str, str]]:
    """All open Terminal windows minus the one running this script."""
    script = '''
    tell application "Terminal"
        set winList to {}
        repeat with i from 1 to count of windows
            set end of winList to ((id of window i) as text) & "::" & (name of window i) & linefeed
        end repeat
        return winList as text
    end tell
    '''
    ok, out, _err = osascript(script)
    if not ok:
        return []
    windows: list[tuple[str, str]] = []
    for entry in out.splitlines():
        if "::" not in entry:
            continue
        wid, title = entry.split("::", 1)
        title = title.strip()
        if "copilot_watcher" in title:
            continue
        windows.append((wid.strip(), title))
    return windows


def choose_window() -> tuple[str, str]:
    windows = list_terminal_windows()
    if not windows:
        log.error("No Terminal windows found. Is Terminal.app running?")
        sys.exit(1)
    print("\nOpen Terminal windows:")
    width = len(str(len(windows)))
    for i, (wid, title) in enumerate(windows, 1):
        print(f"  {i:>{width}}. [id={wid}]  {title}")
    while True:
        try:
            choice = input(f"\nWatch which window? [1-{len(windows)}, default 1]: ").strip()
            idx = int(choice) - 1 if choice else 0
            if 0 <= idx < len(windows):
                return windows[idx]
            print(f"Please enter a number between 1 and {len(windows)}.")
        except (ValueError, KeyboardInterrupt):
            log.info("aborted.")
            sys.exit(0)


def pick_from_arg(arg: str) -> Optional[tuple[str, str]]:
    windows = list_terminal_windows()
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(windows):
            return windows[idx]
    for wid, title in windows:
        if arg.lower() in title.lower():
            return wid, title
    return None


# ── Entry point ───────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    # Handle --version / -V before credential checks so it works without a .env.
    if "-V" in argv[1:] or "--version" in argv[1:]:
        print(f"copilot_watcher {__version__}")
        return 0

    # Parse args. Supported forms:
    #   copilot_watcher.py                  → use TARGET_WINDOW_NAME, else picker
    #   copilot_watcher.py --pick           → always show picker (ignore env)
    #   copilot_watcher.py -p               → same as --pick
    #   copilot_watcher.py --dump [path]    → grab the chosen window's current
    #                                          history to <path> (default stdout)
    #                                          and exit. Use to share missed
    #                                          prompts with the maintainer.
    #   copilot_watcher.py <name|index>     → match by substring or 1-based index
    args = argv[1:]
    force_pick = False
    dump_mode = False
    dump_path: Optional[str] = None
    positional: Optional[str] = None
    i_arg = 0
    while i_arg < len(args):
        a = args[i_arg]
        if a in ("--pick", "-p"):
            force_pick = True
        elif a == "--dump":
            dump_mode = True
            # Optional path argument (must not look like a flag).
            if i_arg + 1 < len(args) and not args[i_arg + 1].startswith("-"):
                dump_path = args[i_arg + 1]
                i_arg += 1
        elif a in ("-V", "--version"):
            print(f"copilot_watcher {__version__}")
            return 0
        elif a in ("-h", "--help"):
            print("Usage: copilot_watcher.py [-p | --dump [path] | <name|index>]")
            print("  -p, --pick       force interactive window picker")
            print("  --dump [path]    capture chosen window's current text and exit")
            print("  -V, --version    print version and exit")
            print("  -h, --help       show this help and exit")
            print("  <name>           substring-match a window title")
            print("  <index>          1-based index into the picker list")
            return 0
        elif positional is None:
            positional = a
        else:
            print(f"ERROR: unexpected argument: {a}", file=sys.stderr)
            return 2
        i_arg += 1

    # --dump doesn't need Telegram creds.
    if not dump_mode and (not BOT_TOKEN or not CHAT_ID):
        print(
            "ERROR: BOT_TOKEN and CHAT_ID must be set (env or .env file next to this script).\n"
            "       See the header docstring for the full list of supported env vars.",
            file=sys.stderr,
        )
        return 2

    tg = Telegram(BOT_TOKEN, CHAT_ID) if not dump_mode else None

    chosen: Optional[tuple[str, str]] = None
    if not force_pick:
        arg = positional if positional else TARGET_WINDOW_NAME
        if arg:
            chosen = pick_from_arg(arg)
            if chosen is None:
                print(f"[WATCHER] No window matching {arg!r}. Available:")
                for i, (wid, t) in enumerate(list_terminal_windows(), 1):
                    print(f"  {i}. [id={wid}]  {t}")
                return 1
    if chosen is None:
        chosen = choose_window()

    window_id, window_name = chosen

    if dump_mode:
        history = get_window_history(window_id)
        if history is None:
            print(f"ERROR: could not read history of window {window_id}", file=sys.stderr)
            return 1
        cleaned = _clean_ansi(history)
        if dump_path:
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(cleaned)
            print(f"Wrote {len(cleaned)} bytes to {dump_path}")
            # Also print parser verdict for quick diagnostics.
            screen = parse_screen(history)
            print(f"parse_screen() → kind={screen['kind']!r}, "
                  f"question={screen.get('question')!r}, "
                  f"options={len(screen.get('options') or [])}")
        else:
            sys.stdout.write(cleaned)
        return 0

    assert tg is not None
    log.info("Watching: %s (id=%s)", window_name, window_id)
    try:
        monitor_screen(tg, window_id, window_name)
    except KeyboardInterrupt:
        log.info("stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
