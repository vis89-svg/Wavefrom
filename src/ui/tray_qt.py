"""System tray icon via QSystemTrayIcon (replaces pystray).

Lives on the Qt main thread -- no competing native message loop.
Same public API as the old TrayIcon so main.py wiring stays unchanged.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

log = logging.getLogger(__name__)

STATE_COLORS = {
    "idle": "#787878",
    "recording": "#e63c3c",
    "transcribing": "#f0aa28",
    "cleaning": "#5aa0dc",
    "error": "#c82828",
}


def _make_icon(color_hex: str = "#787878") -> QIcon:
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QColor(color_hex)
    p.setBrush(c)
    p.setPen(c)
    p.drawEllipse(6, 6, 52, 52)
    p.setBrush(QColor(255, 255, 255))
    p.drawEllipse(22, 22, 20, 20)
    p.end()
    return QIcon(pixmap)


class TrayIcon(QObject):
    """Qt system-tray icon.  Create on the main thread before app.exec().

    `set_state()` is called from DictationEngine's background worker thread,
    so it only emits a signal here -- the actual QSystemTrayIcon/QPixmap work
    happens on the GUI thread via the queued connection below.
    """

    _sig_set_state = Signal(str)

    def __init__(self, on_quit=None, on_remap=None, on_settings=None,
                 on_show=None):
        super().__init__()
        self._sig_set_state.connect(self._apply_state, Qt.ConnectionType.QueuedConnection)
        self._tray = QSystemTrayIcon()
        self._tray.setIcon(_make_icon())
        self._tray.setToolTip("Waveform \u2014 idle")

        menu = QMenu()
        if on_show:
            a_open = QAction("Open", menu)
            a_open.triggered.connect(on_show)
            menu.addAction(a_open)
            menu.addSeparator()
        if on_remap:
            a_remap = QAction("Remap hotkey", menu)
            a_remap.triggered.connect(on_remap)
            menu.addAction(a_remap)
        menu.addSeparator()
        if on_settings:
            a_settings = QAction("Settings", menu)
            a_settings.triggered.connect(on_settings)
            menu.addAction(a_settings)
        menu.addSeparator()
        if on_quit:
            a_quit = QAction("Quit", menu)
            a_quit.triggered.connect(on_quit)
            menu.addAction(a_quit)
        self._tray.setContextMenu(menu)

    def start(self) -> None:
        self._tray.show()

    def set_state(self, state: str) -> None:
        self._sig_set_state.emit(state)

    def _apply_state(self, state: str) -> None:
        color = STATE_COLORS.get(state, "#787878")
        self._tray.setIcon(_make_icon(color))
        self._tray.setToolTip(f"Waveform \u2014 {state}")

    def stop(self) -> None:
        self._tray.hide()
