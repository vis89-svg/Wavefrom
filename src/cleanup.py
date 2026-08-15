"""LLM cleanup pass: turn raw Whisper drafts into polished text.

Two modes:
  - "conservative": grammar/punctuation/filler only; every word is preserved.
  - "correcting" (default): additionally fixes clear mis-transcriptions —
    well-known names, similar-sounding word confusions, hallucinated numbers,
    and plainly-wrong phrases — when the intended meaning is obvious from
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
    "- Never omit or drop any content. Preserve every word that was spoken; "
    "if you are unsure, keep the original words exactly.\n"
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
    "- Fix similar-sounding word confusions that speech recognition commonly "
    "makes, when the surrounding sentence makes the intended word obvious "
    '(e.g. "basic logging system" -> "basic login system", and "business '
    'strategy" -> "business logic" in a software context).\n'
    "- Fix hallucinated or mistranscribed numbers and quantifiers when the "
    "rest of the sentence makes the intended phrase obvious "
    '(e.g. "the four web applications would be gone" -> "the whole application '
    "is basically gone\" when that is clearly what was meant).\n"
    "- When a phrase is plainly wrong but the sentence's meaning is clear, "
    "restore the natural wording that was actually spoken (e.g. \"appear on "
    'the back end" -> "appear in the list").\n'
    "- CRITICAL: Do NOT invent content. Only correct when you are confident of "
    "the intended meaning. If you are unsure, leave the original words "
    "exactly as-is.\n"
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
