"""Local Whisper engine (faster-whisper) — optional offline fallback.

Enabled by setting LOCAL_ENGINE=true in .env. Requires the optional
dependencies: pip install -r requirements-optional.txt
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:
    from faster_whisper import WhisperModel as _WhisperModel
    _HAS_FASTER = True
except ImportError:
    _WhisperModel = None
    _HAS_FASTER = False


class LocalWhisperEngine:
    """Transcribes WAV bytes locally. Slower than Groq but unlimited quota."""

    def __init__(self, model: str = "small", vad_filter: bool = False):
        if not _HAS_FASTER:
            raise RuntimeError(
                "faster-whisper is not installed. Run: "
                "pip install -r requirements-optional.txt (needs the CTranslate2 build)"
            )
        log.info("Loading local Whisper model %r (first load downloads it)...", model)
        self._model = _WhisperModel(model, device="cpu", compute_type="int8")
        self._model_name = model
        # VAD can trim speech between silences; default off so no content is dropped.
        self._vad_filter = vad_filter

    def transcribe_bytes(self, audio_bytes: bytes, filename: str = "audio.wav",
                         language: str | None = None,
                         prompt: str | None = None) -> str:
        import io
        import wave

        with wave.open(io.BytesIO(audio_bytes), "rb") as w:
            rate = w.getframerate()
            import numpy as np
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

        segments, _info = self._model.transcribe(
            pcm.astype("float32") / 32768.0,
            beam_size=5,
            language=language,
            vad_filter=self._vad_filter,
            initial_prompt=prompt,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()