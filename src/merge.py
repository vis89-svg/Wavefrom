"""Overlap-diff merging of streaming transcription segments."""
from __future__ import annotations

import difflib
import os

PUNCT = ".!?…;:,"


def tokenize(text: str) -> list[str]:
    return text.split()


def _norm(w: str) -> str:
    """Normalized form for fuzzy matching: lowercase, punctuation stripped."""
    return "".join(c for c in w.casefold() if c.isalnum())


def merge_segments(committed: list[str], new_text: str,
                   max_overlap: int | None = None) -> tuple[list[str], str]:
    """Merge a new transcript into already-committed words.

    Returns (updated_committed, appended_raw_text). The appended text is the
    non-overlapping tail of `new_text` — exactly what must be typed next.

    Overlap matching is done on punctuation/case-normalized tokens so that
    boundary re-transcriptions like "years," vs "years" still deduplicate.
    `max_overlap` bounds how many committed words may be claimed as overlap;
    the true audio overlap is small (a fraction of a second), so a larger
    bound is a guard against coincidental over-dedup that would drop content.
    """
    new_words = tokenize(new_text)
    if not new_words:
        return committed, ""
    if not committed:
        return new_words, new_text

    best_overlap = 0
    best_ratio = 0.0
    max_o = min(len(committed), len(new_words))
    if max_overlap is not None:
        max_o = min(max_o, max_overlap)
    for o in range(1, max_o + 1):
        a = [_norm(w) for w in committed[-o:]]
        b = [_norm(w) for w in new_words[:o]]
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


MIN_GAP_BLOCK = 3


def union_text(a: str, b: str) -> str:
    """Merge two full transcripts of the same audio, keeping ALL spoken content.

    `a` is the primary transcript (kept as-is); `b` is secondary. If either is
    a (normalized) subsequence of the other, the longer one is returned — the
    common case when one pass dropped nothing. Otherwise `b`'s content is
    merged in ONLY as contiguous blocks of >= MIN_GAP_BLOCK words that `a` is
    missing, anchored by matching context on both sides. This recovers dropped
    sentences while never inserting stray single-word disagreements ("is").
    """
    aw = tokenize(a)
    bw = tokenize(b)
    if not aw:
        return b
    if not bw:
        return a
    # Fast path: the secondary says nothing the primary already covers.
    if _is_subsequence(bw, aw):
        return a
    return " ".join(_gap_fill(aw, bw))


def _is_subsequence(needle: list[str], hay: list[str]) -> bool:
    nw = [_norm(w) for w in needle]
    hw = [_norm(w) for w in hay]
    it = iter(hw)
    return all(n in it for n in nw)


def _gap_fill(a: list[str], b: list[str]) -> list[str]:
    """Keep all of `a`; insert b-only runs of >= MIN_GAP_BLOCK words where they
    fall between matching context in the LCS alignment."""
    ops = _align(a, b)
    out: list[str] = []
    i = 0
    while i < len(ops):
        op, w = ops[i]
        if op == "ins":
            run = []
            while i < len(ops) and ops[i][0] == "ins":
                run.append(ops[i][1])
                i += 1
            if len(run) >= MIN_GAP_BLOCK:
                out.extend(run)
            continue
        out.append(w)
        i += 1
    return out


def _align(a: list[str], b: list[str]) -> list[tuple[str, str]]:
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m - 1, -1, -1):
        for j in range(n - 1, -1, -1):
            if _norm(a[i]) == _norm(b[j]):
                dp[i][j] = dp[i + 1][j + 1] + 1
            else:
                dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])
    ops: list[tuple[str, str]] = []
    i = j = 0
    while i < m and j < n:
        if _norm(a[i]) == _norm(b[j]):
            ops.append(("eq", a[i]))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            ops.append(("del", a[i]))
            i += 1
        else:
            ops.append(("ins", b[j]))
            j += 1
    while i < m:
        ops.append(("del", a[i]))
        i += 1
    while j < n:
        ops.append(("ins", b[j]))
        j += 1
    return ops


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