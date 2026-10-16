"""First-run setup (PySide6): get a Groq key, paste it, try a dictation.

Shown at startup whenever no API key is configured (cloud mode). The key is
checked against Groq before it is saved, and the last step gives the user a
text box to dictate into, so they see the whole flow working once.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.ui import theme as T
from src.version import APP_NAME

log = logging.getLogger(__name__)

KEYS_URL = "https://console.groq.com/keys"
TEST_SENTENCE = "Hello, this is my first dictation with Waveform."

_STYLE = """
QDialog { background-color: #262624; }
QLabel { color: #a09d96; font: 12px 'Segoe UI'; }
QLabel#stepTitle { color: #ece9e1; font: bold 13px 'Segoe UI'; }
QLabel#stepNum {
    background: #3a2e28; color: #ece9e1; border-radius: 12px;
    font: bold 12px 'Segoe UI';
}
QLabel#stepNum[done="true"] { background: #6d8f5b; }
QLabel#ok { color: #7fa36b; }
QLabel#error { color: #d1584f; }
QWidget#step[active="false"] QLabel { color: #75726c; }
QLineEdit, QPlainTextEdit {
    background: #30302e; color: #ece9e1; border: 1px solid #3d3d3a;
    border-radius: 6px; padding: 7px 10px; font: 12px 'Segoe UI';
}
QLineEdit:focus, QPlainTextEdit:focus { border: 1px solid #c8744f; }
QLineEdit:disabled, QPlainTextEdit:disabled { color: #75726c; border-color: #30302e; }
QCheckBox { color: #a09d96; font: 11px 'Segoe UI'; spacing: 8px; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #3d3d3a; border-radius: 4px; background: #30302e; }
QCheckBox::indicator:checked { background: #c8744f; border-color: #c8744f; }
QPushButton {
    background: #c8744f; color: #fff; border: none; border-radius: 6px;
    padding: 8px 18px; font: bold 12px 'Segoe UI';
}
QPushButton:hover { background: #d4865f; }
QPushButton:pressed { background: #b5663f; }
QPushButton:disabled { background: #30302e; color: #75726c; }
QPushButton#secondary { background: #3d3d3a; }
QPushButton#secondary:hover { background: #45443f; }
"""


def _pretty_hotkey(hotkey: str) -> str:
    return " + ".join(p.strip().capitalize() for p in hotkey.split("+") if p.strip())


def check_api_key(key: str) -> tuple[bool, str]:
    """Ask Groq whether `key` works. Returns (ok, message for the user)."""
    try:
        import groq
        groq.Groq(api_key=key, timeout=15.0, max_retries=1).models.list()
        return True, "Key works and is saved."
    except Exception as e:
        name = type(e).__name__
        log.info("API key check failed: %s", name)
        if name in ("AuthenticationError", "PermissionDeniedError"):
            return False, "Groq rejected this key. Copy it again and paste the whole key."
        if name in ("APIConnectionError", "APITimeoutError"):
            return False, "Couldn't reach Groq. Check your internet connection."
        return False, f"Couldn't check the key ({name}). Try again."


class _CheckBridge(QObject):
    """Carries the background key check's result back to the GUI thread."""
    done = Signal(bool, str, str)  # ok, message, key


class SetupWindow(QDialog):
    def __init__(self, hotkey: str, on_key_saved: Callable[[], None] | None = None,
                 save_key: Callable[[str], None] | None = None,
                 check_key: Callable[[str], tuple[bool, str]] = check_api_key,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_key_saved = on_key_saved
        self._save_key = save_key or _default_save_key
        self._check_key = check_key
        self._bridge = _CheckBridge()
        self._bridge.done.connect(self._on_check_done)

        self.setWindowTitle(f"Set up {APP_NAME}")
        self.setWindowIcon(T.app_icon())
        self.setStyleSheet(_STYLE)
        self.setMinimumWidth(480)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.setSpacing(14)

        head = QHBoxLayout()
        head.setSpacing(12)
        logo = QLabel()
        logo.setPixmap(T.logo_pixmap(44, T.ACCENT))
        head.addWidget(logo)
        title = QLabel(f"Welcome to {APP_NAME}")
        title.setStyleSheet(T.page_title_style(24))
        head.addWidget(title)
        head.addStretch()
        lay.addLayout(head)
        sub = QLabel("Three quick steps and you can dictate anywhere.")
        lay.addWidget(sub)

        # Step 1: get a key
        self._step1, self._num1, body1 = self._step(
            1, "Get a free Groq API key",
            "Waveform uses Groq's free speech recognition. Sign in and create "
            "a key (it starts with gsk_).")
        open_btn = QPushButton("Open Groq console")
        open_btn.clicked.connect(self._open_console)
        body1.addWidget(open_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self._step1)

        # Step 2: paste it
        self._step2, self._num2, body2 = self._step(
            2, "Paste your key", "It's saved on this computer only.")
        row = QHBoxLayout()
        self._key_edit = QLineEdit()
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_edit.setPlaceholderText("gsk_...")
        self._key_edit.returnPressed.connect(self._on_save_key)
        row.addWidget(self._key_edit, stretch=1)
        self._save_btn = QPushButton("Save key")
        self._save_btn.clicked.connect(self._on_save_key)
        row.addWidget(self._save_btn)
        body2.addLayout(row)
        show = QCheckBox("Show key")
        show.toggled.connect(lambda on: self._key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        body2.addWidget(show)
        self._key_status = QLabel("")
        self._key_status.setWordWrap(True)
        body2.addWidget(self._key_status)
        lay.addWidget(self._step2)

        # Step 3: try it
        hk = _pretty_hotkey(hotkey)
        self._step3, self._num3, body3 = self._step(
            3, "Try it",
            f"Click in the box, hold {hk}, say “{TEST_SENTENCE}” "
            "and let go.")
        self._try_box = QPlainTextEdit()
        self._try_box.setPlaceholderText("Your words will appear here...")
        self._try_box.setFixedHeight(70)
        self._try_box.textChanged.connect(self._on_try_text)
        body3.addWidget(self._try_box)
        self._try_status = QLabel("")
        self._try_status.setObjectName("ok")
        body3.addWidget(self._try_status)
        lay.addWidget(self._step3)
        self._set_active(self._step3, False)

        # Footer
        foot = QHBoxLayout()
        foot.addStretch()
        self._skip_btn = QPushButton("Skip for now")
        self._skip_btn.setObjectName("secondary")
        self._skip_btn.clicked.connect(self.close)
        foot.addWidget(self._skip_btn)
        self._done_btn = QPushButton("Done")
        self._done_btn.setEnabled(False)
        self._done_btn.clicked.connect(self.close)
        foot.addWidget(self._done_btn)
        lay.addLayout(foot)

    # ------------------------------------------------------------ layout
    def _step(self, n: int, title: str, hint: str):
        box = QWidget()
        box.setObjectName("step")
        box.setProperty("active", "true")
        outer = QHBoxLayout(box)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        num = QLabel(str(n))
        num.setObjectName("stepNum")
        num.setFixedSize(24, 24)
        num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(num, alignment=Qt.AlignmentFlag.AlignTop)
        body = QVBoxLayout()
        body.setSpacing(6)
        t = QLabel(title)
        t.setObjectName("stepTitle")
        body.addWidget(t)
        h = QLabel(hint)
        h.setWordWrap(True)
        body.addWidget(h)
        outer.addLayout(body, stretch=1)
        return box, num, body

    @staticmethod
    def _restyle(w: QWidget) -> None:
        w.style().unpolish(w)
        w.style().polish(w)
        for child in w.findChildren(QWidget):
            child.style().unpolish(child)
            child.style().polish(child)

    def _set_active(self, step: QWidget, active: bool) -> None:
        step.setProperty("active", "true" if active else "false")
        for child in step.findChildren(QWidget):
            if child.objectName() != "stepNum":
                child.setEnabled(active)
        self._restyle(step)

    def _mark_done(self, num: QLabel) -> None:
        num.setText("✓")
        num.setProperty("done", "true")
        self._restyle(num)

    # ----------------------------------------------------------- actions
    def _open_console(self) -> None:
        QDesktopServices.openUrl(QUrl(KEYS_URL))
        self._mark_done(self._num1)
        self._key_edit.setFocus()

    def _on_save_key(self) -> None:
        key = self._key_edit.text().strip()
        if not key:
            self._show_key_status(False, "Paste your key first.")
            return
        self._save_btn.setEnabled(False)
        self._key_edit.setEnabled(False)
        self._show_key_status(True, "Checking key...")
        threading.Thread(target=self._run_check, args=(key,), daemon=True,
                         name="key-check").start()

    def _run_check(self, key: str) -> None:
        ok, msg = self._check_key(key)
        self._bridge.done.emit(ok, msg, key)

    def _on_check_done(self, ok: bool, msg: str, key: str) -> None:
        if ok:
            try:
                self._save_key(key)
            except Exception as e:
                log.error("Saving API key failed: %s", e)
                ok, msg = False, f"Couldn't save the key: {e}"
        if ok and self._on_key_saved:
            try:
                self._on_key_saved()
            except Exception as e:
                log.error("Post-key-save hook failed: %s", e)
        self._show_key_status(ok, msg)
        if not ok:
            self._save_btn.setEnabled(True)
            self._key_edit.setEnabled(True)
            self._key_edit.setFocus()
            return
        self._mark_done(self._num1)
        self._mark_done(self._num2)
        self._set_active(self._step3, True)
        self._done_btn.setEnabled(True)
        self._skip_btn.hide()
        self._try_box.setFocus()

    def _show_key_status(self, ok: bool, msg: str) -> None:
        self._key_status.setObjectName("ok" if ok else "error")
        self._key_status.setText(msg)
        self._restyle(self._key_status)

    def _on_try_text(self) -> None:
        if self._try_box.toPlainText().strip() and not self._try_status.text():
            self._try_status.setText("It works! You can now dictate into any app.")
            self._mark_done(self._num3)


def _default_save_key(key: str) -> None:
    from src.config import set_api_key
    set_api_key(key)
