"""Shared test setup."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_history_db(tmp_path, monkeypatch):
    """Point history/usage storage at a temp file so tests never write to
    the real history.db next to the app."""
    import src.history as history
    monkeypatch.setattr(history, "_DB_PATH", tmp_path / "history.db")
