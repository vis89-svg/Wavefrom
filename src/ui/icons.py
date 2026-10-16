"""Hand-drawn line icons (SF Symbols-like) and a Mac-style toggle switch.

Icons are painted with QPainter so they share one stroke width, size and
colour, instead of relying on Unicode glyphs that each font renders
differently (some even as colour emoji).
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QCheckBox, QSizePolicy

ICON_NAMES = ("dashboard", "history", "settings", "sparkle", "send", "copy",
              "trash", "check", "close", "expand", "collapse")


def _pen(color: QColor, size: int) -> QPen:
    pen = QPen(color, max(1.3, size / 12.0))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _draw_dashboard(p: QPainter, s: float) -> None:
    gap, pad = s * 0.12, s * 0.14
    cell = (s - 2 * pad - gap) / 2
    r = cell * 0.28
    for row in range(2):
        for col in range(2):
            x = pad + col * (cell + gap)
            y = pad + row * (cell + gap)
            p.drawRoundedRect(QRectF(x, y, cell, cell), r, r)


def _draw_history(p: QPainter, s: float) -> None:
    c = QPointF(s / 2, s / 2)
    r = s * 0.36
    p.drawEllipse(c, r, r)
    p.drawLine(c, QPointF(c.x(), c.y() - r * 0.58))
    p.drawLine(c, QPointF(c.x() + r * 0.44, c.y() + r * 0.26))


def _draw_settings(p: QPainter, s: float) -> None:
    c = QPointF(s / 2, s / 2)
    outer, inner, teeth = s * 0.40, s * 0.30, 8
    path = QPainterPath()
    steps = teeth * 4
    for i in range(steps + 1):
        a = 2 * math.pi * i / steps - math.pi / 2
        # tooth profile: two points out, two points in
        rad = outer if (i % 4) in (0, 1) else inner
        pt = QPointF(c.x() + rad * math.cos(a), c.y() + rad * math.sin(a))
        if i == 0:
            path.moveTo(pt)
        else:
            path.lineTo(pt)
    path.closeSubpath()
    p.drawPath(path)
    p.drawEllipse(c, s * 0.13, s * 0.13)


def _draw_sparkle(p: QPainter, s: float) -> None:
    def star(cx, cy, r):
        k = r * 0.28
        path = QPainterPath()
        path.moveTo(cx, cy - r)
        path.quadTo(cx + k, cy - k, cx + r, cy)
        path.quadTo(cx + k, cy + k, cx, cy + r)
        path.quadTo(cx - k, cy + k, cx - r, cy)
        path.quadTo(cx - k, cy - k, cx, cy - r)
        p.drawPath(path)
    star(s * 0.42, s * 0.56, s * 0.30)
    star(s * 0.76, s * 0.24, s * 0.13)


def _draw_send(p: QPainter, s: float) -> None:
    # Return arrow: down from top-right, then left, with an arrowhead.
    x_r, y_t, y_b, x_l = s * 0.78, s * 0.20, s * 0.66, s * 0.22
    path = QPainterPath()
    path.moveTo(x_r, y_t)
    path.lineTo(x_r, y_b - s * 0.10)
    path.quadTo(x_r, y_b, x_r - s * 0.10, y_b)
    path.lineTo(x_l, y_b)
    p.drawPath(path)
    a = s * 0.16
    p.drawLine(QPointF(x_l, y_b), QPointF(x_l + a, y_b - a))
    p.drawLine(QPointF(x_l, y_b), QPointF(x_l + a, y_b + a))


def _draw_copy(p: QPainter, s: float) -> None:
    r = s * 0.10
    p.drawRoundedRect(QRectF(s * 0.34, s * 0.30, s * 0.46, s * 0.52), r, r)
    path = QPainterPath()  # back sheet, only its visible top-left edges
    path.moveTo(s * 0.24, s * 0.66)
    path.lineTo(s * 0.24, s * 0.26)
    path.quadTo(s * 0.24, s * 0.18, s * 0.32, s * 0.18)
    path.lineTo(s * 0.62, s * 0.18)
    p.drawPath(path)


def _draw_trash(p: QPainter, s: float) -> None:
    p.drawLine(QPointF(s * 0.20, s * 0.28), QPointF(s * 0.80, s * 0.28))
    p.drawLine(QPointF(s * 0.40, s * 0.28), QPointF(s * 0.42, s * 0.18))
    p.drawLine(QPointF(s * 0.42, s * 0.18), QPointF(s * 0.58, s * 0.18))
    p.drawLine(QPointF(s * 0.58, s * 0.18), QPointF(s * 0.60, s * 0.28))
    body = QPainterPath()
    body.moveTo(s * 0.28, s * 0.30)
    body.lineTo(s * 0.32, s * 0.80)
    body.quadTo(s * 0.33, s * 0.84, s * 0.38, s * 0.84)
    body.lineTo(s * 0.62, s * 0.84)
    body.quadTo(s * 0.67, s * 0.84, s * 0.68, s * 0.80)
    body.lineTo(s * 0.72, s * 0.30)
    p.drawPath(body)


def _draw_close(p: QPainter, s: float) -> None:
    a, b = s * 0.28, s * 0.72
    p.drawLine(QPointF(a, a), QPointF(b, b))
    p.drawLine(QPointF(b, a), QPointF(a, b))


def _draw_expand(p: QPainter, s: float) -> None:
    """Outward arrows <-> : make wider."""
    y, a = s / 2, s * 0.16
    p.drawLine(QPointF(s * 0.18, y), QPointF(s * 0.82, y))
    for x, d in ((s * 0.18, 1), (s * 0.82, -1)):
        p.drawLine(QPointF(x, y), QPointF(x + d * a, y - a))
        p.drawLine(QPointF(x, y), QPointF(x + d * a, y + a))


def _draw_collapse(p: QPainter, s: float) -> None:
    """Inward arrows >-< : make narrower."""
    y, a = s / 2, s * 0.16
    for x0, x1 in ((s * 0.12, s * 0.42), (s * 0.88, s * 0.58)):
        p.drawLine(QPointF(x0, y), QPointF(x1, y))
        d = -1 if x1 > x0 else 1
        p.drawLine(QPointF(x1, y), QPointF(x1 + d * a, y - a))
        p.drawLine(QPointF(x1, y), QPointF(x1 + d * a, y + a))


def _draw_check(p: QPainter, s: float) -> None:
    path = QPainterPath()
    path.moveTo(s * 0.22, s * 0.52)
    path.lineTo(s * 0.42, s * 0.72)
    path.lineTo(s * 0.80, s * 0.30)
    p.drawPath(path)


_DRAW = {"dashboard": _draw_dashboard, "history": _draw_history,
         "settings": _draw_settings, "sparkle": _draw_sparkle,
         "send": _draw_send, "copy": _draw_copy, "trash": _draw_trash,
         "check": _draw_check, "close": _draw_close,
         "expand": _draw_expand, "collapse": _draw_collapse}


def button_icon(name: str, size: int, color: str, hover_color: str | None = None) -> QIcon:
    """Line icon for a flat button (crisp at 1x and 2x)."""
    icon = QIcon()
    for scale in (1, 2):
        icon.addPixmap(line_pixmap(name, size * scale, color), QIcon.Mode.Normal)
        if hover_color:
            icon.addPixmap(line_pixmap(name, size * scale, hover_color), QIcon.Mode.Active)
    return icon


def line_pixmap(name: str, size: int, color: str) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(_pen(QColor(color), size))
    p.setBrush(Qt.BrushStyle.NoBrush)
    _DRAW[name](p, float(size))
    p.end()
    return pm


def nav_icon(name: str, size: int, off_color: str, on_color: str) -> QIcon:
    """Icon for a checkable nav button: muted when off, bright when on."""
    icon = QIcon()
    for scale in (1, 2):  # crisp on high-DPI screens
        icon.addPixmap(line_pixmap(name, size * scale, off_color),
                       QIcon.Mode.Normal, QIcon.State.Off)
        icon.addPixmap(line_pixmap(name, size * scale, on_color),
                       QIcon.Mode.Normal, QIcon.State.On)
    return icon


class ToggleSwitch(QCheckBox):
    """macOS-style on/off switch. Same API as QCheckBox (isChecked etc.)."""

    def __init__(self, checked: bool = False, on_color: str = "#c8744f",
                 off_color: str = "#3a3a3a", parent=None) -> None:
        super().__init__(parent)
        self._on = QColor(on_color)
        self._off = QColor(off_color)
        self.setChecked(checked)
        self._pos = 1.0 if checked else 0.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(140)
        self._anim.valueChanged.connect(self._on_anim)
        self.toggled.connect(self._animate)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(40, 24)

    def hitButton(self, pos) -> bool:  # noqa: N802
        return self.rect().contains(pos)

    def _animate(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def _on_anim(self, v) -> None:
        self._pos = float(v)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = 40.0, 24.0
        track = QColor(
            int(self._off.red() + (self._on.red() - self._off.red()) * self._pos),
            int(self._off.green() + (self._on.green() - self._off.green()) * self._pos),
            int(self._off.blue() + (self._on.blue() - self._off.blue()) * self._pos),
        )
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        d = h - 4
        x = 2 + (w - d - 4) * self._pos
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(x, 2, d, d))
