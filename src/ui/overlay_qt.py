"""Persistent dock-bar overlay: idle bar + waveform + review panel (PySide6).

Three visual modes in a single always-on-top window fixed at bottom-center:
  1. **Idle bar** — thin pill with waveform icon + "Dictation" label, always
     visible above the taskbar.
  2. **Live indicator** — expands in-place to show animated waveform, state
     label, and Stop button during recording / transcribing / cleaning.
  3. **Review panel** — expands in-place to show cleaned text with Polish /
     Send / Clipboard / X buttons when done.

The window never hides — it collapses back to the idle bar after dictation.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

# ── Win32 constants ──────────────────────────────────────────────────────────
_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_SPI_GETWORKAREA = 0x0030
_HWND_TOPMOST = -1
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001

_user32 = ctypes.WinDLL("user32", use_last_error=True)


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


# ── State helpers ─────────────────────────────────────────────────────────────
_STATE_LABELS = {
    "recording": "Recording\u2026",
    "transcribing": "Transcribing\u2026",
    "cleaning": "Cleaning up\u2026",
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

_LEVEL_DECAY = 0.85
_BAR_COUNT = 14
_AUTO_DISMISS_MS = 15000
_DOCK_MARGIN = 80  # px above the work-area bottom edge
_DOCK_WIDTH = 420
_IDLE_HEIGHT = 36
_PILL_MIN_HEIGHT = 90
_REVIEW_MIN_HEIGHT = 260


# ── Win32 helpers ─────────────────────────────────────────────────────────────
def _work_area() -> tuple[int, int, int, int] | None:
    rect = _RECT()
    if _user32.SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    return None


def _set_window_ex_style(hwnd: int, interactive: bool) -> None:
    """Apply WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, and
    toggle WS_EX_TRANSPARENT on/off for click-through."""
    try:
        GetWindowLongPtrW = _user32.GetWindowLongPtrW
        SetWindowLongPtrW = _user32.SetWindowLongPtrW
        style = GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
        if interactive:
            style = style & ~_WS_EX_TRANSPARENT
        else:
            style = style | _WS_EX_TRANSPARENT
        new_style = style | _WS_EX_LAYERED | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE
        SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, new_style)
    except Exception as e:
        log.debug("Click-through toggle failed: %s", e)


# ── Waveform bar widget ──────────────────────────────────────────────────────
class WaveformBars(QWidget):
    """Custom-painted level bars driven by the engine's set_level(db) feed."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._levels: list[float] = [0.0] * _BAR_COUNT
        self._t0 = time.monotonic()
        self.setMinimumHeight(34)
        self.setMinimumWidth(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def push_level(self, db: float) -> None:
        self._levels.append(float(db))
        if len(self._levels) > _BAR_COUNT:
            self._levels = self._levels[-_BAR_COUNT:]

    def tick_decay(self) -> None:
        self._levels = [v * _LEVEL_DECAY for v in self._levels]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        p.fillRect(0, 0, w, h, QColor("#181c21"))

        bw = max((w - (_BAR_COUNT - 1) * 3) / _BAR_COUNT, 4)
        pulse = 0.75 + 0.25 * ((time.monotonic() - self._t0) % 0.9 / 0.9)

        for i, db in enumerate(self._levels):
            frac = max(0.0, min(1.0, (db + 70.0) / 60.0))
            bar_h = max(2.0, frac * (h - 4)) * pulse
            x0 = i * (bw + 3)
            color = QColor(_STATE_COLORS.get("recording", "#30a46c"))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(color))
            p.drawRoundedRect(QRectF(x0, h - bar_h, bw, bar_h - 1), 2, 2)


# ── Styles ────────────────────────────────────────────────────────────────────
_BAR_STYLE = """
QWidget#bar {
    background-color: #20242a;
    border: 1px solid #3a4048;
    border-radius: 12px;
}
"""
_PILL_STYLE = """
QWidget#pill {
    background-color: #20242a;
    border: 1px solid #3a4048;
    border-radius: 14px;
}
"""
_BTN_BASE = (
    "QPushButton {{ "
    "  background: {bg}; color: #fff; border: none; border-radius: 6px; "
    "  padding: 0 {pad}px; font: 11px 'Segoe UI'; "
    "}}"
    "QPushButton:hover {{ background: {hover}; }}"
    "QPushButton:disabled {{ background: #3a3f48; color: #777; }}"
)
_BTN_STOP = _BTN_BASE.format(bg="#e5484d", hover="#ff5b60", pad=12)
_BTN_POLISH = _BTN_BASE.format(bg="#4a90d9", hover="#5ba0e0", pad=14)
_BTN_SEND = _BTN_BASE.format(bg="#5090d0", hover="#60a0e0", pad=14)
_BTN_CLIP = _BTN_BASE.format(bg="#5090d0", hover="#60a0e0", pad=12)
_BTN_CLOSE = (
    "QPushButton { background: #3a4048; color: #d7dbe0; border: none; "
    "  border-radius: 6px; padding: 0 10px; font: 11px 'Segoe UI'; }"
    "QPushButton:hover { background: #e5484d; color: #fff; }"
)

