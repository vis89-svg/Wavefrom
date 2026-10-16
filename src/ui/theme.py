"""Shared look for the app windows: AMOLED black, Inter type, logo.

One place for colors and fonts so the main window, History, Settings, the
login screen and the setup window stay consistent.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QIcon, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap)

# ── Palette ──────────────────────────────────────────────────────────────────
BG = "#000000"            # page background (AMOLED black)
SIDEBAR = "#000000"
SURFACE = "#121212"       # cards, inputs
SURFACE_HOVER = "#1c1c1c"
BORDER = "#262626"
TEXT = "#ece9e1"
TEXT_MUTED = "#a09d96"
TEXT_FAINT = "#75726c"
ACCENT = "#c8744f"        # warm clay: primary buttons, selection, focus
ACCENT_HOVER = "#d4865f"
ACCENT_SOFT = "#3a2e28"   # clay-tinted background for the selected nav item
DANGER = "#d1584f"
SUCCESS = "#7fa36b"


# Primary buttons share the app tile's clay gradient.
BUTTON_GRADIENT = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                   "stop:0 #d98a63, stop:1 #b9663f)")
BUTTON_GRADIENT_HOVER = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                         "stop:0 #e39a75, stop:1 #c4714a)")
SANS = "Inter"  # bundled (assets/fonts), the closest open font to SF Pro

# ── Shared widget styles ─────────────────────────────────────────────────────
INPUTS = f"""
QLineEdit, QComboBox, QPlainTextEdit {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER};
    border-radius: 8px; padding: 8px 12px; font: 13px '{SANS}';
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit:disabled, QPlainTextEdit:disabled {{ color: {TEXT_FAINT}; border-color: {SURFACE}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER};
    selection-background-color: {ACCENT_SOFT}; selection-color: {TEXT};
}}
QCheckBox {{ color: {TEXT}; font: 13px '{SANS}'; spacing: 10px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border: 1px solid {BORDER};
    border-radius: 4px; background: {SURFACE};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
"""

BUTTONS = f"""
QPushButton {{
    background: {BUTTON_GRADIENT}; color: #ffffff; border: none; border-radius: 8px;
    padding: 8px 20px; font: 600 13px '{SANS}';
}}
QPushButton:hover {{ background: {BUTTON_GRADIENT_HOVER}; }}
QPushButton:disabled {{ background: {SURFACE}; color: {TEXT_FAINT}; }}
QPushButton[kind="ghost"] {{
    background: transparent; color: {TEXT_MUTED}; font: 13px '{SANS}';
    padding: 6px 12px;
}}
QPushButton[kind="ghost"]:hover {{ background: {SURFACE_HOVER}; color: {TEXT}; }}
QPushButton[kind="secondary"] {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER};
}}
QPushButton[kind="secondary"]:hover {{ background: {SURFACE_HOVER}; }}
"""

SCROLLBARS = f"""
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {TEXT_FAINT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QScrollBar:horizontal {{ height: 0; }}
"""

MENUS = f"""
QMenu {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER};
    border-radius: 8px; padding: 4px; font: 13px '{SANS}';
}}
QMenu::item {{ padding: 7px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {SURFACE_HOVER}; }}
QToolTip {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER};
    padding: 4px 8px; font: 12px '{SANS}';
}}
"""


def page_title_style(size: int = 26) -> str:
    return f"color: {TEXT}; font: 700 {size}px '{SANS}'; background: transparent;"


def heading_style(size: int = 16, weight: int = 600, color: str = TEXT) -> str:
    return f"color: {color}; font: {weight} {size}px '{SANS}'; background: transparent;"


_FONT_FILES = ("Inter-Regular.ttf", "Inter-Medium.ttf", "Inter-SemiBold.ttf",
               "Inter-Bold.ttf")


def load_fonts() -> str | None:
    """Register the bundled Inter fonts with Qt. Returns the family name, or
    None if they couldn't be loaded (Qt then falls back to the system font).
    Call once, after the QApplication exists."""
    from PySide6.QtGui import QFontDatabase
    family = None
    for name in _FONT_FILES:
        fid = QFontDatabase.addApplicationFont(str(_asset_path("fonts/" + name)))
        if fid >= 0 and not family:
            fams = QFontDatabase.applicationFontFamilies(fid)
            family = fams[0] if fams else None
    return family


def label_style(color: str = TEXT_MUTED, size: int = 13, weight: str = "normal") -> str:
    return (f"color: {color}; font: {weight} {size}px '{SANS}'; "
            "background: transparent;")


# ── Logo ─────────────────────────────────────────────────────────────────────
def _asset_path(name: str) -> Path:
    """Asset location in a source checkout or a PyInstaller bundle."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base / "assets" / name


