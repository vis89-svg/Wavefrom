"""Tests for the single-box overlay: morph geometry, states, sizes, mic."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

import src.ui.overlay_qt as ov
from src.ui.overlay_qt import (_AUTO_DISMISS_MS, _DOT_GAP_RATIO, _DOT_PX, _RADIUS,
                               _REC_CM, _REC_H_CM, _TEXT_CM, _TEXT_H_CM, _WIDE_CM, _WIDE_H_CM,
                               OverlayWindow,
                               WaveformBars, format_elapsed, morph_shape,
                               px_per_cm)

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _no_saved_prefs(monkeypatch):
    """Never read or write the real 'expanded' preference in tests."""
    saved = []
    monkeypatch.setattr(ov, "load_expanded_pref", lambda: False)
    monkeypatch.setattr(ov, "save_expanded_pref", saved.append)
    return saved


def _settle(win):
    if win._anim is not None:
        win._anim.setCurrentTime(win._anim.duration())
    _app.processEvents()


# ── geometry ─────────────────────────────────────────────────────────────────
def test_morph_starts_as_three_spread_dots():
    kind, w, h, offset = morph_shape(0.0, 8, 130, 34)
    assert kind == "dots"
    assert offset == 8 * (1 + _DOT_GAP_RATIO)
    assert w == 8 + 2 * offset and h == 8


def test_morph_ends_as_rounded_rectangle():
    kind, w, h, radius = morph_shape(1.0, 8, 130, 34)
    assert kind == "box" and (w, h) == (130, 34)
    assert radius == _RADIUS  # a rounded rectangle, not a capsule


def test_morph_dots_merge_before_box_grows():
    _, _, _, offset = morph_shape(0.3499, 8, 130, 34)
    assert offset < 0.5
    kind, w, h, _ = morph_shape(0.36, 8, 130, 34)
    assert kind == "box" and w < 20 and h < 20


def test_px_per_cm_uses_real_screen_size():
    from PySide6.QtCore import QRect, QSizeF

    class FakeScreen:
        def __init__(self, px_w, mm_w):
            self._px, self._mm = px_w, mm_w

        def geometry(self):
            return QRect(0, 0, self._px, 800)

        def physicalSize(self):
            return QSizeF(self._mm, 200)

    # 15.6" laptop at 125%: 1536 logical px across 344 mm
    assert round(px_per_cm(FakeScreen(1536, 344)), 1) == 44.7
    assert round(px_per_cm(FakeScreen(1920, 0)), 1) == 37.8  # unknown size -> 96 dpi


def test_sizes_follow_the_centimetre_spec():
    win = OverlayWindow()
    ppc = win._ppc
    assert win._rec_w == round(_REC_CM * ppc)
    assert win._wide_w == round(_WIDE_CM * ppc)
    assert win._wide_h == round(_WIDE_H_CM * ppc)
    assert win._text_w == round(_TEXT_CM * ppc) and win._text_h == round(_TEXT_H_CM * ppc)
    # Text box is twice the recording box, both ways.
    assert abs(win._text_w - 2 * win._rec_w) <= 1 and abs(win._text_h - 2 * win._rec_h) <= 1
    assert win._rec_h == round(_REC_H_CM * ppc)  # recording box is tall


def test_idle_dots_are_small():
    win = OverlayWindow()
    win.start()
    assert win._dot == _DOT_PX <= 10
    assert win.width() < 120 and win.height() < 32


def test_level_bars_decay_toward_silence():
    bars = WaveformBars()
    bars.push_level(-10.0)
    bars.tick_decay()
    assert bars._levels[-1] < -10.0


def test_format_elapsed():
    assert format_elapsed(0) == "0:00"
    assert format_elapsed(7400) == "0:07"
    assert format_elapsed(65000) == "1:05"


# ── states ───────────────────────────────────────────────────────────────────
def test_one_box_through_record_busy_done_and_back():
    win = OverlayWindow()
    win.start()
    assert win._t == 0.0 and not win._content.isVisible()

    win._apply_state("recording", "")
    _settle(win)
    c = win._content
    assert win._t == 1.0 and c.isVisible()
    assert (win._box_w, win._box_h) == (win._rec_w, win._rec_h)
    assert c._waveform.isVisibleTo(c) and c._timer_lbl.isVisibleTo(c)
    assert not c._body.isVisibleTo(c)

    win._apply_state("transcribing", "")
    _settle(win)
    assert c._spinner.isVisibleTo(c) and c._spinner.spinning
    assert not c._waveform.isVisibleTo(c)
    assert win._box_w == win._rec_w  # same compact box while busy

    win._apply_state("done", "Hello world.")
    _settle(win)
    assert c._body.isVisibleTo(c) and c._text.text() == "Hello world."
    assert (win._box_w, win._box_h) == (win._text_w, win._text_h)
    assert c._strip.height() == win._strip_h  # normal strip: the box is tall enough
    assert c._scroll.height() >= c._text.fontMetrics().lineSpacing()  # a line fits
    assert c._polish_btn.isVisibleTo(c) and c._expand_btn.isVisibleTo(c)
    assert not c._spinner.spinning
    assert win._dismiss_timer.isActive()
    assert win._dismiss_timer.interval() == _AUTO_DISMISS_MS == 10000

    win._collapse_to_bar()
    _settle(win)
    assert win._t == 0.0 and not c.isVisible()
    assert win.width() < 120  # back to the three small dots


def test_long_text_scrolls_inside_the_box():
    win = OverlayWindow()
    win.start()
    long_text = " ".join(f"Sentence number {i} of a long dictation." for i in range(60))
    win._apply_state("done", long_text)
    _settle(win)
    c = win._content
    assert c._text.text() == long_text  # nothing cut off
    assert c._scroll.verticalScrollBar().maximum() > 0


def test_expand_grows_both_ways_and_is_remembered(_no_saved_prefs):
    win = OverlayWindow()
    win.start()
    win._apply_state("done", "Some dictated text that is long enough to wrap a bit.")
    _settle(win)
    win._on_expand()
    _settle(win)
    # Expands both ways: wider and taller.
    assert (win._box_w, win._box_h) == (win._wide_w, win._wide_h)
    assert win._wide_w > win._text_w and win._wide_h > win._text_h
    assert _no_saved_prefs == [True]
    assert win._content._polish_btn.text().strip() == "Polish"  # labels when wide
    win._on_expand()
    _settle(win)
    assert (win._box_w, win._box_h) == (win._text_w, win._text_h)
    assert _no_saved_prefs == [True, False]
    assert win._content._polish_btn.text() == ""  # icon only when compact


def test_hover_keeps_box_and_leave_restarts_countdown():
    win = OverlayWindow()
    win.start()
    win._apply_state("done", "Hello world.")
    _settle(win)
    win.enterEvent(None)
    assert not win._dismiss_timer.isActive()
    win.leaveEvent(None)
    assert win._dismiss_timer.isActive()


def test_polish_makes_box_sticky():
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
    assert win._content._text.text() == "Hello, world."
    assert win._content._status.text() == "Polished"
    win.leaveEvent(None)
    assert not win._dismiss_timer.isActive()  # stays until closed


def test_repeated_recording_updates_keep_the_timer():
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


def test_notice_before_done_is_shown_and_keeps_box():
    win = OverlayWindow()
    win.start()
    win._apply_state("recording", "")
    win._apply_notice("Couldn't type it")
    win._apply_state("done", "Hello world.")
    _settle(win)
    c = win._content
    assert c._status.isVisibleTo(c) and c._status.text() == "Couldn't type it"
    assert win._sticky and not win._dismiss_timer.isActive()


def test_updating_state_shows_spinner_label():
    win = OverlayWindow()
    win.start()
    win._apply_state("recording", "")
    win._apply_state("updating", "")
    _settle(win)
    c = win._content
    assert c._busy_lbl.text() == "Updating…" and c._spinner.isVisibleTo(c)


# ── hover mic ────────────────────────────────────────────────────────────────
def test_mic_drop_pulls_out_then_neck_snaps():
    from src.ui.overlay_qt import _MIC_D, _NECK_SNAP, mic_drop
    dx0, r0, neck0 = mic_drop(0.0, 8)
    assert abs(dx0) < 1e-9 and r0 == 4 and neck0 > 0
    _, _, neck_mid = mic_drop(_NECK_SNAP + 0.01, 8)
    assert neck_mid == 0
    dx1, r1, _ = mic_drop(1.0, 8)
    assert r1 == _MIC_D / 2
    assert dx1 > 8 / 2 + _MIC_D / 2


def test_hover_brings_mic_out_and_click_starts_live_typing(monkeypatch):
    win = OverlayWindow()
    clicked = []
    win.set_mic_callback(lambda: clicked.append(True))
    win.start()
    _settle(win)
    monkeypatch.setattr(ov.QCursor, "pos", staticmethod(lambda: win.geometry().center()))
    win._poll_hover()
    assert win._mic_open
    win._mic_anim.setCurrentTime(win._mic_anim.duration())
    assert win._mic_t == 1.0
    win._on_mic()
    _app.processEvents()
    assert clicked == [True]
    assert win._mic_t == 0.0 and not win._mic_open


def test_cursor_leaving_tucks_mic_back(monkeypatch):
    from PySide6.QtCore import QPoint
    win = OverlayWindow()
    win.start()
    _settle(win)
    monkeypatch.setattr(ov.QCursor, "pos", staticmethod(lambda: win.geometry().center()))
    win._poll_hover()
    monkeypatch.setattr(ov.QCursor, "pos", staticmethod(lambda: QPoint(-5000, -5000)))
    win._poll_hover()
    assert not win._mic_open
    win._mic_anim.setCurrentTime(win._mic_anim.duration())
    assert win._mic_t == 0.0


def test_dictation_start_hides_mic():
    win = OverlayWindow()
    win.start()
    win._mic_t, win._mic_open = 1.0, True
    win._apply_state("recording", "")
    assert win._mic_t == 0.0 and not win._mic_open
    assert not win._hover_timer.isActive()
