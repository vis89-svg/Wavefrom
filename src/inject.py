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

CHAR_DELAY_SECS = 0.03  # keep apps from dropping keystrokes
BACKSPACE_DELAY_SECS = 0.008

# Virtual key codes for the modifier keys. Apps read the *physical* modifier
# state (GetKeyState/GetAsyncKeyState) even for SendInput UNICODE characters,
# so typing while the user still holds Ctrl/Alt/Shift/Win turns the typed text
# into app shortcuts (Ctrl+S = Save As, etc.). Injection must wait for these
# to be released first.
MODIFIER_VKS = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # shift, ctrl, alt, lwin, rwin
RELEASE_POLL_SECS = 0.05
RELEASE_TIMEOUT_SECS = 5.0


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


def inject_text(text: str) -> None:
    """Type `text` into the currently focused window."""
    if not text:
        return
    for ch in text:
        if ch == "\n":
            _send_key(VK_RETURN, False)
            _send_key(VK_RETURN, True)
        elif ch == "\r":
            continue
        else:
            _type_char(ch)


class TextInjector:
    """Object-style facade so the engine can treat typing as one unit."""

    def inject_text(self, text: str) -> None:
        inject_text(text)

    def delete_chars(self, n: int) -> None:
        delete_chars(n)


def delete_chars(n: int) -> None:
    """Press Backspace `n` times (deletes characters left of the caret)."""
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