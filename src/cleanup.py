"""LLM cleanup pass: turn raw Whisper drafts into polished text.

Two modes:
  - "conservative": grammar/punctuation/filler only; every word is preserved.
  - "correcting" (default): additionally fixes clear mis-transcriptions of
    well-known names, places, and terms when the intent is obvious from
    context, with a strict guard against inventing content.
"""
from __future__ import annotations

from groq import Groq

from src.config import Config

_CORE = (
    "You are a dictation editor. Rewrite the user's spoken transcript as clean, "
    "natural, correctly punctuated text. Rules:\n"
    "- Remove filler words (um, uh, like, you know, so) without changing meaning.\n"
    "- Fix grammar, capitalization, and punctuation.\n"
    "- Never invent content that was not said.\n"
    "- Output only the cleaned text, nothing else.\n"
)

CONSERVATIVE_PROMPT = (
    _CORE
    + "- Preserve every word as spoken, including names and numbers.\n"
)

CORRECTING_PROMPT = (
    _CORE
    + "- Correct words that are clearly mis-transcriptions of real, well-known "
    "names, places, and terms when the intended word is obvious from context "
    '(e.g. "L\'Avram Ipsum" -> "Lorem Ipsum", "Caesarea transition" -> '
    '"Cicero translation"). Only correct when very confident.\n'
    "- CRITICAL: Do NOT invent content. Never change an unusual word into a "
    "plausible-sounding but different word. If you are unsure what was meant, "
    "leave the original words exactly as-is.\n"
)

# Backwards-compatible default prompt for callers that don't pick a mode.
SYSTEM_PROMPT = CORRECTING_PROMPT

BATCH_LIMIT = 4


class CleanupClient:
    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile",
                 mode: str = "correcting"):
        self._client = Groq(api_key=api_key)
        self._model = model
        self.mode = mode if mode in ("correcting", "conservative") else "correcting"

    @property
    def system_prompt(self) -> str:
        if self.mode == "conservative":
            return CONSERVATIVE_PROMPT
        return CORRECTING_PROMPT

    def clean(self, transcript: str) -> str:
        if not transcript.strip():
            return transcript
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": transcript},
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content or transcript
