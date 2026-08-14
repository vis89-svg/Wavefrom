"""Unit tests for overlap-diff merging."""
from src.merge import common_prefix_len, diff_plan, ensure_period, merge_segments


def test_merge_appends_new_tail():
    committed, appended = merge_segments(["hello", "world"], "world today is sunny")
    assert committed == ["hello", "world", "today", "is", "sunny"]
    assert appended == "today is sunny"


def test_merge_no_overlap_prepends():
    committed, appended = merge_segments(["alpha"], "beta gamma")
    assert committed == ["alpha", "beta", "gamma"]
    assert appended == "beta gamma"


def test_merge_empty_new():
    committed, appended = merge_segments(["a"], "")
    assert committed == ["a"]
    assert appended == ""


def test_merge_empty_committed():
    committed, appended = merge_segments([], "first words here")
    assert committed == ["first", "words", "here"]
    assert appended == "first words here"


def test_merge_fuzzy_overlap():
    committed = ["the", "quick", "brown", "fox"]
    new = "brown fox jumped over the lazy dog"
    merged, appended = merge_segments(committed, new)
    assert appended.startswith("jumped")
    assert merged == ["the", "quick", "brown", "fox", "jumped", "over", "the", "lazy", "dog"]


def test_ensure_period():
    assert ensure_period(["hello"]) == ["hello", "."]
    assert ensure_period(["hello"]) == ensure_period(["hello", "."])
    assert ensure_period(["ok!"]) == ["ok!"]
    assert ensure_period(["ok"]) == ["ok", "."]


def test_common_prefix_len():
    assert common_prefix_len("hello world", "hello there") == 6
    assert common_prefix_len("abc", "xyz") == 0


def test_diff_plan():
    delete, typed = diff_plan("hello world foo", "hello world bar")
    assert delete == 3
    assert typed == "bar"
    delete, typed = diff_plan("same", "same")
    assert delete == 0 and typed == ""