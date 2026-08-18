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
    + "- NEVER change proper nouns, place names, personal names, organization "
    "names, or any capitalized words unless they are clearly a common English "
    "word that was misheard (e.g. a common noun, not a name).\n"
    "- NEVER replace Indian place names, Indian names, or any non-English "
    "proper nouns with English-sounding alternatives. Always preserve them "
    "exactly as spoken (e.g. keep Thrissur, Puthoor, Kerala, etc.).\n"
    "- NEVER hallucinate or invent words that were not spoken. If unsure, "
    "keep the original word exactly.\n"
    "- Fix only obvious grammar errors, missing punctuation, and filler word "
    "removal. Do NOT rewrite or rephrase sentences.\n"
    "- CRITICAL: When in doubt, ALWAYS keep the original word unchanged. "
    "It is far better to leave a slightly wrong word than to replace it "
    "with something the speaker never said.\n"
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

_GLOSSARY_RULE = (
    "User-specific names and terms that MUST be spelled exactly as written "
    "below. If the transcript contains a close-but-wrong spelling of one of "
    "these (e.g. a similar-sounding word or a mis-capitalization), fix it to "
    "the exact form listed. Never change these terms otherwise: "
)


def _glossary_line(glossary: list[str]) -> str:
    terms = [str(t).strip() for t in glossary if str(t).strip()]
    if not terms:
        return ""
    return _GLOSSARY_RULE + ", ".join(terms) + ".\n"


class CleanupClient:
    def __init__(self, api_key: str, model: str = "openai/gpt-oss-20b",
                 mode: str = "correcting", glossary: list[str] | None = None):
        self._client = Groq(api_key=api_key)
        self._model = model
        self.mode = mode if mode in ("correcting", "conservative") else "correcting"
        seen: set[str] = set()
        terms: list[str] = []
        for t in (glossary or []):
            term = str(t).strip()
            if term and term.lower() not in seen:
                seen.add(term.lower())
                terms.append(term)
        self.glossary = terms

    @property
    def system_prompt(self) -> str:
        if self.mode == "conservative":
            base = CONSERVATIVE_PROMPT
        else:
            base = CORRECTING_PROMPT
        return base + _glossary_line(self.glossary)

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
                    {"role": "system",
                     "content": RECONCILE_PROMPT + _glossary_line(self.glossary)},
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
