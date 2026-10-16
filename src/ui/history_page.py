"""History page: past dictations as a feed of cards, grouped by day.

Backed by :mod:`src.history` (SQLite). Each card shows the local time and
the first lines of the text; click to expand, hover for Copy / Delete.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pyperclip

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui import theme as T
from src.ui.icons import button_icon

log = logging.getLogger(__name__)

_COLLAPSED_LINES = 3

_HISTORY_STYLE = f"""
QWidget#historyPage {{ background-color: {T.BG}; }}
QWidget#feed {{ background: transparent; }}
QFrame#card {{
    background: {T.SURFACE}; border: 1px solid {T.SURFACE}; border-radius: 12px;
}}
QFrame#card:hover {{ border: 1px solid {T.BORDER}; }}
QLineEdit#search {{ border-radius: 10px; padding: 9px 14px; }}
QPushButton::menu-indicator {{ image: none; width: 0; }}
{T.INPUTS}
{T.BUTTONS}
{T.SCROLLBARS}
{T.MENUS}
"""


def local_dt(ts: str) -> datetime | None:
    """Stored UTC ISO timestamp -> local datetime (None if unparseable)."""
    try:
        return datetime.fromisoformat(ts).astimezone()
    except (TypeError, ValueError):
        return None


def day_label(d: date, today: date | None = None) -> str:
    today = today or date.today()
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    if d.year == today.year:
        return f"{d:%A}, {d.day} {d:%B}"
    return f"{d.day} {d:%B %Y}"


class _Card(QFrame):
    """One dictation: time + text, expandable, hover actions."""

    def __init__(self, entry: dict, on_delete, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._entry = entry
        self._on_delete = on_delete
        self._expanded = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 12, 12)
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(4)
        dt = local_dt(entry["ts"])
        self._time = QLabel(dt.strftime("%H:%M") if dt else "")
        self._time.setStyleSheet(T.label_style(T.TEXT_FAINT, 12))
        top.addWidget(self._time)
        top.addStretch()
        self._copy_btn = self._action("Copy", "Copy to clipboard", self._copy, "copy")
        self._del_btn = self._action("Delete", "Delete this dictation",
                                     self._delete, "trash")
        top.addWidget(self._copy_btn)
        top.addWidget(self._del_btn)
        lay.addLayout(top)

        self._text = QLabel(entry["text"])
        self._text.setWordWrap(True)
        # Top-aligned so a collapsed card shows its first lines, not a
        # vertically centred slice of the middle.
        self._text.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._text.setStyleSheet(T.label_style(T.TEXT, 14))
        self._text.setSizePolicy(QSizePolicy.Policy.Preferred,
                                 QSizePolicy.Policy.Minimum)
        lay.addWidget(self._text)
        self._apply_collapse()

    def _action(self, text: str, tip: str, slot, icon: str) -> QPushButton:
        b = QPushButton(" " + text)
        b.setIcon(button_icon(icon, 14, T.TEXT_MUTED, T.TEXT))
        b.setIconSize(QSize(14, 14))
        b.setProperty("kind", "ghost")
        b.setToolTip(tip)
        b.setFixedHeight(26)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(slot)
        sp = b.sizePolicy()
        sp.setRetainSizeWhenHidden(True)  # no layout jump on hover
        b.setSizePolicy(sp)
        b.hide()
        return b

    def _apply_collapse(self) -> None:
        fm = QFontMetrics(self._text.font())
        if self._expanded:
            self._text.setMaximumHeight(16777215)
        else:
            self._text.setMaximumHeight(fm.lineSpacing() * _COLLAPSED_LINES)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._expanded = not self._expanded
            self._apply_collapse()
        super().mouseReleaseEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._copy_btn.show()
        self._del_btn.show()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._copy_btn.hide()
        self._del_btn.hide()
        super().leaveEvent(event)

    def _copy(self) -> None:
        try:
            pyperclip.copy(self._entry["text"])
            self._copy_btn.setText(" Copied")
            self._copy_btn.setIcon(button_icon("check", 14, T.SUCCESS))
            QTimer.singleShot(1500, self._reset_copy)
        except Exception as e:
            log.warning("Clipboard copy failed: %s", e)

    def _reset_copy(self) -> None:
        self._copy_btn.setText(" Copy")
        self._copy_btn.setIcon(button_icon("copy", 14, T.TEXT_MUTED, T.TEXT))

    def _delete(self) -> None:
        self._on_delete(self._entry["id"])


class HistoryPage(QWidget):
    """Search box + day-grouped feed of past dictations."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("historyPage")
        self.setStyleSheet(_HISTORY_STYLE)
        self._entries: list[dict] = []
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 30, 32, 20)
        outer.setSpacing(16)

        hdr = QHBoxLayout()
        title = QLabel("History")
        title.setStyleSheet(T.page_title_style(28))
        hdr.addWidget(title)
        hdr.addStretch()
        self._menu_btn = QPushButton("•••")
        self._menu_btn.setProperty("kind", "ghost")
        self._menu_btn.setFixedSize(40, 32)
        self._menu_btn.setToolTip("More")
        menu = QMenu(self)
        menu.addAction("Clear all history…", self._on_clear)
        self._menu_btn.setMenu(menu)
        hdr.addWidget(self._menu_btn)
        outer.addLayout(hdr)

        self._search = QLineEdit()
        self._search.setObjectName("search")
        self._search.setPlaceholderText("Search your dictations")
        self._search.textChanged.connect(self._on_search)
        outer.addWidget(self._search)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._feed = QWidget()
        self._feed.setObjectName("feed")
        self._feed_lay = QVBoxLayout(self._feed)
        self._feed_lay.setContentsMargins(0, 0, 8, 0)
        self._feed_lay.setSpacing(8)
        self._scroll.setWidget(self._feed)
        outer.addWidget(self._scroll, stretch=1)

    def refresh(self) -> None:
        """Reload history from the database and rebuild the feed."""
        from src.history import search
        self._entries = search(self._search.text())
        while self._feed_lay.count():
            item = self._feed_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._entries:
            self._feed_lay.addStretch()
            self._feed_lay.addWidget(self._empty_state(), alignment=Qt.AlignmentFlag.AlignCenter)
            self._feed_lay.addStretch()
            return

        current_day = None
        for e in self._entries:
            dt = local_dt(e["ts"])
            d = dt.date() if dt else None
            if d != current_day:
                current_day = d
                lbl = QLabel(day_label(d) if d else "Earlier")
                lbl.setStyleSheet(T.label_style(T.TEXT_MUTED, 13, "600"))
                if self._feed_lay.count():
                    self._feed_lay.addSpacing(10)
                self._feed_lay.addWidget(lbl)
            self._feed_lay.addWidget(_Card(e, self._on_delete))
        self._feed_lay.addStretch()

    def _empty_state(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(T.logo_pixmap(80, T.BORDER))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(logo)
        searching = bool(self._search.text().strip())
        head = QLabel("No matches" if searching else "No dictations yet")
        head.setStyleSheet(T.page_title_style(20))
        head.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(head)
        sub = QLabel("Try a different word." if searching else
                     "Hold Ctrl + Win and speak — your words will show up here.")
        sub.setStyleSheet(T.label_style(T.TEXT_FAINT, 13))
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(sub)
        return box

    def _on_search(self, text: str) -> None:
        self.refresh()

    def _on_delete(self, entry_id: int) -> None:
        from src.history import delete
        delete(entry_id)
        QTimer.singleShot(0, self.refresh)  # not from inside the card's own click

    def _on_clear(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Clear history")
        box.setText("Delete all saved dictations? This can't be undone.")
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        from src.history import clear
        clear()
        self.refresh()
