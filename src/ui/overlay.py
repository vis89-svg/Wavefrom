"""Floating live indicator: waveform + state + live text preview.

A small always-on-top, click-through window that appears near the cursor while
dictating, showing the mic level as animated bars, the current pipeline state
(Recording / Transcribing / Cleaning up / Done), and the live committed text.
Mirrors the "Flow Bar" of modern dictation apps so the user sees the app
hearing and reacting to them in real time.
"""
from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
import tkinter as tk

log = logging.getLogger(__name__)

_STATE_LABELS = {
    "recording": "Recording…",
    "transcribing": "Transcribing…",
    "cleaning": "Cleaning up…",
    "done": "Done",
    "error": "Error",
    "idle": "Idle",
}

_STATE_COLORS = {
    "recording": "#e5484d",
    "transcribing": "#f5a623",
    "cleaning": "#4a90d9",
    "done": "#30a46c",
    "error": "#e5484d",
    "idle": "#8a8f98",
}

_ANIM_MS = 40          # animation tick
_LEVEL_DECAY = 0.85    # bar decay toward silence between level updates
_PREVIEW_CHARS = 500   # preview shows the last N chars (what's being typed)
_BAR_COUNT = 14
_OFFSET_X = 18
_OFFSET_Y = 26

_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080

_user32 = ctypes.WinDLL("user32", use_last_error=True)


def _cursor_pos() -> tuple[int, int]:
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    pt = POINT()
    if _user32.GetCursorPos(ctypes.byref(pt)):
        return pt.x, pt.y
    return 0, 0


class OverlayWindow:
    """Thread-safe overlay. All Tk work happens on its own daemon thread."""

    def __init__(self):
        self._q: queue.Queue[tuple] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._root: tk.Tk | None = None
        self._visible = False
        self._state_seq = 0
        self._levels: list[float] = [0.0] * _BAR_COUNT
        self._t0 = time.monotonic()

    # ------------------------------------------------------------ public API
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="overlay")
        self._thread.start()

    def stop(self) -> None:
        self._q.put(("quit", None))
        if self._thread:
            self._thread.join(timeout=3)

    def set_state(self, state: str, text: str = "") -> None:
        self._q.put(("state", (state, text)))

    def set_level(self, db: float) -> None:
        self._q.put(("level", db))

    # ------------------------------------------------------------ tk thread
    def _run(self) -> None:
        root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.94)
        self._root = root

        frame = tk.Frame(root, bg="#20242a", highlightthickness=1,
                         highlightbackground="#3a4048")
        frame.pack(fill="both", expand=True)

        head = tk.Frame(frame, bg=frame["bg"])
        head.pack(fill="x", padx=10, pady=(8, 2))
        self._dot = tk.Canvas(head, width=12, height=12, bg=frame["bg"],
                              highlightthickness=0)
        self._dot.pack(side="left")
        self._state_lbl = tk.Label(head, text="Idle", font=("Segoe UI", 9, "bold"),
                                   fg="#d7dbe0", bg=frame["bg"])
        self._state_lbl.pack(side="left", padx=6)

        self._bars = tk.Canvas(frame, width=200, height=34, bg="#181c21",
                               highlightthickness=0)
        self._bars.pack(fill="x", padx=10, pady=(4, 2))

        self._preview_lbl = tk.Label(
            frame, text="", font=("Segoe UI", 9), fg="#b9c0c9", bg=frame["bg"],
            wraplength=330, justify="left", anchor="w")
        self._preview_lbl.pack(fill="x", padx=10, pady=(2, 8))

        self._bar_ids = [
            self._bars.create_rectangle(0, 0, 0, 0, fill="#30a46c",
                                        outline="")
            for _ in range(_BAR_COUNT)
        ]

        root.after(_ANIM_MS, self._tick)
        root.mainloop()

    def _drain(self) -> None:
        while True:
            try:
                kind, payload = self._q.get_nowait()
            except queue.Empty:
                return
            if kind == "quit":
                if self._root:
                    self._root.destroy()
                self._root = None
                return
            if kind == "level":
                self._levels.pop(0)
                self._levels.append(float(payload))
            elif kind == "state":
                state, text = payload
                self._apply_state(state, text)

    def _apply_state(self, state: str, text: str) -> None:
        if not self._root:
            return
        if state == "idle":
            if self._visible:
                self._root.withdraw()
                self._visible = False
            return
        self._state_seq += 1
        if not self._visible:
            self._place_near_cursor()
            self._visible = True
        color = _STATE_COLORS.get(state, "#8a8f98")
        self._state_lbl.config(text=_STATE_LABELS.get(state, state),
                               fg=color)
        self._dot.config(bg=color)
        preview = (text or "").strip()
        if len(preview) > _PREVIEW_CHARS:
            preview = "…" + preview[-_PREVIEW_CHARS:]
        self._preview_lbl.config(text=preview)
        if state == "done":
            # brief "Done" confirmation, then fade out until the next dictation
            seq = self._state_seq
            self._root.after(2800, lambda: self._maybe_auto_hide(seq))

    def _maybe_auto_hide(self, seq: int) -> None:
        if not self._root:
            return
        if seq != self._state_seq:
            return  # a newer state took over
        if self._visible:
            self._root.withdraw()
            self._visible = False

    def _place_near_cursor(self) -> None:
        if not self._root:
            return
        x, y = _cursor_pos()
        self._root.update_idletasks()
        w = self._root.winfo_reqwidth()
        h = self._root.winfo_reqheight()
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        px = min(x + _OFFSET_X, sw - w - 8)
        py = min(y + _OFFSET_Y, sh - h - 8)
        self._root.geometry(f"+{max(px, 0)}+{max(py, 0)}")
        self._root.deiconify()
        self._make_click_through()

    def _make_click_through(self) -> None:
        try:
            # Tk's toplevel window handle is the parent of winfo_id()'s child.
            hwnd = _user32.GetParent(self._root.winfo_id())
            if not hwnd:
                return
            SetWindowLongPtrW = _user32.SetWindowLongPtrW
            style = SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, 0)
            if style:
                SetWindowLongPtrW(
                    hwnd, _GWL_EXSTYLE,
                    style | _WS_EX_LAYERED | _WS_EX_TRANSPARENT
                    | _WS_EX_TOOLWINDOW)
        except Exception as e:
            log.debug("Click-through style failed: %s", e)

    def _tick(self) -> None:
        if not self._root:
            return
        self._drain()
        if self._root is None:  # quit processed
            return
        if self._visible:
            self._animate()
        self._root.after(_ANIM_MS, self._tick)

    def _animate(self) -> None:
        # bars shrink toward silence when no new level arrives
        self._levels = [v * _LEVEL_DECAY for v in self._levels]
        cw = int(self._bars.winfo_width()) or 200
        ch = int(self._bars.winfo_height()) or 34
        bw = max((cw - (_BAR_COUNT - 1) * 3) / _BAR_COUNT, 4)
        pulse = 0.75 + 0.25 * ((time.monotonic() - self._t0) % 0.9 / 0.9)
        for i, bar in enumerate(self._bar_ids):
            db = self._levels[i]
            frac = max(0.0, min(1.0, (db + 70.0) / 60.0))
            h = max(2.0, frac * (ch - 4)) * pulse
            x0 = i * (bw + 3)
            self._bars.coords(bar, x0, ch - h, x0 + bw, ch - 1)
        self._bars.update_idletasks()