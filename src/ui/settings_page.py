"""Settings page (PySide6) — replaces the Tkinter settings_dialog.

macOS-style grouped rows (label left, control right) with the same
fields, validation, and save behavior.
Wired into the main_window.py ``QStackedWidget`` as the second page.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.config import DEFAULT_CLEANUP_MODEL
from src.ui import theme as T
from src.ui.icons import ToggleSwitch
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


_PAGE_STYLE = f"""
QWidget#settingsPage, QWidget#settingsBody {{ background-color: {T.BG}; }}
QFrame#group {{ background: {T.SURFACE}; border-radius: 12px; }}
QFrame#divider {{ background: #1f1f1f; border: none; }}
QLabel {{ background: transparent; }}
QLineEdit, QComboBox {{
    background: #1a1a1a; color: {T.TEXT}; border: 1px solid #262626;
    border-radius: 7px; padding: 5px 10px; font: 13px '{T.SANS}';
    selection-background-color: {T.ACCENT};
}}
QLineEdit:focus, QComboBox:focus {{ border: 1px solid {T.ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {T.SURFACE}; color: {T.TEXT}; border: 1px solid {T.BORDER};
    selection-background-color: #222222; selection-color: {T.TEXT};
}}
QPushButton#saveBtn {{ padding: 9px 30px; }}
{T.BUTTONS}
{T.SCROLLBARS}
"""

_ROW_TITLE = f"color: {T.TEXT}; font: 14px '{T.SANS}';"
_ROW_HINT = f"color: {T.TEXT_FAINT}; font: 12px '{T.SANS}';"
_GROUP_TITLE = f"color: {T.TEXT_MUTED}; font: 600 12px '{T.SANS}';"


class SettingsPage(QWidget):
    """Settings in the macOS style: grouped rows, label left, control right."""

    def __init__(self, settings, on_save: Callable | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self.setStyleSheet(_PAGE_STYLE)
        self._settings = settings
        self._on_save_callback = on_save
        self._build_ui()

    # ── layout helpers ──────────────────────────────────────────────────
    def _group(self, lay: QVBoxLayout, title: str) -> QVBoxLayout:
        head = QLabel(title.upper())
        head.setStyleSheet(_GROUP_TITLE)
        head.setContentsMargins(4, 0, 0, 0)
        lay.addSpacing(14)
        lay.addWidget(head)
        lay.addSpacing(6)
        frame = QFrame()
        frame.setObjectName("group")
        rows = QVBoxLayout(frame)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(0)
        lay.addWidget(frame)
        return rows

    def _row(self, rows: QVBoxLayout, title: str, control: QWidget,
             hint: str = "", control_width: int | None = None) -> None:
        if rows.count():
            line = QFrame()
            line.setObjectName("divider")
            line.setFixedHeight(1)
            wrap = QHBoxLayout()
            wrap.setContentsMargins(16, 0, 0, 0)  # Mac dividers start inset
            wrap.addWidget(line)
            rows.addLayout(wrap)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(16, 10, 14, 10)
        h.setSpacing(16)
        text = QVBoxLayout()
        text.setSpacing(2)
        t = QLabel(title)
        t.setStyleSheet(_ROW_TITLE)
        text.addWidget(t)
        if hint:
            hl = QLabel(hint)
            hl.setStyleSheet(_ROW_HINT)
            hl.setWordWrap(True)
            hl.setOpenExternalLinks(True)
            text.addWidget(hl)
        h.addLayout(text, stretch=1)
        if control_width:
            control.setFixedWidth(control_width)
        h.addWidget(control, alignment=Qt.AlignmentFlag.AlignVCenter)
        rows.addWidget(row)

    # ── page ────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 30, 32, 20)
        outer.setSpacing(8)

        title = QLabel("Settings")
        title.setStyleSheet(T.page_title_style(28))
        outer.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        container.setObjectName("settingsBody")
        lay = QVBoxLayout(container)
        lay.setContentsMargins(0, 0, 12, 0)
        lay.setSpacing(0)

        s = self._settings

        # ── Dictation ────────────────────────────────────────────────────
        rows = self._group(lay, "Dictation")
        self._hotkey_edit = QLineEdit(s.hotkey)
        self._row(rows, "Hold to dictate", self._hotkey_edit,
                  "Hold, speak, release — typed once, cleaned", 170)
        self._live_hotkey_edit = QLineEdit(getattr(s, "live_hotkey", "ctrl+alt+d"))
        self._row(rows, "Hands-free typing", self._live_hotkey_edit,
                  "Tap once, then speak — types live as you talk", 170)
        self._overlay_chk = ToggleSwitch(bool(getattr(s, "overlay", True)), T.ACCENT)
        self._row(rows, "Show floating indicator", self._overlay_chk,
                  "The dots above the taskbar")
        self._autostart_chk = ToggleSwitch(bool(s.autostart), T.ACCENT)
        self._row(rows, "Start with Windows", self._autostart_chk)

        # ── Recognition ──────────────────────────────────────────────────
        rows = self._group(lay, "Recognition")
        self._lang_edit = QLineEdit(s.language or "")
        self._lang_edit.setPlaceholderText("auto")
        self._row(rows, "Language", self._lang_edit,
                  "Language code, e.g. en. Leave empty to detect automatically.", 90)
        self._glossary_edit = QLineEdit(", ".join(s.glossary))
        self._glossary_edit.setPlaceholderText("Names, terms…")
        self._row(rows, "Custom words", self._glossary_edit,
                  "Comma-separated names and terms to spell exactly", 240)
        self._slice_edit = QLineEdit(str(getattr(s, "slice_secs", 3.0)))
        self._row(rows, "Live preview interval", self._slice_edit,
                  "Seconds between live updates (2–6). Lower is snappier.", 70)

        # ── AI cleanup ───────────────────────────────────────────────────
        rows = self._group(lay, "AI cleanup")
        self._cleanup_combo = QComboBox()
        self._cleanup_combo.addItems(["off", "correcting", "conservative", "polish"])
        current_mode = s.cleanup_mode if s.cleanup_model else "off"
        idx = self._cleanup_combo.findText(current_mode)
        if idx >= 0:
            self._cleanup_combo.setCurrentIndex(idx)
        self._row(rows, "Cleanup mode", self._cleanup_combo,
                  "How much the text is tidied after you speak", 170)
        self._polish_combo = QComboBox()
        self._polish_combo.addItems([
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "qwen/qwen3.8-27b",
        ])
        polish_model = getattr(s, "polish_model", "openai/gpt-oss-120b")
        if polish_model:
            idx = self._polish_combo.findText(polish_model)
            if idx >= 0:
                self._polish_combo.setCurrentIndex(idx)
        self._row(rows, "Polish model", self._polish_combo,
                  "Used by the Polish button", 200)
        self._tone_chk = ToggleSwitch(bool(getattr(s, "app_tone", True)), T.ACCENT)
        self._row(rows, "Match the app's tone", self._tone_chk,
                  "Casual in chat, formal in documents")

        # ── Groq API key ─────────────────────────────────────────────────
        rows = self._group(lay, "Groq API key")
        self._key_edit = QLineEdit()
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_edit.setPlaceholderText("Paste a new key to replace it")
        self._row(rows, "API key", self._key_edit,
                  f'Don\'t have one? <a style="color:{T.ACCENT_HOVER}; '
                  'text-decoration:none" href="https://console.groq.com/keys">'
                  'Get a free key &rarr;</a>', 240)
        self._show_key_chk = ToggleSwitch(False, T.ACCENT)
        self._show_key_chk.toggled.connect(
            lambda on: self._key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on
                else QLineEdit.EchoMode.Password))
        self._row(rows, "Show key", self._show_key_chk)

        lay.addSpacing(18)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._save_btn = QPushButton("Save")
        self._save_btn.setObjectName("saveBtn")
        self._save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self._save_btn)
        lay.addLayout(btn_row)
        lay.addStretch()

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
            "cleanup_model": (DEFAULT_CLEANUP_MODEL
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
