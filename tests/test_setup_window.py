"""Tests for the first-run setup window and runtime client swapping."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.streaming import DictationEngine
from src.config import Config
from src.ui.setup_window import SetupWindow, _pretty_hotkey

_app = QApplication.instance() or QApplication([])


def _wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        _app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_pretty_hotkey():
    assert _pretty_hotkey("ctrl+win") == "Ctrl + Win"


def test_valid_key_is_saved_and_unlocks_try_step():
    saved, hooks = [], []
    win = SetupWindow("ctrl+win", on_key_saved=lambda: hooks.append(1),
                      save_key=saved.append,
                      check_key=lambda k: (True, "Key works and is saved."))
    assert not win._try_box.isEnabled()
    win._key_edit.setText("  gsk_test  ")
    win._on_save_key()
    assert _wait_for(lambda: win._done_btn.isEnabled())
    assert saved == ["gsk_test"]
    assert hooks == [1]
    assert win._try_box.isEnabled()
    win._try_box.setPlainText("Hello, this is my first dictation.")
    assert "It works" in win._try_status.text()


def test_rejected_key_is_not_saved():
    saved = []
    win = SetupWindow("ctrl+win", save_key=saved.append,
                      check_key=lambda k: (False, "Groq rejected this key."))
    win._key_edit.setText("gsk_bad")
    win._on_save_key()
    assert _wait_for(lambda: win._save_btn.isEnabled())
    assert saved == []
    assert "rejected" in win._key_status.text()
    assert not win._done_btn.isEnabled()
    assert not win._try_box.isEnabled()


def test_empty_key_shows_hint_without_checking():
    calls = []
    win = SetupWindow("ctrl+win", check_key=lambda k: calls.append(k) or (True, ""))
    win._on_save_key()
    assert calls == []
    assert "Paste your key" in win._key_status.text()


def test_engine_set_clients_swaps_transcriber_and_cleaner():
    engine = DictationEngine(Config(), transcriber="old-t", cleaner="old-c")
    engine.set_clients("new-t", None)
    assert engine._transcriber == "new-t"
    assert engine._cleaner is None
