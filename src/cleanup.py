"""LLM cleanup pass: turn raw Whisper drafts into polished text."""
from __future__ import annotations

from groq import Groq

from src.config import Config

SYSTEM_PROMPT = (
    "You are a dictation editor. Rewrite the user's spoken transcript as clean, "
    "natural, correctly punctuated text. Rules:\n"
    "- Remove filler words (um, uh, like, you know, so) without changing meaning.\n"
    "- Fix grammar, capitalization, and punctuation.\n"
    "- Never invent content that was not said. Preserve names and numbers exactly.\n"
    "- Output only the cleaned text, nothing else.\n"
    "CODE MODE: if the user says 'code mode' first, format the output as a code "
    "comment block appropriate for source code instead of prose."
)

BATCH_LIMIT = 4


class CleanupClient:
    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile"):
        self._client = Groq(api_key=api_key)
        self._model = model

    def clean(self, transcript: str) -> str:
        if not transcript.strip():
            return transcript
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content or transcript