"""Tests for the dots <-> box overlay: morph geometry and state flow."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.ui.overlay_qt import (_AUTO_DISMISS_MS, _CARD_TEXT_MAX_H, _DOT_GAP_RATIO, _DOT_PX,
                               _PILL_WIDTH, OverlayWindow, WaveformBars,
                               format_elapsed, morph_shape)

_app = QApplication.instance() or QApplication([])


def test_morph_starts_as_three_spread_dots():
    kind, w, h, offset = morph_shape(0.0, 38, 440, 100)
    assert kind == "dots"
    assert offset == 38 * (1 + _DOT_GAP_RATIO)
    assert w == 38 + 2 * offset and h == 38


def test_morph_ends_as_full_box():
    kind, w, h, radius = morph_shape(1.0, 38, 440, 100)
    assert kind == "box"
    assert (w, h) == (440, 100)


def test_morph_dots_merge_before_box_grows():
    _, _, _, offset = morph_shape(0.3499, 38, 440, 100)
    assert offset < 1.0  # dots nearly stacked at the end of the merge phase
    kind, w, h, _ = morph_shape(0.36, 38, 440, 100)
    assert kind == "box" and w < 60 and h < 60  # box starts from one dot


def test_idle_dots_are_small():
    win = OverlayWindow()
    win.start()
    assert win._dot == _DOT_PX <= 10
    assert win.width() < 50 and win.height() < 20


def test_level_bars_decay_toward_silence():
    bars = WaveformBars()
    bars.push_level(-10.0)
    bars.tick_decay()
    assert bars._levels[-1] < -10.0


def test_pill_stays_fully_rounded():
    _, w, h, radius = morph_shape(1.0, 8, 200, 40)
    assert radius == 20  # capsule ends


def test_format_elapsed():
    assert format_elapsed(0) == "0:00"
    assert format_elapsed(7400) == "0:07"
    assert format_elapsed(65000) == "1:05"


def test_card_shows_full_text_and_scrolls_when_long():
    win = OverlayWindow()
    win.start()
    long_text = " ".join(f"Sentence number {i} of a long dictation." for i in range(40))
    win._apply_state("done", long_text)
    _settle(win)
    card = win._card
    assert card._preview.text() == long_text  # nothing cut off
    assert card._scroll.height() == _CARD_TEXT_MAX_H
    assert card._scroll.verticalScrollBar().maximum() > 0  # scrollable

    win._apply_state("done", "Short one.")
    _settle(win)
    assert card._scroll.height() < 40  # short text keeps the card small


def _settle(win):
    if win._anim is not None:
        win._anim.setCurrentTime(win._anim.duration())
    if win._card_anim is not None:
        win._card_anim.setCurrentTime(win._card_anim.duration())
    _app.processEvents()


def test_dots_open_to_pill_then_card_then_collapse():
    win = OverlayWindow()
    win.start()
    assert win._t == 0.0 and not win._pill.isVisible()

    win._apply_state("recording", "")
    _settle(win)
    assert win._t == 1.0
    assert win._pill.isVisible()
    assert not win._card_on
    assert win.width() == _PILL_WIDTH + 6 and win.height() == 46

    win._apply_state("transcribing", "")
    _settle(win)
    assert win._pill._waveform._shimmer

    win._apply_state("done", "Hello world.")
    _settle(win)
    assert win._card_on and win._card.isVisible()
    assert win._card._preview.text() == "Hello world."
    assert win._dismiss_timer.isActive()
    assert win._dismiss_timer.interval() == _AUTO_DISMISS_MS == 10000
    assert win.height() > 100  # card stacked above the pill

    win._collapse_to_bar()
    _settle(win)
    assert win._t == 0.0
    assert not win._pill.isVisible() and not win._card_on
    assert win.width() < 50  # back to the three small dots


def test_hover_keeps_card_and_leave_restarts_countdown():
    win = OverlayWindow()
    win.start()
    win._apply_state("done", "Hello world.")
    _settle(win)
    win.enterEvent(None)
    assert not win._dismiss_timer.isActive()
    win.leaveEvent(None)
    assert win._dismiss_timer.isActive()


def test_polish_makes_card_sticky():
    win = OverlayWindow()
    win.set_polish_callback(lambda: "Hello, world.")
    win.start()
    win._apply_state("done", "hello world")
    _settle(win)
    win._on_polish()
    assert not win._dismiss_timer.isActive()
    # Let the background polish thread deliver its result before `win` can
    # be garbage-collected (a signal into a deleted window aborts Qt).
    import time
    end = time.time() + 3
    while win._polishing and time.time() < end:
        _app.processEvents()
        time.sleep(0.01)
    assert win._card._preview.text() == "Hello, world."
    win.leaveEvent(None)
    assert not win._dismiss_timer.isActive()  # stays until closed


def test_new_recording_hides_previous_card():
    win = OverlayWindow()
    win.start()
    win._apply_state("done", "first")
    _settle(win)
    win._apply_state("recording", "")
    _settle(win)
    assert not win._card_on


def test_repeated_recording_updates_keep_the_timer():
    # The engine re-sends "recording" after every 3s slice; that must not
    # restart the timer or start a new session.
    win = OverlayWindow()
    win.start()
    win._apply_state("recording", "")
    _settle(win)
    session = win._session_id
    started = win._elapsed.msecsSinceReference()
    win._apply_state("recording", "partial text")
    win._apply_state("recording", "more partial text")
    assert win._session_id == session
    assert win._elapsed.msecsSinceReference() == started
