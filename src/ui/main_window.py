"""Main app window with sidebar navigation (PySide6).

A ``QMainWindow`` with a sidebar on the left and a ``QStackedWidget`` on the
right that switches between the History and Settings pages.  Shown after
successful login.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

_SHELL_STYLE = """
QMainWindow {
    background-color: #1a1d23;
}
QWidget#sidebar {
    background-color: #14161b;
    border-right: 1px solid #2a2e36;
}
QPushButton#navBtn {
    background: transparent;
    color: #8a8f98;
    border: none;
    border-radius: 8px;
    padding: 10px 16px;
    font: 13px 'Segoe UI';
    text-align: left;
}
QPushButton#navBtn:hover {
    background: #252930;
    color: #d7dbe0;
}
QPushButton#navBtn:checked {
    background: #2a3040;
    color: #e6e9ee;
    font-weight: bold;
}
QStackedWidget {
    background: #1a1d23;
}
"""


class MainWindow(QMainWindow):
    """Shell window with sidebar + stacked pages."""

    def __init__(self, history_page: QWidget, settings_page: QWidget,
                 display_name: str = "") -> None:
        super().__init__()
        self.setWindowTitle("VoiceFlow Dictation")
        self.setMinimumSize(800, 520)
        self.setStyleSheet(_SHELL_STYLE)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Sidebar ──────────────────────────────────────────────────────
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(180)
        sb_lay = QVBoxLayout(sidebar)
        sb_lay.setContentsMargins(12, 20, 12, 20)
        sb_lay.setSpacing(4)

        app_title = QLabel("VoiceFlow")
        app_title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        app_title.setStyleSheet("color: #e6e9ee;")
        sb_lay.addWidget(app_title)
        sb_lay.addSpacing(16)

        self._nav_buttons: list[QPushButton] = []
        self._stack = QStackedWidget()

        for label, page in [("History", history_page), ("Settings", settings_page)]:
            btn = QPushButton(label)
            btn.setObjectName("navBtn")
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, b=btn, p=page: self._switch(b, p))
            sb_lay.addWidget(btn)
            self._nav_buttons.append(btn)
            self._stack.addWidget(page)

        sb_lay.addStretch()

        if display_name:
            user_lbl = QLabel(display_name)
            user_lbl.setStyleSheet("color: #6a6e78; font: 11px 'Segoe UI';")
            sb_lay.addWidget(user_lbl)

        outer.addWidget(sidebar)
        outer.addWidget(self._stack, stretch=1)

        # Default to History page
        if self._nav_buttons:
            self._nav_buttons[0].setChecked(True)
            self._stack.setCurrentWidget(history_page)

    def _switch(self, btn: QPushButton, page: QWidget) -> None:
        for b in self._nav_buttons:
            if b is not btn:
                b.setChecked(False)
        self._stack.setCurrentWidget(page)
        if hasattr(page, "refresh"):
            page.refresh()

    def closeEvent(self, event: QEvent) -> None:
        """Hide to tray instead of quitting — app keeps running in tray."""
        event.ignore()
        self.hide()

    def refresh_history(self) -> None:
        """Refresh the history page (call after a new dictation is recorded)."""
        page = self._stack.widget(0)
        if hasattr(page, "refresh"):
            page.refresh()
