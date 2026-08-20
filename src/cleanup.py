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

POLISH_PROMPT = (
    _CORE
    + "- Fix grammar: subject-verb agreement, tense, articles, prepositions, "
    "and word order within a sentence.\n"
    "- Fix sentence structure: split run-on sentences, join sentence fragments "
    "into grammatical sentences, and break text into well-formed sentences.\n"
    "- Only reorder words inside a sentence when needed for correct grammar or "
    "natural flow. Do not reorder or rearrange sentences.\n"
    "- Never change the meaning, facts, numbers, dates, or any content that was "
    "spoken.\n"
    "- NEVER change proper nouns, place names, personal names, organization "
    "names, Indian place names, or any capitalized words unless they are "
    "clearly a common English word that was misheard.\n"
    "- Preserve glossary/technical terms and quoted phrases exactly as written.\n"
    "- If the speaker corrected themselves mid-sentence (e.g. 'no, actually', "
    "'wait,', 'I mean', 'make that', 'scratch that'), output ONLY the final "
    "corrected version and drop the superseded part that the speaker rejected.\n"
    "- CRITICAL: When in doubt, ALWAYS keep the original wording unchanged. "
    "It is far better to leave slightly imperfect grammar than to invent or "
    "drop content the speaker never said.\n"
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
    "- If the speaker corrects themselves mid-sentence (e.g. 'no, actually', "
    "'wait,', 'I mean', 'make that', 'scratch that'), output ONLY the final "
    "corrected version and drop the superseded part that the speaker rejected.\n"
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
    "CRITICAL: The following terms are EXACT spellings that MUST appear in your "
    "output. If the transcript contains ANY word that sounds similar or is a "
    "close misspelling of one of these terms, you MUST replace it with the exact "
    "form below. Do NOT leave misspelled versions. Correct forms: "
)

_CORRECTION_MAP_RULE = (
    "The following are KNOWN speech recognition errors that MUST be corrected "
    "to the canonical spelling. If you see the wrong form in the transcript, "
    "replace it with the correct form. Never produce the wrong form: "
)


def _glossary_line(glossary: list[str]) -> str:
    terms = [str(t).strip() for t in glossary if str(t).strip()]
    if not terms:
        return ""
    return _GLOSSARY_RULE + ", ".join(terms) + ".\n"


def _correction_map_line(corrections: dict[str, str]) -> str:
    if not corrections:
        return ""
    pairs = [f"{wrong} -> {right}" for wrong, right in corrections.items()
             if wrong.strip() and right.strip()]
    if not pairs:
        return ""
    return _CORRECTION_MAP_RULE + "; ".join(pairs) + ".\n"


_APP_TONE_RULE = (
    "The user is dictating into the application: \"{title}\". Match the "
    "expected tone and format for that kind of app (e.g. formal for a document "
    "or email editor, conversational for a chat window, plain for a code "
    "editor) WITHOUT changing any of the spoken content.\n"
)


def _app_tone_line(title: str) -> str:
    if not title or not title.strip():
        return ""
    return _APP_TONE_RULE.format(title=title.strip().replace('"', "'"))


class CleanupClient:
    def __init__(self, api_key: str, model: str = "openai/gpt-oss-20b",
                 mode: str = "correcting", glossary: list[str] | None = None,
                 correction_map: dict[str, str] | None = None):
        self._client = Groq(api_key=api_key)
        self._model = model
        self.mode = (mode if mode in ("correcting", "conservative", "polish")
                     else "correcting")
        seen: set[str] = set()
        terms: list[str] = []
        for t in (glossary or []):
            term = str(t).strip()
            if term and term.lower() not in seen:
                seen.add(term.lower())
                terms.append(term)
        self.glossary = terms
        self.correction_map = {k.strip(): v.strip() for k, v in (correction_map or {}).items()
                               if k.strip() and v.strip()}

    @property
    def system_prompt(self) -> str:
        if self.mode == "conservative":
            base = CONSERVATIVE_PROMPT
        elif self.mode == "polish":
            base = POLISH_PROMPT
        else:
            base = CORRECTING_PROMPT
        return base + _glossary_line(self.glossary) + _correction_map_line(self.correction_map)

    def clean(self, transcript: str, app_hint: str | None = None) -> str:
        if not transcript.strip():
            return transcript
        hint = _app_tone_line(app_hint) if app_hint else ""
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": self.system_prompt + hint},
                {"role": "user", "content": transcript},
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content or transcript

    def polish(self, transcript: str, app_hint: str | None = None) -> str:
        """On-demand sentence-structure + grammar polish of an already-cleaned
        transcript. Uses the dedicated POLISH_PROMPT regardless of the mode the
        client was constructed with."""
        if not transcript.strip():
            return transcript
        hint = _app_tone_line(app_hint) if app_hint else ""
        prompt = POLISH_PROMPT + _glossary_line(self.glossary) \
            + _correction_map_line(self.correction_map)
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": prompt + hint},
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
