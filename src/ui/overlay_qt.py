"""Persistent dock overlay: idle dots -> slim pill -> result card (PySide6).

A single always-on-top window anchored bottom-center, just above the taskbar:
  1. **Idle** -- three small pen-point dots (black, muted brown ring), always
     visible and click-through.
  2. **Pill** -- when a dictation starts the outer dots slide into the middle
     one, which stretches into a slim pill: status dot, thin live waveform
     and a timer. A small X (stop) appears on hover. While transcribing /
     cleaning up, the waveform turns into a soft shimmer.
  3. **Card** -- once the text is ready, a small card appears above the pill
     with the full text (scrolls when long) and quiet icon buttons (Polish /
     Send / Copy).
     It fades out after 10 s unless the mouse is over it.

Closing reverses the morph: the pill shrinks back into the three dots.
"""
from __future__ import annotations

import ctypes
import logging
import math
import threading
import time

from PySide6.QtCore import (QEasingCurve, QElapsedTimer, QPointF,
                            QPropertyAnimation, QRect, QRectF, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import QBrush, QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
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
_STATE_COLORS = {
    "recording": "#e5484d",
    "transcribing": "#f5a623",
    "cleaning": "#4a90d9",
    "done": "#30a46c",
    "error": "#e5484d",
    "idle": "#8a8f98",
}

_LEVEL_DECAY = 0.85
_BAR_COUNT = 22
_AUTO_DISMISS_MS = 10000
_CARD_FADE_IN_MS = 180
_CARD_FADE_OUT_MS = 300

# ── Shape geometry ────────────────────────────────────────────────────────────
_PILL_WIDTH = 200
_PILL_HEIGHT = 40
_DOT_PX = 8               # idle dot diameter (logical px; scales with Windows DPI)
_DOT_GAP_RATIO = 0.75     # gap between dots, relative to the diameter
_DOT_BORDER = 1.5         # ring around each idle dot
_MERGE_PHASE = 0.35       # morph progress spent sliding the dots together
_MARGIN = 3               # antialiasing room around the painted pill
_TASKBAR_GAP = 14         # px between the pill and the taskbar
_OPEN_MS = 340
_CLOSE_MS = 280

_CARD_WIDTH = 360
_CARD_SHADOW = 10         # room around the card for its soft shadow
_CARD_GAP = 4             # space between the card's shadow box and the pill
_CARD_RADIUS = 14.0
_CARD_TEXT_MAX_H = 170    # ~8 lines of text before the card scrolls

_DOT_FILL = QColor(0, 0, 0)
_DOT_RING = QColor("#7A5230")
_PILL_FILL = QColor(17, 18, 22, 245)
_PILL_BORDER = QColor(255, 255, 255, 40)
_CARD_FILL = QColor(28, 30, 36, 250)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _ease(t: float) -> float:
    """Smoothstep: gentle start and end for each morph phase."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def morph_shape(t: float, dot: float, box_w: float, box_h: float):
    """Shape of the pill at morph progress `t` (0 = dots, 1 = pill).

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
    radius = min(w, h) / 2  # stays fully rounded: dot -> capsule
    return "box", w, h, radius


def format_elapsed(ms: int) -> str:
    secs = max(0, int(ms // 1000))
    return f"{secs // 60}:{secs % 60:02d}"


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
    """Thin white level bars fed by set_level(db); in "shimmer" mode the bars
    rest low and a soft highlight sweeps across them (busy, not listening)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._levels: list[float] = [-70.0] * _BAR_COUNT
        self._shimmer = False
        self._t0 = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self.update)
        self.setFixedHeight(20)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_shimmer(self, on: bool) -> None:
        self._shimmer = on
        self._t0 = time.monotonic()
        if on:
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def reset(self) -> None:
        self._levels = [-70.0] * _BAR_COUNT
        self.update()

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
        w, h = self.width(), self.height()
        n = _BAR_COUNT
        step = w / n
        bw = max(2.0, step * 0.45)
        p.setPen(Qt.PenStyle.NoPen)
        if self._shimmer:
            # A soft highlight sweeping left to right across resting bars.
            pos = ((time.monotonic() - self._t0) % 1.4) / 1.4 * (n + 6) - 3
            for i in range(n):
                glow = math.exp(-((i - pos) ** 2) / 6.0)
                c = QColor(255, 255, 255, int(60 + 170 * glow))
                bar_h = 3.0 + 5.0 * glow
                p.setBrush(QBrush(c))
                p.drawRoundedRect(QRectF(i * step, (h - bar_h) / 2, bw, bar_h),
                                  bw / 2, bw / 2)
            return
        p.setBrush(QBrush(QColor(255, 255, 255, 225)))
        for i, db in enumerate(self._levels):
            frac = max(0.0, min(1.0, (db + 70.0) / 60.0))
            bar_h = max(3.0, frac * h)
            p.drawRoundedRect(QRectF(i * step, (h - bar_h) / 2, bw, bar_h),
                              bw / 2, bw / 2)


class StatusDot(QWidget):
    """Small round status light; pulses while recording."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(10, 10)
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
        p.drawEllipse(QRectF(1, 1, 8, 8))


# ── Styles ────────────────────────────────────────────────────────────────────
_TIMER_STYLE = ("color: #c9ced6; font: 11px 'Segoe UI'; background: transparent;")
_X_BTN = (
    "QPushButton { background: transparent; color: #8a8f98; border: none; "
    "  border-radius: 9px; font: 11px 'Segoe UI'; padding: 0; }"
    "QPushButton:hover { background: #2e323a; color: #ffffff; }"
)
_ICON_BTN = (
    "QPushButton { background: transparent; color: #9aa0a9; border: none; "
    "  border-radius: 8px; padding: 0 10px; font: 12px 'Segoe UI'; }"
    "QPushButton:hover { background: #2e323a; color: #ffffff; }"
    "QPushButton:disabled { color: #555a63; }"
)
_PREVIEW_STYLE = "color: #e6e9ee; font: 13px 'Segoe UI'; background: transparent;"
_SCROLLBAR_W = 6
_SCROLL_STYLE = (
    "QScrollArea { background: transparent; border: none; }"
    "QScrollArea > QWidget > QWidget { background: transparent; }"
    f"QScrollBar:vertical {{ background: transparent; width: {_SCROLLBAR_W}px; margin: 0; }}"
    "QScrollBar::handle:vertical { background: #3a3f48; border-radius: 3px; min-height: 24px; }"
    "QScrollBar::handle:vertical:hover { background: #555b66; }"
    "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
    "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }"
)
_CAPTION_STYLE = "font: 11px 'Segoe UI'; background: transparent;"


# ── Pill content ──────────────────────────────────────────────────────────────
class _PillContent(QWidget):
    """Status dot + waveform + timer, with an X shown on hover."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(15, 0, 10, 0)
        lay.setSpacing(10)
        self._dot = StatusDot()
        lay.addWidget(self._dot, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._waveform = WaveformBars()
        lay.addWidget(self._waveform, stretch=1,
                      alignment=Qt.AlignmentFlag.AlignVCenter)
        self._timer_lbl = QLabel("0:00")
        self._timer_lbl.setStyleSheet(_TIMER_STYLE)
        self._timer_lbl.setFixedWidth(30)
        self._timer_lbl.setAlignment(Qt.AlignmentFlag.AlignRight
                                     | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._timer_lbl)
        self._x_btn = QPushButton("✕")
        self._x_btn.setStyleSheet(_X_BTN)
        self._x_btn.setFixedSize(18, 18)
        self._x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # Keep the X's slot in the layout while hidden, so the timer doesn't
        # jump sideways every time the mouse enters/leaves.
        sp = self._x_btn.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        self._x_btn.setSizePolicy(sp)
        self._x_btn.hide()
        lay.addWidget(self._x_btn, alignment=Qt.AlignmentFlag.AlignVCenter)


# ── Result card ───────────────────────────────────────────────────────────────
class _Card(QWidget):
    """Two-line preview of the dictation plus quiet icon actions."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._full_text = ""
        s = _CARD_SHADOW
        lay = QVBoxLayout(self)
        lay.setContentsMargins(s + 14, s + 11, s + 10, s + 8)
        lay.setSpacing(6)
        # Full text, wrapped; the card grows with it up to _CARD_TEXT_MAX_H
        # and scrolls beyond that.
        self._preview = QLabel("")
        self._preview.setStyleSheet(_PREVIEW_STYLE)
        self._preview.setWordWrap(True)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._scroll = QScrollArea()
        self._scroll.setWidget(self._preview)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(_SCROLL_STYLE)
        self._scroll.viewport().setAutoFillBackground(False)
        lay.addWidget(self._scroll)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        self._caption = QLabel("")
        self._caption.setStyleSheet(_CAPTION_STYLE)
        row.addWidget(self._caption)
        row.addStretch()
        self._polish_btn = self._icon_btn("✨  Polish", "Rewrite more cleanly")
        self._send_btn = self._icon_btn("↵  Send", "Type the text again at the cursor")
        self._clip_btn = self._icon_btn("⧉  Copy", "Copy to clipboard")
        for b in (self._polish_btn, self._send_btn, self._clip_btn):
            row.addWidget(b)
        lay.addLayout(row)

        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(1.0)
        self.setGraphicsEffect(self._opacity)

    @staticmethod
    def _icon_btn(text: str, tip: str) -> QPushButton:
        b = QPushButton(text)
        b.setStyleSheet(_ICON_BTN)
        b.setFixedHeight(26)
        b.setToolTip(tip)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        return b

    def set_text(self, text: str) -> None:
        self._full_text = (text or "").strip()
        self._preview.setText(self._full_text)
        inner = _CARD_WIDTH - 2 * _CARD_SHADOW - 24 - _SCROLLBAR_W
        self._preview.ensurePolished()  # stylesheet font, not the default
        fm = QFontMetrics(self._preview.font())
        text_h = fm.boundingRect(QRect(0, 0, inner, 100000),
                                 int(Qt.TextFlag.TextWordWrap),
                                 self._full_text or " ").height()
        self._scroll.setFixedHeight(max(fm.height(), min(text_h + 4, _CARD_TEXT_MAX_H)))
        self._scroll.verticalScrollBar().setValue(0)

    def set_caption(self, text: str, color: str = "#8a8f98") -> None:
        self._caption.setText(text)
        self._caption.setStyleSheet(f"color: {color}; {_CAPTION_STYLE}")

    def total_height(self) -> int:
        self.layout().activate()
        return max(self.layout().sizeHint().height(), 70)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = _CARD_SHADOW
        body = QRectF(s, s, self.width() - 2 * s, self.height() - 2 * s)
        # Soft shadow: stacked, slightly offset translucent outlines.
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(s, 0, -1):
            a = int(34 * (1 - i / s) ** 2)
            p.setPen(QPen(QColor(0, 0, 0, a), 1.0))
            p.drawRoundedRect(body.adjusted(-i, -i + 2, i, i + 2),
                              _CARD_RADIUS + i, _CARD_RADIUS + i)
        p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
        p.setBrush(QBrush(_CARD_FILL))
        p.drawRoundedRect(body, _CARD_RADIUS, _CARD_RADIUS)


# ── Main overlay widget ──────────────────────────────────────────────────────
class OverlayWindow(QWidget):
    """Persistent dock overlay.  All Qt work happens on the GUI thread.

    The window's paintEvent draws the morphing pill; `_PillContent` sits on
    the pill once it's fully open, and `_Card` sits above it when a result is
    shown. The window is resized to exactly what's drawn, anchored at
    bottom-center, so it never covers more of the screen than it shows.
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
        self._state = "idle"
        self._pill_state = "idle"  # last state the pill was set up for
        self._hovered = False
        # After an explicit Polish/Send the card stays until closed: the
        # user's eyes are usually on the target app watching the text change,
        # and an auto-hide there made a successful action look like a no-op.
        self._sticky = False

        self._polish_callback = None
        self._send_callback = None
        self._clipboard_callback = None
        self._stop_callback = None

        # Morph state: t (0 = dots, 1 = pill).
        self._dot = _DOT_PX
        self._t = 0.0
        self._anim: QVariantAnimation | None = None
        self._anim_from = 0.0
        self._anim_to = 0.0
        self._card_on = False
        self._card_anim: QPropertyAnimation | None = None
        self._elapsed = QElapsedTimer()

        self._build_ui()
        self._connect_signals()

        self._clock = QTimer(self)
        self._clock.setInterval(250)
        self._clock.timeout.connect(self._tick_clock)

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
        self._pill = _PillContent(self)
        self._pill.hide()
        self._card = _Card(self)
        self._card.hide()

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

        self._pill._x_btn.clicked.connect(self._on_x)
        self._card._polish_btn.clicked.connect(self._on_polish)
        self._card._send_btn.clicked.connect(self._on_send)
        self._card._clip_btn.clicked.connect(self._on_clipboard)

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
        self._pill.setToolTip(f"Hold {hold} · Tap {live}")

    def start(self) -> None:
        """Show the idle dots at the dock position immediately."""
        self._visible = True
        self._t = 0.0
        self._pill.hide()
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

        self._state = state
        if state == "done":
            self._show_panel(text)
        else:
            self._show_live(state)

    def _show_live(self, state: str) -> None:
        """Open the pill: live waveform while recording, shimmer after."""
        pill = self._pill
        pill._x_btn.setEnabled(True)
        color = _STATE_COLORS.get(state, "#8a8f98")
        if state == "recording" and self._pill_state == "recording":
            # The engine re-sends "recording" after every slice; that's the
            # same dictation, not a new one, so keep the timer and waveform.
            return
        self._pill_state = state
        if state == "recording":
            # A fresh dictation is starting -- any Polish/Send/Clipboard
            # result from a previous session that lands after this point is
            # stale and must not be allowed to overwrite this session's text.
            self._session_id += 1
            self._hide_card()
            self._elapsed.start()
            self._clock.start()
            pill._timer_lbl.setText("0:00")
            pill._waveform.reset()
            pill._waveform.set_shimmer(False)
            pill._x_btn.setToolTip("Stop recording")
            pill._dot.set_state(color, pulse=True)
            self._set_interactive(True)
        else:
            self._clock.stop()
            pill._waveform.set_shimmer(True)
            pill._dot.set_state(color, pulse=False)
            pill._x_btn.hide()
            self._make_click_through()
        self._dismiss_timer.stop()
        self._open_pill()

    def _show_panel(self, text: str) -> None:
        """Pill shows done; the result card appears above it."""
        self._pill_state = "done"
        self._clock.stop()
        pill = self._pill
        pill._waveform.set_shimmer(False)
        pill._waveform.reset()
        pill._dot.set_state(_STATE_COLORS["done"], pulse=False)
        pill._x_btn.setEnabled(True)
        pill._x_btn.setToolTip("Close")
        # The prior state (e.g. "cleaning") leaves the window click-through;
        # the card's Polish/Send/Copy buttons need real clicks.
        self._set_interactive(True)

        card = self._card
        card.set_text(text)
        card.set_caption("")
        self._polishing = False
        self._sticky = False
        card._polish_btn.setEnabled(True)
        card._polish_btn.setText("✨  Polish")
        card._send_btn.setEnabled(True)
        card._send_btn.setText("↵  Send")
        self._open_pill()
        self._show_card()
        self._restart_dismiss()

    def _collapse_to_bar(self) -> None:
        """Hide the card and shrink the pill back into the three dots."""
        self._state = "idle"
        self._pill_state = "idle"
        self._dismiss_timer.stop()
        self._clock.stop()
        self._pill._x_btn.hide()
        self._pill._waveform.set_shimmer(False)
        self._pill._dot.set_state(_STATE_COLORS["idle"], False)
        self._hide_card()
        self._make_click_through()
        self._animate_to(0.0, _CLOSE_MS)

    def _tick_clock(self) -> None:
        if self._elapsed.isValid():
            self._pill._timer_lbl.setText(format_elapsed(self._elapsed.elapsed()))

    # ── Card show / fade ────────────────────────────────────────────────
    def _show_card(self) -> None:
        self._stop_card_anim()
        self._card_on = True
        self._card._opacity.setOpacity(0.0)
        self._card.show()
        self._apply_geometry()
        self._fade_card(1.0, _CARD_FADE_IN_MS)

    def _hide_card(self) -> None:
        self._stop_card_anim()
        if not self._card_on:
            return
        self._card_on = False
        self._card.hide()
        self._apply_geometry()

    def _stop_card_anim(self) -> None:
        if self._card_anim is not None:
            self._card_anim.stop()
            self._card_anim = None

    def _fade_card(self, to: float, ms: int, then=None) -> None:
        self._stop_card_anim()
        anim = QPropertyAnimation(self._card._opacity, b"opacity", self)
        anim.setStartValue(self._card._opacity.opacity())
        anim.setEndValue(to)
        anim.setDuration(ms)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        if then is not None:
            anim.finished.connect(then)
        self._card_anim = anim
        anim.start()

    def _restart_dismiss(self) -> None:
        if self._card_on and not self._hovered and not self._sticky:
            self._dismiss_timer.start(_AUTO_DISMISS_MS)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        if self._state in ("recording", "done"):
            self._pill._x_btn.show()
        if self._card_on:
            # Hovering keeps the card: cancel the countdown and any fade.
            self._dismiss_timer.stop()
            self._fade_card(1.0, _CARD_FADE_IN_MS)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self._pill._x_btn.hide()
        self._restart_dismiss()
        super().leaveEvent(event)

    # ── Morph animation ─────────────────────────────────────────────────
    def _open_pill(self) -> None:
        self._animate_to(1.0, _OPEN_MS)

    def _animate_to(self, t: float, duration: int) -> None:
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        if self._t == t:
            self._anim_to = t
            self._finish_anim()
            return
        self._pill.hide()
        self._anim_from, self._anim_to = self._t, t
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
        self._t = _lerp(self._anim_from, self._anim_to, float(p))
        self._apply_geometry()

    def _finish_anim(self) -> None:
        self._anim = None
        self._t = self._anim_to
        self._apply_geometry()
        self._pill.setVisible(self._t >= 1.0)
        self.raise_()
        self._force_topmost()

    # ── Geometry + painting ─────────────────────────────────────────────
    def _apply_geometry(self) -> None:
        """Resize/move the window to the pill (+ card), bottom-center."""
        _, w, h, _ = morph_shape(self._t, self._dot, _PILL_WIDTH, _PILL_HEIGHT)
        pill_w = int(math.ceil(w)) + 2 * _MARGIN
        pill_h = int(math.ceil(h)) + 2 * _MARGIN
        win_w, win_h = pill_w, pill_h
        card_h = 0
        if self._card_on:
            card_h = self._card.total_height()
            win_w = max(win_w, _CARD_WIDTH)
            win_h = pill_h + _CARD_GAP + card_h
        screen = self.screen() or QApplication.primaryScreen()
        rect = screen.availableGeometry()
        cx = rect.left() + rect.width() / 2
        x = int(round(cx - win_w / 2))
        # The bottom of the pill's painted shape sits _TASKBAR_GAP above the
        # work area; the margin is transparent padding below it.
        y = rect.bottom() + 1 - _TASKBAR_GAP + _MARGIN - win_h
        # One setGeometry call (no separate resize + move) so the shape grows
        # upward from its anchor without a one-frame jump.
        self.setGeometry(QRect(x, max(rect.top(), y), win_w, win_h))
        if self._card_on:
            self._card.setGeometry((win_w - _CARD_WIDTH) // 2, 0, _CARD_WIDTH, card_h)
        self._pill.setGeometry(int((win_w - w) / 2), win_h - _MARGIN - int(h),
                               int(w), int(h))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        kind, w, h, payload = morph_shape(self._t, self._dot,
                                          _PILL_WIDTH, _PILL_HEIGHT)
        cx = self.width() / 2
        bottom = self.height() - _MARGIN
        if kind == "dots":
            p.setPen(QPen(_DOT_RING, _DOT_BORDER))
            p.setBrush(QBrush(_DOT_FILL))
            r = (self._dot - _DOT_BORDER) / 2
            cy = bottom - self._dot / 2
            # Draw the outer dots first so the middle one sits on top as
            # they slide together.
            for dx in (-payload, payload, 0.0):
                p.drawEllipse(QPointF(cx + dx, cy), r, r)
            return
        # Pill: fill and ring fade from the dot look to the pill look.
        b = (self._t - _MERGE_PHASE) / (1 - _MERGE_PHASE)
        b = max(0.0, min(1.0, b))
        fill = QColor(
            int(_lerp(_DOT_FILL.red(), _PILL_FILL.red(), b)),
            int(_lerp(_DOT_FILL.green(), _PILL_FILL.green(), b)),
            int(_lerp(_DOT_FILL.blue(), _PILL_FILL.blue(), b)),
            int(_lerp(255, _PILL_FILL.alpha(), b)),
        )
        ring = QColor(
            int(_lerp(_DOT_RING.red(), _PILL_BORDER.red(), b)),
            int(_lerp(_DOT_RING.green(), _PILL_BORDER.green(), b)),
            int(_lerp(_DOT_RING.blue(), _PILL_BORDER.blue(), b)),
            int(_lerp(255, _PILL_BORDER.alpha(), b)),
        )
        p.setPen(QPen(ring, _lerp(_DOT_BORDER, 1.0, b)))
        p.setBrush(QBrush(fill))
        rect = QRectF(cx - w / 2 + 0.5, bottom - h + 0.5, w - 1, h - 1)
        p.drawRoundedRect(rect, payload, payload)

    # ── Result handlers (GUI thread) ────────────────────────────────────
    def _apply_polish_result(self, result, session_id: int) -> None:
        if session_id != self._session_id:
            log.debug("Discarding stale polish result from a previous dictation session")
            return
        self._polishing = False
        card = self._card
        card._polish_btn.setEnabled(True)
        card._polish_btn.setText("✨  Polish")
        if result:
            card.set_text(result)
            card.set_caption("Polished", _STATE_COLORS["done"])
            card._send_btn.setEnabled(True)
            self._apply_geometry()
        else:
            card.set_caption("Polish failed", _STATE_COLORS["error"])

    def _apply_send_result(self, text: str | None, session_id: int) -> None:
        if session_id != self._session_id:
            log.debug("Discarding stale send result from a previous dictation session")
            return
        self._polishing = False
        card = self._card
        if text:
            card.set_caption("Sent", _STATE_COLORS["done"])
            card.set_text(text)
            self._apply_geometry()
        else:
            card.set_caption("Nothing to send", _STATE_COLORS["error"])
        card._send_btn.setEnabled(True)
        card._send_btn.setText("↵  Send")

    def _apply_clipboard_result(self, text: str | None, session_id: int) -> None:
        if session_id != self._session_id:
            log.debug("Discarding stale clipboard result from a previous dictation session")
            return
        self._polishing = False
        if text:
            self._card.set_caption("Copied", _STATE_COLORS["done"])
        else:
            self._card.set_caption("Nothing to copy", _STATE_COLORS["error"])
        self._restart_dismiss()

    # ── Level feed (GUI thread) ─────────────────────────────────────────
    def _on_level(self, db: float) -> None:
        self._pill._waveform.push_level(db)
        self._pill._waveform.tick_decay()

    # ── Button handlers ─────────────────────────────────────────────────
    def _on_polish(self) -> None:
        if self._polishing or self._polish_callback is None:
            return
        self._dismiss_timer.stop()
        self._polishing = True
        self._sticky = True
        self._card._polish_btn.setEnabled(False)
        self._card._polish_btn.setText("✨  Polishing…")
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
        self._sticky = True
        self._card._send_btn.setEnabled(False)
        self._card._send_btn.setText("↵  Sending…")
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

    def _on_x(self) -> None:
        """Pill X: stop while recording, otherwise close."""
        if self._state == "recording":
            self._on_stop()
        else:
            self._on_close()

    def _on_stop(self) -> None:
        if self._stop_callback is None:
            return
        self._dismiss_timer.stop()
        # Re-enabled by the next state change (transcribing / done / idle).
        self._pill._x_btn.setEnabled(False)
        threading.Thread(
            target=self._stop_callback, daemon=True, name="overlay-stop"
        ).start()

    def _on_close(self) -> None:
        self._collapse_to_bar()

    def _on_auto_dismiss(self) -> None:
        if not self._card_on or self._hovered:
            return
        self._fade_card(0.0, _CARD_FADE_OUT_MS, then=self._collapse_to_bar)

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
