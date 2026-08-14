"""Microphone capture with speech-boundary trimming.

Records at 16kHz mono while a hotkey is held, trims leading/trailing silence,
and returns a WAV payload ready for the transcription cloud call.
"""
from __future__ import annotations

import io
import wave

import numpy as np
import sounddevice as sd

from src.config import Config

BUFFER_MS = 30
SILENCE_DB_THRESHOLD = -35.0
MAX_RECORD_SECS = 60.0


class MicRecorder:
    def __init__(self, config: Config):
        self._sample_rate = config.sample_rate
        self._chunk = int(self._sample_rate * BUFFER_MS / 1000)

    def record_until_keyup(self, should_stop) -> bytes:
        """Record until `should_stop()` returns True. Returns a WAV byte payload.

        Speech must start within 3s; recording stops after 1.5s of trailing
        silence beyond ~1s, protecting against endless capture.
        """
        frames: list[np.ndarray] = []
        voiced_budget = int(self._sample_rate * 1.0)   # silence allowed before trim
        tail_silence = int(self._sample_rate * 1.5)    # silence that ends the recording

        with sd.InputStream(samplerate=self._sample_rate, channels=1,
                            dtype="int16", blocksize=self._chunk) as stream:
            while True:
                data, _ = stream.read(self._chunk)
                frames.append(data.copy())

                if should_stop():
                    break
                if len(frames) * self._chunk >= int(self._sample_rate * MAX_RECORD_SECS):
                    break

                silent = _is_silent(data, self._sample_rate)
                if silent:
                    if len(frames) * self._chunk > voiced_budget:
                        tail_silence -= self._chunk
                        if tail_silence <= 0 and _has_speech(frames, self._sample_rate):
                            break
                else:
                    tail_silence = int(self._sample_rate * 1.5)

        audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)
        audio = _trim(audio, self._sample_rate)
        if len(audio) < self._sample_rate // 4:
            return b""
        return _to_wav(audio, self._sample_rate)


def _is_silent(data: np.ndarray, sample_rate: int) -> bool:
    if len(data) == 0:
        return True
    rms = np.sqrt(np.mean(np.square(data.astype(np.float32) / 32768.0)))
    db = 20.0 * np.log10(max(rms, 1e-8))
    return db < SILENCE_DB_THRESHOLD


def _has_speech(frames: list[np.ndarray], sample_rate: int) -> bool:
    joined = np.concatenate(frames)
    win = int(sample_rate * 0.25)
    for i in range(0, len(joined) - win, win):
        if not _is_silent(joined[i : i + win], sample_rate):
            return True
    return False


def _trim(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    if len(audio) == 0:
        return audio
    win = int(sample_rate * 0.03)
    start = 0
    for i in range(0, len(audio), win):
        if not _is_silent(audio[i : i + win], sample_rate):
            start = i
            break
    else:
        return np.zeros(0, dtype=np.int16)
    end = len(audio)
    for i in range(len(audio) - win, 0, -win):
        if not _is_silent(audio[i : i + win], sample_rate):
            end = min(len(audio), i + win)
            break
    return audio[start:end]


def _to_wav(audio: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(audio.tobytes())
    return buf.getvalue()