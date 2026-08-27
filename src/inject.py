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
BACKSPACE_DELAY_SECS = 0.008
PASTE_SETTLE_SECS = 0.05  # let the target app process Ctrl+V before restoring
                          # the clipboard, or the paste can grab the old value
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


def _restore_foreground() -> None:
    """Bring the target app back to foreground before injecting keystrokes.

    When the user presses the dictation hotkey, the keyboard hook suppresses
    the keystrokes and may leave focus on the wrong window (the overlay, the
    tray, etc.).  This ensures SendInput goes to the actual target app.

    Uses the AttachThreadInput trick to bypass Windows 10/11 foreground-lock
    restrictions that silently block SetForegroundWindow.
    """
    if not _target_hwnd:
        return
    try:
        cur = user32.GetForegroundWindow()
        if cur == _target_hwnd:
            return
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        our_tid = _kernel32.GetCurrentThreadId()
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
    except Exception:
        pass


def inject_text(text: str) -> None:
    """Type `text` into the currently focused window."""
    if not text:
        return
    _restore_foreground()
    for ch in text:
        if ch == "\n":
            _send_key(VK_RETURN, False)
            _send_key(VK_RETURN, True)
        elif ch == "\r":
            continue
        else:
            _type_char(ch)


def paste_text(text: str) -> None:
    """Insert `text` at the caret via clipboard paste (Ctrl+V).

    Used for corrections/replacements (finalize's post-cleanup swap, Polish,
    Send) instead of character-by-character SendInput: a 300-character
    correction takes ~15ms/char (~4.5s) to visibly retype but is effectively
    instant as a single paste. Falls back to inject_text() if pyperclip or the
    clipboard round-trip is unavailable. The clipboard's previous contents
    (if any, and if textual) are restored afterward.
    """
    if not text:
        return
    _restore_foreground()
    try:
        import pyperclip
    except ImportError:
        log.warning("pyperclip unavailable; falling back to slow char-by-char retype")
        inject_text(text)
        return
    try:
        previous = pyperclip.paste()
    except Exception:
        previous = None
    try:
        pyperclip.copy(text)
    except Exception as e:
        log.warning("Clipboard copy failed (%s); falling back to slow char-by-char retype", e)
        inject_text(text)
        return
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


class TextInjector:
    """Object-style facade so the engine can treat typing as one unit."""

    def inject_text(self, text: str) -> None:
        inject_text(text)

    def paste_text(self, text: str) -> None:
        paste_text(text)

    def delete_chars(self, n: int) -> None:
        delete_chars(n)


def delete_chars(n: int) -> None:
    """Press Backspace `n` times (deletes characters left of the caret)."""
    _restore_foreground()
    for _ in range(max(0, n)):
        _send_key(VK_BACK, False)
        _send_key(VK_BACK, True)
        time.sleep(BACKSPACE_DELAY_SECS)


def replace_text(old_text: str, new_text: str) -> None:
    """Minimal-edit replacement: backspace the changed tail, retype it."""
    from src.merge import diff_plan

    to_delete, to_type = diff_plan(old_text, new_text)
    if to_delete:
        delete_chars(to_delete)
    if to_type:
        inject_text(to_type)