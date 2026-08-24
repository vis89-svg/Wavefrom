"""Local single-user lock screen authentication.

Stores credentials in `credentials.json` next to `settings.json` (same
`BASE_DIR`).  Password is salted+hashed with PBKDF2-HMAC-SHA256 from the
stdlib -- no new dependency.

First run (no credentials.json) -> ``create_account()``.
Subsequent runs -> ``verify_password()``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from src.config import BASE_DIR

log = logging.getLogger(__name__)

_CREDENTIALS_FILE = BASE_DIR / "credentials.json"


def _hash_password(password: str, salt: bytes) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations=300_000)
    return dk.hex()


def _load_credentials() -> dict | None:
    if not _CREDENTIALS_FILE.exists():
        return None
    try:
        return json.loads(_CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Failed to read credentials file: %s", e)
        return None


def _save_credentials(name: str, password: str) -> None:
    salt = os.urandom(16)
    digest = _hash_password(password, salt)
    data = {
        "name": name,
        "salt": salt.hex(),
        "hash": digest,
    }
    _CREDENTIALS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info("Credentials saved for user: %s", name)


def has_account() -> bool:
    """True if a local account already exists."""
    return _CREDENTIALS_FILE.exists() and _load_credentials() is not None


def create_account(name: str, password: str) -> bool:
    """Create the local account. Returns True on success."""
    if has_account():
        log.warning("Account already exists; cannot create again.")
        return False
    if not name.strip():
        return False
    if len(password) < 4:
        return False
    _save_credentials(name.strip(), password)
    return True


def verify_password(password: str) -> tuple[bool, str]:
    """Verify a password. Returns (success, display_name)."""
    creds = _load_credentials()
    if creds is None:
        return False, ""
    salt = bytes.fromhex(creds["salt"])
    if _hash_password(password, salt) == creds["hash"]:
        return True, creds.get("name", "")
    return False, ""


def delete_account() -> None:
    """Remove the local credentials file."""
    if _CREDENTIALS_FILE.exists():
        _CREDENTIALS_FILE.unlink()
        log.info("Credentials file deleted.")
