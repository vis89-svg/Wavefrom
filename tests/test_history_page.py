"""Tests for the History page: day grouping, local time, card feed."""
import os
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

import src.history as history
from src.ui.history_page import HistoryPage, _Card, day_label, local_dt

_app = QApplication.instance() or QApplication([])


def test_day_labels():
    today = date(2026, 10, 16)
    assert day_label(today, today) == "Today"
    assert day_label(date(2026, 10, 15), today) == "Yesterday"
    assert day_label(date(2026, 10, 13), today) == "Tuesday, 13 October"
    assert day_label(date(2025, 12, 31), today) == "31 December 2025"


def test_local_dt_converts_utc_and_tolerates_junk():
    dt = local_dt("2026-10-16T04:12:40.261094+00:00")
    assert dt is not None and dt.utcoffset() is not None
    assert dt.astimezone(timezone.utc).hour == 4
    assert local_dt("not a date") is None


def _entries():
    now = datetime.now(timezone.utc)
    return [
        {"id": 3, "ts": now.isoformat(), "text": "Newest dictation."},
        {"id": 2, "ts": now.isoformat(), "text": "Second one today."},
        {"id": 1, "ts": (now - timedelta(days=1)).isoformat(), "text": "From yesterday."},
    ]


def test_feed_groups_cards_by_day(monkeypatch):
    monkeypatch.setattr(history, "search", lambda q: _entries())
    page = HistoryPage()
    cards = page._feed.findChildren(_Card)
    assert [c._entry["id"] for c in cards] == [3, 2, 1]
    headers = [w.text() for w in page._feed.findChildren(QLabel)
               if w.text() in ("Today", "Yesterday")]
    assert headers == ["Today", "Yesterday"]


def test_empty_state(monkeypatch):
    monkeypatch.setattr(history, "search", lambda q: [])
    page = HistoryPage()
    texts = [w.text() for w in page._feed.findChildren(QLabel)]
    assert "No dictations yet" in texts


def test_delete_removes_entry(monkeypatch):
    deleted = []
    monkeypatch.setattr(history, "search", lambda q: _entries())
    monkeypatch.setattr(history, "delete", deleted.append)
    page = HistoryPage()
    page._feed.findChildren(_Card)[0]._delete()
    assert deleted == [3]
