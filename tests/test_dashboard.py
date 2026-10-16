"""Tests for dashboard stats, usage counting, history migration and the page."""
import io
import os
import sqlite3
import wave
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import src.history as history
import src.usage as usage
from src import stats as S
from src.inject import app_display_name

_app = QApplication.instance() or QApplication([])


def _entry(text, days_ago=0, duration=None, app=None, today=None):
    today = today or datetime.now(timezone.utc)
    ts = (today - timedelta(days=days_ago)).isoformat()
    return {"id": 0, "ts": ts, "text": text, "duration": duration, "app": app}


def test_word_count():
    assert S.word_count("Hello, world. It's me!") == 4
    assert S.word_count("") == 0


def test_streak_counts_back_from_today_or_yesterday():
    t = date(2026, 10, 16)
    assert S.streak({t, t - timedelta(1), t - timedelta(2)}, t) == 3
    assert S.streak({t - timedelta(1), t - timedelta(2)}, t) == 2  # not broken yet
    assert S.streak({t - timedelta(3)}, t) == 0
    assert S.streak(set(), t) == 0


def test_compute_stats():
    entries = [
        _entry("one two three four five six", duration=3.0 * 10, app="VS Code"),
        _entry("seven eight", duration=1.0 * 10, app="Chrome"),
        _entry("old words here", days_ago=1),  # pre-dashboard entry: no duration/app
    ]
    st = S.compute(entries)
    assert st.total_words == 11 and st.total_dictations == 3
    assert st.words_today == 8
    assert st.streak_days == 2
    assert st.wpm == 8 / (40.0 / 60)  # only timed entries count toward WPM
    assert st.top_apps == [("VS Code", 6), ("Chrome", 2)]
    assert len(st.words_per_day) == 14 and st.words_per_day[-1][1] == 8
    assert st.time_saved_secs > 0


def test_wpm_needs_some_timed_speech():
    assert S.compute([_entry("hi there")]).wpm is None


def test_formatters():
    assert S.format_duration(42) == "42s"
    assert S.format_duration(125) == "2 min"
    assert S.format_duration(3 * 3600 + 20 * 60) == "3 h 20 min"
    assert S.format_count(1234) == "1,234"
    assert S.format_count(12345) == "12.3k"


def test_app_display_names():
    assert app_display_name(r"C:\Program Files\Microsoft VS Code\Code.exe") == "VS Code"
    assert app_display_name(r"C:\x\chrome.exe") == "Chrome"
    assert app_display_name(r"C:\x\someTool.exe") == "SomeTool"


def _wav(secs):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * int(16000 * secs))
    return buf.getvalue()


def test_usage_counts_requests_audio_and_tokens():
    usage.record_transcription("whisper-large-v3-turbo", _wav(3), remaining=1990, req_limit=2000)
    usage.record_transcription("whisper-large-v3-turbo", _wav(25))

    class Resp:
        class usage:  # noqa: N801
            total_tokens = 850
    usage.record_chat("openai/gpt-oss-120b", Resp())
    rows = {r["model"]: r for r in usage.today()}
    turbo = rows["whisper-large-v3-turbo"]
    assert turbo["requests"] == 2
    assert turbo["audio_secs"] == 10 + 25  # 3s is billed as the 10s minimum
    assert turbo["remaining"] == 1990  # last reported figure kept
    assert turbo["limits"]["requests"] == 2000
    assert rows["openai/gpt-oss-120b"]["tokens"] == 850


def test_history_migrates_old_database_and_records_new_fields():
    path = history._DB_PATH
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE dictations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "ts TEXT NOT NULL, text TEXT NOT NULL)")
    conn.execute("INSERT INTO dictations (ts, text) VALUES ('2026-10-01T00:00:00+00:00', 'old')")
    conn.commit()
    conn.close()
    history.record("new one", duration=2.5, app="Notepad")
    rows = history.all_entries()
    assert rows[0]["text"] == "new one" and rows[0]["duration"] == 2.5
    assert rows[0]["app"] == "Notepad"
    assert rows[1]["text"] == "old" and rows[1]["duration"] is None


def test_dashboard_page_renders_stats():
    from src.ui.dashboard_page import DashboardPage, greeting
    history.record(" ".join(["word"] * 20), duration=10.0, app="VS Code")
    page = DashboardPage(display_name="vishag")
    assert page._t_words._value.text() == "20"
    assert page._t_wpm._value.text() == "120 wpm"
    assert "1 day" in page._t_streak._value.text()
    assert greeting("vishag", datetime(2026, 1, 1, 9)) == "Good morning, vishag"
