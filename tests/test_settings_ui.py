"""Tests for the Mac-style settings page, toggle switch and line icons."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.config import Settings
from src.ui.icons import ICON_NAMES, ToggleSwitch, line_pixmap
from src.ui.settings_page import SettingsPage

_app = QApplication.instance() or QApplication([])


def test_toggle_switch_behaves_like_a_checkbox():
    t = ToggleSwitch(True)
    assert t.isChecked()
    t.click()
    assert not t.isChecked()


def test_line_icons_draw_something():
    for name in ICON_NAMES:
        img = line_pixmap(name, 18, "#ffffff").toImage()
        painted = sum(1 for x in range(18) for y in range(18)
                      if img.pixelColor(x, y).alpha() > 0)
        assert painted > 10, name


def test_settings_page_reflects_settings():
    s = Settings(hotkey="ctrl+win", autostart=True, overlay=False, app_tone=True,
                 glossary=["Groq", "Wispr Flow"])
    page = SettingsPage(s)
    assert page._hotkey_edit.text() == "ctrl+win"
    assert page._autostart_chk.isChecked() and not page._overlay_chk.isChecked()
    assert page._glossary_edit.text() == "Groq, Wispr Flow"
    assert page._cleanup_combo.currentText() == "correcting"


def test_bundled_inter_font_loads():
    from src.ui.theme import load_fonts
    assert load_fonts() == "Inter"
