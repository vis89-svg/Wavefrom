"""Dashboard statistics computed from the dictation history.

Pure functions over the entries returned by ``src.history`` (dicts with
``ts``, ``text``, ``duration``, ``app``), so they're easy to test.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

TYPING_WPM = 40.0      # average typing speed used for "time saved"
FALLBACK_SPEAK_WPM = 130.0  # assumed speaking speed when a duration is unknown

_WORD_RE = re.compile(r"[\w']+")


def word_count(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def entry_day(entry: dict) -> date | None:
    try:
        return datetime.fromisoformat(entry["ts"]).astimezone().date()
    except (KeyError, TypeError, ValueError):
        return None


def streak(days: set[date], today: date) -> int:
    """Consecutive days with a dictation, ending today (or yesterday, so a
    streak isn't shown as broken before you've dictated today)."""
    cur = today if today in days else today - timedelta(days=1)
    n = 0
    while cur in days:
        n += 1
        cur -= timedelta(days=1)
    return n


@dataclass
class Stats:
    total_words: int = 0
    total_dictations: int = 0
    words_today: int = 0
    streak_days: int = 0
    time_saved_secs: float = 0.0
    wpm: float | None = None          # None until a timed dictation exists
    words_per_day: list[tuple[date, int]] = field(default_factory=list)
    top_apps: list[tuple[str, int]] = field(default_factory=list)  # (app, words)


def compute(entries: list[dict], today: date | None = None, days: int = 14,
            top_n: int = 5) -> Stats:
    today = today or date.today()
    s = Stats(total_dictations=len(entries))
    per_day: Counter[date] = Counter()
    per_app: Counter[str] = Counter()
    timed_words = 0
    timed_secs = 0.0
    for e in entries:
        words = word_count(e.get("text", ""))
        s.total_words += words
        d = entry_day(e)
        if d is not None:
            per_day[d] += words
        duration = e.get("duration")
        if duration and duration > 0:
            timed_words += words
            timed_secs += duration
            speak_secs = duration
        else:
            speak_secs = words / FALLBACK_SPEAK_WPM * 60
        s.time_saved_secs += max(0.0, words / TYPING_WPM * 60 - speak_secs)
        if e.get("app"):
            per_app[e["app"]] += words
    s.words_today = per_day.get(today, 0)
    s.streak_days = streak({d for d, w in per_day.items() if w > 0}, today)
    if timed_secs >= 5:
        s.wpm = timed_words / (timed_secs / 60)
    s.words_per_day = [(today - timedelta(days=i), per_day.get(today - timedelta(days=i), 0))
                       for i in range(days - 1, -1, -1)]
    s.top_apps = per_app.most_common(top_n)
    return s


def format_duration(secs: float) -> str:
    """'45s', '12 min', '3 h 20 min'."""
    secs = max(0, int(round(secs)))
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins} min"
    h, m = divmod(mins, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def format_count(n: int) -> str:
    """1234 -> '1,234'; 12345 -> '12.3k'."""
    if n >= 10000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n:,}"