_REVIEW_STYLE = """
QWidget#review {
    background-color: #20242a;
    border: 1px solid #3a4048;
    border-radius: 14px;
}
QTextEdit {
    background: #181c21; color: #e6e9ee; border: none;
    border-radius: 6px; padding: 6px; font: 10px 'Segoe UI';
}
"""


# ── Main overlay widget ──────────────────────────────────────────────────────
class OverlayWindow(QWidget):
    """Persistent dock-bar overlay.  All Qt work happens on the GUI thread.

    Three visual modes in one fixed-position window:
      - **idle bar**: thin pill, always visible, click-through.
      - **live indicator** (recording/transcribing/cleaning): expanded pill
        with animated waveform, click-through.
      - **review panel** ("done"): expanded with cleaned text and action
        buttons.  Interactive (no click-through).
    """

    _sig_state = Signal(str, str)
    _sig_level = Signal(float)
    _sig_polish_result = Signal(object)
    _sig_send_result = Signal(object)
    _sig_clipboard_result = Signal(object)

    def __init__(self) -> None:
        super().__init__(None)
        self._visible = False
        self._polishing = False
        self._panel_shown = False
        self._hold_hotkey = ""
        self._live_hotkey = ""

        self._polish_callback = None
        self._send_callback = None
        self._clipboard_callback = None
        self._stop_callback = None

        self._build_ui()
        self._connect_signals()

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self._on_auto_dismiss)

    # ── UI construction ──────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._root_lay = QVBoxLayout(self)
        self._root_lay.setContentsMargins(0, 0, 0, 0)
        self._root_lay.setSpacing(0)

        # ── Idle bar (always visible when app is running) ────────────────
        self._bar = QWidget(self)
        self._bar.setObjectName("bar")
        self._bar.setStyleSheet(_BAR_STYLE)
        bar_lay = QHBoxLayout(self._bar)
        bar_lay.setContentsMargins(14, 4, 14, 4)
        bar_lay.setSpacing(8)

        self._bar_icon = QLabel("\u25b6")  # small waveform-like icon
        self._bar_icon.setStyleSheet(
            "color: #30a46c; font: 11px 'Segoe UI';"
        )
        bar_lay.addWidget(self._bar_icon)

        self._bar_label = QLabel("Dictation")
        self._bar_label.setStyleSheet(
            "color: #8a8f98; font: 10px 'Segoe UI';"
        )
        bar_lay.addWidget(self._bar_label)
        bar_lay.addStretch()

        self._bar_status = QLabel("")
        self._bar_status.setStyleSheet(
            "color: #5a5e68; font: 9px 'Segoe UI';"
        )
        bar_lay.addWidget(self._bar_status)

        # ── Pill container (live view) ───────────────────────────────────
        self._pill = QWidget(self)
        self._pill.setObjectName("pill")
        self._pill.setStyleSheet(_PILL_STYLE)
        pill_lay = QVBoxLayout(self._pill)
        pill_lay.setContentsMargins(14, 8, 14, 8)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        self._dot = QLabel("\u25cf")
        self._dot.setFixedWidth(14)
        self._dot.setStyleSheet("color: #8a8f98; font: 12px 'Segoe UI';")
        head.addWidget(self._dot)

        self._state_lbl = QLabel("Idle")
        self._state_lbl.setStyleSheet(
            "color: #d7dbe0; font: 11px 'Segoe UI'; font-weight: bold;"
        )
        head.addWidget(self._state_lbl)
        head.addStretch()

        self._stop_btn = QPushButton("\u25a0 Stop")
        self._stop_btn.setObjectName("stopBtn")
        self._stop_btn.setStyleSheet(_BTN_STOP)
        self._stop_btn.setFixedHeight(26)
        self._stop_btn.clicked.connect(self._on_stop)
        self._stop_btn.hide()
        head.addWidget(self._stop_btn)
        pill_lay.addLayout(head)

        self._waveform = WaveformBars(self._pill)
        pill_lay.addWidget(self._waveform)

        self._hotkey_lbl = QLabel("")
        self._hotkey_lbl.setStyleSheet("color: #6a6e78; font: 9px 'Segoe UI';")
        pill_lay.addWidget(self._hotkey_lbl)

        # ── Review container (done view) ─────────────────────────────────
        self._review = QWidget(self)
        self._review.setObjectName("review")
        self._review.setStyleSheet(_REVIEW_STYLE)
        rev_lay = QVBoxLayout(self._review)
        rev_lay.setContentsMargins(10, 8, 10, 8)

        self._hotkey_lbl_review = QLabel("")
        self._hotkey_lbl_review.setStyleSheet(
            "color: #6a6e78; font: 9px 'Segoe UI';"
        )
        rev_lay.addWidget(self._hotkey_lbl_review)

        self._txt = QTextEdit()
        self._txt.setReadOnly(True)
        self._txt.setMinimumHeight(120)
        self._txt.setMaximumHeight(200)
        rev_lay.addWidget(self._txt)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(6)

        self._polish_btn = QPushButton("Polish")
        self._polish_btn.setStyleSheet(_BTN_POLISH)
        self._polish_btn.setFixedHeight(28)
        self._polish_btn.clicked.connect(self._on_polish)
        btn_row.addWidget(self._polish_btn)

        self._send_btn = QPushButton("Send")
        self._send_btn.setStyleSheet(_BTN_SEND)
        self._send_btn.setFixedHeight(28)
        self._send_btn.clicked.connect(self._on_send)
        btn_row.addWidget(self._send_btn)

        self._clip_btn = QPushButton("Clipboard")
        self._clip_btn.setStyleSheet(_BTN_CLIP)
        self._clip_btn.setFixedHeight(28)
        self._clip_btn.clicked.connect(self._on_clipboard)
        btn_row.addWidget(self._clip_btn)

        btn_row.addStretch()

        self._close_btn = QPushButton("X")
        self._close_btn.setStyleSheet(_BTN_CLOSE)
        self._close_btn.setFixedHeight(28)
        self._close_btn.setFixedWidth(36)
        self._close_btn.clicked.connect(self._on_close)
        btn_row.addWidget(self._close_btn)

        rev_lay.addLayout(btn_row)

        self._root_lay.addWidget(self._bar)
        self._root_lay.addWidget(self._pill)
        self._root_lay.addWidget(self._review)

        # Initially: bar visible, pill & review hidden
        self._bar.setVisible(True)
        self._pill.setVisible(False)
        self._review.setVisible(False)

        # Shadow
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setOffset(0, 3)
        shadow.setColor(QColor(0, 0, 0, 140))
        self.setGraphicsEffect(shadow)

    def _connect_signals(self) -> None:
        self._sig_state.connect(
            self._apply_state, Qt.ConnectionType.QueuedConnection
        )
        self._sig_level.connect(
            self._on_level, Qt.ConnectionType.QueuedConnection
        )
        self._sig_polish_result.connect(
            self._apply_polish_result, Qt.ConnectionType.QueuedConnection
        )
        self._sig_send_result.connect(
            self._apply_send_result, Qt.ConnectionType.QueuedConnection
        )
        self._sig_clipboard_result.connect(
            self._apply_clipboard_result, Qt.ConnectionType.QueuedConnection
        )

    # ── Public API (called from ANY thread) ─────────────────────────────
    def set_state(self, state: str, text: str = "") -> None:
        self._sig_state.emit(state, text)

    def set_level(self, db: float) -> None:
        self._sig_level.emit(float(db))

    def set_polish_callback(self, callback) -> None:
        self._polish_callback = callback

    def set_send_callback(self, callback) -> None:
        self._send_callback = callback

    def set_clipboard_callback(self, callback) -> None:
        self._clipboard_callback = callback

    def set_stop_callback(self, callback) -> None:
        self._stop_callback = callback

    def set_hotkeys(self, hold: str, live: str) -> None:
        self._hold_hotkey = hold
        self._live_hotkey = live
        text = f"Hold {hold} \u00b7 Tap {live}"
        self._hotkey_lbl.setText(text)
        self._hotkey_lbl_review.setText(text)

    def start(self) -> None:
        """Show the idle bar at the dock position immediately."""
        self._visible = True
        self._bar.setVisible(True)
        self._pill.setVisible(False)
        self._review.setVisible(False)
        self._panel_shown = False
        self._place_dock(_IDLE_HEIGHT)
        self.show()
        self.raise_()
        self._force_topmost()
        self._make_click_through()

    def stop(self) -> None:
        """Collapse back to the idle bar (don't hide the window)."""
        self._collapse_to_bar()

    # ── Click-through ───────────────────────────────────────────────────
    def _set_interactive(self, interactive: bool) -> None:
        hwnd = int(self.winId())
        _set_window_ex_style(hwnd, interactive)

    def _make_click_through(self) -> None:
        self._set_interactive(False)

    # ── State handling (runs on GUI thread via signal) ──────────────────
    def _apply_state(self, state: str, text: str) -> None:
        if state == "idle":
            self._collapse_to_bar()
            return

        # Ensure window is visible for any active state
        if not self._visible:
            self._visible = True
            self.show()
            self.raise_()
            self._force_topmost()

        color = _STATE_COLORS.get(state, "#8a8f98")
        self._dot.setStyleSheet(f"color: {color}; font: 12px 'Segoe UI';")
        self._state_lbl.setText(_STATE_LABELS.get(state, state))
        self._state_lbl.setStyleSheet(
            f"color: {color}; font: 11px 'Segoe UI'; font-weight: bold;"
        )
        self._bar_status.setText(_STATE_LABELS.get(state, state))
        self._bar_status.setStyleSheet(
            f"color: {color}; font: 9px 'Segoe UI';"
        )

        if state == "done":
            self._show_panel(text)
        else:
            self._show_live(state, text)

    def _show_live(self, state: str, text: str) -> None:
        """Expand to the waveform pill for recording / transcribing / cleaning."""
        self._dismiss_timer.stop()
        if self._panel_shown:
            self._review.hide()
            self._panel_shown = False

        self._bar.hide()
        self._pill.show()

        if state == "recording":
            self._set_interactive(True)
            self._stop_btn.show()
        else:
            self._make_click_through()
            self._stop_btn.hide()

        self._place_dock(_PILL_MIN_HEIGHT)
        self.raise_()
        self._force_topmost()

    def _show_panel(self, text: str) -> None:
        """Expand to the review panel for the done state."""
        if not self._panel_shown:
            self._bar.hide()
            self._pill.hide()
            self._review.show()
            self._panel_shown = True

        self._txt.setPlainText((text or "").strip())
        self._polishing = False
        self._polish_btn.setEnabled(True)
        self._polish_btn.setText("Polish")
        self._send_btn.setEnabled(True)
        self._send_btn.setText("Send")
        self._place_dock(_REVIEW_MIN_HEIGHT)
        self.raise_()
        self._force_topmost()
        self._set_interactive(True)
        self._dismiss_timer.start(_AUTO_DISMISS_MS)

    def _collapse_to_bar(self) -> None:
        """Collapse back to the thin idle bar."""
        self._dismiss_timer.stop()
        self._review.hide()
        self._pill.hide()
        self._bar.show()
        self._bar_status.setText("")
        self._panel_shown = False
        self._make_click_through()
        self._place_dock(_IDLE_HEIGHT)

    # ── Result handlers (GUI thread) ────────────────────────────────────
    def _apply_polish_result(self, result) -> None:
        self._polishing = False
        if result:
            self._polish_btn.setEnabled(True)
            self._polish_btn.setText("Polish")
            self._txt.setPlainText((result or "").strip())
            self._state_lbl.setText("Polished")
            self._state_lbl.setStyleSheet(
                f"color: {_STATE_COLORS['done']}; font: 11px 'Segoe UI'; font-weight: bold;"
            )
            self._bar_status.setText("Polished")
            self._bar_status.setStyleSheet(
                f"color: {_STATE_COLORS['done']}; font: 9px 'Segoe UI';"
            )
            self._send_btn.setEnabled(True)
        else:
            self._polish_btn.setEnabled(True)
            self._polish_btn.setText("Polish")
            self._state_lbl.setText("Polish failed")
            self._state_lbl.setStyleSheet(
                "color: #e5484d; font: 11px 'Segoe UI'; font-weight: bold;"
            )
            self._bar_status.setText("Polish failed")
            self._bar_status.setStyleSheet(
                "color: #e5484d; font: 9px 'Segoe UI';"
            )
        if self._panel_shown:
            self._dismiss_timer.start(_AUTO_DISMISS_MS)

    def _apply_send_result(self, text: str | None) -> None:
        self._polishing = False
        if text:
            self._state_lbl.setText("Sent")
            self._state_lbl.setStyleSheet(
                f"color: {_STATE_COLORS['done']}; font: 11px 'Segoe UI'; font-weight: bold;"
            )
            self._bar_status.setText("Sent")
            self._bar_status.setStyleSheet(
                f"color: {_STATE_COLORS['done']}; font: 9px 'Segoe UI';"
            )
        else:
            self._state_lbl.setText("No polished text")
            self._state_lbl.setStyleSheet(
                "color: #e5484d; font: 11px 'Segoe UI'; font-weight: bold;"
            )
            self._bar_status.setText("")
        self._send_btn.setEnabled(True)
        self._send_btn.setText("Send")
        if self._panel_shown:
            self._txt.setPlainText((text or "").strip())
            self._dismiss_timer.start(_AUTO_DISMISS_MS)

    def _apply_clipboard_result(self, text: str | None) -> None:
        self._polishing = False
        if text:
            self._state_lbl.setText("Copied")
            self._state_lbl.setStyleSheet(
                f"color: {_STATE_COLORS['done']}; font: 11px 'Segoe UI'; font-weight: bold;"
            )
            self._bar_status.setText("Copied")
            self._bar_status.setStyleSheet(
                f"color: {_STATE_COLORS['done']}; font: 9px 'Segoe UI';"
            )
        else:
            self._state_lbl.setText("No polished text")
            self._state_lbl.setStyleSheet(
                "color: #e5484d; font: 11px 'Segoe UI'; font-weight: bold;"
            )
        if self._panel_shown:
            self._dismiss_timer.start(_AUTO_DISMISS_MS)

    # ── Level feed (GUI thread) ─────────────────────────────────────────
    def _on_level(self, db: float) -> None:
        self._waveform.push_level(db)
        self._waveform.tick_decay()

    # ── Button handlers ─────────────────────────────────────────────────
    def _on_polish(self) -> None:
        if self._polishing or self._polish_callback is None:
            return
        self._dismiss_timer.stop()
        self._polishing = True
        self._polish_btn.setEnabled(False)
        self._polish_btn.setText("Polishing\u2026")
        threading.Thread(
            target=self._run_polish, daemon=True, name="overlay-polish"
        ).start()

    def _run_polish(self) -> None:
        try:
            result = self._polish_callback()
        except Exception as e:
            log.warning("Polish pass failed: %s", e)
            result = None
        self._sig_polish_result.emit(result)

    def _on_send(self) -> None:
        if self._polishing or self._send_callback is None:
            return
        self._dismiss_timer.stop()
        self._polishing = True
        self._send_btn.setEnabled(False)
        self._send_btn.setText("Sending\u2026")
        threading.Thread(
            target=self._run_send, daemon=True, name="overlay-send"
        ).start()

    def _run_send(self) -> None:
        try:
            result = self._send_callback()
        except Exception as e:
            log.warning("Send pass failed: %s", e)
            result = None
        self._sig_send_result.emit(result)

    def _on_clipboard(self) -> None:
        if self._polishing or self._clipboard_callback is None:
            return
        self._dismiss_timer.stop()
        self._polishing = True
        threading.Thread(
            target=self._run_clipboard, daemon=True, name="overlay-clipboard"
        ).start()

    def _run_clipboard(self) -> None:
        try:
            result = self._clipboard_callback()
        except Exception as e:
            log.warning("Clipboard pass failed: %s", e)
            result = None
        self._sig_clipboard_result.emit(result)

    def _on_stop(self) -> None:
        if self._stop_callback is None:
            return
        self._dismiss_timer.stop()
        self._stop_btn.setEnabled(False)
        self._stop_btn.setText("Stopping\u2026")
        threading.Thread(
            target=self._stop_callback, daemon=True, name="overlay-stop"
        ).start()

    def _on_close(self) -> None:
        self._collapse_to_bar()

    def _on_auto_dismiss(self) -> None:
        self._collapse_to_bar()

    # ── Force topmost via Win32 ─────────────────────────────────────────
    def _force_topmost(self) -> None:
        try:
            hwnd = int(self.winId())
            _user32.SetWindowPos(
                hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                _SWP_NOMOVE | _SWP_NOSIZE,
            )
        except Exception as e:
            log.debug("_force_topmost failed: %s", e)

    # ── Dock placement (fixed bottom-center, always) ───────────────────
    def _place_dock(self, height: int) -> None:
        """Position the window at bottom-center, ``height`` px above the
        work-area bottom edge.  The window is resized to fit the current
        content (bar / pill / review)."""
        area = _work_area()
        if area:
            left, top, right, bottom = area
        else:
            scr = QApplication.primaryScreen().virtualGeometry()
            left, top, right, bottom = (
                scr.left(), scr.top(), scr.right(), scr.bottom(),
            )
        w = _DOCK_WIDTH
        h = max(height, _IDLE_HEIGHT)
        px = left + (right - left - w) // 2
        py = bottom - h - _DOCK_MARGIN
        if py < top:
            py = top + _DOCK_MARGIN
        self.setFixedSize(w, h)
        self.move(px, py)
