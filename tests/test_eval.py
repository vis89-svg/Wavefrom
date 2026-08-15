"""Tests for the evaluation metrics (word error rate + sentence recall)."""
import pytest

from src.eval import sentence_recall, word_error_rate


def test_wer_perfect():
    assert word_error_rate("hello world", "hello world") == 0.0


def test_wer_empty_reference():
    assert word_error_rate("", "anything") == 0.0


def test_wer_some_errors():
    ref = "the quick brown fox jumps over the lazy dog"
    hyp = "the quick brown fox jumps over the dog"  # 'lazy' dropped
    assert 0.0 < word_error_rate(ref, hyp) < 0.3


def test_wer_all_wrong():
    assert word_error_rate("hello world", "goodbye cruel") == 1.0


def test_sentence_recall_all_kept():
    ref = "Hello world. This is a test. Goodbye."
    hyp = "hello world this is a test goodbye"
    recall, dropped = sentence_recall(ref, hyp)
    assert recall == 1.0
    assert dropped == []


def test_sentence_recall_dropped_sentence():
    ref = "Hello world. Various versions have evolved over the years. Goodbye."
    hyp = "hello world goodbye"
    recall, dropped = sentence_recall(ref, hyp)
    assert recall == pytest.approx(2 / 3)
    assert len(dropped) == 1
    assert "Various" in dropped[0]


def test_sentence_recall_empty():
    recall, dropped = sentence_recall("", "whatever")
    assert recall == 1.0
    assert dropped == []
