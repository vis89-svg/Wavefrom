"""Tests for the streaming engine with mocked transcriber + injector."""
from types import SimpleNamespace

import numpy as np

from src.config import Config
from src.streaming import DictationEngine, _is_silent, _to_wav


class FakeTranscriber:
    def __init__(self, map_by_slice):
        self._map = map_by_slice
        self.calls = []

    def transcribe_bytes(self, audio_bytes, **kwargs):
        self.calls.append(len(audio_bytes))
        idx = len(self.calls) - 1
        return self._map[min(idx, len(self._map) - 1)]


class FakeInjector:
    def __init__(self):
        self.parts = []
        self.deleted = 0

    def inject_text(self, text):
        self.parts.append(text)

    def delete_chars(self, n):
        self.deleted += n

    @property
    def typed(self):
        out = ""
        for p in self.parts:
            if out and not out.endswith(" ") and not p.startswith(" "):
                out += " "
            out += p
        return out


def make_config():
    return Config(
        groq_api_key="test-key",
        hotkey="ctrl+space",
        sample_rate=16000,
        local_engine=True,
        toasts=False,
        tray=False,
    )


def make_engine(transcriber, injector=None):
    config = make_config()
    engine = DictationEngine(config, transcriber, cleaner=None, injector=injector,
                             notify=None, tray=None, local_engine=None)
    return engine


def test_silence_vs_speech():
    rate = 16000
    assert _is_silent(np.zeros(rate, dtype=np.int16), rate)
    tone = (np.sin(np.arange(rate) / 8) * 8000).astype(np.int16)
    assert not _is_silent(tone, rate)


def test_to_wav_payload():
    rate = 16000
    tone = (np.sin(np.arange(int(rate * 0.1)) / 8) * 5000).astype(np.int16)
    payload = _to_wav(tone, rate)
    assert payload[:4] == b"RIFF"
    import wave
    import io

    with wave.open(io.BytesIO(payload), "rb") as w:
        assert w.getframerate() == rate
        assert w.getnframes() == len(tone)


def test_worker_slices_merge_and_type():
    transcriber = FakeTranscriber([
        "hello world",
        "world this is streaming",
        "this is streaming dictation",
    ])
    injector = FakeInjector()
    engine = make_engine(transcriber, injector)

    engine._full_audio = _to_wav(np.zeros(1600, dtype=np.int16), 16000)
    engine.start()
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)  # finalize
    engine._worker.join(timeout=5)

    assert engine._committed == ["hello", "world", "this", "is", "streaming", "dictation"]
    typed = injector.typed
    assert "hello world this is streaming dictation" in typed
    assert typed.endswith(".")


def test_finalize_replaces_text_with_full_audio():
    transcriber = FakeTranscriber([
        "hello world",
        "goodbye world this is streaming dictation",
    ])
    injector = FakeInjector()
    engine = make_engine(transcriber, injector)

    engine._full_audio = _to_wav(np.zeros(1600, dtype=np.int16), 16000)
    engine.start()
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert engine.status.committed_text == "goodbye world this is streaming dictation."
    assert injector.deleted > 0


def test_finalize_cleanup_replaces_text():
    transcriber = FakeTranscriber(["um hello world"])
    injector = FakeInjector()
    engine = make_engine(transcriber, injector)

    class FakeCleaner:
        def clean(self, raw: str) -> str:
            return "hello world, everyone here."

    engine._cleaner = FakeCleaner()
    engine._full_audio = _to_wav(np.zeros(1600, dtype=np.int16), 16000)
    engine.start()
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert engine.status.committed_text == "hello world, everyone here."
    assert injector.deleted > 0
    assert any(", everyone here." in p for p in injector.parts)


def test_slice_failure_records_error_and_notifies():
    class BrokenTranscriber:
        def transcribe_bytes(self, audio_bytes, **kwargs):
            raise RuntimeError("429 rate limited")

    toasts = []

    engine = make_engine(BrokenTranscriber(), None)
    engine._notify = lambda title, msg: toasts.append((title, msg))
    engine.start()
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert any("Transcription failed" in msg for _t, msg in toasts)
    # with nothing committed, finalize returns to idle without crashing
    assert engine.status.state == "idle"