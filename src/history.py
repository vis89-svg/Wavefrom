"""Local dictation history backed by SQLite (stdlib only, no new dependency).

Table ``dictations``:
  id       INTEGER PRIMARY KEY
  ts       TEXT    ISO-8601 UTC timestamp
  text     TEXT    the committed dictation text
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.config import BASE_DIR

log = logging.getLogger(__name__)

_DB_PATH = BASE_DIR / "history.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dictations ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  ts TEXT NOT NULL,"
        "  text TEXT NOT NULL"
        ")"
    )
    conn.commit()
    return conn


def record(text: str) -> None:
    """Insert a finished dictation into the history store."""
    if not text or not text.strip():
        return
    ts = datetime.now(timezone.utc).isoformat()
    try:
        conn = _conn()
        conn.execute("INSERT INTO dictations (ts, text) VALUES (?, ?)", (ts, text.strip()))
        conn.commit()
        conn.close()
        log.debug("History recorded (%d chars)", len(text))
    except Exception as e:
        log.warning("History record failed: %s", e)


def search(query: str, limit: int = 200) -> list[dict]:
    """Return recent dictations matching *query* (case-insensitive substring).

    Each dict has keys: ``id``, ``ts``, ``text``.
    """
    conn = _conn()
    if query.strip():
        rows = conn.execute(
            "SELECT id, ts, text FROM dictations "
            "WHERE text LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{query.strip()}%", limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, ts, text FROM dictations ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [{"id": r[0], "ts": r[1], "text": r[2]} for r in rows]


def delete(entry_id: int) -> None:
    """Delete a single history entry by id."""
    try:
        conn = _conn()
        conn.execute("DELETE FROM dictations WHERE id = ?", (entry_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("History delete failed: %s", e)


def clear() -> None:
    """Delete all history entries."""
    try:
        conn = _conn()
        conn.execute("DELETE FROM dictations")
        conn.commit()
        conn.close()
        log.info("History cleared.")
    except Exception as e:
        log.warning("History clear failed: %s", e)
