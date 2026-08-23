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

_SPI_GETWORKAREA = 0x0030


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _work_area() -> tuple[int, int, int, int] | None:
    """Visible desktop area excluding the taskbar, or None on failure."""
    rect = _RECT()
    if _user32.SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    return None


def _clamp_pos(x: int, y: int, w: int, h: int,
               left: int, top: int, right: int, bottom: int,
               gap: int = 14, margin: int = 8,
               offset_x: int = _OFFSET_X, offset_y: int = _OFFSET_Y
               ) -> tuple[int, int]:
    """Clamp a w×h panel into the work area, preferring above (x, y).

    The panel sits with its bottom `gap` pixels above the cursor when there is
    room, otherwise just below it. In all cases it is fully inside
    [left+margin, right-margin] x [top+margin, bottom-margin] so the whole
    panel (including the button row) stays on-screen.
    """
    px = min(max(x + offset_x, left + margin),
             max(right - w - margin, left + margin))
    py = y - h - gap
    if py < top + margin:
        py = y + offset_y
    py = min(max(py, top + margin),
             max(bottom - h - margin, top + margin))
    return px, py


def _cursor_pos() -> tuple[int, int]:
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    pt = POINT()
    if _user32.GetCursorPos(ctypes.byref(pt)):
        return pt.x, pt.y
    return 0, 0


