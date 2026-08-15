"""Groq transcription client with rate-limit awareness."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from groq import Groq

log = logging.getLogger(__name__)


@dataclass
class RateLimitInfo:
    remaining_requests: int | None
    remaining_tokens: int | None
    retry_after: float | None


@dataclass
class Segment:
    """One verbose-json segment with confidence metadata."""
    text: str
    start: float
    end: float
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


@dataclass
class TranscriptResult:
    """Text plus optional per-segment confidence metadata."""
    text: str
    segments: list[Segment] = field(default_factory=list)


class RateLimitError(RuntimeError):
    pass


class TranscriptionClient:
    def __init__(self, api_key: str, model: str = "whisper-large-v3-turbo"):
        self._client = Groq(api_key=api_key)
        self._model = model
        self.last_rate_limit: RateLimitInfo | None = None

    def transcribe_file(self, wav_path: str | Path, language: str | None = None) -> str:
        """Transcribe a local WAV file. Returns the raw transcript text."""
        path = Path(wav_path)
        if not path.is_file():
            raise FileNotFoundError(f"No such file: {path}")

        with path.open("rb") as f:
            kwargs: dict = {
                "model": self._model,
                "file": (path.name, f),
                "response_format": "text",
            }
            if language:
                kwargs["language"] = language
            response = self._client.audio.transcriptions.create(**kwargs)

        self.last_rate_limit = self._read_headers()
        return response.text if hasattr(response, "text") else str(response)

    def transcribe_bytes(self, audio_bytes: bytes, filename: str = "audio.wav",
                         language: str | None = None, prompt: str | None = None,
                         model: str | None = None,
                         temperature: float | None = None,
                         verbose: bool = False) -> str | TranscriptResult:
        """Transcribe raw audio bytes (e.g. a chunk from the microphone).

        Returns the raw transcript text, or a TranscriptResult with per-segment
        confidence metadata when `verbose` is set. `model` overrides the client
        default (used by the verify pass); `temperature` defaults to 0.
        """
        kwargs: dict = {
            "model": model or self._model,
            "file": (filename, audio_bytes),
            "response_format": "verbose_json" if verbose else "text",
        }
        if language:
            kwargs["language"] = language
        if prompt:
            kwargs["prompt"] = prompt
        if temperature is not None:
            kwargs["temperature"] = temperature
        response = self._client.audio.transcriptions.create(**kwargs)
        self.last_rate_limit = self._read_headers()

        if not verbose:
            return response.text if hasattr(response, "text") else str(response)

        segments: list[Segment] = []
        for seg in getattr(response, "segments", []) or []:
            segments.append(Segment(
                text=getattr(seg, "text", ""),
                start=float(getattr(seg, "start", 0.0)),
                end=float(getattr(seg, "end", 0.0)),
                avg_logprob=getattr(seg, "avg_logprob", None),
                no_speech_prob=getattr(seg, "no_speech_prob", None),
            ))
        text = response.text if hasattr(response, "text") else str(response)
        return TranscriptResult(text=text, segments=segments)

    def _read_headers(self) -> RateLimitInfo:
        try:
            headers = self._client.last_response.headers
        except Exception:
            return RateLimitInfo(None, None, None)
        return RateLimitInfo(
            remaining_requests=_int_or_none(headers.get("x-ratelimit-remaining-requests")),
            remaining_tokens=_int_or_none(headers.get("x-ratelimit-remaining-tokens")),
            retry_after=_float_or_none(headers.get("retry-after")),
        )

    @staticmethod
    def wait_for_retry(retry_after: float | None) -> None:
        delay = max(float(retry_after or 2.0), 1.0)
        log.info("Rate limited; sleeping %.1fs", delay)
        time.sleep(delay)


def _int_or_none(v: str | None) -> int | None:
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


def _float_or_none(v: str | None) -> float | None:
    try:
        return float(v) if v else None
    except (TypeError, ValueError):
        return None