"""Persistent dock overlay: idle dots -> one box that changes shape (PySide6).

A single always-on-top window anchored bottom-center, just above the taskbar:
  1. **Idle** -- three small pen-point dots (black, ash grey ring), always
     visible and click-through. Hovering pulls a mic out of the right dot.
  2. **Recording** -- the dots merge and stretch into a compact rounded box
     (about 5.25 x 1.35 cm): status dot, live waveform and timer; an X
     (stop)
     appears on hover.
  3. **Busy** -- the same box shows a spinning ring with Transcribing /
     Cleaning up / Updating.
  4. **Done** -- the same box doubles to 10.5 x 2.7 cm to show the text
     (scrolls when long) with Polish / Send / Copy, an expand button (16 x 6 cm,
     remembered) and close. It shrinks back to the dots after 10 s unless
     the mouse is over it.

Sizes are in centimetres, converted from the screen's real dimensions, so
the box measures the same with a ruler on any display.
"""
from __future__ import annotations

import ctypes
import logging
import math
import threading
import time

from PySide6.QtCore import (QEasingCurve, QElapsedTimer, QPointF, QRect,
                            QRectF, QSize, Qt, QTimer, QVariantAnimation,
                            Signal)
from PySide6.QtGui import (QBrush, QColor, QCursor, QFontMetrics, QPainter,
                           QPainterPath, QPen)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
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
    "updating": "#c8744f",
    "done": "#30a46c",
    "error": "#e5484d",
    "idle": "#8a8f98",
}
_BUSY_LABELS = {
    "transcribing": "Transcribing…",
    "cleaning": "Cleaning up…",
    "updating": "Updating…",
}

_LEVEL_DECAY = 0.85
_BAR_COUNT = 18
_AUTO_DISMISS_MS = 10000
_NOTICE_COLOR = "#d9a441"  # warnings shown in the box (always worded)

# ── Box geometry (centimetres, converted per screen) ─────────────────────────
_TEXT_CM = 10.5           # text box width
_TEXT_H_CM = 2.7          # text box height
_REC_CM = 5.25            # recording / busy box width
_REC_H_CM = 1.35          # recording / busy box height
_WIDE_CM = 16.0           # expanded box width
_WIDE_H_CM = 6.0          # expanded box height
_STRIP_CM = 0.9           # button strip height in the expanded box
_COMPACT_STRIP_CM = 0.62  # slimmer strip if the text box is under 2 cm tall
_RADIUS = 16.0            # corner radius of the box (px)

_DOT_PX = 8               # idle dot diameter (logical px; scales with Windows DPI)
_DOT_GAP_RATIO = 0.75     # gap between dots, relative to the diameter
_DOT_BORDER = 1.5         # ring around each idle dot
_MERGE_PHASE = 0.35       # morph progress spent sliding the dots together
_MARGIN = 3               # antialiasing room around the painted box
_TASKBAR_GAP = 14         # px between the box and the taskbar
_OPEN_MS = 340
_CLOSE_MS = 280
_RESIZE_MS = 200

# Hover mic: a droplet that pulls out of the right-hand dot.
_MIC_D = 22               # mic button diameter
_MIC_GAP = 8              # space between the right dot and the settled mic
_MIC_OUT_MS = 460
_MIC_IN_MS = 300
_NECK_SNAP = 0.55         # mic progress at which the liquid neck snaps
_HOVER_POLL_MS = 50       # idle dots are click-through, so poll the cursor
_HOVER_PAD = 8            # how close the cursor must get to wake the mic
_MIC_ICON = QColor("#8a8a8a")
_MIC_ICON_HOT = QColor("#d6d6d6")
_MIC_FILL_HOT = QColor(26, 26, 26)

_DOT_FILL = QColor(0, 0, 0)
_DOT_RING = QColor("#8A8A8A")  # ash grey
_BOX_FILL = QColor(26, 26, 26, 250)
_BOX_BORDER = QColor(255, 255, 255, 34)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _ease(t: float) -> float:
    """Smoothstep: gentle start and end for each morph phase."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def px_per_cm(screen) -> float:
    """Logical pixels per centimetre on `screen` (falls back to 96 dpi).

    Computed from the screen's size in logical pixels and its physical width
    in millimetres. (Qt's physicalDotsPerInch() is already in logical pixels,
    so dividing it by the scale factor again made everything ~20% too small
    on scaled displays.)
    """
    ppc = 96 / 2.54
    try:
        mm = float(screen.physicalSize().width())
        px = float(screen.geometry().width())
        if mm >= 50.0 and px > 0:
            candidate = px / (mm / 10.0)
            if 15.0 <= candidate <= 120.0:
                ppc = candidate
    except Exception:
        pass
    return max(25.0, min(90.0, ppc))


def morph_shape(t: float, dot: float, box_w: float, box_h: float):
    """Shape at morph progress `t` (0 = dots, 1 = box of box_w x box_h).

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
    radius = min(_lerp(dot / 2, _RADIUS, b), w / 2, h / 2)
    return "box", w, h, radius


