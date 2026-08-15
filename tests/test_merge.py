"""Unit tests for overlap-diff merging."""
from src.merge import (common_prefix_len, diff_plan, ensure_period,
                       merge_segments, union_text)


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


def test_union_text_returns_longer_when_subsequence():
    a = "hello world this is a test goodbye world"
    b = "hello world goodbye world"
    assert union_text(a, b) == a
    assert union_text(b, a) == a


def test_union_text_keeps_unique_words():
    a = "hello world goodbye"
    b = "hello world this is a test goodbye"
    assert union_text(a, b) == b


def test_union_text_lcs_merge():
    a = "the quick brown fox"
    b = "the quick lazy dog jumps"
    out = union_text(a, b)
    for w in "quick brown fox lazy dog jumps".split():
        assert w in out.split()


def test_union_text_empty():
    assert union_text("", "hello") == "hello"
    assert union_text("hello", "") == "hello"
    assert union_text("", "") == ""


def test_union_text_rejects_single_word_noise():
    # A one-word disagreement between passes is transcription noise, not a
    # missing sentence — it must NOT be inserted into the primary.
    a = "the majority have suffered"
    b = "the majority is have suffered"
    assert union_text(a, b) == a


def test_union_text_inserts_dropped_sentence():
    # Secondary contains a whole sentence the primary missed; it must be
    # recovered even though it sits between matching context.
    primary = "hello world goodbye"
    secondary = "hello world various versions have evolved over the years goodbye"
    out = union_text(primary, secondary)
    assert "various versions have evolved over the years" in out
    assert out.startswith("hello world") and out.endswith("goodbye")


def test_merge_segments_normalized_overlap():
    # "years." vs "years" must still deduplicate (punctuation-aware overlap).
    committed, appended = merge_segments(["over", "the", "years."],
                                         "years, sometimes by accident")
    assert appended == "sometimes by accident"
    assert committed == ["over", "the", "years.", "sometimes", "by", "accident"]


def test_merge_segments_max_overlap_cap():
    committed = ["e", "f", "g", "h", "i", "j"]
    new = "e f g h i j k"
    # Cap of 3: a 6-word run (longer than any real audio overlap) can't be
    # claimed as overlap, so the whole new text is appended — nothing eaten.
    merged, appended = merge_segments(committed, new, max_overlap=3)
    assert appended == "e f g h i j k"
    assert len(merged) == len(committed) + 7
    # Without a cap the run is claimed and 'k' is the only new tail.
    merged2, appended2 = merge_segments(committed, new)
    assert appended2 == "k"