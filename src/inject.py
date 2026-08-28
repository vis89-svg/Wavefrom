"""Text injection into the focused window via Win32 SendInput."""
from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

log = logging.getLogger(__name__)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D
VK_BACK = 0x08

CHAR_DELAY_SECS = 0.015  # keep apps from dropping keystrokes
# Correction deletions are the "old text disappearing" the user watches during
# every cleanup/Polish/Send correction, and diff_plan() is prefix-only (it
# can't preserve a common suffix once anything mid-text changes), so a modest
# correction routinely means deleting several hundred characters. At the old
# 8ms/char that's multiple real seconds of pure backspacing before anything
# else in _finalize()/polish() can proceed. 3ms is still conservative next to
# CHAR_DELAY_SECS (typing new content, where per-char pacing is more likely to
# matter) but cuts that wait by more than half.
BACKSPACE_DELAY_SECS = 0.003
# Electron/Chromium apps (OpenCode, etc.) process each keystroke through a JS
# re-render, not a cheap native text-field update. A dense backspace burst can
# leave the app's input queue backlogged well after SendInput itself returns
# -- SendInput only queues the event, it doesn't wait for the receiving app to
# actually handle it. If Ctrl+V's paste gets dispatched to the app while it's
# still catching up, and we've already swapped the clipboard back to its
# previous contents (the old 50ms gave almost no margin for this), the app
# ends up pasting stale/wrong clipboard data instead of the correction --
# looking like the correction "didn't appear" or came out partial/truncated.
POST_DELETE_SETTLE_SECS = 0.15  # let a busy app drain its backspace queue
                                 # before we send anything else
PASTE_SETTLE_SECS = 0.35  # let the target app actually process Ctrl+V before
                          # restoring the clipboard, or the paste can grab the
                          # old value
VK_CONTROL = 0x11
VK_V = 0x56

# Virtual key codes for the modifier keys. Apps read the *physical* modifier
# state (GetKeyState/GetAsyncKeyState) even for SendInput UNICODE characters,
# so typing while the user still holds Ctrl/Alt/Shift/Win turns the typed text
# into app shortcuts (Ctrl+S = Save As, etc.). Injection must wait for these
# to be released first.
MODIFIER_VKS = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # shift, ctrl, alt, lwin, rwin
RELEASE_POLL_SECS = 0.05
RELEASE_TIMEOUT_SECS = 2.0


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    # Must mirror the Win32 SDK union (incl. MOUSEINPUT/HARDWAREINPUT) so that
    # sizeof(INPUT) matches cbSize=40 on x64; otherwise SendInput fails 87.
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_GUI_CARETBLINKING = 0x00000001


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def _send_key(vk: int, keyup: bool) -> None:
    flags = KEYEVENTF_KEYUP if keyup else 0
    inp = _INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags))
    if not user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)):
        raise ctypes.WinError(ctypes.get_last_error())


def modifiers_down() -> bool:
    """True if any Ctrl/Alt/Shift/Win key is physically held right now."""
    for vk in MODIFIER_VKS:
        if user32.GetAsyncKeyState(vk) & 0x8000:
            return True
    return False


def wait_for_modifiers_up(timeout: float = RELEASE_TIMEOUT_SECS) -> bool:
    """Wait until no modifier key is held. Returns True if released in time."""
    deadline = time.monotonic() + timeout
    while modifiers_down():
        if time.monotonic() >= deadline:
            log.warning("Modifier keys still held after %.1fs; typing anyway", timeout)
            return False
        time.sleep(RELEASE_POLL_SECS)
    return True


def foreground_window_title() -> str:
    """Title of the currently focused window ("" if none / on failure).

    Used to hint the cleanup LLM about the target app's expected tone (e.g.
    dictating into a formal document editor vs a chat window).
    """
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value or ""
    except Exception as e:
        log.debug("foreground_window_title failed: %s", e)
        return ""


