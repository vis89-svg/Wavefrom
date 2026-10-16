"""Main app window with sidebar navigation (PySide6).

A ``QMainWindow`` with a sidebar on the left and a ``QStackedWidget`` on the
right that switches between the History and Settings pages.  Shown after
successful login.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.ui import theme as T
from src.ui.icons import nav_icon

log = logging.getLogger(__name__)

_SHELL_STYLE = f"""
QMainWindow {{ background-color: {T.BG}; }}
QWidget#sidebar {{ background-color: {T.SIDEBAR}; border-right: 1px solid #141414; }}
QPushButton#navBtn {{
    background: transparent; color: {T.TEXT_MUTED}; border: none;
    border-radius: 8px; padding: 8px 10px; font: 14px '{T.SANS}';
    text-align: left;
}}
QPushButton#navBtn:hover {{ background: #121212; color: {T.TEXT}; }}
QPushButton#navBtn:checked {{ background: #222222; color: #ffffff; }}
QStackedWidget {{ background: {T.BG}; }}
{T.MENUS}
"""

_ICONS = {"Dashboard": "dashboard", "History": "history", "Settings": "settings"}


class MainWindow(QMainWindow):
    """Shell window with sidebar + stacked pages."""

    def __init__(self, history_page: QWidget, settings_page: QWidget,
                 display_name: str = "", dashboard_page: QWidget | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Waveform")
        self.setWindowIcon(T.app_icon())
        self.setMinimumSize(860, 560)
        self.setStyleSheet(_SHELL_STYLE)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Sidebar ──────────────────────────────────────────────────────
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(208)
        sb_lay = QVBoxLayout(sidebar)
        sb_lay.setContentsMargins(14, 20, 14, 16)
        sb_lay.setSpacing(2)

        brand = QHBoxLayout()
        brand.setContentsMargins(6, 0, 0, 0)
        brand.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(T.tile_pixmap(30))
        logo.setFixedSize(30, 30)
        brand.addWidget(logo)
        name = QLabel("Waveform")
        name.setStyleSheet(T.page_title_style(20))
        brand.addWidget(name)
        brand.addStretch()
        sb_lay.addLayout(brand)
        sb_lay.addSpacing(22)

        self._nav_buttons: list[QPushButton] = []
        self._stack = QStackedWidget()

        pages = [("Dashboard", dashboard_page), ("History", history_page),
                 ("Settings", settings_page)]
        self._pages = [(label, page) for label, page in pages if page is not None]
        for label, page in self._pages:
            btn = QPushButton(f"  {label}")
            btn.setObjectName("navBtn")
            if label in _ICONS:
                # Muted when unselected, white when selected (checked state).
                btn.setIcon(nav_icon(_ICONS[label], 18, T.TEXT_MUTED, "#ffffff"))
                btn.setIconSize(QSize(18, 18))
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, b=btn, p=page: self._switch(b, p))
            sb_lay.addWidget(btn)
            self._nav_buttons.append(btn)
            self._stack.addWidget(page)

        sb_lay.addStretch()

        if display_name:
            user_row = QHBoxLayout()
            user_row.setContentsMargins(6, 0, 0, 0)
            user_row.setSpacing(10)
            avatar = QLabel(display_name[:1].upper())
            avatar.setFixedSize(28, 28)
            avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
            avatar.setStyleSheet(
                f"background: {T.ACCENT_SOFT}; color: {T.ACCENT_HOVER}; "
                f"border-radius: 14px; font: 600 13px '{T.SANS}';")
            user_row.addWidget(avatar)
            user_lbl = QLabel(display_name)
            user_lbl.setStyleSheet(T.label_style(T.TEXT_MUTED, 13))
            user_row.addWidget(user_lbl)
            user_row.addStretch()
            sb_lay.addLayout(user_row)

        outer.addWidget(sidebar)
        outer.addWidget(self._stack, stretch=1)

        # Start on the first page (Dashboard when present)
        if self._nav_buttons:
            self._nav_buttons[0].setChecked(True)
            self._stack.setCurrentWidget(self._pages[0][1])

    def show_page(self, name: str) -> None:
        """Select the page called `name` ("Dashboard", "History", "Settings")."""
        for btn, (label, page) in zip(self._nav_buttons, self._pages):
            if label == name:
                self._switch(btn, page)
                return

    def _switch(self, btn: QPushButton, page: QWidget) -> None:
        for b in self._nav_buttons:
            b.setChecked(b is btn)
        self._stack.setCurrentWidget(page)
        if hasattr(page, "refresh"):
            page.refresh()

    def closeEvent(self, event: QEvent) -> None:
        """Hide to tray instead of quitting — app keeps running in tray."""
        event.ignore()
        self.hide()

    def refresh_history(self) -> None:
        """Refresh History and Dashboard (call after a new dictation is recorded)."""
        for label, page in self._pages:
            if label in ("History", "Dashboard") and hasattr(page, "refresh"):
                page.refresh()