def _ease_out_back(t: float, overshoot: float = 1.0) -> float:
    """Ease out with a small overshoot, so the mic 'lands' like a drop."""
    t = max(0.0, min(1.0, t))
    c1 = 1.70158 * overshoot
    c3 = c1 + 1
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def mic_drop(m: float, dot: float, mic_d: float = _MIC_D):
    """Mic droplet at progress `m` (0 = inside the right dot, 1 = settled).

    Returns (dx, r, neck): the droplet's centre offset from the right dot's
    centre, its radius, and the half-width of the liquid neck joining them
    (0 once it has snapped).
    """
    travel = dot / 2 + _MIC_GAP + mic_d / 2
    dx = travel * _ease_out_back(m, 0.6)
    r = _lerp(dot / 2, mic_d / 2, _ease(min(1.0, m / 0.8)))
    neck = 0.0 if m >= _NECK_SNAP else dot * 0.4 * (1 - m / _NECK_SNAP)
    return dx, r, neck


def idle_half_width(dot: float, mic_d: float = _MIC_D) -> float:
    """Half the idle canvas width: dots centred, room for the mic on the
    right (mirrored on the left so the dots stay centred on screen)."""
    offset = dot * (1 + _DOT_GAP_RATIO)
    return offset + dot / 2 + _MIC_GAP + mic_d + 4  # +4: landing overshoot


def draw_mic_icon(p: QPainter, c: QPointF, r: float, color: QColor) -> None:
    """Standard microphone line icon inside a circle of radius `r` at `c`:
    capsule head, U-shaped holder, stem and base."""
    s = r / 11.0
    pen = QPen(color, max(1.0, 1.4 * s))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.save()
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    head_cy = c.y() - 1.8 * s
    p.drawRoundedRect(QRectF(c.x() - 2.6 * s, head_cy - 4.4 * s, 5.2 * s, 8.8 * s),
                      2.6 * s, 2.6 * s)
    u = QRectF(c.x() - 4.6 * s, head_cy - 3.2 * s, 9.2 * s, 9.2 * s)
    p.drawArc(u, 180 * 16, 180 * 16)
    stem_top = u.bottom()
    p.drawLine(QPointF(c.x(), stem_top), QPointF(c.x(), stem_top + 2.2 * s))
    p.drawLine(QPointF(c.x() - 2.6 * s, stem_top + 2.2 * s),
               QPointF(c.x() + 2.6 * s, stem_top + 2.2 * s))
    p.restore()


