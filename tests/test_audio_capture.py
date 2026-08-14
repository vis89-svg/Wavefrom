"""Unit tests for audio capture internals (no mic / no API needed)."""
import numpy as np
import pytest

from src.audio_capture import _is_silent, _to_wav, _trim
from src import config as config_mod


def test_is_silent_matches_detection():
    rate = 16000
    silence = np.zeros(rate, dtype=np.int16)
    loud = (np.sin(np.arange(rate) / 8) * 12000).astype(np.int16)
    assert _is_silent(silence, rate)
    assert not _is_silent(loud, rate)


def test_trim_removes_edge_silence():
    rate = 16000
    silence = np.zeros(int(rate * 0.5), dtype=np.int16)
    speech = (np.sin(np.arange(int(rate * 0.3)) / 8) * 8000).astype(np.int16)
    audio = np.concatenate([silence, speech, silence])
    trimmed = _trim(audio, rate)
    assert len(trimmed) < len(audio)
    assert len(trimmed) >= len(speech)


def test_to_wav_roundtrip():
    rate = 16000
    tone = (np.sin(np.arange(rate) / 8) * 5000).astype(np.int16)
    payload = _to_wav(tone, rate)
    import wave
    import io

    with wave.open(io.BytesIO(payload), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getframerate() == rate
        assert w.getnframes() == len(tone)