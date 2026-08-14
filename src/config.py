"""Configuration: settings.json + Windows Credential Locker + env fallbacks.

Secrets (the Groq API key) go to the Windows Credential Locker via `keyring`,
never plaintext on disk. Non-secret settings live in settings.json next to the
app (or in the repo root when running from source).
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

if getattr(sys, "frozen", False):  # packaged .exe: look next to the binary
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

SETTINGS_FILE = BASE_DIR / "settings.json"
LOGS_DIR = BASE_DIR / "logs"
ENV_PATH = BASE_DIR / ".env"

DEFAULT_WHISPER_MODEL = "whisper-large-v3-turbo"
DEFAULT_CLEANUP_MODEL = "llama-3.3-70b-versatile"
DEFAULT_SAMPLE_RATE = 16000

KEYRING_SERVICE = "VoiceFlowDictation"
KEYRING_USER = "groq_api_key"

try:
    import keyring  # type: ignore

    _HAS_KEYRING = True
except ImportError:
    keyring = None
    _HAS_KEYRING = False

@dataclass
class Settings:
    hotkey: str = "ctrl+space"
    mode: str = "hold"  # hold | tap
    whisper_model: str = DEFAULT_WHISPER_MODEL
    cleanup_model: str | None = DEFAULT_CLEANUP_MODEL
    language: str | None = None
    sample_rate: int = DEFAULT_SAMPLE_RATE
    toasts: bool = True
    tray: bool = True
    autostart: bool = False
    local_engine: bool = False
    local_model: str = "small"
    version: int = 2
    groq_api_key: str = ""

    @property
    def use_cloud(self) -> bool:
        return not self.local_engine

Config = Settings  # backwards compatibility; new code should use Settings


@dataclass
class Settings:
    hotkey: str = "ctrl+space"
    mode: str = "hold"  # hold | tap
    whisper_model: str = DEFAULT_WHISPER_MODEL
    cleanup_model: str | None = DEFAULT_CLEANUP_MODEL
    language: str | None = None
    sample_rate: int = DEFAULT_SAMPLE_RATE
    toasts: bool = True
    tray: bool = True
    autostart: bool = False
    local_engine: bool = False
    local_model: str = "small"
    version: int = 2
    groq_api_key: str = ""

    @property
    def use_cloud(self) -> bool:
        return not self.local_engine


# ---------------------------------------------------------------- settings file
def load_settings() -> Settings:
    data = {}
    if SETTINGS_FILE.is_file():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("settings.json unreadable (%s); using defaults", e)

    valid = {k: v for k, v in data.items() if k in Settings.__dataclass_fields__}
    s = Settings(**{**asdict(Settings()), **valid})
    if s.mode not in ("hold", "tap"):
        s.mode = "hold"
    return s


def save_settings(settings: Settings) -> None:
    SETTINGS_FILE.write_text(
        json.dumps(asdict(settings), indent=2), encoding="utf-8")


def update_settings(**changes) -> Settings:
    s = load_settings()
    for k, v in changes.items():
        if k in Settings.__dataclass_fields__:
            setattr(s, k, v)
    save_settings(s)
    return s


# ------------------------------------------------------------------- api key
def get_api_key() -> str:
    """Order: keyring (Windows Credential Locker) -> GROQ_API_KEY env -> .env."""
    if _HAS_KEYRING:
        try:
            k = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
            if k:
                return k
        except Exception as e:
            log.debug("keyring read failed: %s", e)

    if os.getenv("GROQ_API_KEY", "").strip():
        return os.getenv("GROQ_API_KEY", "").strip()

    load_dotenv(ENV_PATH)
    return os.getenv("GROQ_API_KEY", "").strip()


def set_api_key(key: str) -> None:
    key = key.strip()
    if not key:
        raise ValueError("API key cannot be empty")
    if _HAS_KEYRING:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)
        log.info("API key stored in Windows Credential Locker")
    else:
        log.warning("keyring unavailable; persisting key to .env")
        ENV_PATH.write_text(f"GROQ_API_KEY={key}\n", encoding="utf-8")


def validate(config: Settings, api_key: str) -> list[str]:
    problems: list[str] = []
    if config.use_cloud and not api_key:
        problems.append(
            "Groq API key is missing. Open Settings to add one.")
    return problems


def validate_config(cfg: Settings) -> list[str]:
    problems: list[str] = []
    if cfg.groq_api_key == "":
        problems.append("GROQ_API_KEY is not set.")
    return problems


# ------------------------------------------------------------------ autostart
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_set(enabled: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        if enabled:
            if getattr(sys, "frozen", False):
                cmd = f'"{sys.executable}" dictate'
            else:
                cmd = f'"{sys.executable}" -m src.main dictate'
            winreg.SetValueEx(k, "VoiceFlowDictation", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, "VoiceFlowDictation")
            except FileNotFoundError:
                pass


def autostart_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0,
                            winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, "VoiceFlowDictation")
        return True
    except FileNotFoundError:
        return False


def load_config() -> Settings:
    """Load settings from environment variables and .env file."""
    kwargs = {}
    # GROQ_API_KEY
    key = os.getenv("GROQ_API_KEY", "").strip()
    if key:
        kwargs["groq_api_key"] = key
    # MODE
    mode = os.getenv("MODE", "").strip().lower()
    if mode in ("hold", "tap"):
        kwargs["mode"] = mode
    # LOCAL_ENGINE
    local_env = os.getenv("LOCAL_ENGINE", "").strip().lower()
    if local_env in ("true", "1"):
        kwargs["local_engine"] = True
    # CLEANUP_MODEL
    cleanup = os.getenv("CLEANUP_MODEL", "").strip()
    if cleanup:
        kwargs["cleanup_model"] = None if cleanup.lower() == "none" else cleanup
    return Settings(**kwargs)