class OverlayWindow:
    """Thread-safe overlay. All Tk work happens on its own daemon thread.

    Two visual modes:
      - live indicator (recording/transcribing/cleaning): click-through waveform
        + short text preview near the cursor, mirrors the "Flow Bar".
      - review panel ("done"): stays open showing the full cleaned text with a
        Polish button and an X (close) button. Click-through is disabled so the
        buttons are clickable; it closes on X or when a new dictation starts.
    """

    def __init__(self):
        self._q: queue.Queue[tuple] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._root: tk.Tk | None = None
        self._visible = False
        self._levels: list[float] = [0.0] * _BAR_COUNT
        self._t0 = time.monotonic()
        self._polish_callback = None
        self._polishing = False
        self._panel_shown = False

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

    def set_polish_callback(self, callback) -> None:
        """callback() -> polished text or None. Called off the Tk thread."""
        self._polish_callback = callback

    def set_send_callback(self, callback) -> None:
        """callback() -> polished text or None. Called off the Tk thread."""
        self._send_callback = callback

    def set_clipboard_callback(self, callback) -> None:
        """callback() -> polished text or None. Called off the Tk thread."""
        self._clipboard_callback = callback

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

        # Review panel: full cleaned text + Polish / X buttons (shown on "done").
        self._panel = tk.Frame(frame, bg=frame["bg"])
        self._txt = tk.Text(
            self._panel, height=8, width=40, wrap="word", font=("Segoe UI", 9),
            bg="#181c21", fg="#e6e9ee", insertbackground="#e6e9ee",
            relief="flat", padx=8, pady=6)
        self._txt_scroll = tk.Scrollbar(self._panel, orient="vertical",
                                        command=self._txt.yview)
        self._txt.configure(yscrollcommand=self._txt_scroll.set)
        self._txt.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=(2, 4))
        self._txt_scroll.grid(row=0, column=1, sticky="ns", pady=(2, 4))
        self._txt.config(state="disabled")

        btn_row = tk.Frame(self._panel, bg=frame["bg"])
        btn_row.grid(row=1, column=0, columnspan=2, sticky="ew",
                     padx=10, pady=(0, 8))
        self._polish_btn = tk.Button(
            btn_row, text="Polish", font=("Segoe UI", 9, "bold"),
            bg="#4a90d9", fg="white", activebackground="#5ba0e0",
            activeforeground="white", relief="flat", padx=12,
            command=self._on_polish)
        self._polish_btn.pack(side="left")
        self._send_btn = tk.Button(
            btn_row, text="Send", font=("Segoe UI", 9, "bold"),
            bg="#70a0e0", fg="white", activebackground="#80b0f0",
            activeforeground="white", relief="flat", padx=12,
            command=self._on_send, state="disabled")
        self._send_btn.pack(side="left")
        self._clipboard_btn = tk.Button(
            btn_row, text="Clipboard", font=("Segoe UI", 9, "bold"),
            bg="#70a0e0", fg="white", activebackground="#80b0f0",
            activeforeground="white", relief="flat", padx=10,
            command=self._on_clipboard)
        self._clipboard_btn.pack(side="left")
        self._close_btn = tk.Button(
            btn_row, text="X", font=("Segoe UI", 9, "bold"),
            bg="#3a4048", fg="#d7dbe0", activebackground="#e5484d",
            activeforeground="white", relief="flat", padx=8,
            command=self._on_close)
        self._close_btn.pack(side="right")

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
            try:
                if kind == "level":
                    self._levels.pop(0)
                    self._levels.append(float(payload))
                elif kind == "state":
                    state, text = payload
                    self._apply_state(state, text)
                elif kind == "polish_result":
                    self._apply_polish_result(payload)
                elif kind == "send_result":
                    self._apply_send_result(payload)
                elif kind == "clipboard_result":
                    self._apply_clipboard_result(payload)
            except Exception:
                log.exception("Overlay message %r failed", kind)

    def _apply_state(self, state: str, text: str) -> None:
        if not self._root:
            return
        if state == "idle":
            if self._visible:
                self._root.withdraw()
                self._visible = False
            return
        if not self._visible:
            self._place_near_cursor()
            self._visible = True
        color = _STATE_COLORS.get(state, "#8a8f98")
        self._state_lbl.config(text=_STATE_LABELS.get(state, state),
                               fg=color)
        self._dot.config(bg=color)
        if state == "done":
            self._show_panel(text)
        else:
            self._show_live(text)

    def _show_live(self, text: str) -> None:
        # Live indicator mode: click-through, waveform + short preview.
        if self._panel_shown:
            self._panel.pack_forget()
            self._panel_shown = False
            self._preview_lbl.pack(fill="x", padx=10, pady=(2, 8))
        self._make_click_through()
        preview = (text or "").strip()
        if len(preview) > _PREVIEW_CHARS:
            preview = "…" + preview[-_PREVIEW_CHARS:]
        self._preview_lbl.config(text=preview)

    def _show_panel(self, text: str) -> None:
        # Review panel mode: full text + Polish/X, interactive (no click-through),
        # stays open until X is clicked or a new dictation starts.
        if not self._panel_shown:
            self._preview_lbl.pack_forget()
            self._panel.pack(fill="both", expand=True, padx=4, pady=(0, 4))
            self._panel_shown = True
        self._txt.config(state="normal")
        self._txt.delete("1.0", "end")
        self._txt.insert("1.0", (text or "").strip())
        self._txt.config(state="disabled")
        self._polishing = False
        self._polish_btn.config(state="normal", text="Polish")
        self._place_review_panel()
        self._set_interactive(True)

    def _apply_polish_result(self, result) -> None:
        if not self._root or not self._panel_shown:
            return
        self._polishing = False
        if result:
            self._polish_btn.config(state="normal", text="Polish")
            self._txt.config(state="normal")
            self._txt.delete("1.0", "end")
            self._txt.insert("1.0", (result or "").strip())
            self._txt.config(state="disabled")
            self._state_lbl.config(text="Polished", fg=_STATE_COLORS["done"])
            self._send_btn.config(state="normal")  # Enable Send button
        else:
            self._polish_btn.config(state="normal", text="Polish")
            self._state_lbl.config(text="Polish failed", fg="#e5484d")

    def _apply_send_result(self, text: str | None) -> None:
        """Handle send result from the queue."""
        if not self._root or not self._panel_shown:
            return
        self._polishing = False
        if text:
            log.info("Send: polished text sent (%d chars)", len(text))
            self._state_lbl.config(text="Sent", fg=_STATE_COLORS["done"])
        else:
            log.info("Send: no polished text yet")
            self._state_lbl.config(text="No polished text", fg="#e5484d")
        self._send_btn.config(state="normal", text="Send")
        # If we're in panel mode, also update the text display
        if self._panel_shown:
            self._txt.config(state="normal")
            self._txt.delete("1.0", "end")
            self._txt.insert("1.0", (text or "").strip())
            self._txt.config(state="disabled")

    def _apply_clipboard_result(self, text: str | None) -> None:
        """Handle clipboard result from the queue."""
        if not self._root or not self._panel_shown:
            return
        self._polishing = False
        if text:
            log.info("Clipboard: copied polished text (%d chars)", len(text))
            self._state_lbl.config(text="Copied", fg=_STATE_COLORS["done"])
        else:
            log.info("Clipboard: no polished text yet")
            self._state_lbl.config(text="No polished text", fg="#e5484d")

    def _on_polish(self) -> None:
        if self._polishing:
            return
        if self._polish_callback is None:
            self._state_lbl.config(text="Polish unavailable", fg="#e5484d")
            return
        self._polishing = True
        self._polish_btn.config(state="disabled", text="Polishing…")
        threading.Thread(target=self._run_polish, daemon=True,
                         name="overlay-polish").start()

    def _run_polish(self) -> None:
        try:
            result = self._polish_callback()
        except Exception as e:
            log.warning("Polish pass failed: %s", e)
            result = None
        if self._root:
            self._q.put(("polish_result", result))

    def _on_send(self) -> None:
        if self._polishing:
            return
        if self._send_callback is None:
            self._state_lbl.config(text="Send unavailable", fg="#e5484d")
            return
        self._polishing = True
        self._send_btn.config(state="disabled", text="Sending…")
        threading.Thread(target=self._run_send, daemon=True,
                         name="overlay-send").start()

    def _run_send(self) -> None:
        try:
            result = self._send_callback()
        except Exception as e:
            log.warning("Send pass failed: %s", e)
            result = None
        if self._root:
            self._q.put(("send_result", result))

    def _on_clipboard(self) -> None:
        if self._polishing:
            return
        if self._clipboard_callback is None:
            self._state_lbl.config(text="Clipboard unavailable", fg="#e5484d")
            return
        self._polishing = True
        threading.Thread(target=self._run_clipboard, daemon=True,
                         name="overlay-clipboard").start()

    def _run_clipboard(self) -> None:
        try:
            result = self._clipboard_callback()
        except Exception as e:
            log.warning("Clipboard pass failed: %s", e)
            result = None
        if self._root:
            self._q.put(("clipboard_result", result))

    def _on_close(self) -> None:
        if self._root:
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

    def _place_review_panel(self) -> None:
        """Place the review panel so the whole window (buttons included) is on
        screen, preferring above the cursor. Uses the final requested size that
        already includes the panel — unlike _place_near_cursor, which sizes to
        the small live indicator and would push the button row off the bottom
        of the screen when dictating near it."""
        if not self._root:
            return
        x, y = _cursor_pos()
        self._root.update_idletasks()
        w = self._root.winfo_reqwidth()
        h = self._root.winfo_reqheight()
        area = _work_area()
        if area is None:
            left, top = 0, 0
            right = self._root.winfo_screenwidth()
            bottom = self._root.winfo_screenheight()
        else:
            left, top, right, bottom = area
        px, py = _clamp_pos(x, y, w, h, left, top, right, bottom)
        self._root.geometry(f"+{px}+{py}")
        self._root.deiconify()

    def _make_click_through(self) -> None:
        self._set_interactive(False)

    def _set_interactive(self, interactive: bool) -> None:
        """Toggle whether the window receives mouse clicks.

        Live indicator mode is click-through (WS_EX_TRANSPARENT) so it never
        blocks the app underneath. The review panel clears that flag so the
        Polish / X buttons are clickable.
        """
        try:
            hwnd = _user32.GetParent(self._root.winfo_id())
            if not hwnd:
                return
            SetWindowLongPtrW = _user32.SetWindowLongPtrW
            style = SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, 0)
            if not style:
                return
            if interactive:
                style = style & ~_WS_EX_TRANSPARENT
            else:
                style = style | _WS_EX_TRANSPARENT
            SetWindowLongPtrW(
                hwnd, _GWL_EXSTYLE,
                style | _WS_EX_LAYERED | _WS_EX_TOOLWINDOW)
        except Exception as e:
            log.debug("Click-through toggle failed: %s", e)

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