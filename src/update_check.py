"""Notify-only update check against GitHub Releases.

No server, no auto-download: just compares the running version against the
repo's latest published release tag and, if newer, shows a toast pointing the
user at the release page. Never blocks startup and never raises -- a failed
check (offline, rate-limited, no releases yet) is silently skipped.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import urllib.request

from src.notify import toast
from src.version import APP_NAME, VERSION

log = logging.getLogger(__name__)

_REPO = "vis89-svg/Wavefrom"
_RELEASES_URL = f"https://api.github.com/repos/{_REPO}/releases/latest"
_TIMEOUT_SECS = 5


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts) or (0,)


def _check_once() -> None:
    try:
        req = urllib.request.Request(
            _RELEASES_URL, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = str(data.get("tag_name", "")).strip()
        html_url = data.get("html_url", "")
        if not tag:
            return
        if _version_tuple(tag) > _version_tuple(VERSION):
            log.info("Update available: %s -> %s (%s)", VERSION, tag, html_url)
            toast(APP_NAME, f"Version {tag} is available -- check GitHub Releases.")
        else:
            log.debug("Up to date (running %s, latest %s)", VERSION, tag)
    except Exception as e:
        log.debug("Update check skipped: %s", e)


def check_for_updates_async() -> None:
    """Fire-and-forget background check; never delays startup."""
    threading.Thread(target=_check_once, daemon=True, name="update-check").start()
