"""Login / lock screen (PySide6).

First run (no credentials.json) shows "Create your account" (name + password +
confirm).  Subsequent runs show "Log in" with just the password field.  On
success the window closes and ``run_app`` is called; on cancel/quit the app
exits.
"""
from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.auth import create_account, has_account, verify_password
from src.version import APP_NAME

log = logging.getLogger(__name__)

_WINDOW_STYLE = """
QMainWindow, QWidget#login {
    background-color: #1a1d23;
}
QLabel {
    color: #d7dbe0;
}
QLineEdit {
    background: #20242a; color: #e6e9ee; border: 1px solid #3a4048;
    border-radius: 6px; padding: 8px 12px; font: 13px 'Segoe UI';
}
QLineEdit:focus {
    border: 1px solid #4a90d9;
}
QPushButton {
    background: #4a90d9; color: #fff; border: none; border-radius: 6px;
    padding: 10px 24px; font: bold 13px 'Segoe UI';
}
QPushButton:hover {
    background: #5ba0e0;
}
QPushButton:pressed {
    background: #3a80c9;
}
QPushButton#cancel {
    background: #3a4048;
}
QPushButton#cancel:hover {
    background: #505068;
}
"""


class LoginWindow(QMainWindow):
    """Local lock-screen gate.  Shown before the main app starts."""

    def __init__(self) -> None:
        super().__init__()
        self._authenticated = False
        self._display_name = ""
        self._is_first_run = not has_account()

        self.setWindowTitle(APP_NAME)
        self.setFixedSize(380, 360 if self._is_first_run else 280)
        self.setStyleSheet(_WINDOW_STYLE)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Dialog
        )

        central = QWidget()
        central.setObjectName("login")
        self.setCentralWidget(central)
        lay = QVBoxLayout(central)
        lay.setContentsMargins(36, 30, 36, 24)
        lay.setSpacing(10)

        title = QLabel(APP_NAME)
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)
        lay.addSpacing(6)

        if self._is_first_run:
            subtitle = QLabel("Create your account")
        else:
            subtitle = QLabel("Log in")
        subtitle.setFont(QFont("Segoe UI", 11))
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet("color: #8a8f98;")
        lay.addWidget(subtitle)
        lay.addSpacing(14)

        if self._is_first_run:
            self._name_edit = QLineEdit()
            self._name_edit.setPlaceholderText("Display name")
            lay.addWidget(self._name_edit)
            lay.addSpacing(6)
        else:
            self._name_edit = None

        self._pw_edit = QLineEdit()
        self._pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw_edit.setPlaceholderText("Password")
        self._pw_edit.returnPressed.connect(self._on_submit)
        lay.addWidget(self._pw_edit)
        lay.addSpacing(4)

        if self._is_first_run:
            self._confirm_edit = QLineEdit()
            self._confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._confirm_edit.setPlaceholderText("Confirm password")
            self._confirm_edit.returnPressed.connect(self._on_submit)
            lay.addWidget(self._confirm_edit)
            lay.addSpacing(4)

        self._error_lbl = QLabel("")
        self._error_lbl.setStyleSheet("color: #e5484d; font: 11px 'Segoe UI';")
        self._error_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._error_lbl)
        lay.addSpacing(8)

        self._submit_btn = QPushButton(
            "Create Account" if self._is_first_run else "Log In"
        )
        self._submit_btn.clicked.connect(self._on_submit)
        lay.addWidget(self._submit_btn)

        self._cancel_btn = QPushButton("Quit")
        self._cancel_btn.setObjectName("cancel")
        self._cancel_btn.clicked.connect(self._on_cancel)
        lay.addWidget(self._cancel_btn)

        lay.addStretch()

    def _on_submit(self) -> None:
        self._error_lbl.setText("")
        if self._is_first_run:
            name = (self._name_edit.text() if self._name_edit else "").strip()
            pw = self._pw_edit.text()
            confirm = self._confirm_edit.text()

            if not name:
                self._error_lbl.setText("Name cannot be empty.")
                return
            if len(pw) < 4:
                self._error_lbl.setText("Password must be at least 4 characters.")
                return
            if pw != confirm:
                self._error_lbl.setText("Passwords do not match.")
                return
            if create_account(name, pw):
                self._authenticated = True
                self._display_name = name
                self.close()
            else:
                self._error_lbl.setText("Failed to create account.")
        else:
            pw = self._pw_edit.text()
            ok, name = verify_password(pw)
            if ok:
                self._authenticated = True
                self._display_name = name
                self.close()
            else:
                self._error_lbl.setText("Incorrect password.")

    def _on_cancel(self) -> None:
        self._authenticated = False
        self.close()

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    @property
    def display_name(self) -> str:
        return self._display_name


def show_login() -> tuple[bool, str]:
    """Show the login window. Returns (authenticated, display_name).

    If no account exists, shows "Create your account"; otherwise "Log in".
    """
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    win = LoginWindow()
    win.show()
    app.exec()
    return win.authenticated, win.display_name
