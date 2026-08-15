"""Streaming dictation engine: continuous capture, slice transcription, live merge.

The engine captures mic audio while the hotkey is held, cuts it into fixed
slices (with overlap so Whisper sees stable context), transcribes each slice
on a worker thread, merges it into the committed text via overlap-diff, and
types only the new tail — giving the "text appears as you speak" feel.
"""
from __future__ import annotations

import io
import logging
import queue
import threading
import time
import wave
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

from src.merge import PUNCT, diff_plan, ensure_period, merge_segments, union_text

log = logging.getLogger(__name__)

SLICE_SECS = 2.5
OVERLAP_SECS = 0.4
SILENCE_PERIOD_SECS = 1.2
MAX_SILENCE_SECS = 3.0
MAX_RECORD_SECS = 120.0
FINAL_CHUNK_SECS = 30.0
FINAL_CHUNK_OVERLAP_SECS = 2.0


@dataclass
class EngineStatus:
    state: str = "idle"  # idle | recording | transcribing | cleaning | error
    committed_text: str = ""

    def to_dict(self) -> dict:
        return {"state": self.state, "text": self.committed_text}


class DictationEngine:
    def __init__(self, config, transcriber, cleaner=None, injector=None,
                 notify=None, tray=None, local_engine=None):
        self._config = config
        self._transcriber = transcriber
        self._cleaner = cleaner
        self._injector = injector
        self._notify = notify
        self._tray = tray
        self._local = local_engine

        self._slice_q: queue.Queue[bytes | None] = queue.Queue()
        self._committed: list[str] = []
        self._typed_text = ""
        self._full_audio: bytes | None = None
        self._active = threading.Event()
        self._stop_capture = threading.Event()
        self._worker: threading.Thread | None = None
        self._status = EngineStatus()
        self._lock = threading.Lock()
        self._pending_period = False

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._stop_capture.clear()
        self._active.set()
        self._pending_period = False
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop_capture.set()
        self._active.wait(timeout=10)
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=15)

    @property
    def status(self) -> EngineStatus:
        return self._status

    # ---------------------------------------------------------- capture loop
    def capture(self) -> None:
        """Read mic in a loop, emitting slices of SLICE_SECS + overlap.

        Also retains every frame so the full utterance can be re-transcribed
        on release (chunked, overlap-merged) for an accurate final text.
        In hold mode recording ends only on hotkey release (or MAX_RECORD_SECS);
        in tap mode a long silence auto-stops the capture.
        """
        rate = self._config.sample_rate
        hold_mode = getattr(self._config, "mode", "hold") == "hold"
        chunk = int(rate * 0.03)
        buffer: list[np.ndarray] = []
        all_frames: list[np.ndarray] = []
        n_buffer = 0
        slice_len = int(rate * SLICE_SECS)
        hold_len = int(rate * (SLICE_SECS + OVERLAP_SECS))
        silence_run = 0
        total = 0
        had_speech = False
        max_len = int(rate * MAX_RECORD_SECS)

        with sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                            blocksize=chunk) as stream:
            while not self._stop_capture.is_set() and self._active.is_set():
                data, _ = stream.read(chunk)
                buffer.append(data.copy())
                all_frames.append(data.copy())
                n_buffer += len(data)
                total += len(data)

                silent = _is_silent(data, rate)
                if silent:
                    silence_run += len(data)
                else:
                    silence_run = 0
                    had_speech = True

                if had_speech and silence_run > int(rate * SILENCE_PERIOD_SECS) \
                        and not self._pending_period:
                    self._pending_period = True

                if not hold_mode and had_speech \
                        and silence_run > int(rate * MAX_SILENCE_SECS):
                    self._stop_capture.set()

                if n_buffer >= slice_len:
                    window = np.concatenate(buffer)
                    if len(window) > hold_len:
                        window = window[-hold_len:]
                    self._slice_q.put(_to_wav(window, rate))
                    buffer = []
                    n_buffer = 0

                if total > max_len:
                    self._stop_capture.set()

            if buffer and had_speech:
                self._slice_q.put(_to_wav(np.concatenate(buffer), rate))

        if had_speech and all_frames:
            self._full_audio = _to_wav(np.concatenate(all_frames), rate)
        else:
            self._full_audio = None
        self._slice_q.put(None)
        self._active.clear()

    # ------------------------------------------------------------ worker loop
    def _worker_loop(self) -> None:
        while True:
            try:
                item = self._slice_q.get(timeout=1.0)
            except queue.Empty:
                if not self._active.is_set():
                    break
                continue
            if item is None:
                break
            self._process_slice(item)
        self._finalize(self._full_audio)

    def _process_slice(self, wav_bytes: bytes) -> None:
        self._status.state = "transcribing"
        self._notify_tray()
        prompt = " ".join(self._committed[-15:]) if self._committed else None
        text = None
        for attempt in range(2):
            try:
                text = self._transcriber.transcribe_bytes(
                    wav_bytes, language=self._config.language, prompt=prompt)
                break
            except Exception as e:
                if attempt == 0 and _is_rate_limit(e):
                    delay = _retry_delay(self._transcriber)
                    log.warning("Rate limited; retrying slice in %.1fs", delay)
                    time.sleep(delay)
                    continue
                log.warning("Slice transcription failed: %s", e)
                self._status.state = "error"
                self.notify("Dictation",
                            "Transcription failed. Check rate limits / network.")
                self._notify_tray()
                return
        if text is None:
            return

        if self._local:
            # optional watermark is handled at display layer in M2+; skip here
            pass

        with self._lock:
            # 0.4s of audio overlap is only a few words; cap the overlap search
            # so a coincidental longer match can never swallow real content.
            committed, appended = merge_segments(self._committed, text,
                                                 max_overlap=8)
            self._committed = committed
            if self._pending_period:
                self._committed = ensure_period(self._committed)
                self._pending_period = False
                appended = (appended + " " if appended else "") + "."
            self._typed_text = " ".join(self._committed)
        if self._injector and appended:
            sep = "" if (not self._typed_text or self._typed_text.endswith(" ")
                         or appended.startswith(" ")) else " "
            self._injector.inject_text(sep + appended)
        self._status.committed_text = self._typed_text
        self._status.state = "recording" if self._active.is_set() else "idle"
        self._notify_tray()

    def _finalize(self, wav_bytes: bytes | None) -> None:
        with self._lock:
            raw = " ".join(self._committed)
            typed = self._typed_text

        final = raw
        if wav_bytes:
            self._status.state = "transcribing"
            self._notify_tray()
            try:
                full_text = self._transcribe_full_audio(wav_bytes)
                if full_text and full_text.strip():
                    # The chunked full-audio pass is the primary transcript;
                    # the slice-level result is folded in as secondary so any
                    # sentence one pass missed is recovered by the other.
                    final = union_text(full_text.strip(), raw)
            except Exception as e:
                log.warning("Full-audio transcription failed, using merged slices: %s", e)

        if not final.strip():
            self._status.state = "idle"
            self._notify_tray()
            return
        if final[-1] not in PUNCT:
            final += "."

        if self._cleaner:
            self._status.state = "cleaning"
            self._notify_tray()
            try:
                final = self._cleaner.clean(final)
            except Exception as e:
                log.warning("Cleanup failed, using raw transcript: %s", e)

        if self._injector and wav_bytes:
            to_delete, to_type = diff_plan(typed, final)
            if to_delete:
                self._injector.delete_chars(to_delete)
            if to_type:
                self._injector.inject_text(to_type)

        self._status.state = "idle"
        self._status.committed_text = final
        self._notify_tray()
        log.info("Dictation finalized: %r", final)

    # ------------------------------------------------- full-audio transcription
    def _transcribe_full_audio(self, wav_bytes: bytes) -> str:
        """Chunked transcription of the whole recording.

        The full utterance is split into overlapping FINAL_CHUNK_SECS windows
        and each is transcribed separately; the overlap-diff merge then stitches
        them into one transcript. Long single-pass Whisper calls are the classic
        cause of dropped sentences, so we never rely on one call over long audio.
        """
        pcm, rate = pcm_from_wav(wav_bytes)
        if len(pcm) == 0:
            return ""
        chunk_len = int(rate * FINAL_CHUNK_SECS)
        overlap = int(rate * FINAL_CHUNK_OVERLAP_SECS)
        min_len = int(rate * 1.0)
        committed: list[str] = []
        i = 0
        while i < len(pcm):
            window = pcm[i:i + chunk_len]
            if len(window) >= min_len:
                text = self._transcriber.transcribe_bytes(
                    _to_wav(window, rate), language=self._config.language)
                committed, _ = merge_segments(committed, text, max_overlap=24)
            i += chunk_len - overlap
        return " ".join(committed)

    def dictate_bytes(self, audio_bytes: bytes) -> str:
        """Offline one-shot dictation of a WAV payload (used by eval/tests).

        Feeds the real capture-slicing + worker + chunked-finalize pipeline and
        returns the final committed text.
        """
        pcm, rate = pcm_from_wav(audio_bytes)
        if len(pcm) == 0:
            return ""
        self.start()
        for wav in slice_audio(pcm, rate):
            self._slice_q.put(wav)
        self._full_audio = _to_wav(pcm, rate)
        self._slice_q.put(None)
        self._worker.join(timeout=120)
        return self._status.committed_text

    # ------------------------------------------------------------------ misc
    def _notify_tray(self) -> None:
        if self._tray:
            try:
                self._tray.set_state(self._status.state)
            except Exception:
                pass

    def notify(self, title: str, msg: str) -> None:
        if self._notify:
            try:
                self._notify(title, msg)
            except Exception as e:
                log.warning("Toast failed: %s", e)

    def set_tray(self, tray) -> None:
        self._tray = tray


