"""Windows toast notifications via Shell_NotifyIcon balloon."""
from __future__ import annotations

import ctypes
import threading
import uuid
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

# Without explicit argtypes/restype, ctypes marshals wparam/lparam as plain
# 32-bit ints for this call. Windows can send messages whose wparam/lparam
# don't fit in 32 bits (common on 64-bit Windows), which raised
# "OverflowError: int too long to convert" inside the wndproc callback on
# essentially every non-custom message the toast window received.
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_longlong

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIF_GUID = 0x00000020
WM_USER = 0x0400
WM_DICTATION_TOAST = WM_USER + 1
MAX_TITLE = 64
MAX_MSG = 256


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.DWORD),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uTimeout", wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


def _toast_thread_worker(nid: _NOTIFYICONDATAW) -> None:
    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)
    MSG = ctypes.wintypes.MSG

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_DICTATION_TOAST:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            user32.DestroyWindow(hwnd)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    wc = WNDCLASSW()
    wc.lpfnWndProc = WNDPROC(wndproc)
    wc.lpszClassName = f"DictToast{uuid.uuid4().hex[:8]}"
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "", 0, 0, 0, 0, 0,
                                  None, None, None, None)
    nid.hWnd = hwnd
    nid.uCallbackMessage = WM_DICTATION_TOAST
    nid.uFlags |= NIF_INFO | NIF_ICON | NIF_GUID
    try:
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
    except Exception:
        user32.DestroyWindow(hwnd)
        return

    msg = MSG()
    deadline = 10.0
    import time
    start = time.time()
    while time.time() - start < deadline:
        res = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if res <= 0:
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
    user32.DestroyWindow(hwnd)


def toast(title: str, message: str, timeout_secs: int = 4) -> None:
    """Show a Windows balloon toast. Fire-and-forget, never raises."""
    try:
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.uID = 1
        nid.uTimeout = timeout_secs * 1000
        nid.szInfoTitle = title[: MAX_TITLE - 1]
        nid.szInfo = message[: MAX_MSG - 1]
        nid.szTip = "Dictation"
        nid.dwInfoFlags = 0x00000001  # NIIF_INFO
        threading.Thread(target=_toast_thread_worker, args=(nid,), daemon=True).start()
    except Exception:
        pass  # toasts must never crash the pipeline