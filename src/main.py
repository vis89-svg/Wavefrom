"""VoiceFlow Dictation — production entrypoint.

Commands:
  transcribe <file.wav>   One-shot file transcription (diagnostics)
  dictate                 Run the desktop app (tray + hotkey + settings)
  --version               Print version
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import logging.handlers
import sys
import threading
import time
import traceback
from dataclasses import fields
from pathlib import Path

import keyboard

from src.cleanup import CleanupClient
from src.config import (ENV_PATH, LOGS_DIR, Settings, autostart_enabled, autostart_set,
                        get_api_key, load_settings, validate)
from src.inject import TextInjector
from src.local_engine import LocalWhisperEngine
from src.notify import toast as toast_win
from src.single_instance import acquire as acquire_mutex
from src.streaming import DictationEngine
from src.transcribe import TranscriptionClient
from src.ui.settings_dialog import show_settings_dialog
from src.ui.tray import TrayIcon
from src.version import APP_ID, APP_NAME, VERSION

LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        *([logging.StreamHandler(sys.stdout)] if sys.stdout is not None else []),
        logging.handlers.RotatingFileHandler(
            LOGS_DIR / "dictation.log", maxBytes=1_000_000, backupCount=3
        ),
    ],
)
log = logging.getLogger("main")


def install_crash_hook() -> None:
    def hook(exc_type, exc, tb) -> None:
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        log.critical("Unhandled exception:\n%s", msg)
        try:
            toast_win(f"{APP_NAME} error", f"{exc_type.__name__}: {exc}")
        except Exception:
            pass

    sys.excepthook = hook
    threading.excepthook = lambda args: hook(args.exc_type, args.exc_value, args.exc_traceback)


# ---------------------------------------------------------------- commands
def _cmd_transcribe(args: argparse.Namespace) -> int:
    settings = load_settings()
    api_key = get_api_key()
    problems = validate(settings, api_key)
    if problems:
        log.error(problems[0])
        return 1
    client = TranscriptionClient(api_key, model=settings.whisper_model)
    try:
        text = client.transcribe_file(args.file, language=settings.language)
    except Exception as e:
        log.error("Transcription failed: %s", e)
        return 1
    if args.json:
        print(json.dumps({"text": text}))
    else:
        print(text)
    return 0


def _cmd_dictate(args: argparse.Namespace) -> int:
    settings = load_settings()
    if args.skip_cleanup:
        settings.cleanup_model = None
    if args.local:
        settings.local_engine = True
    if args.no_tray:
        settings.tray = False
    if args.no_toasts:
        settings.toasts = False

    if not acquire_mutex(APP_ID):
        toast_win(APP_NAME, "Already running (check the tray icon).")
        log.warning("Another instance is already running; exiting.")
        return 0

    api_key = get_api_key()
    if validate(settings, api_key) and not args.local:
        log.warning("No API key configured; opening settings...")
        show_settings_dialog(settings, on_save=lambda s: None)
        settings = load_settings()
        api_key = get_api_key()
        if validate(settings, api_key):
            log.error("Setup cancelled — cannot run without an API key.")
            return 1

    return _run_app(settings, api_key, inject=not args.no_inject)


# ------------------------------------------------------------------- app
def _build_pipeline(settings: Settings, api_key: str, inject: bool):
    if settings.local_engine:
        log.info("Using LOCAL Whisper engine (%s) — offline mode", settings.local_model)
        transcriber = LocalWhisperEngine(settings.local_model,
                                         vad_filter=settings.vad_filter)
    else:
        transcriber = TranscriptionClient(api_key, model=settings.whisper_model)

    cleaner = None
    if settings.cleanup_model and not settings.local_engine:
        cleaner = CleanupClient(api_key, model=settings.cleanup_model,
                                mode=settings.cleanup_mode,
                                glossary=settings.glossary,
                                correction_map=settings.correction_map)
    injector = TextInjector() if inject else None
    return transcriber, cleaner, injector


def _normalize_key_name(name: str) -> str:
    """Map the keyboard lib's canonical names to the tokens used in settings.

    The library reports the Windows key as 'windows' (also 'command' on some
    builds, with 'left/right windows' variants), but the hotkey string is
    written as 'win'. Without normalization, hold mode's hotkey match never
    recognizes a Win-based combo. Unnamed events (name can be None) return "".
    """
    n = (name or "").lower()
    if n in ("windows", "command") or n in ("left windows", "right windows"):
        return "win"
    return n


# Virtual key codes for the modifier tokens used in hotkey strings. The
# generic codes (ctrl/alt/shift) reflect either side being held; the Windows
# key has no generic code, so both left and right are checked.
_MOD_VKS = {
    "ctrl": (0x11,),
    "alt": (0x12,),
    "shift": (0x10,),
    "win": (0x5B, 0x5C),
}
_user32 = ctypes.WinDLL("user32", use_last_error=True)


def _physically_down(vks) -> bool:
    """True if any of the given virtual-key codes is physically held."""
    return any(_user32.GetAsyncKeyState(vk) & 0x8000 for vk in vks)


class HotkeyController:
    # Ignore a press that lands within this long after a release: when a
    # dictation release blocks the keyboard hook thread while the final text is
    # being typed, the queued key events burst through afterwards and could
    # otherwise re-trigger a spurious capture.
    RELEASE_DEBOUNCE_SECS = 0.25
    # How often the release watchdog polls the physical combo state. It exists
    # so a release is detected even if the keyboard event stream misses it
    # (wedged hook thread, Start menu consuming the Win key-up, callback
    # exception), guaranteeing the recording stops shortly after the user lets
    # go of the hotkey.
    WATCH_POLL_SECS = 0.1

    def __init__(self, mode: str, hotkey: str, on_press, on_release):
        self._mode = mode
        self._hotkey = hotkey
        self._on_press = on_press
        self._on_release = on_release
        self._pressed = set()
        self._blocked = set()
        self._was_active = False
        self._emitted = False
        self._last_release = 0.0
        self._hook = None
        self._hook_mode: str | None = None
        self._watch_evt = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._register()

    def _parse(self) -> tuple[set[str], str]:
        parts = [p.strip().lower() for p in self._hotkey.split("+")]
        combos = {"ctrl", "alt", "shift", "win"}
        mods = {p for p in parts if p in combos}
        key = parts[-1]
        return mods, key

    def _combo_evt_down(self) -> bool:
        """Is the hotkey combo held per the keyboard event stream?

        Tracks the same keys the hook actually sees (down/up events), so a
        genuine key-up clears it immediately regardless of what GetAsyncKeyState
        thinks. The final token is normally a modifier; a non-modifier final key
        (e.g. ctrl+alt+k) is tracked via the event set since it is a momentary
        tap, not a held modifier.
        """
        mods, key = self._parse()
        if not all(m in self._pressed for m in mods):
            return False
        return key in mods or key in self._pressed

    def _combo_phys_down(self) -> bool:
        """Is every modifier key physically held (GetAsyncKeyState)?

        Physical state can't "stick": the instant the user lets go of a key it
        stops contributing. But it can LAG: because this hook suppresses the Win
        key-down/up, Windows' async key-state table never sees the Win key-up
        and keeps reporting Win as down for a long time. That is exactly why
        press/suppression require BOTH signals while a release only needs
        EITHER of them to clear.
        """
        mods, _ = self._parse()
        return all(_physically_down(_MOD_VKS[m]) for m in mods)

    def _combo_down(self) -> bool:
        """Is the hotkey combo held right now (event AND physical state)?

        The AND means a stuck physical Win can never re-trigger capture or
        swallow the next Ctrl+C / Ctrl+A / Ctrl+X on its own — the event stream
        must also agree the combo is down.
        """
        return self._combo_evt_down() and self._combo_phys_down()

    def _callback(self, evt) -> bool:
        try:
            return self._handle(evt)
        except Exception as e:
            # Never let a single bad event break the hook: log and pass it
            # through so the app and later key events keep working.
            log.warning("Hotkey callback error (event passed through): %s", e)
            return True

    def _handle(self, evt) -> bool:
        name = _normalize_key_name(evt.name)
        if evt.event_type == "down":
            self._pressed.add(name)
        else:
            self._pressed.discard(name)

        active = self._combo_down()
        if self._mode == "hold":
            now = time.monotonic()
            if active:
                if not self._was_active:
                    self._was_active = True
                    if now - self._last_release >= self.RELEASE_DEBOUNCE_SECS:
                        log.info("Hotkey press; watchdog armed")
                        self._on_press()
                        self._emitted = True
                        self._arm_watchdog()
                    else:
                        # The release just finished (queued events bursting
                        # through after the final typing) — ignore this press.
                        self._emitted = False
            else:
                if self._was_active:
                    # Release fires when EITHER the event stream or the physical
                    # state says the combo is up. The physical Win key can stay
                    # stuck "down" for a long time (its suppressed key-up never
                    # reaches the async state table), so the event key-up alone
                    # must be enough to end the recording.
                    if self._emitted:
                        log.info("Hotkey release (event)")
                        self._on_release()
                        self._last_release = now
                    self._emitted = False
                    self._was_active = False
                    # Self-heal: drop any stale/leaked key state now that the
                    # combo is released, so it can never re-activate on the
                    # next plain Ctrl press.
                    self._pressed.clear()
                    self._blocked.clear()
        elif self._mode == "tap":
            if evt.event_type == "down" and active and not self._was_active:
                self._on_press()
        self._was_active = active

        if self._mode != "hold":
            return True

        # Blocking hook: while the hotkey combo is held, suppress every key so
        # the focused app never sees Ctrl+Win (or Ctrl+Win+<key>). Return True
        # (pass-through) for everything else so normal typing is unaffected.
        # Key-down/up pairs are tracked so releasing a suppressed key doesn't
        # leave a stray key-up reaching the app.
        blocked = active
        if blocked and evt.event_type == "down":
            self._blocked.add(name)
        elif evt.event_type == "up" and name in self._blocked:
            self._blocked.discard(name)
            blocked = True
        return not blocked

    def _arm_watchdog(self) -> None:
        """Start (or reuse) the thread that fires the release if the key events
        never do. Only one watchdog runs at a time."""
        if self._watch_thread and self._watch_thread.is_alive():
            return
        self._watch_evt.clear()
        self._watch_thread = threading.Thread(target=self._watchdog_loop,
                                              daemon=True)
        self._watch_thread.start()

    def _watchdog_loop(self) -> None:
        while not self._watch_evt.is_set():
            if not self._emitted:
                # The event path already handled the release (or no press was
                # accepted); nothing left to watch.
                return
            # Release when EITHER the event stream or the physical state
            # clears — same rule as the event path. The physical Win key can
            # stay stuck "down" (its suppressed key-up never reaches the async
            # state table), so the event state alone is sufficient to fire.
            if not self._combo_evt_down() or not self._combo_phys_down():
                log.info("Hotkey release (watchdog)")
                self._on_release()
                self._last_release = time.monotonic()
                self._emitted = False
                self._was_active = False
                self._pressed.clear()
                self._blocked.clear()
                return
            time.sleep(self.WATCH_POLL_SECS)

    def _register(self) -> None:
        if self._hook is not None:
            self._unregister()
        if self._mode == "hold":
            self._hook = keyboard.hook(self._callback, suppress=True)
        else:
            self._hook = keyboard.add_hotkey(self._hotkey, self._on_press, suppress=True)
        self._hook_mode = self._mode

    def _unregister(self) -> None:
        self._watch_evt.set()
        if self._watch_thread and self._watch_thread.is_alive():
            self._watch_thread.join(timeout=1)
        if self._hook is not None:
            try:
                # Tear down using the mode that actually created this hook,
                # not self._mode — set_mode() already updated self._mode to
                # the NEW mode before calling here, so using it would call
                # unhook() on an add_hotkey() handle (or vice versa), which
                # raises KeyError and leaves the old registration active
                # internally in the `keyboard` library, alongside the new one.
                if self._hook_mode == "hold":
                    keyboard.unhook(self._hook)
                else:
                    keyboard.remove_hotkey(self._hook)
            except Exception:
                pass
            self._hook = None
            self._hook_mode = None

    def set_hotkey(self, hotkey: str) -> None:
        self._hotkey = hotkey
        self._register()

    def stop(self) -> None:
        self._unregister()


def _run_app(settings: Settings, api_key: str, inject: bool = True) -> int:
    log.info("Starting pipeline...")
    transcriber, cleaner, injector = _build_pipeline(settings, api_key, inject)
    if transcriber is None:
        return 1
    log.info("Pipeline ready; building engine...")

    overlay = None
    if settings.overlay:
        from src.ui.overlay import OverlayWindow
        overlay = OverlayWindow()
        overlay.start()

    engine = DictationEngine(
        settings, transcriber, cleaner=cleaner, injector=injector,
        notify=toast_win if settings.toasts else None,
        overlay=overlay,
    )
    if overlay:
        overlay.set_polish_callback(engine.polish)
        overlay.set_send_callback(engine.send)
        overlay.set_clipboard_callback(engine.copy_to_clipboard)
    running = {"v": True}
    capture_state = {"on": False}
    tray = None
    current_settings = {"hotkey": settings.hotkey, "live_hotkey": settings.live_hotkey}

    def on_quit() -> None:
        running["v"] = False

    def on_open_settings() -> None:
        s = load_settings()
        show_settings_dialog(s, on_save=on_settings_saved)

    def on_settings_saved(new_settings: Settings) -> None:
        current_settings["hotkey"] = new_settings.hotkey
        current_settings["live_hotkey"] = new_settings.live_hotkey
        hold_hotkey.set_hotkey(new_settings.hotkey)
        live_hotkey.set_hotkey(new_settings.live_hotkey)
        engine.set_cleaner_mode(bool(new_settings.cleanup_model))
        # engine._config IS this `settings` object — without copying every
        # field over, the running engine keeps deciding things (verify,
        # domain_hint, ...) from stale values until the app is restarted, even
        # though the hotkey controllers and settings.json already moved on.
        for f in fields(Settings):
            setattr(settings, f.name, getattr(new_settings, f.name))
        apply_autostart(new_settings)
        log.info("Settings saved: hold=%s live=%s",
                 new_settings.hotkey, new_settings.live_hotkey)

    def apply_autostart(s: Settings) -> None:
        try:
            if s.autostart != autostart_enabled():
                autostart_set(s.autostart)
                log.info("Autostart %s", "enabled" if s.autostart else "disabled")
        except Exception as e:
            log.warning("Autostart update failed: %s", e)

    def on_remap() -> None:
        on_open_settings()

    if settings.tray:
        tray = TrayIcon(on_quit=on_quit, on_remap=on_open_settings,
                        on_settings=on_open_settings)
        tray.start()
    engine.set_tray(tray)
    apply_autostart(settings)
    log.info("Engine + tray ready; registering hotkeys...")

    def _capture_thread() -> None:
        log.info("Recording...")
        try:
            engine.capture()
        except Exception as e:
            log.error("Capture error: %s", e)
            engine.stop()
        finally:
            capture_state["on"] = False

    def on_press_hold() -> None:
        if capture_state["on"]:
            log.debug("Ignoring press while already recording")
            return
        capture_state["on"] = True
        if not engine.start(mode="hold"):
            log.debug("Ignoring press while engine is busy finalizing")
            capture_state["on"] = False
            return
        # Event-driven "combo held" flag (not GetAsyncKeyState, which can stay
        # stuck "down" on the Win key): drives slice-typing deferral for the
        # whole recording.
        engine.set_hold_active(True)
        threading.Thread(target=_capture_thread, daemon=True).start()

    def on_release_hold() -> None:
        # Clear the flag first: the finalize thread reads it to decide whether
        # it can type immediately. The event key-up always arrives, so this
        # clears instantly even if the physical Win key is stuck "down".
        engine.set_hold_active(False)
        engine.stop()

    def on_press_live() -> None:
        if capture_state["on"]:
            log.debug("Ignoring live-hotkey press while already recording")
            return
        capture_state["on"] = True
        if not engine.start(mode="tap"):
            log.debug("Ignoring press while engine is busy finalizing")
            capture_state["on"] = False
            return
        # Tap-style: no modifier is held during recording, so live slice
        # typing is safe immediately; capture() auto-stops on silence.
        threading.Thread(target=_capture_thread, daemon=True).start()

    # Two independent, fixed-purpose hotkeys instead of one hotkey plus a
    # runtime mode toggle — each controller is created once and never has its
    # mode changed, which avoids the class of bug where tearing down one
    # registration type (add_hotkey) and standing up another (hook) at
    # runtime left the old one still active internally.
    hold_hotkey = HotkeyController("hold", current_settings["hotkey"],
                                   on_press_hold, on_release_hold)
    live_hotkey = HotkeyController("tap", current_settings["live_hotkey"],
                                   on_press_live, None)
    log.info("Dictation ready. Hold %s to dictate, or tap %s for live typing.",
             current_settings["hotkey"], current_settings["live_hotkey"])
    if settings.tray:
        toast_win(APP_NAME,
                 f"Ready. Hold {current_settings['hotkey']} or tap "
                 f"{current_settings['live_hotkey']} for live typing.")

    try:
        while running["v"]:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        hold_hotkey.stop()
        live_hotkey.stop()
        engine.stop()
        if tray:
            tray.stop()
        if overlay:
            overlay.stop()
    log.info("Exited.")
    return 0


def main() -> int:
    install_crash_hook()
    parser = argparse.ArgumentParser(prog="dictation",
                                     description=f"{APP_NAME} v{VERSION}")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("transcribe", help="Transcribe a WAV file via Groq")
    p.add_argument("file", type=Path)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_transcribe)

    p = sub.add_parser("dictate", help="Run the dictation app")
    p.add_argument("--no-inject", action="store_true", help="Print instead of typing")
    p.add_argument("--skip-cleanup", action="store_true")
    p.add_argument("--no-tray", action="store_true")
    p.add_argument("--no-toasts", action="store_true")
    p.add_argument("--local", action="store_true", help="Use local faster-whisper")
    p.set_defaults(func=_cmd_dictate)

    args = parser.parse_args()
    if args.cmd is None:
        args.func = _cmd_dictate
        args.no_inject = False
        args.skip_cleanup = False
        args.no_tray = False
        args.no_toasts = False
        args.local = False
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())