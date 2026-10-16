"""Shared look for the app windows: warm dark palette, serif headings, logo.

One place for colors and fonts so the main window, History, Settings, the
login screen and the setup window stay consistent.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

# ── Palette ──────────────────────────────────────────────────────────────────
BG = "#262624"            # page background
SIDEBAR = "#1f1e1d"
SURFACE = "#30302e"       # cards, inputs
SURFACE_HOVER = "#383835"
BORDER = "#3d3d3a"
TEXT = "#ece9e1"
TEXT_MUTED = "#a09d96"
TEXT_FAINT = "#75726c"
ACCENT = "#c8744f"        # warm clay: primary buttons, selection, focus
ACCENT_HOVER = "#d4865f"
ACCENT_SOFT = "#3a2e28"   # clay-tinted background for the selected nav item
DANGER = "#d1584f"
SUCCESS = "#7fa36b"

SERIF = "Georgia"
SANS = "Segoe UI"

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
    background: {ACCENT}; color: #ffffff; border: none; border-radius: 8px;
    padding: 8px 20px; font: 600 13px '{SANS}';
}}
QPushButton:hover {{ background: {ACCENT_HOVER}; }}
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
    return f"color: {TEXT}; font: {size}px '{SERIF}'; background: transparent;"


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


def app_icon() -> QIcon:
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(logo_pixmap(s, ACCENT))
    return icon
