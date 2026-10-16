"""Tests for guarded on-screen swaps: waits, outcomes, notices, overlay state."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import src.inject as inject
from src.config import Config
from src.streaming import _NOTICE_NO_FOCUS, _NOTICE_PASTE_FAILED, DictationEngine

_app = QApplication.instance() or QApplication([])


def test_swap_waits_scale_with_size():
    drain0, restore0 = inject.swap_waits(0, 10)
    assert drain0 == 0 and restore0 >= inject.PASTE_SETTLE_SECS
    drain, restore = inject.swap_waits(1258, 820)
    # A 1,258-char delete must wait far longer than the old fixed 0.15 s.
    assert drain > 1.0 and restore > 1.0
    big_drain, big_restore = inject.swap_waits(100000, 100000)
    assert big_drain == inject.MAX_DRAIN_SECS and big_restore == inject.MAX_RESTORE_SECS


class SwapInjector:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []
        self.copied = []

    def replace(self, n, text):
        self.calls.append((n, text))
        return self.outcome

    def copy_to_clipboard(self, text):
        self.copied.append(text)
        return True


class NoticeOverlay:
    def __init__(self):
        self.notices = []

    def set_notice(self, msg):
        self.notices.append(msg)

    def set_state(self, *a):
        pass


def _engine(outcome):
    inj = SwapInjector(outcome)
    eng = DictationEngine(Config(), transcriber=None, injector=inj, overlay=NoticeOverlay())
    eng._typed_chars = 20
    return eng, inj


def test_swap_ok_updates_bookkeeping():
    eng, inj = _engine(inject.REPLACE_OK)
    ok, on_screen = eng._swap_on_screen("hello um world", "Hello, world.", 12, "ello, world.", "Test")
    assert ok and on_screen == "Hello, world."
    assert inj.calls == [(12, "ello, world.")]
    assert eng._typed_chars == 20 - 12 + len("ello, world.")
    assert inj.copied == [] and eng._overlay.notices == []


def test_swap_no_focus_copies_full_text_and_tells_user():
    eng, inj = _engine(inject.REPLACE_NO_FOCUS)
    ok, on_screen = eng._swap_on_screen("old text", "New text.", 8, "New text.", "Test")
    assert not ok and on_screen == "old text"  # nothing was touched
    assert inj.copied == ["New text."]
    assert eng._overlay.notices == [_NOTICE_NO_FOCUS]


def test_swap_paste_failed_copies_the_missing_part():
    eng, inj = _engine(inject.REPLACE_PASTE_FAILED)
    ok, on_screen = eng._swap_on_screen("Hello um world", "Hello, world.", 9, "o, world.", "Test")
    assert not ok
    assert on_screen == "Hello"  # old tail is gone
    assert inj.copied == ["o, world."]  # Ctrl+V completes it in place
    assert eng._overlay.notices == [_NOTICE_PASTE_FAILED]
