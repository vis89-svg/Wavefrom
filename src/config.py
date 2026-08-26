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
DEFAULT_CLEANUP_MODEL = "openai/gpt-oss-20b"
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
    hotkey: str = "ctrl+win"
    mode: str = "hold"  # hold | tap — legacy default mode for start() when no
                        # explicit mode is passed (e.g. dictate_bytes()/tests);
                        # the live app now always passes an explicit mode.
    live_hotkey: str = "ctrl+alt+d"  # tap-style live-typing hotkey
    whisper_model: str = DEFAULT_WHISPER_MODEL
    cleanup_model: str | None = DEFAULT_CLEANUP_MODEL
    cleanup_mode: str = "correcting"  # correcting | conservative | polish
    polish_model: str = "openai/gpt-oss-120b"  # stronger model for the Polish button
    language: str | None = "en"  # forced decode language (None = auto-detect)
    sample_rate: int = DEFAULT_SAMPLE_RATE
    toasts: bool = True
    tray: bool = True
    autostart: bool = False
    local_engine: bool = False
    local_model: str = "small"
    vad_filter: bool = False  # local engine only; off = keep all spoken content
    domain_hint: str = ""     # optional Whisper context prompt (e.g. "software development")
    glossary: list[str] = field(default_factory=list)  # names/terms to keep verbatim
    correction_map: dict[str, str] = field(default_factory=dict)  # wrong -> correct mappings
    verify: bool = True       # cross-check final pass with a second model
    verify_model: str | None = None  # None = pick the other large Whisper model
    slice_secs: float = 3.0   # capture window; lower = snappier (more API calls)
    overlay: bool = True      # floating live indicator (waveform + text preview)
    app_tone: bool = True     # pass the foreground window title to cleanup
    version: int = 3
    groq_api_key: str = ""

    @property
    def use_cloud(self) -> bool:
        return not self.local_engine


Config = Settings  # backwards compatibility; new code should use Settings


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
    if s.cleanup_mode not in ("correcting", "conservative", "polish"):
        s.cleanup_mode = "correcting"
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
def _parse_api_keys(raw: str) -> list[str]:
    """Parse a comma-separated list of API keys, stripping whitespace."""
    return [k.strip() for k in raw.split(",") if k.strip()]


def get_api_key() -> str:
    """Order: keyring (Windows Credential Locker) -> GROQ_API_KEYS comma list env -> .env -> single GROQ_API_KEY env -> .env."""
    if _HAS_KEYRING:
        try:
            k = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
            if k:
                return k
        except Exception as e:
            log.debug("keyring read failed: %s", e)

    # Check comma-separated list from env var GROQ_API_KEYS
    keys_env = os.getenv("GROQ_API_KEYS", "").strip()
    if keys_env:
        keys = _parse_api_keys(keys_env)
        if keys:
            return keys[0]

    # Fall back to single GROQ_API_KEY env var
    if os.getenv("GROQ_API_KEY", "").strip():
        return os.getenv("GROQ_API_KEY", "").strip()

    load_dotenv(ENV_PATH)
    # Read .env with possible GROQ_API_KEYS or GROQ_API_KEY
    if ENV_PATH.is_file():
        try:
            for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
                k, _, v = line.partition("=")
                kk = k.strip()
                vv = v.strip()
                if kk == "GROQ_API_KEYS":
                    keys = _parse_api_keys(vv)
                    if keys:
                        return keys[0]
                elif kk == "GROQ_API_KEY" and not os.getenv("GROQ_API_KEYS", "").strip():
                    return vv
        except Exception:
            pass
    return ""


def set_api_key(key: str, index: int = 0) -> None:
    key = key.strip()
    if not key:
        raise ValueError("API key cannot be empty")
    if _HAS_KEYRING:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)
        log.info("API key stored in Windows Credential Locker")
    else:
        log.warning("keyring unavailable; persisting key to .env")
        # Read existing keys from .env
        existing_keys: list[str] = []
        if ENV_PATH.is_file():
            try:
                for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
                    k, _, v = line.partition("=")
                    if k.strip() == "GROQ_API_KEYS":
                        existing_keys = _parse_api_keys(v)
            except Exception:
                pass
        # Append new key if not already present, respecting max 5 keys
        if key not in existing_keys:
            existing_keys.append(key)
            if len(existing_keys) > 5:
                existing_keys = existing_keys[-5:]  # keep last 5
            ENV_PATH.write_text(f"GROQ_API_KEYS={','.join(existing_keys)}\n", encoding="utf-8")
            log.info("API key appended to .env (max 5 keys stored)")


def validate(config: Settings, api_key: str) -> list[str]:
    problems: list[str] = []
    if config.use_cloud and not api_key:
        problems.append(
            "Groq API key is missing. Open Settings to add one.")
    return problems


def validate_cfg_keys(cfg: Settings) -> list[str]:
    problems: list[str] = []
    keys_env = os.getenv("GROQ_API_KEYS", "").strip()
    single_key = os.getenv("GROQ_API_KEY", "").strip()
    has_key = bool(keys_env or single_key)
    if not has_key:
        problems.append("GROQ_API_KEY is not set. Use Settings to add one or more keys.")
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
    # CLEANUP_MODE
    cleanup_mode = os.getenv("CLEANUP_MODE", "").strip().lower()
    if cleanup_mode in ("correcting", "conservative", "polish"):
        kwargs["cleanup_mode"] = cleanup_mode
    # POLISH_MODEL
    polish_model = os.getenv("POLISH_MODEL", "").strip()
    if polish_model:
        kwargs["polish_model"] = polish_model
    # LANGUAGE (forced decode language; "none"/empty = auto-detect)
    language_env = os.getenv("LANGUAGE", "").strip()
    if language_env:
        kwargs["language"] = None if language_env.lower() in ("none", "auto") else language_env
    # DOMAIN_HINT
    hint = os.getenv("DOMAIN_HINT", "").strip()
    if hint:
        kwargs["domain_hint"] = hint
    # GLOSSARY (comma-separated names/terms to keep verbatim)
    glossary_env = os.getenv("GLOSSARY", "").strip()
    if glossary_env:
        kwargs["glossary"] = [t.strip() for t in glossary_env.split(",") if t.strip()]
    # VERIFY
    verify_env = os.getenv("VERIFY", "").strip().lower()
    if verify_env in ("0", "false", "no", "off"):
        kwargs["verify"] = False
    elif verify_env in ("1", "true", "yes", "on"):
        kwargs["verify"] = True
    # VERIFY_MODEL
    verify_model = os.getenv("VERIFY_MODEL", "").strip()
    if verify_model:
        kwargs["verify_model"] = None if verify_model.lower() == "none" else verify_model
    return Settings(**kwargs)