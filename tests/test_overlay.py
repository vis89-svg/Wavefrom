"""Overlay placement tests (pure math, no GUI)."""
from src.ui.overlay import _clamp_pos


def test_panel_prefers_above_cursor():
    px, py = _clamp_pos(960, 500, 324, 235, 0, 0, 1920, 1040)
    assert py == 500 - 235 - 14
    assert px == 960 + 18


def test_panel_falls_below_when_no_room_above():
    px, py = _clamp_pos(960, 50, 324, 235, 0, 0, 1920, 1040)
    assert py == 50 + 26
    assert py > 50


def test_panel_clamped_below_screen_bottom():
    px, py = _clamp_pos(960, 2000, 324, 235, 0, 0, 1920, 1040)
    assert py == 1040 - 235 - 8
    assert py + 235 <= 1040


def test_panel_clamped_right_edge():
    px, py = _clamp_pos(5000, 500, 324, 235, 0, 0, 1920, 1040)
    assert px == 1920 - 324 - 8
    assert px + 324 <= 1920


def test_panel_top_aligned_when_taller_than_screen():
    px, py = _clamp_pos(500, 500, 300, 2000, 0, 0, 1920, 1040)
    assert py == 8


def test_panel_respects_nonzero_work_area_origin():
    # taskbar on the left: work area starts at x=60
    px, py = _clamp_pos(30, 500, 324, 235, 60, 0, 1920, 1040)
    assert px == 60 + 8