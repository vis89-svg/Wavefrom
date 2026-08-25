"""Overlap-diff merging of streaming transcription segments."""
from __future__ import annotations

import difflib
import os
from dataclasses import dataclass

PUNCT = ".!?…;:,"


def tokenize(text: str) -> list[str]:
    return text.split()


def _norm(w: str) -> str:
    """Normalized form for fuzzy matching: lowercase, punctuation stripped."""
    return "".join(c for c in w.casefold() if c.isalnum())


def fuzzy_glossary_replace(text: str, glossary: list[str], threshold: float = 0.75) -> str:
    """Replace words that are close-but-wrong spellings of glossary terms.

    Handles both single-word and multi-word glossary terms (e.g. "hippo
    potamus" -> "hippopotamus").  Multi-word matches use a slightly lower
    threshold because ASR often splits compound words.
    """
    if not glossary:
        return text
    words = text.split()
    skip = set()
    # --- multi-word terms first (greedy, longest match wins) ---
    multi = sorted(
        [t for t in glossary if " " in t], key=lambda t: -len(t.split())
    )
    for term in multi:
        t_parts = term.split()
        n = len(t_parts)
        t_normed = [_norm(p) for p in t_parts]
        for i in range(len(words) - n + 1):
            if any(j in skip for j in range(i, i + n)):
                continue
            candidate = [_norm(words[i + j]) for j in range(n)]
            # exact norm match -> already correct, skip
            if candidate == t_normed:
                continue
            avg = sum(
                difflib.SequenceMatcher(None, candidate[j], t_normed[j]).ratio()
                for j in range(n)
            ) / n
            if avg >= threshold - 0.05:
                words[i : i + n] = [term]
                skip = set()
                break
    # --- single-word terms ---
    for i, word in enumerate(words):
        if i in skip:
            continue
        normed = _norm(word)
        if not normed:
            continue
        best_term = None
        best_ratio = 0.0
        for term in glossary:
            if " " in term:
                continue
            t_norm = _norm(term)
            if t_norm == normed:
                break
            ratio = difflib.SequenceMatcher(None, normed, t_norm).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_term = term
        if best_ratio >= threshold and best_term is not None:
            words[i] = best_term
    return " ".join(words)


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


MIN_REPEAT_WORDS = 3

# A small, well-documented set of stock phrases Whisper tends to "hallucinate"
# on silence/noise/low-information audio -- an artifact of its training data
# (much of it YouTube auto-captions whose silent/outro sections were captioned
# with exactly these lines). Only ever suppressed when a slice/chunk's ENTIRE
# normalized text matches one of these, never as a substring inside a real
# sentence, so genuine speech is never touched.
_HALLUCINATION_FILLERS = {
    "thank you",
    "thanks",
    "thank you for watching",
    "thanks for watching",
    "please subscribe",
    "like and subscribe",
    "dont forget to subscribe",
    "see you next time",
    "see you in the next video",
    "thanks for listening",
    "bye bye",
    "goodbye everyone",
    "music",
    "applause",
    "laughter",
}


def is_hallucinated_filler(text: str) -> bool:
    """True if `text`'s entire normalized content is a known Whisper filler
    hallucination (see `_HALLUCINATION_FILLERS`) -- i.e. nothing else was
    transcribed in this slice/chunk besides the stock phrase."""
    words = [w for w in (_norm(w) for w in tokenize(text)) if w]
    if not words:
        return False
    return " ".join(words) in _HALLUCINATION_FILLERS


