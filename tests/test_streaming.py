"""Tests for the streaming engine with mocked transcriber + injector."""
import io
import wave

import numpy as np

from src.config import Config
from src.streaming import (DictationEngine, _is_silent, _to_wav, pcm_from_wav,
                           slice_audio)


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


def test_finalize_full_audio_merges_missed_content():
    # The slice pass never hears "this is streaming"; only the full-audio pass
    # does. The union final text must keep it (no content replacement).
    transcriber = FakeTranscriber([
        "hello world goodbye",
        "hello world this is streaming goodbye",
    ])
    injector = FakeInjector()
    engine = make_engine(transcriber, injector)

    engine.start()
    engine._full_audio = _to_wav(np.zeros(16000, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert "this is streaming" in engine.status.committed_text
    assert "hello world this is streaming goodbye." == engine.status.committed_text
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


def test_slice_retries_once_on_rate_limit(monkeypatch):
    class FlakyTranscriber:
        def __init__(self):
            self.calls = 0

        def transcribe_bytes(self, audio_bytes, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("429 rate limited")
            return "hello world"

    t = FlakyTranscriber()
    engine = make_engine(t, None)
    monkeypatch.setattr("src.streaming.time.sleep", lambda s: None)
    engine.start()
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert t.calls == 2
    assert engine._committed == ["hello", "world"]
    assert engine.status.state == "idle"


def test_chunked_final_union_recovers_dropped_sentence():
    # Slices omit "this is a test"; the full-audio chunk has it. The union of
    # both passes must keep it — a dropped sentence is never lost.
    transcriber = FakeTranscriber([
        "hello world",                              # slice 1
        "world goodbye world",                      # slice 2
        "hello world this is a test goodbye world", # full-audio chunk
    ])
    engine = make_engine(transcriber, None)

    engine.start()
    engine._full_audio = _to_wav(np.zeros(16000, dtype=np.int16), 16000)
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(_to_wav(np.zeros(0, dtype=np.int16), 16000))
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    assert engine.status.committed_text == "hello world this is a test goodbye world."


def test_full_audio_chunked_into_overlapping_windows():
    rate = 16000
    transcriber = FakeTranscriber([
        "hello world",                  # chunk 1 [0s,30s)
        "world this is a test",         # chunk 2 [28s,58s) overlap head "world"
        "test goodbye",                 # chunk 3 [56s,62s)
    ])
    engine = make_engine(transcriber, None)
    engine.start()
    engine._full_audio = _to_wav(np.zeros(int(rate * 62), dtype=np.int16), rate)
    engine._slice_q.put(None)
    engine._worker.join(timeout=5)

    # 62s -> 3 chunks, overlap-diff stitched with no duplication/loss
    assert engine.status.committed_text == "hello world this is a test goodbye."


class _FakeStream:
    def __init__(self, chunk, speak_chunks, silent_chunks):
        self._chunk = chunk
        self._speak = speak_chunks
        self._silent = silent_chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n):
        if self._speak > 0:
            self._speak -= 1
            return (np.sin(np.arange(n) / 8) * 8000).astype(np.int16), None
        if self._silent > 0:
            self._silent -= 1
            return np.zeros(n, dtype=np.int16), None
        raise StopIteration


def test_hold_mode_ignores_silence_autostop(monkeypatch):
    rate = 16000
    chunk = int(rate * 0.03)
    engine = make_engine(FakeTranscriber(["hello world"]))
    monkeypatch.setattr("src.streaming.sd.InputStream",
                        lambda **kw: _FakeStream(chunk, speak_chunks=10,
                                                 silent_chunks=120))
    engine.start()
    try:
        engine.capture()
    except StopIteration:
        pass
    # 120 silent chunks (~3.6s) exceeds MAX_SILENCE_SECS, but hold mode must
    # keep recording (only hotkey release / MAX_RECORD_SECS ends capture).
    assert not engine._stop_capture.is_set()


def test_tap_mode_stops_after_silence(monkeypatch):
    rate = 16000
    chunk = int(rate * 0.03)
    engine = make_engine(FakeTranscriber(["hello world"]))
    engine._config.mode = "tap"
    monkeypatch.setattr("src.streaming.sd.InputStream",
                        lambda **kw: _FakeStream(chunk, speak_chunks=10,
                                                 silent_chunks=120))
    engine.start()
    engine.capture()
    assert engine._stop_capture.is_set()


def test_slice_audio_matches_capture_windows():
    rate = 16000
    pcm = np.zeros(int(rate * 7.0), dtype=np.int16)
    slices = slice_audio(pcm, rate)
    assert len(slices) == 3
    sizes = []
    for s in slices:
        with wave.open(io.BytesIO(s), "rb") as w:
            sizes.append(w.getnframes())
    # first window is SLICE_SECS (no backlog yet); later windows are capped at
    # SLICE_SECS + OVERLAP_SECS of retained audio.
    assert sizes[0] == int(rate * 2.5)
    assert sizes[1] == int(rate * 2.9)
    assert sizes[2] == int(rate * 2.9)


def test_pcm_from_wav_roundtrip():
    rate = 16000
    tone = (np.sin(np.arange(1000) / 8) * 5000).astype(np.int16)
    pcm, r = pcm_from_wav(_to_wav(tone, rate))
    assert r == rate
    assert np.array_equal(pcm, tone)