def slice_audio(pcm: np.ndarray, rate: int) -> list[bytes]:
    """Cut a full PCM buffer into the same overlapping slice windows the live
    capture loop emits: a window every SLICE_SECS of new audio, each window
    holding the last SLICE_SECS + OVERLAP_SECS of audio.
    """
    slice_len = int(rate * SLICE_SECS)
    hold_len = int(rate * (SLICE_SECS + OVERLAP_SECS))
    if len(pcm) == 0:
        return []
    out: list[bytes] = []
    i = 0
    while i < len(pcm):
        end = min(i + slice_len, len(pcm))
        window = pcm[max(0, end - hold_len):end]
        out.append(_to_wav(window, rate))
        if end == len(pcm):
            break
        i = end
    return out


def pcm_from_wav(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    """Decode a WAV payload to (mono int16 PCM, sample_rate)."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        rate = w.getframerate()
        nch = w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if nch > 1:
            pcm = pcm[::nch]
    return pcm, rate


def _is_rate_limit(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    return "ratelimit" in name or "rate limit" in str(exc).lower()


def _retry_delay(transcriber) -> float:
    rl = getattr(transcriber, "last_rate_limit", None)
    if rl and getattr(rl, "retry_after", None):
        return max(float(rl.retry_after), 1.0)
    return 2.0


def _is_silent(data: np.ndarray, rate: int) -> bool:
    if len(data) == 0:
        return True
    rms = np.sqrt(np.mean(np.square(data.astype(np.float32) / 32768.0)))
    db = 20.0 * np.log10(max(rms, 1e-8))
    return db < -35.0


def _to_wav(audio: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(audio.tobytes())
    return buf.getvalue()