def strip_trailing_repeat(text: str) -> str:
    """Drop a trailing verbatim repeat of an earlier span (Whisper echo loop).

    A prompt echo shows up as the same words again at the end ("... every day
    we go to the store. we go to the store."). Punctuation/case differences
    still match ("years, ... years."). Only trailing spans of >=
    MIN_REPEAT_WORDS words that repeat an earlier span are removed, so real
    short stutters like "no no no" are preserved.
    """
    words = tokenize(text)
    n = len(words)
    if n < 2 * MIN_REPEAT_WORDS:
        return text
    norm = [_norm(w) for w in words]
    for k in range(n // 2, MIN_REPEAT_WORDS - 1, -1):
        tail_n = norm[-k:]
        # The earlier occurrence must not overlap the trailing echo, so its
        # start is bounded by n - 2k.
        for i in range(0, n - 2 * k + 1):
            if norm[i:i + k] == tail_n:
                return " ".join(words[:-k]).strip() or text
    return text


def collapse_adjacent_repeats(text: str) -> str:
    """Collapse adjacent verbatim repeats of a >= MIN_REPEAT_WORDS-word block.

    Whisper loops on silence/echo: "Thank you. Thank you. Thank you." comes
    back word-for-word. The largest repeating block is kept once and the extra
    copies dropped. Shorter repeats ("no no no", "the end the end") are normal
    speech and are preserved.
    """
    words = tokenize(text)
    n = len(words)
    if n < 2 * MIN_REPEAT_WORDS:
        return text
    norm = [_norm(w) for w in words]
    out: list[str] = []
    i = 0
    while i < n:
        best_k = 0
        best_copies = 1
        for k in range(MIN_REPEAT_WORDS, (n - i) // 2 + 1):
            block = norm[i:i + k]
            copies = 1
            j = i + k
            while j + k <= n and norm[j:j + k] == block:
                copies += 1
                j += k
            if copies >= 2 and k > best_k:
                best_k = k
                best_copies = copies
        if best_k:
            out.extend(words[i:i + best_k])
            i += best_k * best_copies
        else:
            out.append(words[i])
            i += 1
    return " ".join(out)


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


@dataclass
class Dispute:
    """A block where two independent transcriptions of the same audio differ.

    `primary_text`/`verify_text` are the two candidate wordings; `prefix` and
    `suffix` are the surrounding primary text for LLM adjudication.
    """
    index: int
    prefix: str
    primary_text: str
    verify_text: str
    suffix: str
    primary_start: int = 0
    primary_end: int = 0
    primary_low_conf: bool = False
    verify_low_conf: bool = False


def _word_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) char offsets of each whitespace-separated token."""
    spans: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        start = i
        while i < n and not text[i].isspace():
            i += 1
        if i > start:
            spans.append((start, i))
    return spans


def find_disputed_blocks(primary: str, verify: str,
                         min_block_words: int = 2, max_blocks: int = 12,
                         primary_low: set[str] | None = None,
                         verify_low: set[str] | None = None) -> list[Dispute]:
    """Find substitution-sized disagreements between two full transcripts.

    A dispute is a stretch where both passes said *something different* for the
    same audio. Opaque diffs are merged into blocks: short equal runs inside a
    differing stretch (e.g. the shared "the" in "on the back end" vs "in the
    list") stay part of the block, while equal runs of >= 2 words split
    disputes apart. A block qualifies when it contains at least one `replace`
    and >= `min_block_words` differing words. Single-word differences
    (homophones) are deliberately ignored — the correcting pass handles those.
    Confidence: `primary_low`/`verify_low` are sets of normalized words from
    low-confidence segments; matching words mark the candidate as uncertain.
    """
    aw = tokenize(primary)
    bw = tokenize(verify)
    an = [_norm(w) for w in aw]
    bn = [_norm(w) for w in bw]
    a_spans = _word_spans(primary)
    b_spans = _word_spans(verify)
    primary_low = primary_low or set()
    verify_low = verify_low or set()

    opcodes = list(difflib.SequenceMatcher(None, an, bn).get_opcodes())
    segments: list[list[tuple]] = []
    cur: list[tuple] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            if cur and (i2 - i1) >= 2:
                segments.append(cur)
                cur = []
            elif cur:
                cur.append((tag, i1, i2, j1, j2))
            continue
        cur.append((tag, i1, i2, j1, j2))
    if cur:
        segments.append(cur)

    disputes: list[Dispute] = []
    for seg in segments:
        non_eq = [op for op in seg if op[0] != "equal"]
        if not any(op[0] == "replace" for op in non_eq):
            continue
        diff_a = sum(i2 - i1 for tag, i1, i2, _j1, _j2 in seg
                     if tag in ("replace", "delete"))
        diff_b = sum(j2 - j1 for tag, _i1, _i2, j1, j2 in seg
                     if tag in ("replace", "insert"))
        if max(diff_a, diff_b) < min_block_words:
            continue
        a1 = non_eq[0][1]
        a2 = non_eq[-1][2]
        b1 = non_eq[0][3]
        b2 = non_eq[-1][4]
        a_start = a_spans[a1][0] if a1 < len(a_spans) else len(primary)
        a_end = a_spans[a2 - 1][1] if a2 > 0 else a_start
        b_start = b_spans[b1][0] if b1 < len(b_spans) else len(verify)
        b_end = b_spans[b2 - 1][1] if b2 > 0 else b_start
        disputes.append(Dispute(
            index=len(disputes),
            prefix=primary[:a_start],
            primary_text=primary[a_start:a_end],
            verify_text=verify[b_start:b_end],
            suffix=primary[a_end:],
            primary_start=a_start,
            primary_end=a_end,
            primary_low_conf=bool(primary_low.intersection(an[a1:a2])),
            verify_low_conf=bool(verify_low.intersection(bn[b1:b2])),
        ))
        if len(disputes) >= max_blocks:
            break
    return disputes


def apply_disputes(primary: str, disputes: list[Dispute],
                   choices: dict[int, str]) -> str:
    """Splice the chosen wording into the primary text.

    `choices` maps dispute index -> "A" (keep primary wording) or "B" (use the
    verify wording). Anything else defaults to the primary wording, so the
    result never contains words from outside the two audio-grounded candidates.
    """
    if not disputes:
        return primary
    result = primary
    for d in sorted(disputes, key=lambda d: d.primary_start, reverse=True):
        chosen = d.verify_text if choices.get(d.index) == "B" else d.primary_text
        result = result[:d.primary_start] + chosen + result[d.primary_end:]
    return result


def diff_plan(old_text: str, new_text: str) -> tuple[int, str]:
    """For a finalized replacement: (chars_to_delete, text_to_type).

    Finds the common prefix, so we backspace only the changed tail and type
    the new tail. This keeps edits minimal and avoids retyping stable text.
    """
    prefix = common_prefix_len(old_text, new_text)
    to_delete = max(0, len(old_text) - prefix)
    to_type = new_text[prefix:]
    return to_delete, to_type