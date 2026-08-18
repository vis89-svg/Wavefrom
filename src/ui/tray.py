"""System tray icon with status + controls."""
from __future__ import annotations

import logging
import threading

import pystray
from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

STATE_COLORS = {
    "idle": (120, 120, 120),
    "recording": (230, 60, 60),
    "transcribing": (240, 170, 40),
    "cleaning": (90, 160, 220),
    "error": (200, 40, 40),
}


class TrayIcon:
    def __init__(self, on_quit=None, on_toggle_mode=None, on_remap=None, on_settings=None, mode="hold"):
        self._icon = None
        self._state = "idle"
        self._mode = mode
        self._on_quit = on_quit
        self._on_toggle_mode = on_toggle_mode
        self._on_remap = on_remap
        self._on_settings = on_settings

    def _make_image(self) -> Image.Image:
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((6, 6, 58, 58), fill=STATE_COLORS.get(self._state, (120, 120, 120)))
        d.ellipse((22, 22, 42, 42), fill=(255, 255, 255))
        return img

    def _on_menu_quit(self, icon, item):
        if self._on_quit:
            self._on_quit()
        icon.stop()

    def _on_menu_settings(self, icon, item):
        if self._on_settings:
            self._on_settings()

    def _on_menu_toggle(self, icon, item):
        self._mode = "tap" if self._mode == "hold" else "hold"
        if self._on_toggle_mode:
            self._on_toggle_mode(self._mode)
        icon.update_menu()

    def _on_menu_remap(self, icon, item):
        if self._on_remap:
            self._on_remap()

    def start(self) -> None:
        self._icon = pystray.Icon(
            "dictation",
            self._make_image(),
            "Dictation — idle",
            menu=pystray.Menu(
                pystray.MenuItem(lambda item: f"Mode: {self._mode} (click to toggle)",
                                 self._on_menu_toggle, default=False),
                pystray.MenuItem("Remap hotkey", self._on_menu_remap),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Settings", self._on_menu_settings),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._on_menu_quit),
            ),
        )
        self._icon.run_detached()

    def set_state(self, state: str) -> None:
        self._state = state if state in STATE_COLORS else "idle"
        if self._icon:
            try:
                self._icon.icon = self._make_image()
                self._icon.title = f"Dictation — {self._state}"
            except Exception as e:
                log.debug("tray update failed: %s", e)

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        if self._icon:
            try:
                self._icon.update_menu()
            except Exception:
                pass

    def stop(self) -> None:
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass