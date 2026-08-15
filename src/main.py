"""VoiceFlow Dictation — production entrypoint.

Commands:
  transcribe <file.wav>   One-shot file transcription (diagnostics)
  dictate                 Run the desktop app (tray + hotkey + settings)
  --version               Print version
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
import threading
import time
import traceback
from pathlib import Path

import keyboard

from src.cleanup import CleanupClient
from src.config import (ENV_PATH, LOGS_DIR, Settings, autostart_enabled, autostart_set,
                        get_api_key, load_settings, save_settings, update_settings,
                        validate)
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
                                glossary=settings.glossary)
    injector = TextInjector() if inject else None
    return transcriber, cleaner, injector


class HotkeyController:
    def __init__(self, mode: str, hotkey: str, on_press, on_release, on_esc):
        self._mode = mode
        self._hotkey = hotkey
        self._on_press = on_press
        self._on_release = on_release
        self._on_esc = on_esc
        self._pressed = set()
        self._was_active = False
        self._hook = None
        self._esc_handle = None
        self._register()

    def _parse(self) -> tuple[set[str], str]:
        parts = [p.strip().lower() for p in self._hotkey.split("+")]
        combos = {"ctrl", "alt", "shift", "win"}
        mods = {p for p in parts if p in combos}
        key = parts[-1]
        return mods, key

    def _callback(self, evt) -> None:
        if evt.event_type == "down":
            self._pressed.add(evt.name.lower())
        else:
            self._pressed.discard(evt.name.lower())

        mods, key = self._parse()
        active = mods <= self._pressed and key in self._pressed
        if self._mode == "hold":
            if active and not self._was_active:
                self._on_press()
            elif not active and self._was_active:
                self._on_release()
        elif self._mode == "tap":
            if evt.event_type == "down" and active and not self._was_active:
                self._on_press()
        self._was_active = active

    def _register(self) -> None:
        if self._hook is not None:
            self._unregister()
        if self._mode == "hold":
            self._hook = keyboard.hook(self._callback)
        else:
            self._hook = keyboard.add_hotkey(self._hotkey, self._on_press, suppress=False)
        self._esc_handle = keyboard.add_hotkey("esc", self._on_esc)

    def _unregister(self) -> None:
        if self._hook is not None:
            try:
                if self._mode == "hold":
                    keyboard.unhook(self._hook)
                else:
                    keyboard.remove_hotkey(self._hook)
            except Exception:
                pass
            self._hook = None
        if self._esc_handle is not None:
            try:
                keyboard.remove_hotkey(self._esc_handle)
            except Exception:
                pass
            self._esc_handle = None

    def set_hotkey(self, hotkey: str) -> None:
        self._hotkey = hotkey
        self._register()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._register()

    def stop(self) -> None:
        self._unregister()


def _run_app(settings: Settings, api_key: str, inject: bool = True) -> int:
    log.info("Starting pipeline...")
    transcriber, cleaner, injector = _build_pipeline(settings, api_key, inject)
    if transcriber is None:
        return 1
    log.info("Pipeline ready; building engine...")

    engine = DictationEngine(
        settings, transcriber, cleaner=cleaner, injector=injector,
        notify=toast_win if settings.toasts else None,
    )
    running = {"v": True}
    tray = None
    current_settings = {"hotkey": settings.hotkey, "mode": settings.mode}

    def on_quit() -> None:
        running["v"] = False

    def on_open_settings() -> None:
        s = load_settings()
        show_settings_dialog(s, on_save=on_settings_saved)

    def on_settings_saved(new_settings: Settings) -> None:
        current_settings["hotkey"] = new_settings.hotkey
        current_settings["mode"] = new_settings.mode
        hotkeys.set_hotkey(new_settings.hotkey)
        hotkeys.set_mode(new_settings.mode)
        engine.set_cleaner_mode(bool(new_settings.cleanup_model))
        apply_autostart(new_settings)
        log.info("Settings saved: hotkey=%s mode=%s", new_settings.hotkey, new_settings.mode)

    def on_toggle_mode(new_mode: str) -> None:
        current_settings["mode"] = new_mode
        hotkeys.set_mode(new_mode)
        update_settings(mode=new_mode)
        log.info("Mode switched to %s", new_mode)

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
        tray = TrayIcon(on_quit=on_quit, on_toggle_mode=on_toggle_mode,
                        on_remap=on_open_settings, on_settings=on_open_settings,
                        mode=current_settings["mode"])
        tray.start()
    engine.set_tray(tray)
    apply_autostart(settings)
    log.info("Engine + tray ready; registering hotkeys...")

    def _capture_thread() -> None:
        log.info("Recording...")
        engine.start()
        try:
            engine.capture()
        except Exception as e:
            log.error("Capture error: %s", e)
            engine.stop()

    def on_press() -> None:
        threading.Thread(target=_capture_thread, daemon=True).start()

    def on_release() -> None:
        engine.stop()

    hotkeys = HotkeyController(current_settings["mode"], current_settings["hotkey"],
                               on_press, on_release, lambda: running.update(v=False))
    log.info("Dictation ready. Hotkey: %s (mode: %s). Esc to quit.",
             current_settings["hotkey"], current_settings["mode"])
    if settings.tray:
        toast_win(APP_NAME, f"Ready. Hold {current_settings['hotkey']} to dictate.")

    try:
        while running["v"]:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        hotkeys.stop()
        engine.stop()
        if tray:
            tray.stop()
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