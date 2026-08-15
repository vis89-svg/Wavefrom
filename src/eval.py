"""Evaluation harness: measure dictation quality against a reference text.

Usage:
    python -m src.eval <audio.wav> <reference.txt> [--local] [--skip-cleanup] [--json]

Metrics:
    wer              word error rate vs the reference (difflib edit distance)
    sentence_recall  fraction of reference sentences fully retained (a dropped
                     sentence counts as 0) — the "keep ALL spoken content" score
    dropped_sentences  reference sentences the pipeline failed to retain
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from src.config import Settings, get_api_key, load_settings, validate
from src.merge import tokenize
from src.streaming import DictationEngine

_PUNCT_RE = re.compile(r"[^a-z0-9']+")


def words(text: str) -> list[str]:
    """Lowercased word tokens with punctuation stripped (e.g. "don't" kept)."""
    return [w for w in _PUNCT_RE.split(text.lower()) if w]


def word_error_rate(reference: str, hypothesis: str) -> float:
    import difflib

    ref = words(reference)
    hyp = words(hypothesis)
    if not ref:
        return 0.0
    sm = difflib.SequenceMatcher(None, ref, hyp)
    errors = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        elif tag == "replace":
            errors += max(i2 - i1, j2 - j1)
        elif tag == "insert":
            errors += j2 - j1
        elif tag == "delete":
            errors += i2 - i1
    return min(1.0, errors / len(ref))


def sentence_coverage(sentence: str, hypothesis: str) -> float:
    """Fraction of the sentence's words present in the hypothesis.

    Frequency-aware bag-of-words: a single spelling variant (humour -> humor)
    costs only that one word; a genuinely dropped sentence costs nearly all of
    its words and is correctly flagged.
    """
    from collections import Counter

    want = Counter(words(sentence))
    if not want:
        return 1.0
    hay = Counter(words(hypothesis))
    found = sum(min(c, hay[w]) for w, c in want.items())
    return found / sum(want.values())


def sentence_recall(reference: str, hypothesis: str,
                    threshold: float = 0.8) -> tuple[float, list[str]]:
    """(fraction of reference sentences retained, list of dropped sentences)."""
    sentences = [s.strip() for s in re.split(r"[.!?]+", reference) if s.strip()]
    dropped = [s for s in sentences if sentence_coverage(s, hypothesis) < threshold]
    retained = len(sentences) - len(dropped)
    return (retained / len(sentences)) if sentences else 1.0, dropped


def evaluate(engine: DictationEngine, wav_bytes: bytes,
             reference: str) -> dict:
    """Run a full dictation through the engine and score it against reference."""
    output = engine.dictate_bytes(wav_bytes)
    wer = word_error_rate(reference, output)
    recall, dropped = sentence_recall(reference, output)
    return {
        "output": output,
        "wer": round(wer, 4),
        "sentence_recall": round(recall, 4),
        "words_reference": len(words(reference)),
        "words_output": len(words(output)),
        "dropped_sentences": dropped,
        "disputed_blocks": _dispute_count(engine),
    }


def _build_engine(settings: Settings, api_key: str) -> DictationEngine:
    from src.cleanup import CleanupClient
    from src.local_engine import LocalWhisperEngine
    from src.transcribe import TranscriptionClient

    if settings.local_engine:
        transcriber = LocalWhisperEngine(settings.local_model,
                                         vad_filter=settings.vad_filter)
    else:
        transcriber = TranscriptionClient(api_key, model=settings.whisper_model)

    cleaner = None
    if settings.cleanup_model and not settings.local_engine:
        cleaner = CleanupClient(api_key, model=settings.cleanup_model,
                                mode=settings.cleanup_mode)
    return DictationEngine(settings, transcriber, cleaner=cleaner,
                           injector=None, notify=None, tray=None)


def _dispute_count(engine: DictationEngine) -> int:
    """How many substitution-sized disagreements the last dictation found."""
    return len(getattr(engine, "_last_disputes", []))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path)
    parser.add_argument("reference", type=Path)
    parser.add_argument("--local", action="store_true", help="use local faster-whisper")
    parser.add_argument("--skip-cleanup", action="store_true")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip the two-model verify/reconcile pass")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not args.wav.is_file():
        print(f"Missing audio file: {args.wav}", file=sys.stderr)
        return 1
    if not args.reference.is_file():
        print(f"Missing reference text: {args.reference}", file=sys.stderr)
        return 1

    settings = load_settings()
    if args.local:
        settings.local_engine = True
    if args.skip_cleanup:
        settings.cleanup_model = None
    if args.no_verify:
        settings.verify = False
    api_key = get_api_key()
    problems = validate(settings, api_key)
    if problems and not settings.local_engine:
        print(problems[0], file=sys.stderr)
        return 1

    engine = _build_engine(settings, api_key)
    reference = args.reference.read_text(encoding="utf-8").strip()
    result = evaluate(engine, args.wav.read_bytes(), reference)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"word error rate:      {result['wer']:.1%}")
        print(f"sentence recall:      {result['sentence_recall']:.1%} "
              f"({result['words_reference'] - result['words_output']:+d} words)")
        print(f"disputed blocks:      {result['disputed_blocks']}")
        if result["dropped_sentences"]:
            print("DROPPED sentences:")
            for s in result["dropped_sentences"]:
                print(f"  - {s}")
        print("\n--- output ---")
        print(result["output"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
