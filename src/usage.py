"""Per-day Groq API usage counted on this computer (for the dashboard).

Groq's free tier limits each model separately (requests per day, audio
seconds per day for Whisper, tokens per day for chat models). We count what
this app sends, per local calendar day, and keep the "requests remaining"
figure Groq returns in its response headers when it provides one -- that
number also reflects use from other devices sharing the key.

Stored in history.db (table ``api_usage``) so it needs no extra file.
"""
from __future__ import annotations

import io
import logging
import sqlite3
import threading
import wave
from datetime import date

log = logging.getLogger(__name__)

# Groq free-tier daily limits per model, as published at the time of writing.
# Groq can change these; the dashboard labels them as approximate.
FREE_LIMITS: dict[str, dict[str, int]] = {
    "whisper-large-v3": {"requests": 2000, "audio_secs": 28800},
    "whisper-large-v3-turbo": {"requests": 2000, "audio_secs": 28800},
    "openai/gpt-oss-120b": {"requests": 1000, "tokens": 200000},
    "openai/gpt-oss-20b": {"requests": 1000, "tokens": 200000},
}
# Groq bills each transcription request for at least this many seconds.
MIN_BILLED_AUDIO_SECS = 10.0

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    from src.history import _DB_PATH
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS api_usage ("
        "  day TEXT NOT NULL, model TEXT NOT NULL,"
        "  requests INTEGER NOT NULL DEFAULT 0,"
        "  audio_secs REAL NOT NULL DEFAULT 0,"
        "  tokens INTEGER NOT NULL DEFAULT 0,"
        "  remaining INTEGER, req_limit INTEGER,"
        "  PRIMARY KEY (day, model))"
    )
    return conn


def wav_seconds(audio_bytes: bytes) -> float:
    """Duration of a WAV payload (0 if it can't be read)."""
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as w:
            rate = w.getframerate()
            return w.getnframes() / rate if rate else 0.0
    except Exception:
        return 0.0


def record(model: str, audio_secs: float = 0.0, tokens: int = 0,
           remaining: int | None = None, req_limit: int | None = None) -> None:
    """Count one API request. Never raises: usage stats must not break dictation."""
    if not model:
        return
    day = date.today().isoformat()
    try:
        with _lock:
            conn = _conn()
            conn.execute(
                "INSERT INTO api_usage (day, model, requests, audio_secs, tokens, "
                "remaining, req_limit) VALUES (?, ?, 1, ?, ?, ?, ?) "
                "ON CONFLICT(day, model) DO UPDATE SET "
                "requests = requests + 1, audio_secs = audio_secs + excluded.audio_secs, "
                "tokens = tokens + excluded.tokens, "
                "remaining = COALESCE(excluded.remaining, remaining), "
                "req_limit = COALESCE(excluded.req_limit, req_limit)",
                (day, model, float(audio_secs), int(tokens), remaining, req_limit))
            conn.commit()
            conn.close()
    except Exception as e:
        log.debug("Usage record failed: %s", e)


def record_transcription(model: str, audio_bytes: bytes,
                         remaining: int | None = None,
                         req_limit: int | None = None) -> None:
    billed = max(MIN_BILLED_AUDIO_SECS, wav_seconds(audio_bytes))
    record(model, audio_secs=billed, remaining=remaining, req_limit=req_limit)


def record_chat(model: str, response) -> None:
    tokens = 0
    try:
        tokens = int(getattr(getattr(response, "usage", None), "total_tokens", 0) or 0)
    except Exception:
        pass
    record(model, tokens=tokens)


def today() -> list[dict]:
    """Today's usage per model, with that model's known free-tier limits."""
    day = date.today().isoformat()
    try:
        with _lock:
            conn = _conn()
            rows = conn.execute(
                "SELECT model, requests, audio_secs, tokens, remaining, req_limit "
                "FROM api_usage WHERE day = ? ORDER BY requests DESC", (day,)
            ).fetchall()
            conn.close()
    except Exception as e:
        log.debug("Usage read failed: %s", e)
        return []
    out = []
    for model, requests, audio_secs, tokens, remaining, req_limit in rows:
        limits = dict(FREE_LIMITS.get(model, {}))
        if req_limit:
            limits["requests"] = req_limit  # Groq's own figure wins
        out.append({"model": model, "requests": requests, "audio_secs": audio_secs,
                    "tokens": tokens, "remaining": remaining, "limits": limits})
    return out