def format_elapsed(ms: int) -> str:
    secs = max(0, int(ms // 1000))
    return f"{secs // 60}:{secs % 60:02d}"


# Remembered "expanded" choice (patched in tests so they never touch it).
def load_expanded_pref() -> bool:
    try:
        from PySide6.QtCore import QSettings
        return bool(QSettings("Waveform", "Waveform").value(
            "overlay/expanded", False, type=bool))
    except Exception:
        return False


def save_expanded_pref(expanded: bool) -> None:
    try:
        from PySide6.QtCore import QSettings
        QSettings("Waveform", "Waveform").setValue("overlay/expanded", expanded)
    except Exception as e:
        log.debug("Could not save overlay width preference: %s", e)


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
    """Thin white level bars fed by set_level(db)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._levels: list[float] = [-70.0] * _BAR_COUNT
        self.setFixedHeight(18)
        self.setMinimumWidth(24)

    def set_height(self, h: int) -> None:
        self.setFixedHeight(h)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

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
        step = w / _BAR_COUNT
        bw = max(2.0, step * 0.45)
        p.setPen(Qt.PenStyle.NoPen)
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
        d = self.width() - 2
        p.drawEllipse(QRectF(1, 1, d, d))


class Spinner(QWidget):
    """Rotating arc: the 'buffering' ring shown while text is processed."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self._color = QColor(_STATE_COLORS["transcribing"])
        self._t0 = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self.update)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def start(self) -> None:
        self._t0 = time.monotonic()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    @property
    def spinning(self) -> bool:
        return self._timer.isActive()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        size = self.width()
        stroke = max(2.0, size / 7.0)
        r = QRectF(stroke, stroke, size - 2 * stroke, size - 2 * stroke)
        track = QColor(255, 255, 255, 40)
        p.setPen(QPen(track, stroke))
        p.drawEllipse(r)
        pen = QPen(self._color, stroke)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        angle = int(((time.monotonic() - self._t0) % 0.9) / 0.9 * 360)
        p.drawArc(r, (90 - angle) * 16, -100 * 16)


# ── Styles ────────────────────────────────────────────────────────────────────
_SMALL_TEXT = "color: #c9ced6; font: 11px 'Inter'; background: transparent;"
_ICON_ONLY_BTN = (
    "QPushButton { background: transparent; color: #9aa0a9; border: none; "
    "  border-radius: 7px; padding: 0 6px; font: 12px 'Inter'; }"
    "QPushButton:hover { background: #2a2a2a; color: #ffffff; }"
    "QPushButton:disabled { color: #555a63; }"
)
_TEXT_STYLE = "color: #ececec; font: 13px 'Inter'; background: transparent;"
_STATUS_STYLE = "font: 11px 'Inter'; background: transparent;"
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
_BODY_PAD_X = 14
_BODY_PAD_TOP = 12
_BODY_PAD_BOTTOM = 8


def _icon_button(icon: str, tip: str, size: int = 14) -> QPushButton:
    from src.ui.icons import button_icon
    b = QPushButton()
    b.setIcon(button_icon(icon, size, "#9aa0a9", "#ffffff"))
    b.setIconSize(QSize(size, size))
    b.setStyleSheet(_ICON_ONLY_BTN)
    b.setToolTip(tip)
    b.setFixedHeight(26)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


# ── Box content ───────────────────────────────────────────────────────────────
class _BoxContent(QWidget):
    """Everything inside the box: an optional text body on top, then the
    bottom strip, whose contents depend on the mode (rec / busy / done)."""

    def __init__(self, strip_h: int, rec_h: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._strip_h = strip_h
        self._rec_h = rec_h
        self._full_text = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Body: text + status line (done only)
        self._body = QWidget()
        body = QVBoxLayout(self._body)
        self._body_lay = body
        body.setContentsMargins(_BODY_PAD_X, _BODY_PAD_TOP, _BODY_PAD_X - 4,
                                _BODY_PAD_BOTTOM)
        body.setSpacing(6)
        self._text = QLabel("")
        self._text.setStyleSheet(_TEXT_STYLE)
        self._text.setWordWrap(True)
        self._text.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._scroll = QScrollArea()
        self._scroll.setWidget(self._text)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(_SCROLL_STYLE)
        self._scroll.viewport().setAutoFillBackground(False)
        body.addWidget(self._scroll)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(_STATUS_STYLE)
        self._status.hide()
        body.addWidget(self._status)
        lay.addWidget(self._body)

        self._divider = QFrame()
        self._divider.setFixedHeight(1)
        self._divider.setStyleSheet("background: #2a2a2a; border: none;")
        lay.addWidget(self._divider)

        # Bottom strip
        self._strip = QWidget()
        self._strip.setFixedHeight(strip_h)
        row = QHBoxLayout(self._strip)
        self._row = row
        row.setContentsMargins(12, 0, 8, 0)
        row.setSpacing(6)
        self._dot = StatusDot()
        row.addWidget(self._dot, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._waveform = WaveformBars()
        row.addWidget(self._waveform, stretch=1, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._spinner = Spinner()
        row.addWidget(self._spinner, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._busy_lbl = QLabel("")
        self._busy_lbl.setStyleSheet(_SMALL_TEXT)
        self._busy_lbl.setToolTip("Please don't type until it finishes")
        row.addWidget(self._busy_lbl, stretch=1, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._polish_btn = _icon_button("sparkle", "Polish — rewrite more cleanly")
        self._send_btn = _icon_button("send", "Send — type it again at the cursor")
        self._clip_btn = _icon_button("copy", "Copy to clipboard")
        for b in (self._polish_btn, self._send_btn, self._clip_btn):
            row.addWidget(b, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._done_stretch = QWidget()
        self._done_stretch.setSizePolicy(QSizePolicy.Policy.Expanding,
                                         QSizePolicy.Policy.Preferred)
        row.addWidget(self._done_stretch)
        self._timer_lbl = QLabel("0:00")
        self._timer_lbl.setStyleSheet(_SMALL_TEXT)
        self._timer_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._timer_lbl, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._expand_btn = _icon_button("expand", "Make wider")
        row.addWidget(self._expand_btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._x_btn = _icon_button("close", "Close", 10)
        self._x_btn.setFixedSize(22, 22)
        row.addWidget(self._x_btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._strip)

        self.set_mode("rec")

    # ── modes ───────────────────────────────────────────────────────────
    def set_mode(self, mode: str) -> None:
        """'rec' (recording), 'busy' (processing) or 'done' (text shown)."""
        self._mode = mode
        rec, busy, done = mode == "rec", mode == "busy", mode == "done"
        self._body.setVisible(done)
        self._divider.setVisible(done)
        self._dot.setVisible(rec)
        self._waveform.setVisible(rec)
        self._timer_lbl.setVisible(rec)
        self._spinner.setVisible(busy)
        self._busy_lbl.setVisible(busy)
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()
        for b in (self._polish_btn, self._send_btn, self._clip_btn,
                  self._expand_btn, self._done_stretch):
            b.setVisible(done)
        self._x_btn.setVisible(done)  # recording: shown on hover only
        self._apply_mode_sizes(big=not done)

    def set_sizes(self, strip_h: int, rec_h: int) -> None:
        self._strip_h, self._rec_h = strip_h, rec_h
        self._apply_mode_sizes(big=self._mode != "done")

    def set_done_layout(self, compact: bool, strip_h: int) -> None:
        """Compact text box: slimmer strip and padding so a line of text fits;
        expanded box: roomier strip and padding."""
        self._strip.setFixedHeight(strip_h)
        if compact:
            self._body_lay.setContentsMargins(12, 6, 8, 2)
        else:
            self._body_lay.setContentsMargins(_BODY_PAD_X, _BODY_PAD_TOP,
                                              _BODY_PAD_X - 4, _BODY_PAD_BOTTOM)
        btn_h = min(26, strip_h - 4)
        for b in (self._polish_btn, self._send_btn, self._clip_btn, self._expand_btn):
            b.setFixedHeight(btn_h)

    def status_height(self, box_w: int) -> int:
        if not self._status.text():
            return 0
        self._status.ensurePolished()
        sfm = QFontMetrics(self._status.font())
        return 6 + sfm.boundingRect(
            QRect(0, 0, max(20, box_w - 2 * _BODY_PAD_X), 1000),
            int(Qt.TextFlag.TextWordWrap), self._status.text()).height()

    def _apply_mode_sizes(self, big: bool) -> None:
        """The recording/busy box is large; the text box's strip is slim."""
        if big:
            h = self._rec_h
            k = h / 40.0  # scale relative to the original 40 px strip
            self._strip.setFixedHeight(h)
            self._row.setContentsMargins(int(18 * k), 0, int(12 * k), 0)
            self._row.setSpacing(int(10 * k))
            self._dot.setFixedSize(int(10 * k), int(10 * k))
            self._waveform.set_height(int(18 * k))
            self._spinner.setFixedSize(int(14 * k), int(14 * k))
            font_px = max(11, int(11 * k * 0.75))
            self._timer_lbl.setStyleSheet(
                f"color: #c9ced6; font: 500 {font_px}px 'Inter'; background: transparent;")
            self._busy_lbl.setStyleSheet(
                f"color: #c9ced6; font: 500 {font_px}px 'Inter'; background: transparent;")
            self._x_btn.setFixedSize(int(22 * k * 0.8), int(22 * k * 0.8))
        else:
            self._strip.setFixedHeight(self._strip_h)
            self._row.setContentsMargins(12, 0, 8, 0)
            self._row.setSpacing(6)
            self._dot.setFixedSize(10, 10)
            self._waveform.set_height(18)
            self._spinner.setFixedSize(14, 14)
            self._timer_lbl.setStyleSheet(_SMALL_TEXT)
            self._busy_lbl.setStyleSheet(_SMALL_TEXT)
            self._x_btn.setFixedSize(22, 22)

    def set_wide(self, wide: bool) -> None:
        """Wide box: buttons get their labels; compact: icons only."""
        from src.ui.icons import button_icon
        labels = ((self._polish_btn, "Polish"), (self._send_btn, "Send"),
                  (self._clip_btn, "Copy"))
        for b, text in labels:
            if b.isEnabled() or not b.text():
                b.setText(f" {text}" if wide else "")
        self._expand_btn.setIcon(button_icon("collapse" if wide else "expand",
                                             14, "#9aa0a9", "#ffffff"))
        self._expand_btn.setToolTip("Make narrower" if wide else "Make wider")

    def set_text(self, text: str) -> None:
        self._full_text = (text or "").strip()
        self._text.setText(self._full_text)
        self._scroll.verticalScrollBar().setValue(0)

    def set_status(self, text: str, color: str = "#8a8f98") -> None:
        self._status.setText(text)
        self._status.setStyleSheet(f"color: {color}; {_STATUS_STYLE}")
        self._status.setVisible(bool(text))

    def fit_body(self, box_w: int, body_h: int) -> None:
        """Give the text area whatever the fixed-size box leaves after the
        padding and status line; longer text scrolls inside it."""
        status_h = self.status_height(box_w)
        self._text.ensurePolished()
        fm = QFontMetrics(self._text.font())
        m = self._body_lay.contentsMargins()
        avail = body_h - m.top() - m.bottom() - status_h
        # Whole lines only, so the last visible line is never cut in half.
        lines = max(1, int((avail - 2) // fm.lineSpacing()))
        self._scroll.setFixedHeight(lines * fm.lineSpacing() + 2)


# ── Main overlay widget ──────────────────────────────────────────────────────
class OverlayWindow(QWidget):
    """Persistent dock overlay.  All Qt work happens on the GUI thread.

    The window's paintEvent draws the morphing shape (dots or box);
    `_BoxContent` sits on top of it once the box is open. The window is
    resized to exactly what's drawn, anchored at bottom-center, so it never
    covers more of the screen than it shows.
    """

    _sig_state = Signal(str, str)
    _sig_level = Signal(float)
    _sig_polish_result = Signal(object, int)
    _sig_send_result = Signal(object, int)
    _sig_clipboard_result = Signal(object, int)
    _sig_notice = Signal(str)

    def __init__(self) -> None:
        super().__init__(None)
        self._visible = False
        self._polishing = False
        self._session_id = 0
        self._hold_hotkey = ""
        self._live_hotkey = ""
        self._state = "idle"
        self._mode_state = "idle"  # last state the box was set up for
        self._hovered = False
        # After an explicit Polish/Send (or a failure notice) the box stays
        # until closed: the user's attention is usually on the target app,
        # and auto-hiding there made a successful action look like a no-op.
        self._sticky = False
        self._pending_notice = ""  # notice that arrives before the text shows

        self._polish_callback = None
        self._send_callback = None
        self._clipboard_callback = None
        self._stop_callback = None
        self._mic_callback = None

        # Hover mic: progress (0 = hidden in the right dot, 1 = settled).
        self._mic_t = 0.0
        self._mic_open = False   # cursor near the dots -> mic out
        self._mic_hot = False    # cursor over the mic button itself
        self._mic_anim: QVariantAnimation | None = None

        # Sizes in px for the current screen (set in start()).
        self._ppc = 96 / 2.54
        self._rec_w = self._rec_h = self._strip_h = self._compact_strip_h = 0
        self._text_w = self._text_h = self._wide_w = self._wide_h = 0
        self._compute_sizes()
        self._expanded = load_expanded_pref()

        # Morph state: t (0 = dots, 1 = box) and the box's current size.
        self._dot = _DOT_PX
        self._t = 0.0
        self._box_w = float(self._rec_w)
        self._box_h = float(self._rec_h)
        self._anim: QVariantAnimation | None = None
        self._anim_from = (0.0, self._box_w, self._box_h)
        self._anim_to = (0.0, self._box_w, self._box_h)
        self._elapsed = QElapsedTimer()

        self._build_ui()
        self._connect_signals()

        self._clock = QTimer(self)
        self._clock.setInterval(250)
        self._clock.timeout.connect(self._tick_clock)

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self._on_auto_dismiss)

        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(_HOVER_POLL_MS)
        self._hover_timer.timeout.connect(self._poll_hover)

    def _compute_sizes(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            self._ppc = px_per_cm(screen)
        self._rec_w = int(round(_REC_CM * self._ppc))
        self._text_w = int(round(_TEXT_CM * self._ppc))
        self._text_h = int(round(_TEXT_H_CM * self._ppc))
        self._wide_w = int(round(_WIDE_CM * self._ppc))
        self._wide_h = int(round(_WIDE_H_CM * self._ppc))
        self._strip_h = max(30, int(round(_STRIP_CM * self._ppc)))
        self._compact_strip_h = max(24, int(round(_COMPACT_STRIP_CM * self._ppc)))
        self._rec_h = max(self._strip_h, int(round(_REC_H_CM * self._ppc)))

    # ── UI construction ──────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._content = _BoxContent(self._strip_h, self._rec_h, self)
        self._content.hide()

    def _connect_signals(self) -> None:
        q = Qt.ConnectionType.QueuedConnection
        self._sig_state.connect(self._apply_state, q)
        self._sig_level.connect(self._on_level, q)
        self._sig_polish_result.connect(self._apply_polish_result, q)
        self._sig_send_result.connect(self._apply_send_result, q)
        self._sig_clipboard_result.connect(self._apply_clipboard_result, q)
        self._sig_notice.connect(self._apply_notice, q)
        c = self._content
        c._x_btn.clicked.connect(self._on_x)
        c._polish_btn.clicked.connect(self._on_polish)
        c._send_btn.clicked.connect(self._on_send)
        c._clip_btn.clicked.connect(self._on_clipboard)
        c._expand_btn.clicked.connect(self._on_expand)

    # ── Public API (called from ANY thread) ─────────────────────────────
    def set_state(self, state: str, text: str = "") -> None:
        self._sig_state.emit(state, text)

    def set_level(self, db: float) -> None:
        self._sig_level.emit(float(db))

    def set_notice(self, message: str) -> None:
        """Explain why text didn't land on screen (shown in the box)."""
        self._sig_notice.emit(message)

    def set_polish_callback(self, callback) -> None:
        self._polish_callback = callback

    def set_send_callback(self, callback) -> None:
        self._send_callback = callback

    def set_clipboard_callback(self, callback) -> None:
        self._clipboard_callback = callback

    def set_stop_callback(self, callback) -> None:
        self._stop_callback = callback

    def set_mic_callback(self, callback) -> None:
        """Called (on the GUI thread) when the hover mic is clicked."""
        self._mic_callback = callback

    def set_hotkeys(self, hold: str, live: str) -> None:
        self._hold_hotkey = hold
        self._live_hotkey = live
        self._content._strip.setToolTip(f"Hold {hold} · Tap {live}")

    def start(self) -> None:
        """Show the idle dots at the dock position immediately."""
        self._visible = True
        self._compute_sizes()
        self._content.set_sizes(self._strip_h, self._rec_h)
        self._t = 0.0
        self._content.hide()
        self._apply_geometry()
        self.show()
        self.raise_()
        self._force_topmost()
        self._make_click_through()
        self._hover_timer.start()

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
        self._hide_mic_now()
        self._state = state
        if state == "done":
            self._show_done(text)
        elif state == "recording":
            self._show_recording()
        else:
            self._show_busy(state)

    def _show_recording(self) -> None:
        if self._mode_state == "recording":
            # The engine re-sends "recording" after every slice; that's the
            # same dictation, not a new one, so keep the timer and waveform.
            return
        self._mode_state = "recording"
        # A fresh dictation is starting -- any Polish/Send/Clipboard result
        # from a previous session that lands after this point is stale and
        # must not be allowed to overwrite this session's text.
        self._session_id += 1
        self._pending_notice = ""
        self._sticky = False
        self._dismiss_timer.stop()
        c = self._content
        c.set_mode("rec")
        c._x_btn.setEnabled(True)
        c._x_btn.setToolTip("Stop recording")
        c._x_btn.setVisible(self._hovered)
        c._dot.set_state(_STATE_COLORS["recording"], pulse=True)
        c._waveform.reset()
        c._timer_lbl.setText("0:00")
        self._elapsed.start()
        self._clock.start()
        self._set_interactive(True)  # the X (stop) needs clicks
        self._animate_to(1.0, self._rec_w, self._rec_h)

    def _show_busy(self, state: str) -> None:
        self._mode_state = state
        self._clock.stop()
        self._dismiss_timer.stop()
        c = self._content
        c.set_mode("busy")
        c._spinner.set_color(_STATE_COLORS.get(state, "#8a8f98"))
        c._busy_lbl.setText(_BUSY_LABELS.get(state, state.capitalize() + "…"))
        self._make_click_through()
        self._animate_to(1.0, self._rec_w, self._rec_h)

    def _show_done(self, text: str) -> None:
        self._mode_state = "done"
        self._clock.stop()
        c = self._content
        c.set_mode("done")
        c.set_text(text)
        c.set_status(self._pending_notice, _NOTICE_COLOR)
        # A box explaining a failure stays until closed.
        self._sticky = bool(self._pending_notice)
        self._pending_notice = ""
        self._polishing = False
        c._x_btn.setEnabled(True)
        c._x_btn.setToolTip("Close")
        for b in (c._polish_btn, c._send_btn, c._clip_btn):
            b.setEnabled(True)
        c.set_wide(self._expanded)
        # The prior state (e.g. "updating") left the window click-through;
        # the buttons need real clicks.
        self._set_interactive(True)
        self._resize_to_done()
        self._restart_dismiss()

    def _done_size(self) -> tuple[int, int]:
        """Text box size: normal (10.5 x 2.7 cm) or expanded (16 x 6 cm)."""
        if self._expanded:
            return self._wide_w, self._wide_h
        return self._text_w, self._text_h

    def _resize_to_done(self) -> None:
        w, h = self._done_size()
        # Only a short text box (under 2 cm) needs the slimmer strip.
        compact = not self._expanded and h < 2.0 * self._ppc
        strip = self._compact_strip_h if compact else self._strip_h
        self._content.set_done_layout(compact, strip)
        if compact:
            # The small box has room for one line; a notice/status line gets
            # its own extra room instead of squeezing the text out.
            h += self._content.status_height(w)
        self._content.fit_body(w, h - 1 - strip)
        self._animate_to(1.0, w, h)

    def _collapse_to_bar(self) -> None:
        """Shrink the box back into the three idle dots."""
        self._state = "idle"
        self._mode_state = "idle"
        self._dismiss_timer.stop()
        self._clock.stop()
        self._content._spinner.stop()
        self._content._dot.set_state(_STATE_COLORS["idle"], False)
        self._make_click_through()
        self._animate_to(0.0, self._box_w, self._box_h, _CLOSE_MS)

    def _tick_clock(self) -> None:
        if self._elapsed.isValid():
            self._content._timer_lbl.setText(format_elapsed(self._elapsed.elapsed()))

    # ── Dismiss / hover ─────────────────────────────────────────────────
    def _restart_dismiss(self) -> None:
        if self._mode_state == "done" and not self._hovered and not self._sticky:
            self._dismiss_timer.start(_AUTO_DISMISS_MS)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        if self._mode_state == "recording":
            self._content._x_btn.show()
        if self._mode_state == "done":
            self._dismiss_timer.stop()  # hovering keeps the box open
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        if self._mode_state == "recording":
            self._content._x_btn.hide()
        self._restart_dismiss()
        super().leaveEvent(event)

    def _on_auto_dismiss(self) -> None:
        if self._mode_state == "done" and not self._hovered:
            self._collapse_to_bar()

    # ── Morph animation ─────────────────────────────────────────────────
    def _animate_to(self, t: float, w: float, h: float,
                    duration: int | None = None) -> None:
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        start = (self._t, self._box_w, self._box_h)
        end = (t, float(w), float(h))
        if start == end:
            self._anim_to = end
            self._finish_anim()
            return
        if duration is None:
            duration = _OPEN_MS if self._t < 1.0 else _RESIZE_MS
        # Content shows only on an open box; while an open box just resizes
        # it stays visible and reflows with each frame.
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
        if self._t == 0.0 and self._state == "idle" and self._visible:
            self._hover_timer.start()  # back to resting dots: mic available
        self.raise_()
        self._force_topmost()

    # ── Expand ──────────────────────────────────────────────────────────
    def _on_expand(self) -> None:
        self._expanded = not self._expanded
        save_expanded_pref(self._expanded)
        self._content.set_wide(self._expanded)
        if self._mode_state == "done":
            self._resize_to_done()

    # ── Hover mic ───────────────────────────────────────────────────────
    def _at_rest(self) -> bool:
        return self._state == "idle" and self._t == 0.0 and self._anim is None

    def _mic_center(self) -> QPointF:
        offset = self._dot * (1 + _DOT_GAP_RATIO)
        dx, _, _ = mic_drop(self._mic_t, self._dot)
        return QPointF(self.width() / 2 + offset + dx, self.height() / 2)

    def _poll_hover(self) -> None:
        if not self._at_rest():
            return
        pos = QCursor.pos()
        g = self.geometry()
        pad = _HOVER_PAD * (2 if self._mic_open else 1)  # hysteresis
        near = g.adjusted(-pad, -pad, pad, pad).contains(pos)
        if near != self._mic_open:
            self._mic_open = near
            # Only while the mic is out does this small area take clicks.
            self._set_interactive(near)
            self._animate_mic(1.0 if near else 0.0)
        hot = False
        if self._mic_open and self._mic_t > 0.9:
            c = self._mic_center()
            local = pos - g.topLeft()
            hot = math.hypot(local.x() - c.x(), local.y() - c.y()) <= _MIC_D / 2 + 2
        if hot != self._mic_hot:
            self._mic_hot = hot
            self.setCursor(Qt.CursorShape.PointingHandCursor if hot
                           else Qt.CursorShape.ArrowCursor)
            self.setToolTip(f"Start live typing ({self._live_hotkey})" if hot else "")
            self.update()

    def _animate_mic(self, target: float) -> None:
        if self._mic_anim is not None:
            self._mic_anim.stop()
        anim = QVariantAnimation(self)
        anim.setStartValue(self._mic_t)
        anim.setEndValue(target)
        anim.setDuration(_MIC_OUT_MS if target > self._mic_t else _MIC_IN_MS)
        anim.setEasingCurve(QEasingCurve.Type.Linear)  # shaping is in mic_drop
        anim.valueChanged.connect(self._on_mic_frame)
        self._mic_anim = anim
        anim.start()

    def _on_mic_frame(self, v) -> None:
        self._mic_t = float(v)
        self.update()

    def _hide_mic_now(self) -> None:
        """A dictation is starting: tuck the mic away without animating."""
        self._hover_timer.stop()
        if self._mic_anim is not None:
            self._mic_anim.stop()
            self._mic_anim = None
        self._mic_t = 0.0
        self._mic_open = False
        if self._mic_hot:
            self._mic_hot = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.setToolTip("")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if (self._at_rest() and self._mic_open and self._mic_t > 0.9
                and event.button() == Qt.MouseButton.LeftButton):
            c = self._mic_center()
            pos = event.position()
            if math.hypot(pos.x() - c.x(), pos.y() - c.y()) <= _MIC_D / 2 + 2:
                self._on_mic()
                return
        super().mousePressEvent(event)

    def _on_mic(self) -> None:
        self._hide_mic_now()
        self._make_click_through()
        self.update()
        if self._mic_callback is not None:
            # Next event-loop turn, so this click handler returns first.
            QTimer.singleShot(0, self._mic_callback)

    # ── Geometry + painting ─────────────────────────────────────────────
    def _apply_geometry(self) -> None:
        """Resize/move the window to the current shape, bottom-center."""
        if self._t == 0.0:
            self._apply_idle_geometry()
            return
        _, w, h, _ = morph_shape(self._t, self._dot, self._box_w, self._box_h)
        win_w = int(math.ceil(w)) + 2 * _MARGIN
        win_h = int(math.ceil(h)) + 2 * _MARGIN
        screen = self.screen() or QApplication.primaryScreen()
        rect = screen.availableGeometry()
        cx = rect.left() + rect.width() / 2
        x = int(round(cx - win_w / 2))
        # The bottom of the painted shape sits _TASKBAR_GAP above the work
        # area; the margin is transparent padding below it.
        y = rect.bottom() + 1 - _TASKBAR_GAP + _MARGIN - win_h
        # One setGeometry call (no separate resize + move) so the shape grows
        # upward from its anchor without a one-frame jump.
        self.setGeometry(QRect(x, max(rect.top(), y), win_w, win_h))
        self._content.setGeometry(_MARGIN, _MARGIN, int(w), int(h))
        self.update()

    def _apply_idle_geometry(self) -> None:
        """Resting dots: a canvas centred on the dots, wide enough for the
        hover mic on the right (mirrored left so the dots stay centred)."""
        half = idle_half_width(self._dot)
        win_w = int(math.ceil(2 * half)) + 2 * _MARGIN
        win_h = _MIC_D + 2 * _MARGIN
        screen = self.screen() or QApplication.primaryScreen()
        rect = screen.availableGeometry()
        cx = rect.left() + rect.width() / 2
        # Same dot centre line as the morph frames use, so nothing jumps.
        dot_cy = rect.bottom() + 1 - _TASKBAR_GAP - self._dot / 2
        x = int(round(cx - win_w / 2))
        y = int(round(dot_cy - win_h / 2))
        self.setGeometry(QRect(x, max(rect.top(), y), win_w, win_h))
        self.update()

    def _paint_idle(self, p: QPainter) -> None:
        """Three dots; the right one can grow a droplet that becomes the mic."""
        cx, cy = self.width() / 2, self.height() / 2
        offset = self._dot * (1 + _DOT_GAP_RATIO)
        r = (self._dot - _DOT_BORDER) / 2
        p.setPen(QPen(_DOT_RING, _DOT_BORDER))
        p.setBrush(QBrush(_DOT_FILL))
        for dx in (-offset, 0.0):
            p.drawEllipse(QPointF(cx + dx, cy), r, r)

        right = QPointF(cx + offset, cy)
        if self._mic_t <= 0.0:
            p.drawEllipse(right, r, r)
            return
        dx, mr, neck = mic_drop(self._mic_t, self._dot)
        drop = QPointF(right.x() + dx, cy)
        shape = QPainterPath()
        shape.addEllipse(right, r, r)
        if neck > 0.0:
            # Liquid bridge: pinched in the middle, thinning until it snaps.
            mid = (right.x() + drop.x()) / 2
            bridge = QPainterPath()
            bridge.moveTo(right.x(), cy - r)
            bridge.cubicTo(mid, cy - neck, mid, cy - neck, drop.x(), cy - mr)
            bridge.lineTo(drop.x(), cy + mr)
            bridge.cubicTo(mid, cy + neck, mid, cy + neck, right.x(), cy + r)
            bridge.closeSubpath()
            shape = shape.united(bridge)
        mic_shape = QPainterPath()
        mic_shape.addEllipse(drop, mr, mr)
        if neck > 0.0:
            p.drawPath(shape.united(mic_shape))
        else:
            p.drawPath(shape)
            p.setBrush(QBrush(_MIC_FILL_HOT if self._mic_hot else _DOT_FILL))
            p.drawPath(mic_shape)
        # The mic glyph fades in as the drop reaches full size.
        alpha = max(0.0, min(1.0, (self._mic_t - 0.6) / 0.3))
        if alpha > 0.0:
            color = QColor(_MIC_ICON_HOT if self._mic_hot else _MIC_ICON)
            color.setAlphaF(alpha)
            draw_mic_icon(p, drop, mr, color)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._t == 0.0:
            self._paint_idle(p)
            return
        kind, w, h, payload = morph_shape(self._t, self._dot,
                                          self._box_w, self._box_h)
        cx = self.width() / 2
        bottom = self.height() - _MARGIN
        if kind == "dots":
            p.setPen(QPen(_DOT_RING, _DOT_BORDER))
            p.setBrush(QBrush(_DOT_FILL))
            r = (self._dot - _DOT_BORDER) / 2
            cy = bottom - self._dot / 2
            # Outer dots first so the middle one sits on top as they merge.
            for dx in (-payload, payload, 0.0):
                p.drawEllipse(QPointF(cx + dx, cy), r, r)
            return
        # Box: fill and ring fade from the dot look to the box look.
        b = max(0.0, min(1.0, (self._t - _MERGE_PHASE) / (1 - _MERGE_PHASE)))
        fill = QColor(
            int(_lerp(_DOT_FILL.red(), _BOX_FILL.red(), b)),
            int(_lerp(_DOT_FILL.green(), _BOX_FILL.green(), b)),
            int(_lerp(_DOT_FILL.blue(), _BOX_FILL.blue(), b)),
            int(_lerp(255, _BOX_FILL.alpha(), b)),
        )
        ring = QColor(
            int(_lerp(_DOT_RING.red(), _BOX_BORDER.red(), b)),
            int(_lerp(_DOT_RING.green(), _BOX_BORDER.green(), b)),
            int(_lerp(_DOT_RING.blue(), _BOX_BORDER.blue(), b)),
            int(_lerp(255, _BOX_BORDER.alpha(), b)),
        )
        p.setPen(QPen(ring, _lerp(_DOT_BORDER, 1.0, b)))
        p.setBrush(QBrush(fill))
        rect = QRectF(cx - w / 2 + 0.5, bottom - h + 0.5, w - 1, h - 1)
        p.drawRoundedRect(rect, payload, payload)

    # ── Result handlers (GUI thread) ────────────────────────────────────
    def _apply_notice(self, message: str) -> None:
        if self._mode_state == "done":
            self._content.set_status(message, _NOTICE_COLOR)
            self._sticky = True
            self._dismiss_timer.stop()
            self._resize_to_done()
        else:
            self._pending_notice = message

    def _show_status(self, text: str, color: str) -> None:
        self._content.set_status(text, color)
        if self._mode_state == "done":
            self._resize_to_done()

    def _notice_showing(self) -> bool:
        return self._content._status.isVisible() and \
            self._content._status.text().startswith(("Couldn't", "Not typed"))

    def _apply_polish_result(self, result, session_id: int) -> None:
        if session_id != self._session_id:
            log.debug("Discarding stale polish result from a previous dictation session")
            return
        self._polishing = False
        c = self._content
        c._polish_btn.setEnabled(True)
        c.set_wide(self._expanded)
        if result:
            c.set_text(result)
            if not self._notice_showing():
                self._show_status("Polished", _STATE_COLORS["done"])
            else:
                self._resize_to_done()
        else:
            self._show_status("Polish failed", _STATE_COLORS["error"])

    def _apply_send_result(self, text: str | None, session_id: int) -> None:
        if session_id != self._session_id:
            log.debug("Discarding stale send result from a previous dictation session")
            return
        self._polishing = False
        c = self._content
        c._send_btn.setEnabled(True)
        c.set_wide(self._expanded)
        if text:
            c.set_text(text)
            if not self._notice_showing():
                self._show_status("Sent", _STATE_COLORS["done"])
            else:
                self._resize_to_done()
        else:
            self._show_status("Nothing to send", _STATE_COLORS["error"])

    def _apply_clipboard_result(self, text: str | None, session_id: int) -> None:
        if session_id != self._session_id:
            log.debug("Discarding stale clipboard result from a previous dictation session")
            return
        self._polishing = False
        if text:
            self._show_status("Copied", _STATE_COLORS["done"])
        else:
            self._show_status("Nothing to copy", _STATE_COLORS["error"])
        self._restart_dismiss()

    # ── Level feed (GUI thread) ─────────────────────────────────────────
    def _on_level(self, db: float) -> None:
        self._content._waveform.push_level(db)
        self._content._waveform.tick_decay()

    # ── Button handlers ─────────────────────────────────────────────────
    def _busy_button(self, btn: QPushButton, label: str) -> None:
        btn.setEnabled(False)
        btn.setText(f" {label}" if self._expanded else "")
        self._content.set_status(f"{label}", "#8a8f98")  # visible even when compact

    def _on_polish(self) -> None:
        if self._polishing or self._polish_callback is None:
            return
        self._dismiss_timer.stop()
        self._polishing = True
        self._sticky = True
        self._busy_button(self._content._polish_btn, "Polishing…")
        self._resize_to_done()
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
        self._busy_button(self._content._send_btn, "Sending…")
        self._resize_to_done()
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
        """X: stop while recording, otherwise close."""
        if self._mode_state == "recording":
            self._on_stop()
        else:
            self._collapse_to_bar()

    def _on_stop(self) -> None:
        if self._stop_callback is None:
            return
        self._dismiss_timer.stop()
        # Re-enabled by the next state change (busy / done / idle).
        self._content._x_btn.setEnabled(False)
        threading.Thread(
            target=self._stop_callback, daemon=True, name="overlay-stop"
        ).start()

    def _on_close(self) -> None:
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
