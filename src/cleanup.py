"""LLM cleanup pass: turn raw Whisper drafts into polished text.

Two modes:
  - "conservative": grammar/punctuation/filler only; every word is preserved.
  - "correcting" (default): additionally fixes clear mis-transcriptions —
    well-known names, similar-sounding word confusions, hallucinated numbers,
    and plainly-wrong phrases — when the intended meaning is obvious from
    context, with a strict guard against inventing content.

Also provides `reconcile`: when two independent transcriptions of the same
audio disagree on a stretch of text, the LLM picks the wording that fits the
surrounding context. The answer is constrained to one of the two candidates,
so it can never introduce words neither pass heard.
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

RECONCILE_PROMPT = (
    "Two independent speech recognitions of the SAME audio disagreed on some "
    "stretches of text. For each numbered dispute, one of the two candidates "
    "matches what was actually said; the other is a mis-recognition. Using the "
    "surrounding context and the note about which candidate had low audio "
    "confidence, decide the correct wording.\n"
    "Rules:\n"
    "- Choose the candidate that fits the surrounding sentence and the "
    "document as a whole.\n"
    "- When both candidates are grammatical, prefer the more natural, "
    "idiomatic wording for the context (a mis-recognition is usually the "
    "less natural phrase).\n"
    "- Never combine the two candidates and never introduce new words.\n"
    '- Reply with one line per dispute, exactly: "<number>: A" or "<number>: B".\n'
    "- If both seem wrong, still pick the one that best fits; output nothing else.\n"
)

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

    def reconcile(self, disputes: list) -> dict[int, str]:
        """Adjudicate transcription disagreements: dispute index -> "A"|"B".

        Returns an empty dict on any parse failure or API error; callers then
        keep the primary wording for every dispute (never a wrong answer).
        """
        if not disputes:
            return {}
        lines = []
        for d in disputes:
            conf = []
            if getattr(d, "primary_low_conf", False):
                conf.append("A had low audio confidence")
            if getattr(d, "verify_low_conf", False):
                conf.append("B had low audio confidence")
            note = f" ({'; '.join(conf)})" if conf else ""
            lines.append(
                f"{d.index}: A: {d.primary_text!r} | B: {d.verify_text!r}"
                f"{note}\n   Context: ...{d.prefix} ___ {d.suffix}..."
            )
        prompt = "\n".join(lines)
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": RECONCILE_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
            content = response.choices[0].message.content or ""
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Reconcile failed, keeping primary: %s", e)
            return {}

        choices: dict[int, str] = {}
        for line in content.splitlines():
            line = line.strip()
            parts = line.replace(":", " ").split()
            if len(parts) >= 2 and parts[0].isdigit():
                idx = int(parts[0])
                pick = parts[1].upper()
                if pick in ("A", "B"):
                    choices[idx] = pick
        return choices
