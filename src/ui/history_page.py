"""History page: shows a list of past dictations with search.

Backed by :mod:`src.history` (SQLite).  Clicking a row shows the full text
with a Copy button.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

import pyperclip

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

_HISTORY_STYLE = """
QWidget#historyPage {
    background-color: #1a1d23;
}
QLabel { color: #d7dbe0; }
QLineEdit {
    background: #20242a; color: #e6e9ee; border: 1px solid #3a4048;
    border-radius: 6px; padding: 8px 12px; font: 12px 'Segoe UI';
}
QLineEdit:focus { border: 1px solid #4a90d9; }
QListWidget {
    background: #20242a; color: #d7dbe0; border: 1px solid #3a4048;
    border-radius: 6px; font: 12px 'Segoe UI';
}
QListWidget::item { padding: 8px; }
QListWidget::item:selected { background: #2a3040; }
QListWidget::item:hover { background: #252930; }
QTextEdit {
    background: #20242a; color: #e6e9ee; border: 1px solid #3a4048;
    border-radius: 6px; padding: 8px; font: 12px 'Segoe UI';
}
QPushButton {
    background: #4a90d9; color: #fff; border: none; border-radius: 6px;
    padding: 8px 18px; font: bold 12px 'Segoe UI';
}
QPushButton:hover { background: #5ba0e0; }
QPushButton#clearBtn {
    background: #e5484d;
}
QPushButton#clearBtn:hover {
    background: #ff5b60;
}
"""


class HistoryPage(QWidget):
    """QWidget showing past dictations with search, list, and detail view."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("historyPage")
        self.setStyleSheet(_HISTORY_STYLE)
        self._entries: list[dict] = []
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(12)

        hdr = QHBoxLayout()
        title = QLabel("History")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        hdr.addWidget(title)
        hdr.addStretch()

        self._clear_btn = QPushButton("Clear All")
        self._clear_btn.setObjectName("clearBtn")
        self._clear_btn.setFixedHeight(30)
        self._clear_btn.clicked.connect(self._on_clear)
        hdr.addWidget(self._clear_btn)
        outer.addLayout(hdr)

        # Search
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search dictations...")
        self._search.textChanged.connect(self._on_search)
        outer.addWidget(self._search)

        # Splitter: list | detail
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_select)
        splitter.addWidget(self._list)

        self._detail = QTextEdit()
        self._detail.setReadOnly(True)
        splitter.addWidget(self._detail)

        splitter.setSizes([320, 400])
        outer.addWidget(splitter, stretch=1)

        # Copy button
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._copy_btn = QPushButton("Copy to Clipboard")
        self._copy_btn.setFixedHeight(32)
        self._copy_btn.clicked.connect(self._on_copy)
        self._copy_btn.setEnabled(False)
        btn_row.addWidget(self._copy_btn)
        outer.addLayout(btn_row)

    def refresh(self) -> None:
        """Reload history from the database."""
        from src.history import search
        self._entries = search(self._search.text())
        self._list.clear()
        for e in self._entries:
            ts = e["ts"][:19].replace("T", " ")
            preview = e["text"][:60].replace("\n", " ")
            self._list.addItem(f"{ts}  {preview}")

    def _on_search(self, text: str) -> None:
        self.refresh()

    def _on_select(self, row: int) -> None:
        if 0 <= row < len(self._entries):
            self._detail.setPlainText(self._entries[row]["text"])
            self._copy_btn.setEnabled(True)
        else:
            self._detail.clear()
            self._copy_btn.setEnabled(False)

    def _on_copy(self) -> None:
        row = self._list.currentRow()
        if 0 <= row < len(self._entries):
            try:
                pyperclip.copy(self._entries[row]["text"])
                self._copy_btn.setText("Copied!")
                from PySide6.QtCore import QTimer
                QTimer.singleShot(1500, lambda: self._copy_btn.setText("Copy to Clipboard"))
            except Exception as e:
                log.warning("Clipboard copy failed: %s", e)

    def _on_clear(self) -> None:
        from src.history import clear
        clear()
        self.refresh()
