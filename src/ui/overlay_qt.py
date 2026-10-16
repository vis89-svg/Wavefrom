"""Persistent dock overlay: three idle dots that morph into one box (PySide6).

A single always-on-top window anchored bottom-center, just above the taskbar:
  1. **Idle** -- three small pen-point dots (black, thin white border), always
     visible and click-through.
  2. **Live box** -- when a dictation starts the outer dots slide into the
     middle one, which stretches into a box: status dot + "Recording" (then
     "Transcribing" / "Cleaning up"), level bars, and a Stop button.
  3. **Review** -- the same box grows to show the final text with Polish /
     Send / Clipboard / X.

Closing reverses the morph: the box shrinks back into the three dots.
"""
from __future__ import annotations

import ctypes
import logging
import math
import threading
import time

from PySide6.QtCore import (QEasingCurve, QPointF, QRect, QRectF, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
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
_HWND_TOPMOST = -1
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001

_user32 = ctypes.WinDLL("user32", use_last_error=True)


# ── State helpers ─────────────────────────────────────────────────────────────
_STATE_LABELS = {
    "recording": "Recording",
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

_LEVEL_DECAY = 0.85
_BAR_COUNT = 14
_AUTO_DISMISS_MS = 15000

# ── Shape geometry ────────────────────────────────────────────────────────────
_BOX_WIDTH = 440
_BOX_RADIUS = 14.0
_DOT_PX = 8               # idle dot diameter (logical px; scales with Windows DPI)
_DOT_GAP_RATIO = 0.75     # gap between dots, relative to the diameter
_DOT_BORDER = 1.5         # white ring around each idle dot
_MERGE_PHASE = 0.35       # morph progress spent sliding the dots together
_MARGIN = 3               # antialiasing room around the painted shape
_TASKBAR_GAP = 14         # px between the shape and the taskbar
_OPEN_MS = 340
_CLOSE_MS = 280
_RESIZE_MS = 200

_DOT_FILL = QColor(0, 0, 0)
_BOX_FILL = QColor(21, 23, 28, 248)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _ease(t: float) -> float:
    """Smoothstep: gentle start and end for each morph phase."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def morph_shape(t: float, dot: float, box_w: float, box_h: float):
    """Shape of the overlay at morph progress `t` (0 = dots, 1 = box).

    Returns (kind, w, h, payload): for kind "dots", payload is the centre
    offset of the outer dots from the middle one; for "box", it is the corner
    radius. `w`/`h` are the shape's bounding size.
    """
    gap = dot * _DOT_GAP_RATIO
    if t < _MERGE_PHASE:
        a = _ease(t / _MERGE_PHASE)
        offset = (dot + gap) * (1 - a)
        return "dots", dot + 2 * offset, dot, offset
    b = _ease((t - _MERGE_PHASE) / (1 - _MERGE_PHASE))
    w = _lerp(dot, max(box_w, dot), b)
    h = _lerp(dot, max(box_h, dot), b)
    radius = _lerp(dot / 2, _BOX_RADIUS, b)
    return "box", w, h, radius


# ── Win32 helpers ─────────────────────────────────────────────────────────────
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


# ── Painted widgets ───────────────────────────────────────────────────────────
class WaveformBars(QWidget):
    """Custom-painted level bars driven by the engine's set_level(db) feed."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._levels: list[float] = [-70.0] * _BAR_COUNT
        self._t0 = time.monotonic()
        self.setFixedHeight(30)
        self.setMinimumWidth(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def push_level(self, db: float) -> None:
        self._levels.append(float(db))
        if len(self._levels) > _BAR_COUNT:
            self._levels = self._levels[-_BAR_COUNT:]

    def tick_decay(self) -> None:
        # Decay toward silence (-70 dB). Scaling the raw (negative) dB value
        # would instead push every bar toward 0 dB, i.e. full height.
        self._levels = [-70.0 + (v + 70.0) * _LEVEL_DECAY for v in self._levels]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        bw = max((w - (_BAR_COUNT - 1) * 3) / _BAR_COUNT, 4)
        pulse = 0.75 + 0.25 * ((time.monotonic() - self._t0) % 0.9 / 0.9)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(_STATE_COLORS["recording"])))
        for i, db in enumerate(self._levels):
            frac = max(0.0, min(1.0, (db + 70.0) / 60.0))
            bar_h = max(3.0, frac * (h - 4) * pulse)
            x0 = i * (bw + 3)
            p.drawRoundedRect(QRectF(x0, (h - bar_h) / 2, bw, bar_h), 2, 2)


class StatusDot(QWidget):
    """Round status light; pulses while recording."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self._color = QColor(_STATE_COLORS["idle"])
        self._pulse = False
        self._t0 = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self.update)

    def set_state(self, color: str, pulse: bool) -> None:
        self._color = QColor(color)
        self._pulse = pulse
        if pulse:
            self._t0 = time.monotonic()
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(self._color)
        if self._pulse:
            phase = (time.monotonic() - self._t0) % 1.2 / 1.2
            c.setAlphaF(0.55 + 0.45 * (0.5 + 0.5 * math.cos(phase * 2 * math.pi)))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(c))
        p.drawEllipse(QRectF(1, 1, 12, 12))


# ── Styles ────────────────────────────────────────────────────────────────────
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
_REVIEW_TEXT = """
QTextEdit {
    background: #0f1115; color: #e6e9ee; border: none;
    border-radius: 8px; padding: 6px; font: 12px 'Segoe UI';
}
"""
_HINT_STYLE = "color: #6a6e78; font: 10px 'Segoe UI'; background: transparent;"
_PREVIEW_STYLE = "color: #b9c0c9; font: 12px 'Segoe UI'; background: transparent;"


def _state_label_style(color: str) -> str:
    return (f"color: {color}; font: bold 13px 'Segoe UI'; "
            "background: transparent;")


# ── Box content ───────────────────────────────────────────────────────────────
class _BoxContent(QWidget):
    """Everything shown inside the box: a shared header, then either the
    live page (level bars + preview) or the review page (text + actions)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 14, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self._dot = StatusDot()
        head.addWidget(self._dot)
        self._state_lbl = QLabel("Idle")
        self._state_lbl.setStyleSheet(_state_label_style(_STATE_COLORS["idle"]))
        head.addWidget(self._state_lbl)
        head.addStretch()
        self._stop_btn = QPushButton("■ Stop")
        self._stop_btn.setStyleSheet(_BTN_STOP)
        self._stop_btn.setFixedHeight(26)
        head.addWidget(self._stop_btn)
        self._close_btn = QPushButton("X")
        self._close_btn.setStyleSheet(_BTN_CLOSE)
        self._close_btn.setFixedSize(32, 26)
        head.addWidget(self._close_btn)
        lay.addLayout(head)

        # Live page
        self._live = QWidget()
        live_lay = QVBoxLayout(self._live)
        live_lay.setContentsMargins(0, 0, 0, 0)
        live_lay.setSpacing(6)
        self._waveform = WaveformBars()
        live_lay.addWidget(self._waveform)
        self._preview = QLabel("")
        self._preview.setWordWrap(True)
        self._preview.setStyleSheet(_PREVIEW_STYLE)
        self._preview.setMaximumHeight(40)
        self._preview.hide()
        live_lay.addWidget(self._preview)
        self._live_hint = QLabel("")
        self._live_hint.setStyleSheet(_HINT_STYLE)
        live_lay.addWidget(self._live_hint)
        lay.addWidget(self._live)

        # Review page
        self._review = QWidget()
        rev_lay = QVBoxLayout(self._review)
        rev_lay.setContentsMargins(0, 0, 0, 0)
        rev_lay.setSpacing(8)
        self._txt = QTextEdit()
        self._txt.setReadOnly(True)
        self._txt.setStyleSheet(_REVIEW_TEXT)
        self._txt.setFixedHeight(120)
        rev_lay.addWidget(self._txt)
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(6)
        self._polish_btn = QPushButton("Polish")
        self._polish_btn.setStyleSheet(_BTN_POLISH)
        self._polish_btn.setFixedHeight(28)
        btn_row.addWidget(self._polish_btn)
        self._send_btn = QPushButton("Send")
        self._send_btn.setStyleSheet(_BTN_SEND)
        self._send_btn.setFixedHeight(28)
        btn_row.addWidget(self._send_btn)
        self._clip_btn = QPushButton("Clipboard")
        self._clip_btn.setStyleSheet(_BTN_CLIP)
        self._clip_btn.setFixedHeight(28)
        btn_row.addWidget(self._clip_btn)
        btn_row.addStretch()
        rev_lay.addLayout(btn_row)
        lay.addWidget(self._review)

        self.show_live()

    def show_live(self) -> None:
        self._review.hide()
        self._live.show()
        self._close_btn.hide()

    def show_review(self) -> None:
        self._live.hide()
        self._review.show()
        self._stop_btn.hide()
        self._close_btn.show()

    def set_status(self, text: str, color: str, pulse: bool = False) -> None:
        self._dot.set_state(color, pulse)
        self._state_lbl.setText(text)
        self._state_lbl.setStyleSheet(_state_label_style(color))

    def set_preview(self, text: str) -> None:
        text = (text or "").strip()
        if len(text) > 140:
            text = "…" + text[-139:]
        self._preview.setText(text)
        self._preview.setVisible(bool(text))

    def height_for_box(self) -> int:
        self.layout().activate()
        return max(self.layout().sizeHint().height(), 60)


# ── Main overlay widget ──────────────────────────────────────────────────────
class OverlayWindow(QWidget):
    """Persistent dock overlay.  All Qt work happens on the GUI thread.

    The window's own paintEvent draws the morphing shape (dots or box);
    `_BoxContent` sits on top of it and is only shown once the box is fully
    open. The window is resized every animation frame to the shape's bounds,
    anchored at bottom-center, so it never covers more of the screen than
    what's drawn.
    """

    _sig_state = Signal(str, str)
    _sig_level = Signal(float)
    _sig_polish_result = Signal(object, int)
    _sig_send_result = Signal(object, int)
    _sig_clipboard_result = Signal(object, int)

    def __init__(self) -> None:
        super().__init__(None)
        self._visible = False
        self._polishing = False
        self._session_id = 0
        self._hold_hotkey = ""
        self._live_hotkey = ""

        self._polish_callback = None
        self._send_callback = None
        self._clipboard_callback = None
        self._stop_callback = None

        # Morph state: t (0 = dots, 1 = box) and the box's current size.
        self._dot = _DOT_PX
        self._t = 0.0
        self._box_w = float(_BOX_WIDTH)
        self._box_h = 100.0
        self._anim: QVariantAnimation | None = None
        self._anim_from = (0.0, self._box_w, self._box_h)
        self._anim_to = (0.0, self._box_w, self._box_h)

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
        self._content = _BoxContent(self)
        self._content.hide()

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

        c = self._content
        c._stop_btn.clicked.connect(self._on_stop)
        c._polish_btn.clicked.connect(self._on_polish)
        c._send_btn.clicked.connect(self._on_send)
        c._clip_btn.clicked.connect(self._on_clipboard)
        c._close_btn.clicked.connect(self._on_close)

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
        text = f"Hold {hold} · Tap {live}"
        self._content._live_hint.setText(text)

    def start(self) -> None:
        """Show the idle dots at the dock position immediately."""
        self._visible = True
        self._t = 0.0
        self._content.hide()
        self._apply_geometry()
        self.show()
        self.raise_()
        self._force_topmost()
        self._make_click_through()

    def stop(self) -> None:
        """Collapse back to the idle dots."""
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

        if not self._visible:
            self._visible = True
            self.show()

        color = _STATE_COLORS.get(state, "#8a8f98")
        self._content.set_status(_STATE_LABELS.get(state, state), color,
                                 pulse=(state == "recording"))
        if state == "done":
            self._show_panel(text)
        else:
            self._content.set_preview(text if state != "recording" else "")
            self._show_live(state)

    def _show_live(self, state: str) -> None:
        """Open (or resize) the box to the live page."""
        if state == "recording":
            # A fresh dictation is starting -- any Polish/Send/Clipboard
            # result from a previous session that lands after this point is
            # stale and must not be allowed to overwrite this session's text.
            self._session_id += 1
        self._dismiss_timer.stop()
        self._content.show_live()
        # Level bars only mean something while the mic is live; afterwards
        # the space goes to the text preview.
        self._content._waveform.setVisible(state == "recording")

        if state == "recording":
            self._set_interactive(True)
            self._content._stop_btn.setEnabled(True)
            self._content._stop_btn.setText("■ Stop")
            self._content._stop_btn.show()
        else:
            self._make_click_through()
            self._content._stop_btn.hide()

        self._open_box()

    def _show_panel(self, text: str) -> None:
        """Grow the box to the review page."""
        self._content.show_review()
        # The prior state (e.g. "cleaning") leaves the window click-through;
        # the review panel's Polish/Send/Clipboard buttons need real clicks.
        self._set_interactive(True)

        self._content._txt.setPlainText((text or "").strip())
        self._polishing = False
        self._content._polish_btn.setEnabled(True)
        self._content._polish_btn.setText("Polish")
        self._content._send_btn.setEnabled(True)
        self._content._send_btn.setText("Send")
        self._open_box()
        self._dismiss_timer.start(_AUTO_DISMISS_MS)

    def _collapse_to_bar(self) -> None:
        """Shrink the box back into the three idle dots."""
        self._dismiss_timer.stop()
        self._content._dot.set_state(_STATE_COLORS["idle"], False)
        self._make_click_through()
        self._animate_to(0.0, self._box_w, self._box_h, _CLOSE_MS)

    # ── Morph animation ─────────────────────────────────────────────────
    def _open_box(self) -> None:
        """Morph to a fully open box sized for the current content."""
        h = float(self._content.height_for_box())
        duration = _OPEN_MS if self._t < 1.0 else _RESIZE_MS
        self._animate_to(1.0, float(_BOX_WIDTH), h, duration)

    def _animate_to(self, t: float, box_w: float, box_h: float,
                    duration: int) -> None:
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        start = (self._t, self._box_w, self._box_h)
        end = (t, box_w, box_h)
        if start == end:
            self._finish_anim()
            return
        # Content is only shown on a fully open box; during a pure resize of
        # an already-open box it stays visible and reflows with each frame.
        if not (start[0] == 1.0 and end[0] == 1.0):
            self._content.hide()
        self._anim_from, self._anim_to = start, end
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(duration)
        anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        anim.valueChanged.connect(self._on_anim_frame)
        anim.finished.connect(self._finish_anim)
        self._anim = anim
        anim.start()

    def _on_anim_frame(self, p) -> None:
        p = float(p)
        (t0, w0, h0), (t1, w1, h1) = self._anim_from, self._anim_to
        self._t = _lerp(t0, t1, p)
        self._box_w = _lerp(w0, w1, p)
        self._box_h = _lerp(h0, h1, p)
        self._apply_geometry()

    def _finish_anim(self) -> None:
        self._anim = None
        self._t, self._box_w, self._box_h = self._anim_to
        self._apply_geometry()
        if self._t >= 1.0:
            self._content.show()
            self._content.raise_()
        else:
            self._content.hide()
        self.raise_()
        self._force_topmost()

    # ── Geometry + painting ─────────────────────────────────────────────
    def _apply_geometry(self) -> None:
        """Resize/move the window to the current shape, bottom-center."""
        kind, w, h, _ = morph_shape(self._t, self._dot, self._box_w, self._box_h)
        win_w = int(math.ceil(w)) + 2 * _MARGIN
        win_h = int(math.ceil(h)) + 2 * _MARGIN
        screen = self.screen() or QApplication.primaryScreen()
        rect = screen.availableGeometry()
        cx = rect.left() + rect.width() / 2
        x = int(round(cx - win_w / 2))
        y = rect.bottom() + 1 - _TASKBAR_GAP - win_h
        # One setGeometry call (no separate resize + move) so the shape grows
        # upward from its anchor without a one-frame jump.
        self.setGeometry(QRect(x, max(rect.top(), y), win_w, win_h))
        if kind == "box":
            self._content.setGeometry(_MARGIN, _MARGIN, int(w), int(h))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        kind, w, h, payload = morph_shape(self._t, self._dot,
                                          self._box_w, self._box_h)
        cx = self.width() / 2
        bottom = self.height() - _MARGIN
        if kind == "dots":
            p.setPen(QPen(QColor(255, 255, 255), _DOT_BORDER))
            p.setBrush(QBrush(_DOT_FILL))
            r = (self._dot - _DOT_BORDER) / 2
            cy = bottom - self._dot / 2
            # Draw the outer dots first so the middle one sits on top as
            # they slide together.
            for dx in (-payload, payload, 0.0):
                p.drawEllipse(QPointF(cx + dx, cy), r, r)
            return
        # Box: fill and border fade from the dot look to the box look.
        b = (self._t - _MERGE_PHASE) / (1 - _MERGE_PHASE)
        b = max(0.0, min(1.0, b))
        fill = QColor(
            int(_lerp(_DOT_FILL.red(), _BOX_FILL.red(), b)),
            int(_lerp(_DOT_FILL.green(), _BOX_FILL.green(), b)),
            int(_lerp(_DOT_FILL.blue(), _BOX_FILL.blue(), b)),
            int(_lerp(255, _BOX_FILL.alpha(), b)),
        )
        border = QColor(255, 255, 255, int(_lerp(255, 70, b)))
        p.setPen(QPen(border, _lerp(_DOT_BORDER, 1.0, b)))
        p.setBrush(QBrush(fill))
        rect = QRectF(cx - w / 2 + 0.5, bottom - h + 0.5, w - 1, h - 1)
        p.drawRoundedRect(rect, payload, payload)

    # ── Result handlers (GUI thread) ────────────────────────────────────
    def _apply_polish_result(self, result, session_id: int) -> None:
        if session_id != self._session_id:
            log.debug("Discarding stale polish result from a previous dictation session")
            return
        self._polishing = False
        self._content._polish_btn.setEnabled(True)
        self._content._polish_btn.setText("Polish")
        if result:
            self._content._txt.setPlainText((result or "").strip())
            self._content.set_status("Polished", _STATE_COLORS["done"])
            self._content._send_btn.setEnabled(True)
        else:
            self._content.set_status("Polish failed", _STATE_COLORS["error"])
        # No auto-dismiss restart here: the user just deliberately asked to see
        # this result (unlike the automatic post-cleanup panel, where nobody
        # explicitly asked and auto-hiding if ignored is reasonable). Restarting
        # a 15s countdown here meant the panel could silently re-collapse while
        # the user's attention was on the target app watching the on-screen
        # text update, making a *successful* Polish look like it never updated.

    def _apply_send_result(self, text: str | None, session_id: int) -> None:
        if session_id != self._session_id:
            log.debug("Discarding stale send result from a previous dictation session")
            return
        self._polishing = False
        if text:
            self._content.set_status("Sent", _STATE_COLORS["done"])
        else:
            self._content.set_status("No polished text", _STATE_COLORS["error"])
        self._content._send_btn.setEnabled(True)
        self._content._send_btn.setText("Send")
        self._content._txt.setPlainText((text or "").strip())
        # No auto-dismiss restart: see _apply_polish_result.

    def _apply_clipboard_result(self, text: str | None, session_id: int) -> None:
        if session_id != self._session_id:
            log.debug("Discarding stale clipboard result from a previous dictation session")
            return
        self._polishing = False
        if text:
            self._content.set_status("Copied", _STATE_COLORS["done"])
        else:
            self._content.set_status("No polished text", _STATE_COLORS["error"])
        self._dismiss_timer.start(_AUTO_DISMISS_MS)

    # ── Level feed (GUI thread) ─────────────────────────────────────────
    def _on_level(self, db: float) -> None:
        self._content._waveform.push_level(db)
        self._content._waveform.tick_decay()

    # ── Button handlers ─────────────────────────────────────────────────
    def _on_polish(self) -> None:
        if self._polishing or self._polish_callback is None:
            return
        self._dismiss_timer.stop()
        self._polishing = True
        self._content._polish_btn.setEnabled(False)
        self._content._polish_btn.setText("Polishing…")
        session_id = self._session_id
        threading.Thread(
            target=self._run_polish, args=(session_id,),
            daemon=True, name="overlay-polish",
        ).start()

    def _run_polish(self, session_id: int) -> None:
        try:
            result = self._polish_callback()
        except Exception as e:
            log.warning("Polish pass failed: %s", e)
            result = None
        self._sig_polish_result.emit(result, session_id)

    def _on_send(self) -> None:
        if self._polishing or self._send_callback is None:
            return
        self._dismiss_timer.stop()
        self._polishing = True
        self._content._send_btn.setEnabled(False)
        self._content._send_btn.setText("Sending…")
        session_id = self._session_id
        threading.Thread(
            target=self._run_send, args=(session_id,),
            daemon=True, name="overlay-send",
        ).start()

    def _run_send(self, session_id: int) -> None:
        try:
            result = self._send_callback()
        except Exception as e:
            log.warning("Send pass failed: %s", e)
            result = None
        self._sig_send_result.emit(result, session_id)

    def _on_clipboard(self) -> None:
        if self._polishing or self._clipboard_callback is None:
            return
        self._dismiss_timer.stop()
        self._polishing = True
        session_id = self._session_id
        threading.Thread(
            target=self._run_clipboard, args=(session_id,),
            daemon=True, name="overlay-clipboard",
        ).start()

    def _run_clipboard(self, session_id: int) -> None:
        try:
            result = self._clipboard_callback()
        except Exception as e:
            log.warning("Clipboard pass failed: %s", e)
            result = None
        self._sig_clipboard_result.emit(result, session_id)

    def _on_stop(self) -> None:
        if self._stop_callback is None:
            return
        self._dismiss_timer.stop()
        self._content._stop_btn.setEnabled(False)
        self._content._stop_btn.setText("Stopping…")
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