def foreground_window_handle() -> int:
    """HWND of the currently focused window (0 if none / on failure)."""
    try:
        return int(user32.GetForegroundWindow()) or 0
    except Exception as e:
        log.debug("foreground_window_handle failed: %s", e)
        return 0


def foreground_caret() -> tuple[int, int] | None:
    """Screen-space (left, top) of the focused window's caret, or None when the
    window does not expose a visible caret (consoles, many editors).

    Used to verify the caret has not moved between the early text type and the
    final correction — if the user clicked/typed elsewhere, blind backspacing
    would destroy the wrong text, so the correction must be skipped instead.
    """
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        gui = _GUITHREADINFO()
        gui.cbSize = ctypes.sizeof(_GUITHREADINFO)
        if not user32.GetGUIThreadInfo(tid, ctypes.byref(gui)):
            return None
        if not (gui.flags & _GUI_CARETBLINKING):
            return None
        if gui.hwndCaret == 0 and gui.rcCaret.left == 0 and gui.rcCaret.top == 0:
            return None
        return (gui.rcCaret.left, gui.rcCaret.top)
    except Exception as e:
        log.debug("foreground_caret failed: %s", e)
        return None


def capture_typing_context() -> dict:
    """Snapshot of (foreground hwnd, caret pos) guarding the final correction.

    The engine records this after the early text type and re-reads it before
    backspacing: if focus moved to another window or the caret moved, the
    screen no longer matches the engine's bookkeeping and deleting would be
    unsafe.
    """
    return {"hwnd": foreground_window_handle(), "caret": foreground_caret()}


def _type_char(ch: str) -> None:
    for state in (False, True):
        inp = _INPUT(
            type=INPUT_KEYBOARD,
            ki=_KEYBDINPUT(
                wVk=0,
                wScan=ord(ch),
                dwFlags=KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if state else 0),
            ),
        )
        if not user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)):
            raise ctypes.WinError(ctypes.get_last_error())
        time.sleep(CHAR_DELAY_SECS)


_target_hwnd: int = 0  # set by engine before typing; restore focus here


def set_target_hwnd(hwnd: int) -> None:
    global _target_hwnd
    _target_hwnd = hwnd


def _describe_hwnd(hwnd: int) -> str:
    """Class name + title of a window, for diagnosing which specific window
    (not just which top-level app) actually received injected input --
    Electron/Chromium apps have a separate embedded render-surface window
    that can hold keyboard focus independently of the top-level app window's
    foreground status."""
    if not hwnd:
        return "(none)"
    try:
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)
        length = user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buf, length + 1)
        return f"hwnd=0x{hwnd:X} class={cls_buf.value!r} title={title_buf.value!r}"
    except Exception as e:
        return f"hwnd=0x{hwnd:X} (describe failed: {e})"


