"""Tests for the dots <-> box overlay: morph geometry and state flow."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.ui.overlay_qt import (_BOX_WIDTH, _DOT_GAP_RATIO, _DOT_PX,
                               OverlayWindow, WaveformBars, morph_shape)

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


def _settle(win):
    if win._anim is not None:
        win._anim.setCurrentTime(win._anim.duration())
    _app.processEvents()


def test_overlay_opens_to_box_and_collapses_to_dots():
    win = OverlayWindow()
    win.start()
    assert win._t == 0.0 and not win._content.isVisible()

    win._apply_state("recording", "")
    _settle(win)
    assert win._t == 1.0
    assert win._content.isVisible()
    assert win._content._state_lbl.text() == "Recording"
    assert win.width() >= _BOX_WIDTH

    win._apply_state("done", "Hello world.")
    _settle(win)
    assert win._content._review.isVisible()
    assert win._content._txt.toPlainText() == "Hello world."

    win._collapse_to_bar()
    _settle(win)
    assert win._t == 0.0
    assert not win._content.isVisible()
    assert win.width() < 200  # back to the three small dots
