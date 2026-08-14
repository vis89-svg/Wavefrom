"""Overlap-diff merging of streaming transcription segments."""
from __future__ import annotations

import difflib
import os

PUNCT = ".!?…;:,"


def tokenize(text: str) -> list[str]:
    return text.split()


def merge_segments(committed: list[str], new_text: str) -> tuple[list[str], str]:
    """Merge a new transcript into already-committed words.

    Returns (updated_committed, appended_raw_text). The appended text is the
    non-overlapping tail of `new_text` — exactly what must be typed next.
    """
    new_words = tokenize(new_text)
    if not new_words:
        return committed, ""
    if not committed:
        return new_words, new_text

    best_overlap = 0
    best_ratio = 0.0
    max_o = min(len(committed), len(new_words))
    for o in range(1, max_o + 1):
        a = [w.casefold() for w in committed[-o:]]
        b = [w.casefold() for w in new_words[:o]]
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        if ratio >= 0.6 and ratio > best_ratio:
            best_ratio = ratio
            best_overlap = o

    append_at = 0 if best_overlap == 0 else best_overlap
    merged = committed + new_words[append_at:]
    appended = " ".join(new_words[append_at:])
    return merged, appended


def ensure_period(committed: list[str]) -> list[str]:
    """Append '.' when the last word doesn't end with punctuation."""
    if not committed:
        return committed
    last = committed[-1]
    if last[-1] not in PUNCT:
        committed = committed + ["."]
    return committed


def common_prefix_len(a: str, b: str) -> int:
    return len(os.path.commonprefix([a, b]))


def diff_plan(old_text: str, new_text: str) -> tuple[int, str]:
    """For a finalized replacement: (chars_to_delete, text_to_type).

    Finds the common prefix, so we backspace only the changed tail and type
    the new tail. This keeps edits minimal and avoids retyping stable text.
    """
    prefix = common_prefix_len(old_text, new_text)
    to_delete = max(0, len(old_text) - prefix)
    to_type = new_text[prefix:]
    return to_delete, to_type