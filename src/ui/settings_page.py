"""Settings page (PySide6) — replaces the Tkinter settings_dialog.

Modern grouped layout with the same fields, validation, and save behavior.
Wired into the main_window.py ``QStackedWidget`` as the second page.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.version import APP_NAME

log = logging.getLogger(__name__)

_HOTKEY_CONFLICTS = {
    "ctrl+space": "Windows IME toggle / IDE autocomplete",
    "alt+space": "Windows system menu",
    "win+space": "Windows keyboard-layout switch",
    "ctrl+shift+space": "IDE method-parameter hints",
    "ctrl+alt+del": "Windows Secure Attention (reserved)",
    "win+l": "Windows lock screen (reserved)",
    "win+e": "Windows File Explorer",
    "win+r": "Windows Run dialog",
    "win+d": "Windows show desktop",
}


def _normalize_hotkey(hotkey: str) -> str:
    return "+".join(sorted(p.strip() for p in hotkey.split("+") if p.strip()))


def _hotkey_conflict(hotkey: str) -> str | None:
    return _HOTKEY_CONFLICTS.get(_normalize_hotkey(hotkey))


_PAGE_STYLE = """
QWidget#settingsPage {
    background-color: #1a1d23;
}
QGroupBox {
    color: #d7dbe0; border: 1px solid #2a2e36; border-radius: 8px;
    margin-top: 14px; padding: 14px 12px 10px 12px;
    font: bold 12px 'Segoe UI';
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 14px; padding: 0 6px;
}
QLabel { color: #b9c0c9; font: 12px 'Segoe UI'; }
QLineEdit {
    background: #20242a; color: #e6e9ee; border: 1px solid #3a4048;
    border-radius: 6px; padding: 7px 10px; font: 12px 'Segoe UI';
}
QLineEdit:focus { border: 1px solid #4a90d9; }
QComboBox {
    background: #20242a; color: #e6e9ee; border: 1px solid #3a4048;
    border-radius: 6px; padding: 7px 10px; font: 12px 'Segoe UI';
}
QComboBox::drop-down { border: none; }
QComboBox QAbstractItemView {
    background: #20242a; color: #e6e9ee; selection-background-color: #2a3040;
}
QCheckBox { color: #d7dbe0; font: 12px 'Segoe UI'; spacing: 8px; }
QCheckBox::indicator {
    width: 18px; height: 18px; border: 1px solid #3a4048;
    border-radius: 4px; background: #20242a;
}
QCheckBox::indicator:checked { background: #4a90d9; border-color: #4a90d9; }
QPushButton#saveBtn {
    background: #4a90d9; color: #fff; border: none; border-radius: 6px;
    padding: 10px 28px; font: bold 13px 'Segoe UI';
}
QPushButton#saveBtn:hover { background: #5ba0e0; }
"""


class SettingsPage(QWidget):
    """Modern settings page for the main window shell."""

    def __init__(self, settings, on_save: Callable | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self.setStyleSheet(_PAGE_STYLE)
        self._settings = settings
        self._on_save_callback = on_save
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(12)

        title = QLabel("Settings")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        title.setStyleSheet("color: #e6e9ee;")
        outer.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        container = QWidget()
        lay = QVBoxLayout(container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        # ── Hotkeys ──────────────────────────────────────────────────────
        g_hotkeys = QGroupBox("Hotkeys")
        fl = QFormLayout(g_hotkeys)
        fl.setSpacing(8)

        self._hotkey_edit = QLineEdit(self._settings.hotkey)
        fl.addRow("Hold hotkey:", self._hotkey_edit)
        self._hotkey_hint = QLabel("Hold, speak, release — types once, cleaned")
        self._hotkey_hint.setStyleSheet("color: #6a6e78; font: 10px 'Segoe UI';")
        fl.addRow("", self._hotkey_hint)

        self._live_hotkey_edit = QLineEdit(
            getattr(self._settings, "live_hotkey", "ctrl+alt+d"))
        fl.addRow("Live hotkey:", self._live_hotkey_edit)
        self._live_hint = QLabel("Tap once, then speak — types live as you talk")
        self._live_hint.setStyleSheet("color: #6a6e78; font: 10px 'Segoe UI';")
        fl.addRow("", self._live_hint)

        lay.addWidget(g_hotkeys)

        # ── Recognition ──────────────────────────────────────────────────
        g_recog = QGroupBox("Recognition")
        fl2 = QFormLayout(g_recog)
        fl2.setSpacing(8)

        self._lang_edit = QLineEdit(self._settings.language or "")
        self._lang_edit.setPlaceholderText("blank = auto-detect")
        fl2.addRow("Language:", self._lang_edit)

        self._glossary_edit = QLineEdit(", ".join(self._settings.glossary))
        self._glossary_edit.setPlaceholderText("comma-separated names/terms")
        fl2.addRow("Custom words:", self._glossary_edit)

        self._slice_edit = QLineEdit(str(getattr(self._settings, "slice_secs", 3.0)))
        fl2.addRow("Slice length (2-6s):", self._slice_edit)

        lay.addWidget(g_recog)

        # ── AI Cleanup ───────────────────────────────────────────────────
        g_ai = QGroupBox("AI Cleanup")
        fl3 = QFormLayout(g_ai)
        fl3.setSpacing(8)

        self._cleanup_combo = QComboBox()
        self._cleanup_combo.addItems(["off", "correcting", "conservative", "polish"])
        current_mode = (self._settings.cleanup_mode
                        if self._settings.cleanup_model else "off")
        idx = self._cleanup_combo.findText(current_mode)
        if idx >= 0:
            self._cleanup_combo.setCurrentIndex(idx)
        fl3.addRow("Cleanup mode:", self._cleanup_combo)

        self._polish_combo = QComboBox()
        self._polish_combo.addItems([
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "qwen/qwen3.6-27b",
        ])
        polish_model = getattr(self._settings, "polish_model", "openai/gpt-oss-120b")
        if polish_model:
            idx = self._polish_combo.findText(polish_model)
            if idx >= 0:
                self._polish_combo.setCurrentIndex(idx)
        fl3.addRow("Polish model:", self._polish_combo)

        lay.addWidget(g_ai)

        # ── Behavior ─────────────────────────────────────────────────────
        g_behavior = QGroupBox("Behavior")
        blay = QVBoxLayout(g_behavior)

        self._overlay_chk = QCheckBox("Show live indicator (waveform near cursor)")
        self._overlay_chk.setChecked(bool(getattr(self._settings, "overlay", True)))
        blay.addWidget(self._overlay_chk)

        self._tone_chk = QCheckBox("Match tone of the app being typed into")
        self._tone_chk.setChecked(bool(getattr(self._settings, "app_tone", True)))
        blay.addWidget(self._tone_chk)

        self._autostart_chk = QCheckBox("Start with Windows")
        self._autostart_chk.setChecked(self._settings.autostart)
        blay.addWidget(self._autostart_chk)

        lay.addWidget(g_behavior)

        # ── API Key ──────────────────────────────────────────────────────
        g_api = QGroupBox("Groq API Key")
        fl4 = QFormLayout(g_api)

        self._key_edit = QLineEdit()
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_edit.setPlaceholderText("Enter Groq API key")
        fl4.addRow("API key:", self._key_edit)

        self._key_hint = QLabel(
            'Don\'t have a key? '
            '<a href="https://console.groq.com/keys">Get a free one &rarr;</a>'
        )
        self._key_hint.setOpenExternalLinks(True)
        fl4.addRow("", self._key_hint)

        key_row = QHBoxLayout()
        self._show_key_chk = QCheckBox("Show")
        self._show_key_chk.toggled.connect(
            lambda on: self._key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on
                else QLineEdit.EchoMode.Password))
        key_row.addWidget(self._show_key_chk)
        key_row.addStretch()
        fl4.addRow("", key_row)

        lay.addWidget(g_api)

        lay.addStretch()

        # ── Save button ──────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._save_btn = QPushButton("Save")
        self._save_btn.setObjectName("saveBtn")
        self._save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self._save_btn)
        lay.addLayout(btn_row)

        scroll.setWidget(container)
        outer.addWidget(scroll, stretch=1)

    def _on_save(self) -> None:
        from src.config import save_settings, set_api_key, get_api_key

        hotkey = self._hotkey_edit.text().strip().lower()
        live_hotkey = self._live_hotkey_edit.text().strip().lower()

        if not hotkey or not live_hotkey:
            QMessageBox.warning(self, "Settings", "Neither hotkey can be empty.")
            return
        if _normalize_hotkey(hotkey) == _normalize_hotkey(live_hotkey):
            QMessageBox.warning(
                self, "Settings", "Hold hotkey and Live hotkey must be different.")
            return
        for label, hk in [("Hold hotkey", hotkey), ("Live hotkey", live_hotkey)]:
            conflict = _hotkey_conflict(hk)
            if conflict:
                QMessageBox.warning(
                    self, "Settings",
                    f"{label} '{hk}' conflicts with {conflict}. Pick another.")
                return

        # Save API key if provided
        key = self._key_edit.text().strip()
        if key:
            try:
                set_api_key(key)
            except Exception as e:
                QMessageBox.critical(self, "Settings",
                                     f"Could not save API key: {e}")
                return

        # Validate slice length
        try:
            slice_secs = float(self._slice_edit.text().strip())
            if not 2.0 <= slice_secs <= 6.0:
                raise ValueError
        except ValueError:
            QMessageBox.warning(
                self, "Settings",
                "Slice length must be a number from 2 to 6.")
            return

        cleanup_mode = self._cleanup_combo.currentText()
        updated = type(self._settings)(**{
            **asdict(self._settings),
            "hotkey": hotkey,
            "live_hotkey": live_hotkey,
            "language": self._lang_edit.text().strip() or None,
            "cleanup_model": ("openai/gpt-oss-20b"
                              if cleanup_mode != "off" else None),
            "cleanup_mode": (cleanup_mode
                             if cleanup_mode != "off" else "correcting"),
            "polish_model": (self._polish_combo.currentText()
                             if cleanup_mode != "off" else None),
            "autostart": self._autostart_chk.isChecked(),
            "overlay": self._overlay_chk.isChecked(),
            "app_tone": self._tone_chk.isChecked(),
            "slice_secs": slice_secs,
            "glossary": [t.strip() for t in
                         self._glossary_edit.text().split(",") if t.strip()],
        })
        save_settings(updated)
        QMessageBox.information(self, "Settings", "Settings saved.")
        if self._on_save_callback:
            try:
                self._on_save_callback(updated)
            except Exception as e:
                log.error("Post-save hook failed: %s", e)