def logo_pixmap(size: int, color: str = TEXT) -> QPixmap:
    """The waveform logo tinted `color`, fitted into a `size` px square
    (it is wide, so it fills the width; empty if the file is missing)."""
    src = QPixmap(str(_asset_path("logo_mark.png")))
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    if src.isNull():
        return out
    src = src.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)
    p = QPainter(out)
    p.drawPixmap((size - src.width()) // 2, (size - src.height()) // 2, src)
    # Keep the logo's shape (alpha), replace its color.
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    p.fillRect(out.rect(), QColor(color))
    p.end()
    return out


# ── App tile (Apple-style squircle) ──────────────────────────────────────────
TILE_TOP = "#d98a63"      # clay gradient, light at the top...
TILE_BOTTOM = "#b2603d"   # ...deeper at the bottom


def squircle_path(rect: QRectF, n: float = 5.0, steps: int = 120) -> QPainterPath:
    """Superellipse |x|^n + |y|^n = 1 fitted to `rect`: the continuous-curve
    'squircle' corner Apple uses, rather than a circular-arc rounded rect."""
    cx, cy = rect.center().x(), rect.center().y()
    a, b = rect.width() / 2, rect.height() / 2
    path = QPainterPath()
    for i in range(steps + 1):
        t = 2 * math.pi * i / steps
        c, s = math.cos(t), math.sin(t)
        x = cx + a * math.copysign(abs(c) ** (2 / n), c)
        y = cy + b * math.copysign(abs(s) ** (2 / n), s)
        if i == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()
    return path


def tile_pixmap(size: int, top: str = TILE_TOP, bottom: str = TILE_BOTTOM,
                glyph: str = "#ffffff") -> QPixmap:
    """The app icon: white waveform on a clay squircle with a soft highlight."""
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    small = size <= 24
    margin = 0.0 if small else size * 0.05   # tiny icons use every pixel
    rect = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    shape = squircle_path(rect)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    grad.setColorAt(0.0, QColor(top))
    grad.setColorAt(1.0, QColor(bottom))
    p.fillPath(shape, grad)
    if not small:
        # Soft light on the upper half, like Apple's tiles.
        p.save()
        p.setClipPath(shape)
        hl = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.center().y()))
        hl.setColorAt(0.0, QColor(255, 255, 255, 46))
        hl.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(QRectF(rect.left(), rect.top(), rect.width(), rect.height() / 2), hl)
        p.restore()
        p.setPen(QPen(QColor(0, 0, 0, 38), max(1.0, size / 96)))
        p.drawPath(shape)
    # The mark is wide and thin: give it more of the tile as the icon shrinks.
    ratio = 0.86 if size <= 24 else 0.76 if size <= 48 else 0.64
    mark = logo_pixmap(int(rect.width() * ratio), glyph)
    p.drawPixmap(int(rect.center().x() - mark.width() / 2),
                 int(rect.center().y() - mark.height() / 2), mark)
    p.end()
    return out


def app_icon() -> QIcon:
    icon = QIcon()
    for s in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        icon.addPixmap(tile_pixmap(s))
    return icon