def _restore_foreground() -> bool:
    """Bring the target app back to foreground before injecting keystrokes.

    When the user presses the dictation hotkey, the keyboard hook suppresses
    the keystrokes and may leave focus on the wrong window (the overlay, the
    tray, etc.).  This ensures SendInput goes to the actual target app.

    Uses the AttachThreadInput trick to bypass Windows 10/11 foreground-lock
    restrictions that silently block SetForegroundWindow -- but
    SetForegroundWindow itself never reports failure even when Windows
    denies it (the foreground-lock timeout only gets stricter the longer
    it's been since the user's last keystroke, and a correction can land
    10-20+ seconds after that). Without checking the actual result, a denied
    request looks identical to a successful one: no exception, no timeout,
    just keystrokes silently delivered to whatever window actually has
    focus instead of the target -- e.g. landing in this app's own overlay
    while the real target app's text never changes. Returns True only when
    the target is confirmed foreground afterward; callers must not inject
    when this is False, since blindly backspacing/pasting into an unverified
    window can corrupt whatever app actually has focus.
    """
    if not _target_hwnd:
        return True  # no known target (e.g. tests/offline) -- nothing to verify
    try:
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        our_tid = _kernel32.GetCurrentThreadId()
        cur = user32.GetForegroundWindow()
        if cur != _target_hwnd:
            cur_tid = user32.GetWindowThreadProcessId(cur, None)
            # Attach to the current foreground thread's input queue so
            # SetForegroundWindow is allowed.
            attached = False
            if cur_tid != our_tid:
                attached = user32.AttachThreadInput(our_tid, cur_tid, True)
            try:
                user32.SetForegroundWindow(_target_hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(our_tid, cur_tid, False)
            # Brief settle so the OS finishes the activation.
            time.sleep(0.02)
        confirmed = user32.GetForegroundWindow() == _target_hwnd
        if not confirmed:
            log.error("SetForegroundWindow did not actually switch focus to "
                      "the target window (%s); Windows likely denied it "
                      "(foreground-lock timeout). Refusing to inject blind.",
                      _describe_hwnd(_target_hwnd))
            return False
        # Diagnostic: which specific window WITHIN the confirmed-foreground
        # app actually holds keyboard input focus right now. For a simple
        # single-window app this is normally _target_hwnd itself; for
        # Electron/Chromium apps it can be a separate embedded render-surface
        # window that SetForegroundWindow's top-level activation does not
        # itself refocus -- if injected text isn't reaching the screen despite
        # "window=True caret=True" in the engine's logs, this line shows
        # exactly which window the keystrokes actually went to.
        try:
            target_tid = user32.GetWindowThreadProcessId(_target_hwnd, None)
            attached2 = (target_tid != our_tid
                        and user32.AttachThreadInput(our_tid, target_tid, True))
            try:
                focused_hwnd = user32.GetFocus()
            finally:
                if attached2:
                    user32.AttachThreadInput(our_tid, target_tid, False)
            log.info("Injecting into target=%s | actually-focused-child=%s",
                     _describe_hwnd(_target_hwnd), _describe_hwnd(focused_hwnd))
        except Exception as e:
            log.debug("Focus diagnostic failed (non-fatal): %s", e)
        return True
    except Exception as e:
        log.debug("_restore_foreground failed: %s", e)
        return False


def inject_text(text: str) -> bool:
    """Type `text` into the currently focused window.

    Returns False (typing nothing) if the target window couldn't be
    confirmed as foreground -- sending keystrokes to an unverified window
    risks corrupting whatever app actually has focus instead.
    """
    if not text:
        return True
    if not _restore_foreground():
        return False
    for ch in text:
        if ch == "\n":
            _send_key(VK_RETURN, False)
            _send_key(VK_RETURN, True)
        elif ch == "\r":
            continue
        else:
            _type_char(ch)
    return True


def paste_text(text: str) -> bool:
    """Insert `text` at the caret via clipboard paste (Ctrl+V).

    Used for corrections/replacements (finalize's post-cleanup swap, Polish,
    Send) instead of character-by-character SendInput: a 300-character
    correction takes ~15ms/char (~4.5s) to visibly retype but is effectively
    instant as a single paste. Falls back to inject_text() if pyperclip or the
    clipboard round-trip is unavailable. The clipboard's previous contents
    (if any, and if textual) are restored afterward.

    Returns False (pasting nothing) if the target window couldn't be
    confirmed as foreground.
    """
    if not text:
        return True
    if not _restore_foreground():
        return False
    try:
        import pyperclip
    except ImportError:
        log.warning("pyperclip unavailable; falling back to slow char-by-char retype")
        return inject_text(text)
    try:
        previous = pyperclip.paste()
    except Exception:
        previous = None
    try:
        pyperclip.copy(text)
    except Exception as e:
        log.warning("Clipboard copy failed (%s); falling back to slow char-by-char retype", e)
        return inject_text(text)
    _send_key(VK_CONTROL, False)
    _send_key(VK_V, False)
    _send_key(VK_V, True)
    _send_key(VK_CONTROL, True)
    time.sleep(PASTE_SETTLE_SECS)
    if previous is not None:
        try:
            pyperclip.copy(previous)
        except Exception:
            pass
    return True


def _run_with_timeout(fn, timeout: float) -> bool:
    """Run `fn` (a zero-arg callable) on a worker thread, bounded by `timeout`.

    Every injection call funnels through _restore_foreground(), which uses
    AttachThreadInput/SetForegroundWindow -- Win32 APIs that can stall for a
    long time if the target window's thread is busy or unresponsive. Without
    a bound here, that stall freezes the whole engine (no more typing, no
    more slice processing, no UI update) with nothing in the log to explain
    why -- exactly what a missing "Polished final text:" log line after a
    logged "Polish correction:" line indicates. This can't kill the stuck
    Win32 call itself (Python threads aren't forcibly killable), but it stops
    the caller from waiting forever and logs clearly when it happens, so the
    engine can recover and the next occurrence is diagnosable instead of a
    silent multi-minute freeze.

    Returns True only if `fn` both completed within `timeout` AND itself
    reported success (fn's own return value, e.g. inject_text() returning
    False when it couldn't confirm the target window). Callers MUST check
    this: a delete-then-paste correction where the delete silently fails but
    the caller pastes anyway lands the new text on top of the never-removed
    old text -- a garbled mix, not a clean failure. False means "nothing
    after this point in the sequence is safe to run," not "carry on as if
    it worked."
    """
    import concurrent.futures
    # No `with` here: ThreadPoolExecutor.__exit__ calls shutdown(wait=True),
    # which would block on the very thread we're trying to stop waiting for.
    # shutdown(wait=False) lets the call site return immediately on timeout;
    # the abandoned thread finishes (or stays stuck) on its own in the
    # background, harmless since it's a single-use pool used once here.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(fn)
    try:
        return bool(future.result(timeout=timeout))
    except concurrent.futures.TimeoutError:
        log.error("Injection call did not return within %.1fs; abandoning "
                  "it (likely stuck in _restore_foreground) -- caller must "
                  "not treat this as having succeeded", timeout)
        return False
    finally:
        pool.shutdown(wait=False)


class TextInjector:
    """Object-style facade so the engine can treat typing as one unit.

    Each method returns True if it actually completed, False if it was
    abandoned after timing out. Callers doing a delete-then-paste correction
    must check this and stop (not paste) if the delete returned False, or
    the new text lands on top of the never-removed old text.
    """

    def inject_text(self, text: str) -> bool:
        timeout = max(10.0, len(text) * 0.05)
        return _run_with_timeout(lambda: inject_text(text), timeout)

    def paste_text(self, text: str) -> bool:
        timeout = max(10.0, len(text) * 0.05)
        return _run_with_timeout(lambda: paste_text(text), timeout)

    def delete_chars(self, n: int) -> bool:
        timeout = max(10.0, n * 0.05)
        return _run_with_timeout(lambda: delete_chars(n), timeout)


def delete_chars(n: int) -> bool:
    """Press Backspace `n` times (deletes characters left of the caret).

    Returns False (deleting nothing) if the target window couldn't be
    confirmed as foreground -- backspacing into an unverified window risks
    destroying the wrong app's content.
    """
    if n <= 0:
        return True
    if not _restore_foreground():
        return False
    for _ in range(n):
        _send_key(VK_BACK, False)
        _send_key(VK_BACK, True)
        time.sleep(BACKSPACE_DELAY_SECS)
    if n > 20:
        # Only apps that actually have a backlog to drain need this -- a
        # handful of backspaces never builds one up. See POST_DELETE_SETTLE_SECS.
        time.sleep(POST_DELETE_SETTLE_SECS)
    return True


def replace_text(old_text: str, new_text: str) -> None:
    """Minimal-edit replacement: backspace the changed tail, retype it."""
    from src.merge import diff_plan

    to_delete, to_type = diff_plan(old_text, new_text)
    if to_delete:
        delete_chars(to_delete)
    if to_type:
        inject_text(to_type)