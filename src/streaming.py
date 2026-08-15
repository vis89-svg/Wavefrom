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

from src.merge import (PUNCT, _norm, apply_disputes, diff_plan, ensure_period,
                       find_disputed_blocks, merge_segments, union_text)

log = logging.getLogger(__name__)

SLICE_SECS = 2.5
OVERLAP_SECS = 0.4
SILENCE_PERIOD_SECS = 1.2
MAX_SILENCE_SECS = 3.0
MAX_RECORD_SECS = 120.0
MAX_PROMPT_CHARS = 120
FINAL_CHUNK_SECS = 30.0
FINAL_CHUNK_OVERLAP_SECS = 2.0
_LOW_CONF_LOGPROB = -0.5  # avg_logprob below this marks a decode as uncertain


def _low_conf_words(result) -> set[str]:
    """Normalized words from segments with an uncertain decode (low logprob).

    These are the regions most likely to contain substitutions or
    hallucinations; the reconciliation prompt uses the set to tell the LLM
    which candidate the audio itself thought was shaky.
    """
    low: set[str] = set()
    for seg in getattr(result, "segments", []):
        if seg.avg_logprob is not None and seg.avg_logprob < _LOW_CONF_LOGPROB:
            low.update(_norm(w) for w in seg.text.split())
    return low


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
        self._last_disputes: list = []
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
        prompt = self._context_prompt()
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
                full_text, primary_low = self._transcribe_full_audio(wav_bytes)
                if full_text and full_text.strip():
                    if self._verify_enabled():
                        full_text = self._verify_and_reconcile(
                            full_text, primary_low, wav_bytes)
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
    def _context_prompt(self, tail: int = 15) -> str | None:
        """Whisper context prompt = domain hint + glossary + last committed words.

        Whisper uses this to bias recognition toward the right words (e.g.
        "login system" instead of "logging system", or a custom name like
        "Razorpay" instead of a mis-hearing). Kept short — a long prompt can be
        echoed back verbatim, so the user-specific bias (hint + glossary) wins
        over the committed-words tail when the prompt gets too long.
        """
        parts: list[str] = []
        hint = getattr(self._config, "domain_hint", "") or ""
        if hint.strip():
            parts.append(hint.strip())
        seen: set[str] = set()
        terms: list[str] = []
        for t in (getattr(self._config, "glossary", None) or []):
            term = str(t).strip()
            if term and term.lower() not in seen:
                seen.add(term.lower())
                terms.append(term)
        if terms:
            parts.append(" ".join(terms))
        if self._committed:
            parts.append(" ".join(self._committed[-tail:]))
        prompt = " ".join(parts)
        if len(prompt) > MAX_PROMPT_CHARS:
            # keep the hint + glossary bias, drop the committed-words tail;
            # if the glossary alone is still too long, trim whole terms.
            head = " ".join(parts[:-1]) if len(parts) > 1 else parts[0]
            if len(head) > MAX_PROMPT_CHARS:
                cut = head[:MAX_PROMPT_CHARS]
                idx = cut.rfind(",")
                if idx > 0:
                    cut = cut[:idx]
                prompt = cut.strip()
            else:
                prompt = head
        return prompt or None

    def _transcribe_full_audio(self, wav_bytes: bytes, model: str | None = None,
                               temperature: float | None = None
                               ) -> tuple[str, set[str]]:
        """Chunked transcription of the whole recording.

        The full utterance is split into overlapping FINAL_CHUNK_SECS windows
        and each is transcribed separately; the overlap-diff merge then stitches
        them into one transcript. Long single-pass Whisper calls are the classic
        cause of dropped sentences, so we never rely on one call over long audio.

        Returns (text, low_conf_words). `model`/`temperature` override the
        decode (used by the verify pass). `low_conf_words` holds normalized
        words from segments whose average log probability suggests an uncertain
        decode — useful for adjudicating disagreements.
        """
        pcm, rate = pcm_from_wav(wav_bytes)
        if len(pcm) == 0:
            return "", set()
        chunk_len = int(rate * FINAL_CHUNK_SECS)
        overlap = int(rate * FINAL_CHUNK_OVERLAP_SECS)
        min_len = int(rate * 1.0)
        committed: list[str] = []
        low_conf: set[str] = set()
        i = 0
        while i < len(pcm):
            window = pcm[i:i + chunk_len]
            if len(window) >= min_len:
                kwargs: dict = {
                    "language": self._config.language,
                    "prompt": self._context_prompt(tail=24),
                }
                if not self._local:
                    kwargs["verbose"] = True
                    if model is not None:
                        kwargs["model"] = model
                    if temperature is not None:
                        kwargs["temperature"] = temperature
                result = self._transcriber.transcribe_bytes(_to_wav(window, rate),
                                                             **kwargs)
                if isinstance(result, str):
                    text = result
                else:  # TranscriptResult with confidence metadata
                    text = result.text
                    low_conf.update(_low_conf_words(result))
                committed, _ = merge_segments(committed, text, max_overlap=24)
            i += chunk_len - overlap
        return " ".join(committed), low_conf

    # -------------------------------------------------- verify / reconciliation
    def _verify_enabled(self) -> bool:
        """Verify pass needs a second decode + an LLM to adjudicate disputes."""
        return (bool(getattr(self._config, "verify", True))
                and not self._local
                and self._cleaner is not None)

    def _verify_model(self) -> str:
        verify_model = getattr(self._config, "verify_model", None)
        if verify_model:
            return verify_model
        primary = getattr(self._config, "whisper_model", "") or ""
        if primary == "whisper-large-v3-turbo":
            return "whisper-large-v3"
        return "whisper-large-v3-turbo"

    def _verify_and_reconcile(self, full_text: str, primary_low: set[str],
                              wav_bytes: bytes) -> str:
        """Cross-check the primary full pass against a second model decode.

        Where the two transcripts substitute different wording for the same
        audio, the cleanup LLM picks the wording that fits the context; the
        chosen candidate is spliced into the primary text. Any failure keeps
        the primary text (never a fabricated third option).
        """
        try:
            verify_text, verify_low = self._transcribe_full_audio(
                wav_bytes, model=self._verify_model(), temperature=0.2)
        except Exception as e:
            log.warning("Verify pass failed, using primary: %s", e)
            self._last_disputes = []
            return full_text
        if not verify_text.strip():
            self._last_disputes = []
            return full_text

        disputes = find_disputed_blocks(full_text, verify_text,
                                        primary_low=primary_low,
                                        verify_low=verify_low)
        self._last_disputes = disputes
        if not disputes:
            return full_text
        log.info("Verify pass found %d disputed block(s)", len(disputes))
        try:
            choices = self._cleaner.reconcile(disputes)
        except Exception as e:
            log.warning("Reconcile failed, keeping primary: %s", e)
            choices = {}
        reconciled = apply_disputes(full_text, disputes, choices)
        if reconciled != full_text:
            log.info("Reconciled %d dispute(s) into final transcript",
                     sum(1 for _ in disputes))
        return reconciled

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